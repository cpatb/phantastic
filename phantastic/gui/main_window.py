"""Main window laid out like PCC 3.11: one tool strip (no menu bar), MDI image panels in the centre,
the Live | Play | Manager control tabs on the right, and PCC's status bar."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QSize, QStandardPaths, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QDesktopServices, QKeySequence, QPalette
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QLabel, QMainWindow, QMdiArea, QMdiSubWindow,
                               QMenu, QMessageBox, QProgressBar, QTabWidget, QToolBar, QToolButton, QWidget)

from .. import protocol as P
from ..camera import Camera, discover, network_report
from ..simulator import Simulator
from .dialogs import ABOUT_TEXT, ConnectDialog, ExportDialog
from .icons import icon
from .image_tools import ImageToolsWindow
from .live_tab import LiveTab
from .logs import camera_log_path, log_dir
from .manager_tab import ManagerTab
from .panels import CameraCineSource, FileSource, ImagePanel, PlaybackPanel, PreviewPanel
from .play_tab import PlayTab
from .workers import CameraSession, TaskManager, describe_error

log = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_S = 1.0
CONNECT_TIMEOUT_S = 10.0
STATUS_MESSAGE_MS = 6000
CONTROL_TAB_WIDTH = 260        # this user's PCC: settings.xml TabWidth
ZOOM_PRESETS = (('Fit', None), ('1/4', 0.25), ('1/2', 0.5), ('1:1', 1.0), ('2:1', 2.0), ('4:1', 4.0), ('8:1', 8.0))
README = Path(__file__).resolve().parents[2] / 'README.md'


def zoom_text(s: float) -> str:
    return f'{s:.3g}:1' if s >= 1 else f'1/{1 / s:.3g}'


class PanelWindow(QMdiSubWindow):
    """MDI child holding one image panel; closing it stops the panel's timers and readers."""
    closed = Signal(object)

    def __init__(self, panel: ImagePanel):
        super().__init__()
        self.panel = panel
        self.setWidget(panel)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(panel.title())

    def closeEvent(self, event):
        self.panel.stop()
        self.closed.emit(self.panel)
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Phantastic')
        self.tasks = TaskManager(on_error=lambda e: self.report_error(describe_error(e)), parent=self)
        self.session: CameraSession | None = None
        self.simulator: Simulator | None = None
        self.last_error: str | None = None
        self._connecting = False
        self.preview: PreviewPanel | None = None
        self.playbacks: dict[object, PanelWindow] = {}
        self.active_panel: ImagePanel | None = None
        self.image_tools: ImageToolsWindow | None = None
        self.view_mode = 'cursor'
        self._zoom_pending = False
        self.files: list[str] = []
        self.snapshot_dir = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.PicturesLocation) or Path.home()) / 'Phantastic Snapshots'

        # -- centre: MDI panels
        self.mdi = QMdiArea()
        self.mdi.setBackground(QBrush(QColor(170, 170, 170)))
        self.mdi.subWindowActivated.connect(self._sub_activated)
        self.setCentralWidget(self.mdi)

        # -- right: Live | Play | Manager
        self.live_tab = LiveTab(self.tasks)
        self.play_tab = PlayTab()
        self.manager_tab = ManagerTab()
        self.control_tabs = QTabWidget()
        self.control_tabs.addTab(self.live_tab, 'Live')
        self.control_tabs.addTab(self.play_tab, 'Play')
        self.control_tabs.addTab(self.manager_tab, 'Manager')
        self.control_tabs.setCurrentWidget(self.manager_tab)     # PCC p.17: Manager first
        self.dock = QDockWidget('Control', self)
        self.dock.setObjectName('control_tabs')
        self.dock.setTitleBarWidget(QWidget())
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.dock.setWidget(self.control_tabs)
        self.dock.setMinimumWidth(CONTROL_TAB_WIDTH - 20)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.resizeDocks([self.dock], [CONTROL_TAB_WIDTH], Qt.Orientation.Horizontal)

        self._toolbar()
        self._status_bar()
        self._wire()
        self._shortcuts()
        self.report('Ready. +S in the Manager tab adds a simulated camera to try it without hardware.')
        self._update_actions()
        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(500)
        self._rate_timer.timeout.connect(self._update_rate)
        self._rate_timer.start()

    # ------------------------------------------------------------------ layout
    def _action(self, name: str, tip: str, slot=None, checkable: bool = False, shortcut: str | None = None) -> QAction:
        a = QAction(icon(name), tip, self)
        a.setToolTip(tip + (f' ({shortcut})' if shortcut else ''))
        a.setCheckable(checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if slot is not None:
            a.triggered.connect(slot)
        return a

    def _menu_button(self, action: QAction, items, instant: bool = False) -> QToolButton:
        b = QToolButton()
        b.setDefaultAction(action)
        m = QMenu(b)
        for text, slot in items:
            m.addAction(text, slot)
        b.setMenu(m)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup if instant
                       else QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        return b

    def _toolbar(self):
        tb = QToolBar('Toolbar')
        tb.setObjectName('toolbar')
        tb.setMovable(False)
        tb.setIconSize(QSize(24, 24))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.toolbar = tb
        # View: Cursor, Pan, Zoom Actual Size, Zoom Fit, Zoom (p.16)
        self.cursor_action = self._action('cursor', 'Cursor', lambda: self.set_view_mode('cursor'), True)
        self.pan_action = self._action('pan', 'Pan', lambda: self.set_view_mode('pan'), True)
        g = QActionGroup(self)
        g.setExclusive(True)
        for a in (self.cursor_action, self.pan_action):
            g.addAction(a)
        self.cursor_action.setChecked(True)
        self.zoom11_action = self._action('zoom11', 'Zoom Actual Size', lambda: self.set_zoom(1.0))
        self.zoomfit_action = self._action('zoomfit', 'Zoom Fit', lambda: self.set_zoom(None))
        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.setToolTip('Zoom (1/16 to 16:1)')
        self.zoom_combo.setMinimumContentsLength(7)
        for text, z in ZOOM_PRESETS:
            self.zoom_combo.addItem(text, z)
        self.zoom_combo.activated.connect(lambda i: self.set_zoom(self.zoom_combo.itemData(i)))
        self.zoom_combo.lineEdit().returnPressed.connect(self._zoom_typed)
        for a in (self.cursor_action, self.pan_action, self.zoom11_action, self.zoomfit_action):
            tb.addAction(a)
        tb.addWidget(self.zoom_combo)
        tb.addSeparator()
        # Gadget: Image Tools, SnapShot + pull-down
        self.imagetools_action = self._action('imagetools', 'Image Tools', self.show_image_tools, shortcut='Ctrl+I')
        tb.addAction(self.imagetools_action)
        self.snapshot_action = self._action('snapshot', 'Snapshot', self.snapshot, shortcut='Ctrl+N')
        self.snapshot_btn = self._menu_button(self.snapshot_action, (
            ('Snapshot Folder...', self.choose_snapshot_folder), ('Explore Snapshots', self.explore_snapshots)))
        tb.addWidget(self.snapshot_btn)
        tb.addSeparator()
        # File: Open File, Batch export
        self.open_action = self._action('open', 'Open File', self.open_dialog, shortcut='Ctrl+O')
        tb.addAction(self.open_action)
        self.batch_action = self._action('batch', 'Batch export (the active file cine)')
        self.batch_btn = self._menu_button(self.batch_action, (
            ('Decimated cine (lossless)...', lambda: self.export('cine')),
            ('TIFF stack (raw values)...', lambda: self.export('tiff')),
            ('TIFF as PCC would (8-bit)...', lambda: self.export('tiff', 'pcc')),
            ('TIFF image sequence (one file per image)...', lambda: self.export('tiffseq')),
            ('MP4 movie (8-bit display render)...', lambda: self.export('mp4'))), instant=True)
        tb.addWidget(self.batch_btn)
        tb.addSeparator()
        # App: Window Tile, Window Auto Tile, Help + pull-down
        self.tile_action = self._action('tile', 'Window Tile', self.mdi.tileSubWindows)
        self.autotile_action = self._action('autotile', 'Window Auto Tile', checkable=True)
        self.autotile_action.setChecked(True)                  # this user's PCC: AutoTile=true
        self.autotile_action.toggled.connect(lambda on: on and self.mdi.tileSubWindows())
        tb.addAction(self.tile_action)
        tb.addAction(self.autotile_action)
        self.help_action = self._action('help', 'Help', self.show_help)
        self.help_btn = self._menu_button(self.help_action, (
            ('Help (README)', self.show_help), ('Open log folder', self.open_log_folder), ('About', self.about)))
        tb.addWidget(self.help_btn)
        tb.addSeparator()
        # Overlay: CrossHair, Grid (display only, not recorded)
        self.cross_action = self._action('crosshair', 'CrossHair', checkable=True)
        self.grid_action = self._action('grid', 'Grid Display', checkable=True)
        self.cross_action.toggled.connect(self._overlays)
        self.grid_action.toggled.connect(self._overlays)
        tb.addAction(self.cross_action)
        tb.addAction(self.grid_action)

    def _status_bar(self):
        sb = self.statusBar()
        self.rate_label = QLabel('Refresh rate - fps')
        self.xy_label = QLabel('X: Y:')
        self.xy_label.setToolTip('Pixel under the cursor, 1-based like PCC (upper-left pixel = 1, 1). '
                                 'The array index is one less.')
        self.value_label = QLabel('Value:')
        self.value_label.setToolTip('Value of that pixel as stored in the file or sent by the camera')
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(140)
        self.progress.setVisible(False)
        for w, width in ((self.rate_label, 140), (self.xy_label, 120), (self.value_label, 150)):
            w.setMinimumWidth(width)
            sb.addPermanentWidget(w)
        sb.addPermanentWidget(self.progress)

    def _wire(self):
        m, live, play = self.manager_tab, self.live_tab, self.play_tab
        m.discover_btn.clicked.connect(self.discover)
        m.add_sim_btn.clicked.connect(self.start_simulator)
        m.ip_btn.clicked.connect(self.connect_by_ip)
        m.test_btn.clicked.connect(self.run_camera_test)
        m.logs_btn.clicked.connect(self.open_log_folder)
        m.network_btn.clicked.connect(self.network_check)
        m.activated.connect(self._tree_activated)
        m.remove_requested.connect(self._tree_remove)
        m.disconnect_requested.connect(self.disconnect)
        live.message.connect(self.report)
        live.error.connect(self.report_error)
        live.cines_changed.connect(self._cines_changed)
        live.recording_changed.connect(lambda on: self.preview and self.preview.set_recording(on))
        play.cine_chosen.connect(self._play_chosen)
        play.save_requested.connect(self._save_requested)

    def _shortcuts(self):
        for seq, slot in (('Ctrl+R', lambda: self.live_tab.capture()), ('Ctrl+T', lambda: self.live_tab.trigger()),
                          ('Ctrl+S', lambda: self.save_cine()), ('Ctrl+A', lambda: self.start_simulator())):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(slot)
            self.addAction(a)

    def _update_actions(self):
        connected = self.session is not None
        self.manager_tab.test_btn.setEnabled(connected)
        f = self._active_file_panel() is not None
        self.batch_btn.setEnabled(f)

    # ------------------------------------------------------------------ status
    def report(self, msg: str):
        self.statusBar().setStyleSheet('')
        self.statusBar().showMessage(msg, STATUS_MESSAGE_MS)

    def report_error(self, msg: str):
        """Errors stay in the status bar (no timeout) until the next message."""
        log.error(msg)
        self.last_error = msg
        self.statusBar().setStyleSheet('QStatusBar { color: #b00020; font-weight: bold; }')
        self.statusBar().showMessage(msg)

    def _update_rate(self):
        r = self.active_panel.refresh_rate() if self.active_panel is not None else None
        self.rate_label.setText(f'Refresh rate {r:.2f} fps' if r else 'Refresh rate - fps')

    def _update_readout(self):
        p = self.active_panel
        ro = None if p is None else p.last_readout
        if ro is None:
            self.xy_label.setText('X: Y:')
            self.value_label.setText('Value:')
            return
        x, y, v = ro
        self.xy_label.setText(f'X: {x + 1} Y: {y + 1}')
        self.value_label.setText(f'RGB: {",".join(map(str, v))}' if isinstance(v, tuple) else f'Value: {v}')

    def _job_progress(self, done: int, total: int):
        if done < 0:
            self.progress.setVisible(False)
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _track(self, dlg):
        dlg.progressed.connect(self._job_progress)
        return dlg

    # ------------------------------------------------------------------ panels
    def _add_panel(self, panel: ImagePanel) -> PanelWindow:
        sub = PanelWindow(panel)
        sub.setWindowIcon(icon('camera' if isinstance(panel, PreviewPanel) else 'cine'))
        panel.error.connect(self.report_error)
        panel.message.connect(self.report)
        panel.readout_changed.connect(lambda p=panel: p is self.active_panel and self._update_readout())
        panel.view.zoom_changed.connect(lambda p=panel: p is self.active_panel and self._show_zoom())
        panel.frame_changed.connect(lambda p=panel: p is self.active_panel and p.frame is not None
                                    and self._zoom_pending and self._show_zoom())
        panel.view.set_mode(self.view_mode)
        panel.view.show_cross = self.cross_action.isChecked()
        panel.view.show_grid = self.grid_action.isChecked()
        sub.closed.connect(self._panel_closed)
        self.mdi.addSubWindow(sub)
        sub.resize(640, 480)
        sub.show()
        if self.autotile_action.isChecked():
            self.mdi.tileSubWindows()
        self.mdi.setActiveSubWindow(sub)
        self._set_active(panel)
        return sub

    def _sub_activated(self, sub):
        if sub is None:          # the main window lost focus (e.g. a dialog opened): keep the panel
            if not self.mdi.subWindowList():
                self._set_active(None)
            return
        self._set_active(sub.widget())

    def _set_active(self, panel: ImagePanel | None):
        self.active_panel = panel
        if isinstance(panel, PlaybackPanel):
            self.play_tab.set_panel(panel)
        if self.image_tools is not None:
            self.image_tools.set_panel(panel)
        self._update_readout()
        self._show_zoom()
        self._update_actions()

    def _panel_closed(self, panel: ImagePanel):
        if panel is self.preview:
            self.preview = None
        for k, sub in list(self.playbacks.items()):
            if sub.panel is panel:
                del self.playbacks[k]
        if self.play_tab.panel is panel:
            self.play_tab.set_panel(None)
        if self.active_panel is panel:
            self.active_panel = None
            if self.image_tools is not None:
                self.image_tools.set_panel(None)
        QTimer.singleShot(0, self._after_close)

    def _after_close(self):
        if self.autotile_action.isChecked():
            self.mdi.tileSubWindows()
        sub = self.mdi.activeSubWindow()
        if sub is not None:
            self._set_active(sub.widget())
        self._update_actions()

    def open_preview(self) -> PreviewPanel | None:
        if self.session is None:
            return None
        if self.preview is not None:
            self.mdi.setActiveSubWindow(self.preview.parentWidget())
            return self.preview
        self.preview = PreviewPanel(self.tasks, self.session)
        self.preview.set_recording(self.live_tab.recording)
        self._add_panel(self.preview)
        return self.preview

    def open_playback(self, source) -> PlaybackPanel:
        sub = self.playbacks.get(source.key)
        if sub is not None:
            source.close()
            self.mdi.setActiveSubWindow(sub)
            self._set_active(sub.panel)
        else:
            panel = PlaybackPanel(self.tasks, source)
            sub = self._add_panel(panel)
            self.playbacks[source.key] = sub
        self.control_tabs.setCurrentWidget(self.play_tab)
        self._update_play_entries()
        self.play_tab.set_panel(sub.panel)
        return sub.panel

    def open_camera_cine(self, cine: int) -> PlaybackPanel | None:
        info = self.live_tab.cine_infos.get(cine)
        if self.session is None or info is None or info.get('firstfr') is None:
            self.report_error(f'Cine {cine} holds no stored recording')
            return None
        return self.open_playback(CameraCineSource(self.session, cine, info))

    def playback(self, key) -> PlaybackPanel | None:
        sub = self.playbacks.get(key)
        return None if sub is None else sub.panel

    def _active_file_panel(self) -> PlaybackPanel | None:
        for p in (self.active_panel, self.play_tab.panel):
            if isinstance(p, PlaybackPanel) and p.source.kind == 'file':
                return p
        return None

    def _update_play_entries(self):
        entries = []
        if self.session is not None:
            serial = self.session.info.get('serial', 'camera')
            entries += [(f'{serial} > Cine {c}', ('camera', c)) for c in self.live_tab.stored_cines()]
        entries += [(Path(f).name, ('file', f)) for f in self.files]
        self.play_tab.set_entries(entries)

    # ------------------------------------------------------------------ view tools
    def set_view_mode(self, mode: str):
        self.view_mode = mode
        for sub in self.mdi.subWindowList():
            sub.widget().view.set_mode(mode)

    def set_zoom(self, z: float | None):
        if self.active_panel is not None:
            self.active_panel.view.set_zoom(z)
        self._show_zoom()

    def _zoom_typed(self):
        text = self.zoom_combo.currentText().strip().lower()
        try:
            if text.startswith('fit'):
                z = None
            elif '/' in text:
                a, b = text.split('/')
                z = float(a) / float(b)
            elif ':' in text:
                a, b = text.split(':')
                z = float(a) / float(b)
            else:
                z = float(text)
        except (ValueError, ZeroDivisionError):
            self.report_error(f'Zoom: cannot read {text!r} (use e.g. 1:1, 1/2, 2:1 or Fit)')
            return
        self.set_zoom(z)

    def _show_zoom(self):
        p = self.active_panel
        self._zoom_pending = p is not None and p.view.image_size is None
        if p is None or p.view.image_size is None:
            return
        s = p.view.scale()
        self.zoom_combo.setEditText(zoom_text(s))

    def _overlays(self):
        for sub in self.mdi.subWindowList():
            v = sub.widget().view
            v.show_cross = self.cross_action.isChecked()
            v.show_grid = self.grid_action.isChecked()
            v.update()

    def show_image_tools(self) -> ImageToolsWindow:
        if self.image_tools is None:
            self.image_tools = ImageToolsWindow(self, self.cross_action, self.grid_action)
            geo = self.geometry()
            self.image_tools.move(geo.right() - CONTROL_TAB_WIDTH - self.image_tools.width() - 20, geo.top() + 80)
        self.image_tools.set_panel(self.active_panel)
        self.image_tools.show()
        self.image_tools.raise_()
        return self.image_tools

    def snapshot(self) -> Path | None:
        p = self.active_panel
        img = None if p is None else p.view.display_image()
        if img is None:
            self.report_error('Snapshot: no image in the active panel')
            return None
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        tag = ''
        if isinstance(p, PlaybackPanel):
            tag = f'_img{p.shown}'
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in p.title())
        path = self.snapshot_dir / f'{safe}{tag}_{time.strftime("%Y%m%d_%H%M%S")}.png'
        if not img.save(str(path)):
            self.report_error(f'Snapshot: could not write {path}')
            return None
        self.report(f'Snapshot saved: {path} (8-bit display image, not raw values)')
        return path

    def choose_snapshot_folder(self):
        d = QFileDialog.getExistingDirectory(self, 'Snapshot folder', str(self.snapshot_dir))
        if d:
            self.snapshot_dir = Path(d)

    def explore_snapshots(self):
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.snapshot_dir)))

    # ------------------------------------------------------------------ cameras
    def discover(self):
        sim = self.simulator
        btn = self.manager_tab.discover_btn
        btn.setEnabled(False)
        self.report('Discovering cameras...')

        def job(task):
            found = discover(timeout=DISCOVERY_TIMEOUT_S)
            if sim is not None and sim.udp is not None:   # the simulator answers on its own UDP port
                found += discover(timeout=0.3, broadcast=('127.0.0.1',), port=sim.udp.getsockname()[1])
            return found

        def done(found):
            btn.setEnabled(True)
            keep = None if self.session is None else (self.session.cam.ip, self.session.cam.port)
            self.manager_tab.set_cameras([(str(c), c.ip, c.port) for c in found], keep=keep)
            self._cines_changed()
            self.report(f'{len(found)} camera(s) found')

        def failed(e):
            btn.setEnabled(True)
            self.report_error(f'Discovery failed: {describe_error(e)}')
        self.tasks.submit(job, done, failed)

    def connect_by_ip(self):
        dlg = ConnectDialog(self)
        if dlg.exec():
            self.connect_to(*dlg.address())

    def start_simulator(self):
        """Add Simulated Camera: start the built-in simulator on 127.0.0.1 (free ports) and connect."""
        if self.simulator is None:
            try:
                self.simulator = Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0).start()
            except OSError as e:
                self.report_error(f'Could not start the simulator: {e}')
                return
        self.manager_tab.add_camera(f'Phantastic Simulator @ 127.0.0.1:{self.simulator.port} (local)',
                                    '127.0.0.1', self.simulator.port)
        self.connect_to('127.0.0.1', self.simulator.port, simulated=True, attach_port=self.simulator.attach_port)

    def connect_to(self, ip: str, port: int, simulated: bool = False, attach_port: int = P.ATTACH_PORT):
        if self._connecting:
            return
        if self.session is not None and (self.session.cam.ip, self.session.cam.port) == (ip, port):
            self.open_preview()
            return
        self.disconnect()
        self._connecting = True
        self.report(f'Connecting to {ip}:{port}...')

        def job(task):
            cam = Camera(ip, port, timeout=CONNECT_TIMEOUT_S, attach_port=attach_port,
                         log_file=camera_log_path(ip)).connect()
            log.info('camera session log: %s', cam.log_file)
            try:
                info = cam.info()
                acq = cam.acquisition()
                try:
                    formats = cam.image_formats()
                except Exception:
                    formats = ['P16']
                extra = {}
                try:
                    extra['membpp'] = cam.get('cam.membpp')
                except P.ProtocolError:
                    pass
            except BaseException:
                cam.close()
                raise
            return cam, info, acq, formats, extra

        def done(r):
            self._connecting = False
            cam, info, acq, formats, extra = r
            self.session = CameraSession(cam, info, formats, simulated=simulated)
            self.session.extra = extra
            item = self.manager_tab.add_camera(str(info.get('name') or info.get('serial')), ip, port)
            item.setToolTip(0, f'{info.get("model", "camera")}, serial {info.get("serial")}, {ip}:{port}')
            self.live_tab.set_session(self.session, acq)
            self.open_preview()
            self._cines_changed()
            self._update_actions()
            self.report(f'Connected to {info.get("model", "camera")} serial {info.get("serial")} at {ip}:{port}')

        def failed(e):
            self._connecting = False
            self.report_error(f'Connect to {ip}:{port} failed: {describe_error(e)}')
        self.tasks.submit(job, done, failed)

    def disconnect(self):
        if self.session is None:
            return
        s, self.session = self.session, None
        for k, sub in list(self.playbacks.items()):
            if k[0] == 'camera':
                sub.close()
        if self.preview is not None:
            self.preview.parentWidget().close()
        self.live_tab.set_session(None)
        s.close()   # a running download fails with a connection error, which its dialog reports
        self.manager_tab.set_connected(None)
        self._update_play_entries()
        self._update_actions()
        self.report('Disconnected')

    def _cines_changed(self):
        live = self.live_tab
        if self.session is None:
            self.manager_tab.set_connected(None)
        else:
            addr = (self.session.cam.ip, self.session.cam.port)
            self.manager_tab.set_connected(addr, [(c, f'Cine {c}') for c in live.stored_cines()])
        # a camera cine re-recorded or erased since it was opened no longer matches its panel
        for key, sub in list(self.playbacks.items()):
            if key[0] != 'camera':
                continue
            now = live.cine_infos.get(key[1])
            old = sub.panel.source.info
            if self.session is not None and now is None and not live.cine_states:
                continue          # not polled yet
            same = now is not None and all(now.get(k) == old.get(k) for k in ('firstfr', 'lastfr', 'trigtime'))
            if not same:
                sub.close()
                self.report(f'Cine {key[1]} changed on the camera; its playback panel was closed')
        if self.preview is not None:
            self.preview.set_recording(live.recording)
        self._update_play_entries()

    def _tree_activated(self, key):
        kind = key[0]
        if kind == 'camera':
            _, ip, port = key
            sim = self.simulator is not None and port == self.simulator.port and ip == '127.0.0.1'
            self.connect_to(ip, port, simulated=sim,
                            attach_port=self.simulator.attach_port if sim else P.ATTACH_PORT)
        elif kind == 'cine':
            self.open_camera_cine(key[1])
        elif kind == 'file':
            self.open_file(key[1])

    def _tree_remove(self, key):
        kind = key[0]
        if kind == 'camera':
            if self.session is not None and (self.session.cam.ip, self.session.cam.port) == key[1:]:
                self.disconnect()
            it = self.manager_tab.find(key)
            if it is not None:
                self.manager_tab.cameras_root.removeChild(it)
        elif kind == 'file':
            sub = self.playbacks.get(key)
            if sub is not None:
                sub.close()
            self.manager_tab.remove_file(key[1])
            self.files = [f for f in self.files if f != key[1]]
            self._update_play_entries()

    def _play_chosen(self, key):
        if key is None:
            return
        if key in self.playbacks:
            self.open_playback_key(key)
        elif key[0] == 'camera':
            self.open_camera_cine(key[1])
        else:
            self.open_file(key[1])

    def open_playback_key(self, key):
        sub = self.playbacks[key]
        self.mdi.setActiveSubWindow(sub)
        self._set_active(sub.panel)

    # ------------------------------------------------------------------ files
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open File', '', 'Cine files (*.cine);;All files (*)')
        if path:
            self.open_file(path)

    def open_file(self, path) -> bool:
        p = str(path)
        if ('file', p) in self.playbacks:
            self.open_playback_key(('file', p))
            self.control_tabs.setCurrentWidget(self.play_tab)
            return True
        try:
            source = FileSource(p)
        except Exception as e:
            self.report_error(f'Cannot open {p}: {describe_error(e)}')
            return False
        self.files = [p] + [f for f in self.files if f != p]
        self.manager_tab.add_file(p)
        self.open_playback(source)
        self.report(f'Opened {p}')
        return True

    def save_cine(self):
        """Save Cine To File for the cine in the Play tab: a camera download, or a lossless file copy."""
        p = self.play_tab.panel
        if p is None:
            self.report_error('Choose a cine in the Play tab first')
            return None
        marks = (p.mark_in, p.mark_out)
        if p.source.kind == 'camera':
            try:
                dlg = self.live_tab.make_save_dialog(p.source.cine, marks=marks, panel=p)
            except ValueError as e:
                self.report_error(str(e))
                return None
        else:
            src = p.source
            dlg = ExportDialog(self.tasks, 'cine', src.path, src.first, src.last, marks=marks, parent=self, panel=p)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)   # the marks it was given cannot move under it
        self._track(dlg).show()
        self._last_dialog = dlg
        return dlg

    def _save_requested(self, what: str):
        if what == 'cine':
            self.save_cine()
        elif what == 'tiff_raw':
            self.export('tiff')
        elif what == 'tiff_pcc':
            self.export('tiff', 'pcc')
        elif what == 'tiff_seq':
            self.export('tiffseq')
        elif what == 'mp4':
            self.export('mp4')
        elif what == 'save_all':
            self.save_all()

    def _active_playback_panel(self) -> PlaybackPanel | None:
        for p in (self.active_panel, self.play_tab.panel):
            if isinstance(p, PlaybackPanel):
                return p
        return None

    def save_all(self):
        """Save All RAM Cines to File (PCC p.62)."""
        if self.session is None or not self.live_tab.stored_cines():
            self.report_error('No stored cine in camera RAM')
            return None
        from .dialogs import SaveAllDialog
        infos = {c: self.live_tab.cine_infos[c] for c in self.live_tab.stored_cines()}
        dlg = SaveAllDialog(self.tasks, self.session, infos, str(Path.home()), parent=self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._track(dlg).show()
        self._last_dialog = dlg
        return dlg

    def export(self, kind: str, values: str = 'raw') -> ExportDialog | None:
        cp = self._active_playback_panel()
        if cp is not None and cp.source.kind == 'camera' and kind in ('tiff', 'tiffseq', 'mp4') and values == 'raw':
            # straight from camera RAM, no save-then-open
            info = self.live_tab.cine_infos.get(cp.source.cine) or cp.source.info
            dlg = ExportDialog(self.tasks, kind, None, cp.source.first, cp.source.last,
                               marks=(cp.mark_in, cp.mark_out), parent=self,
                               camera=(self.session, cp.source.cine, info), panel=cp)
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            self._track(dlg).show()
            self._last_dialog = dlg
            return dlg
        p = self._active_file_panel()
        if p is None:
            self.report_error('Open a cine file first (Open File, Ctrl+O); camera cines must be saved first'
                              if values == 'pcc' or kind == 'cine' else 'Open a cine file or a camera cine first')
            return None
        src = p.source
        dlg = ExportDialog(self.tasks, kind, src.path, src.first, src.last, marks=(p.mark_in, p.mark_out),
                           parent=self, panel=p)
        if kind == 'tiff' and values == 'pcc':
            if dlg.pcc_table is None:
                self.report_error('PCC-identical export is unavailable: no measured table matches this file')
            else:
                dlg.set_values_mode('pcc')
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._track(dlg).show()
        self._last_dialog = dlg
        return dlg

    # ------------------------------------------------------------------ misc
    def run_camera_test(self):
        if self.session is None:
            self.report_error('Connect to a camera first')
            return None
        from .dialogs import CameraTestDialog
        out = str(log_dir() / f'camera_test_{time.strftime("%Y%m%d_%H%M%S")}')
        dlg = CameraTestDialog(self.tasks, self.session, out, parent=self)
        self._track(dlg).show()
        self._test_dialog = dlg
        return dlg

    def network_check(self):
        def done(lines):
            QMessageBox.information(self, 'Network check',
                                    '\n'.join(lines) if lines else 'No IPv4 network adapter found.')
        self.tasks.submit(lambda task: network_report(), done,
                          lambda e: self.report_error(f'Network check failed: {describe_error(e)}'))

    def open_log_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    def show_help(self):
        if README.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(README)))
        else:
            self.report_error(f'README not found at {README}')

    def about(self):
        QMessageBox.about(self, 'About Phantastic', ABOUT_TEXT)

    def closeEvent(self, event):
        self.tasks.cancel_all()
        self.live_tab.timer.stop()
        for sub in self.mdi.subWindowList():
            sub.close()
        self.disconnect()
        if self.image_tools is not None:
            self.image_tools.close()
        self.tasks.pool.waitForDone(3000)
        if self.simulator is not None:
            self.simulator.stop()
            self.simulator = None
        super().closeEvent(event)


def apply_pcc_style(app):
    """Light Windows-like look, as PCC has."""
    app.setStyle('Fusion')
    app.setPalette(app.style().standardPalette())


def apply_dark_palette(app):
    """Neutral dark theme (``--dark``)."""
    app.setStyle('Fusion')
    p = QPalette()
    base, text = QColor(45, 45, 48), QColor(220, 220, 220)
    p.setColor(QPalette.ColorRole.Window, base)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, QColor(30, 30, 32))
    p.setColor(QPalette.ColorRole.AlternateBase, base)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(60, 60, 64))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(120, 120, 120))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(120, 120, 120))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(120, 120, 120))
    app.setPalette(p)
