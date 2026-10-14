"""Test setup: an isolated home folder and an off-screen Qt app (works on Windows, macOS, Linux)."""
import os
import sys
import tempfile

_HOME = tempfile.mkdtemp(prefix="wr-test-home-")
for var in ("HOME", "USERPROFILE"):
    os.environ[var] = _HOME
for var, sub in (("APPDATA", "AppData/Roaming"), ("LOCALAPPDATA", "AppData/Local"),
                 ("XDG_DATA_HOME", ".local/share"), ("XDG_CONFIG_HOME", ".config")):
    os.environ[var] = os.path.join(_HOME, *sub.split("/"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ["WATER_REMINDER_UPDATE_URL"] = "http://127.0.0.1:9/never"  # tests never hit GitHub

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session")
def wr(app):
    import water_reminder
    return water_reminder


@pytest.fixture(autouse=True)
def clean_data(wr):
    """Every test starts with no history and default settings."""
    import shutil
    for d in (wr.DATA_DIR, wr.CONFIG_DIR):
        shutil.rmtree(d, ignore_errors=True)
    yield
