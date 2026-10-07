"""Vendor decoder as a TEST ORACLE (never imported by Phantastic itself).

Runs only where the Phantom SDK/PCC is installed and a Python matching its ``PhPy.pyd`` is
available (PCC 3.11 ships PhPy for CPython 3.11 + numpy 1.x). Usage::

    py311 tools/vendor_oracle.py compare <file.cine> [n_frames]
    py311 tools/vendor_oracle.py dump <file.cine> <first> <last> <out.npz> [processed]

``compare`` checks that the vendor's unprocessed images equal the bytes Phantastic decodes.
``dump`` writes the vendor's images (processed or not) for comparisons run under any Python.
The vendor API is driven only through its public Python binding; nothing is decompiled.
"""
import os
import sys

PHANTOM_DIR = os.environ.get('PHANTOM_SDK_DIR', r'C:\Program Files\Phantom')
os.add_dll_directory(PHANTOM_DIR)
import numpy as np  # noqa: E402
import PhPy  # noqa: E402

D = PhPy.phGetDic()


def open_cine(path, processed=False):
    h = PhPy.phOpenCine(path)
    PhPy.phSetCine(D['NoProcessing'], h, 0 if processed else 1)
    return h


def images(h, first, last):
    return PhPy.phGetCineImage(h, first, last)


def main():
    cmd, path = sys.argv[1], sys.argv[2]
    if cmd == 'dump':
        first, last, out = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
        processed = len(sys.argv) > 6 and sys.argv[6] == 'processed'
        h = open_cine(path, processed)
        info = {k: PhPy.phGetCine(D[k], h) for k in ('BitsPerPixel', 'BlackWhiteLevels', 'Range', 'NoProcessing')}
        np.savez(out, images=images(h, first, last), first=first, last=last, info=repr(info))
        print('wrote', out, info)
    elif cmd == 'compare':
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from phantastic.cine import CineReader
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        r = CineReader(path)
        h = open_cine(path, processed=False)
        print('vendor', {k: PhPy.phGetCine(D[k], h) for k in ('BitsPerPixel', 'BlackWhiteLevels', 'Range', 'NoProcessing')})
        idx = np.unique(np.linspace(0, len(r) - 1, min(n, len(r))).astype(int))
        bad = 0
        for i in idx:
            num = r.first + int(i)
            v = images(h, num, num)[0]
            mine = r.read(int(i))
            same = v.shape == mine.shape[: v.ndim] and np.array_equal(v, mine)
            if not same:
                bad += 1
                flip = np.array_equal(v, mine[::-1]) if v.shape == mine.shape else False
                print(f'image {num}: DIFFER (vendor {v.dtype}{v.shape} [{v.min()},{v.max()}], '
                      f'mine {mine.dtype}{mine.shape} [{mine.min()},{mine.max()}], equal-if-flipped={flip})')
        print(f'compared {len(idx)} images: {len(idx) - bad} identical, {bad} different')
        sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
