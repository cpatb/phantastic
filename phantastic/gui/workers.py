"""Background work for the GUI: a task runner on a thread pool and a lock-guarded camera session.

The UI thread never touches a socket. Every camera operation runs as a :class:`Task` on the
pool, and results come back through queued Qt signals to slots that live in the UI thread.

All access to one camera goes through :class:`CameraSession`, whose lock serialises it. This
matters beyond the control socket: :meth:`Camera.read_images` sends ``img`` and then reads the
pixel bytes off the data socket outside the camera's own command lock, so two threads reading
images at once (live view during a download) would interleave the data stream.
"""
from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..camera import Camera
from ..protocol import ProtocolError

log = logging.getLogger(__name__)

PROGRESS_MIN_INTERVAL_S = 0.05   # throttle progress signals to ~20 Hz; the last one always goes


class Cancelled(Exception):
    """Raised inside a task when the user cancelled it."""


class CameraBusy(Exception):
    """The camera is held by another operation (typically a download)."""


def describe_error(e: BaseException) -> str:
    """User-facing text for an exception; the camera's own ERR text is shown verbatim."""
    if isinstance(e, ProtocolError):
        return f'Camera error: {e}'
    if isinstance(e, Cancelled):
        return 'Cancelled'
    text = str(e) or type(e).__name__
    return f'{type(e).__name__}: {text}'


class CameraSession:
    """One connected camera plus the lock that serialises every operation on it."""

    TRANSCRIPT_KEEP = 2000   # Camera keeps an audit transcript forever; the live view would grow it without bound

    def __init__(self, cam: Camera, info: dict, formats: list[str], simulated: bool = False):
        self.cam = cam
        self.info = info
        self.formats = formats
        self.simulated = simulated
        self.lock = threading.Lock()
        self.cine_info_cache: dict[tuple[int, str], dict] = {}   # touched only while holding lock
        self.extra: dict = {}   # other values read at connect (e.g. cam.membpp)

    @property
    def address(self) -> str:
        return f'{self.cam.ip}:{self.cam.port}'

    @contextmanager
    def use(self, timeout: float = 3.0) -> Iterator[Camera]:
        if not self.lock.acquire(timeout=timeout):
            raise CameraBusy('camera busy: another operation (for example a download) is running')
        try:
            yield self.cam
        finally:
            self.lock.release()

    @contextmanager
    def try_use(self) -> Iterator[Camera | None]:
        """Yield the camera if it is free right now, else None (used by polling: never queue up)."""
        if not self.lock.acquire(blocking=False):
            yield None
            return
        try:
            yield self.cam
        finally:
            self.lock.release()

    def trim_transcript(self):
        """Call while holding the lock."""
        t = self.cam.transcript
        if len(t) > self.TRANSCRIPT_KEEP:
            del t[: len(t) - self.TRANSCRIPT_KEEP]

    def close(self):
        self.cam.close()


class _Bus(QObject):
    done = Signal(int, object)
    failed = Signal(int, object)
    progress = Signal(int, int, int)


class Task(QRunnable):
    """A unit of background work. ``fn(task)`` may call :meth:`progress` and :meth:`check_cancelled`."""

    def __init__(self, tid: int, fn: Callable[['Task'], Any], bus: _Bus):
        super().__init__()
        self.setAutoDelete(False)
        self.tid, self.fn, self._bus = tid, fn, bus
        self.cancelled = threading.Event()
        self._last_emit = 0.0

    def cancel(self):
        self.cancelled.set()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def progress(self, done: int, total: int):
        """Progress callback for library functions; raising here is how cancellation reaches them."""
        self.check_cancelled()
        now = time.monotonic()
        if done >= total or now - self._last_emit >= PROGRESS_MIN_INTERVAL_S:
            self._last_emit = now
            self._bus.progress.emit(self.tid, done, total)

    def run(self):
        try:
            result = self.fn(self)
        except BaseException as e:   # every failure is delivered to the UI, never swallowed
            self._bus.failed.emit(self.tid, e)
        else:
            self._bus.done.emit(self.tid, result)


class TaskManager(QObject):
    """Runs tasks on a private thread pool; callbacks run in the UI thread."""

    def __init__(self, on_error: Callable[[BaseException], None], parent: QObject | None = None, threads: int = 4):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(threads)
        self._bus = _Bus(self)
        self._bus.done.connect(self._done)
        self._bus.failed.connect(self._failed)
        self._bus.progress.connect(self._progress)
        self._ids = itertools.count(1)
        self._active: dict[int, tuple[Task, Callable | None, Callable | None, Callable | None]] = {}
        self.default_error = on_error

    def submit(self, fn: Callable[[Task], Any], on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None,
               on_progress: Callable[[int, int], None] | None = None) -> Task:
        tid = next(self._ids)
        task = Task(tid, fn, self._bus)
        self._active[tid] = (task, on_done, on_error, on_progress)
        self.pool.start(task)
        return task

    def idle(self) -> bool:
        return not self._active

    def cancel_all(self):
        for task, *_ in self._active.values():
            task.cancel()

    def _call(self, cb: Callable, *args):
        try:
            cb(*args)
        except Exception as e:   # a bug in a callback must surface, not vanish into Qt
            log.exception('GUI callback failed')
            self.default_error(e)

    @Slot(int, object)
    def _done(self, tid: int, result):
        entry = self._active.pop(tid, None)
        if entry and entry[1]:
            self._call(entry[1], result)

    @Slot(int, object)
    def _failed(self, tid: int, err):
        entry = self._active.pop(tid, None)
        handler = (entry[2] if entry else None) or self.default_error
        self._call(handler, err)

    @Slot(int, int, int)
    def _progress(self, tid: int, done: int, total: int):
        entry = self._active.get(tid)
        if entry and entry[3]:
            self._call(entry[3], done, total)


# ----------------------------------------------------------------------------- file jobs

def part_path(path: Path) -> Path:
    """Work file next to the destination; renamed onto it only when the job succeeds."""
    return path.with_name(path.name + '.part')


def _discard(paths: list[Path]) -> list[str]:
    gone = []
    for p in paths:
        try:
            if p.exists():
                p.unlink()
                gone.append(str(p))
        except OSError as e:
            log.warning('could not delete partial file %s: %s', p, e)
    return gone


def download_cine(session: CameraSession, cine: int, path, first: int, last: int, step: int, align: str,
                  fmt: str, task: Task, lock_timeout: float = 3.0, as_12bit: bool = False) -> dict:
    """Download a stored camera cine to ``path`` (the work behind the Save cine dialog).

    Writes to ``path + '.part'`` and renames on success, so a cancelled or failed download never
    leaves a truncated cine at ``path`` and never destroys a file that was there before.
    Cancellation is checked after every frame; the frame batch already on the wire is read in
    full first (``Camera.frames`` reads a whole ``img`` run before yielding), so the data stream
    stays in step for the next operation.
    """
    path = Path(path)
    part = part_path(path)
    task.check_cancelled()
    try:
        with session.use(timeout=lock_timeout) as cam:
            res = cam.download(cine, part, first=first, last=last, step=step, fmt=fmt, align=align,
                               progress=task.progress, as_12bit=as_12bit)
        os.replace(part, path)
    except BaseException as e:
        e.partial_deleted = _discard([part])   # type: ignore[attr-defined]
        raise
    res['path'] = str(path)
    return res


def export_file(kind: str, src, dst, first: int, last: int, step: int, align: str, task: Task,
                pcc_table: str | None = None) -> dict:
    """File > Export: 'cine' -> decimate_cine (lossless), 'tiff' -> export_tiff (raw values, or
    PCC's 8-bit export reproduced from a measured table when ``pcc_table`` is given)."""
    from ..decimate import decimate_cine, export_tiff
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError('refusing to write over the source file; choose another output path')
    part = part_path(dst)
    sidecar_part, sidecar = Path(str(part) + '.json'), Path(str(dst) + '.json')
    task.check_cancelled()
    try:
        if kind == 'cine':
            res = decimate_cine(src, part, step, align=align, first=first, last=last, progress=task.progress)
            res['dst'] = str(dst)
        elif kind == 'tiff':
            res = export_tiff(src, part, first=first, last=last, step=step, align=align, progress=task.progress,
                              pcc_table=pcc_table)
            res['pcc_table'] = pcc_table
            os.replace(sidecar_part, sidecar)
            res['dst'] = str(dst)
            res['sidecar'] = str(sidecar)
        else:
            raise ValueError(kind)
        os.replace(part, dst)
    except BaseException as e:
        e.partial_deleted = _discard([part, sidecar_part])   # type: ignore[attr-defined]
        raise
    return res
