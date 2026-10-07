"""Camera client against the simulated camera (end-to-end over real sockets on localhost).

These tests exercise framing, the data stream (both connection methods), every wire format,
time stamps and lossless download. They are a self-consistency check: the simulator and client
were written from the same reading of the spec. Real-camera ground truth is limited to the
transcript parsing in test_protocol.py until a camera is connected.
"""
import numpy as np
import pytest

from phantastic import protocol as P
from phantastic.camera import Camera, discover
from phantastic.cine import CineReader, load_linlut10
from phantastic.simulator import CameraModel, Simulator, synthetic_frame


@pytest.fixture()
def sim():
    s = Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0, model=CameraModel(width=64, height=32)).start()
    yield s
    s.stop()


@pytest.fixture()
def cam(sim):
    with Camera('127.0.0.1', sim.port, timeout=5) as c:
        yield c


def recorded(cam, cine=1, frcount=120, ptframes=20):
    cam.command(f'set defc {{frcount:{frcount}, ptframes:{ptframes}}}')
    cam.record(cine)
    assert 'WTR' in cam.get(f'c{cine}.state')
    cam.trigger()
    cam.wait_stored(cine, timeout=2)
    return cam.cine_info(cine)


def test_discovery(sim):
    found = discover(timeout=0.5, broadcast=('127.0.0.1',), port=sim.udp.getsockname()[1])
    assert len(found) == 1 and found[0].serial == 99001 and found[0].port == sim.port
    assert found[0].name == 'Phantastic Simulator'


def test_get_set_configure(cam):
    info = cam.info()
    assert info['pver'] == 16 and 'P16' in cam.image_formats()
    d = cam.configure(resolution=(128, 64), rate=20000, exposure_ns=40000, post_trigger=50)
    assert d['res'] == P.Resolution(128, 64) and d['rate'] == 20000 and d['exp'] == 40000 and d['ptframes'] == 50
    with pytest.raises(P.ProtocolError, match='unknown'):
        cam.get('defc.nonsense')
    assert cam.transcript[-1][0] == 'get defc.nonsense'


def test_record_trigger_numbering(cam):
    ci = recorded(cam, frcount=120, ptframes=20)
    assert ci['firstfr'] == -100 and ci['lastfr'] == 19   # trigger = 0, pre-trigger negative
    assert 'STR' in ci['state']
    states = cam.cine_states()
    assert 'STR' in states['c1'] and 'ACT' in states['c2']


@pytest.mark.parametrize('method', ['startdata', 'attach'])
@pytest.mark.parametrize('fmt', ['P16', 'P16R', '8', 'P10', 'P12L'])
def test_img_formats(sim, cam, method, fmt):
    recorded(cam)
    cam.open_data(method, attach_port=sim.attach_port)
    frames, hdr = cam.read_images(1, -5, 4, fmt)
    assert frames.shape == (4, 32, 64) and hdr['res'] == P.Resolution(64, 32)
    want = np.stack([synthetic_frame(n, 64, 32, seed=1) for n in range(-5, -1)])
    if fmt in ('P16', 'P16R'):
        assert np.array_equal(frames, want << 4)          # MSB-aligned on the wire
    elif fmt == 'P12L':
        assert np.array_equal(frames, want)
    elif fmt == '8':
        assert np.array_equal(frames, (want >> 4).astype(np.uint8))
    else:  # P10 codes, linearised: within the companding step of the original
        lin = load_linlut10()[frames].astype(int)
        assert np.abs(lin - want).max() <= 40


def test_time_stamps_follow_rate(cam):
    recorded(cam)
    st = cam.read_time_stamps(1, -10, 10)
    t = np.array([s.seconds_of_year for s in st])
    assert np.allclose(np.diff(t), 1 / 10000, atol=2e-6)   # rate 10000 fps, 1 us resolution
    assert all(s.exptime_us == 90 for s in st)


def test_download_lossless_and_decimated(cam, tmp_path):
    recorded(cam, frcount=300, ptframes=50)
    out = tmp_path / 'dl.cine'
    info = cam.download(1, out, step=1, chunk=37)
    r = CineReader(out)
    assert len(r) == 300 and r.first == -250 and info['count'] == 300
    assert r.real_bpp == 16                               # P16 stored exactly as received
    for i in (0, 123, 299):
        assert np.array_equal(r.read(i), synthetic_frame(r.first + i, 64, 32, seed=1) << 4)
    rel = r.relative_times()
    assert np.allclose(np.diff(rel), 1e-4, atol=2e-6)
    out2 = tmp_path / 'dec.cine'
    cam.download(1, out2, step=25, align='trigger')
    r2 = CineReader(out2)
    src_nums = [k * 25 for k in r2.image_numbers.tolist()]   # PCC numbering: image k = camera image 25k
    assert src_nums == list(range(-250, 50, 25))             # multiples of 25 incl. the trigger frame
    assert all(np.array_equal(r2.read(k), synthetic_frame(n, 64, 32, seed=1) << 4) for k, n in enumerate(src_nums))
    t1, t2 = r.relative_times(), r2.relative_times()
    assert np.allclose(t2, t1[np.array(src_nums) + 250])     # true times survive decimation
    out3 = tmp_path / 'dec_first.cine'
    info3 = cam.download(1, out3, first=-247, step=25, align='first')
    r3 = CineReader(out3)
    assert info3['offset'] == (-247) % 25 and r3.first * 25 + info3['offset'] == -247
    assert np.array_equal(r3.read(1), synthetic_frame(-222, 64, 32, seed=1) << 4)


def test_download_as_12bit(sim, cam, tmp_path):
    recorded(cam, frcount=60, ptframes=10)
    out = tmp_path / 'p12.cine'
    info = cam.download(1, out, fmt='P16', as_12bit=True)
    r = CineReader(out)
    assert info['as_12bit'] and r.real_bpp == 12 and info['dropped_low_bits'][0] == 0
    assert np.array_equal(r.read(7), synthetic_frame(r.first + 7, 64, 32, seed=1))   # lossless: original values
    assert (r.setup['BlackLevel'], r.setup['WhiteLevel']) == (64, 4064)               # what PCC writes (v2512)
    # a camera whose P16 low bits carry data (a real v2512 does): floored like PCC's layout, and counted
    import phantastic.simulator as S
    orig = S.Simulator._img

    def noisy_img(self, arg):
        hdr, blob = orig(self, arg)
        return hdr, (np.frombuffer(blob, '<u2') | 1).astype('<u2').tobytes()
    S.Simulator._img = noisy_img
    try:
        out2 = tmp_path / 'noisy.cine'
        sim.model.cines.clear()
        sim.model.partition(4)
        recorded(cam, frcount=60, ptframes=10)
        info2 = cam.download(1, out2, fmt='P16', as_12bit=True)
        r2 = CineReader(out2)
        assert info2['dropped_low_bits'][0] == info2['dropped_low_bits'][1] == 60 * 64 * 32
        assert np.array_equal(r2.read(7), synthetic_frame(r2.first + 7, 64, 32, seed=1))   # (16v | 1) >> 4 == v
    finally:
        S.Simulator._img = orig


def test_download_header_rate_fields(cam, tmp_path):
    # without the f64 rate the vendor SDK reports 10 fps (measured on a v2512 download, 2026-10-07)
    recorded(cam, frcount=60, ptframes=10)
    out = tmp_path / 'rate.cine'
    cam.download(1, out, first=0, last=3)
    s = CineReader(out).setup
    assert s['FrameRate'] == s['FrameRateInt1516'] == 10000 and s['FrameRateDouble'] == 10000.0
    assert s['FrameRate16'] == 10000 and s['Shutter'] == s['Shutter16'] == 90


def test_download_keeps_sub_microsecond_exposure(sim, cam, tmp_path):
    # time stamps carry whole microseconds; the cine's setting (ns) is stored when they agree
    sim.model.defc['exp'] = 90500
    recorded(cam, frcount=60, ptframes=10)
    out = tmp_path / 'exp.cine'
    cam.download(1, out, first=0, last=3)
    ns = CineReader(out).exposures_raw() / 2 ** 32 * 1e9
    assert np.allclose(ns, 90500, atol=0.5)


def test_errors_reported(cam):
    with pytest.raises(P.ProtocolError, match='cine status invalid'):
        cam.read_images(2, 0, 1, 'P16')
    recorded(cam)
    with pytest.raises(P.ProtocolError, match='outside range'):
        cam.read_images(1, 19, 2, 'P16')
