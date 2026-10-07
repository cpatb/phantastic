"""PCC's Image Tools window (Ctrl+I): histogram, display range ('Bit Slider'), flips and overlays
of the active panel. Everything here changes the screen only."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from .panels import ImagePanel
from .widgets import CollapsibleSection

HIST_BINS = 256
HIST_MIN_INTERVAL_MS = 100     # recompute the histogram at most 10 times a second during playback


class HistogramWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.counts: np.ndarray | None = None
        self.top = 255
        self.lo, self.hi = 0, 255
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, counts: np.ndarray | None, top: int, lo: int, hi: int):
        self.counts, self.top, self.lo, self.hi = counts, max(top, 1), lo, hi
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
            # yellow tone line: the display mapping from (lo -> black) to (hi -> white)
            xl = r.left() + r.width() * self.lo / (self.top + 1)
            xh = r.left() + r.width() * self.hi / (self.top + 1)
            p.setPen(QPen(QColor(240, 210, 40), 1.5))
            p.drawLine(QPointF(r.left(), r.bottom()), QPointF(xl, r.bottom()))
            p.drawLine(QPointF(xl, r.bottom()), QPointF(xh, r.top()))
            p.drawLine(QPointF(xh, r.top()), QPointF(r.right(), r.top()))
        p.end()


class ImageToolsWindow(QWidget):
    """Floating tool window bound to the active panel (``set_panel``)."""

    def __init__(self, parent: QWidget | None = None, cross_action=None, grid_action=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle('Image Tools')
        self.panel: ImagePanel | None = None
        self._cross_action, self._grid_action = cross_action, grid_action

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

        # Adjustments: Bit Slider
        self.min_spin = QSpinBox(minimum=0, maximum=65535)
        self.max_spin = QSpinBox(minimum=0, maximum=65535)
        self.min_slider = QSlider(Qt.Orientation.Horizontal)
        self.max_slider = QSlider(Qt.Orientation.Horizontal)
        self.levels_label = QLabel('0 ... levels ... 255')
        self.auto_btn = QPushButton('Auto')
        self.auto_btn.setToolTip('Fit the display range to this frame, 0.35 % saturated. Display only.')
        self.full_btn = QPushButton('Full')
        self.full_btn.setToolTip('Display the full range of the values (bit depth). Display only.')
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
        self.adjustments = CollapsibleSection('Adjustments', aw, expanded=True)
        self.min_spin.valueChanged.connect(self.min_slider.setValue)
        self.max_spin.valueChanged.connect(self.max_slider.setValue)
        self.min_slider.valueChanged.connect(self.min_spin.setValue)
        self.max_slider.valueChanged.connect(self.max_spin.setValue)
        self.min_spin.valueChanged.connect(self._range_edited)
        self.max_spin.valueChanged.connect(self._range_edited)
        self.auto_btn.clicked.connect(lambda: self.panel and self.panel.auto())
        self.full_btn.clicked.connect(lambda: self.panel and self.panel.full())

        # Geometry & Overlays
        self.flip_h = QCheckBox('Flip H')
        self.flip_v = QCheckBox('Flip V')
        self.grid_check = QCheckBox('Grid')
        self.cross_check = QCheckBox('Cross')
        gw = QWidget()
        gg = QGridLayout(gw)
        gg.setContentsMargins(4, 4, 4, 4)
        gg.addWidget(self.flip_h, 0, 0)
        gg.addWidget(self.flip_v, 0, 1)
        gg.addWidget(QLabel('Overlays:'), 1, 0)
        gg.addWidget(self.grid_check, 2, 0)
        gg.addWidget(self.cross_check, 2, 1)
        self.geometry = CollapsibleSection('Geometry & Overlays', gw, expanded=True)
        self.flip_h.toggled.connect(lambda on: self._flip('flip_h', on))
        self.flip_v.toggled.connect(lambda on: self._flip('flip_v', on))
        if grid_action is not None:
            self.grid_check.setChecked(grid_action.isChecked())
            self.grid_check.toggled.connect(grid_action.setChecked)
            grid_action.toggled.connect(self.grid_check.setChecked)
        if cross_action is not None:
            self.cross_check.setChecked(cross_action.isChecked())
            self.cross_check.toggled.connect(cross_action.setChecked)
            cross_action.toggled.connect(self.cross_check.setChecked)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        lay.addWidget(note)
        lay.addWidget(self.target_label)
        for s in (self.histogram, self.adjustments, self.geometry):
            lay.addWidget(s)
        lay.addStretch(1)
        self.resize(290, 470)

        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(HIST_MIN_INTERVAL_MS)
        self._hist_timer.timeout.connect(self._update_histogram)

    def set_panel(self, panel: ImagePanel | None):
        if self.panel is not None:
            for sig, slot in ((self.panel.frame_changed, self._frame_changed),
                              (self.panel.display_changed, self._sync_range)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.panel = panel
        for w in (self.adjustments, self.geometry):
            w.setEnabled(panel is not None)
        if panel is None:
            self.target_label.setText('No active panel')
            self.hist.set_data(None, 255, 0, 255)
            self.avg_label.setText('Avg: -')
            return
        self.target_label.setText(f'Panel: {panel.title()}')
        panel.frame_changed.connect(self._frame_changed)
        panel.display_changed.connect(self._sync_range)
        for w in (self.flip_h, self.flip_v):
            w.blockSignals(True)
        self.flip_h.setChecked(panel.view.flip_h)
        self.flip_v.setChecked(panel.view.flip_v)
        for w in (self.flip_h, self.flip_v):
            w.blockSignals(False)
        self._sync_range()
        self._update_histogram()

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
        self.levels_label.setText(f'0 ... {p.raw_max + 1} levels ... {p.raw_max}')
        self._update_histogram()

    def _range_edited(self):
        if self.panel is not None:
            lo, hi = self.min_spin.value(), self.max_spin.value()
            if (lo, hi) != (self.panel.lo, self.panel.hi):
                self.panel.set_display_range(lo, max(hi, lo + 1))

    def _flip(self, attr: str, on: bool):
        if self.panel is not None:
            setattr(self.panel.view, attr, on)
            self.panel.view.update()
            self.panel.view.refresh_hover()

    def _frame_changed(self):
        if not self._hist_timer.isActive():
            self._hist_timer.start()

    def _update_histogram(self):
        p = self.panel
        if p is None or p.frame is None or not self.isVisible():
            return
        f = p.frame
        counts, _ = np.histogram(f, bins=HIST_BINS, range=(0, p.raw_max + 1))
        self.hist.set_data(counts, p.raw_max, p.lo, p.hi)
        self.avg_label.setText(f'Avg: {float(np.mean(f)):.2f}')

    def showEvent(self, event):
        super().showEvent(event)
        self._update_histogram()
