"""Reproduce PCC's TIFF export of a cine, exactly, from a measured table.

PCC's 8-bit and 16-bit TIFF exports are per-value lookups (no spatial filter) built from the
display settings stored in the cine (black/white level, gain, gamma, tone curve, ...). The SDK's
"no processing" switch does not change them. The table cannot yet be computed from the settings
alone (a parametric model of gain, gamma and tone curve misses by up to 240 of 4095 levels), so it
is measured: a ramp cine carrying the file's SETUP is exported by the vendor SDK, which turns every
12-bit value into its exported value (``tools/make_lut_probe.py``, ``tools/vendor_export_probe.py``,
``tools/check_pcc_lut.py``).

Measured tables ship in ``phantastic/data/pcc_tables`` as ``<name>_<tif8|tif16>.csv`` with a
``.json`` holding the exact SETUP settings they belong to; :func:`find_table` picks the one that
matches a file. Check: the 8-bit tables reproduce all 8 PCC 8-bit exports on the lab drive on 100%
of pixels.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .cine import CineReader

TABLE_DIR = Path(__file__).with_name('data') / 'pcc_tables'
# SETUP fields that define PCC's export curve (two files with equal values share one table)
SETTING_KEYS = ('BlackLevel', 'WhiteLevel', 'fOffset', 'fGain', 'fGamma', 'fGammaR', 'fGammaB', 'fFlare',
                'fSaturation', 'fHue', 'fChroma', 'ToneLabel', 'TonePoints', 'fTone', 'fGain16_8', 'RealBPP')


def load_table(path) -> np.ndarray:
    """A 4096-entry table (.npy, or .csv with one value per line after '#' comments)."""
    p = Path(path)
    t = np.load(p) if p.suffix == '.npy' else np.loadtxt(p, comments='#', dtype=np.int64)
    t = np.asarray(t, np.int64).ravel()
    if t.shape != (4096,):
        raise ValueError(f'{path}: expected 4096 entries, got {t.shape}')
    return t


def render(raw: np.ndarray, table: np.ndarray, dtype=np.uint8) -> np.ndarray:
    """Apply a PCC export table to raw 12-bit values (top-down rows, as CineReader returns)."""
    raw = np.asarray(raw)
    if raw.max(initial=0) > 4095:
        raise ValueError('raw values above 4095: tables are for 12-bit data (download P16 with as_12bit)')
    return table[raw].astype(dtype)


def settings_of(cine_path) -> dict:
    with CineReader(cine_path) as r:
        return {k: r.setup.get(k) for k in SETTING_KEYS}


def same_settings(a, b) -> bool:
    """True when two cines carry identical display settings (so one table serves both)."""
    return settings_of(a) == settings_of(b)


def available_tables(kind: str = 'tif8', directory=TABLE_DIR) -> list[dict]:
    """Shipped tables of one kind: [{'csv', 'name', 'settings', 'checked_against_pcc'}, ...]."""
    out = []
    for meta_path in sorted(Path(directory).glob(f'*_{kind}.json')):
        meta = json.loads(meta_path.read_text())
        out.append(dict(meta, csv=str(meta_path.with_suffix('.csv')), name=meta_path.stem))
    return out


def find_table(cine_path, kind: str = 'tif8', directory=TABLE_DIR) -> dict | None:
    """The shipped table whose settings equal this file's, or None (then none can be applied)."""
    s = json.loads(json.dumps(settings_of(cine_path)))   # same JSON round-trip as the stored settings
    for t in available_tables(kind, directory):
        if t['settings'] == s:
            return t
    return None
