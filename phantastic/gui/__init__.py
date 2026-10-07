"""Phantastic desktop GUI (PySide6). Run with ``python -m phantastic.gui``."""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='phantastic-gui', description='Phantastic camera control and cine viewer')
    ap.add_argument('files', nargs='*', help='.cine files to open')
    ap.add_argument('--simulator', action='store_true', help='start the simulated camera and connect to it')
    ap.add_argument('--dark', action='store_true', help='dark theme instead of the light PCC-like one')
    ap.add_argument('--light', action='store_true', help=argparse.SUPPRESS)   # the default since the PCC layout
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    from .logs import setup_app_logging
    setup_app_logging()

    from PySide6.QtWidgets import QApplication
    from .main_window import MainWindow, apply_dark_palette, apply_pcc_style

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Phantastic')
    if args.dark:
        apply_dark_palette(app)
    else:
        apply_pcc_style(app)
    win = MainWindow()
    if win.geometry_restored:               # last session's window (QSettings)
        win.show()
    else:
        win.resize(1456, 868)
        win.showMaximized()                 # this user's PCC: StartMaximized=true
    for f in args.files:
        win.open_file(f)
    if args.simulator:
        win.start_simulator()
    return app.exec()
