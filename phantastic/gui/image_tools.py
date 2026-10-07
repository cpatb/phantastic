"""PCC's Image Tools window (Ctrl+I): histogram, display range ('Bit Slider'), display curve (Gain,
Brightness, Gamma, Toe), rotation, flips, overlays, crop rectangle and measurements of the active
panel (PCC p.43-47, p.81-85). Everything here changes the screen only; recorded values, saved files
and the camera are never touched."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import measure
from .imageview import BRIGHTNESS_RANGE, GAIN_RANGE, GAMMA_RANGE, TOE_RANGE, display_curve
from .panels import ImagePanel
from .widgets import CollapsibleSection

HIST_BINS = 256
HIST_MIN_INTERVAL_MS = 100     # recompute the histogram at most 10 times a second during playback
CURVE_SAMPLES = 256            # points of the tone line drawn over the histogram
PHADJ_FORMAT = 'phantastic-display-adjustments'   # .phadj: Phantastic's JSON take on PCC's .adj (p.46)
PHADJ_KEYS = ('lo', 'hi', 'gamma', 'gain', 'brightness', 'toe', 'disabled', 'flip_h', 'flip_v', 'rot')
UNITS = ('mm', 'µm', 'cm', 'm', 'in')


def save_adjustments(path, adj: dict):
    """Write display settings (``ImagePanel.adjustments()``) to a .phadj JSON file."""
    Path(path).write_text(json.dumps(dict(format=PHADJ_FORMAT, version=1, **{k: adj[k] for k in PHADJ_KEYS}),
                                     indent=1), encoding='utf-8')


def load_adjustments(path) -> dict:
    d = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(d, dict) or d.get('format') != PHADJ_FORMAT:
        raise ValueError(f'{path} is not a Phantastic display-adjustments (.phadj) file')
    return {k: d[k] for k in PHADJ_KEYS if k in d}


def ask_scale(parent, d_px: float):
    """Ask the real length of a d_px-pixel segment: (length, unit), or None when cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle('Calibrate')
    length = QDoubleSpinBox(decimals=4, minimum=1e-4, maximum=1e9, value=1.0)
    unit = QComboBox()
    unit.setEditable(True)
    unit.addItems(UNITS)
    form = QFormLayout(dlg)
    form.addRow(QLabel(f'The two points are {d_px:.2f} px apart.'))
    form.addRow('Real length', length)
    form.addRow('Unit', unit)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    form.addRow(bb)
    return (length.value(), unit.currentText().strip() or 'unit') if dlg.exec() else None


def _dspin(rng, decimals, step, tip) -> QDoubleSpinBox:
    s = QDoubleSpinBox(decimals=decimals, minimum=rng[0], maximum=rng[1], singleStep=step)
    s.setToolTip(tip)
    s.setKeyboardTracking(False)
    return s


class HistogramWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.counts: np.ndarray | None = None
        self.curve: np.ndarray | None = None      # display level 0..1 at CURVE_SAMPLES points over [0, top]
        self.top = 255
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, counts: np.ndarray | None, top: int, curve: np.ndarray | None = None):
        self.counts, self.top, self.curve = counts, max(top, 1), curve
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        r = QRectF(2, 2, self.width() - 4, self.height() - 4)
        p.fillRect(r, QColor(25, 25, 25))
        if self.counts is not None and self.counts.max() > 0:
            c = self.counts / self.counts.max()
            n = len(c)
            pts = [QPointF(r.left(), r.bottom())]
            for i, v in enumerate(c):
                x = r.left() + r.width() * (i + 0.5) / n
                pts.append(QPointF(x, r.bottom() - v * r.height()))
            pts.append(QPointF(r.right(), r.bottom()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(200, 200, 200))
            p.drawPolygon(QPolygonF(pts))
        if self.curve is not None:     # yellow tone line: raw value (x) -> display level (y)
            m = len(self.curve)
            p.setPen(QPen(QColor(240, 210, 40), 1.5))
            p.drawPolyline(QPolygonF([QPointF(r.left() + r.width() * k / (m - 1), r.bottom() - y * r.height())
                                      for k, y in enumerate(self.curve)]))
        p.end()


class ImageToolsWindow(QWidget):
    """Floating tool window bound to the active panel (``set_panel``).

    The toolbar actions passed in (cross, grid, zebra, focus, crop, measure) are mirrored by the
    check boxes here, so either place toggles the same state.
    """
    zebra_level_changed = Signal(object)     # int raw level, or None for the format's saturation

    def __init__(self, parent: QWidget | None = None, cross_action=None, grid_action=None, zebra_action=None,
                 focus_action=None, crop_action=None, measure_action=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle('Image Tools')
        self.panel: ImagePanel | None = None
        self._cross_action, self._grid_action = cross_action, grid_action
        self._crop_action, self._measure_action = crop_action, measure_action

        note = self.note_label = QLabel('Display only: recorded values are never changed.')
        note.setWordWrap(True)
        note.setStyleSheet('color: #7a1010;')
        self.target_label = QLabel('No active panel')

        # Histogram
        self.hist = HistogramWidget()
        self.avg_label = QLabel('Avg: -')
        hw = QWidget()
        hv = QVBoxLayout(hw)
        hv.setContentsMargins(4, 4, 4, 4)
        hv.addWidget(self.hist)
        hv.addWidget(self.avg_label)
        self.histogram = CollapsibleSection('Histogram', hw, expanded=True)

        # Adjustments: Bit Slider, Gain, Brightness, Gamma, Toe (p.43), Disable / Default / Save / Load (p.46-47)
        self.min_spin = QSpinBox(minimum=0, maximum=65535)
        self.max_spin = QSpinBox(minimum=0, maximum=65535)
        self.min_slider = QSlider(Qt.Orientation.Horizontal)
        self.max_slider = QSlider(Qt.Orientation.Horizontal)
        self.levels_label = QLabel('0 ... levels ... 255')
        self.auto_btn = QPushButton('Auto')
        self.auto_btn.setToolTip('Fit the display range to this frame, 0.35 % saturated. Display only.')
        self.full_btn = QPushButton('Full')
        self.full_btn.setToolTip('Display the full range of the values (bit depth). Display only.')
        self.gain_spin = _dspin(GAIN_RANGE, 3, 0.1, 'Gain (contrast) about black: 1 = off. Display only.')
        self.brightness_spin = _dspin(BRIGHTNESS_RANGE, 3, 0.01,
                                      'Brightness, a fraction of full display scale added: 0 = off. Display only.')
        self.gamma_spin = _dspin(GAMMA_RANGE, 3, 0.1,
                                 'Gamma: display = x^(1/gamma), so 2.222 (PCC\'s default) brightens mid-tones; '
                                 '1 = linear. Display only.')
        self.toe_spin = _dspin(TOE_RANGE, 3, 0.05, 'Toe: below 1 lifts the shadows only; 1 = off. Display only.')
        self.disable_btn = QPushButton('Disable')
        self.disable_btn.setCheckable(True)
        self.disable_btn.setToolTip('Show the values linearly over the full range; click again to re-enable.')
        self.default_btn = QPushButton('Default')
        self.default_btn.setToolTip('Linear curve (gamma, gain, toe 1; brightness 0), range fitted to this frame.')
        self.save_adj_btn = QPushButton('Save...')
        self.save_adj_btn.setToolTip('Save these display settings to a .phadj file')
        self.load_adj_btn = QPushButton('Load...')
        self.load_adj_btn.setToolTip('Apply display settings from a .phadj file to this panel')
        aw = QWidget()
        ag = QGridLayout(aw)
        ag.setContentsMargins(4, 4, 4, 4)
        ag.addWidget(QLabel('Bit Slider'), 0, 0)
        ag.addWidget(self.levels_label, 0, 1, 1, 2)
        ag.addWidget(QLabel('min'), 1, 0)
        ag.addWidget(self.min_slider, 1, 1)
        ag.addWidget(self.min_spin, 1, 2)
        ag.addWidget(QLabel('max'), 2, 0)
        ag.addWidget(self.max_slider, 2, 1)
        ag.addWidget(self.max_spin, 2, 2)
        bh = QHBoxLayout()
        bh.addStretch(1)
        bh.addWidget(self.auto_btn)
        bh.addWidget(self.full_btn)
        ag.addLayout(bh, 3, 0, 1, 3)
        for row, (text, spin) in enumerate((('Gain', self.gain_spin), ('Brightness', self.brightness_spin),
                                            ('Gamma', self.gamma_spin), ('Toe', self.toe_spin)), 4):
            ag.addWidget(QLabel(text), row, 0)
            ag.addWidget(spin, row, 1, 1, 2)
        bh2 = QHBoxLayout()
        for b in (self.disable_btn, self.default_btn, self.save_adj_btn, self.load_adj_btn):
            bh2.addWidget(b)
        ag.addLayout(bh2, 8, 0, 1, 3)
        self.adjustments = CollapsibleSection('Adjustments', aw, expanded=True)
        self.min_spin.valueChanged.connect(self.min_slider.setValue)
        self.max_spin.valueChanged.connect(self.max_slider.setValue)
        self.min_slider.valueChanged.connect(self.min_spin.setValue)
        self.max_slider.valueChanged.connect(self.max_spin.setValue)
        self.min_spin.valueChanged.connect(self._range_edited)
        self.max_spin.valueChanged.connect(self._range_edited)
        self.auto_btn.clicked.connect(lambda: self.panel and self.panel.auto())
        self.full_btn.clicked.connect(lambda: self.panel and self.panel.full())
        for s in (self.gain_spin, self.brightness_spin, self.gamma_spin, self.toe_spin):
            s.valueChanged.connect(self._curve_edited)
        self.disable_btn.toggled.connect(lambda on: self.panel and self.panel.set_curve(disabled=on))
        self.default_btn.clicked.connect(lambda: self.panel and self.panel.default_display())
        self.save_adj_btn.clicked.connect(self._save_dialog)
        self.load_adj_btn.clicked.connect(self._load_dialog)

        # Geometry & Overlays (p.46, p.17, p.40)
        self.flip_h = QCheckBox('Flip H')
        self.flip_v = QCheckBox('Flip V')
        self.rot_ccw = QCheckBox('Rotate 90° CCW')
        self.rot_cw = QCheckBox('Rotate 90° CW')
        self.grid_check = QCheckBox('Grid')
        self.cross_check = QCheckBox('Cross')
        self.zebra_check = QCheckBox('Zebra')
        self.zebra_check.setToolTip('Stripe pixels at or above the level (saturation by default). Display only.')
        self.zebra_spin = QSpinBox(minimum=0, maximum=65535)
        self.zebra_spin.setSpecialValueText('saturation')
        self.zebra_spin.setSuffix(' raw')
        self.zebra_spin.setKeyboardTracking(False)
        self.zebra_spin.setToolTip('Zebra at RAW values >= this, for every panel (camera P16: 16 x the '
                                   '12-bit value the status bar shows); "saturation" = each format\'s full scale')
        self.focus_check = QCheckBox('Focus Assist')
        self.focus_check.setToolTip('Overlay a Sobel edge map on the live image (live panels only). Display only.')
        gw = QWidget()
        gg = QGridLayout(gw)
        gg.setContentsMargins(4, 4, 4, 4)
        gg.addWidget(self.flip_h, 0, 0)
        gg.addWidget(self.flip_v, 0, 1)
        gg.addWidget(self.rot_ccw, 1, 0)
        gg.addWidget(self.rot_cw, 1, 1)
        gg.addWidget(QLabel('Overlays:'), 2, 0)
        gg.addWidget(self.grid_check, 3, 0)
        gg.addWidget(self.cross_check, 3, 1)
        gg.addWidget(self.zebra_check, 4, 0)
        gg.addWidget(self.zebra_spin, 4, 1)
        gg.addWidget(self.focus_check, 5, 0, 1, 2)
        self.geometry = CollapsibleSection('Geometry & Overlays', gw, expanded=True)
        self.flip_h.toggled.connect(lambda on: self._flip('flip_h', on))
        self.flip_v.toggled.connect(lambda on: self._flip('flip_v', on))
        self.rot_ccw.toggled.connect(lambda on: self._rotate(3 if on else 0))
        self.rot_cw.toggled.connect(lambda on: self._rotate(1 if on else 0))
        self.zebra_spin.valueChanged.connect(lambda v: self.zebra_level_changed.emit(v or None))
        for check, action in ((self.grid_check, grid_action), (self.cross_check, cross_action),
                              (self.zebra_check, zebra_action), (self.focus_check, focus_action)):
            if action is not None:
                check.setChecked(action.isChecked())
                check.toggled.connect(action.setChecked)
                action.toggled.connect(check.setChecked)

        # Crop rectangle (p.46): stored-array coordinates, display only here
        self.crop_check = QCheckBox('Crop')
        self.crop_check.setToolTip('Crop rectangle in stored-array pixels (0-based, row 0 at the top). Draw it '
                                   'with the Crop tool or type it. Shown as an outline only.')
        self.crop_spins = {k: QSpinBox(minimum=0 if k in 'xy' else 1, maximum=65535) for k in ('x', 'y', 'w', 'h')}
        for s in self.crop_spins.values():
            s.setKeyboardTracking(False)           # apply on Enter / focus-out, not per keystroke
        self.crop_tool_btn = QPushButton('Draw')
        self.crop_tool_btn.setToolTip('Crop tool: drag a rectangle on the image')
        cw = QWidget()
        cg = QGridLayout(cw)
        cg.setContentsMargins(4, 4, 4, 4)
        cg.addWidget(self.crop_check, 0, 0)
        cg.addWidget(self.crop_tool_btn, 0, 3)
        for i, k in enumerate(('x', 'y', 'w', 'h')):
            cg.addWidget(QLabel(k.upper()), 1 + i // 2, 2 * (i % 2))
            cg.addWidget(self.crop_spins[k], 1 + i // 2, 2 * (i % 2) + 1)
        self.crop = CollapsibleSection('Crop', cw, expanded=False)
        self.crop_check.toggled.connect(self._crop_edited)
        for s in self.crop_spins.values():
            s.valueChanged.connect(self._crop_edited)
        if crop_action is not None:
            self.crop_tool_btn.clicked.connect(lambda: crop_action.trigger())

        # Measurement (p.81-85): calibrate, 2-point distance / angle / speed, results table
        self.calibrate_btn = QPushButton('Calibrate')
        self.calibrate_btn.setToolTip('Click two points a known distance apart, then enter the distance')
        self.clear_scale_btn = QPushButton('No scale')
        self.clear_scale_btn.setToolTip('Forget the scale: measure in pixels')
        self.scale_label = QLabel('Scale: none (pixels)')
        self.measure_btn = QPushButton('Measure')
        self.measure_btn.setToolTip('Click point 1, then point 2 (on another image for a speed)')
        self.measure_status = QLabel('')
        self.measure_status.setWordWrap(True)
        self.table = QTableWidget(0, len(measure.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(measure.COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(110)
        self.table.horizontalHeaderItem(3).setToolTip('Points: stored-array pixel indices, 0-based, row 0 at top')
        self.copy_btn = QPushButton('Copy')
        self.csv_btn = QPushButton('Save CSV...')
        self.clear_meas_btn = QPushButton('Clear')
        mw = QWidget()
        mg = QGridLayout(mw)
        mg.setContentsMargins(4, 4, 4, 4)
        mg.addWidget(self.calibrate_btn, 0, 0)
        mg.addWidget(self.clear_scale_btn, 0, 1)
        mg.addWidget(self.measure_btn, 0, 2)
        mg.addWidget(self.scale_label, 1, 0, 1, 3)
        mg.addWidget(self.measure_status, 2, 0, 1, 3)
        mg.addWidget(self.table, 3, 0, 1, 3)
        mg.addWidget(self.copy_btn, 4, 0)
        mg.addWidget(self.csv_btn, 4, 1)
        mg.addWidget(self.clear_meas_btn, 4, 2)
        self.measurement = CollapsibleSection('Measurement', mw, expanded=False)
        self.calibrate_btn.clicked.connect(self._start_calibration)
        self.clear_scale_btn.clicked.connect(lambda: self.panel and self.panel.set_scale(None))
        if measure_action is not None:
            self.measure_btn.clicked.connect(lambda: measure_action.trigger())
        self.copy_btn.clicked.connect(self.copy_table)
        self.csv_btn.clicked.connect(self._csv_dialog)
        self.clear_meas_btn.clicked.connect(lambda: self.panel and self.panel.clear_measurements())

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        lay.addWidget(note)
        lay.addWidget(self.target_label)
        for s in (self.histogram, self.adjustments, self.geometry, self.crop, self.measurement):
            lay.addWidget(s)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.resize(320, 640)

        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(HIST_MIN_INTERVAL_MS)
        self._hist_timer.timeout.connect(self._update_histogram)

    # ------------------------------------------------------------------ binding
    def _signals(self, panel):
        return ((panel.frame_changed, self._frame_changed), (panel.display_changed, self._sync_range),
                (panel.crop_changed, self._sync_crop), (panel.measurements_changed, self._sync_measurements))

    def set_panel(self, panel: ImagePanel | None):
        if self.panel is not None:
            for sig, slot in self._signals(self.panel):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.panel = panel
        for w in (self.adjustments, self.geometry, self.crop, self.measurement):
            w.setEnabled(panel is not None)
        if panel is None:
            self.target_label.setText('No active panel')
            self.hist.set_data(None, 255)
            self.avg_label.setText('Avg: -')
            return
        self.target_label.setText(f'Panel: {panel.title()}')
        for sig, slot in self._signals(panel):
            sig.connect(slot)
        self._sync_geometry()
        self._sync_range()
        self._sync_crop()
        self._sync_measurements()
        self._update_histogram()

    def _set_quietly(self, widget, setter, value):
        widget.blockSignals(True)
        getattr(widget, setter)(value)
        widget.blockSignals(False)

    def _sync_geometry(self):
        v = self.panel.view
        self._set_quietly(self.flip_h, 'setChecked', v.flip_h)
        self._set_quietly(self.flip_v, 'setChecked', v.flip_v)
        self._set_quietly(self.rot_cw, 'setChecked', v.rot == 1)
        self._set_quietly(self.rot_ccw, 'setChecked', v.rot == 3)

    def _sync_range(self):
        p = self.panel
        if p is None:
            return
        for w in (self.min_spin, self.max_spin, self.min_slider, self.max_slider):
            w.blockSignals(True)
            w.setRange(0, p.raw_max)
        self.min_spin.setValue(p.lo)
        self.max_spin.setValue(p.hi)
        self.min_slider.setValue(p.lo)
        self.max_slider.setValue(p.hi)
        for w in (self.min_spin, self.max_spin, self.min_slider, self.max_slider):
            w.blockSignals(False)
        for spin, v in ((self.gain_spin, p.gain), (self.brightness_spin, p.brightness), (self.gamma_spin, p.gamma),
                        (self.toe_spin, p.toe)):
            self._set_quietly(spin, 'setValue', v)
        self._set_quietly(self.disable_btn, 'setChecked', p.curve_disabled)
        for w in (self.min_spin, self.max_spin, self.min_slider, self.max_slider, self.gain_spin,
                  self.brightness_spin, self.gamma_spin, self.toe_spin):
            w.setEnabled(not p.curve_disabled)
        self.levels_label.setText(f'0 ... {p.raw_max + 1} levels ... {p.raw_max}')
        self._update_histogram()

    def _range_edited(self):
        if self.panel is not None:
            lo, hi = self.min_spin.value(), self.max_spin.value()
            if (lo, hi) != (self.panel.lo, self.panel.hi):
                self.panel.set_display_range(lo, max(hi, lo + 1))

    def _curve_edited(self):
        if self.panel is not None:
            self.panel.set_curve(gamma=self.gamma_spin.value(), gain=self.gain_spin.value(),
                                 brightness=self.brightness_spin.value(), toe=self.toe_spin.value())

    def _flip(self, attr: str, on: bool):
        if self.panel is not None:
            setattr(self.panel.view, attr, on)
            self.panel.view.refresh_hover()

    def _rotate(self, k: int):
        """k clockwise quarter turns: 0, 1 (CW) or 3 (CCW); PCC offers the two 90° turns (p.46)."""
        self._set_quietly(self.rot_cw, 'setChecked', k == 1)
        self._set_quietly(self.rot_ccw, 'setChecked', k == 3)
        if self.panel is not None:
            self.panel.view.rot = k
            self.panel.view.refresh_hover()

    # ------------------------------------------------------------------ adjustments files
    def _save_dialog(self):
        if self.panel is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save display settings', '', 'Display settings (*.phadj)')
        if path:
            save_adjustments(path, self.panel.adjustments())

    def _load_dialog(self):
        if self.panel is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Load display settings', '', 'Display settings (*.phadj)')
        if path:
            try:
                self.load_file(path)
            except (OSError, ValueError, KeyError, TypeError) as e:
                QMessageBox.warning(self, 'Load display settings', f'{path}: {e}')

    def load_file(self, path):
        self.panel.apply_adjustments(load_adjustments(path))
        self._sync_geometry()

    # ------------------------------------------------------------------ crop
    def _sync_crop(self, *_):
        p = self.panel
        if p is None:
            return
        r = p.crop_rect()
        self._set_quietly(self.crop_check, 'setChecked', r is not None)
        if r is not None:
            for k, v in zip(('x', 'y', 'w', 'h'), r):
                self._set_quietly(self.crop_spins[k], 'setValue', v)

    def _crop_edited(self):
        p = self.panel
        if p is None:
            return
        if not self.crop_check.isChecked():
            p.set_crop_rect(None)
            return
        x, y, w, h = (self.crop_spins[k].value() for k in ('x', 'y', 'w', 'h'))
        if p.crop_rect() is None and (w, h) == (1, 1) and p.frame is not None:   # fresh box: whole image
            h, w = p.frame.shape[:2]
        p.set_crop_rect((x, y, w, h))
        self._sync_crop()

    # ------------------------------------------------------------------ measurement
    def _start_calibration(self):
        if self.panel is None:
            return
        self.panel.start_calibration()
        if self._measure_action is not None and not self._measure_action.isChecked():
            self._measure_action.trigger()
        self._sync_measurements()

    def _sync_measurements(self):
        p = self.panel
        if p is None:
            return
        self.scale_label.setText(f'Scale: {p.scale:.6g} px/{p.unit}' if p.scale else 'Scale: none (pixels)')
        if p.pending_point is not None:
            img = p.pending_point['image']
            what = 'calibration' if p.measure_purpose == 'calibrate' else 'measurement'
            self.measure_status.setText(f'Point 1 of the {what} at {p.pending_point["p"]}'
                                        + ('' if img is None else f', image {img}') + '; click point 2.')
        elif p.measure_purpose == 'calibrate':
            self.measure_status.setText('Calibrate: click one end of the known length.')
        else:
            self.measure_status.setText('')
        rows = measure.rows(p.measurements, p.scale, p.unit)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))

    def csv_text(self, delimiter: str = ',') -> str:
        p = self.panel
        return '' if p is None else measure.to_csv(p.measurements, p.scale, p.unit, delimiter)

    def copy_table(self):
        QGuiApplication.clipboard().setText(self.csv_text('\t'))

    def save_csv(self, path):
        Path(path).write_text(self.csv_text(), encoding='utf-8', newline='')

    def _csv_dialog(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save measurements', '', 'CSV (*.csv)')
        if path:
            self.save_csv(path)

    # ------------------------------------------------------------------ histogram
    def _frame_changed(self):
        if not self._hist_timer.isActive():
            self._hist_timer.start()

    def _update_histogram(self):
        p = self.panel
        if p is None or p.frame is None or not self.isVisible():
            return
        f = p.frame
        counts, _ = np.histogram(f, bins=HIST_BINS, range=(0, p.raw_max + 1))
        xs = np.linspace(0, p.raw_max, CURVE_SAMPLES)
        if p.curve_disabled:
            curve = display_curve(xs, 0, p.raw_max)
        else:
            curve = display_curve(xs, p.lo, p.hi, p.gamma, p.gain, p.brightness, p.toe)
        self.hist.set_data(counts, p.raw_max, curve / 255.0)
        self.avg_label.setText(f'Avg: {float(np.mean(f)):.2f}')

    def showEvent(self, event):
        super().showEvent(event)
        self._update_histogram()
