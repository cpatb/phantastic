"""PCC's Play tab: cine chooser, transport buttons, Jump, editor bar with Mark-In / Mark-Out,
playback options, Frame Info, Cine Info and the Save Cine... split button (p.53-62)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .icons import icon
from .panels import PlaybackPanel, format_abs_time
from .widgets import GREEN, CollapsibleSection, EditorBar, style_big_button, transport_button

FAST_STEP_DIVISOR, FAST_STEP_MIN = 1000, 10    # PCC p.53: fast = total/1000 frames, at least 10


def _short(v, limit: int = 120) -> str:
    s = str(v)
    return s if len(s) <= limit else s[: limit - 3] + '...'


class HeaderDialog(QDialog):
    """Every header, bitmap and setup field of a cine file (read only)."""

    def __init__(self, source, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f'Cine header: {source.title}')
        r = source.reader
        tree = QTreeWidget()
        tree.setHeaderLabels(['Field', 'Value'])
        tree.setColumnWidth(0, 200)
        groups = {'File': dict(source.info_rows()), 'Header': r.header, 'Bitmap': r.bitmap, 'Setup': r.setup}
        for name, d in groups.items():
            top = QTreeWidgetItem([name])
            for k, v in d.items():
                item = QTreeWidgetItem([str(k), _short(v)])
                item.setToolTip(1, str(v)[:4000])
                top.addChild(item)
            tree.addTopLevelItem(top)
            top.setExpanded(name in ('File', 'Header'))
        QVBoxLayout(self).addWidget(tree)
        self.resize(560, 600)


class PlayTab(QWidget):
    cine_chosen = Signal(object)       # key of the cine picked in the 'Cine:' combo
    save_requested = Signal(str)       # 'cine', 'save_all', 'tiff_raw', 'tiff_pcc', 'tiff_seq' or 'mp4'

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.panel: PlaybackPanel | None = None

        self.cine_combo = QComboBox()
        self.cine_combo.setToolTip('Stored camera cines (serial > Cine N) and open files')
        self.cine_combo.activated.connect(lambda i: self.cine_chosen.emit(self.cine_combo.itemData(i)))

        # -- transport (p.53)
        self.rew_btn = transport_button(icon('rewind'), 'Rewind (play in reverse)')
        self.pause_btn = transport_button(icon('pause'), 'Pause')
        self.play_btn = transport_button(icon('play'), 'Play')
        self.frew_btn = transport_button(icon('fastrew'), 'Fast Rewind (total/1000 images, at least 10)', 40, 34)
        self.stepb_btn = transport_button(icon('stepback'), 'Step Backward', 40, 34)
        self.stepf_btn = transport_button(icon('stepfwd'), 'Step Forward', 40, 34)
        self.ffwd_btn = transport_button(icon('fastfwd'), 'Fast Forward (total/1000 images, at least 10)', 40, 34)
        self.rew_btn.clicked.connect(lambda: self._do(lambda p: p.play(-1)))
        self.pause_btn.clicked.connect(lambda: self._do(lambda p: p.pause()))
        self.play_btn.clicked.connect(lambda: self._do(lambda p: p.play(+1)))
        self.frew_btn.clicked.connect(lambda: self._do(lambda p: p.step(-self.fast_step())))
        self.stepb_btn.clicked.connect(lambda: self._do(lambda p: p.step(-p.player_step)))
        self.stepf_btn.clicked.connect(lambda: self._do(lambda p: p.step(p.player_step)))
        self.ffwd_btn.clicked.connect(lambda: self._do(lambda p: p.step(self.fast_step())))
        r1, r2 = QHBoxLayout(), QHBoxLayout()
        for row, btns in ((r1, (self.rew_btn, self.pause_btn, self.play_btn)),
                          (r2, (self.frew_btn, self.stepb_btn, self.stepf_btn, self.ffwd_btn))):
            row.addStretch(1)
            for b in btns:
                row.addWidget(b)
            row.addStretch(1)
            row.setSpacing(3)

        # -- Jump (p.60)
        self.trig_btn = QPushButton('T')
        self.trig_btn.setToolTip('Jump to Trigger (image 0)')
        self.start_btn = QPushButton()
        self.start_btn.setIcon(icon('jumpstart'))
        self.start_btn.setToolTip('Jump to Start (Mark-In when Limit to Range is on)')
        self.end_btn = QPushButton()
        self.end_btn.setIcon(icon('jumpend'))
        self.end_btn.setToolTip('Jump to End (Mark-Out when Limit to Range is on)')
        for b in (self.trig_btn, self.start_btn, self.end_btn):
            b.setFixedSize(28, 24)
        self.goto_spin = QSpinBox(minimum=0, maximum=0)
        self.goto_spin.setMinimumWidth(60)       # its range would otherwise set the panel width
        self.goto_spin.setKeyboardTracking(False)
        self.goto_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.goto_spin.setToolTip('Image number to show (0 = trigger)')
        self.trig_btn.clicked.connect(lambda: self._do(lambda p: p.goto(0)))
        self.start_btn.clicked.connect(lambda: self._do(lambda p: p.goto(p.bounds()[0])))
        self.end_btn.clicked.connect(lambda: self._do(lambda p: p.goto(p.bounds()[1])))
        self.goto_spin.lineEdit().returnPressed.connect(self.goto_typed)
        jump = QGroupBox('Jump')
        jh = QHBoxLayout(jump)
        jh.setContentsMargins(6, 2, 6, 4)
        for b in (self.trig_btn, self.start_btn, self.end_btn):
            jh.addWidget(b)
        jh.addSpacing(8)
        jh.addWidget(QLabel('To #'))
        jh.addWidget(self.goto_spin, 1)

        # -- editor bar and marks (p.58-59)
        self.editor = EditorBar()
        self.editor.scrubbed.connect(lambda n: self._do(lambda p: p.goto(n)))
        self.markin_btn = QPushButton('[')
        self.markin_btn.setToolTip('Mark-In at the current image')
        self.markout_btn = QPushButton(']')
        self.markout_btn.setToolTip('Mark-Out at the current image')
        for b in (self.markin_btn, self.markout_btn):
            b.setFixedSize(26, 22)
        self.markin_btn.clicked.connect(lambda: self._do(lambda p: p.set_mark_in()))
        self.markout_btn.clicked.connect(lambda: self._do(lambda p: p.set_mark_out()))
        marks = QHBoxLayout()
        marks.addSpacing(20)
        marks.addWidget(self.markin_btn)
        marks.addWidget(self.markout_btn)
        marks.addStretch(1)

        # -- Play Speed & Options (p.55)
        self.limit_check = QCheckBox('Limit to Range')
        self.repeat_check = QCheckBox('Repeat')
        self.pingpong_check = QCheckBox('Ping Pong')
        self.fps_spin = QDoubleSpinBox(minimum=0.1, maximum=100.0, decimals=1, value=20.0)
        self.fps_spin.setToolTip('Playback rate on screen (display only)')
        self.fps_spin.setMaximumWidth(58)
        self.step_spin = QSpinBox(minimum=1, maximum=10 ** 6, value=1)
        self.step_spin.setMaximumWidth(58)
        loop = QGroupBox('Loop Options')
        lv = QVBoxLayout(loop)
        lv.setContentsMargins(4, 2, 4, 2)
        for c in (self.limit_check, self.repeat_check, self.pingpong_check):
            lv.addWidget(c)
        opts = QWidget()
        og = QGridLayout(opts)
        og.setContentsMargins(6, 4, 4, 4)
        og.addWidget(loop, 0, 0, 3, 1)
        og.addWidget(QLabel('fps'), 0, 1)
        og.addWidget(self.fps_spin, 0, 2)
        og.addWidget(QLabel('Player\nStep'), 1, 1)
        og.addWidget(self.step_spin, 1, 2)
        og.addWidget(QLabel('frames'), 2, 2)
        og.setColumnStretch(3, 1)
        for c in (self.limit_check, self.repeat_check, self.pingpong_check):
            c.toggled.connect(self._options_changed)
        self.fps_spin.valueChanged.connect(self._options_changed)
        self.step_spin.valueChanged.connect(self._options_changed)
        self.speed = CollapsibleSection('Play Speed & Options', opts)

        # -- Frame Info (p.55-56)
        self.fi = {k: QLabel('-') for k in ('time', 'elapsed', 'image', 'exposure')}
        fw = QWidget()
        ff = QFormLayout(fw)
        ff.setContentsMargins(6, 4, 4, 4)
        for k, text in (('time', 'Time'), ('elapsed', 'Elapsed Time\nfrom Trigger'), ('image', 'Image#'),
                        ('exposure', 'Exposure')):
            self.fi[k].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.fi[k].setWordWrap(True)
            ff.addRow(text, self.fi[k])
        self.frame_info = CollapsibleSection('Frame Info', fw, expanded=True)

        # -- Cine Info (p.57-58)
        self.ci_widget = QWidget()
        self.ci_form = QFormLayout(self.ci_widget)
        self.ci_form.setContentsMargins(6, 4, 4, 4)
        self.header_btn = QPushButton('All header fields...')
        self.header_btn.clicked.connect(self.show_header)
        civ = QWidget()
        cv = QVBoxLayout(civ)
        cv.setContentsMargins(0, 0, 0, 4)
        cv.addWidget(self.ci_widget)
        cv.addWidget(self.header_btn, 0, Qt.AlignmentFlag.AlignRight)
        self.cine_info = CollapsibleSection('Cine Info', civ)

        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(1)
        for s in (self.speed, self.frame_info, self.cine_info):
            bv.addWidget(s)
        bv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)

        # -- Save Cine... split button (p.62)
        self.save_btn = QToolButton()
        self.save_btn.setText('Save Cine...')
        self.save_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.save_btn.setMinimumSize(150, 40)
        self.save_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        style_big_button(self.save_btn, GREEN)
        menu = QMenu(self.save_btn)
        self.save_action = menu.addAction('Save Cine To File', lambda: self.save_requested.emit('cine'))
        self.tiff_raw_action = menu.addAction('Export TIFF (raw values)...', lambda: self.save_requested.emit('tiff_raw'))
        self.save_all_action = menu.addAction('Save All RAM Cines to File...',
                                              lambda: self.save_requested.emit('save_all'))   # p.62
        menu.addSeparator()
        self.tiff_pcc_action = menu.addAction('Export TIFF as PCC would (8-bit)...',
                                              lambda: self.save_requested.emit('tiff_pcc'))
        self.tiff_seq_action = menu.addAction('Export TIFF image sequence...',
                                              lambda: self.save_requested.emit('tiff_seq'))   # p.72, 76
        self.mp4_action = menu.addAction('Export MP4 (8-bit display render)...',
                                         lambda: self.save_requested.emit('mp4'))           # p.75
        self.save_btn.setMenu(menu)
        self.save_btn.clicked.connect(lambda: self.save_requested.emit('cine'))
        self.save_btn.setToolTip('Save Cine To File (Ctrl+S); the arrow offers TIFF exports')
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_row.addWidget(self.save_btn)
        save_row.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 4, 2, 6)
        lay.setSpacing(3)
        lay.addWidget(QLabel('Cine:'))
        lay.addWidget(self.cine_combo)
        lay.addLayout(r1)
        lay.addLayout(r2)
        lay.addWidget(jump)
        lay.addWidget(self.editor)
        lay.addLayout(marks)
        lay.addWidget(scroll, 1)
        lay.addLayout(save_row)
        self.set_panel(None)

    # ------------------------------------------------------------------ cine list
    def set_entries(self, entries: list[tuple[str, object]]):
        current = self.cine_combo.currentData()
        self.cine_combo.blockSignals(True)
        self.cine_combo.clear()
        for title, key in entries:
            self.cine_combo.addItem(title, key)
        i = self.index_of(current) if current is not None else -1
        self.cine_combo.setCurrentIndex(i if i >= 0 else (0 if entries else -1))
        self.cine_combo.blockSignals(False)
        if self.panel is not None:
            self.select_key(self.panel.source.key)

    def index_of(self, key) -> int:
        """Combo index of a key (tuples are compared in Python; QComboBox.findData cannot)."""
        for i in range(self.cine_combo.count()):
            if self.cine_combo.itemData(i) == key:
                return i
        return -1

    def select_key(self, key):
        i = self.index_of(key)
        if i >= 0:
            self.cine_combo.setCurrentIndex(i)

    # ------------------------------------------------------------------ panel
    def set_panel(self, panel: PlaybackPanel | None):
        if self.panel is not None:
            try:
                self.panel.position_changed.disconnect(self.update_view)
            except (RuntimeError, TypeError):
                pass
        self.panel = panel
        controls = (self.rew_btn, self.pause_btn, self.play_btn, self.frew_btn, self.stepb_btn, self.stepf_btn,
                    self.ffwd_btn, self.trig_btn, self.start_btn, self.end_btn, self.goto_spin, self.markin_btn,
                    self.markout_btn, self.save_btn, self.speed, self.header_btn)
        for w in controls:
            w.setEnabled(panel is not None)
        while self.ci_form.rowCount():
            self.ci_form.removeRow(0)
        if panel is None:
            self.editor.clear()
            for lab in self.fi.values():
                lab.setText('-')
            return
        panel.position_changed.connect(self.update_view)
        self.select_key(panel.source.key)
        for w in (self.limit_check, self.repeat_check, self.pingpong_check, self.fps_spin, self.step_spin):
            w.blockSignals(True)
        self.limit_check.setChecked(panel.limit_to_range)
        self.repeat_check.setChecked(panel.repeat)
        self.pingpong_check.setChecked(panel.ping_pong)
        self.fps_spin.setValue(panel.fps)
        self.step_spin.setValue(panel.player_step)
        for w in (self.limit_check, self.repeat_check, self.pingpong_check, self.fps_spin, self.step_spin):
            w.blockSignals(False)
        for label, value in panel.source.info_rows():
            v = QLabel(value)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.ci_form.addRow(label, v)
        is_file = panel.source.kind == 'file'
        self.header_btn.setVisible(is_file)
        # raw TIFF, TIFF sequence and MP4 work straight from camera RAM; PCC-identical TIFF needs a file's
        # display settings; Save All is for the camera's RAM cines
        self.tiff_pcc_action.setEnabled(is_file)
        self.tiff_pcc_action.setToolTip('' if is_file else 'Needs a saved file\'s display settings: save the cine, '
                                        'open the file, then export.')
        self.save_all_action.setEnabled(not is_file)
        from ..export import MP4_MISSING, find_ffmpeg
        have = find_ffmpeg() is not None
        self.mp4_action.setEnabled(have)
        self.mp4_action.setToolTip('' if have else MP4_MISSING)
        self.goto_spin.setRange(panel.source.first, panel.source.last)
        self.update_view()

    def goto_typed(self):
        """Enter in 'To #': go to the typed image number."""
        self.goto_spin.interpretText()
        self.goto_spin.lineEdit().setModified(False)
        self._do(lambda p: p.goto(self.goto_spin.value()))

    def _do(self, fn):
        if self.panel is not None:
            fn(self.panel)

    def fast_step(self) -> int:
        p = self.panel
        total = p.source.last - p.source.first + 1 if p else 0
        return max(FAST_STEP_MIN, total // FAST_STEP_DIVISOR)

    def _options_changed(self):
        p = self.panel
        if p is None:
            return
        p.limit_to_range = self.limit_check.isChecked()
        p.repeat = self.repeat_check.isChecked()
        p.ping_pong = self.pingpong_check.isChecked()
        p.player_step = self.step_spin.value()
        p.set_fps(self.fps_spin.value())

    def update_view(self):
        p = self.panel
        if p is None:
            return
        self.editor.set_state(p.source.first, p.source.last, p.cur, p.mark_in, p.mark_out)
        if not self.goto_spin.lineEdit().isModified():    # never overwrite what the user is typing
            self.goto_spin.blockSignals(True)
            self.goto_spin.setValue(p.cur)
            self.goto_spin.blockSignals(False)
        if p.frame is None:
            return
        info = p.frame_info()
        self.fi['time'].setText(format_abs_time(info['abs'], '\n'))
        self.fi['elapsed'].setText(f'{info["elapsed_s"] * 1e6:.2f} µs' + (' (from rate)' if info['synthesized'] else ''))
        img = str(info['number'])
        if info['origin'] is not None:
            img += f' ({info["origin"][0]} image {info["origin"][1]})'
        if p.shown != p.cur:
            img += f'  (loading {p.cur}...)'
        self.fi['image'].setText(img)
        exp = info['exposure_us']
        # rounded to 1 ns: a file stores exposure in 2^-32 s units, so 40 us reads back as 39.99998 us
        self.fi['exposure'].setText('-' if exp is None else f'{round(exp, 3):g} µs')

    def show_header(self):
        if self.panel is not None and self.panel.source.kind == 'file':
            dlg = HeaderDialog(self.panel.source, self)
            dlg.show()
            self._header_dialog = dlg
