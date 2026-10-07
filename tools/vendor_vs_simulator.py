"""Drive the vendor SDK (PhPy, as used by PCC) against a running Phantastic simulator.

Start the simulator first, reachable on the LAN (vendor discovery uses broadcasts):
    python -m phantastic.simulator --host 0.0.0.0 --log captured_commands.txt
then run this with the PhPy Python (CPython 3.11 + numpy 1.x):
    py311 tools/vendor_vs_simulator.py
It prints what the vendor library reports about the simulated camera. The simulator's log is
then the exact command sequence the vendor library sent (protocol capture by observation).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from vendor_oracle import PhPy, D  # noqa: E402


def t(label, fn, *a):
    try:
        r = fn(*a)
        print(f'{label}: {r!r}'[:400])
        return r
    except Exception as e:  # probing: every failure is information
        print(f'{label}: {type(e).__name__}: {e}')


t('Register', PhPy.phDoPh, D['Register'])
for wait in (1, 3, 6):
    time.sleep(wait)
    n = t(f'CameraCount after {wait}s', PhPy.phGetPh, D['CameraCount'])
    if n:
        break
if n:
    for sel in ('Serial', 'Model', 'HardwareVersion', 'Name', 'Resolution', 'FrameRate', 'Exposure',
                'PostTriggerFrames', 'PartitionsCount', 'Offline', 'Recorded', 'ActivePartition'):
        t(f'cam0 {sel}', PhPy.phGetCam, D[sel], 0)
