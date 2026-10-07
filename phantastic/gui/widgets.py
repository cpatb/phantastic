"""Small PCC-style widgets: collapsible 'selectors', big coloured buttons, the image-range bar,
the cine editor bar and the Live/Play/Rec status line of an image panel."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

LAVENDER = '#e3e3f5'        # PCC's selector header colour (img p.30, p.53)
RED, GREEN, GREY = '#e0301e', '#1f9a2e', '#9a9a9a'


class CollapsibleSection(QWidget):
    """A PCC 'selector': a lavender header with a triangle; click it to show or hide the body."""
    toggled = Signal(bool)

    def __init__(self, title: str, body: QWidget | None = None, expanded: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.header = QToolButton()
        self.header.setText(title.replace('&', '&&'))
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setStyleSheet(f'QToolButton {{ background: {LAVENDER}; border: none; padding: 2px 3px; '
                                  'text-align: left; color: #202020; }}')
        self.body = body or QWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(self.body)
        self.header.toggled.connect(self._set)
        self.header.setChecked(expanded)
        self._set(expanded)

    def _set(self, on: bool):
        self.header.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)
        self.toggled.emit(on)

    def expand(self, on: bool = True):
        self.header.setChecked(on)

    @property
    def expanded(self) -> bool:
        return self.header.isChecked()


def big_button(text: str, color: str, height: int = 40, radius: int = 8) -> QPushButton:
    """A large rounded button (PCC's Capture / Trigger / Save Cine)."""
    b = QPushButton(text)
    b.setMinimumHeight(height)
    style_big_button(b, color, radius)
    return b


def style_big_button(b, color: str, radius: int = 8):
    c = QColor(color)
    cls = type(b).__name__
    b.setStyleSheet(
        f'{cls} {{ background: {c.name()}; color: white; border: 1px solid {c.darker(140).name()}; '
        f'border-radius: {radius}px; font-weight: bold; padding: 4px 8px; }}'
        f'{cls}:hover {{ background: {c.lighter(112).name()}; }}'
        f'{cls}:pressed {{ background: {c.darker(120).name()}; }}'
        f'{cls}:disabled {{ background: {QColor(GREY).name()}; color: #e8e8e8; border-color: #808080; }}')


def transport_button(icon, tip: str, w: int = 52, h: int = 38) -> QPushButton:
    b = QPushButton()
    b.setIcon(icon)
    b.setIconSize(QSize(22, 22))
    b.setToolTip(tip)
    b.setFixedSize(w, h)
    style_big_button(b, GREEN, radius=6)
    return b


class TriggerBar(QWidget):
    """PCC's 'Image Range and Trigger Position' bar: red = pre-trigger, green = post-trigger.

    Drawing the T cursor sets the post-trigger count (``post_changed``); the bar never talks to
    the camera itself.
    """
    post_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.total = 0
        self.post = 0
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip('Red: pre-trigger frames. Green: post-trigger frames. Drag T to set Last.')

    def set_values(self, total: int | None, post: int | None):
        self.total = max(int(total or 0), 0)
        self.post = min(max(int(post or 0), 0), self.total) if self.total else 0
        self.update()

    def _bar(self) -> QRectF:
        return QRectF(6, 16, self.width() - 12, 9)

    def paintEvent(self, event):
        p = QPainter(self)
        r = self._bar()
        p.fillRect(r, QColor(220, 220, 220))
        if self.total:
            xt = r.left() + r.width() * (self.total - self.post) / self.total
            p.fillRect(QRectF(r.left(), r.top(), xt - r.left(), r.height()), QColor(150, 20, 20))
            p.fillRect(QRectF(xt, r.top(), r.right() - xt, r.height()), QColor(20, 150, 30))
            p.setPen(QPen(QColor(30, 30, 30), 2))
            p.drawLine(QPointF(xt, r.top() - 3), QPointF(xt, r.bottom() + 2))
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(xt - 8, 0, 16, 14), Qt.AlignmentFlag.AlignCenter, 'T')
        p.end()

    def _drag(self, x: float):
        if not self.total:
            return
        r = self._bar()
        frac = min(max((x - r.left()) / r.width(), 0.0), 1.0)
        post = int(round(self.total * (1 - frac)))
        if post != self.post:
            self.post = post
            self.update()
            self.post_changed.emit(post)

    def mousePressEvent(self, e):
        self._drag(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._drag(e.position().x())


class EditorBar(QWidget):
    """PCC's cine editor bar (p.58-59): first / current / last image above, Mark-In / Mark-Out
    below, the marked range drawn darker blue, a black play-head. Image 0 is the trigger.
    Clicking or dragging emits ``scrubbed(image number)``."""
    scrubbed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.lo = self.hi = self.cur = self.mark_in = self.mark_out = 0
        self.valid = False
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, lo: int, hi: int, cur: int, mark_in: int, mark_out: int):
        self.lo, self.hi, self.cur, self.mark_in, self.mark_out = lo, hi, cur, mark_in, mark_out
        self.valid = True
        self.update()

    def clear(self):
        self.valid = False
        self.update()

    def _x(self, n: int, r: QRectF) -> float:
        span = max(self.hi - self.lo, 1)
        return r.left() + r.width() * (n - self.lo) / span

    def _line(self) -> QRectF:
        return QRectF(8, 24, self.width() - 16, 4)

    def paintEvent(self, event):
        p = QPainter(self)
        r = self._line()
        fm = p.fontMetrics()
        p.setPen(QColor(40, 40, 40))
        if not self.valid:
            p.fillRect(r, QColor(200, 200, 200))
            p.end()
            return
        top = QRectF(0, 2, self.width(), 16)
        p.drawText(top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(self.lo))
        p.drawText(top, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(self.hi))
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.drawText(top, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, str(self.cur))
        p.setFont(self.font())
        p.fillRect(r, QColor(120, 160, 230))
        xi, xo = self._x(self.mark_in, r), self._x(self.mark_out, r)
        p.fillRect(QRectF(xi, r.top() - 1, max(xo - xi, 1.0), r.height() + 2), QColor(20, 50, 160))
        p.setPen(QPen(QColor(40, 40, 40), 1))
        for k in range(11):                         # ticks
            x = r.left() + r.width() * k / 10
            p.drawLine(QPointF(x, r.bottom() + 2), QPointF(x, r.bottom() + 5))
        p.setPen(QPen(QColor(20, 50, 160), 2))
        for x in (xi, xo):                           # mark brackets
            p.drawLine(QPointF(x, r.top() - 6), QPointF(x, r.bottom() + 6))
        if self.lo <= 0 <= self.hi:                  # trigger tick
            p.setPen(QPen(QColor(0, 140, 0), 2))
            x0 = self._x(0, r)
            p.drawLine(QPointF(x0, r.top() - 5), QPointF(x0, r.top()))
        xc = self._x(self.cur, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0))
        p.drawPolygon(QPolygonF([QPointF(xc - 5, r.top() - 8), QPointF(xc + 5, r.top() - 8), QPointF(xc, r.top() + 1)]))
        p.setPen(QColor(40, 40, 40))
        bottom = QRectF(0, r.bottom() + 6, self.width(), fm.height() + 2)
        p.drawText(bottom, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(self.mark_in))
        p.drawText(bottom, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(self.mark_out))
        p.end()

    def number_at(self, x: float) -> int:
        r = self._line()
        frac = min(max((x - r.left()) / max(r.width(), 1.0), 0.0), 1.0)
        return int(round(self.lo + frac * (self.hi - self.lo)))

    def mousePressEvent(self, e):
        if self.valid:
            self.scrubbed.emit(self.number_at(e.position().x()))

    def mouseMoveEvent(self, e):
        if self.valid and e.buttons() & Qt.MouseButton.LeftButton:
            self.scrubbed.emit(self.number_at(e.position().x()))


class _Lamp(QLabel):
    def __init__(self, text: str, color: str):
        super().__init__()
        self.text_, self.color = text, color
        self.set_on(False)

    def set_on(self, on: bool):
        self.on = on
        dot = f'<span style="color:{self.color if on else "#b0b0b0"}">&#9679;</span>'
        self.setText(f'{dot} {self.text_}')


class PanelStatusLine(QFrame):
    """Bottom line of an image panel: (o) Live (o) Play (o) Rec ... Current Time: ..."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.live = _Lamp('Live', '#1e5ad2')
        self.play = _Lamp('Play', '#1f9a2e')
        self.rec = _Lamp('Rec', '#d01818')
        self.info = QLabel()
        self.time = QLabel('Current Time: -')
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 1, 4, 1)
        for w in (self.live, self.play, self.rec):
            h.addWidget(w)
        h.addSpacing(8)
        h.addWidget(self.info, 1)
        h.addWidget(self.time)
        self.setStyleSheet('PanelStatusLine { background: #f0f0f0; border-top: 1px solid #c8c8c8; } '
                           'QLabel { color: #202020; }')
