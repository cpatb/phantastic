"""Dialogs: connect by IP, save a camera cine, export a file, about."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .. import protocol as P
from ..decimate import last_for_count, renumbering, select_numbers
from ..pcc_render import find_table
from .workers import CameraSession, Cancelled, TaskManager, describe_error, download_cine, export_file

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
        self.buttons = QDialogButtonBox()
        self.start_btn = self.buttons.addButton('Start', QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_btn = self.buttons.addButton('Cancel', QDialogButtonBox.ButtonRole.RejectRole)
        self.close_btn = self.buttons.addButton('Close', QDialogButtonBox.ButtonRole.DestructiveRole)
        self.cancel_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn.clicked.connect(self.cancel)
        self.close_btn.clicked.connect(self.close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.inputs)
        lay.addWidget(self.progress)
        lay.addWidget(self.summary)
        lay.addWidget(self.buttons)
        self.resize(560, 560)

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

    def start(self):
        if self.running():
            return
        msg = self._validate()
        if msg:
            self.summary.setPlainText(msg)
            return
        path = self.path_edit.text().strip()
        if Path(path).exists() and path != self._confirmed_overwrite:
            if QMessageBox.question(self, 'Overwrite?', f'{self.path_edit.text()} exists. Replace it?') \
                    != QMessageBox.StandardButton.Yes:
                return
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
        self.summary.setPlainText(head + tail + f'\nNothing was written to {self.path_edit.text()}.')
        self.finished_job.emit(e)

    def closeEvent(self, event):
        if self.running():
            if QMessageBox.question(self, 'Cancel?', 'A job is running. Cancel it?') != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancel()
        super().closeEvent(event)


class SaveCineDialog(_JobDialog):
    """PCC's Save Cine: download a stored camera cine to a .cine file.

    ``marks`` = (Mark In, Mark Out) from the Play tab; the Range Option defaults to them.
    """

    def __init__(self, tasks: TaskManager, session: CameraSession, cine: int, info: dict,
                 default_path: str = '', marks: tuple[int, int] | None = None, parent: QWidget | None = None):
        super().__init__(tasks, 'Save Cine', parent)
        self.session, self.cine, self.cine_info = session, cine, info
        lo, hi = int(info['firstfr']), int(info['lastfr'])
        res = info.get('res')
        head = QLabel(f'Camera {session.info.get("serial", "")} > Cine {cine}: images {lo} .. {hi} '
                      f'({hi - lo + 1} frames), {res}, {info.get("rate")} fps, trigger = image 0')
        head.setWordWrap(True)
        self.range = RangeWidget(lo, hi, marks)
        self.fmt_combo = QComboBox()
        offered = [f for f in DOWNLOAD_FORMATS if f in session.formats] or ['P16']
        for f in offered:
            self.fmt_combo.addItem(f, f)
            self.fmt_combo.setItemData(self.fmt_combo.count() - 1, FORMAT_HELP[f], Qt.ItemDataRole.ToolTipRole)
        self.fmt_help = QLabel(wordWrap=True)
        self.fmt_combo.currentIndexChanged.connect(self._format_changed)
        self.fmt_help.setText(FORMAT_HELP[self.fmt_combo.currentData()])
        self.as12_check = QCheckBox('PCC-compatible 12-bit (untick for full 16-bit: ImageJ/Phantastic only)')
        self.as12_check.setToolTip('P16 arrives full-scale (12-bit value x 16); PCC shows such a file almost white. '
                                   'Ticked, the file holds value >> 4 in PCC\'s own layout. P16R loses nothing; P16 '
                                   'loses only the camera\'s correction fraction below one 12-bit count.')
        self.as12_check.setEnabled(self.fmt_combo.currentData() in ('P16', 'P16R'))
        self.as12_check.setChecked(self.as12_check.isEnabled())
        self.path_edit.setText(default_path)
        g1 = QGroupBox('Range')
        QVBoxLayout(g1).addWidget(self.range)
        g2 = QGroupBox('Wire format')
        v = QVBoxLayout(g2)
        v.addWidget(self.fmt_combo)
        v.addWidget(self.fmt_help)
        v.addWidget(self.as12_check)
        form = QFormLayout()
        form.addRow('Output file', _path_row(self.path_edit, self.browse_btn))
        self.inputs_layout.addWidget(head)
        self.inputs_layout.addWidget(g1)
        self.inputs_layout.addWidget(g2)
        self.inputs_layout.addLayout(form)

    def _format_changed(self):
        fmt = self.fmt_combo.currentData()
        self.fmt_help.setText(FORMAT_HELP[fmt])
        ok = fmt in ('P16', 'P16R')
        self.as12_check.setEnabled(ok)
        self.as12_check.setChecked(ok)

    def set_format(self, fmt: str):
        self.fmt_combo.setCurrentIndex(self.fmt_combo.findData(fmt))

    def _file_filter(self):
        return 'Cine files (*.cine)'

    def _validate(self):
        if len(self.range.numbers()) == 0:
            return 'No images selected.'
        return super()._validate()

    def _job(self):
        session, cine = self.session, self.cine
        first, last, step, align = self.range.values()
        fmt = self.fmt_combo.currentData()
        path = self.path_edit.text().strip()
        as12 = self.as12_check.isChecked()   # unchecked whenever the format does not allow it
        return lambda task: download_cine(session, cine, path, first, last, step, align, fmt, task, as_12bit=as12)

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
        ])


def _dropped_text(d) -> str:
    if not d or not d[1]:
        return 'value >> 4'
    if d[0] == 0:
        return 'value >> 4, lossless'
    return f'value >> 4; {100 * d[0] / d[1]:.1f} % of pixels lost a sub-count correction fraction'


class ExportDialog(_JobDialog):
    """File > Export: decimated cine (lossless) or 16-bit TIFF stack (raw values)."""

    KINDS = {'cine': ('Export decimated cine (lossless)', 'Cine files (*.cine)', '.cine'),
             'tiff': ('Export 16-bit TIFF stack (raw values)', 'TIFF files (*.tif *.tiff)', '.tif')}

    def __init__(self, tasks: TaskManager, kind: str, src: str, first: int, last: int,
                 marks: tuple[int, int] | None = None, parent: QWidget | None = None):
        title, _, ext = self.KINDS[kind]
        super().__init__(tasks, title, parent)
        self.kind, self.src = kind, src
        head = QLabel(f'Source: {src}\nImages {first} .. {last} (numbers as stored in the file)')
        head.setWordWrap(True)
        self.range = RangeWidget(first, last, marks)
        note = QLabel('Pixels, time stamps and exposures are copied bit-for-bit.' if kind == 'cine' else
                      'Pages hold the raw decoded values (no LUT, gain or gamma); image numbers and '
                      'times go into the page-0 description and a .json sidecar.')
        note.setWordWrap(True)
        sp = Path(src)
        self.path_edit.setText(str(sp.with_name(f'{sp.stem}_export{ext}')))
        g = QGroupBox('Range')
        QVBoxLayout(g).addWidget(self.range)
        form = QFormLayout()
        form.addRow('Output file', _path_row(self.path_edit, self.browse_btn))
        self.inputs_layout.addWidget(head)
        self.inputs_layout.addWidget(g)
        self.inputs_layout.addWidget(note)
        self.pcc_table = None
        self.values_combo = None
        if kind == 'tiff':
            self.values_combo = QComboBox()
            self.values_combo.addItem('Raw sensor values (16-bit, no processing)', 'raw')
            self.values_combo.addItem('As PCC would export (8-bit, bit-identical)', 'pcc')
            try:
                self.pcc_table = find_table(src)
            except Exception:   # an unreadable SETUP simply means no table applies
                self.pcc_table = None
            self.values_help = QLabel(wordWrap=True)
            if self.pcc_table is None:
                self.values_combo.model().item(1).setEnabled(False)
                self.values_help.setText('PCC-identical export is unavailable: no measured table matches this '
                                         'file\'s display settings.')
            self.values_combo.currentIndexChanged.connect(self._values_changed)
            gv = QGroupBox('Pixel values')
            vv = QVBoxLayout(gv)
            vv.addWidget(self.values_combo)
            vv.addWidget(self.values_help)
            self.inputs_layout.addWidget(gv)
            self.note = note
        self.inputs_layout.addLayout(form)

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
        return self.KINDS[self.kind][1]

    def _validate(self):
        if len(self.range.numbers()) == 0:
            return 'No images selected.'
        msg = super()._validate()
        if msg:
            return msg
        if Path(self.path_edit.text().strip()).resolve() == Path(self.src).resolve():
            return 'The output would overwrite the source file. Choose another name.'
        return None

    def _job(self):
        kind, src = self.kind, self.src
        first, last, step, align = self.range.values()
        dst = self.path_edit.text().strip()
        table = None
        if self.values_combo is not None and self.values_combo.currentData() == 'pcc' and self.pcc_table is not None:
            table = self.pcc_table['csv']
        return lambda task: export_file(kind, src, dst, first, last, step, align, task, pcc_table=table)

    def _summary(self, r: dict) -> str:
        if self.kind == 'cine':
            return '\n'.join([
                f'Wrote {r["count"]} images to {r["dst"]} (lossless copy)',
                f'Source images from {r["first"]}, step {r["step"]}, alignment {r["align"]}',
                f'Image-number mapping: {mapping_text(r["step"], r["offset"], r["first_out"], r["count"], "source")}',
            ])
        what = (f'PCC-identical 8-bit pages (table {Path(r["pcc_table"]).stem})' if r.get('pcc_table')
                else 'raw-value pages')
        return '\n'.join([
            f'Wrote {r["count"]} {what} to {r["dst"]}',
            f'Source images {r["first"]} .. {r["last"]}; numbers and times per page in {r["sidecar"]}',
            f'Frame interval (median of time stamps): {r["finterval_s"]:.9g} s',
        ])


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
