"""Pixels the camera flags as defective, and PCC's fill-in for them.

Measured on a Phantom v2512 (s/n 21598, firmware 23070, 2026-10-07): in corrected P16 a fixed set of
pixels reads exactly 0xFF00 in every frame (128 pixels on a regular grid in a 256x256 window); no
other pixel reached that value. Uncorrected P16R has none, so the camera's correction inserts them.
PCC's saved cine holds, at exactly those pixels, the mean of the 8 neighbours: on 25 600 flagged
pixels (200 frames), round-half-down of the 8-neighbour mean of the 12-bit values reproduced PCC
exactly on 92.1 % (mean |difference| 0.08 count); every other pixel was identical. That the flag
marks defects, and PCC's exact arithmetic, are inferences from those files, not documentation.
"""
from __future__ import annotations

import numpy as np

FLAG_P16 = 0xFF00          # value of a flagged pixel in P16 (12-bit 0xFF0 x 16)
_NEIGHBOURS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def convert(frame: np.ndarray, fmt: str, as_12bit: bool, fill_flags: bool) -> tuple[np.ndarray, int]:
    """One downloaded frame -> stored values: optional 12-bit (value >> 4), then optional fill-in.

    The fill-in runs on the whole frame (before any crop, so edge pixels keep all neighbours) and only
    for corrected P16, the only format seen to carry flags. Returns (values, pixels filled).
    """
    out = frame >> 4 if as_12bit else frame
    if not (fill_flags and fmt == 'P16'):
        return out, 0
    m = flagged(out, FLAG_P16 >> 4 if as_12bit else FLAG_P16)   # 12-bit: the domain PCC was measured in
    n = int(m.sum())
    return (fill(out, m) if n else out), n


def flagged(frame: np.ndarray, flag: int) -> np.ndarray:
    """Pixels equal to ``flag`` whose neighbours are all below it.

    Isolation keeps a genuinely saturated region (which could also clip at the flag value) from being
    'repaired'; a flagged pixel next to saturation is then left as it is.
    """
    f = np.asarray(frame)
    hit = f == flag
    if not hit.any():
        return hit
    p = np.pad(f, 1, mode='constant', constant_values=0)   # off-image neighbours never block isolation
    h, w = f.shape
    iso = hit.copy()
    for dy, dx in _NEIGHBOURS:
        nb = p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        iso &= nb < flag
    return iso


def fill(frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Copy of ``frame`` with masked pixels set to the 8-neighbour mean, rounded half down.

    Neighbours that are themselves masked or off the image are left out of the mean.
    """
    f = np.asarray(frame)
    out = f.copy()
    if not mask.any():
        return out
    for r, c in zip(*np.nonzero(mask)):
        vals = [int(f[r + dy, c + dx]) for dy, dx in _NEIGHBOURS
                if 0 <= r + dy < f.shape[0] and 0 <= c + dx < f.shape[1] and not mask[r + dy, c + dx]]
        if vals:
            out[r, c] = int(np.ceil(sum(vals) / len(vals) - 0.5))   # round half down
    return out
