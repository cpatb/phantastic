"""Read and write Vision Research Phantom ``.cine`` files, bit-exact and without processing.

Layout (little-endian), per the published Vision Research "Cine File Format"::

    CINEFILEHEADER (44 B) | BITMAPINFOHEADER (40 B) | SETUP (Length B) | tagged blocks
    | int64 image offsets[ImageCount]
    | per image: AnnotationSize u32, annotation bytes, ImageSize u32, pixel bytes

Reading returns the values the sensor recorded. The only transforms are those the storage
format requires: unpacking packed pixels, mapping 10-bit packed codes through the camera's
linearisation table, and turning bottom-up storage into top-down rows. Gain, gamma, tone curve,
white balance, colour matrix and filters stored in SETUP are *display settings*; they are
exposed in :attr:`CineReader.setup` and never applied here (see :mod:`phantastic.pcc_render`
for an explicit re-implementation of PCC's 8-bit rendering).

Frame numbers are the camera's: the trigger frame is 0 and pre-trigger frames are negative.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HEADER_SIZE = 44
BITMAPINFO_SIZE = 40
SETUP_LENGTH = 10412            # SETUP length written by current PCC (every file on the test drive)
BI_RGB, BI_PACKED, BI_PACKED_12L = 0, 256, 1024
CC_RGB, CC_JPEG, CC_UNINT = 0, 1, 2
TAG_TIME_ONLY, TAG_EXPOSURE_ONLY = 1002, 1003
TIME64_SCALE = 2.0 ** 32

# SETUP offsets (bytes from the start of SETUP), with struct formats.
SETUP_FIELDS = {
    'FrameRate16': (0, '<H'), 'Shutter16': (2, '<H'), 'PostTrigger16': (4, '<H'),
    'Mark': (140, '2s'), 'Length': (142, '<H'),
    'ImWidth': (737, '<H'), 'ImHeight': (739, '<H'), 'Serial': (743, '<I'),
    'bFlipH': (756, '<i'), 'bFlipV': (760, '<i'),
    'FrameRate': (768, '<I'), 'Shutter': (772, '<I'), 'PostTrigger': (780, '<I'),
    'bEnableColor': (788, '<I'), 'CameraVersion': (792, '<I'), 'FirmwareVersion': (796, '<I'),
    'SoftwareVersion': (800, '<I'), 'CFA': (808, '<I'), 'Rotate': (884, '<i'),
    'RealBPP': (896, '<I'),
    'ShutterNs': (1568, '<I'), 'EDRShutterNs': (1572, '<I'), 'FrameDelayNs': (1576, '<I'),
    'ImPosXAcq': (1580, '<I'), 'ImPosYAcq': (1584, '<I'),
    'ImWidthAcq': (1588, '<I'), 'ImHeightAcq': (1592, '<I'),
    'Description': (1596, '4096s'),
    'BlackLevel': (5732, '<i'), 'WhiteLevel': (5736, '<i'),
    'LensDescription': (5740, '256s'),
    'fOffset': (6008, '<f'), 'fGain': (6012, '<f'), 'fSaturation': (6016, '<f'),
    'fHue': (6020, '<f'), 'fGamma': (6024, '<f'), 'fGammaR': (6028, '<f'),
    'fGammaB': (6032, '<f'), 'fFlare': (6036, '<f'),
    'fPedestalR': (6040, '<f'), 'fPedestalG': (6044, '<f'), 'fPedestalB': (6048, '<f'),
    'fChroma': (6052, '<f'), 'ToneLabel': (6056, '256s'), 'TonePoints': (6312, '<I'),
    'fTone': (6316, '<64f'), 'EnableMatrices': (6828, '<I'), 'cmUser': (6832, '<9f'),
    'fGain16_8': (6900, '<f'), 'CineName': (6984, '256s'),
    'fGainR': (7240, '<f'), 'fGainG': (7244, '<f'), 'fGainB': (7248, '<f'),
    'cmCalib': (7252, '<9f'), 'fWBTemp': (7288, '<f'), 'fWBCc': (7292, '<f'),
    'CalibrationInfo': (7296, '1024s'), 'OpticalFilter': (8320, '1024s'),
    # Names not from the spec (not on hand). On all 145 PCC cines on the lab drive (2026-10-07) both hold the
    # recording rate: u32 @1516 its integer part, f64 @10400 the exact value (39180.23003 in one file). The
    # vendor SDK reports the f64 as FrameRate and falls back to 10 fps when it is 0 (measured).
    'FrameRateInt1516': (1516, '<I'), 'FrameRateDouble': (10400, '<d'),
}

PACKINGS = ('mono8', 'mono16', 'packed10', 'packed12L', 'bgr24', 'bgr48')
_PACKING_CODES = {  # packing -> (biBitCount as written by PCC, biCompression)
    # Packed files carry biBitCount = 16 (the unpacked depth); verified on a real PCC P10 file
    # (CineFiles.jl test data) and by the vendor SDK refusing biBitCount = 10.
    'mono8': (8, BI_RGB), 'mono16': (16, BI_RGB), 'packed10': (16, BI_PACKED),
    'packed12L': (16, BI_PACKED_12L), 'bgr24': (24, BI_RGB), 'bgr48': (48, BI_RGB),
}
_STORED_BITS = {'mono8': 8, 'mono16': 16, 'packed10': 10, 'packed12L': 12, 'bgr24': 24, 'bgr48': 48}


def packing_from_header(bit_count: int, compression: int) -> str:
    if compression == BI_PACKED:
        return 'packed10'
    if compression == BI_PACKED_12L:
        return 'packed12L'
    if compression == BI_RGB and bit_count in (8, 16, 24, 48):
        return {8: 'mono8', 16: 'mono16', 24: 'bgr24', 48: 'bgr48'}[bit_count]
    raise ValueError(f'unsupported pixel format biBitCount={bit_count} biCompression={compression}')


def _linlut_path() -> Path:
    return Path(__file__).with_name('linlut10.txt')


def load_linlut10() -> np.ndarray:
    """The 1024-entry table mapping 10-bit packed codes to 12-bit linear values.

    Measured black-box from the vendor decoder by ``tools/derive_linlut.py``.
    """
    vals = [int(t) for line in _linlut_path().read_text().splitlines()
            if line.strip() and not line.startswith('#') for t in line.replace(',', ' ').split()]
    if len(vals) != 1024:
        raise ValueError(f'linlut10.txt has {len(vals)} entries, expected 1024')
    return np.asarray(vals, dtype=np.uint16)


# ----------------------------------------------------------------------------- packing

def unpack10(buf: np.ndarray, n: int) -> np.ndarray:
    """Unpack 10-bit codes, 4 pixels per 5 bytes, MSB first. Returns raw *codes* (not linear)."""
    b = buf[: (n + 3) // 4 * 5].astype(np.uint16).reshape(-1, 5)
    out = np.empty((b.shape[0], 4), np.uint16)
    out[:, 0] = (b[:, 0] << 2) | (b[:, 1] >> 6)
    out[:, 1] = ((b[:, 1] & 0x3F) << 4) | (b[:, 2] >> 4)
    out[:, 2] = ((b[:, 2] & 0x0F) << 6) | (b[:, 3] >> 2)
    out[:, 3] = ((b[:, 3] & 0x03) << 8) | b[:, 4]
    return out.ravel()[:n]


def pack10(codes: np.ndarray) -> bytes:
    """Inverse of :func:`unpack10` for one row (length padded to a multiple of 4)."""
    c = np.asarray(codes, np.uint16).ravel()
    if c.max(initial=0) > 1023:
        raise ValueError('10-bit codes must be <= 1023')
    c = np.pad(c, (0, (-len(c)) % 4)).reshape(-1, 4).astype(np.uint32)
    out = np.empty((c.shape[0], 5), np.uint8)
    out[:, 0] = c[:, 0] >> 2
    out[:, 1] = ((c[:, 0] & 0x3) << 6) | (c[:, 1] >> 4)
    out[:, 2] = ((c[:, 1] & 0xF) << 4) | (c[:, 2] >> 6)
    out[:, 3] = ((c[:, 2] & 0x3F) << 2) | (c[:, 3] >> 8)
    out[:, 4] = c[:, 3] & 0xFF
    return out.tobytes()


def unpack12L(buf: np.ndarray, n: int) -> np.ndarray:
    """Unpack P12L (biCompression 1024), MSB first: p0 = b0 << 4 | b1 >> 4, p1 = (b1 & 0xF) << 8 | b2.

    'L' means linear (no companding LUT). Bit order as in every published reader; no real P12L
    file was available to verify it.
    """
    b = buf[: (n + 1) // 2 * 3].astype(np.uint16).reshape(-1, 3)
    out = np.empty((b.shape[0], 2), np.uint16)
    out[:, 0] = (b[:, 0] << 4) | (b[:, 1] >> 4)
    out[:, 1] = ((b[:, 1] & 0x0F) << 8) | b[:, 2]
    return out.ravel()[:n]


def pack12L(values: np.ndarray) -> bytes:
    v = np.asarray(values, np.uint16).ravel()
    if v.max(initial=0) > 4095:
        raise ValueError('12-bit values must be <= 4095')
    v = np.pad(v, (0, len(v) % 2)).reshape(-1, 2).astype(np.uint32)
    out = np.empty((v.shape[0], 3), np.uint8)
    out[:, 0] = v[:, 0] >> 4
    out[:, 1] = ((v[:, 0] & 0x0F) << 4) | (v[:, 1] >> 8)
    out[:, 2] = v[:, 1] & 0xFF
    return out.tobytes()


def stored_bottom_up(packing: str, flip_v: int = 0) -> bool:
    """Row order on disk. Spec: BI_RGB data are bottom-up, packed (P10) data top-down
    (confirmed for BI_RGB on real files against PCC's TIFF export). SETUP.bFlipV inverts it,
    following FFmpeg's cine demuxer (no bFlipV=1 file was available to confirm)."""
    return (packing not in ('packed10', 'packed12L')) != bool(flip_v)


# ----------------------------------------------------------------------------- setup

def parse_setup(raw: bytes) -> dict:
    """Decode the known SETUP fields that lie within the block's own Length."""
    length = struct.unpack_from('<H', raw, 142)[0]
    out = {}
    for name, (off, fmt) in SETUP_FIELDS.items():
        size = struct.calcsize(fmt)
        if off + size > min(length, len(raw)):
            continue
        val = struct.unpack_from(fmt, raw, off)
        if fmt.endswith('s'):
            out[name] = val[0].split(b'\0', 1)[0].decode('latin-1')
        elif len(val) == 1:
            out[name] = val[0]
        else:
            out[name] = list(val)
    n = out.get('TonePoints', 0)
    if 'fTone' in out:
        out['fTone'] = out['fTone'][: 2 * min(n, 32)]
    return out


def build_setup(fields: dict, template: bytes | None = None, length: int = SETUP_LENGTH) -> bytes:
    """SETUP bytes: ``template`` (or zeros) with ``fields`` overwritten. Mark/Length always set.

    A template keeps its own length, so fields absent from an older file stay absent (padding it
    would turn them into present zeros). Setting a field beyond the template's length is an error.
    """
    buf = bytearray(template if template is not None else bytes(length))
    struct.pack_into('2s', buf, 140, b'ST')
    struct.pack_into('<H', buf, 142, len(buf))
    for name, val in fields.items():
        off, fmt = SETUP_FIELDS[name]
        if off + struct.calcsize(fmt) > len(buf):
            raise ValueError(f'SETUP field {name} lies beyond this SETUP block ({len(buf)} bytes)')
        if fmt.endswith('s'):
            size = struct.calcsize(fmt)
            enc = val.encode('latin-1') if isinstance(val, str) else bytes(val)
            struct.pack_into(fmt, buf, off, enc[: size - 1])
        elif fmt[-1] == 'f' and fmt[1:-1]:          # float array
            n = int(fmt[1:-1])
            arr = list(val) + [0.0] * (n - len(val))
            struct.pack_into(fmt, buf, off, *arr[:n])
        else:
            struct.pack_into(fmt, buf, off, val)
    return bytes(buf)


# ----------------------------------------------------------------------------- reader

@dataclass
class CineReader:
    """Random-access reader. Use as a context manager or call :meth:`close`."""
    path: str | os.PathLike
    header: dict = field(init=False)
    bitmap: dict = field(init=False)
    setup: dict = field(init=False)
    setup_raw: bytes = field(init=False)
    blocks: dict = field(init=False)
    offsets: np.ndarray = field(init=False)

    def __post_init__(self):
        self._f = open(self.path, 'rb')
        try:
            self._parse()
        except BaseException:
            self._f.close()
            raise

    def _parse(self):
        h = self._read(0, HEADER_SIZE)
        if h[:2] != b'CI':
            self.close()
            raise ValueError(f'not a cine file: {self.path}')
        (_, hs, comp, ver, fmi, tic, fin, ic, ofih, ofs, ofio, tfrac, tsec) = struct.unpack('<2sHHHiIiIIIIII', h)
        self.header = dict(HeaderSize=hs, Compression=comp, Version=ver, FirstMovieImage=fmi,
                           TotalImageCount=tic, FirstImageNo=fin, ImageCount=ic, OffImageHeader=ofih,
                           OffSetup=ofs, OffImageOffsets=ofio, TriggerTime=(tsec, tfrac))
        if comp == CC_JPEG:
            raise ValueError('JPEG-compressed cines are not supported')
        bi = struct.unpack('<IiiHHIIiiII', self._read(ofih, BITMAPINFO_SIZE))
        self.bitmap = dict(biSize=bi[0], biWidth=bi[1], biHeight=bi[2], biPlanes=bi[3], biBitCount=bi[4],
                           biCompression=bi[5], biSizeImage=bi[6], biXPelsPerMeter=bi[7],
                           biYPelsPerMeter=bi[8], biClrUsed=bi[9], biClrImportant=bi[10])
        self.width, self.height = bi[1], abs(bi[2])
        self.packing = packing_from_header(bi[4], bi[5])
        min_stride = (self.width * _STORED_BITS[self.packing] + 7) // 8
        si = bi[6]
        self.stride = si // self.height if si and si % self.height == 0 and si // self.height >= min_stride else min_stride
        mark, slen = struct.unpack('<2sH', self._read(ofs + 140, 4))
        if mark != b'ST':
            raise ValueError('SETUP has no ST mark')
        self.setup_raw = self._read(ofs, slen)
        self.setup = parse_setup(self.setup_raw)
        self.bottom_up = stored_bottom_up(self.packing, self.setup.get('bFlipV', 0))
        self.blocks = {}
        pos = ofs + slen
        while pos + 8 <= ofio:
            size, typ = struct.unpack('<IH', self._read(pos, 6))
            if size < 8 or pos + size > ofio:
                break
            self.blocks[typ] = (pos + 8, size - 8)
            pos += size
        self.offsets = np.frombuffer(self._read(ofio, 8 * ic), '<i8').copy()
        self._lut = None

    # -- basic facts
    def __len__(self):
        return len(self.offsets)

    @property
    def first(self) -> int:
        return self.header['FirstImageNo']

    @property
    def image_numbers(self) -> np.ndarray:
        return np.arange(self.first, self.first + len(self), dtype=np.int64)

    @property
    def frame_rate(self) -> float:
        return float(self.setup['FrameRate'])

    @property
    def real_bpp(self) -> int:
        """Significant bits in values returned by :meth:`read` (P10 decodes to 12-bit linear,
        whatever SETUP.RealBPP says: a real P10 file stores RealBPP = 10)."""
        if self.packing == 'packed10':
            return 12
        rb = self.setup.get('RealBPP', 0)
        if 0 < rb <= 16:
            return rb
        return {'mono8': 8, 'bgr24': 8, 'packed10': 12, 'packed12L': 12}.get(self.packing, 16)

    @property
    def is_color(self) -> bool:
        return self.packing in ('bgr24', 'bgr48')

    @property
    def trigger_time(self) -> float:
        s, f = self.header['TriggerTime']
        return s + f / TIME64_SCALE

    def index_of(self, image_number: int) -> int:
        i = image_number - self.first
        if not 0 <= i < len(self):
            raise IndexError(f'image {image_number} not in file ({self.first}..{self.first + len(self) - 1})')
        return i

    # -- tagged blocks
    def block_bytes(self, tag: int) -> bytes | None:
        if tag not in self.blocks:
            return None
        off, n = self.blocks[tag]
        return self._read(off, n)

    def image_times(self) -> np.ndarray | None:
        """Absolute per-image time (tag 1002) in s since 1970-01-01 UTC, float64."""
        b = self.block_bytes(TAG_TIME_ONLY)
        if b is None:
            return None
        t = np.frombuffer(b, '<u4')[: 2 * len(self)].reshape(-1, 2)
        return t[:, 1].astype(np.float64) + t[:, 0].astype(np.float64) / TIME64_SCALE

    def image_times_raw(self) -> np.ndarray | None:
        """Tag 1002 as uint32 (fraction, seconds) pairs, exactly as stored."""
        b = self.block_bytes(TAG_TIME_ONLY)
        return None if b is None else np.frombuffer(b, '<u4')[: 2 * len(self)].reshape(-1, 2).copy()

    def exposures_raw(self) -> np.ndarray | None:
        b = self.block_bytes(TAG_EXPOSURE_ONLY)
        return None if b is None else np.frombuffer(b, '<u4')[: len(self)].copy()

    def has_complete_times(self) -> bool:
        """True when tag 1002 holds a time stamp for every stored image."""
        raw = self.image_times_raw()
        return raw is not None and len(raw) == len(self)

    def relative_times(self) -> np.ndarray:
        """Seconds relative to the trigger.

        From the per-image time stamps when every image has one; otherwise image number / SETUP
        frame rate for ALL images, with a warning, because that fallback is wrong for a decimated
        file (whose SETUP.FrameRate is the recording rate).
        """
        raw = self.image_times_raw() if self.has_complete_times() else None
        if raw is None:
            import warnings
            warnings.warn(f'{self.path}: no complete per-image time stamps; using image number / frame rate')
        if raw is not None:
            # Subtract in integer TIME64 units: float64 of an epoch time (~1.8e9 s) resolves only
            # 2^-22 s = 0.24 us, a quarter of the frame interval at 1 Mfps.
            tsec, tfrac = self.header['TriggerTime']
            return ((raw[:, 1].astype(np.int64) - tsec)
                    + (raw[:, 0].astype(np.int64) - tfrac) / TIME64_SCALE)
        return self.image_numbers / self.frame_rate

    # -- pixels
    def annotation(self, index: int) -> bytes:
        p = int(self.offsets[index])
        size = struct.unpack('<I', self._read(p, 4))[0]
        return self._read(p + 4, size - 8)

    def stored_bytes(self, index: int) -> bytes:
        p = int(self.offsets[index])
        size = struct.unpack('<I', self._read(p, 4))[0]
        return self._read(p + size, self.stride * self.height)

    def read(self, index: int) -> np.ndarray:
        """Decode stored image ``index`` to top-down rows.

        Returns uint8 (H, W) for mono8, uint16 (H, W) for mono16 / packed10 (linearised to
        12 bit) / packed12L, and RGB (H, W, 3) uint8/uint16 for colour.
        """
        if not 0 <= index < len(self):
            raise IndexError(index)
        s = np.frombuffer(self.stored_bytes(index), np.uint8).reshape(self.height, self.stride)
        w = self.width
        if self.packing == 'mono8':
            img = s[:, :w]
        elif self.packing == 'mono16':
            img = s[:, : 2 * w].copy().view('<u2')
        elif self.packing == 'packed10':
            if self._lut is None:
                self._lut = load_linlut10()
            img = np.stack([self._lut[unpack10(r, w)] for r in s])
        elif self.packing == 'packed12L':
            img = np.stack([unpack12L(r, w) for r in s])
        elif self.packing == 'bgr24':
            img = s[:, : 3 * w].reshape(self.height, w, 3)[:, :, ::-1]
        else:  # bgr48
            img = s[:, : 6 * w].copy().view('<u2').reshape(self.height, w, 3)[:, :, ::-1]
        if self.bottom_up:
            img = img[::-1]
        return np.ascontiguousarray(img)

    def read_codes10(self, index: int) -> np.ndarray:
        """For packed10 files: the stored 10-bit codes (top-down), before linearisation."""
        if self.packing != 'packed10':
            raise ValueError('not a 10-bit packed file')
        s = np.frombuffer(self.stored_bytes(index), np.uint8).reshape(self.height, self.stride)
        img = np.stack([unpack10(r, self.width) for r in s])
        return np.ascontiguousarray(img[::-1] if self.bottom_up else img)

    def __iter__(self):
        for i in range(len(self)):
            yield self.read(i)

    # -- plumbing
    def _read(self, pos: int, n: int) -> bytes:
        self._f.seek(pos)
        b = self._f.read(n)
        if len(b) != n:
            raise EOFError(f'short read at {pos} in {self.path}')
        return b

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


# ----------------------------------------------------------------------------- writer

def _stored_bytes(img: np.ndarray, packing: str, stride: int, bottom_up: bool) -> bytes:
    a = np.asarray(img)
    if bottom_up:
        a = a[::-1]
    rows = []
    for r in a:
        if packing == 'mono8':
            b = np.asarray(r, np.uint8).tobytes()
        elif packing == 'mono16':
            b = np.asarray(r, '<u2').tobytes()
        elif packing == 'packed10':
            b = pack10(r)
        elif packing == 'packed12L':
            b = pack12L(r)
        elif packing == 'bgr24':
            b = np.asarray(r[:, ::-1], np.uint8).tobytes()
        else:
            b = np.asarray(r[:, ::-1], '<u2').tobytes()
        rows.append(b.ljust(stride, b'\0'))
    return b''.join(rows)


class CineWriter:
    """Write a new cine in PCC's layout: header, bitmap, SETUP, tagged blocks, offsets, images.

    The number of images ``n_images`` must be known up front (it always is: a camera cine, a
    source range or a synthetic test). Images passed to :meth:`append` are top-down arrays in
    the convention :meth:`CineReader.read` returns, except that ``packed10`` takes 10-bit
    *codes*. Per-image time stamps (TIME64 as ``(seconds, fraction)``) and exposures (tag 1003
    raw uint32) are written when given for every image.
    """

    def __init__(self, path, width: int, height: int, n_images: int, packing: str = 'mono16', *,
                 first_image_no: int = 0, setup: bytes | None = None, setup_fields: dict | None = None,
                 trigger_time: tuple[int, int] = (0, 0), compression: int = CC_RGB,
                 first_movie_image: int | None = None, total_image_count: int | None = None,
                 with_times: bool = True, with_exposures: bool = True, annotation: bytes = b'',
                 clr_important: int | None = None, pels_per_meter: tuple[int, int] = (0, 0),
                 stride: int | None = None, extra_blocks: dict[int, bytes] | None = None):
        if packing not in PACKINGS:
            raise ValueError(packing)
        if n_images < 1:
            raise ValueError('n_images must be >= 1')
        self.w, self.h, self.n, self.packing = width, height, n_images, packing
        self.bpp, self.bic = _PACKING_CODES[packing]
        min_stride = (width * _STORED_BITS[packing] + 7) // 8
        if stride is not None and stride < min_stride:
            raise ValueError(f'stride {stride} < minimum {min_stride}')
        self.stride = stride or min_stride   # header biSizeImage and the data use the same value
        self.annotation = annotation
        self._tcount = self._ecount = 0
        setup_bytes = self._setup = build_setup(setup_fields or {}, template=setup)
        real = parse_setup(setup_bytes).get('RealBPP', 0) or (8 if packing in ('mono8', 'bgr24') else 12)
        clr = clr_important if clr_important is not None else 1 << min(16, real)
        self._times = np.zeros((n_images, 2), '<u4') if with_times else None
        self._exp = np.zeros(n_images, '<u4') if with_exposures else None
        self._offsets = np.zeros(n_images, '<i8')
        self._k = 0

        f = self._f = open(path, 'w+b')
        off_setup = HEADER_SIZE + BITMAPINFO_SIZE
        pos = off_setup + len(setup_bytes)
        self._off_times = self._off_exp = None
        if self._times is not None:
            self._off_times = pos + 8
            pos += 8 + 8 * n_images
        if self._exp is not None:
            self._off_exp = pos + 8
            pos += 8 + 4 * n_images
        extra = extra_blocks or {}
        pos += sum(8 + len(b) for b in extra.values())
        self._off_offsets = pos
        sec, frac = trigger_time
        fmi = first_image_no if first_movie_image is None else first_movie_image
        tic = n_images if total_image_count is None else total_image_count
        f.write(struct.pack('<2sHHHiIiIIIIII', b'CI', HEADER_SIZE, compression, 1, fmi, tic,
                            first_image_no, n_images, HEADER_SIZE, off_setup, self._off_offsets, frac, sec))
        f.write(struct.pack('<IiiHHIIiiII', BITMAPINFO_SIZE, width, height, 1, self.bpp, self.bic,
                            self.stride * height, pels_per_meter[0], pels_per_meter[1], 0, clr))
        f.write(setup_bytes)
        if self._times is not None:
            f.write(struct.pack('<IHH', 8 + 8 * n_images, TAG_TIME_ONLY, 0) + bytes(8 * n_images))
        if self._exp is not None:
            f.write(struct.pack('<IHH', 8 + 4 * n_images, TAG_EXPOSURE_ONLY, 0) + bytes(4 * n_images))
        for tag, body in extra.items():
            f.write(struct.pack('<IHH', 8 + len(body), tag, 0) + body)
        f.write(bytes(8 * n_images))

    def append(self, img: np.ndarray, time: tuple[int, int] | None = None, exposure: int | None = None):
        img = np.asarray(img)
        exp_shape = (self.h, self.w, 3) if self.packing in ('bgr24', 'bgr48') else (self.h, self.w)
        if img.shape != exp_shape:
            raise ValueError(f'image shape {img.shape} != {exp_shape}')
        flip_v = parse_setup(self._setup).get('bFlipV', 0)
        self.append_stored(_stored_bytes(img, self.packing, self.stride, stored_bottom_up(self.packing, flip_v)),
                           time, exposure)

    def append_stored(self, data: bytes, time: tuple[int, int] | None = None, exposure: int | None = None):
        """Append already-stored pixel bytes (bottom-up, packed) verbatim, for lossless copies."""
        if self._k >= self.n:
            raise ValueError(f'more than n_images={self.n} images appended')
        if len(data) != self.stride * self.h:
            raise ValueError(f'stored image is {len(data)} bytes, expected {self.stride * self.h}')
        f = self._f
        f.seek(0, os.SEEK_END)
        self._offsets[self._k] = f.tell()
        f.write(struct.pack('<I', 8 + len(self.annotation)) + self.annotation + struct.pack('<I', len(data)))
        f.write(data)
        if self._times is not None and time is not None:
            self._times[self._k] = (time[1], time[0])  # stored as (fraction, seconds)
            self._tcount += 1
        if self._exp is not None and exposure is not None:
            self._exp[self._k] = exposure
            self._ecount += 1
        self._k += 1

    def close(self):
        if self._k != self.n:
            self._f.close()
            raise ValueError(f'{self._k} images appended, header promises {self.n}')
        if self._times is not None and self._tcount != self.n:
            self._f.close()
            raise ValueError(f'time stamps given for {self._tcount} of {self.n} images (zeros would be written)')
        if self._exp is not None and self._ecount != self.n:
            self._f.close()
            raise ValueError(f'exposures given for {self._ecount} of {self.n} images')
        f = self._f
        if self._times is not None:
            f.seek(self._off_times)
            f.write(self._times.tobytes())
        if self._exp is not None:
            f.seek(self._off_exp)
            f.write(self._exp.tobytes())
        f.seek(self._off_offsets)
        f.write(self._offsets.tobytes())
        f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self._f.close()
