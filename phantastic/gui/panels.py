"""MDI image panels: the live Preview panel of a camera and the Playback panel of a cine.

A Playback panel plays either a saved file (``FileSource``, read through ``CineReader``) or a
cine still in camera memory (``CameraCineSource``), whose frames are fetched on demand in the
background through the ``CameraSession`` lock. Display range, curve, rotation, flips and overlays
change the screen only; ``panel.frame`` always holds the values as read (file) or as sent (camera).

Crop rectangle (``crop_rect`` / ``set_crop_rect`` / ``crop_changed``) and measurements are kept
per panel in STORED-array coordinates (0-based, row 0 at the top, as ``CineReader.read`` returns).
"""
from __future__ import annotations

import collections
import datetime as _dt
import re
import time
import warnings
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .. import protocol as P
from ..cine import TIME64_SCALE, CineReader
from ..defects import FLAG_P16, fill, flagged
from ..measure import Measurement, px_per_unit
from .imageview import (GAIN_RANGE, GAMMA_RANGE, IDENTITY_CURVE, TOE_RANGE, BRIGHTNESS_RANGE, ImageView,
                        auto_range, display_curve, focus_overlay, zebra_overlay)
from .widgets import PanelStatusLine
from .workers import CameraBusy, CameraSession, TaskManager, describe_error

LIVE_INTERVAL_MS = 40          # request ceiling ~25 live frames/s; the camera/link decides the real rate
P16_SENSOR_BITS = 12           # P16 carries a 12-bit value v as 16*v (README; v2512, 2026-10-07)
P16_SATURATED = ((1 << P16_SENSOR_BITS) - 1) << (16 - P16_SENSOR_BITS)   # 65520: zebra level for 16-bit data
CLOCK_INTERVAL_MS = 1000
CAMERA_CACHE_FRAMES = 32       # recently fetched camera frames kept for stepping back and forth
CAMERA_FETCH_LOCK_S = 2.0      # wait this long for the camera lock (live view holds it briefly)
DEFAULT_PLAY_FPS = 20.0        # this user's PCC setting (settings.xml speed=20)

# The mapping phrase Phantastic writes into a decimated file's Description:
#   camera download: "Image k = camera image k*N+off"; decimate_cine: "image k = source image k*N+off".
# Each decimation appends its phrase, so a file decimated twice carries both, oldest first.
_MAPPING_RE = re.compile(r'image k = (camera|source) image k\*(\d+)\+(\d+)', re.IGNORECASE)


def parse_mapping(description: str) -> tuple[str, int, int] | None:
    """Compose every mapping phrase into one: (origin, step, offset) with origin image = step*k + offset.

    The newest phrase maps this file to its immediate source, so phrases are applied newest first.
    origin is 'camera' when the chain starts at a camera download, else 'source'.
    """
    found = [(m.group(1).lower(), int(m.group(2)), int(m.group(3))) for m in _MAPPING_RE.finditer(description)]
    if not found:
        return None
    step, off = 1, 0
    for _, s, o in found:          # n_older = s*n_newer + o; compose from the oldest outward
        step, off = step * s, off + step * o
    return found[0][0], step, off


def format_abs_time(t: float | None, sep: str = '; ') -> str:
    """PCC's Frame Info time: 'hh:mm:ss.ms µs; Day Mon dd yyyy' (local time)."""
    if t is None:
        return '-'
    d = _dt.datetime.fromtimestamp(t)
    return f'{d:%H:%M:%S}.{d.microsecond // 1000:03d} {d.microsecond % 1000:03d}{sep}{d:%a %b %d %Y}'


def _fmt(v) -> str:
    if v is None:
        return '-'
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


def format_scaled(v: int, div: int) -> str:
    """``v / div`` for the readout: an integer as such, otherwise one decimal (P16 19753 / 16 -> '1234.6')."""
    q = v / div
    return str(int(q)) if q == int(q) else f'{q:.1f}'


# ----------------------------------------------------------------------------- sources

class FileSource:
    """A saved .cine file. Reads are synchronous (local disk)."""
    kind = 'file'

    def __init__(self, path):
        self.reader = CineReader(path)          # raises ValueError for a non-cine; the caller reports it
        r = self.reader
        self.path = str(path)
        self.first, self.last = r.first, r.first + len(r) - 1
        self.raw_max = (1 << r.real_bpp) - 1
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            self.rel = r.relative_times()
        self.times_synthesized = bool(w)
        self.abs = r.image_times() if r.has_complete_times() else None
        exp = r.exposures_raw()
        self.exposure_us = None if exp is None or len(exp) != len(r) else exp.astype(np.float64) * 1e6 / TIME64_SCALE
        self.mapping = parse_mapping(r.setup.get('Description', ''))

    @property
    def key(self):
        return ('file', self.path)

    @property
    def title(self) -> str:
        return Path(self.path).name

    def read(self, n: int) -> np.ndarray:
        return self.reader.read(self.reader.index_of(n))

    def frame_info(self, n: int, stamp=None) -> dict:
        i = n - self.first
        origin = None
        if self.mapping is not None:
            src, step, off = self.mapping
            origin = (src, n * step + off)
        return dict(abs=None if self.abs is None else float(self.abs[i]), elapsed_s=float(self.rel[i]),
                    exposure_us=None if self.exposure_us is None else float(self.exposure_us[i]),
                    number=n, origin=origin, synthesized=self.times_synthesized)

    def info_rows(self) -> list[tuple[str, str]]:
        r = self.reader
        rate = r.setup.get('FrameRate')
        shutter = r.setup.get('ShutterNs')
        trig = format_abs_time(r.trigger_time) if r.header.get('TriggerTime') else '-'
        rec_first = r.header.get('FirstMovieImage')
        rec_n = r.header.get('TotalImageCount')
        recorded = (f'{rec_first} .. {rec_first + rec_n - 1}' if rec_first is not None and rec_n else '-')
        rows = [('Source', self.path), ('Resolution', f'{r.width} x {r.height}'),
                ('Sample Rate', f'{_fmt(rate)} fps'),
                ('Exposure', '-' if shutter is None else f'{shutter / 1000:g} µs'),
                ('Recorded Range', recorded), ('Saved Range', f'{self.first} .. {self.last} ({len(r)} images)'),
                ('Bits per pixel', str(r.real_bpp)), ('Trigger Time', trig)]
        if self.mapping is not None:
            rows.append(('Image mapping', f'image k = {self.mapping[0]} image k*{self.mapping[1]}+{self.mapping[2]}'))
        if self.times_synthesized:
            rows.append(('Time stamps', 'none in file: image number / frame rate'))
        return rows

    def close(self):
        self.reader.close()


class CameraCineSource:
    """A stored cine in camera memory, before saving. Frames are fetched with ``img`` on demand.

    P16 is used when the camera offers it (values as sent: a 12-bit sensor value v arrives as
    16*v), else 8-bit. Time stamps come from the camera's ``time`` command.
    """
    kind = 'camera'

    def __init__(self, session: CameraSession, cine: int, info: dict):
        self.session, self.cine, self.info = session, cine, dict(info)
        self.first, self.last = int(info['firstfr']), int(info['lastfr'])
        self.fmt = 'P16' if 'P16' in session.formats else '8'
        self.raw_max = 65535 if self.fmt == 'P16' else 255
        trig = info.get('trigtime') if isinstance(info.get('trigtime'), dict) else {}
        self.trig_secs = int(trig.get('secs', 0))
        self.trig_us = int(trig.get('frac', 0))
        self.year0 = (int(_dt.datetime(_dt.datetime.fromtimestamp(self.trig_secs, _dt.timezone.utc).year, 1, 1,
                                       tzinfo=_dt.timezone.utc).timestamp()) if self.trig_secs else None)
        self.cache: collections.OrderedDict[int, tuple[np.ndarray, P.TimeStamp | None]] = collections.OrderedDict()
        self.serial = session.info.get('serial', 'camera')

    @property
    def key(self):
        return ('camera', self.cine)

    @property
    def title(self) -> str:
        return f'{self.serial} > Cine {self.cine}'

    def fetch(self, n: int) -> tuple[int, np.ndarray, P.TimeStamp | None]:
        """Runs in a worker thread: one image and its time stamp."""
        with self.session.use(timeout=CAMERA_FETCH_LOCK_S) as cam:
            frames, _ = cam.read_images(self.cine, n, 1, self.fmt)
            try:
                stamp = cam.read_time_stamps(self.cine, n, 1)[0]
            except P.ProtocolError:
                stamp = None
            self.session.trim_transcript()
        return n, frames[0], stamp

    def store(self, n: int, frame: np.ndarray, stamp):
        self.cache[n] = (frame, stamp)
        self.cache.move_to_end(n)
        while len(self.cache) > CAMERA_CACHE_FRAMES:
            self.cache.popitem(last=False)

    def frame_info(self, n: int, stamp=None) -> dict:
        rate = float(self.info.get('rate') or 0)
        if stamp is not None and self.year0 is not None:
            t = self.year0 + stamp.csecs / 100.0 + (stamp.frac >> 2) * 1e-6
            elapsed = t - (self.trig_secs + self.trig_us * 1e-6)
            return dict(abs=t, elapsed_s=elapsed, exposure_us=self.exposure_us(stamp), number=n, origin=None,
                        synthesized=False)
        return dict(abs=None, elapsed_s=n / rate if rate else 0.0, exposure_us=self.exposure_us(None), number=n,
                    origin=None, synthesized=True)

    def exposure_us(self, stamp) -> float | None:
        """The cine's exposure (``exp``, ns). The time stamp's own field is whole µs and 16-bit (0.5 µs
        reads 0), so it is used only when the cine reports no exposure."""
        exp = self.info.get('exp')
        if exp is not None:
            return int(exp) / 1000.0
        return None if stamp is None else float(stamp.exptime_us)

    def info_rows(self) -> list[tuple[str, str]]:
        i = self.info
        trig = format_abs_time(self.trig_secs + self.trig_us * 1e-6) if self.trig_secs else '-'
        exp = i.get('exp')
        return [('Source', f'camera {self.serial}, cine {self.cine} (RAM, not saved)'),
                ('Resolution', _fmt(i.get('res'))), ('Sample Rate', f'{_fmt(i.get("rate"))} fps'),
                ('Exposure', '-' if exp is None else f'{int(exp) / 1000:g} µs'),
                ('Recorded Range', f'{self.first} .. {self.last} ({_fmt(i.get("frcount"))} images)'),
                ('Saved Range', 'not saved'), ('Post Trigger', _fmt(i.get('ptframes'))),
                ('Transfer format', f'{self.fmt} (values as sent by the camera)'), ('Trigger Time', trig)]

    def close(self):
        self.cache.clear()


# ----------------------------------------------------------------------------- panels

class ImagePanel(QWidget):
    """Image view + status line, raw frame, display curve (screen only), pixel readout, crop
    rectangle and measurements.

    Crop rectangle API (for saving / export code): ``crop_rect()`` returns ``(x, y, w, h)`` or None,
    in STORED-array coordinates: x = first column, y = first row (0-based, row 0 = the top row of
    the array ``CineReader.read`` returns, or the camera sends), w x h pixels, so the region is
    ``frame[y:y + h, x:x + w]``. Rotation, flips and zoom never change it. ``crop_changed`` is
    emitted with the new value (tuple or None) whenever it changes. Display only: the panel never
    crops anything itself.
    """
    readout_changed = Signal()
    frame_changed = Signal()
    display_changed = Signal()
    crop_changed = Signal(object)
    measurements_changed = Signal()
    calibration_requested = Signal(object, object)     # the two stored points, (x, y) each
    error = Signal(str)
    message = Signal(str)
    focus_allowed = False          # Focus Assist works on live images only (PCC p.17)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view = ImageView()
        self.status = PanelStatusLine()
        self.frame: np.ndarray | None = None
        self.raw_max = 255
        self.lo, self.hi = 0, 255
        self.gamma, self.gain, self.brightness, self.toe = (IDENTITY_CURVE[k] for k in
                                                            ('gamma', 'gain', 'brightness', 'toe'))
        self.curve_disabled = False
        self.zebra = False
        self.zebra_level: int | None = None      # None: the format's saturation level
        self.focus_assist = False
        self.readout_div, self.readout_bits = 1, None   # camera P16: show value / 16 on the 12-bit scale
        self.flag_value: int | None = None    # live P16: pixels the camera flags (0xFF00) are filled ON SCREEN only
        self._flag_mask: np.ndarray | None = None
        self._flag_src = self._flag_filled = None
        self._crop: tuple[int, int, int, int] | None = None
        self.scale: float | None = None           # pixels per unit (PCC 'Calibrate', p.81), per panel
        self.unit = ''
        self.measurements: list[Measurement] = []
        self.measure_purpose = 'measure'          # or 'calibrate': the next two clicks set the scale
        self.pending_point: dict | None = None
        self._auto_next = True
        self.last_readout: tuple[int, int, object] | None = None
        self._stamps: collections.deque[float] = collections.deque(maxlen=200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.status)
        self.view.hovered.connect(self._readout)
        self.view.clicked.connect(self._clicked)
        self.view.rect_drawn.connect(lambda x, y, w, h: self.set_crop_rect((x, y, w, h)))

    def title(self) -> str:
        return ''

    def display_source(self) -> np.ndarray | None:
        """What the screen is drawn from: ``frame``, or for live P16 a copy with the camera-flagged pixels
        (exactly 0xFF00, isolated) replaced by PCC's 8-neighbour mean (:mod:`phantastic.defects`). ``frame``
        itself, the readout and anything saved keep the raw values."""
        f = self.frame
        if f is None or self.flag_value is None or f.ndim != 2:
            self._flag_mask = None
            return f
        if self._flag_src is not f:
            self._flag_mask = flagged(f, self.flag_value)
            self._flag_filled = fill(f, self._flag_mask) if self._flag_mask.any() else f
            self._flag_src = f
        return self._flag_filled

    def show_frame(self, raw: np.ndarray):
        resized = self.frame is None or self.frame.shape[:2] != raw.shape[:2]
        self.frame = raw
        if self._auto_next:
            self._auto_next = False
            self.lo, self.hi = auto_range(self.display_source())   # initial display range (screen only)
            self.display_changed.emit()
        if resized and self._crop is not None:
            self.set_crop_rect(self._crop)        # re-clamp to the new image size
        self.render()
        now = time.monotonic()
        self._stamps.append(now)
        if self.last_readout is not None:
            self._readout(*self.last_readout[:2])
        self.frame_changed.emit()

    # ------------------------------------------------------------------ display (screen only)
    @property
    def sat_level(self) -> int:
        """Raw value of a saturated pixel: the sensor's full scale (camera P16: 4095 * 16)."""
        if self.readout_bits is not None:
            return ((1 << self.readout_bits) - 1) * self.readout_div
        if self.raw_max == 65535:   # 16-bit data is usually a 12-bit sensor's P16: 4095 * 16 + a 0-15
            return P16_SATURATED     # correction fraction never reaches 65535 (README 'P16 is full scale')
        return self.raw_max

    def display_array(self) -> np.ndarray | None:
        """The uint8 display copy of ``frame`` in stored orientation, overlays included."""
        f = self.frame
        if f is None:
            return None
        f = self.display_source()
        if self.curve_disabled:
            a8 = display_curve(f, 0, self.raw_max)
        else:
            a8 = display_curve(f, self.lo, self.hi, self.gamma, self.gain, self.brightness, self.toe)
        if self.focus_assist and self.focus_allowed:
            a8 = focus_overlay(a8, f)
        if self.zebra:
            a8 = zebra_overlay(a8, f, self.sat_level if self.zebra_level is None else self.zebra_level)
        return a8

    def render(self):
        if self.frame is not None:
            self.view.set_array(self.display_array())

    def set_display_range(self, lo: int, hi: int):
        self.lo, self.hi = int(lo), int(max(hi, lo + 1))
        self.render()
        self.display_changed.emit()

    def set_curve(self, gamma: float | None = None, gain: float | None = None, brightness: float | None = None,
                  toe: float | None = None, disabled: bool | None = None):
        """Gamma / Gain / Brightness / Toe of the display curve (``display_curve``); screen only."""
        new = [(name, v, rng) for name, v, rng in (('gamma', gamma, GAMMA_RANGE), ('gain', gain, GAIN_RANGE),
               ('brightness', brightness, BRIGHTNESS_RANGE), ('toe', toe, TOE_RANGE)) if v is not None]
        for name, v, (a, b) in new:              # all checked before any is set
            if not a <= float(v) <= b:
                raise ValueError(f'{name} {v} outside [{a}, {b}]')
        for name, v, _ in new:
            setattr(self, name, float(v))
        if disabled is not None:
            self.curve_disabled = bool(disabled)
        self.render()
        self.display_changed.emit()

    def default_display(self):
        """Image Tools 'Default' (PCC p.47): identity curve, enabled, range fitted to this frame."""
        self.gamma, self.gain, self.brightness, self.toe = (IDENTITY_CURVE[k] for k in
                                                            ('gamma', 'gain', 'brightness', 'toe'))
        self.curve_disabled = False
        if self.frame is not None:
            self.lo, self.hi = auto_range(self.display_source())
        self.render()
        self.display_changed.emit()

    def adjustments(self) -> dict:
        """The display settings (what a .phadj file holds)."""
        v = self.view
        return dict(lo=self.lo, hi=self.hi, gamma=self.gamma, gain=self.gain, brightness=self.brightness,
                    toe=self.toe, disabled=self.curve_disabled, flip_h=v.flip_h, flip_v=v.flip_v, rot=v.rot)

    def apply_adjustments(self, d: dict):
        self.set_curve(gamma=d.get('gamma'), gain=d.get('gain'), brightness=d.get('brightness'), toe=d.get('toe'),
                       disabled=d.get('disabled'))
        v = self.view
        v.flip_h, v.flip_v, v.rot = d.get('flip_h', v.flip_h), d.get('flip_v', v.flip_v), d.get('rot', v.rot)
        if 'lo' in d and 'hi' in d:
            self.set_display_range(int(d['lo']), int(d['hi']))
        v.refresh_hover()

    def set_overlays(self, zebra: bool | None = None, focus: bool | None = None, zebra_level=False):
        """Zebra / Focus Assist on or off; ``zebra_level`` an int (raw) or None (saturation). Screen only."""
        if zebra is not None:
            self.zebra = bool(zebra)
        if focus is not None:
            self.focus_assist = bool(focus)
        if zebra_level is not False:
            self.zebra_level = None if zebra_level is None else int(zebra_level)
        self.render()

    def auto(self):
        """Fit the display range to THIS frame, 0.35 % saturated. Display only; data unchanged."""
        if self.frame is not None:
            self.set_display_range(*auto_range(self.display_source()))

    def full(self):
        self.set_display_range(0, self.raw_max)

    def refresh_rate(self) -> float | None:
        now = time.monotonic()
        recent = [t for t in self._stamps if now - t <= 1.0]
        if len(recent) < 2:
            return None
        return (len(recent) - 1) / (recent[-1] - recent[0]) if recent[-1] > recent[0] else None

    # ------------------------------------------------------------------ readout
    def _readout(self, x: int, y: int):
        f = self.frame
        if f is None or x < 0 or y < 0 or y >= f.shape[0] or x >= f.shape[1]:
            self.last_readout = None
        else:
            v = f[y, x]            # the raw value, also at a flagged pixel
            self.last_readout = (x, y, tuple(int(c) for c in v) if np.ndim(v) else int(v))
        self.readout_changed.emit()

    def readout_text(self) -> str:
        """Status-bar value text. Raw values; camera P16 on the sensor's 12-bit scale (value / 16)."""
        if self.last_readout is None:
            return 'Value:'
        v = self.last_readout[2]
        if isinstance(v, tuple):
            tag = f' ({self.readout_bits}-bit)' if self.readout_div != 1 else ''
            return 'RGB: ' + ','.join(format_scaled(c, self.readout_div) for c in v) + tag
        x, y = self.last_readout[:2]
        m = self._flag_mask
        tag = ' flagged by camera' if m is not None and y < m.shape[0] and x < m.shape[1] and m[y, x] else ''
        if self.readout_div != 1:
            return f'Value: {format_scaled(v, self.readout_div)} ({self.readout_bits}-bit){tag}'
        return f'Value: {v}{tag}'

    # ------------------------------------------------------------------ crop rectangle (display only)
    def crop_rect(self) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) in stored-array coordinates, or None. See the class docstring."""
        return self._crop

    def set_crop_rect(self, rect):
        """Set (clamped to the image when one is shown) or clear (None) the crop rectangle."""
        if rect is not None:
            x, y, w, h = (int(v) for v in rect)
            if self.frame is not None:
                fh, fw = self.frame.shape[:2]
                x0, y0 = min(max(x, 0), fw), min(max(y, 0), fh)
                x1, y1 = min(max(x + w, 0), fw), min(max(y + h, 0), fh)
                x, y, w, h = x0, y0, x1 - x0, y1 - y0
            rect = (x, y, w, h) if w > 0 and h > 0 else None
        if rect != self._crop:
            self._crop = rect
            self.view.crop = rect
            self.view.update()
            self.crop_changed.emit(rect)

    # ------------------------------------------------------------------ measurement (PCC p.81-85)
    def point_time(self) -> tuple[int | None, float | None, bool]:
        """(image number, seconds from trigger, synthesized) of the image on screen; live: no time."""
        return None, None, False

    def start_calibration(self):
        self.measure_purpose, self.pending_point = 'calibrate', None
        self.view.marks = []
        self.view.update()

    def set_scale(self, px_per: float | None, unit: str = ''):
        if px_per is not None and not px_per > 0:
            raise ValueError(f'scale must be positive, got {px_per}')
        self.scale, self.unit = px_per, (unit if px_per is not None else '')
        self.measurements_changed.emit()

    def calibrate(self, p1, p2, length: float, unit: str):
        self.set_scale(px_per_unit(p1, p2, length), unit)

    def clear_measurements(self):
        self.measurements.clear()
        self.pending_point = None
        self.view.marks = []
        self.view.update()
        self.measurements_changed.emit()

    def _clicked(self, x: int, y: int):
        n, t, synth = self.point_time()
        pt = dict(p=(x, y), image=n, t=t, synth=synth)
        if self.pending_point is None:
            self.pending_point = pt
            self.view.marks = [('point', x, y)]
            self.view.update()
            self.measurements_changed.emit()
            return
        a, self.pending_point = self.pending_point, None
        self.view.marks = [('point', *a['p']), ('point', x, y), ('line', *a['p'], x, y)]
        self.view.update()
        if self.measure_purpose == 'calibrate':
            self.measure_purpose = 'measure'
            self.calibration_requested.emit(a['p'], (x, y))
            return
        self.measurements.append(Measurement(a['p'], (x, y), a['image'], n, a['t'], t, a['synth'] or synth))
        self.measurements_changed.emit()


class PreviewPanel(ImagePanel):
    """Live preview of a camera: polls live frames in the background while visible.

    Full bit depth: P16 when the camera offers it (12-bit x 16, 2x the bytes of 8-bit), else 8-bit;
    a camera that refuses P16 live falls back to 8-bit once (PCC's 'Image display transfer', p.161).
    """
    focus_allowed = True

    def __init__(self, tasks: TaskManager, session: CameraSession, parent: QWidget | None = None):
        super().__init__(parent)
        self.tasks, self.session = tasks, session
        self.set_format('P16' if 'P16' in session.formats else '8')
        self.view.placeholder = 'Waiting for live image...'
        self.frames_received = 0
        self.paused = False
        self.recording = False
        self._busy = False
        self.status.live.set_on(True)
        self.status.info.linkActivated.connect(lambda _: self.resume())
        self.timer = QTimer(self)
        self.timer.setInterval(LIVE_INTERVAL_MS)
        self.timer.timeout.connect(self._tick)
        self.clock = QTimer(self)
        self.clock.setInterval(CLOCK_INTERVAL_MS)
        self.clock.timeout.connect(self._clock)
        self.timer.start()
        self.clock.start()
        self._clock()

    def title(self) -> str:
        info = self.session.info
        name = info.get('name') or info.get('model') or 'camera'
        return f'{name} ({info.get("serial")})'

    def set_format(self, fmt: str):
        """Live transfer format: 'P16' (raw_max 65535, readout on the 12-bit scale) or '8'."""
        self.fmt = fmt
        p16 = fmt == 'P16'
        self.raw_max = 65535 if p16 else 255
        self.readout_div, self.readout_bits = (1 << (16 - P16_SENSOR_BITS), P16_SENSOR_BITS) if p16 else (1, None)
        self.flag_value = FLAG_P16 if p16 else None    # flags seen in corrected P16 only (phantastic.defects)
        self._flag_src = None
        self._auto_next = True
        self.display_changed.emit()

    def _clock(self):
        self.status.time.setText(f'Current Time: {time.strftime("%H:%M:%S  %a %b %d %Y")}')

    def set_recording(self, on: bool):
        self.recording = on
        self.status.rec.set_on(on)

    def resume(self):
        self.paused = False
        self.status.live.set_on(True)
        self.status.info.setText('')

    def pause(self, why: str = ''):
        self.paused = True
        self.status.live.set_on(False)
        self.status.info.setText(f'{why} <a href="resume">Resume live</a>')

    def active(self) -> bool:
        win = self.window()
        return (self.session is not None and not self.paused and self.isVisible()
                and not (win is not None and win.isMinimized()))

    def stop(self):
        self.timer.stop()
        self.clock.stop()
        self.session = None

    def _tick(self):
        if self._busy or not self.active():
            return
        session, fmt = self.session, self.fmt
        self._busy = True

        def job(task):
            with session.try_use() as cam:
                return None if cam is None else cam.live_image(fmt)

        def done(r):
            self._busy = False
            if r is None or session is not self.session or fmt != self.fmt:
                return
            frame, _ = r
            self.frames_received += 1
            self.show_frame(frame)

        def failed(e):
            self._busy = False
            if session is not self.session:
                return
            if fmt == 'P16' and self.frames_received == 0 and isinstance(e, P.ProtocolError):
                self.set_format('8')
                self.message.emit(f'Live view: P16 refused ({describe_error(e)}); showing 8-bit')
                return
            self.pause('Live view paused.')   # do not repeat a failing request 25 times a second
            self.error.emit(f'Live view paused: {describe_error(e)}')
        self.tasks.submit(job, done, failed)


class PlaybackPanel(ImagePanel):
    """Playback of one cine. The Play tab drives it; ``position_changed`` reports any change of the
    current image or of the marks."""
    position_changed = Signal()

    def __init__(self, tasks: TaskManager, source, parent: QWidget | None = None):
        super().__init__(parent)
        self.tasks, self.source = tasks, source
        self.raw_max = source.raw_max
        self.lo, self.hi = 0, source.raw_max
        if source.kind == 'camera' and source.fmt == 'P16':     # files: the stored value, unscaled
            self.readout_div, self.readout_bits = 1 << (16 - P16_SENSOR_BITS), P16_SENSOR_BITS
        self.cur = self.shown = max(source.first, min(0, source.last))   # open at the trigger image
        self.mark_in, self.mark_out = source.first, source.last
        self.limit_to_range, self.repeat, self.ping_pong = True, True, False   # this user's PCC settings
        self.fps = DEFAULT_PLAY_FPS
        self.player_step = 1
        self.direction = 0
        self.stamp = None
        self._busy = False
        self.stopped = False
        self.view.placeholder = 'Loading image...' if source.kind == 'camera' else 'No image'
        self.status.play.set_on(True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.goto(self.cur)

    def title(self) -> str:
        return self.source.title

    # ------------------------------------------------------------------ navigation
    def bounds(self) -> tuple[int, int]:
        if self.limit_to_range:
            return self.mark_in, self.mark_out
        return self.source.first, self.source.last

    def goto(self, n: int):
        n = int(min(max(n, self.source.first), self.source.last))
        self.cur = n
        if self.source.kind == 'file':
            try:
                frame = self.source.read(n)
            except Exception as e:
                self.pause()
                self.error.emit(f'Cannot read image {n}: {describe_error(e)}')
                return
            self.shown = n
            self.show_frame(frame)
        else:
            hit = self.source.cache.get(n)
            if hit is not None:
                self.source.cache.move_to_end(n)
                self.shown, self.stamp = n, hit[1]
                self.show_frame(hit[0])
            else:
                self._fetch()
        self.position_changed.emit()

    def _fetch(self):
        """Fetch ``self.cur`` from the camera; one request in flight, the newest wish wins."""
        if self._busy:
            return
        self._busy = True
        source, n = self.source, self.cur

        def done(r):
            self._busy = False
            num, frame, stamp = r
            source.store(num, frame, stamp)
            if self.stopped:          # the panel was closed while the image was on its way
                return
            if num == self.cur:
                self.shown, self.stamp = num, stamp
                self.show_frame(frame)
                self.position_changed.emit()
            else:
                self._fetch()

        def failed(e):
            self._busy = False
            if self.stopped:
                return
            self.cur = self.shown          # back to the image actually on screen
            self.pause()
            if isinstance(e, CameraBusy):
                self.message.emit(f'Camera busy (a download is running): image {n} not fetched')
            else:
                self.error.emit(f'Cannot fetch image {n} from the camera: {describe_error(e)}')
        self.tasks.submit(lambda task: source.fetch(n), done, failed)

    def step(self, k: int):
        lo, hi = self.bounds()
        self.goto(min(max(self.cur + k, lo), hi))

    def play(self, direction: int):
        self.direction = direction
        self.timer.setInterval(max(1, int(round(1000.0 / max(self.fps, 0.1)))))
        self.timer.start()
        self.position_changed.emit()

    def pause(self):
        self.direction = 0
        self.timer.stop()
        self.position_changed.emit()

    def set_fps(self, fps: float):
        self.fps = fps
        if self.timer.isActive():
            self.timer.setInterval(max(1, int(round(1000.0 / max(fps, 0.1)))))

    def _tick(self):
        if self.direction == 0 or (self.source.kind == 'camera' and (self._busy or self.shown != self.cur)):
            return                      # never queue camera requests faster than they come back
        lo, hi = self.bounds()
        nxt = self.cur + self.direction * self.player_step
        if nxt > hi or nxt < lo:
            if self.ping_pong:
                self.direction = -self.direction
                nxt = self.cur + self.direction * self.player_step
                nxt = min(max(nxt, lo), hi)
            elif self.repeat:
                nxt = lo if nxt > hi else hi
            else:
                self.goto(hi if nxt > hi else lo)
                self.pause()
                return
        self.goto(nxt)

    # ------------------------------------------------------------------ marks
    def set_mark_in(self, n: int | None = None):
        n = self.cur if n is None else int(n)
        self.mark_in = n
        if self.mark_out < n:
            self.mark_out = self.source.last
        self.position_changed.emit()

    def set_mark_out(self, n: int | None = None):
        n = self.cur if n is None else int(n)
        self.mark_out = n
        if self.mark_in > n:
            self.mark_in = self.source.first
        self.position_changed.emit()

    def frame_info(self) -> dict:
        stamp = self.stamp if self.source.kind == 'camera' else None
        return self.source.frame_info(self.shown, stamp)

    def point_time(self) -> tuple[int | None, float | None, bool]:
        """The image on screen and its time from the trigger: the per-image time stamp
        (``CineReader.relative_times`` / the camera's ``time``), not image number / rate."""
        fi = self.frame_info()
        return self.shown, fi['elapsed_s'], bool(fi['synthesized'])

    def show_frame(self, raw):
        super().show_frame(raw)
        self.status.time.setText(f'Current Time: {format_abs_time(self.frame_info()["abs"])}')

    def stop(self):
        self.stopped = True
        self.direction = 0
        self.timer.stop()
        self.source.close()
