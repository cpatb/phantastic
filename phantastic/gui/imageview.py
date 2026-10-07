"""Image display: raw arrays -> 8-bit display copies, a zoomable view, cursor -> pixel mapping.

Display mapping is for the screen only. The raw frame is never modified; the viewer reads pixel
values for the cursor readout from the raw array, not from the display copy.
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QSizePolicy, QWidget

# ImageJ's "Auto" contrast convention: 0.35 % of pixels saturated in total, half at each end.
AUTO_SATURATED_PERCENT = 0.35
GRID_DIVISIONS = 8     # overlay grid: 8 x 8 cells over the image


def auto_range(img: np.ndarray, saturated: float = AUTO_SATURATED_PERCENT) -> tuple[int, int]:
    """Display limits leaving ``saturated`` % of the pixels of THIS frame clipped (half low, half high)."""
    lo, hi = np.percentile(img, [saturated / 2, 100 - saturated / 2])
    lo, hi = int(math.floor(lo)), int(math.ceil(hi))
    return (lo, lo + 1) if hi <= lo else (lo, hi)


def to_display8(img: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Linear map [lo, hi] -> [0, 255] into a NEW uint8 array (display only)."""
    if hi <= lo:
        hi = lo + 1
    a = (img.astype(np.float32) - np.float32(lo)) * np.float32(255.0 / (hi - lo))
    return np.rint(np.clip(a, 0, 255)).astype(np.uint8)


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

    Flip H / Flip V and the cross / grid overlays are display only. ``hovered(x, y)`` reports the
    pixel of the UNFLIPPED image array under the cursor (column, row; row 0 = top), or (-1, -1)
    outside the image. ``mode`` is 'cursor' (crosshair) or 'pan' (drag to move a zoomed image).
    """
    hovered = Signal(int, int)
    zoom_changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img: QImage | None = None
        self.placeholder = 'No image'
        self.zoom: float | None = None          # None = fit to the panel
        self.offset = QPointF(0, 0)             # pan, in widget pixels, when zoomed
        self.flip_h = self.flip_v = False
        self.show_cross = self.show_grid = False
        self.mode = 'cursor'
        self._drag_from: QPointF | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_array(self, a8: np.ndarray):
        self._img = qimage_from_uint8(a8)
        self.update()

    def clear(self):
        self._img = None
        self.update()

    def display_image(self) -> QImage | None:
        """The image as displayed (flips applied), at native size."""
        if self._img is None:
            return None
        return self._img.transformed(QTransform().scale(-1 if self.flip_h else 1, -1 if self.flip_v else 1))

    @property
    def image_size(self) -> tuple[int, int] | None:
        return None if self._img is None else (self._img.width(), self._img.height())

    # ------------------------------------------------------------------ zoom / pan
    def scale(self) -> float:
        if self._img is None:
            return 1.0
        if self.zoom is None:
            return min(self.width() / self._img.width(), self.height() / self._img.height())
        return self.zoom

    def set_zoom(self, z: float | None):
        self.zoom = None if z is None else min(max(float(z), ZOOM_MIN), ZOOM_MAX)
        if z is None:
            self.offset = QPointF(0, 0)
        self.update()
        self.zoom_changed.emit()
        self.refresh_hover()

    def refresh_hover(self):
        """Re-report the pixel under a resting mouse after the mapping changed (zoom, pan, flip)."""
        pos = self.mapFromGlobal(QCursor.pos())
        if self.underMouse() and self.rect().contains(pos):
            xy = self.image_coords(QPointF(pos))
            self.hovered.emit(*(xy if xy else (-1, -1)))

    def set_mode(self, mode: str):
        self.mode = mode
        self.setCursor(Qt.CursorShape.OpenHandCursor if mode == 'pan' else Qt.CursorShape.CrossCursor)

    def target_rect(self) -> QRectF:
        if self._img is None:
            return QRectF()
        s = self.scale()
        w, h = self._img.width() * s, self._img.height() * s
        off = self.offset if self.zoom is not None else QPointF(0, 0)
        return QRectF((self.width() - w) / 2 + off.x(), (self.height() - h) / 2 + off.y(), w, h)

    def image_coords(self, pos: QPointF) -> tuple[int, int] | None:
        if self._img is None:
            return None
        r = self.target_rect()
        if r.width() <= 0:
            return None
        s = r.width() / self._img.width()
        x = math.floor((pos.x() - r.x()) / s)
        y = math.floor((pos.y() - r.y()) / s)
        w, h = self._img.width(), self._img.height()
        if not (0 <= x < w and 0 <= y < h):
            return None
        return (w - 1 - x if self.flip_h else x), (h - 1 - y if self.flip_v else y)

    def widget_point(self, x: int, y: int) -> QPointF:
        """Widget position of the centre of image-array pixel (x, y)."""
        r = self.target_rect()
        s = r.width() / self._img.width()
        dx = self._img.width() - 1 - x if self.flip_h else x
        dy = self._img.height() - 1 - y if self.flip_v else y
        return QPointF(r.x() + (dx + 0.5) * s, r.y() + (dy + 0.5) * s)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(170, 170, 170))      # PCC's grey MDI background
        if self._img is None:
            p.setPen(QColor(70, 70, 70))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            p.end()
            return
        r = self.target_rect()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.save()
        p.translate(r.center())
        p.scale(-1 if self.flip_h else 1, -1 if self.flip_v else 1)
        p.drawImage(QRectF(-r.width() / 2, -r.height() / 2, r.width(), r.height()), self._img)
        p.restore()
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
        p.end()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, event):
        if self.mode == 'pan' and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            if self.zoom is None:                 # panning starts from the fitted view
                self.zoom = self.scale()
                self.zoom_changed.emit()
            self.offset += event.position() - self._drag_from
            self._drag_from = event.position()
            self.update()
        xy = self.image_coords(event.position())
        self.hovered.emit(*(xy if xy else (-1, -1)))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hovered.emit(-1, -1)
        super().leaveEvent(event)
