"""PCC's Manager tab: camera and file tree with the button rows above and below it (p.17, p.108-111)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QMenu, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .icons import icon

ROLE = Qt.ItemDataRole.UserRole


def _icon_button(name: str, tip: str) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(name))
    b.setIconSize(QSize(28, 28))
    b.setFixedSize(38, 38)
    b.setToolTip(tip)
    b.setAutoRaise(False)
    return b


class ManagerTab(QWidget):
    """Tree items carry a key in UserRole: ('camera', ip, port), ('cine', cine) or ('file', path)."""
    activated = Signal(object)     # double-click on a camera, camera cine or file
    remove_requested = Signal(object)
    disconnect_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.discover_btn = _icon_button('discover', 'Discover cameras on the network')
        self.add_sim_btn = _icon_button('addsim', 'Add Simulated Camera (starts the built-in simulator) (Ctrl+A)')
        self.remove_btn = _icon_button('remove', 'Remove from tree (a camera is disconnected, a file closed)')
        self.ip_btn = _icon_button('connectip', 'Connect by IP...')
        top = QHBoxLayout()
        top.setContentsMargins(8, 8, 8, 4)
        for b in (self.discover_btn, self.add_sim_btn, self.remove_btn, self.ip_btn):
            top.addWidget(b)
        top.addStretch(1)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.cameras_root = QTreeWidgetItem(['Cameras'])
        self.files_root = QTreeWidgetItem(['Files'])
        for r in (self.cameras_root, self.files_root):
            r.setIcon(0, icon('folder'))
            self.tree.addTopLevelItem(r)
            r.setExpanded(True)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.remove_btn.clicked.connect(self._remove_selected)

        self.test_btn = _icon_button('wrench', 'Run camera test...')
        self.network_btn = _icon_button('network', 'Network check: list this computer\'s network adapters')
        self.logs_btn = _icon_button('gear', 'Open log folder')
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 8)
        for b in (self.test_btn, self.network_btn, self.logs_btn):
            bottom.addWidget(b)
        bottom.addStretch(1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.tree, 1)
        lay.addLayout(bottom)

    # ------------------------------------------------------------------ cameras
    def camera_items(self) -> list[QTreeWidgetItem]:
        return [self.cameras_root.child(i) for i in range(self.cameras_root.childCount())]

    def add_camera(self, text: str, ip: str, port: int) -> QTreeWidgetItem:
        key = ('camera', ip, port)
        for it in self.camera_items():
            if it.data(0, ROLE) == key:
                it.setText(0, text)
                return it
        it = QTreeWidgetItem([text])
        it.setIcon(0, icon('camera'))
        it.setData(0, ROLE, key)
        self.cameras_root.addChild(it)
        self.cameras_root.setExpanded(True)
        return it

    def set_cameras(self, found: list[tuple[str, str, int]], keep: tuple[str, int] | None = None):
        """Replace the discovered cameras; the connected one (``keep``) stays."""
        for it in self.camera_items():
            _, ip, port = it.data(0, ROLE)
            if keep is None or (ip, port) != keep:
                self.cameras_root.removeChild(it)
        for text, ip, port in found:
            self.add_camera(text, ip, port)

    def set_connected(self, address: tuple[str, int] | None, cines: list[tuple[int, str]] = ()):
        """Bold the connected camera and list its stored cines under it."""
        for it in self.camera_items():
            _, ip, port = it.data(0, ROLE)
            on = address is not None and (ip, port) == tuple(address)
            f = QFont(it.font(0))
            f.setBold(on)
            it.setFont(0, f)
            want = list(cines) if on else []
            have = [(it.child(i).data(0, ROLE)[1], it.child(i).text(0)) for i in range(it.childCount())]
            if have != [(c, text) for c, text in want]:
                it.takeChildren()
                for c, text in want:
                    ch = QTreeWidgetItem([text])
                    ch.setIcon(0, icon('cine'))
                    ch.setData(0, ROLE, ('cine', c))
                    it.addChild(ch)
                it.setExpanded(True)

    # ------------------------------------------------------------------ files
    def add_file(self, path: str):
        for i in range(self.files_root.childCount()):
            if self.files_root.child(i).data(0, ROLE) == ('file', path):
                self.files_root.removeChild(self.files_root.child(i))
                break
        it = QTreeWidgetItem([Path(path).name])
        it.setToolTip(0, path)
        it.setIcon(0, icon('cine'))
        it.setData(0, ROLE, ('file', path))
        self.files_root.insertChild(0, it)
        self.files_root.setExpanded(True)

    def remove_file(self, path: str):
        for i in range(self.files_root.childCount()):
            if self.files_root.child(i).data(0, ROLE) == ('file', path):
                self.files_root.takeChild(i)
                return

    def find(self, key) -> QTreeWidgetItem | None:
        stack = [self.cameras_root, self.files_root]
        while stack:
            it = stack.pop()
            if it.data(0, ROLE) == key:
                return it
            stack.extend(it.child(i) for i in range(it.childCount()))
        return None

    # ------------------------------------------------------------------ interaction
    def selected_key(self):
        it = self.tree.currentItem()
        return None if it is None else it.data(0, ROLE)

    def _double_clicked(self, item: QTreeWidgetItem, _col: int):
        key = item.data(0, ROLE)
        if key is not None:
            self.activated.emit(key)

    def _remove_selected(self):
        key = self.selected_key()
        if key is not None:
            self.remove_requested.emit(key)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        key = None if item is None else item.data(0, ROLE)
        if key is None:
            return
        m = QMenu(self)
        if key[0] == 'camera':
            m.addAction('Connect and show preview', lambda: self.activated.emit(key))
            m.addAction('Disconnect', self.disconnect_requested.emit)
        else:
            m.addAction('Open in Play', lambda: self.activated.emit(key))
        if key[0] != 'cine':
            m.addAction('Remove from tree', lambda: self.remove_requested.emit(key))
        m.exec(self.tree.viewport().mapToGlobal(pos))
