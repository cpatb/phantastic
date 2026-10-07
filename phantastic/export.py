"""Export selected images of a cine, from a file or straight from camera RAM.

Sources (same interface: ``numbers``, ``times``, ``exposures_s``, ``meta``, ``images()``):
  :class:`FileFrames`    a .cine file, through :class:`CineReader` (raw decoded values)
  :class:`CameraFrames`  a stored camera cine, through :meth:`Camera.frames` (values as the camera
                         sends them; P10 linearised as a file reader would; ``as_12bit`` = value >> 4,
                         the PCC file layout of :meth:`Camera.download`)

Writers:
  :func:`write_tiff_stack`     multi-page TIFF of raw values (or a measured PCC table), JSON sidecar
  :func:`write_tiff_sequence`  one TIFF per image (PCC "TIFF" single-image format, manual p.72, 76)
  :func:`write_mp4`            H.264 movie: an 8-bit DISPLAY RENDER, not for measurement (p.75)

Every writer takes ``crop`` = (x, y, w, h) (see :mod:`phantastic.crop`), keeps true per-image times
(from time stamps; the metadata says when they were synthesized from number / rate), writes to a
temporary name and renames only on success, so a failed export leaves nothing that looks complete.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .cine import TIME64_SCALE, CineReader
from .crop import check_crop, crop_dict, crop_image, crop_note
from .decimate import _indices, select_numbers
from .naming import DEFAULT_SEQUENCE_PATTERN, expand_name, tokens_in

RAW_PROCESSING = 'none (raw sensor values)'
NOT_FOR_MEASUREMENT = ('8-bit display render (black/white window, gamma) - NOT FOR MEASUREMENT; '
                       'use the cine or a raw TIFF export for intensities')


@contextmanager
def atomic_path(dst) -> Iterator[Path]:
    """Yield ``dst + '.part'``; rename it onto ``dst`` on success, delete it on any failure."""
    dst = Path(dst)
    part = dst.with_name(dst.name + '.part')
    try:
        yield part
        os.replace(part, dst)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_pair(dst, sidecar) -> Iterator[tuple[Path, Path]]:
    """Yield (``dst.part``, ``sidecar.part``). On success the MAIN file is renamed first, then the sidecar;
    if the main rename fails (e.g. the old file is open in a player) both parts are deleted and the old
    pair is untouched; if only the sidecar rename fails, the old sidecar is deleted too, so no sidecar
    ever sits beside a file it does not describe."""
    dst, sidecar = Path(dst), Path(sidecar)
    part, spart = dst.with_name(dst.name + '.part'), sidecar.with_name(sidecar.name + '.part')
    try:
        yield part, spart
        os.replace(part, dst)
    except BaseException:
        part.unlink(missing_ok=True)
        spart.unlink(missing_ok=True)
        raise
    try:
        os.replace(spart, sidecar)
    except BaseException:
        spart.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise


def check_not_source(src, dst):
    """Refuse an output that is the source file itself (never modify sources)."""
    if Path(dst).resolve() == Path(src).resolve():
        raise ValueError(f'refusing to write over the source file {src}; choose another output path')


# ----------------------------------------------------------------------------- sources

class FileFrames:
    """Selected images of a cine file. Use as a context manager."""
    kind = 'file'

    def __init__(self, src, first: int | None = None, last: int | None = None, step: int = 1,
                 align: str = 'trigger'):
        self.reader = r = CineReader(src)
        try:
            self.idx = _indices(r, first, last, step, align)
            if len(self.idx) == 0:
                raise ValueError('no images selected')
            complete = r.has_complete_times()
            self.times = r.relative_times()[self.idx]
            self.times_from = ('per-image time stamps' if complete else
                               'SYNTHESIZED from image number / SETUP frame rate (file has no complete time stamps)')
            exps = r.exposures_raw()
            if exps is not None and len(exps) == len(r):
                self.exposures_s = exps[self.idx] / TIME64_SCALE
            elif r.setup.get('ShutterNs'):
                self.exposures_s = np.full(len(self.idx), r.setup['ShutterNs'] * 1e-9)
            else:
                self.exposures_s = None
        except BaseException:
            r.close()
            raise
        self.numbers = (r.first + self.idx).astype(np.int64)
        self.width, self.height = r.width, r.height
        self.real_bpp, self.frame_rate, self.step, self.align = r.real_bpp, r.frame_rate, step, align
        self.name = Path(src).stem
        self.trigger_time = r.trigger_time
        self.meta = {'source': str(src), 'software': 'Phantastic', 'real_bpp': r.real_bpp, 'packing': r.packing,
                     'frame_rate_setup': r.frame_rate,
                     'setup': {k: v for k, v in r.setup.items() if isinstance(v, (int, float, str))}}

    def __len__(self):
        return len(self.idx)

    def images(self) -> Iterator[np.ndarray]:
        for i in self.idx:
            yield self.reader.read(int(i))

    def close(self):
        self.reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class CameraFrames:
    """Selected images of a stored camera cine, read from camera RAM (no file in between).

    The caller holds the camera for the whole export (the GUI's session lock). All time stamps
    are read first (one ``time`` per run), so writers can put every time into page 0.
    """
    kind = 'camera'

    def __init__(self, cam, cine: int, first: int | None = None, last: int | None = None, step: int = 1,
                 align: str = 'trigger', fmt: str = 'P16', as_12bit: bool = False, chunk: int = 64):
        from . import protocol as P
        from .camera import stamp_time64, year_start
        if as_12bit and fmt not in ('P16', 'P16R'):
            raise ValueError('as_12bit applies to P16/P16R only')
        if fmt not in P.IMAGE_FORMATS:
            raise ValueError(f'unknown format {fmt!r}')
        ci = cam.cine_info(cine)
        if ci.get('firstfr') is None:
            raise ValueError(f'cine {cine} has no stored recording')
        lo = ci['firstfr'] if first is None else max(first, ci['firstfr'])
        hi = ci['lastfr'] if last is None else min(last, ci['lastfr'])
        self.numbers = select_numbers(lo, hi, step, align)
        if len(self.numbers) == 0:
            raise ValueError(f'no images in [{lo}, {hi}] with step {step}')
        self.cam, self.cine, self.fmt, self.as_12bit, self.chunk = cam, cine, fmt, as_12bit, chunk
        self.step, self.align = step, align
        res = ci['res']
        self.width, self.height = res.width, res.height
        self.frame_rate = float(ci['rate'] or 0)
        trig = ci.get('trigtime') if isinstance(ci.get('trigtime'), dict) else {}
        tsec, tfrac_us = int(trig.get('secs', 0)), int(trig.get('frac', 0))
        self.trigger_time = tsec + tfrac_us * 1e-6 if tsec else None
        frac64, year0 = int(tfrac_us * (1 << 32) // 1_000_000), year_start(tsec)
        exp_ns = ci.get('exp')
        self.exposures_s = None if exp_ns is None else np.full(len(self.numbers), int(exp_ns) * 1e-9)
        try:     # same TIME64 arithmetic as Camera.download, relative to the trigger in integer units
            t64, e_ns = [], []
            for start, n in cam.runs(self.numbers, chunk):
                stamps = cam.read_time_stamps(cine, start, n)
                if len(stamps) != n:
                    raise ValueError(f'camera returned {len(stamps)} time stamps for {n} images from {start}')
                for s in stamps:
                    sec, fr = stamp_time64(s, year0)
                    t64.append(((sec - tsec) << 32) + fr - frac64)
                    # Camera.download's rule: the cine's setting (ns) when the stamp's whole-us value agrees
                    # with it, else the stamp's own value (e.g. auto-exposure changed it)
                    e_ns.append(int(exp_ns) if exp_ns and abs(s.exptime_us * 1000 - int(exp_ns)) < 1000
                                else s.exptime_us * 1000)
            self.times = np.asarray(t64, np.int64) / TIME64_SCALE
            self.exposures_s = np.asarray(e_ns, np.float64) * 1e-9
            self.times_from = 'per-image time stamps (camera)'
        except P.ProtocolError:
            self.times = self.numbers / (self.frame_rate or 1.0)
            self.times_from = 'SYNTHESIZED from image number / frame rate (camera gave no time stamps)'
        bits = P.IMAGE_FORMATS[fmt][1]
        self.real_bpp = 12 if (as_12bit or fmt in ('P10', 'P12L')) else bits
        self._lut = None
        if fmt == 'P10':
            from .cine import load_linlut10
            self._lut = load_linlut10()
        self.dropped = [0, 0]     # P16 pixels whose low 4 bits were non-zero, pixels converted (as_12bit)
        serial = cam._safe_get('info.serial', None)
        self.name = f'cine{cine}_{serial}'
        self.meta = {'source': f'camera {serial} cine {cine} (RAM, not saved)', 'software': 'Phantastic',
                     'real_bpp': self.real_bpp, 'wire_format': fmt,
                     'camera_corrected': bool(P.IMAGE_FORMATS[fmt][2]),
                     'values': ('transmitted value >> 4 (12-bit, PCC file layout)' if as_12bit else
                                'P10 codes linearised to 12 bit (cine.load_linlut10)' if fmt == 'P10' else
                                'as transmitted'),
                     'frame_rate_setup': self.frame_rate, 'camera_cine': {k: str(v) for k, v in ci.items()}}

    def __len__(self):
        return len(self.numbers)

    def images(self, crop=None) -> Iterator[np.ndarray]:
        for _, frame, _ in self.cam.frames(self.cine, self.numbers, self.fmt, self.chunk):
            frame = crop_image(frame, crop)
            if self._lut is not None:
                frame = self._lut[frame]
            if self.as_12bit:
                self.dropped[0] += int(np.count_nonzero(frame & 0xF))
                self.dropped[1] += frame.size
                frame = frame >> 4
            yield frame

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _cropped_images(frames, crop) -> Iterator[np.ndarray]:
    """Images of the selection, cropped. Camera sources crop before converting (fewer pixels)."""
    if isinstance(frames, CameraFrames):
        yield from frames.images(crop)
    else:
        for img in frames.images():
            yield crop_image(img, crop)


def _base_meta(frames, crop, processing: str) -> dict:
    meta = dict(frames.meta)
    meta.update(processing=processing, image_numbers=[int(n) for n in frames.numbers],
                time_rel_trigger_s=[float(t) for t in frames.times], times_from=frames.times_from,
                step=frames.step, align=frames.align, crop=crop_dict(crop, frames.width, frames.height))
    if crop:
        meta['crop_note'] = crop_note(crop, frames.width, frames.height)
    return meta


def _finterval(frames) -> float:
    if len(frames.times) > 1:
        return float(np.median(np.diff(frames.times)))
    return 1.0 / max(frames.frame_rate, 1)


def _extra(frames, res: dict) -> dict:
    if isinstance(frames, CameraFrames) and frames.as_12bit:
        res['dropped_low_bits'] = list(frames.dropped)
    return res


# ----------------------------------------------------------------------------- TIFF

def write_tiff_stack(frames, dst, crop=None, table: np.ndarray | None = None, table_name: str | None = None,
                     progress=None) -> dict:
    """Multi-page TIFF: raw values (or ``table`` applied: a measured PCC export table), top-down.

    Per-image numbers and times go into a JSON ImageDescription on page 0 and a sidecar
    ``<dst>.json`` (with the median frame interval). The sidecar is written before the TIFF is
    renamed into place; both appear together or not at all.
    """
    import tifffile
    from .pcc_render import render
    crop = check_crop(crop, frames.width, frames.height)
    meta = _base_meta(frames, crop, RAW_PROCESSING if table is None else f'PCC export table {table_name}')
    w = crop[2] if crop else frames.width
    h = crop[3] if crop else frames.height
    n = len(frames)
    big = w * h * 2 * n * (3 if frames.meta.get('packing', '').startswith('bgr') else 1) > 3.9e9
    finterval = _finterval(frames)
    dst = Path(dst)
    with atomic_pair(dst, Path(str(dst) + '.json')) as (tpart, spart):
        with tifffile.TiffWriter(tpart, bigtiff=big) as tw:
            for k, img in enumerate(_cropped_images(frames, crop)):
                if table is not None:
                    img = render(img, table, np.uint8 if table.max() < 256 else np.uint16)
                tw.write(img, contiguous=False, photometric='rgb' if img.ndim == 3 else 'minisblack',
                         description=json.dumps(meta) if k == 0 else None, metadata=None, software='Phantastic')
                if progress:
                    progress(k + 1, n)
        spart.write_text(json.dumps(dict(meta, finterval_s=finterval), indent=1))
    return _extra(frames, dict(dst=str(dst), count=n, first=int(frames.numbers[0]), last=int(frames.numbers[-1]),
                               finterval_s=finterval, crop=crop, times_from=frames.times_from))


def write_tiff_sequence(frames, out_dir, pattern: str = DEFAULT_SEQUENCE_PATTERN, crop=None,
                        table: np.ndarray | None = None, table_name: str | None = None, fields: dict | None = None,
                        overwrite: bool = False, progress=None) -> dict:
    """One TIFF per image in ``out_dir``, named by ``pattern`` (tokens of :mod:`phantastic.naming`).

    The pattern must hold {image} or {+image}; '.tif' is added when it has no .tif/.tiff ending.
    Each file carries its own image number and time in a JSON ImageDescription; the sidecar
    ``<source>_sequence.json`` lists every file with its number and time. Files are written into a
    hidden temporary folder inside ``out_dir`` and moved into place only after the last one is
    complete. Existing files are never replaced unless ``overwrite``.
    """
    import tifffile
    from .pcc_render import render
    crop = check_crop(crop, frames.width, frames.height)
    used = tokens_in(pattern)
    if not used & {'image', 'image_pos'}:
        raise ValueError('the pattern needs {image} or {+image} (one file per image)')
    if not pattern.lower().endswith(('.tif', '.tiff')):
        pattern += '.tif'
    base = dict(source=frames.name, **(fields or {}))
    first = int(frames.numbers[0])
    names = [expand_name(pattern, **base, image=int(n), image_pos=int(n) - first) for n in frames.numbers]
    if len(set(n.lower() for n in names)) != len(names):
        raise ValueError(f'pattern {pattern!r} gives the same name to two images')
    if any(Path(n).name != n for n in names):
        raise ValueError('the pattern must be a file name, not a path (choose the folder separately)')
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sidecar = out_dir / f'{frames.name}_sequence.json'
    clash = [p for p in [out_dir / n for n in names] + [sidecar] if p.exists()]
    if clash and not overwrite:
        raise FileExistsError(f'{len(clash)} file(s) already exist, e.g. {clash[0]}; nothing was written')
    meta = _base_meta(frames, crop, RAW_PROCESSING if table is None else f'PCC export table {table_name}')
    meta['files'] = names
    tmp = out_dir / f'phantastic-INCOMPLETE-{uuid.uuid4().hex[:12]}.part'     # visible, and says what it is
    tmp.mkdir()
    moved: list[Path] = []
    try:
        for k, img in enumerate(_cropped_images(frames, crop)):
            if table is not None:
                img = render(img, table, np.uint8 if table.max() < 256 else np.uint16)
            page = {'source': meta['source'], 'software': 'Phantastic', 'processing': meta['processing'],
                    'image_number': int(frames.numbers[k]), 'time_rel_trigger_s': float(frames.times[k]),
                    'times_from': frames.times_from, 'real_bpp': frames.real_bpp, 'crop': meta['crop']}
            tifffile.imwrite(tmp / names[k], img, photometric='rgb' if img.ndim == 3 else 'minisblack',
                             description=json.dumps(page), metadata=None, software='Phantastic')
            if progress:
                progress(k + 1, len(names))
        (tmp / sidecar.name).write_text(json.dumps(dict(meta, finterval_s=_finterval(frames)), indent=1))
        if not overwrite:
            clash = [p for p in [out_dir / n for n in names] + [sidecar] if p.exists()]
            if clash:
                raise FileExistsError(f'{len(clash)} file(s) appeared during the export, e.g. {clash[0]}; '
                                      'nothing was written')
        # The sidecar is the record that the set is complete: drop an old one first, move it in last; if a
        # move fails, the files moved so far are removed again, so no old/new mixture is left described.
        sidecar.unlink(missing_ok=True)
        for n in names + [sidecar.name]:
            os.replace(tmp / n, out_dir / n)
            moved.append(out_dir / n)
    except BaseException:
        for q in moved:
            q.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _extra(frames, dict(dst=str(out_dir), files=names, count=len(names), sidecar=str(sidecar), first=first,
                               last=int(frames.numbers[-1]), finterval_s=_finterval(frames), crop=crop,
                               times_from=frames.times_from))


# ----------------------------------------------------------------------------- MP4

MP4_MISSING = ('MP4 export needs ffmpeg with the libx264 encoder: pip install "phantastic[video]" '
               '(bundles one via imageio-ffmpeg) or put ffmpeg on PATH.')
BORDER_FONT_PX_MIN, BORDER_MARGIN_PX = 12, 4
_ffmpeg_cache: dict = {}


def find_ffmpeg() -> str | None:
    """An ffmpeg that can encode H.264 (libx264): imageio-ffmpeg's bundled binary, else PATH."""
    if 'exe' in _ffmpeg_cache:
        return _ffmpeg_cache['exe']
    cands = []
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:       # not installed, or no binary for this platform
        pass
    if shutil.which('ffmpeg'):
        cands.append(shutil.which('ffmpeg'))
    exe = None
    for c in cands:
        try:
            out = subprocess.run([c, '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if 'libx264' in out:
            exe = c
            break
    _ffmpeg_cache['exe'] = exe
    return exe


def window_render(black: float, white: float, gamma: float = 1.0) -> Callable[[np.ndarray], np.ndarray]:
    """Display curve: (v - black) / (white - black), clipped to [0, 1], raised to 1/gamma, x255, rounded.

    gamma > 1 brightens mid-tones. Same linear map as the GUI's display range at gamma 1.
    """
    black, white, gamma = float(black), float(white), float(gamma)
    if not white > black:
        raise ValueError(f'white ({white}) must be above black ({black})')
    if not gamma > 0:
        raise ValueError('gamma must be > 0')

    def render(frame: np.ndarray) -> np.ndarray:
        a = (np.asarray(frame, np.float64) - black) / (white - black)
        np.clip(a, 0.0, 1.0, out=a)
        if gamma != 1.0:
            a **= 1.0 / gamma
        return np.rint(a * 255.0).astype(np.uint8)
    render.params = {'black': black, 'white': white, 'gamma': gamma}   # type: ignore[attr-defined]
    return render


def _orient(img: np.ndarray, rotate: int, flip_h: bool, flip_v: bool) -> np.ndarray:
    """Flip (in source orientation), then rotate clockwise by ``rotate`` degrees."""
    if flip_h:
        img = img[:, ::-1]
    if flip_v:
        img = img[::-1]
    return np.ascontiguousarray(np.rot90(img, k=-(rotate // 90)))


def _time_unit(times) -> tuple[str, float]:
    """One unit per movie, so the digits do not jump: us below 10 ms of |t|, else ms. ASCII 'us':
    the bundled PIL font has no glyph for the micro sign."""
    return ('us', 1e6) if np.max(np.abs(times)) < 1e-2 else ('ms', 1e3)


class _Border:
    """Border data strip BELOW the image (PCC's 'Standard' style, manual p.77): image number, time from
    trigger (per-image stamps), recording rate, exposure. White text on black, never over pixels."""

    def __init__(self, frames, img_w: int):
        from PIL import ImageFont
        self.frames = frames
        self.font_px = max(BORDER_FONT_PX_MIN, img_w // 40)
        self.font = ImageFont.load_default(size=self.font_px)
        self.unit, self.scale = _time_unit(frames.times)
        # enough decimals that neighbouring images never show the same time (frame interval in the unit)
        dt = float(np.min(np.abs(np.diff(frames.times)))) if len(frames) > 1 else 0.0
        # (1e-6 tolerance: a 1 us step read back as 0.99999999 us must not cost an extra digit)
        self.decimals = 3 if dt <= 0 else max(3, int(np.ceil(-np.log10(dt * self.scale) - 1e-6)) + 1)
        self.line_h = self.font_px + 3
        # numbers and times are monotonic, so their longest text is at an end; exposures can vary per image
        cand = {0, len(frames) - 1}
        if frames.exposures_s is not None:
            cand.add(int(np.argmax([len(f'{e * 1e6:.3f}') for e in frames.exposures_s])))
        widest = max(self.font.getlength(line) for k in cand for line in self.lines(k))
        self.width = max(img_w, int(np.ceil(widest)) + 2 * BORDER_MARGIN_PX + self.font_px)
        self.height = 3 * self.line_h + 2 * BORDER_MARGIN_PX

    def lines(self, k: int) -> list[str]:
        f = self.frames
        exp = '' if f.exposures_s is None else f'   exposure {f.exposures_s[k] * 1e6:.3f} us'
        return [f'Image {int(f.numbers[k])}',
                f'{f.times[k] * self.scale:+.{self.decimals}f} {self.unit} from trigger',
                f'{f.frame_rate:g} fps{exp}']

    def strip(self, k: int) -> np.ndarray:
        from PIL import Image, ImageDraw
        im = Image.new('L', (self.width, self.height), 0)
        d = ImageDraw.Draw(im)
        for j, line in enumerate(self.lines(k)):
            d.text((BORDER_MARGIN_PX, BORDER_MARGIN_PX + j * self.line_h), line, font=self.font, fill=255)
        return np.asarray(im)

    def params(self) -> dict:
        return {'position': 'strip below the image (PCC Standard style)', 'font_px': self.font_px,
                'strip_height_px': self.height, 'time_unit': self.unit,
                'fields': ['image number', 'time from trigger', 'recording rate', 'exposure']}


def _compose(img8: np.ndarray, border: _Border | None, k: int, size: tuple[int, int], rgb: bool) -> np.ndarray:
    """Place the image top-left on a black canvas of ``size`` (W, H): border strip, then even padding."""
    w, h = size
    canvas = np.zeros((h, w, 3) if rgb else (h, w), np.uint8)
    ih, iw = img8.shape[:2]
    canvas[:ih, :iw] = img8 if (img8.ndim == 3 or not rgb) else img8[:, :, None]
    if border is not None:
        s = border.strip(k)
        canvas[ih:ih + s.shape[0], :s.shape[1]] = s[:, :, None] if rgb else s
    return canvas


def write_mp4(frames, dst, fps: float = 30.0, black: float | None = None, white: float | None = None,
              gamma: float = 1.0, render: Callable[[np.ndarray], np.ndarray] | None = None, crop=None,
              rotate: int = 0, flip_h: bool = False, flip_v: bool = False, border: bool = False, crf: int = 18,
              ffmpeg: str | None = None, progress=None) -> dict:
    """H.264 MP4 (yuv420p, +faststart: plays in PowerPoint) at a constant ``fps`` chosen here.

    An 8-bit display render, NOT for measurement: each (cropped) raw frame goes through ``render``
    (frame -> uint8, same shape) or else :func:`window_render` (``black``..``white``, default the
    full ``real_bpp`` range, and ``gamma``); then flips, a clockwise ``rotate`` (0/90/180/270), the
    optional border strip, and black padding on the right/bottom to even dimensions (never
    stretched). The MP4 comment/description tags and the ``<dst>.json`` sidecar record all of it,
    plus the image numbers and true times that the constant movie rate does not carry.
    """
    exe = ffmpeg or find_ffmpeg()
    if exe is None:
        raise RuntimeError(MP4_MISSING)
    if rotate not in (0, 90, 180, 270):
        raise ValueError('rotate must be 0, 90, 180 or 270 (degrees clockwise)')
    if not fps > 0:
        raise ValueError('fps must be > 0')
    crop = check_crop(crop, frames.width, frames.height)
    if render is None:
        render = window_render(0 if black is None else black,
                               (1 << frames.real_bpp) - 1 if white is None else white, gamma)
        curve = dict(render.params)
    else:
        curve = dict(getattr(render, 'params', {'custom': repr(render)}))
    rate = Fraction(fps).limit_denominator(1001)
    dst = Path(dst)
    n = len(frames)
    it = _cropped_images(frames, crop)
    first8 = _orient(render(next(it)), rotate, flip_h, flip_v)
    if first8.dtype != np.uint8 or first8.ndim not in (2, 3):
        raise TypeError(f'render must return a uint8 (H, W) or (H, W, 3) array, got {first8.dtype} {first8.shape}')
    rgb = first8.ndim == 3
    ih, iw = first8.shape[:2]
    bd = _Border(frames, iw) if border else None
    w0, h0 = (bd.width, ih + bd.height) if bd else (iw, ih)
    size = (w0 + w0 % 2, h0 + h0 % 2)
    meta = _base_meta(frames, crop, NOT_FOR_MEASUREMENT)
    meta.update(movie_fps=float(rate), movie_size=list(size), display_curve=curve, rotate_cw_deg=rotate,
                flip_h=flip_h, flip_v=flip_v, border=bd.params() if bd else None,
                pad_right_bottom_px=[size[0] - w0, size[1] - h0], codec='H.264 (libx264) yuv420p, +faststart',
                crf=crf)
    comment = (f'Phantastic {NOT_FOR_MEASUREMENT}. Source {meta["source"]}, images {int(frames.numbers[0])}..'
               f'{int(frames.numbers[-1])} step {frames.step}; true times in {dst.name}.json')
    cmd = [exe, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24' if rgb else 'gray',
           '-s', f'{size[0]}x{size[1]}', '-framerate', f'{rate.numerator}/{rate.denominator}', '-i', 'pipe:0',
           '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', str(int(crf)), '-pix_fmt', 'yuv420p',
           '-movflags', '+faststart', '-metadata', f'comment={comment}', '-metadata', f'description={comment}',
           '-f', 'mp4']
    with ExitStack() as stack:
        mpart, spart = stack.enter_context(atomic_pair(dst, Path(str(dst) + '.json')))
        err = stack.enter_context(tempfile.TemporaryFile())
        try:
            proc = subprocess.Popen(cmd + [str(mpart)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        except OSError as e:
            raise RuntimeError(f'cannot run ffmpeg {exe!r}: {e}. {MP4_MISSING}') from e
        try:
            for k in range(n):
                img8 = first8 if k == 0 else _orient(render(next(it)), rotate, flip_h, flip_v)
                if img8.shape != first8.shape or img8.dtype != np.uint8:
                    raise ValueError(f'frame {k}: render gave {img8.dtype} {img8.shape}, first was {first8.shape}')
                proc.stdin.write(_compose(img8, bd, k, size, rgb).tobytes())
                if progress:
                    progress(k + 1, n)
            proc.stdin.close()
            rc = proc.wait()
        except OSError:       # BrokenPipeError; on Windows a dead pipe raises OSError(EINVAL)
            proc.kill()
            rc = proc.wait() or -1
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if rc != 0:
            err.seek(0)
            raise RuntimeError(f'ffmpeg failed (exit {rc}): {err.read().decode("utf-8", "replace").strip()[-2000:]}')
        spart.write_text(json.dumps(dict(meta, finterval_s=_finterval(frames)), indent=1))
    return _extra(frames, dict(dst=str(dst), count=n, first=int(frames.numbers[0]), last=int(frames.numbers[-1]),
                               fps=float(rate), size=size, crop=crop, border=bool(bd), times_from=frames.times_from,
                               sidecar=str(dst) + '.json'))


# ----------------------------------------------------------------------------- file conveniences

def export_tiff_sequence(src, out_dir, first: int | None = None, last: int | None = None, step: int = 1,
                         align: str = 'trigger', pattern: str = DEFAULT_SEQUENCE_PATTERN, crop=None, pcc_table=None,
                         overwrite: bool = False, progress=None) -> dict:
    """A cine file -> one TIFF per selected image (see :func:`write_tiff_sequence`)."""
    from .decimate import load_pcc_table
    check_not_source(src, out_dir)
    table, name = load_pcc_table(src, pcc_table) if pcc_table is not None else (None, None)
    with FileFrames(src, first, last, step, align) as fr:
        return write_tiff_sequence(fr, out_dir, pattern, crop=crop, table=table, table_name=name,
                                   overwrite=overwrite, progress=progress)


def export_mp4(src, dst, first: int | None = None, last: int | None = None, step: int = 1, align: str = 'trigger',
               progress=None, **opts) -> dict:
    """A cine file -> MP4 display render (see :func:`write_mp4` for ``opts``)."""
    check_not_source(src, dst)
    check_not_source(src, str(dst) + '.json')
    with FileFrames(src, first, last, step, align) as fr:
        return write_mp4(fr, dst, progress=progress, **opts)


__all__ = ['FileFrames', 'CameraFrames', 'write_tiff_stack', 'write_tiff_sequence', 'write_mp4', 'find_ffmpeg',
           'window_render', 'atomic_path', 'export_tiff_sequence', 'export_mp4', 'MP4_MISSING']
