"""Two-point image measurements (PCC p.81-85, simplified): scale calibration, distance, angle, speed.

Points are STORED-array pixel indices (x = column, y = row, 0-based, row 0 at the top), the same
coordinates as ``CineReader.read`` and the status-bar readout minus one. Pixels are assumed square.

Angle: from the image's +x axis (towards higher columns), counter-clockwise positive as the stored
image appears with row 0 at the top, in (-180, 180] degrees. So a point 3 columns right and 4 rows
UP of the first is at +53.13 deg. Speed = distance / (t2 - t1) from the two images' own time stamps
(seconds from the trigger, ``CineReader.relative_times``), never from image number / frame rate.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass


def distance_angle(p1: tuple[float, float], p2: tuple[float, float]) -> tuple[float, float]:
    """(distance in pixels, angle in degrees) of the segment p1 -> p2."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    return math.hypot(dx, dy), math.degrees(math.atan2(-dy, dx))   # rows grow downwards: -dy is up


def px_per_unit(p1: tuple[float, float], p2: tuple[float, float], length: float) -> float:
    """Scale from two points a known ``length`` apart (PCC's 'Calibrate', p.81)."""
    d, _ = distance_angle(p1, p2)
    if not (length > 0 and d > 0):
        raise ValueError(f'calibration needs two distinct points and a positive length (got {d} px, {length})')
    return d / length


@dataclass
class Measurement:
    """One 2-point measurement. ``image``/``t`` are None for a live frame; ``t`` is seconds from trigger."""
    p1: tuple[int, int]
    p2: tuple[int, int]
    image1: int | None = None
    image2: int | None = None
    t1: float | None = None
    t2: float | None = None
    times_synthesized: bool = False     # True when a source has no time stamps (then t = number / rate)

    @property
    def distance_px(self) -> float:
        return distance_angle(self.p1, self.p2)[0]

    @property
    def angle_deg(self) -> float:
        return distance_angle(self.p1, self.p2)[1]

    @property
    def dt(self) -> float | None:
        """t2 - t1 when the points are on different images with times, else None."""
        if self.t1 is None or self.t2 is None or self.image1 == self.image2 or self.t2 == self.t1:
            return None
        return self.t2 - self.t1

    def speed(self, scale: float | None = None) -> float | None:
        """Distance / |dt| in units per second (pixels per second when ``scale`` is None); a speed,
        so never negative, also when point 2 was clicked on an earlier image (``dt`` keeps the sign)."""
        dt = self.dt
        if dt is None:
            return None
        return self.distance_px / (scale or 1.0) / abs(dt)


COLUMNS = ('n', 'image1', 'image2', 'x1', 'y1', 'x2', 'y2', 'distance_px', 'distance', 'unit', 'angle_deg',
           'dt_s', 'speed_px_per_s', 'speed_unit_per_s', 'time_source')


def rows(measurements: list[Measurement], scale: float | None, unit: str) -> list[list]:
    """Table rows (``COLUMNS``); calibrated columns are blank when there is no scale."""
    out = []
    for k, m in enumerate(measurements, 1):
        dt = m.dt
        out.append([k, '' if m.image1 is None else m.image1, '' if m.image2 is None else m.image2,
                    m.p1[0], m.p1[1], m.p2[0], m.p2[1], f'{m.distance_px:.6g}',
                    '' if scale is None else f'{m.distance_px / scale:.6g}', unit if scale else '',
                    f'{m.angle_deg:.4f}', '' if dt is None else f'{dt:.9g}',
                    '' if dt is None else f'{m.speed():.6g}',
                    '' if dt is None or scale is None else f'{m.speed(scale):.6g}',
                    '' if dt is None else ('frame rate (no time stamps)' if m.times_synthesized else 'time stamps')])
    return out


def to_csv(measurements: list[Measurement], scale: float | None, unit: str, delimiter: str = ',') -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator='\n')
    w.writerow(COLUMNS)
    w.writerows(rows(measurements, scale, unit))
    return buf.getvalue()
