"""Have the vendor SDK write packed copies of a real cine (PhPy Python). Test-oracle only.

Usage: py311 vendor_save_packed.py <src.cine> <outdir> <first> <last>
Writes <outdir>/vendor_packed<k>.cine for SavePacked = 0, 1, 2 and reports what each became.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(__file__))
from vendor_oracle import PhPy, D  # noqa: E402

src, outdir, first, last = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
os.makedirs(outdir, exist_ok=True)


def tryset(sel, h, val):
    try:
        PhPy.phSetCine(D[sel], h, val)
        return 'ok'
    except Exception as e:  # probing undocumented API
        return f'{type(e).__name__}: {e}'


for packed in (0, 1, 2):
    h = PhPy.phOpenCine(src)
    out = os.path.join(outdir, f'vendor_packed{packed}.cine')
    r = [tryset('NoProcessing', h, 1), tryset('SaveName', h, out), tryset('SaveType', h, D[os.environ.get('SAVETYPE', 'svv_RawCine')]),
         tryset('SaveRange', h, (first, last)), tryset('SavePacked', h, packed)]
    try:
        res = PhPy.phDoCine(D['Save'], h)
        status = f'save -> {res!r}'
    except Exception as e:
        status = f'save failed {type(e).__name__}: {e}'
    info = ''
    if os.path.exists(out):
        with open(out, 'rb') as f:
            b = f.read(84)
        bi = struct.unpack('<IiiHHIIiiII', b[44:84])
        info = f'size {os.path.getsize(out)} biBitCount {bi[4]} biCompression {bi[5]} biSizeImage {bi[6]}'
    print(f'SavePacked={packed}: setters {r} {status} {info}')
