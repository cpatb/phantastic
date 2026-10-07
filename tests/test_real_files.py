"""Checks against real recordings and PCC's own outputs (lab data drive; skipped elsewhere).

Set PHANTASTIC_DATA to the drive root (default F:/). These are the anchors that do not pass
through Phantastic's own writer: PCC-written decimated cines and PCC-written TIFFs.
"""
import hashlib
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from phantastic.cine import CineReader
from phantastic.decimate import decimate_cine

ROOT = Path(os.environ.get('PHANTASTIC_DATA', 'F:/'))
# The session folder was renamed once (to ..._0.44VF_06OCT26); find it by pattern.
_SESSION = next(iter(sorted(ROOT.glob('800nm_si_sphere*06OCT26'))), ROOT / '800nm_si_sphere_06OCT26') if ROOT.exists() else ROOT
SIDE = _SESSION / 'u0_1' / 'side'
CAL = _SESSION / 'calibration'
pytestmark = pytest.mark.skipif(not (SIDE / 'drop1.cine').exists(), reason='lab data drive not present')


def probe_frame(path, i):
    """Independent minimal decoder: header offsets by struct, 16-bit bottom-up rows."""
    with open(path, 'rb') as f:
        h = f.read(84)
        ic, ofio = struct.unpack_from('<I', h, 20)[0], struct.unpack_from('<I', h, 32)[0]
        w, hgt, size = struct.unpack_from('<i', h, 48)[0], struct.unpack_from('<i', h, 52)[0], struct.unpack_from('<I', h, 64)[0]
        f.seek(ofio + 8 * i)
        p = struct.unpack('<q', f.read(8))[0]
        f.seek(p)
        ann = struct.unpack('<I', f.read(4))[0]
        f.seek(p + ann)
        a = np.frombuffer(f.read(size), '<u2').reshape(hgt, w)
    assert 0 <= i < ic
    return a[::-1]


def test_reader_matches_independent_probe():
    p = SIDE / 'drop1_dec1000.cine'
    r = CineReader(p)
    for i in (0, 500, len(r) - 1):
        assert np.array_equal(r.read(i), probe_frame(p, i))


def test_decimation_reproduces_pcc(tmp_path):
    out = tmp_path / 'x1000.cine'
    decimate_cine(SIDE / 'drop1.cine', out, 1000, 'trigger')
    a, b = CineReader(out), CineReader(SIDE / 'drop1_dec1000.cine')
    assert len(a) == 10 and a.first == -1075
    for i in range(len(a)):
        j = b.index_of(a.first + i)
        assert hashlib.md5(a.read(i).tobytes()).digest() == hashlib.md5(b.read(j).tobytes()).digest()
        assert np.array_equal(a.image_times_raw()[i], b.image_times_raw()[j])
        assert a.exposures_raw()[i] == b.exposures_raw()[j]


def test_orientation_matches_pcc_tiff():
    tifffile = pytest.importorskip('tifffile')
    r = CineReader(CAL / 'side1.cine')
    t = tifffile.imread(CAL / 'side1_2.tif').astype(np.int64)
    a = r.read(0).astype(np.int64)
    # PCC's 8-bit export is a per-value LUT: every raw value maps to one output value
    # only in the orientation Phantastic returns.
    def spread(x):
        order = np.argsort(x, axis=None)
        xv, tv = x.ravel()[order], t.ravel()[order]
        _, start = np.unique(xv, return_index=True)
        return int((np.maximum.reduceat(tv, start) - np.minimum.reduceat(tv, start)).max())
    assert spread(a) == 0
    assert spread(a[::-1]) > 100   # discriminating negative: the flipped image is not a LUT of it


def test_pcc_8bit_export_reproduced_exactly():
    """Measured PCC tables reproduce PCC 8-bit exports on the drive; the wrong table does not."""
    tifffile = pytest.importorskip('tifffile')
    from phantastic.pcc_render import load_table, render, same_settings
    tables = Path(__file__).resolve().parents[1] / 'phantastic' / 'data' / 'pcc_tables'
    t_a = load_table(tables / 'lab_2026-10-06_tif8.csv')
    t_b = load_table(tables / 'lab_group2_tif8.csv')
    cases = [(CAL / f'side{i}.cine', CAL / f'side{i}_2.tif') for i in (1, 2, 3)]
    oct01 = next(iter(ROOT.glob('800nm_si_sphere_01OCT26')), None)
    if oct01 is not None:
        cases.append((oct01 / 'calibration' / '01OCT26 calibration.cine',
                      oct01 / 'calibration' / '01OCT26 calibration_2.tif'))
    for src, tif in cases:
        raw, pcc = CineReader(src).read(0), tifffile.imread(tif)
        right, wrong = (t_a, t_b) if same_settings(src, CAL / 'side1.cine') else (t_b, t_a)
        assert np.array_equal(render(raw, right), pcc), src
        assert np.mean(render(raw, wrong) == pcc) < 0.2
