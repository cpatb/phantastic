"""Serve a simulated camera whose frames hold every 12-bit value (for vendor_format_probe.py).

    python tools/sim_ramp_server.py <seconds> <formats> <received.txt>
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import phantastic.simulator as S  # noqa: E402

W = H = 256
RAMP = (np.arange(W * H) % 4096).astype(np.uint16).reshape(H, W)   # every 12-bit value, 16 times


def ramp_frame(_number, width, height, seed=1):  # noqa: ARG001  (same signature as synthetic_frame)
    return RAMP[:height, :width].copy()


S.synthetic_frame = ramp_frame
secs, formats, out = float(sys.argv[1]), sys.argv[2].replace(',', ' '), Path(sys.argv[3])
model = S.CameraModel(profile='miro-m310', width=W, height=H)
model.info['imgformats'] = formats
model.permissive = True
sim = S.Simulator('0.0.0.0', 7115, model=model).start()
print('ready', flush=True)
try:
    time.sleep(secs)
finally:
    sim.stop()
    out.write_text('\n'.join(model.received) + '\n')
