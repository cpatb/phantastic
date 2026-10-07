"""Crop, TIFF stack / sequence, MP4 and file naming, on synthetic cines and the simulated camera.

Anchors that do not pass through the code under test: the pattern each synthetic image is built
from (``pattern``: value = row*W + col + 7k, so every row and column is identifiable), the
simulator's ``synthetic_frame`` (row 0 = image number mod 4096), plain numpy slices, and ffprobe /
imageio-ffmpeg for what an MP4 actually holds.
"""
import json
import shutil
import struct
import subprocess

import numpy as np
import pytest
import tifffile

from phantastic.camera import Camera
from phantastic.cine import CineReader, CineWriter
from phantastic.crop import check_crop, parse_crop
from phantastic.decimate import decimate_cine, export_tiff
from phantastic.export import (CameraFrames, FileFrames, export_mp4, export_tiff_sequence, find_ffmpeg,
                               window_render, write_mp4, write_tiff_sequence, write_tiff_stack)
from phantastic.naming import DEFAULT_CINE_TEMPLATE, expand_name, tokens_in, unique_path
from phantastic.simulator import CameraModel, Simulator, synthetic_frame

W, H, N, FIRST = 33, 21, 6, -3          # odd sizes on purpose
T0 = 1_760_000_000


def pattern(k, packing='mono16', width=W):
    rr, cc = np.mgrid[0:H, 0:width]
    v = rr * width + cc + 7 * k
    if packing == 'mono8':
        return (v % 256).astype(np.uint8)
    if packing == 'packed10':
        return (v % 1024).astype(np.uint16)              # 10-bit codes
    if packing == 'bgr24':
        return np.stack([v % 256, (v + 1) % 256, (v + 2) % 256], axis=-1).astype(np.uint8)
    return (v % 4096).astype(np.uint16)


def make_cine(path, packing='mono16', desc='synthetic', width=W):
    with CineWriter(path, width, H, N, packing, first_image_no=FIRST, trigger_time=(T0, 0),
                    setup_fields={'FrameRate': 10000, 'RealBPP': 8 if packing in ('mono8', 'bgr24') else 12,
                                  'Description': desc, 'ImWidth': width, 'ImHeight': H,
                                  'ImWidthAcq': width, 'ImHeightAcq': H, 'ShutterNs': 40000}) as w:
        for k in range(N):
            n = FIRST + k
            t64 = (T0 << 32) + n * (1 << 32) // 10000      # 100 µs apart, image 0 at the trigger
            w.append(pattern(k, packing, width), time=(t64 >> 32, t64 & 0xFFFFFFFF), exposure=1000 + k)
    return path


# ----------------------------------------------------------------------------- crop

@pytest.mark.parametrize('packing', ['mono16', 'mono8', 'bgr24'])
@pytest.mark.parametrize('crop', [(4, 3, 11, 7), (0, 0, W, 10), (32, 0, 1, H), (5, 20, 9, 1), (0, 0, W, H)])
def test_decimate_crop_is_the_numpy_slice(tmp_path, packing, crop):
    check_decimate_crop(tmp_path, packing, crop, W)


@pytest.mark.parametrize('packing', ['packed10', 'packed12L'])
@pytest.mark.parametrize('crop', [(3, 3, 12, 7), (0, 0, 36, 10), (1, 0, 4, H), (31, 20, 4, 1), (0, 0, 36, H)])
def test_decimate_crop_packed(tmp_path, packing, crop):
    """Packed cines (width 36: whole 4-pixel P10 groups); any x, widths in whole packed groups."""
    check_decimate_crop(tmp_path, packing, crop, 36)


@pytest.mark.parametrize('packing,w', [('packed10', 6), ('packed10', 1), ('packed12L', 3)])
def test_decimate_crop_packed_partial_group_refused(tmp_path, packing, w):
    src = make_cine(tmp_path / 'src.cine', packing, width=36)
    with pytest.raises(ValueError, match='multiple of'):
        decimate_cine(src, tmp_path / 'c.cine', 1, crop=(1, 1, w, 2))
    assert not (tmp_path / 'c.cine').exists() and not (tmp_path / 'c.cine.part').exists()
    export_tiff(src, tmp_path / 'c.tif', crop=(1, 1, w, 2))          # TIFF holds any width
    lut = __import__('phantastic.cine', fromlist=['load_linlut10']).load_linlut10()
    want = pattern(0, packing, 36)[1:3, 1:1 + w]
    assert np.array_equal(tifffile.imread(tmp_path / 'c.tif')[0], lut[want] if packing == 'packed10' else want)


def check_decimate_crop(tmp_path, packing, crop, width):
    src = make_cine(tmp_path / 'src.cine', packing, width=width)
    dst = tmp_path / 'crop.cine'
    res = decimate_cine(src, dst, 1, crop=crop)
    x, y, w, h = crop
    assert res['crop'] == crop and not (tmp_path / 'crop.cine.part').exists()
    with CineReader(dst) as r, CineReader(src) as s:
        assert (r.width, r.height, len(r), r.first, r.packing) == (w, h, N, FIRST, packing)
        for k in range(N):
            want = pattern(k, packing, width)[y:y + h, x:x + w]
            got = r.read_codes10(k) if packing == 'packed10' else r.read(k)
            assert np.array_equal(got, want), k
        assert np.array_equal(r.image_times_raw(), s.image_times_raw())        # true times kept
        assert np.array_equal(r.exposures_raw(), s.exposures_raw())
        assert (r.setup['ImWidth'], r.setup['ImHeight']) == (w, h)
        assert (r.setup['ImWidthAcq'], r.setup['ImHeightAcq']) == (width, H)    # the recorded size
        assert (r.setup['ImPosXAcq'], r.setup['ImPosYAcq']) == (0, 0)           # deliberately untouched
        assert f'Cropped from {width}x{H} at {x},{y} to {w}x{h}' in r.setup['Description']
        assert r.setup['Description'].startswith('synthetic')


def test_crop_bottom_up_storage_does_not_flip(tmp_path):
    """BI_RGB 16-bit is stored bottom-up: rows 0..9 of the top-down image must stay the TOP rows,
    and on disk the first stored row of the cropped file must be top-down row 9 (read the raw bytes)."""
    src = make_cine(tmp_path / 'src.cine')
    with CineReader(src) as r:
        assert r.bottom_up
    dst = tmp_path / 'top.cine'
    decimate_cine(src, dst, 1, crop=(0, 0, W, 10))
    with CineReader(dst) as r:
        assert r.bottom_up
        assert np.array_equal(r.read(0)[:, 0], np.arange(10) * W)      # column 0 = row*W: rows 0..9
        stored = np.frombuffer(r.stored_bytes(0), '<u2').reshape(10, W)
        assert np.array_equal(stored[0], pattern(0)[9]) and np.array_equal(stored[-1], pattern(0)[0])


def test_crop_rejects_out_of_range_and_writes_nothing(tmp_path):
    src = make_cine(tmp_path / 'src.cine')
    for bad in [(0, 0, W + 1, 1), (1, 0, W, 1), (0, 21, 1, 1), (-1, 0, 2, 2), (0, 0, 0, 5), (0, 0, 2.5, 2), (1, 2, 3)]:
        with pytest.raises(ValueError):
            decimate_cine(src, tmp_path / 'x.cine', 1, crop=bad)
        with pytest.raises(ValueError):
            export_tiff(src, tmp_path / 'x.tif', crop=bad)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['src.cine']
    assert check_crop(None, 5, 5) is None and check_crop((1.0, 2, 3, 3), 5, 5) == (1, 2, 3, 3)
    assert parse_crop(' 1, 2,3 ,4') == (1, 2, 3, 4)
    with pytest.raises(ValueError):
        parse_crop('1,2,3')


def test_decimate_with_crop_and_step(tmp_path):
    src = make_cine(tmp_path / 'src.cine')
    dst = tmp_path / 'd.cine'
    res = decimate_cine(src, dst, 2, crop=(1, 1, 5, 5))
    with CineReader(dst) as r:
        assert r.first == res['first_out'] == -1 and len(r) == 3        # images -2, 0, 2 -> -1, 0, 1
        for k, n in enumerate((-2, 0, 2)):
            assert np.array_equal(r.read(k), pattern(n - FIRST)[1:6, 1:6])
        assert 'image k = source image k*2+0' in r.setup['Description']


# ----------------------------------------------------------------------------- TIFF

def test_tiff_stack_crop_and_times(tmp_path):
    src = make_cine(tmp_path / 'src.cine')
    out = tmp_path / 'c.tif'
    res = export_tiff(src, out, crop=(2, 3, 7, 4))
    pages = tifffile.imread(out)
    assert pages.shape == (N, 4, 7)
    for k in range(N):
        assert np.array_equal(pages[k], pattern(k)[3:7, 2:9])
    meta = json.loads((tmp_path / 'c.tif.json').read_text())
    assert meta['crop'] == {'x': 2, 'y': 3, 'w': 7, 'h': 4, 'source_width': W, 'source_height': H,
                            'convention': '0-based, rows top-down as CineReader.read returns them'}
    assert meta['image_numbers'] == list(range(FIRST, FIRST + N))
    assert np.allclose(meta['time_rel_trigger_s'], np.arange(FIRST, FIRST + N) * 1e-4, atol=1e-9)
    assert meta['times_from'] == 'per-image time stamps' and res['finterval_s'] == pytest.approx(1e-4)
    assert not list(tmp_path.glob('*.part'))


def test_tiff_sequence_names_and_pixels(tmp_path):
    src = make_cine(tmp_path / 'shot.cine')
    out = tmp_path / 'seq'
    res = export_tiff_sequence(src, out)
    names = [f'shot_{"m" if n < 0 else ""}{abs(n):06d}.tif' for n in range(FIRST, FIRST + N)]
    assert res['files'] == names
    assert sorted(p.name for p in out.iterdir()) == sorted(names + ['shot_sequence.json'])   # no temp folder left
    for k, name in enumerate(names):
        with tifffile.TiffFile(out / name) as t:
            assert np.array_equal(t.asarray(), pattern(k))
            page = json.loads(t.pages[0].description)
        assert page['image_number'] == FIRST + k
        assert page['time_rel_trigger_s'] == pytest.approx((FIRST + k) * 1e-4, abs=1e-9)
    side = json.loads((out / 'shot_sequence.json').read_text())
    assert side['files'] == names and side['image_numbers'] == list(range(FIRST, FIRST + N))
    # existing files are never replaced, and nothing at all is written then
    before = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}
    with pytest.raises(FileExistsError):
        export_tiff_sequence(src, out)
    assert {p.name: p.stat().st_mtime_ns for p in out.iterdir()} == before
    # {+image}: non-negative, sortable; crop and step apply
    res2 = export_tiff_sequence(src, tmp_path / 'seq2', pattern='img_{+image4}', step=2, crop=(0, 0, 3, 2))
    assert res2['files'] == ['img_0000.tif', 'img_0002.tif', 'img_0004.tif']   # images -2, 0, 2
    assert np.array_equal(tifffile.imread(tmp_path / 'seq2' / 'img_0002.tif'), pattern(0 - FIRST)[:2, :3])
    with pytest.raises(ValueError, match='image'):
        export_tiff_sequence(src, tmp_path / 'seq3', pattern='{source}')


def test_tiff_sequence_failure_leaves_nothing(tmp_path):
    src = make_cine(tmp_path / 'shot.cine')
    out = tmp_path / 'seq'

    def boom(done, total):
        if done == 3:
            raise RuntimeError('stop')
    with pytest.raises(RuntimeError):
        export_tiff_sequence(src, out, progress=boom)
    assert list(out.iterdir()) == []


# ----------------------------------------------------------------------------- naming

def test_expand_name():
    f = dict(cinenr=1, serial=99001, camname='Lab cam: v2512', date='Y20261007', time='H153012', count=2)
    assert expand_name(DEFAULT_CINE_TEMPLATE, **f) == 'cine1_99001'          # today's default name
    assert expand_name('{name}_{cinenr3}_{count}_{date}{time}', **f) == 'Lab cam_ v2512_001_2_Y20261007H153012'
    assert expand_name('i{image6}', image=-123) == 'im000123'
    assert expand_name('i{image:06d}', image=45) == 'i000045'
    assert expand_name('i{image2}', image=12345) == 'i12345'                 # width grows, value kept (p.69)
    assert expand_name('{+image4}', image_pos=7) == '0007'
    assert expand_name(r'C:\data\{source}.tif', source='a/b.c') == r'C:\data\a_b_c.tif'
    assert tokens_in('{name}{+image2}{cinenr}') == {'camname', 'image_pos', 'cinenr'}
    for bad, msg in [('{nope}', 'unknown token'), ('{serial}', 'no value'), ('{camname3}', 'only to numbers'),
                     ('a{b', 'unbalanced'), ('a}', 'unbalanced'), ('{image3:04d}', 'twice'), ('{}', 'malformed')]:
        with pytest.raises(ValueError, match=msg):
            expand_name(bad, camname='x')


def test_unique_path(tmp_path):
    p = tmp_path / 'a.cine'
    assert unique_path(p) == p
    p.write_bytes(b'x')
    assert unique_path(p) == tmp_path / 'a_1.cine'
    (tmp_path / 'a_1.cine').write_bytes(b'x')
    assert unique_path(p) == tmp_path / 'a_2.cine'


# ----------------------------------------------------------------------------- MP4

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason='no ffmpeg with libx264')


def ffprobe(path):
    exe = shutil.which('ffprobe')
    if exe is None:
        pytest.skip('ffprobe not on PATH')
    out = subprocess.run([exe, '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json',
                          str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def mp4_frames(path):
    import imageio_ffmpeg
    gen = imageio_ffmpeg.read_frames(str(path))
    meta = next(gen)
    w, h = meta['size']
    return meta, [np.frombuffer(f, np.uint8).reshape(h, w, 3) for f in gen]


@needs_ffmpeg
def test_mp4_frames_size_fps_and_render(tmp_path):
    src = make_cine(tmp_path / 's.cine')
    dst = tmp_path / 'm.mp4'
    res = export_mp4(src, dst, fps=25, black=0, white=800, crop=(0, 0, 31, 21))      # odd 31x21 -> 32x22
    assert res['size'] == (32, 22) and res['count'] == N and not list(tmp_path.glob('*.part'))
    info = ffprobe(dst)
    st = info['streams'][0]
    assert (st['codec_name'], st['pix_fmt'], st['width'], st['height']) == ('h264', 'yuv420p', 32, 22)
    assert int(st['nb_read_frames']) == N and st['avg_frame_rate'] == '25/1'
    assert 'NOT FOR MEASUREMENT' in info['format']['tags']['comment']
    side = json.loads((tmp_path / 'm.mp4.json').read_text())
    assert side['image_numbers'] == list(range(FIRST, FIRST + N)) and side['pad_right_bottom_px'] == [1, 1]
    meta, frames = mp4_frames(dst)
    assert len(frames) == N and tuple(meta['size']) == (32, 22)
    want = np.clip(pattern(0)[:, :31] / 800 * 255, 0, 255)          # the render, computed independently
    got = frames[0][:21, :31].mean(axis=2)
    assert abs(got.mean() - want.mean()) < 3                        # lossy codec: mean, not pixels
    assert frames[0][:21, 31].mean() < 8 and frames[0][21].mean() < 8      # padding is black, not stretched


@needs_ffmpeg
def test_mp4_border_burns_text_below_image(tmp_path):
    src = make_cine(tmp_path / 's.cine')
    plain = export_mp4(src, tmp_path / 'p.mp4', black=0, white=800)
    burnt = export_mp4(src, tmp_path / 'b.mp4', black=0, white=800, border=True)
    _, fp = mp4_frames(tmp_path / 'p.mp4')
    _, fb = mp4_frames(tmp_path / 'b.mp4')
    assert plain['size'] == (34, 22)
    bw, bh = burnt['size']
    assert bh > H + 20 and bw >= W
    strip = fb[0][H + 2:, :].astype(int)
    assert strip.max() > 200 and (strip > 128).sum() > 50           # text is there
    img = fb[0][:H, :W].astype(int)
    assert abs(img.mean() - fp[0][:H, :W].astype(int).mean()) < 3   # the image area is the same render
    side = json.loads((tmp_path / 'b.mp4.json').read_text())
    assert side['border']['time_unit'] == 'us' and side['border']['position'].startswith('strip below')


@needs_ffmpeg
def test_mp4_rotate_flip_and_render_hook(tmp_path):
    src = make_cine(tmp_path / 's.cine')
    calls = []

    def hook(frame):
        calls.append(frame.shape)
        return np.where(frame > 300, 255, 0).astype(np.uint8)
    res = export_mp4(src, tmp_path / 'r.mp4', render=hook, rotate=90, crop=(0, 0, 20, 10))
    assert calls == [(10, 20)] * N and res['size'] == (10, 20)          # rotated: W and H swap
    _, fr = mp4_frames(tmp_path / 'r.mp4')
    want = np.rot90(np.where(pattern(0)[:10, :20] > 300, 255, 0), k=-1)
    assert np.abs(fr[0].mean(axis=2) - want).mean() < 20
    with pytest.raises(ValueError):
        export_mp4(src, tmp_path / 'x.mp4', rotate=45)
    assert not (tmp_path / 'x.mp4').exists()


def test_window_render():
    r = window_render(100, 300, 1.0)
    assert np.array_equal(r(np.array([0, 100, 200, 300, 4000])), [0, 0, 128, 255, 255])
    g = window_render(0, 100, 2.0)
    assert g(np.array([25]))[0] == round(255 * 0.25 ** 0.5)
    with pytest.raises(ValueError):
        window_render(5, 5)


def test_mp4_without_ffmpeg_is_refused(tmp_path):
    src = make_cine(tmp_path / 's.cine')
    with FileFrames(src) as fr:
        with pytest.raises(RuntimeError, match='ffmpeg'):
            write_mp4(fr, tmp_path / 'x.mp4', ffmpeg=None) if find_ffmpeg() is None else \
                write_mp4(fr, tmp_path / 'x.mp4', ffmpeg=str(tmp_path / 'no_such_ffmpeg.exe'))
    assert list(tmp_path.glob('x.mp4*')) == []


# ----------------------------------------------------------------------------- camera

@pytest.fixture()
def cam():
    s = Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0, model=CameraModel(width=64, height=32)).start()
    with Camera('127.0.0.1', s.port, timeout=5) as c:
        yield c
    s.stop()


def record(cam, cine=1, frcount=40, ptframes=10):
    cam.command(f'set defc {{frcount:{frcount}, ptframes:{ptframes}}}')
    cam.record(cine)
    cam.trigger()
    cam.wait_stored(cine, timeout=2)
    return cam.cine_info(cine)


def test_camera_download_crop(cam, tmp_path):
    record(cam)
    out = tmp_path / 'c.cine'
    res = cam.download(1, out, first=-5, last=4, fmt='P16', as_12bit=True, crop=(3, 0, 17, 9))
    assert res['crop'] == (3, 0, 17, 9) and (res['width'], res['height']) == (17, 9)
    with CineReader(out) as r:
        assert (r.width, r.height, len(r), r.first) == (17, 9, 10, -5)
        for k, n in enumerate(range(-5, 5)):
            assert np.array_equal(r.read(k), synthetic_frame(n, 64, 32)[0:9, 3:20]), n
            assert r.read(k)[0, 0] == synthetic_frame(n, 64, 32)[0, 3]       # row 0 = top (number ramp)
        assert 'Cropped from 64x32 at 3,0 to 17x9' in r.setup['Description']
        assert (r.setup['ImWidthAcq'], r.setup['ImHeightAcq']) == (64, 32)
    with pytest.raises(ValueError):
        cam.download(1, tmp_path / 'bad.cine', crop=(60, 0, 5, 5))
    assert not (tmp_path / 'bad.cine').exists()


@pytest.mark.parametrize('fmt,as12,conv', [('P16', True, lambda f: f), ('P16', False, lambda f: f << 4),
                                           ('8', False, lambda f: (f >> 4).astype(np.uint8))])
def test_camera_tiff_straight_from_ram(cam, tmp_path, fmt, as12, conv):
    record(cam)
    with CameraFrames(cam, 1, first=-4, last=3, step=2, fmt=fmt, as_12bit=as12) as fr:
        assert fr.times_from.startswith('per-image time stamps')
        res = write_tiff_stack(fr, tmp_path / 'ram.tif', crop=(0, 0, 10, 4))
    pages = tifffile.imread(tmp_path / 'ram.tif')
    nums = [-4, -2, 0, 2]
    assert pages.shape == (4, 4, 10)
    for k, n in enumerate(nums):
        assert np.array_equal(pages[k], conv(synthetic_frame(n, 64, 32))[:4, :10])
    meta = json.loads((tmp_path / 'ram.tif.json').read_text())
    assert meta['image_numbers'] == nums and meta['source'].startswith('camera 99001 cine 1')
    # true times: the simulator stamps images 1/rate apart, the trigger at image 0
    rate = float(cam.cine_info(1)['rate'])
    assert np.allclose(meta['time_rel_trigger_s'], np.array(nums) / rate, atol=2e-6)
    if as12:
        assert res['dropped_low_bits'] == [0, 4 * 4 * 10]       # simulator P16 is exactly 12-bit x 16


def test_camera_sequence_and_download_agree(cam, tmp_path):
    record(cam)
    cam.download(1, tmp_path / 'd.cine', first=-2, last=2, fmt='P16', as_12bit=True)
    with CameraFrames(cam, 1, first=-2, last=2, fmt='P16', as_12bit=True) as fr:
        write_tiff_sequence(fr, tmp_path / 'seq', fields={'serial': 99001})
    with CineReader(tmp_path / 'd.cine') as r:
        for k, n in enumerate(range(-2, 3)):
            name = f'cine1_99001_{"m" if n < 0 else ""}{abs(n):06d}.tif'
            assert np.array_equal(tifffile.imread(tmp_path / 'seq' / name), r.read(k))


@needs_ffmpeg
def test_camera_mp4(cam, tmp_path):
    record(cam)
    with CameraFrames(cam, 1, first=0, last=9, fmt='P16', as_12bit=True) as fr:
        res = write_mp4(fr, tmp_path / 'cam.mp4', fps=30, black=0, white=4095, border=True)
    assert res['count'] == 10
    assert int(ffprobe(tmp_path / 'cam.mp4')['streams'][0]['nb_read_frames']) == 10


def test_download_all(cam, tmp_path):
    cam.partition(3)
    for c in (1, 2):
        record(cam, c, frcount=20, ptframes=5)
    assert cam.stored_cines() == [1, 2]
    (tmp_path / 'cine1_99001.cine').write_bytes(b'keep me')
    res = cam.download_all(tmp_path, fmt='P16', as_12bit=True)
    assert [r['path'] for r in res] == [str(tmp_path / 'cine1_99001_1.cine'), str(tmp_path / 'cine2_99001.cine')]
    assert (tmp_path / 'cine1_99001.cine').read_bytes() == b'keep me'          # never overwritten
    for r in res:
        with CineReader(r['path']) as cr:
            assert len(cr) == 20 and cr.first == -15
            cine = int(r['cine'])
            assert np.array_equal(cr.read(0), synthetic_frame(-15, 64, 32, seed=cine))   # the simulator seeds by cine
    res2 = cam.download_all(tmp_path / 'b', template='{count}_x')        # no {cinenr}: appended as PCC does
    assert [r['path'].rsplit('\\', 1)[-1].rsplit('/', 1)[-1] for r in res2] == ['1_x_Cine1.cine', '2_x_Cine2.cine']
    assert not list(tmp_path.rglob('*.part'))


def test_cine_header_records_crop_offset_in_description_only(tmp_path):
    """The crop offset is in the Description; ImPosXAcq/ImPosYAcq (SETUP offsets 1580/1584) stay as in the source."""
    src = make_cine(tmp_path / 'src.cine')
    decimate_cine(src, tmp_path / 'c.cine', 1, crop=(7, 5, 3, 3))
    with CineReader(tmp_path / 'c.cine') as r:
        raw = r.setup_raw
    assert struct.unpack_from('<II', raw, 1580) == (0, 0)
    assert struct.unpack_from('<HH', raw, 737) == (3, 3)
