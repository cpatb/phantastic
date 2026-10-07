"""Where the app writes its logs: one file per camera connection plus one for the app itself.

``%LOCALAPPDATA%\\Phantastic\\logs`` on Windows (``~/.phantastic/logs`` elsewhere). The camera logs
record every command, response and data transfer with timestamps, so a problem seen with real
hardware can be diagnosed afterwards.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path


def log_dir() -> Path:
    base = os.environ.get('LOCALAPPDATA')
    d = Path(base) / 'Phantastic' / 'logs' if base else Path.home() / '.phantastic' / 'logs'
    d.mkdir(parents=True, exist_ok=True)
    return d


def camera_log_path(ip: str) -> Path:
    safe = re.sub(r'[^0-9A-Za-z.]+', '_', ip)
    return log_dir() / f'camera_{time.strftime("%Y%m%d_%H%M%S")}_{safe}.log'


def setup_app_logging() -> Path:
    """Send the app's own log (warnings, errors, worker failures) to a dated file as well."""
    path = log_dir() / f'app_{time.strftime("%Y%m%d")}.log'
    h = logging.FileHandler(path, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    h.setLevel(logging.INFO)
    root = logging.getLogger()
    if not any(isinstance(x, logging.FileHandler) and getattr(x, 'baseFilename', '') == str(path)
               for x in root.handlers):
        root.addHandler(h)
    root.setLevel(min(root.level or logging.INFO, logging.INFO))
    return path
