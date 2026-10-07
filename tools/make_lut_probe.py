"""Write a 256x256 ramp cine (every 12-bit value 16 times) carrying a real file's SETUP.

    python tools/make_lut_probe.py <template.cine> <out.cine>

Exporting this file with PCC's processing turns its display settings into a per-value table.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.cine import CineReader, CineWriter  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
r = CineReader(src)
ramp = (np.arange(256 * 256) % 4096).astype(np.uint16).reshape(256, 256)
with CineWriter(dst, 256, 256, 1, 'mono16', first_image_no=0, setup=r.setup_raw,
                trigger_time=r.header['TriggerTime'], clr_important=r.bitmap['biClrImportant'],
                with_exposures=False) as w:
    w.append(ramp, time=r.header['TriggerTime'])
print('wrote', dst, 'with SETUP of', src)
