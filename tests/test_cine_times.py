"""relative_times() must keep the TIME64 resolution (2^-32 s), not float64-of-epoch resolution.

At an epoch of ~1.8e9 s float64 spacing is 2^-22 s = 0.24 us, so subtracting two absolute float
times loses up to ~0.24 us -- a quarter of the frame interval at 1 Mfps. Anchor: times written
as exact integer TIME64 values whose differences from the trigger are known in closed form.
"""
import numpy as np

from phantastic.cine import TIME64_SCALE, CineReader, CineWriter

TRIG_SEC = 1_791_366_586          # 2026-10 epoch seconds, where float64 spacing is 2^-22 s
TRIG_FRAC = 3_660_153_949         # an arbitrary fraction (as on a real trigger)
STEP_FRAC = 4295                  # ~1.0000 us in units of 2^-32 s


def test_relative_times_keep_time64_resolution(tmp_path):
    n = 50
    path = tmp_path / 't.cine'
    offsets = (np.arange(n) - 25) * STEP_FRAC            # images -25..24, 1 us apart
    with CineWriter(path, 4, 2, n, 'mono16', first_image_no=-25, trigger_time=(TRIG_SEC, TRIG_FRAC),
                    setup_fields={'FrameRate': 1_000_000}, with_exposures=False) as w:
        for k in range(n):
            total = TRIG_FRAC + int(offsets[k])
            w.append(np.zeros((2, 4), np.uint16), time=(TRIG_SEC + total // 2**32, total % 2**32))
    with CineReader(path) as r:
        rel = r.relative_times()
    exact = offsets / TIME64_SCALE
    assert np.abs(rel - exact).max() < 1e-12            # well below the 2.3e-10 s TIME64 quantum
    assert rel[25] == 0.0                                 # the trigger image is exactly t = 0
