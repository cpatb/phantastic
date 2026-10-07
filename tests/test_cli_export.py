"""Command-line parity: --crop on download/decimate/tiff/mp4, tiff --sequence, mp4, download --all."""
import json

import numpy as np
import pytest
import tifffile

from phantastic.cine import CineReader
from phantastic.cli import main
from phantastic.export import find_ffmpeg
from phantastic.simulator import CameraModel, Simulator, synthetic_frame

from test_export import FIRST, N, W, make_cine, pattern


def test_cli_decimate_and_tiff_crop(tmp_path):
    src = make_cine(tmp_path / 's.cine')
    assert main(['decimate', str(src), str(tmp_path / 'd.cine'), '--step', '1', '--crop', '2,3,5,4']) == 0
    with CineReader(tmp_path / 'd.cine') as r:
        assert np.array_equal(r.read(0), pattern(0)[3:7, 2:7])
    assert main(['tiff', str(src), str(tmp_path / 't.tif'), '--crop', '0,0,1,21']) == 0
    assert np.array_equal(tifffile.imread(tmp_path / 't.tif')[:, :, 0], np.stack([pattern(k)[:, 0] for k in range(N)]))
    # out of range: refused with exit code 4 and nothing written
    assert main(['tiff', str(src), str(tmp_path / 'bad.tif'), '--crop', f'0,0,{W + 1},1']) == 4
    assert not list(tmp_path.glob('bad.tif*'))


def test_cli_tiff_sequence(tmp_path, capsys):
    src = make_cine(tmp_path / 'shot.cine')
    out = tmp_path / 'seq'
    assert main(['tiff', str(src), str(out), '--sequence', '--first', '-1', '--count', '3',
                 '--pattern', 'f_{image:04d}']) == 0
    assert sorted(p.name for p in out.iterdir()) == ['f_0000.tif', 'f_0001.tif', 'f_m0001.tif', 'shot_sequence.json']
    assert np.array_equal(tifffile.imread(out / 'f_m0001.tif'), pattern(-1 - FIRST))
    assert main(['tiff', str(src), str(out), '--sequence', '--first', '-1', '--count', '3',
                 '--pattern', 'f_{image:04d}']) == 4                                  # exists: refused
    assert 'already exist' in capsys.readouterr().err


@pytest.mark.skipif(find_ffmpeg() is None, reason='no ffmpeg with libx264')
def test_cli_mp4(tmp_path):
    src = make_cine(tmp_path / 's.cine')
    assert main(['mp4', str(src), str(tmp_path / 'm.mp4'), '--fps', '12', '--white', '900', '--crop', '0,0,20,10',
                 '--border', '--rotate', '180']) == 0
    side = json.loads((tmp_path / 'm.mp4.json').read_text())
    assert side['movie_fps'] == 12 and side['rotate_cw_deg'] == 180 and side['crop']['w'] == 20
    assert side['display_curve'] == {'black': 0.0, 'white': 900.0, 'gamma': 1.0}
    assert side['processing'].startswith('8-bit display render')


@pytest.fixture()
def sim():
    s = Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0, model=CameraModel(width=64, height=32)).start()
    yield s
    s.stop()


def test_cli_download_crop_and_all(sim, tmp_path):
    cam = ['--ip', '127.0.0.1', '--port', str(sim.port), '--timeout', '5']
    from phantastic.camera import Camera
    with Camera('127.0.0.1', sim.port, timeout=5) as c:
        c.partition(2)
        for cine in (1, 2):
            c.command('set defc {frcount:12, ptframes:4}')
            c.record(cine)
            c.trigger()
            c.wait_stored(cine, timeout=2)
    assert main(['download', *cam, '--cine', '2', '--out', str(tmp_path / 'x_{cinenr}.cine'), '--crop', '1,2,8,3',
                 '--as-12bit']) == 0
    with CineReader(tmp_path / 'x_2.cine') as r:
        assert (r.width, r.height, len(r)) == (8, 3, 12)
        assert np.array_equal(r.read(0), synthetic_frame(-8, 64, 32, seed=2)[2:5, 1:9])
    assert main(['download', *cam, '--all', '--out-dir', str(tmp_path / 'all'), '--name', 'run_{cinenr2}',
                 '--as-12bit']) == 0
    assert sorted(p.name for p in (tmp_path / 'all').iterdir()) == ['run_01.cine', 'run_02.cine']
    with pytest.raises(SystemExit):
        main(['download', *cam, '--all', '--cine', '1', '--out-dir', str(tmp_path)])
