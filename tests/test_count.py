"""'First image + number of frames' ranges (decimate.last_for_count, CLI --count)."""
import numpy as np

from phantastic.cine import CineReader, CineWriter
from phantastic.cli import main
from phantastic.decimate import last_for_count, select_numbers


def test_count_property_random():
    rng = np.random.default_rng(7)
    for _ in range(2000):
        first = int(rng.integers(-100000, 100000))
        count = int(rng.integers(1, 500))
        step = int(rng.integers(1, 1000))
        align = ('trigger', 'first')[int(rng.integers(2))]
        last = last_for_count(first, count, step, align)
        kept = select_numbers(first, last, step, align)
        assert len(kept) == count and kept[-1] == last            # exactly n, ending on the n-th
        assert kept[0] >= first and kept[0] - first < step        # starts at (or just after) first
        if align == 'first':
            assert kept[0] == first
        else:
            assert kept[0] % step == 0


def test_cli_count(tmp_path):
    src = tmp_path / 's.cine'
    with CineWriter(src, 4, 2, 100, 'mono16', first_image_no=-50, setup_fields={'FrameRate': 1000}) as w:
        for k in range(100):
            w.append(np.full((2, 4), k, np.uint16), time=(0, k << 20), exposure=1)
    out = tmp_path / 'c.cine'
    assert main(['decimate', str(src), str(out), '--step', '1', '--first', '-10', '--count', '7']) == 0
    with CineReader(out) as r:
        assert r.image_numbers.tolist() == list(range(-10, -3))
        assert int(r.read(0)[0, 0]) == 40                          # source image -10 holds value 40
    out2 = tmp_path / 'c2.cine'
    assert main(['decimate', str(src), str(out2), '--step', '5', '--first', '-12', '--count', '4']) == 0
    with CineReader(out2) as r:                                    # multiples of 5 from -12: -10 -5 0 5
        assert [int(r.read(k)[0, 0]) - 50 for k in range(len(r))] == [-10, -5, 0, 5]
