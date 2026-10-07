"""Which (biBitCount, biCompression, RealBPP) make the vendor decode a packed ramp? (PhPy Python)"""
import os
import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from vendor_oracle import open_cine, images  # noqa: E402

synth = Path(sys.argv[1])
tmp = synth / 'probe'
tmp.mkdir(exist_ok=True)
for src in ('ramp_packed10.cine', 'ramp_packed12L.cine'):
    for bc in (8, 10, 12, 16):
        for comp in (0, 1, 2, 256, 512, 1024, 0x40, 0x100 | 0x400):
            for real in (10, 12):
                dst = tmp / f'{src[:-5]}_{bc}_{comp}_{real}.cine'
                shutil.copy(synth / src, dst)
                with open(dst, 'r+b') as f:
                    f.seek(44 + 14)
                    f.write(struct.pack('<H', bc))
                    f.seek(44 + 16)
                    f.write(struct.pack('<I', comp))
                    f.seek(84 + 896)
                    f.write(struct.pack('<I', real))
                try:
                    v = images(open_cine(str(dst)), 0, 0)[0]
                    if v.max() > 0:
                        print(f'{src}: biBitCount={bc} biCompression={comp} RealBPP={real} -> {v.dtype}{v.shape} '
                              f'max {v.max()} first {v.ravel()[:6]} last {v.ravel()[-3:]}')
                except Exception as e:  # probe: report failures by type only
                    print(f'{src}: {bc}/{comp}/{real}: {type(e).__name__}')
