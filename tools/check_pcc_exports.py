"""Check measured PCC tables against every PCC TIFF export found under a folder.

    python tools/check_pcc_exports.py <root> <table8.csv> [<table8.csv> ...]

A PCC export ``X_2.tif`` has a sidecar ``X_2tif.chd`` holding a copy of the source cine's
header; the source cine is the .cine in the same folder with the same FirstImageNo and
TriggerTime. Each export is rendered from its source with every table whose settings match.
"""
import struct
import sys
from pathlib import Path

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.cine import CineReader  # noqa: E402
from phantastic.pcc_render import load_table, render  # noqa: E402


def key(b):
    return struct.unpack_from('<i', b, 16)[0], struct.unpack_from('<II', b, 36)


def main(root, *tables):
    tabs = {Path(t).name: load_table(t) for t in tables}
    for chd in sorted(Path(root).rglob('*tif.chd')):
        tif = chd.with_name(chd.name[:-len('tif.chd')] + '.tif')
        k = key(chd.read_bytes()[:44])
        src = None
        for c in chd.parent.glob('*.cine'):
            with open(c, 'rb') as f:
                if key(f.read(44)) == k:
                    src = c
                    break
        if src is None or not tif.exists():
            print(f'{tif.name}: source cine not found')
            continue
        raw = CineReader(src).read(0)
        pcc = tifffile.imread(tif)
        res = {n: float(np.mean(render(raw, t) == pcc)) for n, t in tabs.items()}
        print(f'{src.parent.name}/{src.name} -> {tif.name}: exact-match fraction per table {res}')


if __name__ == '__main__':
    main(*sys.argv[1:])
