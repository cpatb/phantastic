"""PCC's Live tab: camera and cine settings in collapsible selectors, Capture / Trigger at the bottom.

Settings are sent with ``Camera.configure`` (one ``set defc``) and the camera's read-back is
shown, because the camera may round what it was asked for. Every camera call runs in the
background through the session lock; the cine states are polled once a second.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QSpinBox, QVBoxLayout, QWidget)

from .. import camsettings as CS
from .. import protocol as P
from .camera_settings import SetTimeDialog, SettingsController, SettingsForm, format_clock, pc_timezone_west
from .dialogs import SaveCineDialog
from ..naming import DEFAULT_CINE_TEMPLATE
from .widgets import GREEN, RED, CollapsibleSection, TriggerBar, big_button
from .workers import CameraSession, TaskManager, describe_error

CSR_TIMEOUT_S = 60.0     # give up waiting for auto.bref_progress after this long (a guess: CSR length is not documented)


def _keys(section: str) -> list[str]:
    return [s.key for s in CS.SETTINGS if s.section == section]

REFRESH_INTERVAL_MS = 1000
NS_PER_US = 1000
RATE_PRESETS = (100, 1000, 2000, 5000, 10000, 20000, 50000, 100000)
_RES_RE = re.compile(r'^\s*(\d+)\s*[xX×*]\s*(\d+)\s*$')


def _fmt(v) -> str:
    if v is None:
        return '-'
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


def flags(st) -> tuple[str, ...]:
    return tuple(st.names) if isinstance(st, P.Flags) else ()


def is_recording(states: dict) -> bool:
    """A partition is waiting for a trigger or recording post-trigger frames."""
    for key, st in states.items():
        f = flags(st)
        if int(key[1:]) >= 1 and ('WTR' in f or ('TRG' in f and 'STR' not in f)):
            return True
    return False


def waiting_for_trigger(states: dict) -> bool:
    return any('WTR' in flags(st) for st in states.values())


class LiveTab(QWidget):
    message = Signal(str)
    error = Signal(str)
    cines_changed = Signal()
    recording_changed = Signal(bool)
    roi_draw_requested = Signal()          # user wants to drag the auto-trigger area on the live image
    roi_overlay_changed = Signal(object)   # (x, y, w, h) in stored live-image pixels, or None

    def __init__(self, tasks: TaskManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.tasks = tasks
        self.session: CameraSession | None = None
        self.last_readback: dict | None = None
        self.cine_states: dict = {}
        self.cine_infos: dict[int, dict] = {}
        self.frcount: int | None = None
        self.recording = False
        self._refresh_busy = False
        self.dialogs: list[SaveCineDialog] = []
        self.bref_available = False
        self.last_csr: dict | None = None

        # -- top: Camera:
        self.camera_combo = QComboBox()
        self.camera_combo.setToolTip('Connected camera (connect one in the Manager tab).')

        # -- Camera Settings
        self.settings = SettingsController(tasks, self)
        self.settings.message.connect(self.message)
        self.settings.error.connect(self.error)
        self.time_label = QLabel('-')
        self.time_label.setToolTip('Computer clock.')
        self.camclock_label = QLabel('-', wordWrap=True)
        self.camclock_label.setToolTip('irig.sec as last read, advanced by the computer clock since. Shown as UTC: '
                                       'whether the camera keeps UTC or local time is not verified.')
        self.settime_btn = QPushButton('Set Time..')
        self.settime_btn.setToolTip('Set the camera clock to the computer clock (PCC p.30-32). Asks first: it '
                                    'changes the time stamps of every later recording.')
        self.settime_btn.clicked.connect(self.set_time)
        self.bitdepth_label = QLabel('-')
        self.bitdepth_label.setToolTip('cam.membpp, read only')
        self.partition_combo = QComboBox()
        self.partition_combo.setToolTip('Number of memory partitions (cines). Changing it ERASES ALL cines.')
        self.partition_combo.activated.connect(self._partition_chosen)
        self.backup_load_btn = QPushButton('Load...')
        self.backup_load_btn.setToolTip('Load settings saved by Phantastic: they appear as pending changes in each '
                                        'selector; nothing is sent until you press that selector\'s Apply.')
        self.backup_load_btn.clicked.connect(lambda: self.load_backup())
        self.backup_save_btn = QPushButton('Save...')
        self.backup_save_btn.setToolTip('Read the camera settings shown in these selectors and save them to a JSON '
                                        'file on this computer (PCC p.31-32; the camera\'s memory slots are not used).')
        self.backup_save_btn.clicked.connect(lambda: self.save_backup())
        self.backup_label = QLabel('', wordWrap=True)
        self.backup_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cs = QWidget()
        f = QFormLayout(cs)
        f.setContentsMargins(6, 4, 4, 4)
        trow = QHBoxLayout()
        trow.addWidget(self.time_label, 1)
        trow.addWidget(self.settime_btn)
        f.addRow('Current Time:', trow)
        f.addRow('Camera clock:', self.camclock_label)
        row = QHBoxLayout()
        row.addWidget(QLabel('Bit Depth'))
        row.addWidget(self.bitdepth_label)
        row.addSpacing(10)
        row.addWidget(QLabel('Partitions'))
        row.addWidget(self.partition_combo)
        row.addStretch(1)
        f.addRow(row)
        brow = QHBoxLayout()
        brow.addWidget(QLabel('Backup & restore settings'))
        brow.addStretch(1)
        brow.addWidget(self.backup_load_btn)
        brow.addWidget(self.backup_save_btn)
        f.addRow(brow)
        f.addRow(self.backup_label)
        self.camera_settings = CollapsibleSection('Camera Settings', cs)

        # -- forms for the settings in phantastic.camsettings (one Apply each)
        self.meta_form = SettingsForm(_keys(CS.CINE_META))
        self.cine_adv_form = SettingsForm(_keys(CS.CINE_ADV))
        self.adv_form = SettingsForm(_keys(CS.ADVANCED))
        self.signals_form = SettingsForm(_keys(CS.SIGNALS))
        self.aexp_form = SettingsForm(_keys(CS.AUTO_EXP))
        self.ibat_form = SettingsForm(_keys(CS.IBAT))
        self.forms = (self.meta_form, self.cine_adv_form, self.adv_form, self.signals_form, self.aexp_form,
                      self.ibat_form)
        for form in self.forms:
            self.settings.add_form(form)

        # -- Cine Settings
        self.cine_combo = QComboBox()
        self.cine_combo.setToolTip('Cine to record into on Capture. Preview = the next free cine. '
                                   'Recording into a cine deletes what it holds.')
        self.res_combo = QComboBox(editable=True)
        self.res_combo.setToolTip('Width x Height in pixels')
        self.rate_combo = QComboBox(editable=True)
        for r in RATE_PRESETS:
            self.rate_combo.addItem(str(r))
        self.exp_spin = QDoubleSpinBox(minimum=0.001, maximum=1e7, decimals=3, value=100.0)
        self.exp_spin.setToolTip('Shown in µs; sent to the camera in ns.')
        self.edr_spin = QDoubleSpinBox(minimum=0.0, maximum=1e7, decimals=3, value=0.0)
        self.edr_spin.setToolTip('Extreme Dynamic Range exposure, µs (0 = off); sent in ns.')
        self.csr_btn = QPushButton('CSR')
        self.csr_btn.clicked.connect(self.csr)
        self.csr_progress = QProgressBar()
        self.csr_progress.setRange(0, 100)
        self.csr_progress.setVisible(False)
        self.csr_progress.setToolTip('auto.bref_progress, read as percent (an assumption; not verified on hardware)')
        self.apply_btn = QPushButton('Apply')
        self.apply_btn.setToolTip('Send Resolution, Sample Rate, Exposure, EDR and Last to the camera and read '
                                  'back what it accepted.')
        self.apply_btn.clicked.connect(self.apply)
        self.readback_label = QLabel('Camera accepted: -', wordWrap=True)
        g = QGridLayout()
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(4)
        rows = (('Cine', self.cine_combo, ''), ('Resolution', self.res_combo, ''),
                ('Sample Rate', self.rate_combo, 'fps'), ('Exposure Time', self.exp_spin, 'µs'),
                ('EDR', self.edr_spin, 'µs'))
        for i, (lab, w, unit) in enumerate(rows):
            g.addWidget(QLabel(lab), i, 0)
            g.addWidget(w, i, 1)
            if unit:
                g.addWidget(QLabel(unit), i, 2)
        g.setColumnStretch(1, 1)
        btns = QHBoxLayout()
        btns.addWidget(self.csr_btn)
        btns.addWidget(self.csr_progress, 1)
        btns.addStretch(1)
        btns.addWidget(self.apply_btn)
        self.apply_preview = QLabel('', wordWrap=True)
        self.apply_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.apply_preview.setStyleSheet('QLabel { color: #404040; font-family: Consolas, monospace; }')
        for sig in (self.res_combo.currentTextChanged, self.rate_combo.currentTextChanged):
            sig.connect(lambda _: self._update_apply_preview())
        for spin in (self.exp_spin, self.edr_spin):
            spin.valueChanged.connect(lambda _: self._update_apply_preview())

        # Image Range and Trigger Position (p.35)
        self.pre_label = QLabel('-')
        self.pt_spin = QSpinBox(minimum=0, maximum=100_000_000, value=0)
        self.pt_spin.setToolTip('Post-trigger frames')
        self.trigger_bar = TriggerBar()
        self.duration_label = QLabel('Duration: -')
        self.pretrig_label = QLabel('Pretrigger Time: -')
        self.pt_spin.valueChanged.connect(self._update_range)
        self.pt_spin.valueChanged.connect(lambda _: self._update_apply_preview())
        self.rate_combo.currentTextChanged.connect(lambda _: self._update_range())
        self.trigger_bar.post_changed.connect(self.pt_spin.setValue)
        gr = QGroupBox('Image Range and Trigger Position')
        v = QVBoxLayout(gr)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(2)
        top = QHBoxLayout()
        top.addWidget(self.pre_label)
        top.addStretch(1)
        top.addWidget(QLabel('Last:'))
        top.addWidget(self.pt_spin)
        v.addLayout(top)
        v.addWidget(self.trigger_bar)
        v.addWidget(self.duration_label)
        v.addWidget(self.pretrig_label)

        cine_w = QWidget()
        v = QVBoxLayout(cine_w)
        v.setContentsMargins(6, 4, 4, 4)
        v.addWidget(self.meta_form)          # Name / Description (PCC p.33), applied on their own
        v.addLayout(g)
        v.addLayout(btns)
        v.addWidget(self.apply_preview)
        v.addWidget(gr)
        v.addWidget(self.readback_label)
        self.cine_settings = CollapsibleSection('Cine Settings', cine_w, expanded=True)

        # -- Advanced Settings (p.47-49): Cine Advanced (burst, shutter offset; sent with configure) first
        adv = QWidget()
        v = QVBoxLayout(adv)
        v.setContentsMargins(6, 4, 4, 4)
        v.addWidget(QLabel('<b>Cine Advanced</b>'))
        v.addWidget(self.cine_adv_form)
        v.addWidget(QLabel('<b>Recording actions, sync, start-up, fans, time code</b>'))
        v.addWidget(self.adv_form)
        self.advanced_settings = CollapsibleSection('Advanced Settings', adv)
        self.camera_signals = CollapsibleSection('Camera Signals', self._padded(self.signals_form))
        self.auto_exposure = CollapsibleSection('Auto Exposure', self._padded(self.aexp_form))

        # -- Image-Based Auto-Trigger (p.115-117): fields, plus the area drawn on / shown on the live image
        self.roi_draw_btn = QPushButton('Draw on live image')
        self.roi_draw_btn.setToolTip('Drag a rectangle on the live image; it fills x, y, w, h here (not sent until '
                                     f'Apply). Coordinate convention: {CS.ROI_CONVENTION}.')
        self.roi_draw_btn.clicked.connect(self.roi_draw_requested)
        self.roi_show_check = QCheckBox('Show on image')
        self.roi_show_check.setToolTip('Yellow dotted box on the live image (screen only). '
                                       f'Convention: {CS.ROI_CONVENTION}.')
        self.roi_show_check.toggled.connect(lambda _: self._emit_roi())
        self.ibat_form.edited.connect(self._emit_roi)
        self.settings.updated.connect(self._emit_roi)
        ib = QWidget()
        v = QVBoxLayout(ib)
        v.setContentsMargins(6, 4, 4, 4)
        v.addWidget(self.ibat_form)
        r = QHBoxLayout()
        r.addWidget(self.roi_draw_btn)
        r.addWidget(self.roi_show_check)
        r.addStretch(1)
        v.addLayout(r)
        note = QLabel(f'Region x, y: {CS.ROI_CONVENTION}. PCC p.117: never rely on it where a missed or '
                      'false trigger could cause harm.', wordWrap=True)
        note.setStyleSheet('QLabel { color: #606060; }')
        v.addWidget(note)
        self.auto_trigger = CollapsibleSection('Image-Based Auto-Trigger', ib)

        # -- Camera Info (p.38)
        self.info_labels = {k: QLabel('-') for k in ('name', 'serial', 'ip', 'hwver', 'firmware', 'ram', 'cinemem')}
        ci = QWidget()
        f = QFormLayout(ci)
        f.setContentsMargins(6, 4, 4, 4)
        for k, text in (('name', 'Camera Name'), ('serial', 'Serial'), ('ip', 'Ip'), ('hwver', 'Hardware version'),
                        ('firmware', 'PhFW version'), ('ram', 'RAM (MB)'), ('cinemem', 'cinemem')):
            self.info_labels[k].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            f.addRow(text, self.info_labels[k])
        self.info_labels['ram'].setToolTip('info.memsz')
        self.camera_info = CollapsibleSection('Camera Info', ci)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(1)
        self.sections = (self.camera_settings, self.cine_settings, self.advanced_settings, self.camera_signals,
                         self.auto_exposure, self.auto_trigger, self.camera_info)     # PCC's order (p.30)
        for s in self.sections:
            bv.addWidget(s)
        bv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)

        # -- pinned: Capture / Trigger (p.27, p.36)
        self.capture_btn = big_button('Capture', RED)
        self.capture_btn.setToolTip('Start recording (Ctrl+R). While recording: Abort Recording.')
        self.trigger_btn = big_button('Trigger', GREEN)
        self.trigger_btn.setToolTip('Software trigger (Ctrl+T); available while a cine waits for a trigger.')
        self.capture_btn.clicked.connect(self.capture)
        self.trigger_btn.clicked.connect(self.trigger)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(6, 6, 6, 6)
        bottom.addWidget(self.capture_btn)
        bottom.addWidget(self.trigger_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 4, 2, 2)
        lay.setSpacing(2)
        lay.addWidget(QLabel('Camera:'))
        lay.addWidget(self.camera_combo)
        lay.addWidget(scroll, 1)
        lay.addLayout(bottom)

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_INTERVAL_MS)
        self.timer.timeout.connect(self.refresh)
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._tick_clock)
        self.settings.updated.connect(self._tick_clock)
        self.clock.start()
        self.set_session(None)

    @staticmethod
    def _padded(w: QWidget) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 4, 4, 4)
        v.addWidget(w)
        return box

    def _tick_clock(self):
        self.time_label.setText(time.strftime('%a %b %d %Y %H:%M:%S'))
        self.camclock_label.setText(format_clock(self.settings.camera_clock()) if self.session is not None else '-')

    # ------------------------------------------------------------------ session
    def set_session(self, session: CameraSession | None, acquisition: dict | None = None):
        self.session = session
        self.cine_states, self.cine_infos = {}, {}
        self.last_readback = None
        self.recording = False
        self.camera_combo.clear()
        self.cine_combo.clear()
        self.partition_combo.clear()
        for w in self.sections + (self.capture_btn, self.camera_combo):
            w.setEnabled(session is not None)
        self.trigger_btn.setEnabled(False)
        self.capture_btn.setText('Capture')
        self.backup_label.setText('')
        self.csr_progress.setVisible(False)
        self.settings.set_session(session)      # reads with 'get' only; nothing is written on connect
        if session is None:
            self.timer.stop()
            self.apply_preview.setText('')
            for lab in self.info_labels.values():
                lab.setText('-')
            self.bitdepth_label.setText('-')
            self.readback_label.setText('Camera accepted: -')
            self.cines_changed.emit()
            return
        info = session.info
        self.camera_combo.addItem(f'{info.get("name") or info.get("serial")}'
                                  + (' (simulated)' if session.simulated else ''))
        self.info_labels['name'].setText(_fmt(info.get('name')))
        self.info_labels['serial'].setText(_fmt(info.get('serial')))
        self.info_labels['ip'].setText(session.address)
        self.info_labels['hwver'].setText(_fmt(info.get('hwver')))
        self.info_labels['firmware'].setText(f'{_fmt(info.get("fver"))} (swver {_fmt(info.get("swver"))})')
        self.info_labels['ram'].setText(_fmt(info.get('memsz')))
        self.info_labels['cinemem'].setText(_fmt(info.get('cinemem')))
        self.bitdepth_label.setText(_fmt(session.extra.get('membpp')))
        self.res_combo.clear()
        xmax, ymax = int(info.get('xmax') or 0), int(info.get('ymax') or 0)
        if xmax and ymax:
            for d in (1, 2, 4):
                self.res_combo.addItem(f'{xmax // d} x {ymax // d}')
        maxparts = max(1, int(info.get('maxcines') or 64) - 1)   # cine 0 is the preview cine
        for n in range(1, maxparts + 1):
            self.partition_combo.addItem(str(n), n)
        bref = 'bref' in str(info.get('features', '')).split()
        self.bref_available = bref
        self.csr_btn.setEnabled(bref)
        self.csr_btn.setToolTip('Current Session Reference (black reference, "bref"): cover the lens first.' if bref
                                else 'This camera does not list the "bref" feature, so CSR is not available.')
        if acquisition:
            self._fill_form(acquisition)
        self.timer.start()
        self.refresh()

    def _fill_form(self, d: dict):
        res = d.get('res')
        if isinstance(res, P.Resolution):
            self.res_combo.setEditText(f'{res.width} x {res.height}')
        if d.get('rate') is not None:
            self.rate_combo.setEditText(_fmt(d['rate']))
        if d.get('exp') is not None:
            self.exp_spin.setValue(int(d['exp']) / NS_PER_US)
        if d.get('edrexp') is not None:
            self.edr_spin.setValue(int(d['edrexp']) / NS_PER_US)
        if d.get('frcount') is not None:
            self.frcount = int(d['frcount'])
        if d.get('ptframes') is not None:
            self.pt_spin.setValue(int(d['ptframes']))
        self._update_range()
        self._update_apply_preview()

    def _update_apply_preview(self):
        """The exact line Cine Settings' Apply will send (it always sends these five fields)."""
        from ..camera import acquisition_update
        if self.session is None:
            return
        try:
            self.apply_preview.setText('Apply will send:\n' + P.set_line('defc', acquisition_update(
                **self.requested_settings()), ' '))
        except ValueError as e:
            self.apply_preview.setText(f'Cannot apply: {e}')

    def _update_range(self):
        """Duration and pretrigger lines from frcount, post-trigger frames and the sample rate (p.35)."""
        total, post = self.frcount, self.pt_spin.value()
        self.trigger_bar.set_values(total, post)
        try:
            rate = float(self.rate_combo.currentText())
        except ValueError:
            rate = 0.0
        if not total or rate <= 0:
            self.pre_label.setText('-')
            self.duration_label.setText('Duration: -')
            self.pretrig_label.setText('Pretrigger Time: -')
            return
        pre = max(total - post, 0)
        self.pre_label.setText(str(-pre))
        self.duration_label.setText(f'Duration: {total / rate:.3f}s ({total}p)')
        self.pretrig_label.setText(f'Pretrigger Time: {-pre / rate:.3f}s ({-pre}p)')

    # ------------------------------------------------------------------ commands
    def _run(self, what: str, fn, on_done=None):
        session = self.session
        if session is None:
            self.error.emit('Not connected')
            return None

        def job(task):
            with session.use() as cam:
                return fn(cam)

        def done(r):
            if session is not self.session:
                return
            self.message.emit(what)
            if on_done:
                on_done(r)
            self.refresh()

        def failed(e):
            self.error.emit(f'{what} failed: {describe_error(e)}')

        return self.tasks.submit(job, done, failed)

    def set_resolution(self, w: int, h: int):
        self.res_combo.setEditText(f'{w} x {h}')

    def set_rate(self, rate: float):
        self.rate_combo.setEditText(_fmt(rate))

    def requested_settings(self) -> dict:
        m = _RES_RE.match(self.res_combo.currentText())
        if not m:
            raise ValueError(f'Resolution must be "W x H", not {self.res_combo.currentText()!r}')
        rate = float(self.rate_combo.currentText())
        if rate <= 0:
            raise ValueError('Sample Rate must be positive')
        return dict(resolution=(int(m.group(1)), int(m.group(2))), rate=int(rate) if rate.is_integer() else rate,
                    exposure_ns=int(round(self.exp_spin.value() * NS_PER_US)),
                    edr_exposure_ns=int(round(self.edr_spin.value() * NS_PER_US)), post_trigger=self.pt_spin.value())

    def apply(self):
        try:
            req = self.requested_settings()
        except ValueError as e:
            self.error.emit(str(e))
            return None

        def show(d: dict):
            self.last_readback = d
            self._fill_form(d)
            exp = d.get('exp')
            exp_txt = f'{int(exp) / NS_PER_US:g} µs ({int(exp)} ns)' if exp is not None else '-'
            self.readback_label.setText(f'Camera accepted: {_fmt(d.get("res"))}, {_fmt(d.get("rate"))} fps, '
                                        f'exposure {exp_txt}, {_fmt(d.get("ptframes"))} post-trigger frames')
        return self._run('Settings applied', lambda cam: cam.configure(**req), show)

    def selected_cine(self) -> int | None:
        """Cine chosen for Capture: a number, or None for the next free one ('Preview')."""
        c = self.cine_combo.currentData()
        return None if not c else int(c)

    def select_cine(self, cine: int | None):
        self.cine_combo.setCurrentIndex(max(0, self.cine_combo.findData(cine or 0)))

    def capture(self):
        """Capture, or Abort Recording (``rec 0``) while recording."""
        if self.recording:
            return self._run('Recording aborted (rec 0, back to preview)', lambda cam: cam.preview())
        cine = self.selected_cine()
        session = self.session
        if cine and 'STR' in flags(self.cine_states.get(f'c{cine}')):
            if QMessageBox.warning(self, 'Erase the recording in this cine?',
                                   f'Cine {cine} holds a stored recording. Capture into cine {cine} ERASES it '
                                   '(save it first if you need it). Record into it anyway?',
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return None

        def rec(cam):
            # a re-recorded cine must not be described by the details of its previous recording
            for k in [k for k in session.cine_info_cache if cine is None or k[0] == cine]:
                del session.cine_info_cache[k]
            cam.record(cine)
        return self._run(f'Recording into cine {cine if cine else "(next free)"}; waiting for trigger', rec)

    def trigger(self):
        if not self.trigger_btn.isEnabled():
            return None
        return self._run('Triggered', lambda cam: cam.trigger())

    def preview(self):
        return self._run('Preview (rec 0)', lambda cam: cam.preview())

    def csr(self):
        """CSR (PCC p.34): confirm, send ``bref``, then follow ``auto.bref_progress`` on a progress bar."""
        session = self.session
        if session is None or not self.csr_btn.isEnabled():
            return None
        text = ('Cover the lens: the sensor must be dark (PCC p.34). The camera then takes a new black reference '
                'for the current settings, replacing the one it holds. Continue?')
        if self.settings.current.get('auto.trigger.mode'):
            text += ('\n\nImage-Based Auto-Trigger is enabled (auto.trigger.mode = '
                     f'{self.settings.current["auto.trigger.mode"]}); PCC p.34 says to disable it before a CSR.')
        if QMessageBox.question(self, 'CSR', text,
                                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel) \
                != QMessageBox.StandardButton.Ok:
            return None
        has_progress = 'auto.bref_progress' in CS.flatten('auto', self.settings.structs.get('auto', {}), {})
        self.csr_btn.setEnabled(False)
        self.csr_progress.setValue(0)
        self.csr_progress.setVisible(True)

        def job(task):
            with session.use() as cam:
                if not has_progress:
                    cam.command('bref')
                    return dict(confirmed=False, reason='this camera does not report auto.bref_progress; '
                                                        'completion not confirmed')
                return cam.black_reference(progress=task.progress, timeout=CSR_TIMEOUT_S)

        def finish():
            self.csr_btn.setEnabled(session is self.session and self.bref_available)
            self.csr_progress.setVisible(False)

        def done(r):
            finish()
            if session is not self.session:
                return
            self.last_csr = r
            if r['confirmed']:
                self.message.emit(f'CSR done ({r["reason"]})')
            else:
                self.error.emit(f'CSR sent; {r["reason"]}')

        def failed(e):
            finish()
            self.error.emit(f'CSR failed: {describe_error(e)}')
        return self.tasks.submit(job, done, failed, lambda d, t: self.csr_progress.setValue(d))

    # ------------------------------------------------------------------ clock (PCC p.30-32)
    def set_time(self):
        """Set Time..: a dialog states what changes; only its Set button sends ``setrtc``."""
        if self.session is None:
            return None
        dlg = SetTimeDialog(self.settings.camera_clock(), 'timezone' in self.settings.structs.get('cam', {}), self)
        self._set_time_dialog = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return self.send_time(dlg.with_timezone)

    def send_time(self, with_timezone: bool):
        """Send the PC clock (``setrtc``, and ``set cam.timezone`` if asked), then re-read. Called only after
        the Set Time dialog was accepted."""
        session = self.session
        if session is None:
            return None
        tz = pc_timezone_west()

        def job(task):
            with session.use() as cam:
                cam.set_clock(int(time.time()))
                if with_timezone:
                    cam.set('cam.timezone', tz)
                return CS.read_settings(cam)

        def done(r):
            if session is self.session:
                self.settings._push(*r)
                self.message.emit(f'Camera clock set: {format_clock(self.settings.camera_clock())}'
                                  + (f'; cam.timezone = {r[1].get("cam", {}).get("timezone")}' if with_timezone else ''))

        def failed(e):
            self.error.emit(f'Set Time failed: {describe_error(e)}')
        return self.tasks.submit(job, done, failed)

    # ------------------------------------------------------------------ backup & restore (PCC p.31-32)
    def save_backup(self, path: str | None = None):
        """Re-read the camera (``get`` only) and save its settings to a JSON file on this computer."""
        session = self.session
        if session is None:
            return None
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, 'Save camera settings',
                                                  f'phantastic_settings_{session.info.get("serial")}.json',
                                                  'Phantastic settings (*.json)')
            if not path:
                return None

        def job(task):
            with session.use() as cam:
                cur, structs = CS.read_settings(cam)
            CS.save_backup(path, cur, session.info)
            return cur, structs

        def done(r):
            if session is self.session:
                self.settings._push(*r)
                self.backup_label.setText(f'Saved {len(r[0])} settings to {path}')
                self.message.emit(f'Camera settings saved to {path}')

        def failed(e):
            self.error.emit(f'Saving camera settings failed: {describe_error(e)}')
        return self.tasks.submit(job, done, failed)

    def load_backup(self, path: str | None = None):
        """Show a saved file's differences from the camera as pending edits. NOTHING is sent: each selector's
        Apply sends its own part. Returns (wanted, notes) or None."""
        if self.session is None:
            return None
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, 'Load camera settings', '', 'Phantastic settings (*.json)')
            if not path:
                return None
        try:
            saved = CS.load_backup(path)
        except (OSError, ValueError) as e:
            self.error.emit(f'Cannot load camera settings: {e}')
            return None
        cur = self.settings.current
        wanted, notes = CS.backup_diff(cur, saved['settings'])
        for form in self.forms:
            form.set_wanted(wanted)
        cam = saved.get('camera') or {}
        lines = [f'{k}: camera {CS.display(cur[k])} -> file {CS.display(v)}' for k, v in wanted.items()]
        self.backup_label.setText(
            f'{Path(path).name} (saved {saved.get("saved")}, serial {cam.get("serial")}): '
            + (f'{len(wanted)} difference(s), shown as pending in the selectors below; NOT sent until you press '
               'Apply there.\n' + '\n'.join(lines) if wanted else 'no differences from the camera.')
            + ('\n' + '\n'.join(notes) if notes else ''))
        return wanted, notes

    # ------------------------------------------------------------------ auto-trigger area on the live image
    def _ibat_value(self, k: str) -> int | None:
        """auto.trigger.<k> as currently in the field (pending edits included), else as read."""
        key = f'auto.trigger.{k}'
        w = self.ibat_form.fields[key]
        try:
            return CS.parse(CS.BY_KEY[key], w.text())
        except ValueError:
            v = self.settings.current.get(key)
            return v if isinstance(v, int) else None

    def _live_size(self) -> tuple[int, int] | None:
        res = self.settings.structs.get('defc', {}).get('res')
        return (res.width, res.height) if isinstance(res, P.Resolution) else None

    def ibat_rect(self) -> tuple[int, int, int, int] | None:
        """The auto-trigger area in stored live-image pixels (left, top, w, h), by CS.ROI_CONVENTION."""
        size = self._live_size()
        vals = [self._ibat_value(k) for k in 'xywh']
        if size is None or None in vals or not all(k in self.settings.current for k in
                                                   ('auto.trigger.x', 'auto.trigger.y', 'auto.trigger.w',
                                                    'auto.trigger.h')):
            return None
        return CS.roi_from_camera(*vals, *size)

    def set_ibat_rect(self, rect):
        """A rectangle drawn on the live image -> the x, y, w, h fields (pending; sent only by Apply)."""
        size = self._live_size()
        if size is None or rect is None:
            return
        x, y, w, h = CS.roi_to_camera(*rect, *size)
        self.ibat_form.set_wanted({'auto.trigger.x': x, 'auto.trigger.y': y, 'auto.trigger.w': w, 'auto.trigger.h': h})

    def _emit_roi(self):
        self.roi_overlay_changed.emit(self.ibat_rect() if self.roi_show_check.isChecked() else None)

    def _partition_chosen(self, index: int):
        n = self.partition_combo.itemData(index)
        current = self._partition_count()
        if n is None or n == current:
            return
        if QMessageBox.warning(self, 'Erase all cines?',
                               f'Partitioning erases ALL recordings on the camera and splits memory into {n}. Continue?',
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                != QMessageBox.StandardButton.Yes:
            self._show_partitions()
            return
        self.partition(n)

    def partition(self, n: int):
        session = self.session

        def do(cam):
            cam.partition(n)
            session.cine_info_cache.clear()   # we hold the session lock here
        return self._run(f'Memory split into {n} partitions', do)

    def _partition_count(self) -> int | None:
        n = sum(1 for k in self.cine_states if int(k[1:]) >= 1)
        return n or None

    def _show_partitions(self):
        n = self._partition_count()
        if n is not None:
            i = self.partition_combo.findData(n)
            if i >= 0 and i != self.partition_combo.currentIndex():
                self.partition_combo.setCurrentIndex(i)

    # ------------------------------------------------------------------ cine states
    def refresh(self):
        """Poll cine states (and details of stored cines) without blocking or queueing up."""
        session = self.session
        if session is None or self._refresh_busy:
            return
        self._refresh_busy = True

        def job(task):
            with session.try_use() as cam:
                if cam is None:
                    return None
                states = cam.cine_states()
                infos = {}
                for key, st in states.items():
                    c = int(key[1:])
                    if isinstance(st, P.Flags) and 'STR' in st:
                        ck = (c, str(st))
                        if ck not in session.cine_info_cache:
                            session.cine_info_cache[ck] = cam.cine_info(c)
                        infos[c] = session.cine_info_cache[ck]
                    else:      # no longer stored: forget its details, a new recording gets fresh ones
                        for k in [k for k in session.cine_info_cache if k[0] == c]:
                            del session.cine_info_cache[k]
                session.trim_transcript()
                return states, infos

        def done(r):
            self._refresh_busy = False
            if r is not None and session is self.session:
                self.cine_states, self.cine_infos = r
                self._update_cines()

        def failed(e):
            self._refresh_busy = False
            self.error.emit(f'Cine status refresh failed: {describe_error(e)}')
        self.tasks.submit(job, done, failed)

    def _update_cines(self):
        keys = sorted(self.cine_states, key=lambda k: int(k[1:]))
        want = self.cine_combo.currentData()
        self.cine_combo.blockSignals(True)
        entries = [('Preview', 0)] + [(f'Cine {int(k[1:])}  {" ".join(flags(self.cine_states[k]))}', int(k[1:]))
                                      for k in keys if int(k[1:]) >= 1]
        if [self.cine_combo.itemData(i) for i in range(self.cine_combo.count())] != [d for _, d in entries]:
            self.cine_combo.clear()
            for text, d in entries:
                self.cine_combo.addItem(text, d)
        else:
            for i, (text, _) in enumerate(entries):
                if self.cine_combo.itemText(i) != text:
                    self.cine_combo.setItemText(i, text)
        i = self.cine_combo.findData(want if want is not None else 0)
        self.cine_combo.setCurrentIndex(max(i, 0))
        self.cine_combo.blockSignals(False)
        self._show_partitions()
        rec = is_recording(self.cine_states)
        self.trigger_btn.setEnabled(waiting_for_trigger(self.cine_states))
        if rec != self.recording:
            self.recording = rec
            self.recording_changed.emit(rec)
        self.capture_btn.setText('Abort Recording' if rec else 'Capture')
        self.cines_changed.emit()

    def stored_cines(self) -> list[int]:
        return sorted(c for c, ci in self.cine_infos.items() if ci.get('firstfr') is not None)

    def make_save_dialog(self, cine: int, marks: tuple[int, int] | None = None,
                         default_dir: str | None = None, panel=None) -> SaveCineDialog:
        info = self.cine_infos.get(cine)
        if info is None or info.get('firstfr') is None:
            raise ValueError(f'cine {cine} has no stored recording (state must contain STR)')
        # the default name is a template (PCC p.66-73) that expands to today's cine<N>_<serial>.cine
        default = str(Path(default_dir or Path.home()) / f'{DEFAULT_CINE_TEMPLATE}.cine')
        dlg = SaveCineDialog(self.tasks, self.session, cine, info, default_path=default, marks=marks,
                             parent=self.window(), panel=panel)
        self.dialogs = [d for d in self.dialogs if d.isVisible()] + [dlg]
        return dlg
