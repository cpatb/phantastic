"""Talk to a Phantom camera over Ethernet (PH16 protocol): discover, configure, record, download.

Everything the camera sends is kept as sent. Downloads go straight into a cine file with the
pixels exactly as transmitted, plus the camera's own time stamps; nothing is filtered, scaled or
resampled. Choose the wire format deliberately:

  P16  / 8   : corrected by the camera (fixed-pattern noise and pixel response, FPN/PRNU).
               P16 is full-scale 16-bit (spec: range 0-65535): a 12-bit sensor value v arrives
               as v*16. The vendor library divides by 16; Phantastic stores what arrives
               (RealBPP 16) and says so in the file description.
  P16R / 8R  : uncorrected sensor values
  P10        : companded 10-bit codes (smaller, not linear; see cine.load_linlut10)

The protocol is documented in Vision Research's "PH16 camera protocol" v2.3; see protocol.py.
"""
from __future__ import annotations

import datetime as _dt
import logging
import select
import socket
import struct
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterator

import numpy as np

from . import protocol as P
from .cine import CineWriter

log = logging.getLogger(__name__)

PCC_BLACK_12, PCC_WHITE_12 = 64, 4064   # what PCC writes for a 12-bit v2512 cine
LIVE_CINE = -1          # img from cine -1 is the live image [spec 5.8]
LIVE_LOG_EVERY = 200    # session log keeps 1 live frame in 200 (~every 10 s at 20 fps); errors always


@dataclass
class DiscoveredCamera:
    ip: str
    port: int
    protocol: str
    serial: int | None
    hwver: int | None
    name: str | None

    def __str__(self):
        label = self.name or (f'serial {self.serial}' if self.serial is not None else 'camera')
        return f'{label} @ {self.ip}:{self.port} ({self.protocol})'


def local_ipv4() -> list[tuple[str, str, str]]:
    """(adapter name, IPv4 address, netmask) for every up adapter except loopback (needs psutil)."""
    try:
        import psutil
    except ImportError:
        return []
    up = {n for n, st in psutil.net_if_stats().items() if st.isup}
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        if name not in up:
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith('127.'):
                out.append((name, a.address, a.netmask or '255.255.255.255'))
    return out


def directed_broadcast(ip: str, mask: str) -> str:
    i = struct.unpack('>I', socket.inet_aton(ip))[0]
    m = struct.unpack('>I', socket.inet_aton(mask))[0]
    return socket.inet_ntoa(struct.pack('>I', (i & m) | (~m & 0xFFFFFFFF)))


def network_report() -> list[str]:
    """Human-readable adapter list, flagging the usual Phantom subnets (100.100.x.x, 172.16.x.x)."""
    lines = []
    for name, ip, mask in local_ipv4():
        tag = ''
        if ip.startswith('100.100.'):
            tag = '  <- Phantom 1 GbE subnet'
        elif ip.startswith('172.16.'):
            tag = '  <- Phantom 10 GbE subnet'
        elif ip.startswith('169.254.'):
            tag = '  (link-local: no address assigned)'
        lines.append(f'{name}: {ip} / {mask}{tag}')
    return lines


def discover(timeout: float = 1.0, broadcast: tuple[str, ...] = ('255.255.255.255', '100.100.255.255', '172.16.255.255'),
             port: int = P.DISCOVERY_PORT, bind: str | None = None) -> list[DiscoveredCamera]:
    """Broadcast ``phantom?`` to UDP 7380 and collect replies for ``timeout`` seconds [spec 6].

    With several network adapters (Wi-Fi, WSL, VMs) Windows sends a plain broadcast out of one
    adapter only, so unless ``bind`` is given the request goes out from every adapter, to the
    listed addresses and to that adapter's own subnet broadcast.
    """
    sources = [(bind, None)] if bind is not None else [('', None)] + [(ip, mask) for _, ip, mask in local_ipv4()]
    socks = []
    for src, mask in sources:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind((src, 0))
        except OSError as e:
            log.debug('cannot bind discovery socket to %s: %s', src, e)
            s.close()
            continue
        targets = list(broadcast) + ([directed_broadcast(src, mask)] if mask else [])
        for addr in dict.fromkeys(targets):
            try:
                s.sendto(P.DISCOVERY_REQUEST, (addr, port))
            except OSError as e:   # an address may be unroutable from this adapter; others may work
                log.debug('discovery %s -> %s failed: %s', src or '*', addr, e)
        socks.append(s)
    found: dict[tuple, DiscoveredCamera] = {}
    end = time.monotonic() + timeout
    while socks and (left := end - time.monotonic()) > 0:
        r, _, _ = select.select(socks, [], [], left)
        if not r:
            break
        for s in r:
            try:
                data, (ip, _) = s.recvfrom(4096)
            except OSError:      # e.g. ICMP port unreachable surfaced as WSAECONNRESET on Windows
                continue
            info = P.parse_discovery_reply(data)
            if info:
                cam = DiscoveredCamera(ip=ip, **info)
                # one camera can answer through several adapters: keep it once (by serial)
                found.setdefault(cam.serial if cam.serial is not None else (ip, cam.port), cam)
    for s in socks:
        s.close()
    return list(found.values())


class Camera:
    """A control connection to one camera. Use as a context manager."""

    def __init__(self, ip: str, port: int = P.CONTROL_PORT, timeout: float = 10.0,
                 attach_port: int = P.ATTACH_PORT, data_method: str = 'auto', log_file=None):
        """``data_method``: 'auto' uses ``attach`` (we connect out to the camera, as PCC does, so a
        PC firewall does not get in the way) when the camera lists the ``attach`` feature, else
        ``startdata`` (the camera connects back). ``log_file``: path to append a timestamped record
        of every command, response and data transfer."""
        self.ip, self.port, self.timeout = ip, port, timeout
        self.attach_port, self.data_method = attach_port, data_method
        self._log = open(log_file, 'a', encoding='utf-8', buffering=1) if log_file else None
        self.log_file = str(log_file) if log_file else None
        self.sock: socket.socket | None = None
        self._buf = b''
        self._lock = threading.Lock()
        self.data: socket.socket | None = None
        self.events: list[str] = []
        self.transcript: list[tuple[str, str]] = []   # (command, raw response) for auditing
        self.keep_transcript = True
        self._quiet = False      # True while a live frame that is not logged is in flight
        self._live_n = 0
        self._closing = False

    # ------------------------------------------------------------- connection
    def connect(self):
        self.note(f'connect {self.ip}:{self.port}')
        self.sock = socket.create_connection((self.ip, self.port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self

    def close(self):
        self._closing = True     # a read cut off by this close is not a camera error
        for s in (self.data, self.sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self.data = self.sock = None
        if self._log is not None:
            self.note('closed')
            self._log.close()
            self._log = None

    def note(self, text: str):
        """Append a line to the session log (no-op without one)."""
        if self._log is not None:
            try:
                self._log.write(f'{time.strftime("%H:%M:%S")}.{int(time.time() * 1000) % 1000:03d} {text}\n')
            except (OSError, ValueError):
                pass

    def _note_routine(self, text: str):
        """Log a routine line, except during an unlogged live frame (errors always use :meth:`note`)."""
        if not self._quiet:
            self.note(text)

    def _note_failure(self, text: str):
        self.note(f'   stopped: connection closed by Phantastic ({text})' if self._closing else f'!! {text}')

    def __enter__(self):
        return self.connect() if self.sock is None else self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------- commands
    def command(self, line: str) -> str:
        """Send one command line; return the cleaned response with ``Ok!`` stripped.

        Raises :class:`protocol.ProtocolError` for ``ERR:`` answers.
        """
        if self.sock is None:
            raise ConnectionError('not connected')
        with self._lock:
            self._note_routine(f'>> {line}')
            try:
                self.sock.sendall(line.encode('latin-1') + P.NEWLINE)
                while True:
                    raw, self._buf = P.split_response(self._buf)
                    if raw is not None:
                        break
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError('camera closed the control connection')
                    self._buf += chunk
            except (OSError, ConnectionError) as e:
                self._note_failure(f'{type(e).__name__}: {e}')
                raise
            self._note_routine('<< ' + raw.decode('latin-1').replace('\\\r\n', ' ').replace('\t', ' ')[:4000])
        if len(raw) >= P.MAX_RESPONSE:
            raise P.ProtocolError(f'response to {line!r} truncated at 64 KB by the camera; query a smaller structure')
        text, ev = P.strip_events(P.clean_line(raw))
        self.events.extend(ev)
        if self.keep_transcript:
            self.transcript.append((line, raw.decode('latin-1')))
        return P.check_ok(text)

    def get(self, name: str):
        return P.parse_get(self.command(f'get {name}'))

    def set(self, name: str, value):
        self.command(f'set {name} {P.format_value(value)}')

    # ------------------------------------------------------------- high level
    def info(self) -> dict:
        return self.get('info')

    def features(self) -> set[str]:
        return set(str(self.get('info.features')).split())

    def image_formats(self) -> list[str]:
        return str(self.get('info.imgformats')).split()

    def acquisition(self) -> dict:
        """Current acquisition settings (``defc``)."""
        return self.get('defc')

    def configure(self, resolution: tuple[int, int] | None = None, rate: float | None = None,
                  exposure_ns: int | None = None, post_trigger: int | None = None,
                  edr_exposure_ns: int | None = None) -> dict:
        """Set acquisition parameters atomically (one ``set defc {...}``) and read them back.

        The camera may round values (e.g. exposure to its clock); the read-back is returned so
        the caller sees what will actually be used.
        """
        upd = {}
        if resolution is not None:
            upd['res'] = P.Resolution(*resolution)
        if rate is not None:
            upd['rate'] = rate
        if exposure_ns is not None:
            upd['exp'] = int(exposure_ns)
        if post_trigger is not None:
            upd['ptframes'] = int(post_trigger)
        if edr_exposure_ns is not None:
            upd['edrexp'] = int(edr_exposure_ns)
        if upd:
            self.command(f'set defc {P.format_value(upd)}')
        return self.acquisition()

    def cine_states(self) -> dict:
        """``cstats``: {'c0': Flags(...), 'c1': ...}."""
        v = P.parse_get(self.command('cstats'))
        return v if isinstance(v, dict) else {'c0': v}

    def cine_info(self, cine: int) -> dict:
        keys = ('state', 'firstfr', 'lastfr', 'frcount', 'res', 'rate', 'exp', 'ptframes', 'trigtime')
        out = {}
        for k in keys:
            try:
                out[k] = self.get(f'c{cine}.{k}')
            except P.ProtocolError:
                out[k] = None
        return out

    def partition(self, n: int):
        """Erase ALL cines and split memory into n partitions."""
        self.command(f'partition {{num:{int(n)}}}')

    def record(self, cine: int | None = None):
        """Start recording (``rec`` / ``rec n``). A stored recording in that cine is deleted by the camera."""
        self.command('rec' if cine is None else f'rec {int(cine)}')

    def trigger(self):
        """Software trigger (``trig``)."""
        self.command('trig')

    def preview(self):
        """Return to live preview (``rec 0``, as the Diamond EPICS driver does; the spec has no abort)."""
        self.command('rec 0')

    def delete(self, cine: int):
        self.command(f'del {int(cine)}')

    def wait_stored(self, cine: int, timeout: float = 60.0, poll: float = 0.1) -> P.Flags:
        """Poll ``c#.state`` until it contains STR."""
        end = time.monotonic() + timeout
        while True:
            st = self.get(f'c{cine}.state')
            if isinstance(st, P.Flags) and 'STR' in st:
                return st
            if time.monotonic() > end:
                raise TimeoutError(f'cine {cine} not stored after {timeout} s (state {st})')
            time.sleep(poll)

    # ------------------------------------------------------------- data stream
    def open_data(self, method: str | None = None, host: str = '', port: int = 0, attach_port: int | None = None):
        """Create the data stream.

        startdata: we listen, the camera connects back [spec 5.8].
        attach   : we connect to the camera's TCP 7116 and tell it our local port [spec 5.9];
                   better behind firewalls/NAT, and what PCC uses (captured).
        auto     : attach when the camera lists the 'attach' feature, falling back to startdata.
        """
        method = method or self.data_method
        attach_port = self.attach_port if attach_port is None else attach_port
        if self.data is not None:
            self.data.close()
            self.data = None
        if method == 'auto':
            try:
                feats = self.features()
            except P.ProtocolError:
                feats = set()
            if 'attach' in feats:
                try:
                    return self.open_data('attach', host, port, attach_port)
                except (OSError, P.ProtocolError) as e:
                    self.note(f'attach failed ({e}); trying startdata')
            return self.open_data('startdata', host, port, attach_port)
        self.note(f'data stream: {method}')
        if method == 'startdata':
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((host, port))
            srv.listen(1)
            srv.settimeout(self.timeout)
            got: dict = {}

            def accept():
                try:
                    got['sock'], _ = srv.accept()
                except OSError as e:
                    got['err'] = e
            t = threading.Thread(target=accept, daemon=True)
            t.start()
            try:
                self.command(f'startdata {{port:{srv.getsockname()[1]}}}')
                t.join(self.timeout)
            finally:
                srv.close()
            if 'sock' not in got:
                raise ConnectionError(f'camera did not connect back for the data stream: {got.get("err")}')
            self.data = got['sock']
        elif method == 'attach':
            d = socket.create_connection((self.ip, attach_port), timeout=self.timeout)
            self.command(f'attach {{port:{d.getsockname()[1]}}}')
            self.data = d
        else:
            raise ValueError(method)
        self.data.settimeout(self.timeout)
        self.data.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        return self.data

    def _recv_exact(self, n: int) -> bytes:
        if self.data is None:
            self.open_data()
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        t0 = time.monotonic()
        try:
            while got < n:
                k = self.data.recv_into(view[got:], n - got)
                if k == 0:
                    raise ConnectionError(f'data stream closed after {got} of {n} bytes')
                got += k
        except (OSError, ConnectionError) as e:
            self._note_failure(f'data: {type(e).__name__} after {got} of {n} bytes: {e}')
            raise
        dt = time.monotonic() - t0
        rate = f'{n / dt / 1e6:.1f} MB/s' if dt >= 1e-3 else 'under 1 ms'
        self._note_routine(f'   data: {n} bytes in {dt:.3f} s ({rate})')
        return bytes(buf)

    def read_images_raw(self, cine: int, start: int, count: int, fmt: str = 'P16', source: str | None = None):
        """``img`` without decoding: (wire bytes, header dict with the format actually used)."""
        args = {'cine': cine, 'start': start, 'cnt': count, 'fmt': fmt}
        if source:
            args['from'] = source
        if self.data is None:
            self.open_data()
        if cine == LIVE_CINE:   # the live view asks ~20 times a second: log one frame in LIVE_LOG_EVERY
            self._live_n += 1
            self._quiet = (self._live_n - 1) % LIVE_LOG_EVERY != 0
        try:
            return self._read_images_raw(args, fmt, count)
        finally:
            self._quiet = False

    def _read_images_raw(self, args: dict, fmt: str, count: int):
        hdr = P.parse_value(self.command(f'img {P.format_value(args)}'))
        res = hdr['res']
        wire_fmt = P.format_number(hdr.get('fmt', fmt))
        if wire_fmt not in P.IMAGE_FORMATS:
            # e.g. a numeric code this client does not know (P12L's is not published): the camera
            # answers with the format it was asked for, so size the read from the request.
            self.note(f'unknown format code {wire_fmt!r} in img reply; assuming requested {fmt}')
            hdr['fmt_code'] = wire_fmt
            wire_fmt = fmt
        nbytes = P.frame_bytes(wire_fmt, res.width, res.height) * count
        return self._recv_exact(nbytes), dict(hdr, fmt=wire_fmt)

    def read_images(self, cine: int, start: int, count: int, fmt: str = 'P16', source: str | None = None):
        """``img``: returns (frames (count, H, W), header dict as answered by the camera).

        For P10 the frames are the raw 10-bit codes; linearise with ``cine.load_linlut10()``.
        """
        raw, hdr = self.read_images_raw(cine, start, count, fmt, source)
        res = hdr['res']
        return P.decode_frames(raw, hdr['fmt'], res.width, res.height, count), hdr

    def read_time_stamps(self, cine: int, start: int, count: int) -> list[P.TimeStamp]:
        if self.data is None:
            self.open_data()
        hdr = P.parse_value(self.command(f'time {P.format_value({"cine": cine, "start": start, "cnt": count})}'))
        size, cnt = int(hdr['size']), int(hdr['cnt'])
        return P.decode_time_stamps(self._recv_exact(size * cnt), size, cnt)

    def live_image(self, fmt: str = '8'):
        """One live frame from the active cine (``img {cine:-1, ...}``)."""
        frames, hdr = self.read_images(-1, 0, 1, fmt)
        return frames[0], hdr

    # ------------------------------------------------------------- download to file
    def runs(self, numbers: np.ndarray, chunk: int = 64) -> Iterator[tuple[int, int]]:
        """Split image numbers into (start, count) runs of consecutive numbers, at most ``chunk`` long."""
        numbers = np.asarray(numbers, dtype=np.int64)
        i = 0
        while i < len(numbers):
            j = i + 1
            while j < len(numbers) and j - i < chunk and numbers[j] == numbers[j - 1] + 1:
                j += 1
            yield int(numbers[i]), j - i
            i = j

    def frames(self, cine: int, numbers: np.ndarray, fmt: str, chunk: int = 64,
               with_times: bool = False) -> Iterator[tuple[int, np.ndarray, P.TimeStamp | None]]:
        """Yield (image number, frame, time stamp or None), one ``img`` (and ``time``) per run."""
        for start, n in self.runs(numbers, chunk):
            batch, _ = self.read_images(cine, start, n, fmt)
            stamps = self.read_time_stamps(cine, start, n) if with_times else [None] * n
            for k in range(n):
                yield start + k, batch[k], stamps[k]

    def download(self, cine: int, path, first: int | None = None, last: int | None = None, step: int = 1,
                 fmt: str = 'P16', align: str = 'trigger', chunk: int = 64,
                 progress: Callable[[int, int], None] | None = None, description: str = '',
                 as_12bit: bool = False) -> dict:
        """Download a stored cine (or a decimated range of it) into a new .cine file, losslessly.

        ``as_12bit`` (P16/P16R only): store value >> 4 as 12-bit data, the layout of PCC's own files
        (PCC shows a 16-bit file almost white). P16R is exactly 12-bit x 16, so this is lossless for it.
        A v2512 fills P16's low 4 bits with the fraction left by its FPN/PRNU correction (93.6 % of
        pixels, 2026-10-07); those are dropped (floor) and counted in the returned ``dropped_low_bits``.
        Whether PCC floors or rounds is not yet verified.

        ``step``/``align`` choose the kept image numbers explicitly: 'trigger' keeps numbers that
        are multiples of ``step`` (PCC's rule, measured), 'first' keeps first, first+step, ...
        Returns a summary dict (what was requested, what the camera reported, kept numbers).
        """
        from .decimate import renumbering, select_numbers
        ci = self.cine_info(cine)
        if ci['state'] is not None and 'STR' not in ci['state']:
            log.warning('cine %s state %s has no STR flag; frames may be incomplete', cine, ci['state'])
        lo = ci['firstfr'] if first is None else max(first, ci['firstfr'])
        hi = ci['lastfr'] if last is None else min(last, ci['lastfr'])
        numbers = select_numbers(lo, hi, step, align)
        if len(numbers) == 0:
            raise ValueError(f'no images in [{lo}, {hi}] with step {step}')
        res = ci['res']
        try:
            self.read_time_stamps(cine, int(numbers[0]), 1)
            have_times = True
        except P.ProtocolError as e:
            log.warning('time stamps unavailable: %s; writing number/rate times instead', e)
            have_times = False
        packing = {'8': 'mono8', '8R': 'mono8', 'P16': 'mono16', 'P16R': 'mono16',
                   'P10': 'packed10', 'P12L': 'packed12L'}[fmt]
        trig = ci.get('trigtime') or {}
        tsec = int(trig.get('secs', 0)) if isinstance(trig, dict) else 0
        tfrac_us = int(trig.get('frac', 0)) if isinstance(trig, dict) else 0
        year0 = int(_dt.datetime(_dt.datetime.fromtimestamp(tsec, _dt.timezone.utc).year, 1, 1,
                                 tzinfo=_dt.timezone.utc).timestamp()) if tsec else 0
        bits = P.IMAGE_FORMATS[fmt][1]
        if as_12bit and fmt not in ('P16', 'P16R'):
            raise ValueError('as_12bit applies to P16/P16R only')
        if as_12bit:
            bits = 12
        serial = self._safe_get('info.serial', 0)
        desc = (f'{description}\nPhantastic download: format {fmt} '
                f'({"camera-corrected FPN/PRNU" if P.IMAGE_FORMATS[fmt][2] else "uncorrected"}), '
                f'step {step} align {align}; '
                + ('12-bit, PCC layout: values = transmitted value >> 4 (any correction fraction below '
                   'one 12-bit count is dropped).' if as_12bit else 'pixels stored exactly as transmitted.')).strip()
        rate_i, exp_ns, pt = int(round(float(ci['rate'] or 0))), int(ci['exp'] or 0), int(ci['ptframes'] or 0)
        # Black/white levels are display hints for PCC only (Phantastic never applies them). PCC writes
        # 64/4064 for a 12-bit v2512 cine (F: 02OCT26 calibration.cine); P16 is the same scale x 16.
        if fmt in ('P10', 'P12L') or bits == 12:
            black, white = PCC_BLACK_12, PCC_WHITE_12
        elif bits == 16:
            black, white = PCC_BLACK_12 * 16, PCC_WHITE_12 * 16
        else:
            black, white = 0, (1 << bits) - 1
        fields = dict(FrameRate=rate_i, FrameRateInt1516=rate_i, FrameRateDouble=float(ci['rate'] or 0), ShutterNs=exp_ns, PostTrigger=pt, ImWidth=res.width, ImHeight=res.height,
                      # legacy 16-bit copies: with FrameRate16 = 0 the vendor reader reports 10 fps
                      # (measured 2026-10-07); PCC writes min(value, 65535) and whole microseconds
                      FrameRate16=min(rate_i, 0xFFFF), PostTrigger16=min(pt, 0xFFFF),
                      Shutter=round(exp_ns / 1000), Shutter16=min(round(exp_ns / 1000), 0xFFFF),
                      ImWidthAcq=res.width, ImHeightAcq=res.height,
                      CameraVersion=int(self._safe_get('info.hwver', 0) or 0),
                      FirmwareVersion=int(self._safe_get('info.swver', 0) or 0),
                      RealBPP=12 if fmt == 'P10' else bits, BlackLevel=black, WhiteLevel=white,
                      Serial=int(serial or 0), Description=desc,
                      fGain=1.0, fGamma=1.0, fSaturation=1.0, fGain16_8=1.0, fGainR=1.0, fGainG=1.0, fGainB=1.0)
        frac64 = int(tfrac_us * (1 << 32) // 1_000_000)
        if not have_times:
            fields['Description'] += ' Time stamps SYNTHESIZED from image number / frame rate (camera gave none).'

        out_first, offset = renumbering(numbers, step) if step > 1 else (int(numbers[0]), 0)
        if step > 1:
            fields['Description'] += f' Image k = camera image k*{step}+{offset} (a cine numbers images consecutively).'
        try:
            dropped = self._write_download(path, res, numbers, packing, out_first, fields, tsec, frac64, ci,
                                           have_times, cine, fmt, chunk, year0, as_12bit, progress)
        except BaseException:
            Path(path).unlink(missing_ok=True)      # never leave a partial file that looks complete
            raise
        return dict(cine=cine, path=str(path), fmt=fmt, first=int(numbers[0]), last=int(numbers[-1]),
                    count=len(numbers), step=step, align=align, offset=offset, first_out=out_first,
                    camera=ci, times=have_times, as_12bit=as_12bit, dropped_low_bits=dropped)

    def _write_download(self, path, res, numbers, packing, out_first, fields, tsec, frac64, ci, have_times,
                        cine, fmt, chunk, year0, as_12bit, progress):
        with CineWriter(path, res.width, res.height, len(numbers), packing, first_image_no=out_first,
                        setup_fields=fields, trigger_time=(tsec, frac64),
                        first_movie_image=int(ci['firstfr']), total_image_count=int(ci['lastfr'] - ci['firstfr'] + 1),
                        with_times=True, with_exposures=have_times) as w:
            done = 0
            dropped = [0, 0]     # pixels whose low 4 bits were non-zero, pixels converted
            for num, frame, s in self.frames(cine, numbers, fmt, chunk, with_times=have_times):
                e = None
                # Without camera stamps: trigger time + number / rate (stated in the description).
                t_rel = int(round(num / float(ci['rate'] or 1) * (1 << 32)))
                t64 = (tsec << 32) + frac64 + t_rel
                t = (t64 >> 32, t64 & 0xFFFFFFFF)
                if s is not None:
                    sec = year0 + s.csecs // 100
                    usec = (s.csecs % 100) * 10000 + (s.frac >> 2)
                    t = (sec, int(usec * (1 << 32) // 1_000_000))
                    # The stamp's exposure is whole microseconds (16-bit). When it agrees with the
                    # cine's exposure setting (ns) to within that resolution, store the precise
                    # setting; otherwise (e.g. auto-exposure changed it) keep the stamp's value.
                    # exptime32/frac32 stay unused until checked on hardware.
                    exp_ns = int(ci['exp'] or 0)
                    if exp_ns and abs(s.exptime_us * 1000 - exp_ns) < 1000:
                        e = int(exp_ns * (1 << 32) // 1_000_000_000)
                    else:
                        e = int(s.exptime_us * (1 << 32) // 1_000_000)
                if as_12bit:
                    dropped[0] += int(np.count_nonzero(frame & 0xF))
                    dropped[1] += frame.size
                    frame = frame >> 4
                w.append(frame, time=t, exposure=e)
                done += 1
                if progress:
                    progress(done, len(numbers))
        return dropped if as_12bit else None

    def _safe_get(self, name, default=None):
        try:
            return self.get(name)
        except P.ProtocolError:
            return default
