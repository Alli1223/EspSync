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
        self.stops = [*getattr(self, "stops", []), restore]

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


def test_scene_button_stops_sync_without_restoring_then_sets_the_lights(window, monkeypatch):
    from espsync.scenes import SCENES, SceneResult

    assert list(window.scene_buttons) == [s.id for s in SCENES]
    jobs = []
    monkeypatch.setattr(mw, "run_async", lambda factory, done=None, failed=None: jobs.append((factory, done, failed)))
    applied = []

    async def fake_apply(config, scene):
        applied.append((config, scene.id))
        return SceneResult(applied=3)

    monkeypatch.setattr(mw, "apply_scene", fake_apply)
    window.sync_btn.setChecked(True)
    assert window.engine.sending
    window.engine.stops.clear()
    window.scene_buttons["warm"].click()
    # sync is off, and the lights were not put back first (the scene replaces that)
    assert not window.sync_btn.isChecked() and not window.engine.sending
    assert window.engine.stops[0] is False  # (a later stop is the hidden window pausing its preview)
    assert window._stop_restore is None  # the next ordinary stop restores as usual
    factory, done, _failed = jobs[-1]
    import asyncio

    result = asyncio.run(factory())
    assert applied == [(window.config, "warm")]
    done(result)
    assert window.statusBar().currentMessage() == "Warm white: set 3 lights"


def test_scene_while_not_syncing_leaves_the_preview_running(window, monkeypatch):
    jobs = []
    monkeypatch.setattr(mw, "run_async", lambda factory, done=None, failed=None: jobs.append(factory))
    window.engine.stops = []
    window.apply_scene("off")
    assert window.engine.stops == [] and len(jobs) == 1
    window.apply_scene("no-such-scene")
    assert len(jobs) == 1


def test_overwatch_test_buttons_reach_the_engine(window):
    played = []
    window.engine.test_overwatch = played.append
    window.settings.ow_tests["damage"].click()
    window.settings.ow_tests["elimination"].click()
    assert played == ["damage", "elimination"]


def test_tabs_and_status_pill(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Light", "Picture", "Games", "Setup"]
    window._tick_stats()
    assert window.state_pill.text() == "PREVIEW"
    window.sync_btn.setChecked(True)
    assert window.state_pill.text() == "SYNCING" and window.sync_btn.text() == "■  Stop sync"
    window.engine.stats.error = "capture: lost"
    window._tick_stats()
    assert window.state_pill.text() == "CAPTURE ERROR"


def test_scene_menu_actions_pick_their_own_scene(window, monkeypatch):
    from PySide6.QtWidgets import QMenu

    chosen = []
    monkeypatch.setattr(window, "apply_scene", chosen.append)
    menu = QMenu()
    window.add_scene_actions(menu)
    for action in menu.actions():
        action.trigger()
    assert chosen == list(window.scene_buttons)
