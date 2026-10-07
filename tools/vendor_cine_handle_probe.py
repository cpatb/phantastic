"""Probe how PhPy opens a camera cine (PhPy Python, simulator serving with a stored cine 1)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from vendor_oracle import PhPy, D  # noqa: E402
from vendor_record_download import t  # noqa: E402  (re-runs record+trigger at import)

for args in [(0, 1), ((0, 1),)]:
    h = t(f'phOpenCine{args}', PhPy.phOpenCine, *args)
for s in ('0 1', '0,1', 'cam0 cine1', '0:1'):
    t(f'phGetPh CineHandle {s!r}', PhPy.phGetPh, D['CineHandle'], s)
t('phGetCam CineHandle 0 1', PhPy.phGetCam, D['CineHandle'], 0, 1)
time.sleep(0.2)
