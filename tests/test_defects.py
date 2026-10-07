"""Camera-flagged pixels (0xFF00 in P16) and PCC's 8-neighbour fill-in (phantastic.defects)."""
import os
from pathlib import Path

import numpy as np
import pytest

from phantastic.defects import FLAG_P16, convert, fill, flagged

PCC_TEST = Path(os.environ.get('PHANTASTIC_PCC_TEST', r'C:\Users\compl\Desktop\PCC_test'))


def test_fill_is_the_rounded_half_down_mean_of_8_neighbours():
    f = np.full((5, 5), 100, np.uint16)
    f[1:4, 1:4] = [[10, 11, 12], [13, FLAG_P16, 14], [15, 16, 17]]
    m = flagged(f, FLAG_P16)
    assert m.sum() == 1 and m[2, 2]
    # neighbours sum 108 / 8 = 13.5 -> half DOWN = 13 (half-up would give 14; this is the measured rule)
    assert fill(f, m)[2, 2] == 13
    f[1, 1] = 11                                  # sum 109 / 8 = 13.625 -> 14
    assert fill(f, flagged(f, FLAG_P16))[2, 2] == 14
    g = f.copy()
    fill(g, flagged(g, FLAG_P16))
    assert np.array_equal(g, f)                   # input untouched


def test_saturated_region_is_not_repaired_and_edges_use_available_neighbours():
    f = np.full((6, 6), 500, np.uint16)
    f[0:2, 0:2] = FLAG_P16                        # a clipped block: no pixel is isolated
    assert not flagged(f, FLAG_P16).any()
    h = np.full((4, 4), 7, np.uint16)
    h[0, 0] = FLAG_P16                            # corner: 3 neighbours, all 7
    assert fill(h, flagged(h, FLAG_P16))[0, 0] == 7


def test_convert_12bit_domain_and_formats():
    f = np.full((3, 3), 1600, np.uint16)          # 12-bit 100
    f[1, 1] = FLAG_P16
    v, n = convert(f, 'P16', as_12bit=True, fill_flags=True)
    assert n == 1 and v[1, 1] == 100 and v[0, 0] == 100
    v, n = convert(f, 'P16', as_12bit=True, fill_flags=False)
    assert n == 0 and v[1, 1] == FLAG_P16 >> 4
    v, n = convert(f, 'P16R', as_12bit=False, fill_flags=True)
    assert n == 0 and v[1, 1] == FLAG_P16       # only corrected P16 carries flags


@pytest.mark.skipif(not (PCC_TEST / 'PCC_clip_saved.cine').exists(), reason='PCC comparison files not present')
def test_matches_pcc_save_of_the_same_camera_cine():
    """Anchor: Phantastic's 12-bit save (flags kept) + fill vs PCC's save of the same v2512 cine (2026-10-07)."""
    from phantastic.cine import CineReader
    a, b = CineReader(PCC_TEST / 'Phantastic_clip_saved.cine'), CineReader(PCC_TEST / 'PCC_clip_saved.cine')
    exact = total = 0
    for i in np.linspace(0, len(a) - 1, 40).astype(int):
        x, y = a.read(int(i)), b.read(int(i)).astype(np.int64)
        m = flagged(x, FLAG_P16 >> 4)
        assert m.sum() == 128
        d = fill(x, m).astype(np.int64) - y
        assert not d[~m].any()                    # every unflagged pixel identical: PCC floors too
        assert np.abs(d[m]).max() <= 1
        exact += int((d[m] == 0).sum())
        total += int(m.sum())
    assert exact / total > 0.9                    # measured 92.1 % on 200 frames
