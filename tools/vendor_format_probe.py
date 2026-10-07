"""How does the vendor SDK decode each wire format? (run with the PhPy Python, e.g. py311)

Runs Phantastic's simulator in-process with a frame holding every 12-bit value (a ramp), lets
the vendor SDK discover it, record, trigger and download one frame in the given format, and
prints the vendor's value for each transmitted value. Usage:

    py311 tools/vendor_format_probe.py P12L|P16|P10|8 [out.npz]
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
import numpy as np  # noqa: E402

import subprocess  # noqa: E402

from vendor_oracle import PhPy, D  # noqa: E402

fmt = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else None
W = H = 256
RAMP = (np.arange(W * H) % 4096).astype(np.uint16).reshape(H, W)   # must match sim_ramp_server.py
# The simulator runs in its own process: PhPy blocks the interpreter during camera I/O.
log = os.path.join(os.environ.get('TEMP', '.'), f'vendor_probe_{fmt}.txt')
srv = subprocess.Popen([os.environ.get('PHANTASTIC_PY', 'python'), os.path.join(HERE, 'sim_ramp_server.py'),
                        '40', fmt, log], stdout=subprocess.PIPE, text=True)
assert srv.stdout.readline().strip() == 'ready'
try:
    PhPy.phDoPh(D['Register'])
    for _ in range(40):
        if PhPy.phGetPh(D['CameraCount']):
            break
        time.sleep(0.25)
    assert PhPy.phGetPh(D['CameraCount']) >= 1, 'vendor SDK did not find the simulator'
    PhPy.phDoCam(D['Record'], 0)
    time.sleep(1.0)
    PhPy.phDoCam(D['Trigger'], 0)
    for _ in range(40):
        time.sleep(0.25)
        if PhPy.phGetCam(D['Recorded'], 0, 1):
            break
    h = PhPy.phGetCam(D['CineHandle'], 0, 1)
    PhPy.phSetCine(D['NoProcessing'], h, 1)
    first = PhPy.phGetCine(D['Range'], h)[0]
    img = PhPy.phGetCineImage(h, first, first)[0].astype(np.int64)
finally:
    srv.wait(60)
cmds = [c for c in open(log).read().splitlines() if c.startswith('img')]

sent = RAMP.astype(np.int64)
print('vendor request:', cmds[-1] if cmds else None)
print('vendor image', img.shape, img.dtype, 'range', img.min(), img.max())
pairs = {}
for a, b in zip(sent.ravel(), img.ravel()):
    pairs.setdefault(int(a), set()).add(int(b))
amb = sum(len(v) > 1 for v in pairs.values())
f = np.array([min(pairs[v]) if v in pairs else -1 for v in range(4096)])
print('per-value function?', amb == 0, f'({amb} ambiguous values)')
for v in (0, 1, 15, 16, 64, 100, 255, 256, 1000, 2048, 4000, 4064, 4095):
    print(f'  sent {v:5d} -> vendor {sorted(pairs.get(v, []))[:4]}')
if out:
    np.savez(out, sent=sent, vendor=img, fmt=fmt, request=str(cmds[-1] if cmds else ''))
