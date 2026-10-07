"""Simple drawn icons with PCC's shapes (no image files). ``icon(name)`` returns a cached QIcon."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

S = 32   # drawing grid; Qt scales the pixmap to the button's icon size

YELLOW, DARK, GREY = QColor(232, 190, 60), QColor(60, 60, 60), QColor(120, 120, 120)
WHITE, RED, BLUE, GREEN = QColor(255, 255, 255), QColor(200, 30, 30), QColor(40, 90, 200), QColor(30, 140, 50)


def _poly(p: QPainter, pts, fill: QColor, pen: QColor | None = None):
    p.setPen(QPen(pen, 1.2) if pen else Qt.PenStyle.NoPen)
    p.setBrush(QBrush(fill))
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))


def _folder(p, star=False):
    _poly(p, [(3, 9), (12, 9), (14, 12), (29, 12), (29, 27), (3, 27)], YELLOW, DARK)
    if star:
        p.setPen(QPen(RED, 2))
        for a, b in (((22, 3), (22, 10)), ((18.5, 6.5), (25.5, 6.5)), ((19.5, 4), (24.5, 9)), ((24.5, 4), (19.5, 9))):
            p.drawLine(QPointF(*a), QPointF(*b))


def _text(p, rect, text, color=DARK, size=14, bold=True):
    f = QFont()
    f.setPixelSize(size)
    f.setBold(bold)
    p.setFont(f)
    p.setPen(color)
    p.drawText(QRectF(*rect), Qt.AlignmentFlag.AlignCenter, text)


def _camera(p, color=GREY):
    p.setPen(QPen(DARK, 1.2))
    p.setBrush(color)
    p.drawRoundedRect(QRectF(3, 10, 26, 16), 3, 3)
    p.drawRect(QRectF(9, 6, 8, 4))
    p.setBrush(WHITE)
    p.drawEllipse(QPointF(16, 18), 5, 5)


def _draw(name: str, p: QPainter):
    if name == 'cursor':
        p.setPen(QPen(DARK, 2))
        p.drawLine(16, 3, 16, 29)
        p.drawLine(3, 16, 29, 16)
    elif name == 'pan':
        p.setPen(QPen(DARK, 1.4))
        p.setBrush(QColor(245, 215, 180))
        path = QPainterPath()
        path.addRoundedRect(QRectF(8, 13, 17, 15), 5, 5)
        for x in (9, 13, 17, 21):
            path.addRoundedRect(QRectF(x, 5 if x in (13, 17) else 8, 4, 12), 2, 2)
        p.drawPath(path.simplified())
    elif name == 'zoom11':
        _text(p, (0, 0, S, S), '1:1', DARK, 15)
    elif name == 'zoomfit':
        p.setPen(QPen(DARK, 2))
        for (x0, y0), (x1, y1) in (((16, 3), (16, 29)), ((3, 16), (29, 16))):
            p.drawLine(x0, y0, x1, y1)
        for pts in ([(16, 2), (12, 7), (20, 7)], [(16, 30), (12, 25), (20, 25)],
                    [(2, 16), (7, 12), (7, 20)], [(30, 16), (25, 12), (25, 20)]):
            _poly(p, pts, DARK)
    elif name == 'imagetools':
        p.setPen(QPen(DARK, 1.2))
        p.setBrush(QColor(235, 225, 200))
        p.drawEllipse(QRectF(3, 5, 26, 22))
        for (x, y), c in zip(((10, 11), (17, 9), (23, 13), (11, 19)), (RED, YELLOW, BLUE, GREEN)):
            p.setBrush(c)
            p.drawEllipse(QPointF(x, y), 3, 3)
    elif name == 'snapshot':
        _camera(p)
    elif name == 'open':
        _poly(p, [(3, 8), (12, 8), (14, 11), (25, 11), (25, 14), (3, 14)], YELLOW, DARK)
        _poly(p, [(3, 27), (7, 14), (30, 14), (25, 27)], QColor(245, 210, 100), DARK)
    elif name == 'batch':
        for r in range(3):
            for c in range(2):
                p.setPen(QPen(DARK, 1))
                p.setBrush(YELLOW if c == 0 else QColor(160, 60, 160))
                p.drawRect(QRectF(4 + c * 16, 4 + r * 9, 7, 6))
        p.setPen(QPen(DARK, 1.5))
        for r in range(3):
            p.drawLine(12, 7 + r * 9, 19, 7 + r * 9)
    elif name in ('tile', 'autotile'):
        p.setPen(QPen(DARK, 1))
        p.setBrush(YELLOW)
        for x, y in ((3, 4), (17, 4), (3, 17), (17, 17)):
            p.drawRect(QRectF(x, y, 12, 11))
        if name == 'autotile':
            _text(p, (4, 6, 24, 24), 'A', QColor(0, 0, 0), 20)
    elif name == 'help':
        p.setPen(QPen(DARK, 1.2))
        p.setBrush(YELLOW)
        p.drawEllipse(QRectF(3, 3, 26, 26))
        _text(p, (3, 3, 26, 26), '?', DARK, 18)
    elif name == 'crosshair':
        p.setPen(QPen(YELLOW.darker(120), 5))
        p.drawLine(16, 3, 16, 29)
        p.drawLine(3, 16, 29, 16)
    elif name == 'grid':
        p.setPen(QPen(YELLOW.darker(120), 3))
        for v in (10, 22):
            p.drawLine(v, 3, v, 29)
            p.drawLine(3, v, 29, v)
    elif name == 'focus':
        p.setPen(QPen(DARK, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(5, 5, 22, 22))
        p.setPen(QPen(GREEN, 2.5))
        for (x0, y0), (x1, y1) in (((16, 2), (16, 11)), ((16, 21), (16, 30)), ((2, 16), (11, 16)), ((21, 16), (30, 16))):
            p.drawLine(x0, y0, x1, y1)
    elif name == 'zebra':
        p.setPen(QPen(DARK, 1))
        p.setBrush(WHITE)
        p.drawRect(QRectF(4, 4, 24, 24))
        p.setClipRect(QRectF(4, 4, 24, 24))
        p.setPen(QPen(RED, 3))
        for k in (-12, -4, 4, 12):
            p.drawLine(QPointF(4 + k, 28), QPointF(28 + k, 4))
        p.setClipping(False)
    elif name == 'crop':
        p.setPen(QPen(DARK, 2.5))
        p.drawLine(9, 3, 9, 23)
        p.drawLine(9, 23, 29, 23)
        p.drawLine(3, 9, 23, 9)
        p.drawLine(23, 9, 23, 29)
    elif name == 'measure':
        p.setPen(QPen(QColor(200, 0, 200), 2))
        p.drawLine(5, 26, 27, 6)
        p.setPen(QPen(DARK, 2))
        for x, y in ((5, 26), (27, 6)):
            p.drawLine(x - 3, y, x + 3, y)
            p.drawLine(x, y - 3, x, y + 3)
    elif name == 'discover':
        p.setPen(QPen(DARK, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(4, 4, 16, 16))
        p.drawLine(18, 18, 28, 28)
    elif name == 'addsim':
        _text(p, (8, 6, 24, 26), 'S', DARK, 24)
        p.setPen(QPen(QColor(220, 180, 0), 3))
        p.drawLine(8, 3, 8, 13)
        p.drawLine(3, 8, 13, 8)
    elif name == 'remove':
        p.setPen(QPen(RED, 3))
        p.drawLine(8, 8, 24, 24)
        p.drawLine(24, 8, 8, 24)
    elif name == 'connectip':
        p.setPen(QPen(DARK, 1.2))
        p.setBrush(QColor(50, 60, 90))
        p.drawRect(QRectF(3, 5, 18, 13))
        p.drawRect(QRectF(11, 12, 18, 13))
        p.setBrush(QColor(110, 160, 230))
        p.drawRect(QRectF(13, 14, 14, 9))
    elif name == 'wrench':
        p.setPen(QPen(GREY.darker(130), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(9, 25, 22, 10)
        p.setPen(QPen(GREY.darker(130), 2))
        p.setBrush(QColor(200, 200, 200))
        p.drawEllipse(QPointF(23, 8), 5, 5)
    elif name == 'gear':
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(DARK)
        p.translate(16, 16)
        for _ in range(8):
            p.drawRect(QRectF(-2.5, -14, 5, 7))
            p.rotate(45)
        p.drawEllipse(QPointF(0, 0), 10, 10)
        p.setBrush(WHITE)
        p.drawEllipse(QPointF(0, 0), 4, 4)
    elif name == 'network':
        p.setPen(QPen(DARK, 2))
        p.drawLine(9, 22, 16, 10)
        p.drawLine(16, 10, 24, 22)
        for (x, y), c in (((16, 9), BLUE), ((8, 23), DARK), ((24, 23), RED)):
            p.setBrush(c)
            p.setPen(QPen(DARK, 1))
            p.drawEllipse(QPointF(x, y), 5, 5)
    elif name == 'folder':
        _folder(p)
    elif name == 'camera':
        _camera(p, QColor(90, 120, 170))
    elif name == 'cine':
        p.setPen(QPen(DARK, 1))
        p.setBrush(QColor(60, 170, 70))
        p.drawRect(QRectF(5, 5, 22, 22))
        p.setBrush(WHITE)
        for y in (8, 14, 20):
            p.drawRect(QRectF(7, y, 3, 3))
            p.drawRect(QRectF(22, y, 3, 3))
    # transport glyphs (white on the green buttons)
    elif name == 'rewind':
        _poly(p, [(24, 6), (24, 26), (8, 16)], WHITE)
    elif name == 'play':
        _poly(p, [(8, 6), (8, 26), (24, 16)], WHITE)
    elif name == 'pause':
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(WHITE)
        p.drawRect(QRectF(8, 6, 6, 20))
        p.drawRect(QRectF(18, 6, 6, 20))
    elif name == 'fastrew':
        _poly(p, [(16, 7), (16, 25), (3, 16)], WHITE)
        _poly(p, [(29, 7), (29, 25), (16, 16)], WHITE)
    elif name == 'fastfwd':
        _poly(p, [(3, 7), (3, 25), (16, 16)], WHITE)
        _poly(p, [(16, 7), (16, 25), (29, 16)], WHITE)
    elif name == 'stepback':
        _poly(p, [(19, 7), (19, 25), (6, 16)], WHITE)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(WHITE)
        p.drawRect(QRectF(21, 7, 5, 18))
    elif name == 'stepfwd':
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(WHITE)
        p.drawRect(QRectF(6, 7, 5, 18))
        _poly(p, [(13, 7), (13, 25), (26, 16)], WHITE)
    elif name == 'jumpstart':
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(DARK)
        p.drawRect(QRectF(7, 8, 3, 16))
        _poly(p, [(25, 8), (25, 24), (11, 16)], DARK)
    elif name == 'jumpend':
        _poly(p, [(7, 8), (7, 24), (21, 16)], DARK)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(DARK)
        p.drawRect(QRectF(22, 8, 3, 16))
    else:
        raise KeyError(name)


@lru_cache(maxsize=None)
def icon(name: str) -> QIcon:
    pm = QPixmap(S, S)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _draw(name, p)
    p.end()
    return QIcon(pm)
