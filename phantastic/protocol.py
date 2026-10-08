"""The Phantom PH16 text protocol: framing, value grammar, and pixel/time-stamp decoding.

Clean-room implementation from Vision Research's published "PH16 camera protocol" v2.3 (2014)
and real camera transcripts (Miro M310 logs redistributed by DiamondLightSource/miroCamera).
Nothing here is derived from vendor binaries.

Wire facts (spec section numbers in brackets):
  * control: TCP 7115, ASCII, one response line per command [2.1, 3.1]
  * a response ends with CRLF not preceded by a backslash; ``\\<CRLF>`` continues it [3.1]
  * success is ``Ok!``/``OK!`` (case varies between firmware), errors start ``ERR:``
  * leaf ``get`` answers ``leaf : value`` (leaf name only); resolutions print as ``1280 x 504``
  * asynchronous ``@event@`` lines may appear anywhere once ``notify`` is enabled [5.26]
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

import numpy as np

CONTROL_PORT = 7115
ATTACH_PORT = 7116
DISCOVERY_PORT = 7380
DISCOVERY_REQUEST = b'phantom?'
NEWLINE = b'\r\n'
MAX_RESPONSE = 65536     # longer answers are truncated by the camera (spec 5.1): never use 'get *'

# Image formats [5.10]: token -> (number, bits stored per pixel, corrected by camera?, linear?)
IMAGE_FORMATS = {
    '8': (8, 8, True, True),
    '8R': (-8, 8, False, True),
    'P16': (272, 16, True, True),
    'P16R': (-272, 16, False, True),
    'P10': (266, 10, True, False),      # companded: linearise with the cine LinLUT
    'P12L': (None, 12, True, True),     # newer than spec v2.3; bit order from OSS clients
}
_FORMAT_BY_NUMBER = {v[0]: k for k, v in IMAGE_FORMATS.items() if v[0] is not None}


class ProtocolError(Exception):
    """The camera answered ``ERR: ...`` or something unparsable."""


@dataclass(frozen=True)
class Resolution:
    width: int
    height: int

    def __str__(self):
        return f'{self.width}x{self.height}'


@dataclass(frozen=True)
class Flags:
    """A flag list such as ``{ WTR DEF ABL ACT }``."""
    names: tuple

    def __contains__(self, item):
        return item in self.names

    def __str__(self):
        return '{' + ' '.join(self.names) + '}'


# ----------------------------------------------------------------------------- framing

def split_response(buf: bytes) -> tuple[bytes | None, bytes]:
    """Return (one complete response line without its CRLF, remaining bytes) or (None, buf).

    A CRLF preceded by a backslash continues the line; it is kept for :func:`clean_line`.
    Event notifications ``@name@`` are left in place; :func:`strip_events` removes them.
    """
    start = 0
    while True:
        i = buf.find(NEWLINE, start)
        if i < 0:
            return None, buf
        if buf[:i].rstrip(b'\r').endswith(b'\\'):   # tolerate extra CRs (seen in real logs)
            start = i + 2
            continue
        return buf[:i], buf[i + 2:]


_EVENT_RE = re.compile(r'@[A-Za-z0-9_]+@\\?\r?\n?')


def strip_events(text: str) -> tuple[str, list[str]]:
    """Remove ``@event@`` notifications; return (text, [event names])."""
    events = [m.group(0).strip('@\\\r\n') for m in _EVENT_RE.finditer(text)]
    return _EVENT_RE.sub('', text), events


def clean_line(raw: bytes) -> str:
    """Join ``\\<CRLF>`` continuations and turn tabs into spaces."""
    s = raw.decode('latin-1')
    return re.sub(r'\\\r*\n', ' ', s).replace('\t', ' ')


def check_ok(line: str) -> str:
    """Raise on ``ERR:``; strip a leading ``Ok!``/``OK!`` and return the rest."""
    s = line.strip()
    if s.upper().startswith('ERR'):
        raise ProtocolError(s[4:].strip() if s[3:4] == ':' else s)
    if s[:3].upper() == 'OK!':
        return s[3:].strip()
    return s


# ----------------------------------------------------------------------------- value grammar

_TOKEN_RE = re.compile(r'\s*(?:(\{)|(\})|(,)|(:)|("(?:[^"\\]|\\.)*")|([^\s{},:"]+))')
_NUM_RE = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$')
_RES_RE = re.compile(r'^(\d+)x(\d+)$')


def _tokens(s: str):
    pos = 0
    out = []
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            if s[pos:].strip() == '':
                break
            raise ProtocolError(f'cannot tokenise at {pos}: {s[pos:pos + 40]!r}')
        pos = m.end()
        kind = m.lastindex
        out.append((kind, m.group(kind)))
    return out


def _scalar(tok: str):
    if _NUM_RE.match(tok):
        f = float(tok)
        return int(tok) if re.match(r'^[+-]?\d+$', tok) else f
    if re.match(r'^0[xX][0-9a-fA-F]+$', tok):
        return int(tok, 16)
    m = _RES_RE.match(tok)
    if m:
        return Resolution(int(m.group(1)), int(m.group(2)))
    return tok


class _Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def value(self):
        kind, text = self.peek()
        if kind == 1:
            return self.braced()
        if kind == 5:
            self.take()
            return bytes(text[1:-1], 'latin-1').decode('unicode_escape')
        if kind == 6:
            self.take()
            # resolution printed with spaces: 1280 x 504
            if re.match(r'^\d+$', text) and self.peek()[1] == 'x' and re.match(r'^\d+$', str(self.peek(1)[1])):
                self.take()
                h = self.take()[1]
                return Resolution(int(text), int(h))
            return _scalar(text)
        raise ProtocolError(f'unexpected token {text!r}')

    def braced(self):
        self.take()  # {
        if self.peek()[0] == 2:
            self.take()
            return {}
        # tagged list if the second token is ':'
        if self.peek()[0] == 6 and self.peek(1)[0] == 4:
            out = {}
            while True:
                kind, name = self.take()
                if kind != 6:
                    raise ProtocolError(f'expected name, got {name!r}')
                if self.take()[0] != 4:
                    raise ProtocolError('expected :')
                out[name] = self.value()
                kind, _ = self.take()
                if kind == 2:
                    return out
                if kind != 3:
                    raise ProtocolError('expected , or }')
                if self.peek()[0] == 2:  # trailing comma
                    self.take()
                    return out
        names = []
        while self.peek()[0] not in (2, None):
            kind, text = self.take()
            if kind == 1:          # nested anonymous value (spec examples such as {{res:..}, ...})
                self.i -= 1
                names.append(self.braced())
            elif kind != 3:
                names.append(_scalar(text) if kind == 6 else text)
        self.take()
        if all(isinstance(n, str) for n in names):
            return Flags(tuple(names))
        return names


def parse_value(s: str):
    """Parse one protocol value (number, string, resolution, flag list or tagged list)."""
    p = _Parser(_tokens(s))
    v = p.value()
    if p.i != len(p.t):
        raise ProtocolError(f'trailing data after value: {s!r}')
    return v


def parse_get(line: str):
    """Parse a ``get`` answer ``name : value`` (or several, as ``cstats`` prints).

    Returns the value for a single ``name : value`` and a dict when several names are present.
    """
    s = check_ok(line)
    toks = _tokens(s)
    p = _Parser(toks)
    out = {}
    while p.i < len(toks):
        kind, name = p.take()
        if kind != 6 or p.take()[0] != 4:
            raise ProtocolError(f'expected "name : value", got {s[:80]!r}')
        out[name] = p.value()
        if p.peek()[0] == 3:
            p.take()
    if len(out) == 1:
        return next(iter(out.values()))
    return out


def format_value(v) -> str:
    """Encode a Python value for ``set``/command arguments."""
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, Resolution):
        return str(v)
    if isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, int) for x in v):
        return f'{v[0]}x{v[1]}'
    if isinstance(v, str):
        if re.match(r'^[A-Za-z0-9_.+-]+$', v):
            return v
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(v, dict):
        return '{' + ', '.join(f'{k}:{format_value(x)}' for k, x in v.items()) + '}'
    if isinstance(v, Flags):
        return str(v)
    raise TypeError(f'cannot encode {type(v).__name__}')


def set_line(name: str, value, sep: str = ':') -> str:
    """The exact ``set`` command line for ``name`` = ``value``.

    ``sep=':'`` gives ``set cam.timezone:18000``, the form the vendor SDK was captured sending for a
    single variable (docs/captures); ``sep=' '`` gives ``set defc {...}``, the form Phantastic's
    ``configure`` has always sent (and that a v2512 accepted, 2026-10-07).
    """
    if sep not in (':', ' '):
        raise ValueError(f'separator must be ":" or " ", not {sep!r}')
    return f'set {name}{sep}{format_value(value)}'


def format_number(fmt) -> str:
    """Normalise an image format token or number to its token."""
    if isinstance(fmt, int):
        return _FORMAT_BY_NUMBER.get(fmt, str(fmt))
    return str(fmt)


# ----------------------------------------------------------------------------- pixels

def frame_bytes(fmt: str, width: int, height: int) -> int:
    bits = IMAGE_FORMATS[fmt][1]
    n = width * height * bits
    if n % 8:
        raise ValueError(f'{fmt}: {width}x{height} is not a whole number of bytes')
    return n // 8


def decode_frames(buf: bytes, fmt: str, width: int, height: int, count: int) -> np.ndarray:
    """Decode ``count`` frames of the given wire format to (count, H, W) arrays.

    Returns uint8 for 8/8R, uint16 for P16/P16R, raw 10-bit *codes* (uint16, companded) for P10,
    and uint16 12-bit values for P12L. Rows are top-down as transmitted.
    """
    from .cine import unpack10, unpack12L
    fb = frame_bytes(fmt, width, height)
    if len(buf) != fb * count:
        raise ValueError(f'{len(buf)} bytes for {count} {fmt} frames of {width}x{height} (need {fb * count})')
    a = np.frombuffer(buf, np.uint8)
    if fmt in ('8', '8R'):
        return a.reshape(count, height, width).copy()
    if fmt in ('P16', 'P16R'):
        return a.view('<u2').reshape(count, height, width).copy()
    n = width * height * count
    if fmt == 'P10':
        return unpack10(a, n).reshape(count, height, width)
    if fmt == 'P12L':
        return unpack12L(a, n).reshape(count, height, width)
    raise ValueError(fmt)


# ----------------------------------------------------------------------------- time stamps

TIME_STAMP_SIZES = {8: 0, 12: 1, 24: 2, 28: 3}   # record size -> cam.tsformat


@dataclass
class TimeStamp:
    """One ``time`` record [5.12]. Times mark the END of exposure.

    csecs: 1/100 s since the start of the year (camera clock); frac: bits 15..2 = microseconds
    (0..9999) within the centisecond, bit 1 = event (0 = active), bit 0 = lock (0 = locked).
    exptime: exposure in microseconds (16-bit). exptime32/frac32 (formats 1 and 3, 12/28-byte records)
    extend exposure and time below a microsecond in units of 1/65536 us, as measured on a v2512 (see
    camera.stamp_exposure64 / stamp_time64); they are kept raw here.
    """
    csecs: int
    exptime_us: int
    frac: int
    exptime32: int | None = None
    frac32: int | None = None
    range_data: tuple | None = None

    @property
    def seconds_of_year(self) -> float:
        return self.csecs / 100.0 + (self.frac >> 2) * 1e-6

    @property
    def locked(self) -> bool:
        return not (self.frac & 1)

    @property
    def event(self) -> bool:
        return not (self.frac & 2)


def decode_time_stamps(buf: bytes, size: int, count: int) -> list[TimeStamp]:
    """Decode big-endian time-stamp records (size 8, 12, 24 or 28 bytes)."""
    if size not in TIME_STAMP_SIZES:
        raise ValueError(f'unknown time stamp size {size}')
    if len(buf) != size * count:
        raise ValueError(f'{len(buf)} bytes for {count} stamps of {size}')
    out = []
    for k in range(count):
        r = buf[k * size:(k + 1) * size]
        csecs, exptime, frac = struct.unpack_from('>IHH', r, 0)
        ts = TimeStamp(csecs, exptime, frac)
        off = 8
        if size in (12, 28):
            ts.exptime32, ts.frac32 = struct.unpack_from('>HH', r, 8)
            off = 12
        if size in (24, 28):
            ts.range_data = struct.unpack_from('>IIII', r, off)
        out.append(ts)
    return out


def parse_discovery_reply(data: bytes) -> dict | None:
    """Parse a discovery answer: ``PH16 <port> <hwver> <serial> ["name"]`` or ``PH7 <port>``."""
    s = data.rstrip(b'\0').decode('latin-1').strip()
    m = re.match(r'^PH16\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+"([^"]*)")?', s)
    if m:
        return dict(protocol='PH16', port=int(m.group(1)), hwver=int(m.group(2)),
                    serial=int(m.group(3)), name=m.group(4))
    m = re.match(r'^PH7\s+(\d+)', s)
    if m:
        return dict(protocol='PH7', port=int(m.group(1)), hwver=None, serial=None, name=None)
    return None
