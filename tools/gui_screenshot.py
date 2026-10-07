"""Render the PCC-style main window offscreen with the simulator connected.

    python tools/gui_screenshot.py [out_dir]      (default docs/)

Writes screenshot_pcc_manager.png, screenshot_pcc_live.png, screenshot_pcc_play.png (camera cine
in the Play tab, marks set) and screenshot_pcc_save.png (the Save Cine dialog), at 1456 x 868
(the size of a PCC 3.11 main window).
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if os.name == 'nt':                       # the offscreen platform finds no system fonts by itself
    os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from phantastic.gui.main_window import MainWindow, apply_pcc_style  # noqa: E402

SIZE = (1456, 868)


def pump(cond=lambda: True, timeout=15.0, settle=0.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            break
        time.sleep(0.01)
    else:
        raise TimeoutError
    t = time.monotonic() + settle
    while time.monotonic() < t:
        QApplication.processEvents()
        time.sleep(0.01)


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'docs'
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    apply_pcc_style(app)
    if os.name == 'nt':
        from PySide6.QtGui import QFont
        app.setFont(QFont('Segoe UI', 9))   # offscreen otherwise picks the first font file it finds
    win = MainWindow()
    win.resize(*SIZE)
    win.show()
    win.start_simulator()
    pump(lambda: win.session is not None)
    live = win.live_tab
    live.set_resolution(320, 240)
    live.set_rate(20000)
    live.exp_spin.setValue(40.0)
    live.pt_spin.setValue(100)
    live.apply()
    pump(lambda: live.last_readback is not None)
    pump(lambda: live.cine_combo.findData(1) >= 0)
    live.select_cine(1)
    live.capture()
    pump(lambda: live.trigger_btn.isEnabled())
    live.trigger()
    pump(lambda: 1 in live.cine_infos)
    live.capture()                                       # Abort Recording: back to preview
    pump(lambda: not live.recording)
    pump(lambda: win.preview is not None and win.preview.frames_received > 2, settle=0.3)

    win.control_tabs.setCurrentWidget(win.manager_tab)
    pump(settle=0.3)
    win.grab().save(str(out / 'screenshot_pcc_manager.png'))

    win.control_tabs.setCurrentWidget(live)
    live.camera_settings.expand(True)
    pump(settle=0.3)
    win.grab().save(str(out / 'screenshot_pcc_live.png'))

    panel = win.open_camera_cine(1)
    pump(lambda: panel.frame is not None)
    panel.goto(-600)
    pump(lambda: panel.shown == -600)
    panel.set_mark_in()
    panel.goto(-200)
    pump(lambda: panel.shown == -200)
    panel.set_mark_out()
    panel.goto(-350)
    pump(lambda: panel.shown == -350)
    win.play_tab.speed.expand(True)
    win.play_tab.cine_info.expand(True)
    win.show_image_tools()
    pump(settle=0.4)
    win.grab().save(str(out / 'screenshot_pcc_play.png'))
    win.image_tools.grab().save(str(out / 'screenshot_pcc_imagetools.png'))
    win.image_tools.hide()

    dlg = win.save_cine()
    dlg.path_edit.setText(str(Path.home() / 'cine1_99001.cine'))
    pump(settle=0.3)
    dlg.grab().save(str(out / 'screenshot_pcc_save.png'))
    dlg.close()
    win.close()
    for n in ('manager', 'live', 'play', 'save', 'imagetools'):
        print(out / f'screenshot_pcc_{n}.png')


if __name__ == '__main__':
    main()
