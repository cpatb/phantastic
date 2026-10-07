"""Regression tests for the 2026-10-07 code review (each reproduced a silent error before the fix)."""
import struct

import numpy as np
import pytest

from phantastic.cine import CineReader, CineWriter, build_setup
from phantastic.decimate import decimate_cine


def _fields(rate=1000):
    return dict(FrameRate=rate, RealBPP=8, BlackLevel=0, WhiteLevel=255)


def test_padded_stride_survives_decimation(tmp_path):
    # width 6, rows padded to 8 bytes: before the fix the copy's header said stride 6 -> sheared.
    src = tmp_path / 'pad.cine'
    rng = np.random.default_rng(1)
    frames = [rng.integers(0, 256, (5, 6), dtype=np.uint8) for _ in range(20)]
    with CineWriter(src, 6, 5, 20, 'mono8', first_image_no=-10, setup_fields=_fields(), stride=8,
                    trigger_time=(1_000_000, 0)) as w:
        for k, f in enumerate(frames):
            w.append(f, time=(1_000_000, k << 20), exposure=1)
    r = CineReader(src)
    assert r.stride == 8 and np.array_equal(r.read(3), frames[3])
    out = tmp_path / 'dec.cine'
    decimate_cine(src, out, 5)
    d = CineReader(out)
    assert d.stride == 8
    for k, n in enumerate(range(-10, 10, 5)):
        assert np.array_equal(d.read(k), frames[n + 10])


def test_decimation_without_stamps_keeps_true_times(tmp_path):
    src = tmp_path / 'nostamp.cine'
    with CineWriter(src, 4, 4, 40, 'mono8', first_image_no=-20, setup_fields=_fields(1000),
                    with_times=False, with_exposures=False, trigger_time=(1_000_000, 0)) as w:
        for k in range(40):
            w.append(np.full((4, 4), k, np.uint8))
    with pytest.warns(UserWarning):
        truth = CineReader(src).relative_times()
    out = tmp_path / 'dec.cine'
    info = decimate_cine(src, out, 10)
    d = CineReader(out)
    assert d.has_complete_times()
    # before the fix: numbers/fps on the renumbered file gave times 10x too small
    assert np.allclose(d.relative_times(), truth[np.arange(0, 40, 10)], atol=1e-9)
    assert 'SYNTHESIZED' in d.setup['Description'] and info['offset'] == 0


def test_per_image_blocks_are_kept(tmp_path):
    src = tmp_path / 'tc.cine'
    tc = np.arange(30 * 8, dtype=np.uint8).reshape(30, 8)          # 8-byte per-image records (like 1007)
    with CineWriter(src, 4, 4, 30, 'mono8', first_image_no=0, setup_fields=_fields(),
                    extra_blocks={1007: tc.tobytes()}, trigger_time=(1_000_000, 0)) as w:
        for k in range(30):
            w.append(np.full((4, 4), k, np.uint8), time=(1_000_000, k), exposure=k)
    out = tmp_path / 'dec.cine'
    decimate_cine(src, out, 3)
    d = CineReader(out)
    assert np.array_equal(np.frombuffer(d.block_bytes(1007), np.uint8).reshape(-1, 8), tc[::3])


def test_short_setup_keeps_its_length():
    old = bytearray(9000)
    struct.pack_into('<I', old, 768, 500)
    s = build_setup({'FrameRate': 600}, template=bytes(old))
    assert len(s) == 9000 and struct.unpack_from('<H', s, 142)[0] == 9000
    assert struct.unpack_from('<i', build_setup({'WhiteLevel': 4095}, template=bytes(old)), 5736)[0] == 4095
    with pytest.raises(ValueError, match='beyond'):
        build_setup({'OpticalFilter': 'ND2'}, template=bytes(old))   # 8320..9344 > 9000: absent


def test_long_description_keeps_mapping_phrases():
    from phantastic.decimate import append_note, DESCRIPTION_BYTES
    desc = 'x' * 4000 + ' Image k = camera image k*10+0 (a cine numbers images consecutively).'
    out = append_note(desc, 'Phantastic: ... image k = source image k*2+0; ...')
    assert len(out) <= DESCRIPTION_BYTES
    assert out.index('camera image k*10+0') < out.index('source image k*2+0')   # oldest first
