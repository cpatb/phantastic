"""Compare two cines image by image (e.g. a Phantastic download with a PCC save of the same range).

    python tools/compare_cines.py a.cine b.cine

Matches images by camera image number, then reports for the overlap: identical, a == 16*b,
b == 16*a, or the largest difference; and whether the time stamps agree.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.cine import CineReader  # noqa: E402


def main(pa, pb):
    a, b = CineReader(pa), CineReader(pb)
    lo, hi = max(a.first, b.first), min(a.first + len(a), b.first + len(b)) - 1
    print(f'A {pa}: images {a.first}..{a.first + len(a) - 1}, {a.packing}, real_bpp {a.real_bpp}')
    print(f'B {pb}: images {b.first}..{b.first + len(b) - 1}, {b.packing}, real_bpp {b.real_bpp}')
    if hi < lo:
        print('no common image numbers')
        return 1
    counts = {'identical': 0, 'A == 16*B': 0, 'B == 16*A': 0, 'different': 0}
    worst = 0
    for n in range(lo, hi + 1):
        x = a.read(a.index_of(n)).astype(np.int64)
        y = b.read(b.index_of(n)).astype(np.int64)
        if x.shape != y.shape:
            print(f'image {n}: shapes differ {x.shape} vs {y.shape}')
            return 1
        if np.array_equal(x, y):
            counts['identical'] += 1
        elif np.array_equal(x, 16 * y):
            counts['A == 16*B'] += 1
        elif np.array_equal(y, 16 * x):
            counts['B == 16*A'] += 1
        else:
            counts['different'] += 1
            worst = max(worst, int(np.abs(x - y).max()))
    ta, tb = a.image_times(), b.image_times()
    t_ok = None
    if ta is not None and tb is not None:
        t_ok = bool(np.allclose(ta[lo - a.first:hi - a.first + 1], tb[lo - b.first:hi - b.first + 1], atol=1e-7))
    print(f'{hi - lo + 1} common images: {counts}; largest |A-B| among different: {worst}; time stamps equal: {t_ok}')
    return 0 if counts['different'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
