"""Derive PCC's export table from a vendor-exported ramp and test it on real PCC exports.

    python tools/check_pcc_lut.py <ramp.cine> <ramp_tif8.tif> <ramp_tif16.tif> <calibration dir>

The calibration dir must hold PCC exports named <name>_2.tif next to <name>.cine (as on the lab
drive). Prints per-value consistency of the table and the exact-match fraction on each real image.
"""
import sys
from pathlib import Path

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.cine import CineReader  # noqa: E402


def table(ramp, out):
    ramp, out = ramp.ravel().astype(np.int64), out.ravel().astype(np.int64)
    t = np.full(4096, -1, np.int64)
    bad = 0
    for v in range(4096):
        o = np.unique(out[ramp == v])
        if len(o) != 1:
            bad += 1
        t[v] = o[0]
    return t, bad


def main(ramp_cine, tif8, tif16, cal_dir):
    ramp = CineReader(ramp_cine).read(0)
    results = {}
    for name, path in (('8-bit', tif8), ('16-bit', tif16)):
        img = tifffile.imread(path)
        flip = False
        t, bad = table(ramp, img)
        if bad:                               # try the other row order
            t2, bad2 = table(ramp[::-1], img)
            if bad2 < bad:
                t, bad, flip = t2, bad2, True
        results[name] = t
        print(f'{name}: {img.dtype} {img.shape}; one output per input value: {bad == 0} ({bad} ambiguous); '
              f'rows flipped vs Phantastic: {flip}; table[0, 64, 1000, 4064, 4095] = {t[[0, 64, 1000, 4064, 4095]]}')
        np.save(Path(path).with_suffix('.table.npy'), t)
    t8 = results['8-bit']
    for cine in sorted(Path(cal_dir).glob('*.cine')):
        tif = cine.with_name(cine.stem + '_2.tif')
        if not tif.exists():
            continue
        raw = CineReader(cine).read(0).astype(np.int64)
        pcc = tifffile.imread(tif).astype(np.int64)
        pred = t8[raw]
        print(f'{cine.name}: table(raw) == PCC export: {np.mean(pred == pcc):.6f} of pixels, '
              f'max |diff| {np.abs(pred - pcc).max()}')


if __name__ == '__main__':
    main(*sys.argv[1:5])
