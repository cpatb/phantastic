"""Live tab selectors for camera settings beyond resolution / rate / exposure (PCC p.30-49, p.115-117,
p.136-139), built from :data:`phantastic.camsettings.SETTINGS`.

* A row appears only for a variable the connected camera reported in ``get <structure>``.
* Field text is what the camera reported; editing it changes nothing on the camera. Each selector
  lists the exact ``set`` lines its **Apply** button will send; Apply sends only the changed fields and
  every field then shows the camera's read-back.
* Nothing here sends anything on connect or open: :class:`SettingsController` reads with ``get`` only,
  until a user presses an Apply button.
"""
from __future__ import annotations

import datetime as _dt
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from .. import camsettings as CS
from .workers import CameraSession, TaskManager, describe_error

PENDING_STYLE = 'QLineEdit { background: #fff6c8; }'     # a field that differs from the camera's value


class SettingsForm(QWidget):
    """One selector's fields: label + editable text (or read-only value) per variable, the pending
    command preview and an Apply button. ``apply_requested(changes)`` carries {key: typed value}."""
    apply_requested = Signal(object)   # a dict; Signal(dict) would become a QVariantMap and re-sort the keys
    edited = Signal()

    def __init__(self, keys: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = [CS.BY_KEY[k] for k in keys]
        self.current: dict = {}
        self.original: dict[str, str] = {}
        self.fields: dict[str, QLineEdit | QLabel] = {}
        self.labels: dict[str, QLabel] = {}
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for s in self.settings:
            lab = QLabel(s.label)
            lab.setToolTip(f'{s.key}: {s.note}')
            if s.editable:
                w = QLineEdit()
                w.textChanged.connect(self._changed)
            else:
                w = QLabel('-')
                w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            w.setToolTip(f'{s.key} ({s.typ.__name__}): {s.note}')
            self.fields[s.key], self.labels[s.key] = w, lab
            form.addRow(lab, w)
        self.form = form
        self.absent = QLabel('This camera reports none of these settings.')
        self.preview = QLabel('', wordWrap=True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setStyleSheet('QLabel { color: #404040; font-family: Consolas, monospace; }')
        self.apply_btn = QPushButton('Apply')
        self.apply_btn.setToolTip('Send the lines listed above (changed fields only) and read the values back.')
        self.apply_btn.clicked.connect(self._apply)
        self.readback = QLabel('', wordWrap=True)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.apply_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.absent)
        lay.addLayout(form)
        lay.addWidget(self.preview)
        lay.addLayout(row)
        lay.addWidget(self.readback)
        self.set_values({})

    # ------------------------------------------------------------------ values
    def present(self) -> list[str]:
        return [s.key for s in self.settings if s.key in self.current]

    def set_values(self, current: dict, force=()):
        """Show the camera's values. A field the user is editing keeps its text unless it is in ``force``
        (the keys just applied, which must show the read-back)."""
        self.current = {k: current[k] for k in current if k in self.fields}
        for s in self.settings:
            w, lab = self.fields[s.key], self.labels[s.key]
            here = s.key in self.current
            w.setVisible(here)
            lab.setVisible(here)
            if not here:
                self.original.pop(s.key, None)
                continue
            v = self.current[s.key]
            text = CS.display(v)
            ok = CS.type_ok(s, v)
            if not s.editable:
                w.setText(f'{text}  (raw, meaning not verified)')
            else:
                edited = s.key in self.original and w.text() != self.original[s.key]
                self.original[s.key] = text
                if s.key in force or not edited:
                    w.blockSignals(True)
                    w.setText(text)
                    w.blockSignals(False)
                w.setReadOnly(not ok)
                if not ok:
                    w.setToolTip(f'{s.key}: the camera reported {v!r}, not the {s.typ.__name__} a v2512 reports; '
                                 'shown, never written.')
        self.absent.setVisible(not self.current)
        self._changed()

    def set_wanted(self, wanted: dict):
        """Put values into the fields (e.g. from a backup) WITHOUT sending them: they show as pending."""
        for k, v in wanted.items():
            w = self.fields.get(k)
            if isinstance(w, QLineEdit) and k in self.current and not w.isReadOnly():
                w.setText(CS.display(v))

    def pending(self) -> dict:
        """{key: typed value} for edited fields that differ from the camera. Raises ValueError."""
        wanted = {}
        for k in self.present():
            w = self.fields[k]
            if isinstance(w, QLineEdit) and not w.isReadOnly() and w.text() != self.original.get(k):
                wanted[k] = CS.parse(CS.BY_KEY[k], w.text())
        return CS.changes(self.current, wanted)

    def _changed(self):
        try:
            chg = self.pending()
            err = None
        except ValueError as e:
            chg, err = {}, str(e)
        for k in self.present():
            w = self.fields[k]
            if isinstance(w, QLineEdit):
                w.setStyleSheet(PENDING_STYLE if k in chg else '')
        if err:
            self.preview.setText(f'Cannot apply: {err}')
        elif chg:
            self.preview.setText('Apply will send:\n' + '\n'.join(CS.set_lines(chg)))
        else:
            self.preview.setText('No changes to send.' if self.current else '')
        self.apply_btn.setEnabled(bool(chg) and err is None)
        self.apply_btn.setVisible(any(s.editable for s in self.settings if s.key in self.current))
        self.edited.emit()

    def _apply(self):
        try:
            chg = self.pending()
        except ValueError:
            return
        if chg:
            self.apply_requested.emit(chg)

    def show_readback(self, requested: dict, back: dict):
        parts = []
        for k, v in requested.items():
            b = back.get(k)
            same = b is not None and CS.type_ok(CS.BY_KEY[k], b) and CS.same(CS.BY_KEY[k], b, v)
            parts.append(f'{k} = {CS.display(b)}' + ('' if same else f'  (asked {CS.display(v)})'))
        self.readback.setText('Camera reads back: ' + '; '.join(parts))


class SettingsController(QObject):
    """Reads the camera's settings (``get`` only) and applies a form's changes on request."""
    message = Signal(str)
    error = Signal(str)
    updated = Signal()            # current / structs changed (after a read)

    def __init__(self, tasks: TaskManager, parent: QObject | None = None):
        super().__init__(parent)
        self.tasks = tasks
        self.session: CameraSession | None = None
        self.current: dict = {}
        self.structs: dict = {}
        self.read_at: float | None = None     # time.time() of the last read (for the camera clock)
        self.forms: list[SettingsForm] = []
        self.busy = False

    def add_form(self, form: SettingsForm):
        self.forms.append(form)
        form.apply_requested.connect(lambda chg, f=form: self.apply(f, chg))

    def set_session(self, session: CameraSession | None):
        self.session = session
        self.current, self.structs, self.read_at = {}, {}, None
        for f in self.forms:
            f.readback.setText('')
            f.set_values({})
        self.updated.emit()
        if session is not None:
            self.read()

    def _push(self, current: dict, structs: dict, force=()):
        self.current, self.structs, self.read_at = current, structs, time.time()
        for f in self.forms:
            f.set_values(current, force=force)
        self.updated.emit()

    def read(self):
        """Read every structure (``get`` only) and refresh the forms."""
        session = self.session
        if session is None:
            return None

        def job(task):
            with session.use() as cam:
                return CS.read_settings(cam)

        def done(r):
            if session is self.session:
                self._push(*r)

        def failed(e):
            self.error.emit(f'Reading camera settings failed: {describe_error(e)}')
        return self.tasks.submit(job, done, failed)

    def apply(self, form: SettingsForm, chg: dict):
        """Send ``chg`` (a form's pending changes), then re-read everything."""
        session = self.session
        if session is None or self.busy:
            return None
        self.busy = True
        form.apply_btn.setEnabled(False)

        def job(task):
            with session.use() as cam:
                back = CS.apply_changes(cam, chg)
                return back, CS.read_settings(cam)

        def done(r):
            self.busy = False
            if session is not self.session:
                return
            back, (current, structs) = r
            self._push(current, structs, force=tuple(chg))
            form.show_readback(chg, back)
            self.message.emit('Camera settings applied: ' + ', '.join(f'{k} = {CS.display(back.get(k))}' for k in chg))

        def failed(e):
            self.busy = False
            form._changed()
            self.error.emit(f'Applying camera settings failed: {describe_error(e)}')
            self.read()          # show what the camera holds now, whatever got through
        return self.tasks.submit(job, done, failed)

    def camera_clock(self) -> int | None:
        """``irig.sec`` as of the last read, advanced by the time since (None if the camera has none)."""
        sec = self.structs.get('irig', {}).get('sec')
        if not isinstance(sec, int) or self.read_at is None:
            return None
        return sec + int(time.time() - self.read_at)


def format_clock(sec: int | None) -> str:
    """Camera clock text: the unix seconds as UTC (whether the camera keeps UTC or local time is not verified)."""
    if sec is None:
        return 'not reported'
    d = _dt.datetime.fromtimestamp(sec, _dt.timezone.utc)
    return f'{d:%a %b %d %Y %H:%M:%S} UTC (irig.sec {sec})'


def pc_timezone_west() -> int:
    """Seconds west of UTC of the PC's STANDARD time (``time.timezone``). The vendor SDK sent
    ``set cam.timezone:18000`` (UTC-5) on 2026-10-07, while US Eastern was on daylight time (UTC-4),
    so it sends the standard offset; whether the camera then applies daylight saving is not known."""
    return int(time.timezone)


class SetTimeDialog(QDialog):
    """Set Time (PCC p.30-32): sets the camera clock to the PC clock after an explicit confirmation."""

    def __init__(self, camera_sec: int | None, has_timezone: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Set Camera Time')
        tz = pc_timezone_west()
        now = time.time()
        local = time.strftime('%a %b %d %Y %H:%M:%S', time.localtime(now))
        self.summary = QLabel(wordWrap=True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setText(
            f'Computer time: {local} (UTC{-tz / 3600:+g} standard time)\n'
            f'Camera clock: {format_clock(camera_sec)}\n\n'
            'Set changes the camera clock to the computer clock. EVERY RECORDING MADE FROM NOW ON is time '
            'stamped by the new clock; recordings already in the camera keep their stamps. If an IRIG-B time '
            'code is connected the camera may return to it (PCC p.32).\n\n'
            'Will send:\n  setrtc <computer time in unix seconds at the moment you press Set>')
        self.tz_check = QCheckBox(f'Also send set cam.timezone:{tz} (seconds west of UTC, standard time)')
        self.tz_check.setChecked(False)
        self.tz_check.setEnabled(has_timezone)
        if not has_timezone:
            self.tz_check.setToolTip('This camera did not report cam.timezone.')
        btns = QDialogButtonBox()
        self.set_btn = btns.addButton('Set', QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addWidget(self.tz_check)
        lay.addWidget(btns)

    @property
    def with_timezone(self) -> bool:
        return self.tz_check.isEnabled() and self.tz_check.isChecked()
