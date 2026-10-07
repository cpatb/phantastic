"""Persistent GUI preferences (QSettings, organisation and application "Phantastic", INI format).

INI rather than the platform default (the registry on Windows) so the file is readable and so
``QSettings.setPath`` can redirect it: the tests point it at a temporary folder and never touch
the user's own settings. Only conveniences live here (folders, geometry, last IP, Image Tools
toggles); nothing that changes recorded data.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings

ORGANISATION = APPLICATION = 'Phantastic'


def settings() -> QSettings:
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, ORGANISATION, APPLICATION)


def get(key: str, default=None, type_=None):
    s = settings()
    if not s.contains(key):
        return default
    try:
        return s.value(key, default, type=type_) if type_ is not None else s.value(key, default)
    except (TypeError, ValueError):
        return default


def put(key: str, value):
    s = settings()
    s.setValue(key, value)
    s.sync()
