"""Dialogs: connect by IP, save a camera cine, export a file, about."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import protocol as P
from ..crop import check_crop
from ..decimate import last_for_count, renumbering, select_numbers
from ..naming import DEFAULT_CINE_TEMPLATE, DEFAULT_SEQUENCE_PATTERN, cine_fields, expand_name, tokens_in, unique_path
from ..pcc_render import find_table
from .workers import (CameraSession, Cancelled, TaskManager, describe_error, download_all, download_cine,
                      export_camera, export_file)

ABOUT_TEXT = """<h3>Phantastic</h3>
<p>An open-source program to control high-speed cameras that speak the PH16 Ethernet protocol,
and to read, decimate and export <tt>.cine</tt> files without altering the recorded values.</p>
<p><b>Phantastic is an independent open-source project. It is not affiliated with, endorsed by,
or supported by Vision Research or AMETEK.</b> &ldquo;Phantom&rdquo; is a trademark of
Vision Research / AMETEK; it is used here only to describe compatibility.</p>
<p>The protocol was implemented from Vision Research's published PH16 protocol document and
public camera transcripts. Pixel values are stored and shown exactly as the camera sent them;
display range settings change the screen only.</p>"""

FORMAT_HELP = {
    'P16': 'P16: 16-bit, corrected by the camera (fixed-pattern noise and pixel response, FPN/PRNU). '
           'The usual choice.',
    'P16R': 'P16R: 16-bit, UNCORRECTED raw sensor values (no FPN/PRNU correction).',
    'P10': 'P10: 10-bit companded codes, corrected by the camera. About 5/8 the size of P16, but not '
           'linear; readers map the codes to 12-bit values through the camera\'s LUT.',
    'P12L': 'P12L: 12-bit linear packed, corrected by the camera. Not yet verified on a real camera; '
            'prefer P16.',
    '8': '8: 8-bit, corrected by the camera. Smallest, but drops the low bits of a 12-bit sensor.',
    '8R': '8R: 8-bit, uncorrected.',
}
DOWNLOAD_FORMATS = ('P16', 'P16R', 'P10', 'P12L', '8', '8R')   # what Camera.download can store

ALIGN_CHOICES = (('Multiples of N relative to trigger (PCC-compatible)', 'trigger'),
                 ('From first image', 'first'))
RANGE_OPTIONS = (('[Mark In, Mark Out]', 'marks'), ('Full cine', 'full'), ('User Defined', 'user'))   # PCC p.64


def mapping_text(step: int, offset: int, first_out: int, count: int, source: str = 'camera') -> str:
    """The image-number mapping written into the file, from a result dict's fields."""
    if step == 1:
        return f'Image k = {source} image k (numbers unchanged; file images {first_out} .. {first_out + count - 1})'
    return (f'Image k = {source} image k*{step}+{offset} '
            f'(file images {first_out} .. {first_out + count - 1})')


class RangeWidget(QWidget):
    """PCC's Range Option ([Mark In, Mark Out] / Full cine / User Defined), first/last image number
    (or a frame count), 'Decimate by' N and alignment, with a live preview of what is kept."""
    changed = Signal()

    def __init__(self, lo: int, hi: int, marks: tuple[int, int] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.lo, self.hi = lo, hi
        mi, mo = marks if marks is not None else (lo, hi)
        self.marks = (min(max(mi, lo), hi), min(max(mo, lo), hi))
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.first_spin = QSpinBox(minimum=lo, maximum=hi, value=lo)
        self.last_spin = QSpinBox(minimum=lo, maximum=hi, value=hi)
        self.step_spin = QSpinBox(minimum=1, maximum=max(1, hi - lo + 1), value=1)
        self.align_combo = QComboBox()
        for text, key in ALIGN_CHOICES:
            self.align_combo.addItem(text, key)
        # End of the range: a last image number, or a number of frames from the first image.
        self.end_combo = QComboBox()
        self.end_combo.addItem('Last image', 'last')
        self.end_combo.addItem('Number of frames', 'count')
        self.count_spin = QSpinBox(minimum=1, maximum=max(1, hi - lo + 1), value=min(100, hi - lo + 1))
        self.count_spin.setToolTip('Frames to save, counting the first kept image as frame 1. '
                                   'With step N they are N camera frames apart.')
        self.summary = QLabel(wordWrap=True)
        self.option_combo = QComboBox()
        for text, key in RANGE_OPTIONS:
            self.option_combo.addItem(text, key)
        self.option_combo.setToolTip(f'[Mark In, Mark Out] = images {self.marks[0]} .. {self.marks[1]} '
                                     '(set with [ and ] in the Play tab). User Defined enables the fields below.')
        self.step_spin.setToolTip('Keep every Nth image (PCC "Decimate by"). 1 keeps every image.')
        form.addRow('Range Option', self.option_combo)
        form.addRow('First image', self.first_spin)
        form.addRow('End range by', self.end_combo)
        form.addRow('Last image', self.last_spin)
        form.addRow('Frames to save', self.count_spin)
        form.addRow('Decimate by', self.step_spin)
        form.addRow('Alignment', self.align_combo)
        form.addRow(self.summary)
        self._form = form
        for w in (self.first_spin, self.last_spin, self.step_spin, self.count_spin):
            w.valueChanged.connect(self._update)
        self.align_combo.currentIndexChanged.connect(self._update)
        self.end_combo.currentIndexChanged.connect(self._mode_changed)
        self.option_combo.currentIndexChanged.connect(self._option_changed)
        self._option_changed()

    def option(self) -> str:
        return self.option_combo.currentData()

    def set_option(self, key: str):
        self.option_combo.setCurrentIndex(self.option_combo.findData(key))

    def _option_changed(self):
        key = self.option()
        user = key == 'user'
        for w in (self.first_spin, self.last_spin, self.end_combo, self.count_spin):
            w.setEnabled(user)
        if not user:
            first, last = self.marks if key == 'marks' else (self.lo, self.hi)
            for w in (self.first_spin, self.last_spin, self.end_combo):
                w.blockSignals(True)
            self.end_combo.setCurrentIndex(self.end_combo.findData('last'))
            self.first_spin.setValue(first)
            self.last_spin.setValue(last)
            for w in (self.first_spin, self.last_spin, self.end_combo):
                w.blockSignals(False)
        self._mode_changed()

    def _mode_changed(self):
        by_count = self.end_combo.currentData() == 'count'
        self._form.setRowVisible(self.last_spin, not by_count)
        self._form.setRowVisible(self.count_spin, by_count)
        self._update()

    def _last(self) -> int:
        """The effective last image: typed, or derived from first + frame count (clipped to the file)."""
        if self.end_combo.currentData() != 'count':
            return self.last_spin.value()
        last = last_for_count(self.first_spin.value(), self.count_spin.value(), self.step_spin.value(),
                              self.align_combo.currentData())
        return min(last, self.hi)

    def values(self) -> tuple[int, int, int, str]:
        return (self.first_spin.value(), self._last(), self.step_spin.value(),
                self.align_combo.currentData())

    def set_values(self, first: int | None = None, last: int | None = None, step: int | None = None,
                   align: str | None = None, count: int | None = None):
        if first is not None or last is not None or count is not None:
            self.set_option('user')
        if count is not None:
            self.end_combo.setCurrentIndex(self.end_combo.findData('count'))
            self.count_spin.setValue(count)
        elif last is not None:
            self.end_combo.setCurrentIndex(self.end_combo.findData('last'))
        if first is not None:
            self.first_spin.setValue(first)
        if last is not None:
            self.last_spin.setValue(last)
        if step is not None:
            self.step_spin.setValue(step)
        if align is not None:
            self.align_combo.setCurrentIndex(self.align_combo.findData(align))

    def numbers(self):
        first, last, step, align = self.values()
        return select_numbers(first, last, step, align)

    def _update(self):
        nums = self.numbers()
        step = self.step_spin.value()
        if len(nums) == 0:
            self.summary.setText('<b>No images selected.</b>')
        else:
            kept = ', '.join(str(int(n)) for n in nums[:3]) + (f', ... {int(nums[-1])}' if len(nums) > 3 else '')
            text = f'{len(nums)} images kept: {kept}'
            if self.end_combo.currentData() == 'count' and len(nums) < self.count_spin.value():
                text += (f'<br><b>Only {len(nums)} of the {self.count_spin.value()} requested frames exist '
                         f'(the recording ends at image {self.hi}).</b>')
            if step > 1:
                out_first, off = renumbering(nums, step)
                text += f'<br>Saved as images {out_first} .. {out_first + len(nums) - 1} (image k = source image k*{step}+{off})'
            self.summary.setText(text)
        self.changed.emit()


def _path_row(edit: QLineEdit, browse: QPushButton) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    h.addWidget(browse)
    return w


class _JobDialog(QDialog):
    """Common layout and behaviour: inputs, progress bar, summary, Start / Cancel / Close."""
    finished_job = Signal(object)   # result dict, or the exception
    progressed = Signal(int, int)   # (done, total); (0, 0) busy; (-1, -1) over. Mirrored in the main status bar.

    def __init__(self, tasks: TaskManager, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.tasks = tasks
        self.task = None
        self._confirmed_overwrite = ''
        self.resolved_path = ''
        self.result_info: dict | None = None
        self.error: BaseException | None = None
        self.inputs = QWidget()
        self.inputs_layout = QVBoxLayout(self.inputs)
        self.inputs_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.browse_btn = QPushButton('Browse...')
        self.browse_btn.clicked.connect(self._browse)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.summary = QPlainTextEdit(readOnly=True)
        self.summary.setMinimumHeight(110)
        self.summary.setMaximumHeight(170)       # the inputs above get the rest of the height
        self.buttons = QDialogButtonBox()
        self.start_btn = self.buttons.addButton('Start', QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_btn = self.buttons.addButton('Cancel', QDialogButtonBox.ButtonRole.RejectRole)
        self.close_btn = self.buttons.addButton('Close', QDialogButtonBox.ButtonRole.DestructiveRole)
        self.cancel_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn.clicked.connect(self.cancel)
        self.close_btn.clicked.connect(self.close)
        lay = QVBoxLayout(self)
        scroll = QScrollArea()                     # the export options can be taller than a laptop screen
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.inputs)
        lay.addWidget(scroll, 1)
        lay.addWidget(self.progress)
        lay.addWidget(self.summary)
        lay.addWidget(self.buttons)
        self.resize(560, 560)
        self._sized = False

    def showEvent(self, event):
        if not self._sized:          # first show: fit the inputs, within 90 % of the screen
            self._sized = True
            screen = self.screen().availableGeometry() if self.screen() else None
            h = self.inputs.sizeHint().height() + 260
            w = max(self.width(), self.inputs.sizeHint().width() + 40)
            if screen is not None:
                h, w = min(h, int(screen.height() * 0.9)), min(w, int(screen.width() * 0.9))
            self.resize(w, max(h, 400))
        super().showEvent(event)

    # subclasses provide these
    def _file_filter(self) -> str:
        return 'All files (*)'

    def _job(self):
        raise NotImplementedError

    def _summary(self, res: dict) -> str:
        raise NotImplementedError

    def _browse(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Output file', self.path_edit.text(), self._file_filter())
        if path:
            self.path_edit.setText(path)
            self._confirmed_overwrite = path   # the file dialog already asked about replacing it

    def _validate(self) -> str | None:
        """Return an error message, or None."""
        p = self.path_edit.text().strip()
        if not p:
            return 'Choose an output file.'
        if not Path(p).parent.exists():
            return f'Folder does not exist: {Path(p).parent}'
        return None

    def running(self) -> bool:
        return self.task is not None

    def output_path(self) -> str:
        """The path the job will write (subclasses expand file-name tokens here)."""
        return self.path_edit.text().strip()

    def _overwrite_ok(self, path: str) -> bool:
        if Path(path).exists() and path != self._confirmed_overwrite:
            return QMessageBox.question(self, 'Overwrite?', f'{path} exists. Replace it?') \
                == QMessageBox.StandardButton.Yes
        return True

    def start(self):
        if self.running():
            return
        msg = self._validate()
        if msg:
            self.summary.setPlainText(msg)
            return
        try:
            path = self.output_path()
        except ValueError as e:
            self.summary.setPlainText(str(e))
            return
        if not self._overwrite_ok(path):
            return
        self.resolved_path = path
        self.result_info = self.error = None
        self.inputs.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setRange(0, 0)   # busy until the first progress report
        self.progressed.emit(0, 0)
        self.summary.setPlainText('Working...')
        self.task = self.tasks.submit(self._job(), self._on_done, self._on_error, self._on_progress)

    def cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.summary.setPlainText('Cancelling (the frames already in transit are finished first)...')

    def _on_progress(self, done: int, total: int):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progressed.emit(done, total)

    def _finish(self):
        self.task = None
        self.progressed.emit(-1, -1)
        self.inputs.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _on_done(self, res: dict):
        self._finish()
        self.result_info = res
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.summary.setPlainText(self._summary(res))
        self.finished_job.emit(res)

    def _on_error(self, e: BaseException):
        self._finish()
        self.error = e
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        deleted = getattr(e, 'partial_deleted', [])
        tail = (f'\nPartial file deleted: {", ".join(deleted)}' if deleted else '\nNo partial file was left behind.')
        head = 'Cancelled.' if isinstance(e, Cancelled) else f'Failed. {describe_error(e)}'
        written = getattr(e, 'written_without_sidecar', [])
        what = (f'\nWRITTEN, but its .json sidecar (image numbers and times) could not be saved: {", ".join(written)}'
                if written else f'\nNothing was written to {self.resolved_path or self.output_path()}.')
        self.summary.setPlainText(head + tail + what)
        self.finished_job.emit(e)

    def closeEvent(self, event):
        if self.running():
            if QMessageBox.question(self, 'Cancel?', 'A job is running. Cancel it?') != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancel()
        super().closeEvent(event)


class CropWidget(QGroupBox):
    """Crop on save/export: to the image panel's rectangle when the panel offers one
    (``panel.crop_rect() -> (x, y, w, h) | None``, stored-array coordinates), or to typed X/Y/W/H.
    0-based, rows counted from the top, as the file reader returns them. Never clamped: a rectangle
    that does not fit is refused at Start."""

    def __init__(self, width: int, height: int, panel=None, parent: QWidget | None = None):
        super().__init__('Crop', parent)
        self.img_w, self.img_h, self.panel = int(width), int(height), panel
        self.rect_check = QCheckBox('Crop to rectangle')
        self.manual_check = QCheckBox('Crop to X / Y / W / H:')
        self.x_spin = QSpinBox(minimum=0, maximum=max(0, self.img_w - 1), value=0)
        self.y_spin = QSpinBox(minimum=0, maximum=max(0, self.img_h - 1), value=0)
        self.w_spin = QSpinBox(minimum=1, maximum=max(1, self.img_w), value=self.img_w)
        self.h_spin = QSpinBox(minimum=1, maximum=max(1, self.img_h), value=self.img_h)
        row = QHBoxLayout()
        row.addWidget(self.manual_check)
        for lab, sp in (('X', self.x_spin), ('Y', self.y_spin), ('W', self.w_spin), ('H', self.h_spin)):
            row.addWidget(QLabel(lab))
            row.addWidget(sp)
        row.addStretch(1)
        note = QLabel(f'Image {self.img_w} x {self.img_h}. X, Y = column and row of the top-left pixel (0-based, '
                      'rows from the top). Pixel values are not changed; the crop is recorded in the output.')
        note.setWordWrap(True)
        v = QVBoxLayout(self)
        v.addWidget(self.rect_check)
        v.addLayout(row)
        v.addWidget(note)
        self.rect_check.toggled.connect(lambda on: on and self.manual_check.setChecked(False))
        self.manual_check.toggled.connect(self._manual_toggled)
        self._manual_toggled(False)
        self.refresh()

    def _manual_toggled(self, on: bool):
        if on:
            self.rect_check.setChecked(False)
        for sp in (self.x_spin, self.y_spin, self.w_spin, self.h_spin):
            sp.setEnabled(on)

    def panel_rect(self):
        fn = getattr(self.panel, 'crop_rect', None)
        if not callable(fn):
            return None
        try:
            r = fn()
        except Exception:      # a panel without a usable rectangle simply offers none
            return None
        return None if r is None else tuple(int(v) for v in r)

    def refresh(self):
        r = self.panel_rect()
        self.rect_check.setEnabled(r is not None)
        self.rect_check.setText(f'Crop to rectangle: x {r[0]}, y {r[1]}, {r[2]} x {r[3]}' if r else
                                'Crop to rectangle (none drawn on the image)')
        if r is None:
            self.rect_check.setChecked(False)

    def set_crop(self, crop):
        """Typed crop (tests and scripts); None = no crop."""
        self.manual_check.setChecked(crop is not None)
        if crop is not None:
            for sp, v in zip((self.x_spin, self.y_spin, self.w_spin, self.h_spin), crop):
                sp.setValue(int(v))

    def crop(self):
        if self.rect_check.isChecked():
            r = self.panel_rect()
            if r is None:
                raise ValueError('the image panel no longer has a crop rectangle')
            return r
        if self.manual_check.isChecked():
            return (self.x_spin.value(), self.y_spin.value(), self.w_spin.value(), self.h_spin.value())
        return None

    def problem(self, packing: str | None = None) -> str | None:
        try:
            check_crop(self.crop(), self.img_w, self.img_h, packing)
        except ValueError as e:
            return f'Crop: {e}'
        return None


def _format_group(session: CameraSession) -> tuple[QGroupBox, QComboBox, QLabel, QCheckBox]:
    """Wire format combo, its help line and the PCC-compatible 12-bit option (Save Cine, Save All)."""
    combo = QComboBox()
    offered = [f for f in DOWNLOAD_FORMATS if f in session.formats] or ['P16']
    for f in offered:
        combo.addItem(f, f)
        combo.setItemData(combo.count() - 1, FORMAT_HELP[f], Qt.ItemDataRole.ToolTipRole)
    help_ = QLabel(FORMAT_HELP[combo.currentData()], wordWrap=True)
    as12 = QCheckBox('PCC-compatible 12-bit (untick for full 16-bit: ImageJ/Phantastic only)')
    as12.setToolTip('P16 arrives full-scale (12-bit value x 16); PCC shows such a file almost white. '
                    'Ticked, the file holds value >> 4 in PCC\'s own layout. P16R loses nothing; P16 '
                    'loses only the camera\'s correction fraction below one 12-bit count.')
    as12.setEnabled(combo.currentData() in ('P16', 'P16R'))
    as12.setChecked(as12.isEnabled())

    def changed():
        fmt = combo.currentData()
        help_.setText(FORMAT_HELP[fmt])
        ok = fmt in ('P16', 'P16R')
        as12.setEnabled(ok)
        as12.setChecked(ok)
    combo.currentIndexChanged.connect(changed)
    g = QGroupBox('Wire format')
    v = QVBoxLayout(g)
    v.addWidget(combo)
    v.addWidget(help_)
    v.addWidget(as12)
    return g, combo, help_, as12


TOKEN_HELP = ('File-name tokens (PCC style): {cinenr} {serial} {camname} {date} {time} {count}; a digit sets a '
              'minimum width, e.g. {cinenr3}. A name that already exists gets _1, _2, ...')


def _cine_fields(session: CameraSession, cine: int, info: dict, count: int = 1) -> dict:
    trig = info.get('trigtime') if isinstance(info.get('trigtime'), dict) else {}
    return cine_fields(cine, session.info.get('serial'), session.info.get('name'),
                       int(trig.get('secs', 0)) or None, count)


class SaveCineDialog(_JobDialog):
    """PCC's Save Cine: download a stored camera cine to a .cine file.

    ``marks`` = (Mark In, Mark Out) from the Play tab; the Range Option defaults to them.
    """

    def __init__(self, tasks: TaskManager, session: CameraSession, cine: int, info: dict,
                 default_path: str = '', marks: tuple[int, int] | None = None, parent: QWidget | None = None,
                 panel=None):
        super().__init__(tasks, 'Save Cine', parent)
        self.session, self.cine, self.cine_info = session, cine, info
        lo, hi = int(info['firstfr']), int(info['lastfr'])
        res = info.get('res')
        head = QLabel(f'Camera {session.info.get("serial", "")} > Cine {cine}: images {lo} .. {hi} '
                      f'({hi - lo + 1} frames), {res}, {info.get("rate")} fps, trigger = image 0')
        head.setWordWrap(True)
        self.range = RangeWidget(lo, hi, marks)
        g2, self.fmt_combo, self.fmt_help, self.as12_check = _format_group(session)
        self.crop_widget = CropWidget(res.width, res.height, panel) if res is not None else None
        self.path_edit.setText(default_path)
        self.path_edit.setToolTip(TOKEN_HELP)
        self.actual_label = QLabel(wordWrap=True)
        self.path_edit.textChanged.connect(self._update_actual)
        g1 = QGroupBox('Range')
        QVBoxLayout(g1).addWidget(self.range)
        form = QFormLayout()
        form.addRow('Output file', _path_row(self.path_edit, self.browse_btn))
        form.addRow('Actual name', self.actual_label)
        self.inputs_layout.addWidget(head)
        self.inputs_layout.addWidget(g1)
        self.inputs_layout.addWidget(g2)
        if self.crop_widget is not None:
            self.inputs_layout.addWidget(self.crop_widget)
        self.inputs_layout.addLayout(form)
        self._update_actual()

    def _templated(self) -> bool:
        return '{' in self.path_edit.text()

    def output_path(self) -> str:
        """The typed path, or with file-name tokens (PCC p.66-73) expanded and made unique (_1, _2...)."""
        text = self.path_edit.text().strip()
        if not self._templated():
            return text
        name = expand_name(text, **_cine_fields(self.session, self.cine, self.cine_info))
        if not name.lower().endswith('.cine'):
            name += '.cine'
        return str(unique_path(name))

    def _overwrite_ok(self, path: str) -> bool:
        return True if self._templated() else super()._overwrite_ok(path)

    def _update_actual(self):
        try:
            self.actual_label.setText(self.output_path() if self._templated() else '(as typed)')
        except ValueError as e:
            self.actual_label.setText(f'<b>{e}</b>')

    def set_format(self, fmt: str):
        self.fmt_combo.setCurrentIndex(self.fmt_combo.findData(fmt))

    def _file_filter(self):
        return 'Cine files (*.cine)'

    def _validate(self):
        if len(self.range.numbers()) == 0:
            return 'No images selected.'
        if self.crop_widget is not None:
            packing = {'P10': 'packed10', 'P12L': 'packed12L'}.get(self.fmt_combo.currentData())
            msg = self.crop_widget.problem(packing)
            if msg:
                return msg
        try:
            path = self.output_path()
        except ValueError as e:
            return f'File name: {e}'
        if not path:
            return 'Choose an output file.'
        if not Path(path).parent.exists():
            return f'Folder does not exist: {Path(path).parent}'
        return None

    def _job(self):
        session, cine = self.session, self.cine
        first, last, step, align = self.range.values()
        fmt = self.fmt_combo.currentData()
        path = self.resolved_path
        as12 = self.as12_check.isChecked()   # unchecked whenever the format does not allow it
        crop = self.crop_widget.crop() if self.crop_widget is not None else None
        return lambda task: download_cine(session, cine, path, first, last, step, align, fmt, task, as_12bit=as12,
                                          crop=crop)

    def _summary(self, r: dict) -> str:
        corrected = 'camera-corrected FPN/PRNU' if P.IMAGE_FORMATS[r['fmt']][2] else 'uncorrected'
        return '\n'.join([
            f'Saved {r["count"]} images to {r["path"]}',
            f'Camera cine {r["cine"]}: camera images {r["first"]} .. {r["last"]}, step {r["step"]}, '
            f'alignment {r["align"]}',
            f'Wire format: {r["fmt"]} ({corrected})'
            + (f'; stored as 12-bit, PCC layout ({_dropped_text(r.get("dropped_low_bits"))})' if r.get('as_12bit') else
               ('; stored as received (full-scale 16-bit)' if r['fmt'] in ('P16', 'P16R') else '')),
            f'Image-number mapping: {mapping_text(r["step"], r["offset"], r["first_out"], r["count"])}',
            f'Per-image time stamps: {"yes" if r["times"] else "NO (camera did not supply them)"}',
        ] + ([f'Cropped: x {r["crop"][0]}, y {r["crop"][1]}, {r["crop"][2]} x {r["crop"][3]} (recorded in the '
              'file description)'] if r.get('crop') else []))


class SaveAllDialog(_JobDialog):
    """Save All RAM Cines to File (PCC p.62): every stored camera cine, full range, into one folder."""

    def __init__(self, tasks: TaskManager, session: CameraSession, infos: dict[int, dict], default_dir: str = '',
                 parent: QWidget | None = None):
        super().__init__(tasks, 'Save All RAM Cines', parent)
        self.session = session
        self.infos = {c: i for c, i in sorted(infos.items()) if i.get('firstfr') is not None}
        head = QLabel(f'Camera {session.info.get("serial", "")}: {len(self.infos)} stored cine(s): '
                      + ', '.join(str(c) for c in self.infos) + '. Each is saved in full.', wordWrap=True)
        self.path_edit.setText(default_dir or str(Path.home()))
        self.template_edit = QLineEdit(DEFAULT_CINE_TEMPLATE)
        self.template_edit.setToolTip(TOKEN_HELP + ' Without {cinenr}, _Cine<N> is appended (as PCC does).')
        self.preview = QLabel(wordWrap=True)
        self.template_edit.textChanged.connect(self._update_preview)
        g, self.fmt_combo, self.fmt_help, self.as12_check = _format_group(session)
        form = QFormLayout()
        form.addRow('Folder', _path_row(self.path_edit, self.browse_btn))
        form.addRow('File name', self.template_edit)
        form.addRow('Names', self.preview)
        self.inputs_layout.addWidget(head)
        self.inputs_layout.addWidget(g)
        self.inputs_layout.addLayout(form)
        self._update_preview()

    def _names(self) -> list[str]:
        t = self.template_edit.text().strip() or DEFAULT_CINE_TEMPLATE
        if 'cinenr' not in tokens_in(t):
            t += '_Cine{cinenr}'
        return [expand_name(t, **_cine_fields(self.session, c, i, k)) + '.cine'
                for k, (c, i) in enumerate(self.infos.items(), 1)]

    def _update_preview(self):
        try:
            self.preview.setText(', '.join(self._names()) + ' (existing names get _1, _2, ...)')
        except ValueError as e:
            self.preview.setText(f'<b>{e}</b>')

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Folder', self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def _validate(self):
        if not self.infos:
            return 'No stored cine in camera RAM.'
        p = self.path_edit.text().strip()
        if not p:
            return 'Choose a folder.'
        if not Path(p).exists() and not Path(p).parent.exists():
            return f'Folder does not exist: {Path(p).parent}'
        try:
            self._names()
        except ValueError as e:
            return f'File name: {e}'
        return None

    def _overwrite_ok(self, path: str) -> bool:
        return True            # never overwrites: existing names get a suffix

    def _job(self):
        session, folder = self.session, self.resolved_path
        template = self.template_edit.text().strip() or DEFAULT_CINE_TEMPLATE
        fmt, as12 = self.fmt_combo.currentData(), self.as12_check.isChecked()
        return lambda task: download_all(session, folder, template, fmt, task, as_12bit=as12)

    def _summary(self, res) -> str:
        lines = [f'Saved {len(res)} cine(s) to {self.resolved_path}:']
        for r in res:
            lines.append(f'  cine {r["cine"]}: {r["count"]} images {r["first"]} .. {r["last"]} -> {Path(r["path"]).name}'
                         + ('' if r['times'] else ' (NO camera time stamps)'))
        return '\n'.join(lines)


def _dropped_text(d) -> str:
    if not d or not d[1]:
        return 'value >> 4'
    if d[0] == 0:
        return 'value >> 4, lossless'
    return f'value >> 4; {100 * d[0] / d[1]:.1f} % of pixels lost a sub-count correction fraction'


class ExportDialog(_JobDialog):
    """File > Export, and export straight from camera RAM (``camera=(session, cine, info)``, no file):
    'cine' decimated cine (lossless, files only), 'tiff' 16-bit TIFF stack (raw values), 'tiffseq' one
    TIFF per image (PCC p.72, 76), 'mp4' H.264 movie (8-bit display render, not for measurement)."""

    KINDS = {'cine': ('Export decimated cine (lossless)', 'Cine files (*.cine)', '.cine'),
             'tiff': ('Export 16-bit TIFF stack (raw values)', 'TIFF files (*.tif *.tiff)', '.tif'),
             'tiffseq': ('Export TIFF image sequence (one file per image)', '', ''),
             'mp4': ('Export MP4 movie (8-bit display render)', 'MP4 files (*.mp4)', '.mp4')}

    def __init__(self, tasks: TaskManager, kind: str, src: str | None, first: int, last: int,
                 marks: tuple[int, int] | None = None, parent: QWidget | None = None, *,
                 camera: tuple | None = None, panel=None):
        title, _, ext = self.KINDS[kind]
        super().__init__(tasks, title + (' from camera RAM' if camera else ''), parent)
        self.kind, self.src, self.camera, self.panel = kind, src, camera, panel
        self.pcc_table = None
        self.values_combo = None
        self.as12_check = None
        if camera is not None:
            if kind == 'cine':
                raise ValueError('a camera cine is saved with Save Cine, not exported as a decimated cine')
            session, cine, info = camera
            res = info['res']
            self.img_w, self.img_h, self.packing = res.width, res.height, None
            self.fmt = 'P16' if 'P16' in session.formats else '8'      # what the Play tab shows
            self.real_bpp = P.IMAGE_FORMATS[self.fmt][1]
            serial = session.info.get('serial', '')
            stem, folder = f'cine{cine}_{serial}', Path.home()
            head = QLabel(f'Source: camera {serial} > Cine {cine} (RAM, not saved), {self.img_w} x {self.img_h}, '
                          f'read as {self.fmt}\nImages {first} .. {last} (trigger = image 0)')
        else:
            from ..cine import CineReader
            with CineReader(src) as r:
                self.img_w, self.img_h, self.packing, self.real_bpp = r.width, r.height, r.packing, r.real_bpp
            stem, folder = Path(src).stem, Path(src).parent
            head = QLabel(f'Source: {src}\nImages {first} .. {last} (numbers as stored in the file)')
        head.setWordWrap(True)
        self.range = RangeWidget(first, last, marks)
        notes = {'cine': 'Pixels, time stamps and exposures are copied bit-for-bit.',
                 'tiff': 'Pages hold the raw decoded values (no LUT, gain or gamma); image numbers and '
                         'times go into the page-0 description and a .json sidecar.',
                 'tiffseq': 'One TIFF per image, raw decoded values; each file carries its image number and time, '
                            'and <source>_sequence.json lists them all. Existing files are never replaced.',
                 'mp4': 'An 8-bit DISPLAY RENDER for viewing and presentations (H.264, plays in PowerPoint). '
                        'NOT FOR MEASUREMENT: the black/white window and gamma below are baked in. True image '
                        'numbers and times go into a .json sidecar.'}
        note = QLabel(notes[kind])
        note.setWordWrap(True)
        self.note = note
        self.path_edit.setText(str(folder / (f'{stem}_tiff' if kind == 'tiffseq' else f'{stem}_export{ext}')))
        g = QGroupBox('Range')
        QVBoxLayout(g).addWidget(self.range)
        form = QFormLayout()
        form.addRow('Output folder' if kind == 'tiffseq' else 'Output file', _path_row(self.path_edit, self.browse_btn))
        self.inputs_layout.addWidget(head)
        self.inputs_layout.addWidget(g)
        self.inputs_layout.addWidget(note)
        if kind in ('tiff', 'tiffseq'):
            self._add_values_group()
        if kind == 'mp4':
            self._add_mp4_group()
        self.crop_widget = CropWidget(self.img_w, self.img_h, panel)
        self.inputs_layout.addWidget(self.crop_widget)
        if kind == 'tiffseq':
            self.pattern_edit = QLineEdit(DEFAULT_SEQUENCE_PATTERN)
            self.pattern_edit.setToolTip('{source} = source name, {image} = signed image number (negative: m + digits, '
                                         '-123 -> m000123), {+image} = image - first image; a digit sets the minimum '
                                         'width. .tif is added.')
            self.pattern_preview = QLabel(wordWrap=True)
            self.pattern_edit.textChanged.connect(self._update_pattern)
            self.range.changed.connect(self._update_pattern)
            form.addRow('File names', self.pattern_edit)
            form.addRow('', self.pattern_preview)
            self._update_pattern()
        self.inputs_layout.addLayout(form)

    # -- groups
    def _add_values_group(self):
        self.values_combo = QComboBox()
        self.values_combo.addItem('Raw sensor values (16-bit, no processing)', 'raw')
        self.values_combo.addItem('As PCC would export (8-bit, bit-identical)', 'pcc')
        self.values_help = QLabel(wordWrap=True)
        if self.camera is None:
            try:
                self.pcc_table = find_table(self.src)
            except Exception:   # an unreadable SETUP simply means no table applies
                self.pcc_table = None
        if self.pcc_table is None:
            self.values_combo.model().item(1).setEnabled(False)
            self.values_help.setText('PCC-identical export is unavailable: ' + (
                'it needs a saved file\'s display settings.' if self.camera is not None else
                'no measured table matches this file\'s display settings.'))
        self.values_combo.currentIndexChanged.connect(self._values_changed)
        gv = QGroupBox('Pixel values')
        vv = QVBoxLayout(gv)
        vv.addWidget(self.values_combo)
        vv.addWidget(self.values_help)
        if self.camera is not None and self.fmt in ('P16', 'P16R'):
            self.as12_check = QCheckBox('12-bit values (P16 value >> 4, as a PCC-compatible 12-bit save)')
            self.as12_check.setChecked(True)
            self.as12_check.setToolTip('Unticked: the 16-bit values exactly as the camera sends them (12-bit x 16).')
            vv.addWidget(self.as12_check)
        self.inputs_layout.addWidget(gv)

    def _add_mp4_group(self):
        from ..export import MP4_MISSING, find_ffmpeg
        self.ffmpeg = find_ffmpeg()
        full = (1 << self.real_bpp) - 1
        lo, hi = getattr(self.panel, 'lo', None), getattr(self.panel, 'hi', None)
        if not (isinstance(lo, int) and isinstance(hi, int) and hi > lo):
            lo, hi = 0, full
        self.fps_spin = QDoubleSpinBox(minimum=0.1, maximum=240.0, decimals=2, value=30.0)
        self.fps_spin.setToolTip('Playback rate of the movie (constant). The recording rate and true times are in '
                                 'the sidecar and the border data.')
        self.black_spin = QSpinBox(minimum=0, maximum=full, value=min(lo, full))
        self.white_spin = QSpinBox(minimum=1, maximum=full, value=min(hi, full))
        self.gamma_spin = QDoubleSpinBox(minimum=0.1, maximum=5.0, decimals=2, singleStep=0.05, value=1.0)
        self.gamma_spin.setToolTip('Display gamma: output = input^(1/gamma); above 1 brightens mid-tones.')
        self.rotate_combo = QComboBox()
        for deg in (0, 90, 180, 270):
            self.rotate_combo.addItem(f'{deg} deg clockwise' if deg else 'No rotation', deg)
        self.fliph_check = QCheckBox('Flip horizontally')
        self.flipv_check = QCheckBox('Flip vertically')
        self.border_check = QCheckBox('Border data: image number, time from trigger, rate, exposure '
                                      '(strip below the image)')
        gm = QGroupBox('Movie')
        f = QFormLayout(gm)
        f.addRow('Frame rate (fps)', self.fps_spin)
        f.addRow('Black (raw value)', self.black_spin)
        f.addRow('White (raw value)', self.white_spin)
        f.addRow('Gamma', self.gamma_spin)
        f.addRow('Rotate', self.rotate_combo)
        flips = QHBoxLayout()
        flips.addWidget(self.fliph_check)
        flips.addWidget(self.flipv_check)
        flips.addStretch(1)
        f.addRow('Flip', flips)
        f.addRow(self.border_check)
        if self.ffmpeg is None:
            gm.setEnabled(False)
            self.start_btn.setEnabled(False)
            f.addRow(QLabel(f'<b>{MP4_MISSING}</b>', wordWrap=True))
        self.inputs_layout.addWidget(gm)

    def mp4_options(self) -> dict:
        return dict(fps=self.fps_spin.value(), black=self.black_spin.value(), white=self.white_spin.value(),
                    gamma=self.gamma_spin.value(), rotate=self.rotate_combo.currentData(),
                    flip_h=self.fliph_check.isChecked(), flip_v=self.flipv_check.isChecked(),
                    border=self.border_check.isChecked())

    def _sequence_names(self) -> tuple[str, str]:
        nums = self.range.numbers()
        if len(nums) == 0:
            return '', ''
        pat = self.pattern_edit.text().strip()
        if not tokens_in(pat) & {'image', 'image_pos'}:
            raise ValueError('the file name needs {image} or {+image}')
        stem = (f'cine{self.camera[1]}_{self.camera[0].info.get("serial", "")}' if self.camera is not None
                else Path(self.src).stem)
        fields = dict(source=stem, serial=self.camera[0].info.get('serial') if self.camera else None,
                      cinenr=self.camera[1] if self.camera else None)
        ext = '' if pat.lower().endswith(('.tif', '.tiff')) else '.tif'
        a = expand_name(pat, **fields, image=int(nums[0]), image_pos=0) + ext
        b = expand_name(pat, **fields, image=int(nums[-1]), image_pos=int(nums[-1] - nums[0])) + ext
        return a, b

    def _update_pattern(self):
        try:
            a, b = self._sequence_names()
            self.pattern_preview.setText(f'{a} .. {b}' if a else '')
        except ValueError as e:
            self.pattern_preview.setText(f'<b>{e}</b>')

    def _values_changed(self):
        pcc = self.values_combo is not None and self.values_combo.currentData() == 'pcc'
        if pcc and self.pcc_table is not None:
            self.values_help.setText(
                f'Pages are what PCC writes when it exports this file to 8-bit TIFF (display curve applied), '
                f'from table {self.pcc_table["name"]}; checked against {self.pcc_table["checked_against_pcc"]}. '
                'Not suitable for measuring intensities.')
            self.note.setText('Image numbers and times go into the page-0 description and a .json sidecar.')
        elif self.pcc_table is not None:
            self.values_help.setText('')
            self.note.setText('Pages hold the raw decoded values (no LUT, gain or gamma); image numbers and '
                              'times go into the page-0 description and a .json sidecar.')

    def set_values_mode(self, mode: str):
        if self.values_combo is not None:
            self.values_combo.setCurrentIndex(self.values_combo.findData(mode))

    def _file_filter(self):
        return self.KINDS[self.kind][1] or 'All files (*)'

    def _browse(self):
        if self.kind != 'tiffseq':
            return super()._browse()
        path = QFileDialog.getExistingDirectory(self, 'Output folder', self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def _overwrite_ok(self, path: str) -> bool:
        return True if self.kind == 'tiffseq' else super()._overwrite_ok(path)   # the sequence refuses clashes itself

    def _validate(self):
        if len(self.range.numbers()) == 0:
            return 'No images selected.'
        p = self.path_edit.text().strip()
        if not p:
            return 'Choose an output folder.' if self.kind == 'tiffseq' else 'Choose an output file.'
        if not Path(p).parent.exists():
            return f'Folder does not exist: {Path(p).parent}'
        if self.src is not None and Path(p).resolve() == Path(self.src).resolve():
            return 'The output would overwrite the source file. Choose another name.'
        msg = self.crop_widget.problem(self.packing if self.kind == 'cine' else None)
        if msg:
            return msg
        if self.kind == 'tiffseq':
            try:
                self._sequence_names()
            except ValueError as e:
                return f'File names: {e}'
        if self.kind == 'mp4':
            if self.ffmpeg is None:
                from ..export import MP4_MISSING
                return MP4_MISSING
            if self.white_spin.value() <= self.black_spin.value():
                return 'White must be above black.'
        return None

    def _job(self):
        kind, src, dst = self.kind, self.src, self.resolved_path
        first, last, step, align = self.range.values()
        crop = self.crop_widget.crop()
        pattern = self.pattern_edit.text().strip() if kind == 'tiffseq' else None
        mp4 = self.mp4_options() if kind == 'mp4' else None
        if self.camera is not None:
            session, cine, _ = self.camera
            fmt, as12 = self.fmt, bool(self.as12_check is not None and self.as12_check.isChecked() and kind != 'mp4')
            return lambda task: export_camera(session, cine, kind, dst, first, last, step, align, task, fmt=fmt,
                                              as_12bit=as12, crop=crop, pattern=pattern, mp4=mp4)
        table = None
        if self.values_combo is not None and self.values_combo.currentData() == 'pcc' and self.pcc_table is not None:
            table = self.pcc_table['csv']
        return lambda task: export_file(kind, src, dst, first, last, step, align, task, pcc_table=table, crop=crop,
                                        pattern=pattern, mp4=mp4)

    def _summary(self, r: dict) -> str:
        crop = ([f'Cropped: x {r["crop"][0]}, y {r["crop"][1]}, {r["crop"][2]} x {r["crop"][3]} (recorded in the '
                 'metadata)'] if r.get('crop') else [])
        times = [f'Times: {r["times_from"]}'] if r.get('times_from') else []
        if self.kind == 'cine':
            return '\n'.join([
                f'Wrote {r["count"]} images to {r["dst"]} (lossless copy)',
                f'Source images from {r["first"]}, step {r["step"]}, alignment {r["align"]}',
                f'Image-number mapping: {mapping_text(r["step"], r["offset"], r["first_out"], r["count"], "source")}',
            ] + crop)
        if self.kind == 'tiffseq':
            return '\n'.join([f'Wrote {r["count"]} TIFF files to {r["dst"]}: {r["files"][0]} .. {r["files"][-1]}',
                              f'Numbers and times of every file in {r["sidecar"]}'] + times + crop)
        if self.kind == 'mp4':
            return '\n'.join([f'Wrote {r["count"]} frames to {r["dst"]} at {r["fps"]:g} fps, {r["size"][0]} x '
                              f'{r["size"][1]} (8-bit display render, NOT for measurement)',
                              f'Image numbers and true times in {r["sidecar"]}'] + times + crop)
        what = (f'PCC-identical 8-bit pages (table {Path(r["pcc_table"]).stem})' if r.get('pcc_table')
                else 'raw-value pages')
        return '\n'.join([
            f'Wrote {r["count"]} {what} to {r["dst"]}',
            f'Source images {r["first"]} .. {r["last"]}; numbers and times per page in {r["sidecar"]}',
            f'Frame interval (median of time stamps): {r["finterval_s"]:.9g} s',
        ] + times + crop)


class CameraTestDialog(_JobDialog):
    """Camera > Run camera test: the hardware checklist, automated (phantastic.selftest)."""

    def __init__(self, tasks: TaskManager, session: CameraSession, report_dir: str, parent: QWidget | None = None):
        super().__init__(tasks, 'Camera test', parent)
        self.session = session
        info = QLabel(
            f'Tests the connected camera ({session.address}) and writes a report:<br>'
            '&bull; identity, firmware and image formats (read only)<br>'
            '&bull; one live frame: size, values, 12-bit alignment<br>'
            '&bull; the same 20 stored frames in every format the camera offers, cross-checked<br>'
            '&bull; time stamps<br>'
            'Uses the first stored cine. To record a short test clip first, tick the box: '
            '<b>recording erases what that cine holds</b>.')
        info.setWordWrap(True)
        self.record_check = QCheckBox('Record a test clip into cine')
        self.record_cine = QSpinBox(minimum=1, maximum=64, value=1)
        row = QHBoxLayout()
        row.addWidget(self.record_check)
        row.addWidget(self.record_cine)
        row.addStretch(1)
        self.path_edit.setText(report_dir)
        self.open_btn = QPushButton('Open report folder')
        self.open_btn.clicked.connect(self._open_folder)
        form = QFormLayout()
        form.addRow('Report folder', _path_row(self.path_edit, self.browse_btn))
        self.inputs_layout.addWidget(info)
        self.inputs_layout.addLayout(row)
        self.inputs_layout.addLayout(form)
        self.buttons.addButton(self.open_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.lines: list[str] = []

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, 'Report folder', self.path_edit.text())
        if path:
            self.path_edit.setText(path)
            self._confirmed_overwrite = path

    def _validate(self):
        p = self.path_edit.text().strip()
        if not p:
            return 'Choose a report folder.'
        if self.record_check.isChecked() and QMessageBox.question(
                self, 'Record?', f'Recording erases what cine {self.record_cine.value()} holds. Continue?') \
                != QMessageBox.StandardButton.Yes:
            return 'Not started.'
        return None

    def _open_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.path_edit.text().strip()))

    def _job(self):
        from ..selftest import run
        session = self.session
        out = self.path_edit.text().strip()
        cine = self.record_cine.value() if self.record_check.isChecked() else None
        lines = self.lines = []
        steps = 15

        def job(task):
            def say(line):
                lines.append(line)
                task.progress(min(len(lines), steps), steps)
            with session.use(timeout=10) as cam:
                res = run(cam.ip, cam.port, outdir=out, record_cine=cine, say=say,
                          cancelled=task.cancelled.is_set, cam=cam)
            return {'lines': list(lines), 'outdir': out, 'failed': sum(r.status == 'FAIL' for r in res),
                    'warned': sum(r.status == 'WARN' for r in res)}
        return job

    def _summary(self, r: dict) -> str:
        skipped = sum(line.startswith('[SKIP]') for line in r['lines'])
        if r['failed'] or r['warned']:
            head = f'{r["failed"]} FAILED, {r["warned"]} WARNINGS'
        elif skipped:
            head = f'NO FAILURES, {skipped} SKIPPED (tick Record to test the download formats)'
        else:
            head = 'ALL CHECKS PASSED'
        return head + '\n' + '\n'.join(r['lines'])


class ConnectDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, ip: str = '100.100.100.1', port: int = P.CONTROL_PORT):
        super().__init__(parent)
        self.setWindowTitle('Connect by IP')
        self.ip_edit = QLineEdit(ip)
        self.port_spin = QSpinBox(minimum=1, maximum=65535, value=port)
        form = QFormLayout(self)
        form.addRow('IP address', self.ip_edit)
        form.addRow('Control port', self.port_spin)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def address(self) -> tuple[str, int]:
        return self.ip_edit.text().strip(), self.port_spin.value()
