"""Image display: raw arrays -> 8-bit display copies, a zoomable view, cursor -> pixel mapping.

Display mapping is for the screen only. The raw frame is never modified; the viewer reads pixel
values for the cursor readout from the raw array, not from the display copy.

Coordinates: "stored" means the array as ``CineReader.read`` returns it (or as the camera sent
it): x = column, y = row, 0-based, row 0 at the top. Rotation and flips are applied to the
display copy only; every coordinate the view reports (hover, clicks, rectangles) is a stored one.
"""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

# ImageJ's "Auto" contrast convention: 0.35 % of pixels saturated in total, half at each end.
AUTO_SATURATED_PERCENT = 0.35
GRID_DIVISIONS = 8     # overlay grid: 8 x 8 cells over the image

# Display curve ranges. Gamma, Gain and Toe use PCC's ranges and defaults' meaning (p.43); the
# formulas are Phantastic's own (PCC's are unpublished), written out in ``display_curve``.
GAMMA_RANGE = (0.1, 10.0)
GAIN_RANGE = (0.1, 10.0)
TOE_RANGE = (0.1, 2.0)
BRIGHTNESS_RANGE = (-1.0, 1.0)   # fraction of full display scale (PCC's -10..10 unit is unpublished)
IDENTITY_CURVE = dict(gamma=1.0, gain=1.0, brightness=0.0, toe=1.0)

ZEBRA_PERIOD = 8                  # zebra stripes: 8 stored pixels per light+dark pair (PCC p.40, p.161)
ZEBRA_RGB = (255, 0, 0)
FOCUS_NORM_PERCENTILE = 99.5      # focus assist: the edge strength at this percentile is fully coloured
FOCUS_RGB = (0, 255, 0)


def auto_range(img: np.ndarray, saturated: float = AUTO_SATURATED_PERCENT) -> tuple[int, int]:
    """Display limits leaving ``saturated`` % of the pixels of THIS frame clipped (half low, half high)."""
    lo, hi = np.percentile(img, [saturated / 2, 100 - saturated / 2])
    lo, hi = int(math.floor(lo)), int(math.ceil(hi))
    return (lo, lo + 1) if hi <= lo else (lo, hi)


def _curve(v: np.ndarray, lo: float, hi: float, gamma: float, gain: float, brightness: float,
           toe: float) -> np.ndarray:
    """float64 raw values -> uint8. See ``display_curve``."""
    if hi <= lo:
        hi = lo + 1
    x = np.clip(gain * ((v - lo) / (hi - lo)) + brightness, 0.0, 1.0)
    if gamma != 1.0 or toe != 1.0:
        p = 1.0 / gamma if toe == 1.0 else (1.0 / gamma) * np.power(toe, 1.0 - x)
        x = np.power(x, p)
    return np.rint(255.0 * x).astype(np.uint8)


@lru_cache(maxsize=16)
def _curve_lut(n: int, lo: float, hi: float, gamma: float, gain: float, brightness: float, toe: float) -> np.ndarray:
    lut = _curve(np.arange(n, dtype=np.float64), lo, hi, gamma, gain, brightness, toe)
    lut.setflags(write=False)
    return lut


def display_curve(raw: np.ndarray, lo: float, hi: float, gamma: float = 1.0, gain: float = 1.0,
                  brightness: float = 0.0, toe: float = 1.0) -> np.ndarray:
    """Raw values -> a NEW uint8 display array (screen only; ``raw`` is never modified).

    x = clip(gain * (raw - lo) / (hi - lo) + brightness, 0, 1)      window, then gain about black
    y = x ** ((1 / gamma) * toe ** (1 - x))                          gamma as in video: 2.222 brightens
    out = rint(255 * y)

    Gamma follows PCC's sense (default 2.222 = 1/0.45, the video encoding exponent, p.43), so
    gamma > 1 lifts mid-tones. Toe scales the exponent most at black and not at all at white:
    toe < 1 lifts shadows, toe = 1 is off. Identity: gamma = gain = toe = 1, brightness = 0.
    Unsigned data up to 16 bit go through a cached look-up table (exactly the same arithmetic).
    """
    if raw.dtype.kind == 'u' and raw.dtype.itemsize <= 2:
        lut = _curve_lut(1 << (8 * raw.dtype.itemsize), float(lo), float(hi), float(gamma), float(gain),
                         float(brightness), float(toe))
        return lut[raw]
    return _curve(raw.astype(np.float64), float(lo), float(hi), float(gamma), float(gain), float(brightness),
                  float(toe))


def to_display8(img: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Linear map [lo, hi] -> [0, 255] into a NEW uint8 array (display only)."""
    return display_curve(img, lo, hi)


def zebra_overlay(a8: np.ndarray, raw: np.ndarray, level: float) -> np.ndarray:
    """RGB copy of ``a8`` with diagonal stripes on pixels whose raw value is >= ``level`` (any channel)."""
    sat = raw >= level
    if sat.ndim == 3:
        sat = sat.any(axis=2)
    rgb = a8 if a8.ndim == 3 else np.repeat(a8[:, :, None], 3, axis=2)
    if not sat.any():
        return rgb
    h, w = sat.shape
    diag = np.add.outer(np.arange(h, dtype=np.int32), np.arange(w, dtype=np.int32))
    stripe = (diag // (ZEBRA_PERIOD // 2)) % 2 == 0
    rgb = rgb.copy()
    rgb[sat & stripe] = ZEBRA_RGB
    return rgb


def focus_overlay(a8: np.ndarray, raw: np.ndarray) -> np.ndarray:
    """RGB copy of ``a8`` with the Sobel gradient magnitude of ``raw`` blended in as colour (PCC p.17, p.39)."""
    from scipy import ndimage
    g = raw.astype(np.float32)
    if g.ndim == 3:
        g = g.mean(axis=2)
    mag = np.hypot(ndimage.sobel(g, axis=1), ndimage.sobel(g, axis=0))
    rgb = a8 if a8.ndim == 3 else np.repeat(a8[:, :, None], 3, axis=2)
    norm = float(np.percentile(mag, FOCUS_NORM_PERCENTILE))
    if norm <= 0:
        return rgb.copy()
    w = np.minimum(mag * np.float32(255.0 / norm), 255).astype(np.uint32)[:, :, None]   # blend weight /255
    out = rgb.astype(np.uint32) * (255 - w) + np.asarray(FOCUS_RGB, np.uint32) * w
    return ((out + 127) // 255).astype(np.uint8)


def qimage_from_uint8(a: np.ndarray) -> QImage:
    """QImage owning a copy of a (H, W) or (H, W, 3) uint8 array."""
    if a.dtype != np.uint8:
        raise TypeError(f'expected uint8, got {a.dtype}')
    a = np.ascontiguousarray(a)
    h, w = a.shape[:2]
    if a.ndim == 2:
        img = QImage(a.data, w, h, w, QImage.Format.Format_Grayscale8)
    elif a.ndim == 3 and a.shape[2] == 3:
        img = QImage(a.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    else:
        raise ValueError(f'unsupported shape {a.shape}')
    return img.copy()   # detach from the numpy buffer


ZOOM_MIN, ZOOM_MAX = 1 / 16, 16.0    # PCC's zoom range (p.16)


class ImageView(QWidget):
    """Shows one image, fitted (default) or at a fixed zoom, nearest-neighbour so pixels stay pixels.

    Rotation (``rot``: quarter turns clockwise, PCC p.46), Flip H / Flip V and the overlays are
    display only. The display is rotate-then-flip: Flip H always mirrors left-right ON SCREEN.
    ``hovered(x, y)`` reports the pixel of the STORED array under the cursor (column, row; row 0 =
    top), or (-1, -1) outside the image. ``mode``: 'cursor' (crosshair), 'pan' (drag to move a
    zoomed image), 'crop' (drag a rectangle: ``rect_drawn(x, y, w, h)``) or 'measure' (each left
    click emits ``clicked(x, y)``). All of these are stored-array coordinates.
    """
    hovered = Signal(int, int)
    clicked = Signal(int, int)
    rect_drawn = Signal(int, int, int, int)
    zoom_changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._a8: np.ndarray | None = None     # display copy, stored orientation
        self._img: QImage | None = None        # display copy, rotated and flipped
        self._flip_h = self._flip_v = False
        self._rot = 0
        self.placeholder = 'No image'
        self.zoom: float | None = None          # None = fit to the panel
        self.offset = QPointF(0, 0)             # pan, in widget pixels, when zoomed
        self.show_cross = self.show_grid = False
        self.crop: tuple[int, int, int, int] | None = None     # outline, stored coordinates
        self.marks: list[tuple] = []            # measurement graphics: ('point', x, y) / ('line', x1, y1, x2, y2)
        self.mode = 'cursor'
        self._drag_from: QPointF | None = None
        self._crop_from: tuple[int, int] | None = None
        self._band: tuple[int, int, int, int] | None = None     # rubber band while dragging
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------ geometry (display only)
    def _set_geom(self, attr: str, value):
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            self._img = None
            self.update()

    flip_h = property(lambda self: self._flip_h, lambda self, on: self._set_geom('_flip_h', bool(on)))
    flip_v = property(lambda self: self._flip_v, lambda self, on: self._set_geom('_flip_v', bool(on)))
    rot = property(lambda self: self._rot, lambda self, k: self._set_geom('_rot', int(k) % 4))

    def set_array(self, a8: np.ndarray):
        if a8.dtype != np.uint8 or a8.ndim not in (2, 3):
            raise ValueError(f'expected a (H, W) or (H, W, 3) uint8 array, got {a8.dtype} {a8.shape}')
        self._a8 = a8
        self._img = None
        self.update()

    def clear(self):
        self._a8 = self._img = None
        self.update()

    def display_array(self) -> np.ndarray | None:
        """The display copy rotated and flipped, as on screen (a view, do not modify)."""
        if self._a8 is None:
            return None
        d = np.rot90(self._a8, -self._rot) if self._rot else self._a8    # np.rot90(k=-1) is clockwise
        if self._flip_h:
            d = d[:, ::-1]
        if self._flip_v:
            d = d[::-1]
        return d

    def display_image(self) -> QImage | None:
        """The image as displayed (rotation and flips applied), at native size."""
        if self._a8 is None:
            return None
        if self._img is None:
            self._img = qimage_from_uint8(self.display_array())
        return self._img

    @property
    def image_size(self) -> tuple[int, int] | None:
        """(width, height) of the STORED array."""
        return None if self._a8 is None else (self._a8.shape[1], self._a8.shape[0])

    @property
    def display_size(self) -> tuple[int, int] | None:
        s = self.image_size
        return None if s is None else (s[::-1] if self._rot % 2 else s)

    def to_display(self, u: float, v: float) -> tuple[float, float]:
        """Stored continuous coordinates (pixel (x, y) spans [x, x+1) x [y, y+1)) -> display ones."""
        w, h = self.image_size
        for _ in range(self._rot):              # one clockwise quarter turn: (u, v) -> (h - v, u)
            u, v, w, h = h - v, u, h, w
        if self._flip_h:
            u = w - u
        if self._flip_v:
            v = h - v
        return u, v

    def to_stored(self, u: float, v: float) -> tuple[float, float]:
        """Inverse of ``to_display``."""
        w, h = self.display_size
        if self._flip_h:
            u = w - u
        if self._flip_v:
            v = h - v
        for _ in range(self._rot):              # undo a clockwise quarter turn: (a, b) -> (b, w - a)
            u, v, w, h = v, w - u, h, w
        return u, v

    # ------------------------------------------------------------------ zoom / pan
    def scale(self) -> float:
        ds = self.display_size
        if ds is None:
            return 1.0
        if self.zoom is None:
            return min(self.width() / ds[0], self.height() / ds[1])
        return self.zoom

    def set_zoom(self, z: float | None):
        self.zoom = None if z is None else min(max(float(z), ZOOM_MIN), ZOOM_MAX)
        if z is None:
            self.offset = QPointF(0, 0)
        self.update()
        self.zoom_changed.emit()
        self.refresh_hover()

    def refresh_hover(self):
        """Re-report the pixel under a resting mouse after the mapping changed (zoom, pan, flip, rotation)."""
        pos = self.mapFromGlobal(QCursor.pos())
        if self.underMouse() and self.rect().contains(pos):
            xy = self.image_coords(QPointF(pos))
            self.hovered.emit(*(xy if xy else (-1, -1)))

    def set_mode(self, mode: str):
        self.mode = mode
        self._crop_from = self._band = None
        self.setCursor(Qt.CursorShape.OpenHandCursor if mode == 'pan' else Qt.CursorShape.CrossCursor)

    def target_rect(self) -> QRectF:
        ds = self.display_size
        if ds is None:
            return QRectF()
        s = self.scale()
        w, h = ds[0] * s, ds[1] * s
        off = self.offset if self.zoom is not None else QPointF(0, 0)
        return QRectF((self.width() - w) / 2 + off.x(), (self.height() - h) / 2 + off.y(), w, h)

    def image_coords(self, pos: QPointF, clamp: bool = False) -> tuple[int, int] | None:
        """Stored pixel under widget position ``pos``; None outside the image unless ``clamp``."""
        if self._a8 is None:
            return None
        r = self.target_rect()
        if r.width() <= 0:
            return None
        s = r.width() / self.display_size[0]
        dw, dh = self.display_size
        u, v = (pos.x() - r.x()) / s, (pos.y() - r.y()) / s
        if clamp:
            u, v = min(max(u, 0.0), dw - 1e-6), min(max(v, 0.0), dh - 1e-6)
        elif not (0 <= u < dw and 0 <= v < dh):
            return None
        # floor in DISPLAY space, then map that pixel's centre: a flip or quarter turn is a
        # reflection, so flooring after the mapping would put pixel edges in the neighbour
        su, sv = self.to_stored(math.floor(u) + 0.5, math.floor(v) + 0.5)
        w, h = self.image_size
        x, y = min(max(math.floor(su), 0), w - 1), min(max(math.floor(sv), 0), h - 1)
        return x, y

    def widget_at(self, u: float, v: float) -> QPointF:
        """Widget position of stored continuous coordinates (u, v)."""
        r = self.target_rect()
        s = r.width() / self.display_size[0]
        du, dv = self.to_display(u, v)
        return QPointF(r.x() + du * s, r.y() + dv * s)

    def widget_point(self, x: int, y: int) -> QPointF:
        """Widget position of the centre of stored pixel (x, y)."""
        return self.widget_at(x + 0.5, y + 0.5)

    def widget_rect(self, x: int, y: int, w: int, h: int) -> QRectF:
        """Widget rectangle covering stored pixels [x, x+w) x [y, y+h)."""
        return QRectF(self.widget_at(x, y), self.widget_at(x + w, y + h)).normalized()

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(170, 170, 170))      # PCC's grey MDI background
        img = self.display_image()
        if img is None:
            p.setPen(QColor(70, 70, 70))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            p.end()
            return
        r = self.target_rect()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(r, img)
        if self.show_grid:
            p.setPen(QPen(QColor(255, 220, 0, 170), 1))
            for k in range(1, GRID_DIVISIONS):
                x = r.left() + r.width() * k / GRID_DIVISIONS
                y = r.top() + r.height() * k / GRID_DIVISIONS
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        if self.show_cross:
            p.setPen(QPen(QColor(255, 60, 60), 1))
            c = r.center()
            p.drawLine(QPointF(c.x(), r.top()), QPointF(c.x(), r.bottom()))
            p.drawLine(QPointF(r.left(), c.y()), QPointF(r.right(), c.y()))
        for rect in (self.crop, self._band):
            if rect is not None:
                p.setPen(QPen(QColor(0, 200, 255), 1.5, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(self.widget_rect(*rect))
        if self.marks:
            p.setPen(QPen(QColor(230, 0, 230), 1.5))       # magenta, as PCC draws measurements (p.85)
            for m in self.marks:
                if m[0] == 'line':
                    p.drawLine(self.widget_point(m[1], m[2]), self.widget_point(m[3], m[4]))
                else:
                    c = self.widget_point(m[1], m[2])
                    p.drawLine(c - QPointF(5, 0), c + QPointF(5, 0))
                    p.drawLine(c - QPointF(0, 5), c + QPointF(0, 5))
        p.end()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.mode == 'pan':
                self._drag_from = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            elif self.mode == 'crop':
                self._crop_from = self.image_coords(event.position())
            elif self.mode == 'measure':
                xy = self.image_coords(event.position())
                if xy is not None:
                    self.clicked.emit(*xy)
        super().mousePressEvent(event)

    def _crop_drag(self, pos: QPointF) -> tuple[int, int, int, int] | None:
        if self._crop_from is None:
            return None
        x1, y1 = self.image_coords(pos, clamp=True)
        x0, y0 = self._crop_from
        return min(x0, x1), min(y0, y1), abs(x1 - x0) + 1, abs(y1 - y0) + 1

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        rect = self._crop_drag(event.position())
        if rect is not None:
            self._crop_from = self._band = None
            self.update()
            if (rect[2], rect[3]) != (1, 1):        # a click without a drag draws nothing
                self.rect_drawn.emit(*rect)
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            if self.zoom is None:                 # panning starts from the fitted view
                self.zoom = self.scale()
                self.zoom_changed.emit()
            self.offset += event.position() - self._drag_from
            self._drag_from = event.position()
            self.update()
        rect = self._crop_drag(event.position())
        if rect is not None:                      # rubber band
            self._band = rect
            self.update()
        xy = self.image_coords(event.position())
        self.hovered.emit(*(xy if xy else (-1, -1)))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hovered.emit(-1, -1)
        super().leaveEvent(event)
