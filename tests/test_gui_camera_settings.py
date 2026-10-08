"""Offscreen GUI tests of the camera-settings selectors against the simulated camera ('v2512' profile).

Anchor outside the GUI: the simulator's model state (what the camera was actually told) and its log of
received commands, which must hold no write the user did not ask for.
"""
import json
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if os.name == 'nt':
    os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

import numpy as np
import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMessageBox

from phantastic import camsettings as CS
from phantastic.defects import FLAG_P16
from phantastic.gui import camera_settings as GCS
from phantastic.gui.main_window import MainWindow
from phantastic.gui.panels import ImagePanel

WRITES = ('set', 'setrtc', 'bref', 'rec', 'del', 'partition', 'trig')


def wait_until(cond, timeout=15.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f'timed out waiting for {what}')


def settle(seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def make_win(profile):
    w = MainWindow()
    w.simulator_profile = profile
    w.resize(1456, 868)
    w.show()
    w.manager_tab.add_sim_btn.click()
    wait_until(lambda: w.session is not None, what='connect')
    wait_until(lambda: w.live_tab.settings.read_at is not None, what='settings read')
    return w


@pytest.fixture()
def win(app):
    w = make_win('v2512')
    yield w
    w.close()


def writes(win, since=0):
    return [c for c in win.simulator.model.received[since:] if c.split(' ')[0].split(':')[0] in WRITES]


def edit(form, key, text):
    w = form.fields[key]
    assert isinstance(w, QLineEdit)
    w.setText(text)


def apply_and_wait(win, form):
    n = len(win.simulator.model.received)
    form.apply_btn.click()
    wait_until(lambda: form.readback.text().startswith('Camera reads back'), what='read-back')
    return writes(win, n)


def test_connect_reads_and_writes_nothing(win):
    live = win.live_tab
    settle(0.5)
    assert writes(win) == []                                       # connect + read: 'get' only
    sig = live.signals_form
    assert sig.fields['cam.trigpol'].text() == '0' and not sig.fields['cam.trigpol'].isHidden()
    aux = sig.fields['cam.aux1mode']
    assert isinstance(aux, QLabel) and 'raw, meaning not verified' in aux.text()
    assert not sig.apply_btn.isEnabled() and sig.preview.text() == 'No changes to send.'
    assert live.ibat_form.fields['auto.trigger.w'].text() == '64'
    assert live.camclock_label.text().startswith('Sat Jan 01 2000')   # the v2512's never-set clock


def test_edit_preview_apply_readback(win):
    live, model = win.live_tab, win.simulator.model
    sig = live.signals_form
    edit(sig, 'cam.trigpol', 'x')
    assert not sig.apply_btn.isEnabled() and 'Cannot apply' in sig.preview.text()
    edit(sig, 'cam.trigpol', '1')
    edit(sig, 'cam.trigfilt', '32')
    assert sig.preview.text() == 'Apply will send:\nset cam.trigpol:1\nset cam.trigfilt:32'
    assert writes(win) == []                                       # editing sends nothing
    assert apply_and_wait(win, sig) == ['set cam.trigpol:1', 'set cam.trigfilt:32']
    assert (model.cam['trigpol'], model.cam['trigfilt']) == (1, 32)
    assert sig.fields['cam.trigpol'].text() == '1' and 'cam.trigpol = 1' in sig.readback.text()
    assert not sig.apply_btn.isEnabled()                           # nothing pending after the read-back


def test_unsent_edits_survive_another_selectors_apply(win):
    live = win.live_tab
    edit(live.adv_form, 'cam.quiet', '1')                          # pending, not applied
    edit(live.aexp_form, 'defc.aexpcomp', '-0.25')
    assert apply_and_wait(win, live.aexp_form) == ['set defc {aexpcomp:-0.25}']   # configure path, one field
    assert win.simulator.model.defc['aexpcomp'] == -0.25
    assert live.adv_form.fields['cam.quiet'].text() == '1' and live.adv_form.apply_btn.isEnabled()
    assert win.simulator.model.cam['quiet'] == 0


def test_burst_goes_through_configure_only_changed_fields(win):
    form = win.live_tab.cine_adv_form
    edit(form, 'defc.bcount', '3')
    assert form.preview.text() == 'Apply will send:\nset defc {bcount:3}'
    assert apply_and_wait(win, form) == ['set defc {bcount:3}']
    assert win.simulator.model.defc['bcount'] == 3 and win.simulator.model.defc['bperiod'] == 0


def test_cine_name_description(win):
    form = win.live_tab.meta_form
    edit(form, 'meta.comment', 'run 7, 2.1 m/s')
    assert apply_and_wait(win, form) == ['set meta.comment:"run 7, 2.1 m/s"']
    assert win.simulator.model.meta['comment'] == 'run 7, 2.1 m/s'


def test_set_time_needs_the_dialog(win, monkeypatch):
    live = win.live_tab
    monkeypatch.setattr(GCS.SetTimeDialog, 'exec', lambda self: QDialog.DialogCode.Rejected)
    live.settime_btn.click()
    settle()
    assert writes(win) == []                                       # cancelled: nothing sent
    assert 'EVERY RECORDING MADE FROM NOW ON' in live._set_time_dialog.summary.text()

    def accept(self):
        self.tz_check.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(GCS.SetTimeDialog, 'exec', accept)
    live.settime_btn.click()
    wait_until(lambda: len(writes(win)) == 2, what='setrtc')
    w = writes(win)
    assert w[0].startswith('setrtc ') and abs(int(w[0].split()[1]) - time.time()) < 5
    assert w[1] == f'set cam.timezone:{GCS.pc_timezone_west()}'
    wait_until(lambda: abs(live.settings.camera_clock() - time.time()) < 5, what='clock read back')


def test_csr_confirms_and_follows_progress(win, monkeypatch):
    live = win.live_tab
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Cancel)
    live.csr_btn.click()
    settle()
    assert writes(win) == []
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Ok)
    live.settings.structs = {}        # settings read not back yet: progress support is asked in the job (review)
    live.csr_btn.click()
    assert not live.csr_progress.isHidden()
    wait_until(lambda: live.last_csr is not None, what='CSR done')
    assert live.last_csr['confirmed'] and writes(win) == ['bref']
    assert live.csr_progress.isHidden() and live.csr_btn.isEnabled()


def drag(view, a, b):
    for kind, pos, btn in ((QEvent.Type.MouseButtonPress, a, Qt.MouseButton.LeftButton),
                           (QEvent.Type.MouseMove, b, Qt.MouseButton.NoButton),
                           (QEvent.Type.MouseButtonRelease, b, Qt.MouseButton.LeftButton)):
        p = view.widget_point(*pos)
        ev = QMouseEvent(kind, QPointF(p), view.mapToGlobal(p), btn,
                         Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseMove else btn,
                         Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view, ev)


def test_auto_trigger_area_drawn_and_shown(win):
    live = win.live_tab
    wait_until(lambda: win.preview is not None and win.preview.frame is not None, what='live frame')
    view = win.preview.view
    live.roi_show_check.setChecked(True)
    # simulator: x 0, y 0, w 64, h 16 on the 256 x 256 live image -> centred (ROI_CONVENTION)
    assert view.roi == (96, 120, 64, 16) == live.ibat_rect()
    live.roi_draw_btn.click()
    assert view.mode == 'roi'
    drag(view, (10, 20), (49, 39))                                 # 40 x 20 pixels from (10, 20)
    assert view.mode == win.view_mode                              # tool restored
    f = live.ibat_form
    want = CS.roi_to_camera(10, 20, 40, 20, 256, 256)
    assert [int(f.fields[f'auto.trigger.{k}'].text()) for k in 'xywh'] == list(want)
    assert view.roi == (10, 20, 40, 20) and writes(win) == []      # shown, not sent
    lines = apply_and_wait(win, f)
    assert lines == [f'set auto.trigger.{k}:{v}' for k, v in zip('xywh', want)]
    assert [win.simulator.model.auto['trigger'][k] for k in 'xywh'] == list(want)


def test_backup_save_load_shows_diff_and_needs_apply(win, tmp_path):
    live, model = win.live_tab, win.simulator.model
    path = tmp_path / 'setup.json'
    live.save_backup(str(path))
    wait_until(lambda: path.exists() and 'Saved' in live.backup_label.text(), what='backup saved')
    assert json.loads(path.read_text(encoding='utf-8'))['settings']['cam.trigpol'] == 0
    model.cam['trigpol'] = 1                                       # the camera changes behind our back
    live.settings.read()
    wait_until(lambda: live.settings.current.get('cam.trigpol') == 1, what='re-read')
    n = len(model.received)
    wanted, notes = live.load_backup(str(path))
    assert wanted == {'cam.trigpol': 0} and notes == []
    assert 'cam.trigpol: camera 1 -> file 0' in live.backup_label.text()
    assert live.signals_form.preview.text() == 'Apply will send:\nset cam.trigpol:0'
    settle()
    assert writes(win, n) == []                                    # loading sends nothing
    assert apply_and_wait(win, live.signals_form) == ['set cam.trigpol:0'] and model.cam['trigpol'] == 0


def test_capture_into_stored_cine_asks(win, monkeypatch):
    live = win.live_tab
    wait_until(lambda: live.cine_combo.findData(1) >= 0, what='cine list')
    live.select_cine(1)
    live.capture_btn.click()
    wait_until(lambda: live.trigger_btn.isEnabled(), what='WTR')
    live.trigger_btn.click()
    wait_until(lambda: 'STR' in getattr(live.cine_states.get('c1'), 'names', ()), what='STR')
    live.capture_btn.click()                                       # the simulator re-armed cine 2: abort first
    wait_until(lambda: not live.recording, what='abort')
    live.select_cine(1)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: QMessageBox.StandardButton.No)
    n = len(win.simulator.model.received)
    live.capture()
    settle()
    assert 'rec 1' not in win.simulator.model.received[n:]
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a, **k: QMessageBox.StandardButton.Yes)
    live.capture()
    wait_until(lambda: 'rec 1' in win.simulator.model.received[n:], what='rec 1')


def test_default_camera_hides_what_it_does_not_report(app):
    w = make_win(None)
    try:
        live = w.live_tab
        assert live.ibat_form.absent.isVisible() or not live.auto_trigger.expanded
        assert live.ibat_form.present() == [] and live.aexp_form.present() == []
        assert 'cam.trigpol' in live.signals_form.present()
        assert 'cam.trigdelay' not in live.signals_form.present()       # not reported -> hidden
        assert not live.signals_form.fields['cam.trigdelay'].isVisibleTo(live.signals_form)
        assert not live.csr_btn.isEnabled()
    finally:
        w.close()


def test_live_flag_fill_is_display_only(app):
    panel = ImagePanel()
    frame = np.full((8, 8), 1000 * 16, np.uint16)
    frame[3:6, 3:6] = np.arange(9).reshape(3, 3) * 16 + 2000 * 16
    frame[4, 4] = FLAG_P16
    panel.raw_max, panel.readout_div, panel.readout_bits = 65535, 16, 12
    panel.flag_value = FLAG_P16
    panel.show_frame(frame)
    raw = frame.copy()
    shown = panel.display_source()
    # PCC's fill: mean of the 8 neighbours, rounded half down (phantastic.defects); hand-computed here
    nb = [2000 * 16 + 16 * k for k in (0, 1, 2, 3, 5, 6, 7, 8)]
    assert shown[4, 4] == int(np.ceil(sum(nb) / 8 - 0.5)) == 32064
    assert np.array_equal(panel.frame, raw) and panel.frame[4, 4] == FLAG_P16   # data untouched
    panel._readout(4, 4)
    assert panel.readout_text() == 'Value: 4080 (12-bit) flagged by camera'     # raw value, labelled
    panel._readout(3, 3)
    assert 'flagged' not in panel.readout_text()
    panel.flag_value = None                                        # 8-bit live: no fill
    panel._flag_src = None
    assert panel.display_source()[4, 4] == FLAG_P16


def test_abort_while_triggered_recording_fills_asks(win, monkeypatch):
    from phantastic import protocol as P
    live = win.live_tab
    asked = []

    def no(*a, **k):
        asked.append(a[1])
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, 'warning', no)
    n = len(win.simulator.model.received)
    live._capture_decided(True, None, {'c0': P.Flags(('RDY',)), 'c1': P.Flags(('TRG', 'DEF', 'ACT'))})
    settle()
    assert asked and 'rec 0' not in win.simulator.model.received[n:]
    asked.clear()
    live._capture_decided(True, None, {'c1': P.Flags(('WTR', 'DEF', 'ABL', 'ACT'))})   # only waiting: no question
    wait_until(lambda: 'rec 0' in win.simulator.model.received[n:], what='rec 0')
    assert asked == []


def test_trigger_area_uses_the_resolution_after_apply(win):
    live = win.live_tab
    assert live._live_size() == (256, 256)
    live.set_resolution(128, 64)
    live.apply_btn.click()
    wait_until(lambda: live._live_size() == (128, 64), what='settings re-read after Cine Settings Apply')
    live.set_ibat_rect((0, 0, 32, 16))
    assert [int(live.ibat_form.fields[f'auto.trigger.{k}'].text()) for k in 'xywh'] == \
        list(CS.roi_to_camera(0, 0, 32, 16, 128, 64))


def test_multiline_text_cannot_be_applied(win):
    form = win.live_tab.meta_form
    edit(form, 'meta.comment', 'line 1\nline 2')
    assert not form.apply_btn.isEnabled() and 'control characters' in form.preview.text()


def test_double_capture_sends_one_rec(win):
    live = win.live_tab
    wait_until(lambda: live.cine_combo.findData(1) >= 0, what='cine list')
    live.select_cine(1)
    n = len(win.simulator.model.received)
    live.capture()
    live.capture()                     # a second press while the first is in flight (review finding)
    wait_until(lambda: 'rec 1' in win.simulator.model.received[n:], what='rec 1')
    settle(0.5)
    assert win.simulator.model.received[n:].count('rec 1') == 1


def test_stale_button_label_sends_nothing(win):
    from phantastic import protocol as P
    live = win.live_tab
    errors = []
    live.error.connect(errors.append)
    n = len(win.simulator.model.received)
    # pressed as Capture (abort False) while the fresh states show a triggered cine still filling
    assert live._capture_decided(False, 2, {'c1': P.Flags(('TRG', 'DEF', 'ACT')), 'c2': P.Flags(('RDY',))}) is None
    settle()
    assert writes(win, n) == [] and 'nothing was sent' in errors[-1]
