"""Choosing a capture backend, and how the engine handles backends that fail or hang."""

import threading
import time

import pytest

from espsync.capture import available_backends, resolve_backend
from espsync.capture.base import CaptureError
from espsync.config import AppConfig


@pytest.mark.parametrize(
    ("name", "platform", "expected"),
    [
        ("auto", "win32", "dxgi"),
        ("auto", "linux", "mss"),
        ("auto", "darwin", "mss"),
        ("mss", "win32", "mss"),
        ("dxgi", "win32", "dxgi"),
        # 0.1.x could save the Wayland "portal" backend, which 0.2.0 removed.
        ("portal", "linux", "mss"),
        ("portal", "win32", "dxgi"),
    ],
)
def test_backend_selection(name, platform, expected):
    assert resolve_backend(name, platform) == expected


def test_available_backends_per_platform():
    assert available_backends("win32") == ["dxgi", "mss"]
    assert available_backends("linux") == ["mss"]


def test_old_wayland_settings_load_cleanly():
    cfg = AppConfig.from_dict({"sync": {"capture_backend": "portal", "portal_restore_token": "abc"}})
    assert not hasattr(cfg.sync, "portal_restore_token")
    assert resolve_backend(cfg.sync.capture_backend, "win32") == "dxgi"


# ------------------------------------------------------------------- engine
def _wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_engine_does_not_retry_refused_capture_until_settings_change(monkeypatch):
    import espsync.engine as engine_mod

    calls = []

    def refused(settings):
        calls.append(settings.monitor)
        raise CaptureError("capture was refused", retry=False)

    monkeypatch.setattr(engine_mod, "create_backend", refused)
    cfg = AppConfig()
    engine = engine_mod.SyncEngine(cfg)
    engine.start(send=False)
    try:
        assert _wait_for(lambda: calls)
        time.sleep(0.4)  # must not keep retrying
        assert len(calls) == 1
        assert "refused" in engine.stats.error
        cfg.sync.monitor = 1  # changed capture settings -> try again straight away
        assert _wait_for(lambda: len(calls) == 2)
    finally:
        engine.shutdown()


def test_engine_stale_capture_thread_exits_after_restart(monkeypatch):
    import espsync.engine as engine_mod

    gate = threading.Event()
    calls = []

    def slow(settings):
        calls.append(threading.current_thread())
        if len(calls) == 1:
            gate.wait(5)  # stuck opening the backend past stop()'s 3 s join
        raise CaptureError("x", retry=False)

    monkeypatch.setattr(engine_mod, "create_backend", slow)
    engine = engine_mod.SyncEngine(AppConfig())
    engine.start(send=False)
    try:
        assert _wait_for(lambda: calls)
        first = engine._capture_thread
        engine._running.clear()  # simulate stop() giving up on the join
        engine._capture_thread = None
        engine.start(send=False)
        assert _wait_for(lambda: len(calls) == 2)
        assert calls[1] is not first
        gate.set()
        first.join(2)
        assert not first.is_alive()  # it saw it was stale and didn't keep capturing
        assert engine._capture_thread.is_alive()
    finally:
        engine.shutdown()


def test_settings_panel_lists_only_these_backends(app):
    from espsync.gui.settings_panel import SettingsPanel

    panel = SettingsPanel(AppConfig())
    data = [panel.backend.itemData(i) for i in range(panel.backend.count())]
    assert data == ["auto", *available_backends()]
    assert panel.monitor.isEnabled()
    panel.deleteLater()
