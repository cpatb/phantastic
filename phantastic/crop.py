"""Crop rectangles for save and export: ``(x, y, w, h)`` in stored-array coordinates.

x, y are 0-based column and row of the top-left pixel, with rows counted top-down as
:meth:`phantastic.cine.CineReader.read` returns them (whatever the storage order on disk).
A crop is validated against the image size and never clamped: a rectangle that does not fit
is an error. Cropping selects pixels; it never changes a value.

PCC's Image Tools crop (manual p.46) is stored as metadata in a Cine Raw file and only "baked in"
on conversion; Phantastic's crop writes the smaller image and records where it came from.
"""
from __future__ import annotations

import numbers

import numpy as np

Crop = tuple[int, int, int, int]


PACKED_WIDTH_MULTIPLE = {'packed10': 4, 'packed12L': 2}   # pixels per packed group (5 B / 3 B)


def check_crop(crop, width: int, height: int, packing: str | None = None) -> Crop | None:
    """Validated ``(x, y, w, h)`` (ints), or None for no crop. Raises ValueError otherwise.

    For a packed cine output (``packing`` 'packed10' / 'packed12L') the width must fill whole
    packed groups: Phantastic's writer packs each row in 4- (P10) or 2-pixel (P12L) groups, and how a
    row ending mid-group is laid out in a real packed file is not known (cameras use widths in steps
    of 16), so such a crop is refused rather than written in a guessed layout.
    """
    if crop is None:
        return None
    vals = tuple(crop)
    if len(vals) != 4:
        raise ValueError(f'crop must be (x, y, w, h), got {crop!r}')
    if not all(isinstance(v, numbers.Integral) or (isinstance(v, float) and v.is_integer()) for v in vals):
        raise ValueError(f'crop values must be whole pixels, got {crop!r}')
    x, y, w, h = (int(v) for v in vals)
    if w < 1 or h < 1:
        raise ValueError(f'crop width and height must be >= 1, got {w}x{h}')
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise ValueError(f'crop x={x} y={y} w={w} h={h} does not fit the {width}x{height} image '
                         f'(needs x+w <= {width}, y+h <= {height})')
    m = PACKED_WIDTH_MULTIPLE.get(packing or '', 1)
    if w % m:
        raise ValueError(f'a {packing} cine needs a crop width that is a multiple of {m} (got {w}); '
                         'or export TIFF, which holds any width')
    return x, y, w, h


def parse_crop(text: str) -> Crop:
    """'x,y,w,h' -> (x, y, w, h) (command line)."""
    parts = [p.strip() for p in str(text).split(',')]
    if len(parts) != 4:
        raise ValueError(f"crop must be 'x,y,w,h', got {text!r}")
    try:
        return tuple(int(p) for p in parts)          # type: ignore[return-value]
    except ValueError:
        raise ValueError(f"crop must be four integers 'x,y,w,h', got {text!r}") from None


def crop_image(img: np.ndarray, crop: Crop | None) -> np.ndarray:
    """The rectangle of a top-down (H, W) or (H, W, C) image (a contiguous copy; values unchanged)."""
    if crop is None:
        return img
    x, y, w, h = crop
    return np.ascontiguousarray(img[y:y + h, x:x + w])


def crop_note(crop: Crop, width: int, height: int) -> str:
    """The sentence written into a file's Description / metadata."""
    x, y, w, h = crop
    return (f'Cropped from {width}x{height} at {x},{y} to {w}x{h} '
            '(x, y = 0-based column, row of the top-left pixel; rows counted top-down).')


def crop_dict(crop: Crop | None, width: int, height: int) -> dict | None:
    """JSON metadata for a crop (None when not cropped)."""
    if crop is None:
        return None
    x, y, w, h = crop
    return {'x': x, 'y': y, 'w': w, 'h': h, 'source_width': width, 'source_height': height,
            'convention': '0-based, rows top-down as CineReader.read returns them'}
