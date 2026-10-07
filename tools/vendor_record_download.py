"""Vendor SDK: record, trigger and download from the (simulated) camera 0. PhPy Python only.

Run while tools/capture_client.py is serving; the capture then shows the vendor's exact
record/trigger/download command sequence, and the downloaded pixels are compared with the
simulator's deterministic frames.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np  # noqa: E402
from vendor_oracle import PhPy, D  # noqa: E402


def t(label, fn, *a):
    try:
        r = fn(*a)
        s = f'ndarray {r.shape} {r.dtype} [{r.min()}, {r.max()}]' if isinstance(r, np.ndarray) else repr(r)
        print(f'{label}: {s}'[:300])
        return r
    except Exception as e:  # probing: every failure is information
        print(f'{label}: {type(e).__name__}: {e}')


t('Register', PhPy.phDoPh, D['Register'])
for _ in range(20):
    if PhPy.phGetPh(D['CameraCount']):
        break
    time.sleep(0.5)
print('cameras', PhPy.phGetPh(D['CameraCount']))
t('Record', PhPy.phDoCam, D['Record'], 0)
time.sleep(0.5)
t('Trigger', PhPy.phDoCam, D['Trigger'], 0)
time.sleep(1.0)
t('Recorded', PhPy.phGetCam, D['Recorded'], 0, 1)
h = t('phGetCam CineHandle (camera 0, cine 1)', PhPy.phGetCam, D['CineHandle'], 0, 1)
if isinstance(h, int) and h:
    PhPy.phSetCine(D['NoProcessing'], h, 1)
    rng = t('Range', PhPy.phGetCine, D['Range'], h)
    if rng:
        first = rng[0]
        img = t('phGetCineImage first..first+4', PhPy.phGetCineImage, h, first, first + 4)
        if isinstance(img, np.ndarray):
            np.save(os.path.join(os.environ.get('OUTDIR', '.'), 'vendor_dl.npy'), img)
            from phantastic.simulator import synthetic_frame
            want = np.stack([synthetic_frame(n, img.shape[2], img.shape[1], seed=1) for n in range(first, first + 5)])
            print('vendor frames == simulator frames:', np.array_equal(img, want),
                  ' equal if flipped:', np.array_equal(img[:, ::-1], want),
                  ' max|diff|:', int(np.abs(img.astype(int) - want).max()))
