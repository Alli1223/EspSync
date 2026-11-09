import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import espsync.gui.main_window as mw  # noqa: E402
from espsync.config import AppConfig  # noqa: E402
from espsync.engine import EngineStats  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class StubEngine:
    """Stands in for SyncEngine: no screen capture, no network."""

    def __init__(self, config):
        self.config = config
        self.stats = EngineStats()
        self.running = False
        self.sending = False

    def start(self, send=True):
        self.running, self.sending = True, send

    def stop(self, restore=None):
        self.running = False

    def restart(self):
        pass

    def mark_dirty(self):
        pass

    def colors(self):
        return {}

    def preview(self):
        return None

    def shutdown(self):
        self.running = False


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("ESPSYNC_CONFIG", str(tmp_path / "config.json"))  # never touch the real config
    monkeypatch.setattr(mw, "SyncEngine", StubEngine)
    cfg = AppConfig()
    cfg.sync.brightness = 0.8
    win = mw.MainWindow(cfg, QIcon())
    yield win
    for t in (win._ui_timer, win._preview_timer, win._stats_timer, win._save_timer):
        t.stop()
    win.deleteLater()


def test_toolbar_brightness_sets_all_lights_and_settings(window):
    assert window.brightness.value() == 80 and window.brightness_label.text() == "80%"
    window.brightness.setValue(45)
    assert window.config.sync.brightness == pytest.approx(0.45)
    assert window.settings.brightness.value() == 45  # Settings slider follows
    assert window.brightness_label.text() == "45%"
    assert window._save_timer.isActive()


def test_settings_brightness_moves_toolbar_slider(window):
    window.settings.brightness.setValue(130)
    assert window.config.sync.brightness == pytest.approx(1.3)
    assert window.brightness.value() == 130 and window.brightness_label.text() == "130%"
