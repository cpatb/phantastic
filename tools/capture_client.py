"""Run the simulator in capture mode for N seconds, then write every command it received.

    python tools/capture_client.py <seconds> <out.txt> [--strict]

Point any PH16 client (e.g. vendor software) at this machine while it runs. Unknown ``get``
requests are answered with 0 unless --strict, so a client can be observed past its handshake.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.simulator import CameraModel, Simulator  # noqa: E402

secs, out = float(sys.argv[1]), Path(sys.argv[2])
sim = Simulator('0.0.0.0', 7115, model=CameraModel(profile='miro-m310'))
sim.model.permissive = '--strict' not in sys.argv
for a in sys.argv[3:]:
    if a.startswith('--formats='):
        sim.model.info['imgformats'] = a.split('=', 1)[1].replace(',', ' ')
sim.start()
t0 = time.time()
try:
    while time.time() - t0 < secs:
        time.sleep(0.5)
finally:
    sim.stop()
    out.write_text('\n'.join(sim.model.received) + '\n')
    Path(str(out) + '.unknown.txt').write_text('\n'.join(sorted(set(sim.model.unknown))) + '\n')
    print(f'{len(sim.model.received)} commands captured -> {out}; {len(set(sim.model.unknown))} unknown names')
