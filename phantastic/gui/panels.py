"""MDI image panels: the live Preview panel of a camera and the Playback panel of a cine.

A Playback panel plays either a saved file (``FileSource``, read through ``CineReader``) or a
cine still in camera memory (``CameraCineSource``), whose frames are fetched on demand in the
background through the ``CameraSession`` lock. Display range, flips and overlays change the
screen only; ``panel.frame`` always holds the values as read (file) or as sent (camera).
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
from .imageview import ImageView, auto_range, to_display8
from .widgets import PanelStatusLine
from .workers import CameraBusy, CameraSession, TaskManager, describe_error

LIVE_INTERVAL_MS = 40          # request ceiling ~25 live frames/s; the camera/link decides the real rate
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
    """Image view + status line, raw frame, display range (screen only) and pixel readout."""
    readout_changed = Signal()
    frame_changed = Signal()
    display_changed = Signal()
    error = Signal(str)
    message = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view = ImageView()
        self.status = PanelStatusLine()
        self.frame: np.ndarray | None = None
        self.raw_max = 255
        self.lo, self.hi = 0, 255
        self._auto_next = True
        self.last_readout: tuple[int, int, object] | None = None
        self._stamps: collections.deque[float] = collections.deque(maxlen=200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.status)
        self.view.hovered.connect(self._readout)

    def title(self) -> str:
        return ''

    def show_frame(self, raw: np.ndarray):
        self.frame = raw
        if self._auto_next:
            self._auto_next = False
            self.lo, self.hi = auto_range(raw)    # initial display range from the first frame (screen only)
            self.display_changed.emit()
        self.render()
        now = time.monotonic()
        self._stamps.append(now)
        if self.last_readout is not None:
            self._readout(*self.last_readout[:2])
        self.frame_changed.emit()

    def render(self):
        if self.frame is not None:
            self.view.set_array(to_display8(self.frame, self.lo, self.hi))

    def set_display_range(self, lo: int, hi: int):
        self.lo, self.hi = int(lo), int(max(hi, lo + 1))
        self.render()
        self.display_changed.emit()

    def auto(self):
        """Fit the display range to THIS frame, 0.35 % saturated. Display only; data unchanged."""
        if self.frame is not None:
            self.set_display_range(*auto_range(self.frame))

    def full(self):
        self.set_display_range(0, self.raw_max)

    def refresh_rate(self) -> float | None:
        now = time.monotonic()
        recent = [t for t in self._stamps if now - t <= 1.0]
        if len(recent) < 2:
            return None
        return (len(recent) - 1) / (recent[-1] - recent[0]) if recent[-1] > recent[0] else None

    def _readout(self, x: int, y: int):
        f = self.frame
        if f is None or x < 0 or y < 0 or y >= f.shape[0] or x >= f.shape[1]:
            self.last_readout = None
        else:
            v = f[y, x]
            self.last_readout = (x, y, tuple(int(c) for c in v) if np.ndim(v) else int(v))
        self.readout_changed.emit()


class PreviewPanel(ImagePanel):
    """Live preview of a camera: polls 8-bit live frames in the background while visible."""

    def __init__(self, tasks: TaskManager, session: CameraSession, parent: QWidget | None = None):
        super().__init__(parent)
        self.tasks, self.session = tasks, session
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
        session = self.session
        self._busy = True

        def job(task):
            with session.try_use() as cam:
                return None if cam is None else cam.live_image('8')

        def done(r):
            self._busy = False
            if r is None or session is not self.session:
                return
            frame, _ = r
            self.frames_received += 1
            self.show_frame(frame)

        def failed(e):
            self._busy = False
            if session is not self.session:
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

    def show_frame(self, raw):
        super().show_frame(raw)
        self.status.time.setText(f'Current Time: {format_abs_time(self.frame_info()["abs"])}')

    def stop(self):
        self.stopped = True
        self.direction = 0
        self.timer.stop()
        self.source.close()
