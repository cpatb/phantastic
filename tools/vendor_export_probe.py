"""Export a cine with the vendor SDK's processing (PhPy Python). Test oracle only.

    py311 tools/vendor_export_probe.py <in.cine> <out_basename> <save_type>

save_type: svi_Tif8, svi_Tif12, svi_Tif16, svv_Cine, ... (keys of the SDK dictionary).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from vendor_oracle import PhPy, D  # noqa: E402

src, out, kind = sys.argv[1], sys.argv[2], sys.argv[3]
h = PhPy.phOpenCine(src)
PhPy.phSetCine(D['NoProcessing'], h, int(os.environ.get('NOPROC', '0')))
rng = PhPy.phGetCine(D['Range'], h)
for sel, val in (('SaveName', out), ('SaveType', D[kind]), ('SaveRange', rng)):
    PhPy.phSetCine(D[sel], h, val)
PhPy.phDoCine(D['Save'], h)
d = os.path.dirname(out) or '.'
print('range', rng, 'files:', sorted(f for f in os.listdir(d) if os.path.basename(out) in f))
