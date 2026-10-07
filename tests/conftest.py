"""Every test gets its own empty QSettings folder, so the user's Phantastic settings are never read
or written by the suite (the GUI stores them as INI, which ``QSettings.setPath`` redirects)."""
import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory):
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        yield None
        return
    d = tmp_path_factory.mktemp('qsettings')
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(d))
    yield d
