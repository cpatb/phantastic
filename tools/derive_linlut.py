"""Black-box checks of the synthetic ramps against the vendor decoder (run with the PhPy Python).

For each synthetic file, prints whether the vendor's unprocessed decode equals the ramp that was
written. For ramp_packed10.cine the vendor output IS the 10-bit linearisation table, which is
written to phantastic/linlut10.txt (and copied to the Java plugin by hand).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import numpy as np  # noqa: E402
from vendor_oracle import open_cine, images, PhPy, D  # noqa: E402

synth = Path(sys.argv[1])
expected = {
    'ramp_mono16.cine': np.arange(4096).reshape(64, 64),
    'ramp_packed12L.cine': np.arange(4096).reshape(64, 64),
    'ramp_mono8.cine': np.arange(256).reshape(16, 16),
}
yy, xx = np.mgrid[0:16, 0:32]
expected['ramp_bgr24.cine'] = np.stack([xx * 8, yy * 16, 255 - xx * 8], -1)
expected['ramp_bgr48.cine'] = np.stack([xx * 128, yy * 256, 4095 - xx * 128], -1)

for name in sorted(expected) + ['ramp_packed10.cine']:
    h = open_cine(str(synth / name), processed=False)
    info = {k: PhPy.phGetCine(D[k], h) for k in ('BitsPerPixel', 'BlackWhiteLevels')}
    v = images(h, 0, 0)[0].astype(np.int64)
    if name == 'ramp_packed10.cine':
        lut = v.ravel()
        print(f'{name}: vendor {v.shape} {info} table[0..8]={lut[:8]} table[-4:]={lut[-4:]} monotone={np.all(np.diff(lut) >= 0)}')
        if not (lut[-1] > lut[0] and np.all(np.diff(lut) >= 0) and lut.max() > 1023):
            print('REFUSING to write linlut10.txt: vendor output is not a plausible 10->12-bit table')
            continue
        out = Path(__file__).resolve().parents[1] / 'phantastic' / 'linlut10.txt'
        with open(out, 'w', newline='\n') as f:
            f.write('# Phantom 10-bit packed code -> 12-bit linear value, index = code (0..1023).\n')
            f.write('# Measured black-box: every code written into a synthetic cine and decoded by the\n')
            f.write('# vendor SDK (PhPy, NoProcessing, BlackLevel 0, WhiteLevel 4095). tools/derive_linlut.py\n')
            for i in range(0, 1024, 16):
                f.write(' '.join(str(int(x)) for x in lut[i:i + 16]) + '\n')
        print('wrote', out)
        continue
    e = expected[name]
    same = v.shape == e.shape and np.array_equal(v, e)
    msg = 'IDENTICAL' if same else 'DIFFERENT'
    extra = ''
    if not same and v.shape == e.shape:
        d = v - e
        extra = (f' max|d|={np.abs(d).max()} flipped-equal={np.array_equal(v, e[::-1])} '
                 f'ratio~{(v.ravel()[-1] / max(1, e.ravel()[-1])):.4f}')
    print(f'{name}: vendor {v.dtype}{v.shape} {info} vs written ramp: {msg}{extra}')
