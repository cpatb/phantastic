"""A simulated Phantom camera speaking the PH16 protocol, for tests, demos and protocol capture.

It answers UDP discovery (7380), accepts control connections (7115), opens data streams by
``startdata`` (connects back) or ``attach`` (accepts on 7116), and serves synthetic recordings
through ``img`` and ``time``. Response formatting copies real Miro M310 transcripts (tabs,
``\\<CRLF>`` continuation, ``Ok!``). Every received command is logged in ``received`` so the
simulator doubles as a capture tool: point any client (including vendor software) at it and
read exactly which commands it sends.

It is NOT an independent check of Phantastic's camera client (both were written from the same
reading of the spec); the independent checks are the real-camera transcripts in the tests and,
eventually, a real camera.

Run:  python -m phantastic.simulator [--host 127.0.0.1] [--port 7115]
"""
from __future__ import annotations

import argparse
import calendar
import logging
import socket
import socketserver
import struct
import threading
import time

from pathlib import Path

import numpy as np

from . import protocol as P
from .cine import load_linlut10, pack10, pack12L

log = logging.getLogger(__name__)

# Synthetic scene constants (12-bit sensor values)
BACKGROUND = 800
DISK_LEVEL = 3200
NOISE_SD = 12.0


def synthetic_frame(number: int, width: int, height: int, seed: int = 1) -> np.ndarray:
    """Deterministic 12-bit test frame for image ``number``: a disk crossing the field + noise.

    A horizontal ramp in the top 8 rows encodes the frame number (row 0 = number mod 4096) so a
    downloaded frame can be identified unambiguously.
    """
    rng = np.random.default_rng((seed, number & 0xFFFFFFFF))
    yy, xx = np.mgrid[0:height, 0:width]
    cx = (number * 3) % max(width, 1)
    cy = height / 2
    r = max(4.0, min(width, height) / 8)
    img = np.full((height, width), BACKGROUND, np.float64) + rng.normal(0, NOISE_SD, (height, width))
    img[(xx - cx) ** 2 + (yy - cy) ** 2 < r * r] = DISK_LEVEL
    out = np.clip(np.rint(img), 0, 4095).astype(np.uint16)
    out[0, :] = number % 4096
    return out


def _inverse_lut():
    lut = load_linlut10().astype(np.int64)
    # 12-bit value -> nearest 10-bit code (for serving P10)
    v = np.arange(4096)
    idx = np.searchsorted(lut, v)
    idx = np.clip(idx, 1, 1023)
    left, right = lut[idx - 1], lut[idx]
    return np.where(np.abs(v - left) <= np.abs(right - v), idx - 1, idx).astype(np.uint16)


class CameraModel:
    """State of the simulated camera."""

    def __init__(self, serial=99001, name='Phantastic Simulator', ncines=4, width=256, height=256,
                 profile: str | None = None):
        self.lock = threading.RLock()
        self.info = {
            'pver': 16, 'model': 'Phantastic Simulated Camera', 'serial': serial, 'hwver': 9999,
            'name': name, 'swver': 1, 'fver': 1, 'xmax': 1280, 'ymax': 800, 'xinc': 16, 'yinc': 8,
            'maxcines': 17, 'kernsz': 24, 'cinemem': 8192, 'minexp': 1000, 'maxrate': 500000,
            'features': 'attach notify earlyimg', 'imgformats': '8 8R P16 P16R P10 P12L',
            'xver': 0, 'cfa': 0,
        }
        # 'hw.*' values are queried by the vendor library during its connect handshake (captured
        # with this simulator). Their meaning is not documented; these are placeholders.
        self.hw = {'vresh': 0, 'vresds': 0, 'vpix': 0, 'vpixl': 0, 'smppw': 0, 'prepw': 0, 'mcode': 0}
        self.info.update({'kernel': 0, 'sensor': 0})
        self.eth = {'ip': '127.0.0.1', 'netmask': '255.0.0.0', 'broadcast': '127.255.255.255',
                    'gateway': '0.0.0.0'}
        self.permissive = False   # capture mode: unknown 'get' leaves answer 0 instead of ERR
        self.unknown: list[str] = []
        self.extra: dict = {}     # structures this model does not simulate, answered from a profile
        self.defc = {'res': P.Resolution(width, height), 'rate': 10000, 'exp': 90000, 'shoff': 0,
                     'edrexp': 0, 'ptframes': 200, 'frcount': 1000, 'decimation': 1}
        self.cam = {'syncimg': 0, 'frdelay': 0, 'trigpol': 0, 'tsformat': 1, 'cines': ncines, 'membpp': 12,
                    'timezone': 0}
        self.rtc_offset = 0.0   # set by 'setrtc' (vendor software sends the PC clock on connect)
        self.irig = {'flags': 0, 'sec': 0, 'yearbegin': 0}   # 'sec' is answered live: time.time() + rtc_offset
        self.auto: dict | None = None   # 'auto' and 'meta' exist only when a profile defines them
        self.meta: dict | None = None
        self.bref_started: float | None = None
        self.ncines = ncines
        self.cines = {}
        self.partition(ncines)
        self.active = 0
        self.received: list[str] = []
        if profile:
            self._load_profile(profile)

    def _load_profile(self, profile):
        """Fill in parameters from a real camera's tree (data/<profile>_tree.txt).

        Simulated state (defc, cines, identity) is kept; other structures and missing leaves are
        answered exactly as that camera answered them.
        """
        path = Path(__file__).with_name('data') / f'{profile.replace("-", "_")}_tree.txt'
        for line in path.read_text(encoding='latin-1').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            name = line.split(' : ', 1)[0].strip()
            value = P.parse_get(line)
            own = {'info': self.info, 'cam': self.cam, 'hw': self.hw, 'eth': self.eth, 'irig': self.irig,
                   'defc': self.defc}.get(name)
            if own is not None and isinstance(value, dict):
                for k, v in value.items():
                    own.setdefault(k, v)
                if name == 'info' and 'features' in value:    # the profile camera's feature list
                    self.info['features'] = value['features']
                if name == 'irig':      # that camera's fields as answered; 'sec' then runs from where its clock was
                    self.irig.update(value)
                    if isinstance(value.get('sec'), int):
                        self.rtc_offset = value['sec'] - time.time()
            elif name in ('auto', 'meta') and isinstance(value, dict):
                setattr(self, name, value)                    # settable, like the real camera's
            elif not (name[0] == 'c' and name[1:].isdigit()):
                self.extra[name] = value

    def partition(self, n):
        self.ncines = n
        self.cines = {0: {'state': ['RDY', 'DEF', 'PRE', 'ACT']}}
        for c in range(1, n + 1):
            self.cines[c] = {'state': ['RDY', 'DEF', 'ABL']}
        self.active = 0

    # Per-cine image-processing settings the vendor library reads before every download
    # ("adj", captured with this simulator). Neutral values: the simulator applies no processing.
    NEUTRAL_ADJ = {'red': 1, 'green': 1, 'blue': 1, 'wbtemp': 5600, 'wbcc': 0, 'wbred': 1.0, 'wbblue': 1.0,
                   'offset': 0, 'gain': 1, 'gamma': 1, 'rgamma': 0, 'bgamma': 0, 'toe': 1, 'flare': 0,
                   'hue': 0, 'sat': 1, 'rped': 0, 'gped': 0, 'bped': 0, 'chroma': 1, 'tone': '',
                   'cmatrix': '', 'umatrix': '', 'matrix': 0, 'filter': ''}

    def cine_struct(self, c):
        cn = self.cines[c]
        out = {'state': P.Flags(tuple(cn['state']))}
        if 'firstfr' in cn:
            out.update({k: cn[k] for k in ('firstfr', 'lastfr', 'frcount', 'res', 'rate', 'exp', 'ptframes')})
            out['in'], out['out'] = cn['firstfr'], cn['lastfr']
            out['trigtime'] = {'secs': cn['trigsecs'], 'frac': cn['trigfrac']}
            r = cn['res']
            out['adj'] = dict(self.NEUTRAL_ADJ)
            out['meta'] = {'crop': 0, 'ox': 0, 'oy': 0, 'w': r.width, 'h': r.height, 'resize': 0,
                           'ow': r.width, 'oh': r.height, 'tcrate': 0, 'pbrate': 0, 'trigtc': ''}
            if 'meta' in cn:     # name / comment as they were when the cine was stored (PCC p.33)
                out['meta'].update(cn['meta'])
        return out

    def record(self, c=None):
        if c is None:
            c = next((k for k in range(1, self.ncines + 1) if 'RDY' in self.cines[k]['state']), None)
            if c is None:
                raise P.ProtocolError('no ready cine')
        if c == 0:
            self.active = 0
            self.cines[0]['state'] = ['RDY', 'DEF', 'PRE', 'ACT']
            for k in range(1, self.ncines + 1):
                if 'ACT' in self.cines[k]['state']:
                    self.cines[k] = {'state': ['RDY', 'DEF', 'ABL']}
            return
        if c not in self.cines:
            raise P.ProtocolError('invalid cine number')
        self.cines[0]['state'] = ['RDY', 'DEF', 'PRE']
        self.cines[c] = {'state': ['WTR', 'DEF', 'ABL', 'ACT']}
        self.active = c

    BREF_DURATION_S = 0.6   # simulated CSR length; bref_progress rises 1..99 during it, then reads 0

    def bref_progress(self) -> int:
        if self.bref_started is None:
            return 0
        f = (time.monotonic() - self.bref_started) / self.BREF_DURATION_S
        if f >= 1:
            self.bref_started = None
            return 0
        return max(1, min(99, int(100 * f)))

    def settable(self) -> dict:
        roots = {'defc': self.defc, 'cam': self.cam, 'info': self.info}
        for name in ('auto', 'meta'):
            if getattr(self, name) is not None:
                roots[name] = getattr(self, name)
        return roots

    def trigger(self):
        c = self.active
        if c == 0:
            return
        d = self.defc
        now = time.time()
        pt = int(d['ptframes'])
        self.cines[c] = {
            'state': ['STR', 'DEF', 'TRG'], 'firstfr': pt - int(d['frcount']), 'lastfr': pt - 1,
            'frcount': int(d['frcount']), 'res': d['res'], 'rate': d['rate'], 'exp': d['exp'],
            'ptframes': pt, 'trigsecs': int(now), 'trigfrac': int((now % 1) * 1e6),
        }
        if self.meta is not None:
            self.cines[c]['meta'] = {'name': self.meta.get('name', ''), 'comment': self.meta.get('comment', '')}
        nxt = next((k for k in range(c + 1, self.ncines + 1) if 'RDY' in self.cines[k]['state']), 0)
        self.active = nxt
        if nxt:
            self.cines[nxt]['state'] = ['WTR', 'DEF', 'ABL', 'ACT']
        else:
            self.cines[0]['state'] = ['RDY', 'DEF', 'PRE', 'ACT']


def _same_kind(old, new) -> bool:
    """int stays int, a float variable takes int or float, a string stays a string."""
    if isinstance(old, str) or isinstance(new, str):
        return isinstance(old, str) and isinstance(new, str)
    if isinstance(old, float):
        return isinstance(new, (int, float))
    return isinstance(old, int) and isinstance(new, int)


def format_struct(v, indent=0) -> str:
    """Render a value the way the camera does (tabs, ``\\<CRLF>`` line breaks)."""
    tab = '\t' * (indent + 1)
    if isinstance(v, dict):
        items = [f'{tab}{k} : {format_struct(x, indent + 1)}' for k, x in v.items()]
        return '{\t\t\\\r\n' + ',\t\\\r\n'.join(items) + ' \t\\\r\n' + '\t' * indent + '}'
    if isinstance(v, P.Resolution):
        return f'{v.width} x {v.height}'
    if isinstance(v, P.Flags):
        return '{ ' + ' '.join(v.names) + ' }'
    if isinstance(v, str):
        return '"' + v + '"'
    if isinstance(v, float):
        return f'{v:.10g}'
    return str(v)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        sim: Simulator = self.server.sim
        peer = self.client_address[0]
        state = {'data': None}
        while True:
            line = self.rfile.readline()
            if not line:
                break
            text = line.decode('latin-1').strip()
            if not text:
                continue
            with sim.model.lock:
                sim.model.received.append(text)
            log.info('<< %s', text)
            try:
                resp = sim.execute(text, peer, state)
            except P.ProtocolError as e:
                resp = f'ERR: {e}'
            except Exception as e:  # a simulator must answer, never crash the connection
                log.exception('simulator error')
                resp = f'ERR: internal {type(e).__name__}'
            payload = resp if isinstance(resp, str) else resp[0]
            self.wfile.write(payload.encode('latin-1') + b'\r\n')
            self.wfile.flush()
            if not isinstance(resp, str):
                data = resp[1]
                if state['data'] is None:
                    log.warning('img/time requested without a data stream')
                else:
                    state['data'].sendall(data)
        if state['data'] is not None:
            state['data'].close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Simulator:
    def __init__(self, host='127.0.0.1', port=P.CONTROL_PORT, discovery_port=P.DISCOVERY_PORT,
                 attach_port=P.ATTACH_PORT, model: CameraModel | None = None, discovery=True):
        self.model = model or CameraModel()
        self.host = host
        self.ctrl = _Server((host, port), _Handler)
        self.ctrl.sim = self
        self.port = self.ctrl.server_address[1]
        self.attach_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.attach_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.attach_srv.bind((host, attach_port))
        self.attach_srv.listen(4)
        self.attach_port = self.attach_srv.getsockname()[1]
        self._attached = {}   # client port -> socket
        self.udp = None
        if discovery:
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.udp.bind(('', discovery_port))
        self._ilut = None
        self._threads = []

    # ------------------------------------------------------------ lifecycle
    def start(self):
        for target in (self.ctrl.serve_forever, self._attach_loop) + ((self._udp_loop,) if self.udp else ()):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        if self._threads:              # shutdown() blocks forever if serve_forever never ran
            self.ctrl.shutdown()
        self.ctrl.server_close()
        for s in [self.attach_srv, self.udp] + list(self._attached.values()):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    def _udp_loop(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(1024)
            except OSError:
                return
            if data.rstrip(b'\0') == P.DISCOVERY_REQUEST:
                m = self.model
                reply = f'PH16 {self.port} {m.info["hwver"]} {m.info["serial"]} "{m.info["name"]}"'.encode() + b'\0'
                self.udp.sendto(reply, addr)

    def _attach_loop(self):
        while True:
            try:
                s, addr = self.attach_srv.accept()
            except OSError:
                return
            self._attached[addr[1]] = s

    # ------------------------------------------------------------ commands
    def execute(self, text: str, peer: str, state: dict):
        m = self.model
        cmd, _, arg = text.partition(' ')
        arg = arg.strip()
        with m.lock:
            if cmd == 'get':
                return self._get(arg)
            if cmd == 'set':
                self._set(arg)
                return 'Ok!'
            if cmd == 'cstats':
                lines = [f'c{c} : {format_struct(P.Flags(tuple(m.cines[c]["state"])))}' for c in sorted(m.cines)]
                return '\\\r\n'.join(lines)
            if cmd == 'rec':
                m.record(self._cine_arg(arg) if arg else None)
                return 'Ok!'
            if cmd == 'trig':
                m.trigger()
                return 'Ok!'
            if cmd == 'del':
                c = self._cine_arg(arg)
                m.cines[c] = {'state': ['RDY', 'DEF', 'ABL']}
                return 'Ok!'
            if cmd == 'partition':
                n = int(P.parse_value(arg)['num']) if arg.startswith('{') else int(arg or m.ncines)
                m.partition(n)
                return 'OK!'
            if cmd in ('notify', 'clean'):
                return 'Ok!'
            if cmd == 'setrtc':
                v = P.parse_value(arg)
                secs = int(v['value']) if isinstance(v, dict) else int(v)
                m.rtc_offset = secs - time.time()
                return 'Ok!'
            if cmd == 'bref':
                if 'bref' not in m.info['features'].split():
                    raise P.ProtocolError('unknown command bref')
                m.bref_started = time.monotonic()
                return 'Ok!'
        if cmd == 'startdata':
            port = self._port_arg(arg)
            s = socket.create_connection((peer, port), timeout=5)
            if state['data'] is not None:
                state['data'].close()
            state['data'] = s
            return 'Ok!'
        if cmd == 'attach':
            port = self._port_arg(arg)
            for _ in range(50):
                if port in self._attached:
                    break
                time.sleep(0.02)
            if port not in self._attached:
                raise P.ProtocolError('attach failure')
            if state['data'] is not None:
                state['data'].close()
            state['data'] = self._attached.pop(port)
            return 'Ok!'
        if cmd == 'img':
            return self._img(arg)
        if cmd == 'time':
            return self._time(arg)
        raise P.ProtocolError(f'unknown command {cmd}')

    @staticmethod
    def _cine_arg(arg):
        # 'rec 1' (spec) or 'rec {cine: 1}' (what the vendor library sends)
        v = P.parse_value(arg)
        return int(v['cine']) if isinstance(v, dict) else int(v)

    @staticmethod
    def _port_arg(arg):
        v = P.parse_value(arg)
        return int(v['port']) if isinstance(v, dict) else int(v)

    def _resolve(self, path):
        m = self.model
        if path.endswith('.*'):        # 'get defc.*' == 'get defc' (vendor software uses this form)
            path = path[:-2]
        parts = path.split('.')
        irig = dict(m.irig, sec=int(time.time() + m.rtc_offset))
        if m.auto is not None:
            m.auto['bref_progress'] = m.bref_progress()
        roots = {**m.extra, **m.settable(), 'hw': m.hw, 'eth': m.eth, 'irig': irig}
        head = parts[0]
        if head in roots:
            node = roots[head]
        elif head.startswith('c') and head[1:].isdigit() and int(head[1:]) in m.cines:
            node = m.cine_struct(int(head[1:]))
        else:
            raise P.ProtocolError(f'name {path}  is unknown ')
        for p in parts[1:]:
            if not isinstance(node, dict) or p not in node:
                raise P.ProtocolError(f'name {path}  is unknown ')
            node = node[p]
        return parts[-1], node

    def _get(self, path):
        if not path:
            raise P.ProtocolError('expecting  varname')
        if path == '*':
            raise P.ProtocolError('get * not simulated')
        try:
            name, val = self._resolve(path)
        except P.ProtocolError:
            if not self.model.permissive:
                raise
            self.model.unknown.append(path)
            log.info('   (capture mode: unknown %s answered 0)', path)
            name, val = path.split('.')[-1], 0
        return f'{name} : {format_struct(val)}'

    def _set(self, arg):
        # 'set name value' or 'set name:value' (the vendor library sends the latter)
        colon, space = arg.find(':'), arg.find(' ')
        if colon >= 0 and (space < 0 or colon < space):     # 'set meta.comment:"two words"'
            path, _, value = arg.partition(':')
        else:
            path, _, value = arg.partition(' ')
        v = P.parse_value(value)
        m = self.model
        roots = m.settable()
        if path in ('defc', 'cam') and isinstance(v, dict):
            target = roots[path]
            for k, x in v.items():
                if k not in target:
                    raise P.ProtocolError(f'name {path}.{k}  is unknown ')
                target[k] = x
            return
        parts = path.split('.')
        node = roots.get(parts[0])
        for p in parts[1:-1]:
            node = node.get(p) if isinstance(node, dict) else None
        leaf = parts[-1]
        if len(parts) < 2 or not isinstance(node, dict) or leaf not in node or isinstance(node[leaf], dict):
            raise P.ProtocolError(f'name {path}  is unknown ')
        if parts[0] == 'info' and leaf != 'name':
            raise P.ProtocolError('read only')
        if parts[0] in ('cam', 'auto', 'meta') and not _same_kind(node[leaf], v):
            # stricter than any camera is known to be: catches a client sending the wrong type
            raise P.ProtocolError(f'{path} expects {type(node[leaf]).__name__}, got {type(v).__name__}')
        node[leaf] = v

    def _cine_frames(self, c, start, cnt):
        m = self.model
        if c == -1:
            res = m.defc['res']
            return res, [synthetic_frame(int(time.time() * 1000) % 100000, res.width, res.height)]
        if c not in m.cines:
            raise P.ProtocolError('invalid cine number')
        cn = m.cines[c]
        if 'STR' not in cn['state']:
            raise P.ProtocolError('cine status invalid')
        if not cn['firstfr'] <= start <= cn['lastfr']:
            raise P.ProtocolError('start frame outside range')
        if start + cnt - 1 > cn['lastfr']:
            raise P.ProtocolError('start+count frame outside range')
        res = cn['res']
        return res, [synthetic_frame(n, res.width, res.height, seed=c) for n in range(start, start + cnt)]

    def _img(self, arg):
        a = P.parse_value(arg)
        c, start, cnt = int(a['cine']), int(a.get('start', 0)), int(a['cnt'])
        if cnt <= 0:
            raise P.ProtocolError('count should be > 0')
        fmt = P.format_number(a.get('fmt', '8'))
        if fmt not in P.IMAGE_FORMATS or fmt not in self.model.info['imgformats'].split():
            raise P.ProtocolError('unsupported image format')
        res, frames = self._cine_frames(c, start, 1 if c == -1 else cnt)
        blobs = []
        for f in frames:
            if fmt in ('8', '8R'):
                blobs.append((f >> 4).astype(np.uint8).tobytes())
            elif fmt in ('P16', 'P16R'):
                # MSB-aligned: the vendor library reads P16 as full-scale 16-bit and divides by 16
                # (measured against this simulator), matching the spec's "range 0-65535".
                blobs.append((f.astype(np.uint16) << 4).astype('<u2').tobytes())
            elif fmt == 'P10':
                if self._ilut is None:
                    self._ilut = _inverse_lut()
                blobs.append(b''.join(pack10(r) for r in self._ilut[f]))
            else:
                blobs.append(b''.join(pack12L(r) for r in f))
        num = P.IMAGE_FORMATS[fmt][0]
        return f'Ok! {{ cine: {c}, res: {res.width} x {res.height}, fmt: {num if num is not None else fmt} }}', b''.join(blobs)

    def _time(self, arg):
        a = P.parse_value(arg)
        c, start, cnt = int(a['cine']), int(a['start']), int(a['cnt'])
        m = self.model
        cn = m.cines.get(c)
        if cn is None or 'STR' not in cn['state']:
            raise P.ProtocolError('cine status invalid')
        if not (cn['firstfr'] <= start and start + cnt - 1 <= cn['lastfr']):
            raise P.ProtocolError('start frame outside range')
        year0 = calendar.timegm((time.gmtime(cn['trigsecs']).tm_year, 1, 1, 0, 0, 0))
        out = []
        for n in range(start, start + cnt):
            t = cn['trigsecs'] + cn['trigfrac'] * 1e-6 + n / float(cn['rate']) - year0
            csecs = int(t * 100)
            us = int(round((t * 100 - csecs) * 10000)) % 10000
            exp = int(cn['exp'])     # like a v2512: whole us + exptime32 in 1/65536 us (frac32: sub-us time, 0 here)
            out.append(struct.pack('>IHHHH', csecs, min(exp // 1000, 65535), us << 2,
                                   (exp % 1000) * 65536 // 1000, 0))
        return f'Ok! {{cine: {c}, cnt: {cnt}, size: 12}}', b''.join(out)


def main():
    ap = argparse.ArgumentParser(description='Simulated Phantom PH16 camera')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=P.CONTROL_PORT)
    ap.add_argument('--attach-port', type=int, default=P.ATTACH_PORT)
    ap.add_argument('--no-discovery', action='store_true')
    ap.add_argument('--log', help='write every received command to this file')
    ap.add_argument('--profile', help="answer unsimulated parameters like a real camera, e.g. 'miro-m310'")
    ap.add_argument('--capture', action='store_true',
                    help='answer unknown get requests with 0 (to observe a client further)')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    sim = Simulator(a.host, a.port, attach_port=a.attach_port, discovery=not a.no_discovery,
                    model=CameraModel(profile=a.profile))
    sim.model.permissive = a.capture
    sim.start()
    print(f'simulated camera on {a.host}:{sim.port} (attach {sim.attach_port}); Ctrl+C to stop')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if a.log:
            with open(a.log, 'w') as f:
                f.write('\n'.join(sim.model.received) + '\n')
        sim.stop()


if __name__ == '__main__':
    main()
