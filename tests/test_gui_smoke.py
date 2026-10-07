"""Offscreen smoke test of the PCC-style GUI against the simulated camera, through the GUI's own code paths.

Anchors that do not pass through the GUI: the simulator's camera model state (what the camera
was actually told), ``synthetic_frame`` (what the camera's pixels are), and the frame-number
ramp the simulator writes into row 0 of every frame (number mod 4096; P16 carries it << 4).
"""
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if os.name == 'nt':                       # the offscreen platform finds no system fonts by itself
    os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

import numpy as np
import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from phantastic import protocol as P
from phantastic.cine import CineReader
from phantastic.decimate import select_numbers
from phantastic.gui.main_window import MainWindow
from phantastic.gui.panels import PlaybackPanel
from phantastic.gui.workers import Cancelled, download_cine
from phantastic.simulator import synthetic_frame

W, H, RATE, EXP_US, PT = 128, 64, 20000, 40.0, 50
FRCOUNT = 1000   # simulator default (CameraModel.defc['frcount'])
SERIAL = 99001   # simulator default


def p16(number):
    """What a P16 download of camera image ``number`` holds: the 12-bit frame, MSB-aligned on the wire."""
    return synthetic_frame(number, W, H, seed=1) << 4


def wait_until(cond, timeout=15.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f'timed out waiting for {what}')


@pytest.fixture(scope='module')
def app():
    a = QApplication.instance() or QApplication([])
    if os.name == 'nt':
        from PySide6.QtGui import QFont
        a.setFont(QFont('Segoe UI', 9))    # what a Windows session uses; the layout is sized for it
    return a


@pytest.fixture()
def win(app):
    w = MainWindow()
    w.resize(1456, 868)
    w.show()
    yield w
    w.close()


def hover(view, x, y):
    pos = view.widget_point(x, y)
    ev = QMouseEvent(QEvent.Type.MouseMove, pos, view.mapToGlobal(pos), Qt.MouseButton.NoButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view, ev)


def type_goto(play, n):
    """Type an image number into 'To #' and press Enter, as a user does."""
    le = play.goto_spin.lineEdit()
    le.setFocus()
    le.selectAll()
    QTest.keyClicks(le, str(n))
    QTest.keyClick(le, Qt.Key.Key_Return)


def states(win):
    return win.live_tab.cine_states


def flags_of(win, key):
    return getattr(states(win).get(key), 'names', ())


def connect_and_record(win):
    win.manager_tab.add_sim_btn.click()                 # Manager tab '+S' (Add Simulated Camera)
    wait_until(lambda: win.session is not None, what='connect')
    live = win.live_tab
    live.set_resolution(W, H)
    live.set_rate(RATE)
    live.exp_spin.setValue(EXP_US)
    live.pt_spin.setValue(PT)
    live.apply_btn.click()
    wait_until(lambda: live.last_readback is not None, what='settings read-back')
    rb = live.last_readback
    assert rb['res'] == P.Resolution(W, H) and rb['rate'] == RATE and rb['ptframes'] == PT
    assert rb['exp'] == 40000                                   # µs in the form, ns on the wire
    defc = win.simulator.model.defc                              # what the camera was actually told
    assert defc['exp'] == 40000 and defc['res'] == P.Resolution(W, H) and defc['rate'] == RATE
    assert 'exposure 40 µs (40000 ns)' in live.readback_label.text()
    assert live.exp_spin.value() == pytest.approx(EXP_US)
    # Image Range and Trigger Position lines from frcount / ptframes / rate
    assert live.duration_label.text() == f'Duration: {FRCOUNT / RATE:.3f}s ({FRCOUNT}p)'
    assert live.pretrig_label.text() == f'Pretrigger Time: {-(FRCOUNT - PT) / RATE:.3f}s ({PT - FRCOUNT}p)'
    # Trigger is grey until a cine waits for a trigger
    wait_until(lambda: live.cine_combo.findData(1) >= 0, what='cine list')
    assert not live.trigger_btn.isEnabled() and live.capture_btn.text() == 'Capture'
    live.select_cine(1)
    live.capture_btn.click()
    wait_until(lambda: 'WTR' in flags_of(win, 'c1'), what='WTR')
    assert live.capture_btn.text() == 'Abort Recording' and live.trigger_btn.isEnabled()
    assert win.simulator.model.received.count('rec 1') == 1
    live.trigger_btn.click()
    wait_until(lambda: 'STR' in flags_of(win, 'c1') and 1 in live.cine_infos, what='STR')
    ci = live.cine_infos[1]
    assert (ci['firstfr'], ci['lastfr'], ci['frcount']) == (PT - FRCOUNT, PT - 1, FRCOUNT)
    # the cine table now lives in the Live 'Cine' combo, the Manager tree and the Play 'Cine:' combo
    wait_until(lambda: 'STR' in live.cine_combo.itemText(live.cine_combo.findData(1)), what='cine combo')
    assert win.manager_tab.find(('cine', 1)) is not None
    assert win.play_tab.cine_combo.itemText(win.play_tab.index_of(('camera', 1))) == f'{SERIAL} > Cine 1'
    return live


def test_layout_is_pcc(win):
    assert win.windowTitle() == 'Phantastic'
    assert win.menuBar().actions() == []                              # PCC has no text menu bar
    tabs = win.control_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ['Live', 'Play', 'Manager']
    assert tabs.currentWidget() is win.manager_tab                    # Manager selected at start
    assert win.dockWidgetArea(win.dock) == Qt.DockWidgetArea.RightDockWidgetArea
    assert abs(win.dock.width() - 260) <= 20
    m = win.manager_tab
    assert [m.tree.topLevelItem(i).text(0) for i in range(2)] == ['Cameras', 'Files']
    tips = [a.toolTip() for a in win.toolbar.actions() if a.toolTip()]
    for name in ('Cursor', 'Pan', 'Zoom Actual Size', 'Zoom Fit', 'Image Tools (Ctrl+I)', 'Open File (Ctrl+O)',
                 'Window Tile', 'Window Auto Tile', 'CrossHair', 'Grid Display'):
        assert name in tips, name
    assert win.autotile_action.isChecked()
    for seq in ('Ctrl+R', 'Ctrl+T', 'Ctrl+S'):
        assert any(a.shortcut().toString() == seq for a in win.actions()), seq


def test_gui_end_to_end(win, tmp_path):
    live = connect_and_record(win)
    wait_until(lambda: win.preview is not None and win.preview.frames_received >= 3, what='live frames')
    assert win.preview.view.image_size == (W, H)                     # live view runs in the background
    assert not live.csr_btn.isEnabled() and 'bref' in live.csr_btn.toolTip()   # the simulator has no bref

    # -- Abort Recording: the simulator re-armed cine 2 after the trigger; Abort sends rec 0
    assert live.capture_btn.text() == 'Abort Recording'
    live.capture_btn.click()
    wait_until(lambda: not live.recording, what='abort')
    assert 'rec 0' in win.simulator.model.received
    assert 'RDY' in win.simulator.model.cines[2]['state']
    assert live.capture_btn.text() == 'Capture' and not live.trigger_btn.isEnabled()

    # -- Save Cine dialog, Range Option 'Full cine': decimate by 10, multiples of N relative to trigger, P16
    out = tmp_path / 'dec10.cine'
    dlg = live.make_save_dialog(1)
    assert dlg.windowTitle() == 'Save Cine' and dlg.range.option() == 'marks'
    dlg.range.set_option('full')
    dlg.range.set_values(step=10, align='trigger')
    assert dlg.range.option() == 'full' and dlg.range.values()[:2] == (PT - FRCOUNT, PT - 1)
    dlg.set_format('P16')
    dlg.path_edit.setText(str(out))
    dlg.start_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, what='download')
    assert dlg.error is None, dlg.summary.toPlainText()
    res = dlg.result_info
    assert 'Image-number mapping: Image k = camera image k*10+0' in dlg.summary.toPlainText()
    assert not (tmp_path / 'dec10.cine.part').exists()
    r = CineReader(out)
    try:
        cam_nums = np.arange(PT - FRCOUNT, PT, 10)                     # -950 .. 40, the multiples of 10
        assert len(r) == len(cam_nums) == res['count'] == 100
        assert r.first == -95 and r.image_numbers.tolist() == (cam_nums // 10).tolist()
        for k in (0, 50, 99):
            assert np.array_equal(r.read(k), p16(int(cam_nums[k])) >> 4)   # Save Cine defaults to PCC's 12-bit
        rel = r.relative_times()
        assert np.allclose(rel, cam_nums / RATE, atol=2e-6)          # true times, trigger at 0
    finally:
        r.close()

    # -- file playback: open, 'To #', Frame Info, raw readout in the status bar
    assert win.open_file(out)
    panel = win.play_tab.panel
    assert isinstance(panel, PlaybackPanel) and panel.source.kind == 'file'
    assert win.control_tabs.currentWidget() is win.play_tab
    assert win.manager_tab.find(('file', str(out))) is not None
    i = 57
    n = -95 + i                                                       # file image number
    type_goto(win.play_tab, n)
    assert panel.shown == n and win.play_tab.editor.cur == n
    fi = win.play_tab.fi
    assert fi['image'].text() == f'{n} (camera image {10 * n})'
    with CineReader(out) as ref:
        t = ref.relative_times()[i]
        raw = ref.read(i)
    assert fi['elapsed'].text() == f'{t * 1e6:.2f} µs' and t == pytest.approx(10 * n / RATE, abs=2e-6)
    assert fi['exposure'].text() == '40 µs'
    assert np.array_equal(panel.frame, raw)
    for (x, y) in ((37, 21), (5, 0), (W - 1, H - 1)):
        hover(panel.view, x, y)
        assert panel.last_readout == (x, y, int(raw[y, x]))
        assert win.xy_label.text() == f'X: {x + 1} Y: {y + 1}'          # 1-based like PCC
        assert win.value_label.text() == f'Value: {int(raw[y, x])}'
    hover(panel.view, 5, 0)
    assert panel.last_readout[2] == (10 * n) % 4096                         # simulator's row-0 ramp, 12-bit file

    # -- Image Tools: display only. Auto changes the display, never the raw frame; flips keep the readout honest
    tools = win.show_image_tools()
    assert tools.windowTitle() == 'Image Tools' and 'never changed' in tools.note_label.text()
    before = panel.frame.copy()
    tools.auto_btn.click()
    lo, hi = tools.min_spin.value(), tools.max_spin.value()
    assert (lo, hi) == (panel.lo, panel.hi) and lo < hi and np.array_equal(panel.frame, before)
    assert lo == pytest.approx(np.percentile(raw, 0.175), abs=1) and hi == pytest.approx(np.percentile(raw, 99.825), abs=1)
    assert tools.avg_label.text() == f'Avg: {raw.mean():.2f}'
    tools.full_btn.click()
    assert (panel.lo, panel.hi) == (0, 4095)                              # full range of a 12-bit file
    tools.flip_h.setChecked(True)
    hover(panel.view, 37, 21)
    assert panel.last_readout == (37, 21, int(raw[21, 37])) and np.array_equal(panel.frame, before)
    tools.flip_h.setChecked(False)
    win.snapshot_dir = tmp_path / 'snaps'
    snap = win.snapshot()                                            # Ctrl+N: the display image as PNG
    from PySide6.QtGui import QImage
    assert snap is not None and snap.exists() and QImage(str(snap)).size().toTuple() == (W, H)

    # -- Batch export from the toolbar: decimated cine and TIFF
    dec = win.export('cine')
    assert dec.range.option() == 'marks' and dec.range.values()[:2] == (-95, 4)   # marks default to the whole file
    dec.range.set_values(step=2, align='trigger')
    dec.path_edit.setText(str(tmp_path / 'dec20.cine'))
    dec.start_btn.click()
    wait_until(lambda: dec.result_info is not None or dec.error is not None, what='export cine')
    assert dec.error is None, dec.summary.toPlainText()
    with CineReader(tmp_path / 'dec20.cine') as r2:
        assert len(r2) == 50 and r2.first == -47                      # file images -94..4 step 2 -> -47..2
        assert np.array_equal(r2.read(10), p16(20 * (-47 + 10)) >> 4)   # exported from the 12-bit save
    assert 'Image k = source image k*2+0' in dec.summary.toPlainText()
    assert win.open_file(tmp_path / 'dec20.cine')                     # twice decimated: mappings compose
    p2 = win.play_tab.panel
    p2.goto(-37)
    assert win.play_tab.fi['image'].text() == '-37 (camera image -740)'
    with CineReader(tmp_path / 'dec20.cine') as r2:
        hover(p2.view, 3, 0)
        assert p2.last_readout[2] == (-740) % 4096 == int(r2.read(10)[0, 3])
    assert win.open_file(out)                                         # already open: activates its panel
    assert win.play_tab.panel is panel

    import tifffile
    win.play_tab.tiff_raw_action.trigger()                            # Save Cine ▾ > Export TIFF (raw values)
    tif = win._last_dialog
    assert tif.kind == 'tiff' and tif.values_combo.currentData() == 'raw'
    tif.range.set_values(first=-5, last=4, step=1)
    tif.path_edit.setText(str(tmp_path / 'stack.tif'))
    tif.start_btn.click()
    wait_until(lambda: tif.result_info is not None or tif.error is not None, what='export tiff')
    assert tif.error is None, tif.summary.toPlainText()
    pages = tifffile.imread(tmp_path / 'stack.tif')
    assert pages.shape == (10, H, W) and pages.dtype == np.uint16
    assert np.array_equal(pages[3], p16(10 * (-5 + 3)) >> 4)
    assert (tmp_path / 'stack.tif.json').exists() and not (tmp_path / 'stack.tif.part.json').exists()

    # -- camera errors reach the status bar verbatim
    live._run('Query', lambda cam: cam.get('defc.nonsense'))
    wait_until(lambda: win.last_error is not None, what='error report')
    assert 'Camera error' in win.last_error and 'is unknown' in win.last_error
    assert win.statusBar().currentMessage() == win.last_error


def test_play_camera_cine_marks_and_save(win, tmp_path):
    """PCC's workflow: review the camera cine in Play before saving, mark in/out, Save Cine."""
    connect_and_record(win)
    item = win.manager_tab.find(('cine', 1))
    win.manager_tab.tree.itemDoubleClicked.emit(item, 0)              # double-click 'Cine 1' in the Manager tree
    play = win.play_tab
    panel = play.panel
    assert panel is not None and panel.source.kind == 'camera' and panel.source.title == f'{SERIAL} > Cine 1'
    assert win.playbacks[('camera', 1)].windowTitle() == f'{SERIAL} > Cine 1'
    wait_until(lambda: panel.frame is not None, what='first camera frame')
    assert panel.shown == 0 and np.array_equal(panel.frame, p16(0))  # opens at the trigger image
    assert not play.tiff_raw_action.isEnabled() and not play.tiff_pcc_action.isEnabled()

    def show(n):
        wait_until(lambda: panel.shown == n and panel.frame is not None, what=f'image {n}')
        assert np.array_equal(panel.frame, p16(n))                     # fetched with img, P16 as sent

    type_goto(play, -123)
    show(-123)
    elapsed = float(play.fi['elapsed'].text().split()[0])
    assert elapsed == pytest.approx(-123 / RATE * 1e6, abs=2)          # from the camera's time stamps (1 µs)
    assert play.fi['exposure'].text() == '40 µs' and play.fi['image'].text() == '-123'
    play.stepf_btn.click()
    show(-122)
    play.stepb_btn.click()
    show(-123)
    play.ffwd_btn.click()                                              # 1000 frames: fast step = max(10, 1)
    show(-113)
    play.frew_btn.click()
    show(-123)
    play.trig_btn.click()
    show(0)
    play.start_btn.click()
    show(PT - FRCOUNT)
    play.end_btn.click()
    show(PT - 1)
    for y, x in ((10, 20), (0, 7)):
        hover(panel.view, x, y)
        assert panel.last_readout == (x, y, int(p16(PT - 1)[y, x]))
        assert win.value_label.text() == f'Value: {int(p16(PT - 1)[y, x])}'

    # Mark-In / Mark-Out with [ and ]
    type_goto(play, -603)
    show(-603)
    play.markin_btn.click()
    type_goto(play, -397)
    show(-397)
    play.markout_btn.click()
    assert (panel.mark_in, panel.mark_out) == (-603, -397)
    assert (play.editor.mark_in, play.editor.mark_out) == (-603, -397)
    play.start_btn.click()                                             # Limit to Range: start = Mark-In
    show(-603)

    # 'To #' keeps focus after Enter; scrubbing the editor bar must still be what ] marks
    type_goto(play, -500)
    show(-500)
    play.editor.scrubbed.emit(-450)
    show(-450)
    assert play.goto_spin.value() == -450
    play.goto_spin.lineEdit().editingFinished.emit()                  # focus-out of the box must not jump back
    assert panel.cur == -450
    play.markout_btn.click()
    assert panel.mark_out == -450
    play.goto_spin.lineEdit().setFocus()
    type_goto(play, -397)
    show(-397)
    play.markout_btn.click()
    assert (panel.mark_in, panel.mark_out) == (-603, -397)

    # a fetch refused because the camera is busy (a download holds the lock) leaves the panel usable
    assert win.session.lock.acquire(timeout=5)
    try:
        play.stepb_btn.click()                       # -398: inside the marks and never fetched, so not cached
        assert panel.cur == -398
        wait_until(lambda: 'Camera busy' in win.statusBar().currentMessage(), timeout=10, what='busy report')
        assert panel.cur == panel.shown == -397 and 'loading' not in play.fi['image'].text()
    finally:
        win.session.lock.release()
    play.stepb_btn.click()
    show(-398)
    play.goto_spin.lineEdit().setFocus()
    type_goto(play, -603)
    show(-603)

    # Save Cine... defaults to Range Option [Mark In, Mark Out]
    play.save_btn.click()
    dlg = win._last_dialog
    assert dlg.windowTitle() == 'Save Cine' and dlg.cine == 1
    assert dlg.range.option() == 'marks' and dlg.range.values()[:2] == (-603, -397)
    assert not dlg.range.first_spin.isEnabled()                        # only User Defined enables first/last
    dlg.range.set_values(step=10, align='trigger')                    # 'Decimate by' 10
    assert dlg.range.option() == 'marks'
    dlg.set_format('P16')
    out = tmp_path / 'marked.cine'
    dlg.path_edit.setText(str(out))
    dlg.start_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, what='marked download')
    assert dlg.error is None, dlg.summary.toPlainText()
    cam_nums = select_numbers(-603, -397, 10, 'trigger')
    assert cam_nums.tolist() == list(range(-600, -399, 10))           # multiples of 10 inside the marks
    with CineReader(out) as r:
        assert len(r) == 21 and r.image_numbers.tolist() == (cam_nums // 10).tolist()
        for k in (0, 10, 20):
            assert np.array_equal(r.read(k), p16(int(cam_nums[k])) >> 4)
        assert np.allclose(r.relative_times(), cam_nums / RATE, atol=2e-6)


def test_playback_loop_options(win, tmp_path):
    from phantastic.cine import CineWriter
    src = tmp_path / 'loop.cine'
    with CineWriter(src, 8, 4, 10, 'mono16', first_image_no=-5, setup_fields={'FrameRate': 100}) as w:
        for k in range(10):
            w.append(np.full((4, 8), k, np.uint16), time=(k, 0), exposure=1)
    assert win.open_file(src)
    p, play = win.play_tab.panel, win.play_tab
    p.goto(-3)
    play.markin_btn.click()
    p.goto(-1)
    play.markout_btn.click()
    assert play.limit_check.isChecked() and play.repeat_check.isChecked() and not play.pingpong_check.isChecked()
    p.direction = 1
    p._tick()
    assert p.cur == -3                                   # Repeat + Limit to Range: wraps to Mark-In
    assert int(p.frame[0, 0]) == 2                       # image -3 is the third stored image
    play.pingpong_check.setChecked(True)
    p.goto(-1)
    p._tick()
    assert p.direction == -1 and p.cur == -2             # Ping Pong: turns round at Mark-Out
    play.pingpong_check.setChecked(False)
    play.repeat_check.setChecked(False)
    play.limit_check.setChecked(False)
    p.goto(4)
    p.play(1)
    p._tick()
    assert p.cur == 4 and p.direction == 0               # no repeat: stops at the end
    play.step_spin.setValue(3)
    p.goto(-5)
    play.stepf_btn.click()
    assert p.cur == -2                                   # Player Step


def test_readout_matches_painted_pixel_with_flips(win, tmp_path):
    from phantastic.cine import CineWriter
    src = tmp_path / 'pattern.cine'
    w_, h_ = 8, 4
    pattern = (np.arange(w_)[None, :] * 20 + np.arange(h_)[:, None] * 3 + 10).astype(np.uint16)   # all distinct, < 256
    with CineWriter(src, w_, h_, 1, 'mono16', setup_fields={'FrameRate': 100, 'RealBPP': 8}) as w:
        w.append(pattern, time=(0, 0), exposure=1)
    assert win.open_file(src)
    panel = win.play_tab.panel
    panel.set_display_range(0, 255)                     # display value = raw value
    win.mdi.activeSubWindow().showMaximized()
    QApplication.processEvents()
    view = panel.view
    r = view.target_rect()
    s = r.width() / w_
    pos = r.topLeft() + type(r.topLeft())(1.5 * s, 2.5 * s)          # centre of the 2nd column, 3rd row ON SCREEN
    for flip_h, flip_v, want in ((False, False, (1, 2)), (True, False, (w_ - 2, 2)), (False, True, (1, h_ - 3)),
                                 (True, True, (w_ - 2, h_ - 3))):
        view.flip_h, view.flip_v = flip_h, flip_v
        view.update()
        QApplication.processEvents()
        ev = QMouseEvent(QEvent.Type.MouseMove, pos, view.mapToGlobal(pos.toPoint()), Qt.MouseButton.NoButton,
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view, ev)
        x, y, v = panel.last_readout
        assert (x, y) == want and v == int(pattern[y, x])
        painted = view.grab().toImage().pixelColor(pos.toPoint())
        assert painted.red() == painted.green() == painted.blue() == v, (flip_h, flip_v)


class _CancelAfter:
    """Stand-in for a Task that cancels after ``n`` frames (deterministic mid-download cancel)."""
    def __init__(self, n):
        self.n, self.seen = n, 0

    def check_cancelled(self):
        pass

    def progress(self, done, total):
        self.seen = done
        if done >= self.n:
            raise Cancelled()


def test_cancel_deletes_partial_and_camera_stays_usable(win, tmp_path):
    connect_and_record(win)
    wait_until(lambda: win.preview is not None, what='preview')
    win.preview.pause()
    out = tmp_path / 'cancel.cine'
    with pytest.raises(Cancelled) as ei:
        download_cine(win.session, 1, out, PT - FRCOUNT, PT - 1, 1, 'trigger', 'P16', _CancelAfter(70))
    # the library removes its own partial file on cancel; the GUI then finds nothing left to delete
    assert ei.value.partial_deleted in ([], [str(tmp_path / 'cancel.cine.part')])
    assert not out.exists() and not (tmp_path / 'cancel.cine.part').exists()
    # cancelled mid-run (chunk 64, so inside the second img run); the data stream must still be in step
    ok = tmp_path / 'after.cine'
    res = download_cine(win.session, 1, ok, -300, 299, 100, 'trigger', 'P16', _CancelAfter(10 ** 9))
    assert res['count'] == 4
    with CineReader(ok) as r:
        assert r.image_numbers.tolist() == [-3, -2, -1, 0]
        assert all(np.array_equal(r.read(k), p16(100 * n))
                   for k, n in enumerate(r.image_numbers.tolist()))


def test_dialog_cancel_reports_and_leaves_nothing(win, tmp_path):
    live = connect_and_record(win)
    out = tmp_path / 'c.cine'
    dlg = live.make_save_dialog(1)
    dlg.path_edit.setText(str(out))
    dlg.start_btn.click()
    dlg.cancel_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, what='download end')
    if dlg.error is not None:                       # usual case: cancelled
        assert isinstance(dlg.error, Cancelled)
        assert dlg.summary.toPlainText().startswith('Cancelled.')
        assert not out.exists()
    assert not (tmp_path / 'c.cine.part').exists()


def test_decimate_progress_callback(tmp_path):
    from phantastic.cine import CineWriter
    from phantastic.decimate import decimate_cine
    src = tmp_path / 's.cine'
    with CineWriter(src, 8, 4, 20, 'mono16', first_image_no=-10, setup_fields={'FrameRate': 100}) as w:
        for k in range(20):
            w.append(np.full((4, 8), k, np.uint16), time=(k, 0), exposure=1)
    calls = []
    decimate_cine(src, tmp_path / 'd.cine', 5, progress=lambda d, t: calls.append((d, t)))
    assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]
    with CineReader(tmp_path / 'd.cine') as r:
        assert r.image_numbers.tolist() == [-2, -1, 0, 1] and int(r.read(2)[0, 0]) == 10


def test_save_dialog_12bit_option(win, tmp_path):
    live = connect_and_record(win)
    dlg = live.make_save_dialog(1)
    dlg.set_format('P10')
    assert not dlg.as12_check.isEnabled()                         # only meaningful for P16/P16R
    dlg.set_format('P16')
    assert dlg.as12_check.isEnabled() and dlg.as12_check.isChecked()   # PCC-compatible by default
    dlg.range.set_values(first=-20, last=9, step=1)
    assert dlg.range.option() == 'user'
    out = tmp_path / 'as12.cine'
    dlg.path_edit.setText(str(out))
    dlg.start_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, what='12-bit download')
    assert dlg.error is None, dlg.summary.toPlainText()
    assert 'stored as 12-bit' in dlg.summary.toPlainText()
    with CineReader(out) as r:
        assert r.real_bpp == 12 and r.first == -20 and len(r) == 30
        assert np.array_equal(r.read(5), synthetic_frame(r.first + 5, W, H, seed=1))   # value/16 = sensor value


def test_save_dialog_frame_count(win, tmp_path):
    live = connect_and_record(win)
    dlg = live.make_save_dialog(1)
    dlg.set_format('P16')
    dlg.range.set_values(first=-37, count=12, step=3, align='trigger')
    assert dlg.range._form.labelForField(dlg.range.step_spin).text() == 'Decimate by'
    assert dlg.range.last_spin.isHidden() and not dlg.range.count_spin.isHidden()
    assert '12 images kept: -36, -33, -30' in dlg.range.summary.text()
    out = tmp_path / 'count.cine'
    dlg.path_edit.setText(str(out))
    dlg.start_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, what='count download')
    assert dlg.error is None, dlg.summary.toPlainText()
    with CineReader(out) as r:
        assert len(r) == 12 and r.image_numbers.tolist() == list(range(-12, 0))   # camera -36..-3, as k*3
        assert np.array_equal(r.read(0), p16(-36) >> 4) and np.array_equal(r.read(11), p16(-3) >> 4)
    # asking for more frames than the recording holds says so and saves what exists
    dlg2 = live.make_save_dialog(1)
    dlg2.range.set_values(first=FRCOUNT - 1000 + PT - 5, count=50)   # 5 frames before the end
    assert 'Only 5 of the 50 requested frames exist' in dlg2.range.summary.text()


def test_camera_test_dialog(win, tmp_path):
    connect_and_record(win)
    assert win.manager_tab.test_btn.isEnabled()
    win.manager_tab.test_btn.click()                              # Manager tab wrench
    dlg = win._test_dialog
    dlg.path_edit.setText(str(tmp_path / 'report'))
    dlg.start_btn.click()
    wait_until(lambda: dlg.result_info is not None or dlg.error is not None, timeout=60, what='camera test')
    assert dlg.error is None, dlg.summary.toPlainText()
    text = dlg.summary.toPlainText()
    for step in ('identity', 'live', 'format P16', 'format P12L', 'format P10', 'times'):
        assert f'] {step}' in text, step
    assert '[FAIL]' not in text, text
    assert (tmp_path / 'report' / 'report.txt').exists() and (tmp_path / 'report' / 'formats.npz').exists()
    # the app's own connection survives the test: live view keeps receiving frames
    n = win.preview.frames_received
    wait_until(lambda: win.preview.frames_received > n + 2, what='live view after test')


def test_export_tiff_pcc_option_unavailable_without_table(win, tmp_path):
    from phantastic.cine import CineWriter
    src = tmp_path / 'odd.cine'
    with CineWriter(src, 8, 4, 3, 'mono16', setup_fields={'FrameRate': 100, 'RealBPP': 12, 'BlackLevel': 7,
                                                          'WhiteLevel': 4000}) as w:
        for k in range(3):
            w.append(np.full((4, 8), k, np.uint16), time=(k, 0), exposure=1)
    assert win.open_file(src)
    tif = win.export('tiff')
    assert tif.pcc_table is None
    assert not tif.values_combo.model().item(1).isEnabled()       # PCC option greyed out, with a reason
    assert 'no measured table' in tif.values_help.text()
    win.play_tab.tiff_pcc_action.trigger()                        # the Save Cine ▾ item says why it cannot
    assert 'unavailable' in win.last_error


PCC_DRIVE = next(iter(sorted(__import__('pathlib').Path('F:/').glob('800nm_si_sphere*06OCT26'))), None) \
    if os.path.exists('F:/') else None


@pytest.mark.skipif(PCC_DRIVE is None, reason='lab data drive not present')
def test_export_tiff_pcc_identical(win, tmp_path):
    import tifffile
    cal = PCC_DRIVE / 'calibration'
    assert win.open_file(cal / 'side1.cine')
    win.play_tab.tiff_pcc_action.trigger()                        # Save Cine ▾ > Export TIFF as PCC would (8-bit)
    tif = win._last_dialog
    assert tif.pcc_table is not None and tif.pcc_table['name'] == 'lab_2026-10-06_tif8'
    assert tif.values_combo.currentData() == 'pcc'
    tif.range.set_option('full')
    tif.path_edit.setText(str(tmp_path / 'pcc.tif'))
    tif.start_btn.click()
    wait_until(lambda: tif.result_info is not None or tif.error is not None, what='PCC-identical export')
    assert tif.error is None, tif.summary.toPlainText()
    assert 'PCC-identical' in tif.summary.toPlainText()
    assert np.array_equal(tifffile.imread(tmp_path / 'pcc.tif'), tifffile.imread(cal / 'side1_2.tif'))
