"""GUI paths of the export tools against the simulated camera: export from camera RAM, crop,
file-name tokens in Save Cine, Save All RAM Cines, TIFF sequence and MP4 dialogs.

Anchor outside the GUI: the simulator's ``synthetic_frame`` (P16 = frame << 4, seeded by cine).
"""
import json

import numpy as np
import pytest

pytest.importorskip('PySide6')
import tifffile  # noqa: E402

from phantastic.cine import CineReader  # noqa: E402
from phantastic.export import find_ffmpeg  # noqa: E402
from phantastic.simulator import synthetic_frame  # noqa: E402

from test_gui_smoke import H, SERIAL, W, app, connect_and_record, wait_until, win  # noqa: E402,F401


def run(dlg, what):
    dlg.start_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, what=what)
    assert dlg.error is None, dlg.summary.toPlainText()
    return dlg.result_info


def open_camera_cine(win):  # noqa: F811 (pytest fixture from test_gui_smoke)
    connect_and_record(win)
    win.manager_tab.tree.itemDoubleClicked.emit(win.manager_tab.find(('cine', 1)), 0)
    panel = win.play_tab.panel
    wait_until(lambda: panel.frame is not None, what='first camera frame')
    return panel


def test_export_tiff_and_sequence_from_camera_ram(win, tmp_path):  # noqa: F811 (pytest fixture from test_gui_smoke)
    panel = open_camera_cine(win)
    win.play_tab.tiff_raw_action.trigger()                       # Save Cine ▾ > Export TIFF (raw values)
    dlg = win._last_dialog
    assert dlg.camera is not None and dlg.kind == 'tiff' and dlg.as12_check.isChecked()
    assert not dlg.values_combo.model().item(1).isEnabled()      # PCC table needs a file
    dlg.range.set_values(first=-3, last=2)
    assert not dlg.crop_widget.rect_check.isEnabled()            # this panel offers no rectangle
    dlg.crop_widget.set_crop((5, 0, 20, 6))
    dlg.path_edit.setText(str(tmp_path / 'ram.tif'))
    res = run(dlg, 'camera tiff')
    pages = tifffile.imread(tmp_path / 'ram.tif')
    assert pages.shape == (6, 6, 20) and res['crop'] == (5, 0, 20, 6)
    for k, n in enumerate(range(-3, 3)):
        assert np.array_equal(pages[k], synthetic_frame(n, W, H)[0:6, 5:25])      # 12-bit: P16 >> 4
    meta = json.loads((tmp_path / 'ram.tif.json').read_text())
    assert meta['image_numbers'] == list(range(-3, 3)) and meta['crop']['x'] == 5
    assert 'Cropped' in dlg.summary.toPlainText()

    # a panel that offers a crop rectangle (the parallel crop UI's hook) enables 'Crop to rectangle'
    panel.crop_rect = lambda: (2, 3, 4, 5)
    win.play_tab.tiff_seq_action.trigger()
    seq = win._last_dialog
    assert seq.kind == 'tiffseq' and seq.crop_widget.rect_check.isEnabled()
    seq.crop_widget.rect_check.setChecked(True)
    seq.range.set_values(first=-1, last=1)
    seq.path_edit.setText(str(tmp_path / 'seq'))
    assert seq.pattern_preview.text() == f'cine1_{SERIAL}_m000001.tif .. cine1_{SERIAL}_000001.tif'
    run(seq, 'camera sequence')
    for n in (-1, 0, 1):
        name = f'cine1_{SERIAL}_{"m" if n < 0 else ""}{abs(n):06d}.tif'
        assert np.array_equal(tifffile.imread(tmp_path / 'seq' / name), synthetic_frame(n, W, H)[3:8, 2:6])


@pytest.mark.skipif(find_ffmpeg() is None, reason='no ffmpeg with libx264')
def test_export_mp4_from_camera_ram(win, tmp_path):  # noqa: F811 (pytest fixture from test_gui_smoke)
    panel = open_camera_cine(win)
    assert win.play_tab.mp4_action.isEnabled()
    win.play_tab.mp4_action.trigger()
    dlg = win._last_dialog
    assert dlg.kind == 'mp4' and (dlg.black_spin.value(), dlg.white_spin.value()) == (panel.lo, panel.hi)
    assert 'NOT FOR MEASUREMENT' in dlg.note.text()
    dlg.range.set_values(first=0, last=9)
    dlg.fps_spin.setValue(10)
    dlg.border_check.setChecked(True)
    dlg.path_edit.setText(str(tmp_path / 'cam.mp4'))
    res = run(dlg, 'camera mp4')
    assert res['count'] == 10 and res['fps'] == 10 and res['border']
    side = json.loads((tmp_path / 'cam.mp4.json').read_text())
    assert side['display_curve']['black'] == panel.lo and side['image_numbers'] == list(range(10))


def test_save_cine_template_crop_and_no_overwrite(win, tmp_path):  # noqa: F811 (pytest fixture from test_gui_smoke)
    live = connect_and_record(win)
    dlg = live.make_save_dialog(1, default_dir=str(tmp_path))
    assert dlg.path_edit.text() == str(tmp_path / 'cine{cinenr}_{serial}.cine')     # the template
    assert dlg.actual_label.text() == str(tmp_path / f'cine1_{SERIAL}.cine')          # today's default name
    dlg.range.set_values(first=-4, last=5)
    dlg.crop_widget.set_crop((0, 0, 16, 8))
    run(dlg, 'save 1')
    with CineReader(tmp_path / f'cine1_{SERIAL}.cine') as r:
        assert (r.width, r.height, len(r)) == (16, 8, 10)
        assert np.array_equal(r.read(4), synthetic_frame(0, W, H)[:8, :16])
        assert f'Cropped from {W}x{H} at 0,0 to 16x8' in r.setup['Description']
    dlg2 = live.make_save_dialog(1, default_dir=str(tmp_path))
    dlg2.range.set_values(first=0, last=0)
    assert dlg2.actual_label.text().endswith(f'cine1_{SERIAL}_1.cine')               # never overwrites
    run(dlg2, 'save 2')
    assert (tmp_path / f'cine1_{SERIAL}_1.cine').exists()
    dlg3 = live.make_save_dialog(1, default_dir=str(tmp_path))
    dlg3.path_edit.setText(str(tmp_path / '{bogus}.cine'))
    assert 'unknown token' in dlg3.actual_label.text()
    dlg3.start_btn.click()
    assert 'unknown token' in dlg3.summary.toPlainText() and dlg3.task is None
    dlg3.path_edit.setText(str(tmp_path / 'p10.cine'))
    dlg3.set_format('P10')
    dlg3.crop_widget.set_crop((0, 0, 6, 4))                     # 6 px: not whole 4-pixel P10 groups
    dlg3.start_btn.click()
    assert 'multiple of 4' in dlg3.summary.toPlainText() and not (tmp_path / 'p10.cine').exists()


def test_save_all_ram_cines(win, tmp_path):  # noqa: F811 (pytest fixture from test_gui_smoke)
    connect_and_record(win)
    dlg = win.save_all()
    assert dlg is not None and list(dlg.infos) == [1]
    dlg.path_edit.setText(str(tmp_path / 'all'))
    dlg.template_edit.setText('shot_{cinenr2}')
    assert dlg.preview.text().startswith('shot_01.cine')
    res = run(dlg, 'save all')
    assert [r['cine'] for r in res] == [1]
    with CineReader(tmp_path / 'all' / 'shot_01.cine') as r:
        assert len(r) == 1000 and np.array_equal(r.read(0), synthetic_frame(r.first, W, H))
