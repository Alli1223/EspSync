"""Wayland portal backend: everything that can be checked without a compositor.

The D-Bus conversation runs against a fake portal that speaks real jeepney
messages (serialised and parsed back, so variants look exactly as they do on
the wire); GStreamer is replaced by a small fake as well.
"""

import os
import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from espsync.capture import available_backends, create_backend, list_monitors, resolve_backend
from espsync.capture import portal_backend as pb
from espsync.capture.base import SRGB_TO_LINEAR, CaptureError
from espsync.config import AppConfig, SyncSettings

needs_jeepney = pytest.mark.skipif(pb.jeepney is None, reason="jeepney not installed")

WAYLAND = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}


# ---------------------------------------------------------- backend selection
@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {}, "dxgi"),
        ("linux", WAYLAND, "portal"),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, "portal"),  # no XDG_SESSION_TYPE (ssh/tty)
        ("linux", {"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, "mss"),
        ("linux", {"XDG_SESSION_TYPE": "tty"}, "mss"),
        ("linux", {}, "mss"),
        ("darwin", WAYLAND, "mss"),
    ],
)
def test_auto_backend_selection(platform, env, expected):
    assert resolve_backend("auto", platform, env) == expected


def test_explicit_backend_is_kept():
    assert resolve_backend("mss", "linux", WAYLAND) == "mss"
    assert resolve_backend("portal", "linux", {}) == "portal"


def test_available_backends_per_platform():
    assert available_backends("win32") == ["dxgi", "mss"]
    assert available_backends("linux") == ["portal", "mss"]
    assert available_backends("darwin") == ["mss"]


def test_portal_monitor_list_is_the_dialog_choice():
    (mon,) = list_monitors("portal")
    assert mon.index == 0
    assert "0x0" not in mon.label() and "dialog" in mon.label()


def test_config_round_trips_restore_token():
    cfg = AppConfig(sync=SyncSettings(capture_backend="portal", portal_restore_token="abc"))
    back = AppConfig.from_dict(cfg.to_dict())
    assert back.sync.capture_backend == "portal"
    assert back.sync.portal_restore_token == "abc"


# ----------------------------------------------------------- frame conversion
def test_frame_from_bgrx_ignores_row_padding_and_swaps_channels():
    w, h, stride = 3, 2, 16  # 12 bytes of pixels + 4 bytes of padding per row
    raw = np.full((h, stride), 77, np.uint8)
    px = raw[:, : w * 4].reshape(h, w, 4)
    px[...] = (10, 128, 255, 0)  # B, G, R, x
    px[0, 0] = (0, 0, 0, 255)
    frame = pb.frame_from_bgrx(raw.tobytes(), w, h, (1920, 1280))
    assert frame.image.shape == (h, w, 3) and frame.image.dtype == np.float32
    np.testing.assert_array_equal(frame.image[1, 2], SRGB_TO_LINEAR[[255, 128, 10]])
    np.testing.assert_array_equal(frame.image[0, 0], [0, 0, 0])
    assert frame.source_size == (1920, 1280) and frame.aspect == pytest.approx(1.5)
    assert not frame.hdr


def test_frame_from_bgrx_defaults_source_size_and_rejects_short_buffers():
    data = bytes(160 * 90 * 4)
    assert pb.frame_from_bgrx(data, 160, 90).source_size == (160, 90)
    with pytest.raises(CaptureError):
        pb.frame_from_bgrx(data[:-1], 160, 90)
    with pytest.raises(CaptureError):
        pb.frame_from_bgrx(b"", 0, 0)


# ------------------------------------------------------------- pure helpers
def test_request_path_follows_portal_convention():
    assert pb.request_path(":1.42", "tok") == "/org/freedesktop/portal/desktop/request/1_42/tok"


def test_select_sources_options_by_portal_version():
    v5 = pb.select_sources_options(5, cursor_modes=7, restore_token="old")
    assert v5 == {
        "types": ("u", 1),
        "multiple": ("b", False),
        "cursor_mode": ("u", 1),
        "persist_mode": ("u", 2),
        "restore_token": ("s", "old"),
    }
    first_run = pb.select_sources_options(5, cursor_modes=2, restore_token="")
    assert "restore_token" not in first_run and "cursor_mode" not in first_run  # hidden cursor unsupported
    assert first_run["persist_mode"] == ("u", 2)
    v3 = pb.select_sources_options(3, cursor_modes=7, restore_token="old")
    assert "persist_mode" not in v3 and "restore_token" not in v3


def test_pipeline_keeps_only_the_newest_small_frame():
    desc = pb.pipeline_description(fd=7, node_id=42, width=160)
    assert desc.startswith("pipewiresrc name=src fd=7 path=42 ")
    assert "format=BGRx,width=160,pixel-aspect-ratio=1/1" in desc
    # downscale before colour conversion (cheap), newest frame only, no clock sync
    assert desc.index("videoscale") < desc.index("videoconvert")
    assert "drop=true max-buffers=1 sync=false" in desc


# ------------------------------------------------------------ fake D-Bus portal
def _wire(msg):
    """Serialise and parse back: tests then see exactly what D-Bus delivers."""
    from jeepney.low_level import Parser

    parser = Parser()
    parser.add_data(msg.serialise(serial=1))
    return parser.get_next_message()


class FakePortalBus:
    """Stands in for a jeepney blocking DBusConnection talking to xdg-desktop-portal."""

    unique_name = ":1.42"

    def __init__(self, *, version=5, cursor_modes=7, start_code=0, answer_start=True, new_token="tok-2",
                 service=True):
        from jeepney.io.common import MessageFilters

        self.version = version
        self.cursor_modes = cursor_modes
        self.start_code = start_code
        self.answer_start = answer_start
        self.new_token = new_token
        self.service = service
        self.filters = MessageFilters()
        self.inbox = deque()
        self.calls = []  # (path, member, body)
        self.closed = False

    # -- the subset of DBusConnection the backend uses -------------------------
    def filter(self, rule, *, queue=None, bufsize=1):
        from jeepney.io.common import FilterHandle

        return FilterHandle(self.filters, rule, queue if queue is not None else deque(maxlen=bufsize))

    def recv_until_filtered(self, queue, *, timeout=None):
        while self.inbox:
            msg = self.inbox.popleft()
            for handle in self.filters.matches(msg):
                handle.queue.append(msg)
        if queue:
            return queue.popleft()
        raise TimeoutError

    def close(self):
        self.closed = True

    def send_and_get_reply(self, msg, *, timeout=None):
        from jeepney import HeaderFields, new_error, new_method_return

        wire = _wire(msg)  # also proves the signature matches the body
        fields = wire.header.fields
        iface, member, path = fields.get(HeaderFields.interface), fields[HeaderFields.member], fields[HeaderFields.path]
        self.calls.append((path, member, wire.body))
        if iface == "org.freedesktop.DBus":  # AddMatch
            return new_method_return(wire)
        if not self.service:
            return new_error(wire, "org.freedesktop.DBus.Error.ServiceUnknown", "s", ("no portal",))
        if iface == "org.freedesktop.DBus.Properties":
            value = {"version": self.version, "AvailableCursorModes": self.cursor_modes}[wire.body[1]]
            return _wire(new_method_return(wire, "v", (("u", value),)))
        if member == "Close":
            return new_method_return(wire)
        if member == "OpenPipeWireRemote":
            return new_method_return(wire, "h", (99,))  # a real reply carries a FileDescriptor
        handle = pb.request_path(self.unique_name, wire.body[-1]["handle_token"][1])
        code, results = 0, {}
        if member == "CreateSession":
            token = wire.body[0]["session_handle_token"][1]
            results = {"session_handle": ("s", f"/org/freedesktop/portal/desktop/session/1_42/{token}")}
        elif member == "Start":
            if not self.answer_start:  # the dialog stays open
                return _wire(new_method_return(wire, "o", (handle,)))
            code = self.start_code
            results = {"streams": ("a(ua{sv})", [(57, {"size": ("(ii)", (3840, 2160)), "source_type": ("u", 1)})])}
            if self.new_token:
                results["restore_token"] = ("s", self.new_token)
        self.emit(handle, pb.REQUEST_IFACE, "Response", "ua{sv}", (code, results))
        return _wire(new_method_return(wire, "o", (handle,)))

    def emit(self, path, iface, member, signature, body):
        from jeepney import DBusAddress, new_signal

        self.inbox.append(_wire(new_signal(DBusAddress(path, interface=iface), member, signature, body)))

    def members(self):
        return [m for _, m, _ in self.calls if m not in ("Get", "AddMatch")]

    def body_of(self, member):
        return next(b for _, m, b in self.calls if m == member)


@needs_jeepney
def test_portal_handshake_restores_screen_and_returns_new_token():
    bus = FakePortalBus()
    portal = pb.ScreenCastPortal(connection=bus)
    stream = portal.start("tok-1")
    assert stream == pb.PortalStream(fd=99, node_id=57, size=(3840, 2160), restore_token="tok-2")
    assert bus.members() == ["CreateSession", "SelectSources", "Start", "OpenPipeWireRemote"]
    session, opts = bus.body_of("SelectSources")
    assert session == portal.session
    assert opts["types"] == ("u", 1) and opts["persist_mode"] == ("u", 2)
    assert opts["restore_token"] == ("s", "tok-1") and opts["cursor_mode"] == ("u", 1)
    assert bus.body_of("Start")[:2] == (portal.session, "")

    assert not portal.session_closed()
    bus.emit(portal.session, pb.SESSION_IFACE, "Closed", "a{sv}", ({},))
    assert portal.session_closed()

    portal.close()
    assert (session, "Close", ()) in bus.calls and bus.closed


@needs_jeepney
def test_portal_without_persistence_gives_no_token():
    bus = FakePortalBus(version=3, new_token="")
    stream = pb.ScreenCastPortal(connection=bus).start("tok-1")
    assert stream.restore_token == ""
    _, opts = bus.body_of("SelectSources")
    assert "persist_mode" not in opts and "restore_token" not in opts


@needs_jeepney
def test_cancelled_dialog_is_not_retried():
    portal = pb.ScreenCastPortal(connection=FakePortalBus(start_code=1))
    with pytest.raises(CaptureError, match="cancelled") as err:
        portal.start()
    assert err.value.retry is False


@needs_jeepney
def test_abort_while_dialog_open_closes_the_request():
    polls = []

    def abort():
        polls.append(1)
        return len(polls) > 3  # the user leaves the dialog open; sync is stopped

    bus = FakePortalBus(answer_start=False)
    portal = pb.ScreenCastPortal(connection=bus, abort=abort)
    with pytest.raises(CaptureError) as err:
        portal.start()
    assert err.value.retry is False
    path, member, _ = bus.calls[-1]
    assert member == "Close" and "/request/" in path


@needs_jeepney
def test_missing_portal_service_has_a_clear_error():
    with pytest.raises(CaptureError, match="portal") as err:
        pb.ScreenCastPortal(connection=FakePortalBus(service=False)).start()
    assert err.value.retry is False


# ------------------------------------------------------- backend with fakes
class FakeSink:
    def __init__(self):
        self.samples = deque()

    def emit(self, signal, timeout_ns):
        assert signal == "try-pull-sample" and timeout_ns > 0
        return self.samples.popleft() if self.samples else None


class FakeBus:
    def __init__(self):
        self.messages = deque()

    def pop_filtered(self, types):
        return self.messages.popleft() if self.messages else None


def _fake_gst(sink, bus, states):
    caps = SimpleNamespace(get_size=lambda: 1, get_structure=lambda i: SimpleNamespace(
        get_int=lambda k: (True, {"width": 3840, "height": 2160}[k])))
    src = SimpleNamespace(get_static_pad=lambda name: SimpleNamespace(get_current_caps=lambda: caps))

    class Pipeline:
        def get_by_name(self, name):
            return {"sink": sink, "src": src}[name]

        def get_bus(self):
            return bus

        def set_state(self, state):
            states.append(state)
            return "ok"

    parsed = []
    gst = SimpleNamespace(
        MSECOND=1_000_000,
        State=SimpleNamespace(PLAYING="playing", NULL="null"),
        StateChangeReturn=SimpleNamespace(FAILURE="failure"),
        MessageType=SimpleNamespace(ERROR=1, EOS=2),
        parse_launch=lambda desc: parsed.append(desc) or Pipeline(),
    )
    return gst, parsed


def _sample(w, h, bgrx):
    data = np.tile(np.array(bgrx, np.uint8), (h, w, 1)).tobytes()
    caps = SimpleNamespace(get_structure=lambda i: SimpleNamespace(get_value={"width": w, "height": h}.get))
    buf = SimpleNamespace(get_size=lambda: len(data), extract_dup=lambda off, n: data[off:off + n])
    return SimpleNamespace(get_caps=lambda: caps, get_buffer=lambda: buf)


class FakePortal:
    def __init__(self, abort=None):
        self.version = 5
        self.closed_signal = False
        self.closed = False
        self.started_with = None

    def start(self, restore_token=""):
        self.started_with = restore_token
        self.fd = os.open(os.devnull, os.O_RDONLY)
        return pb.PortalStream(fd=self.fd, node_id=57, size=None, restore_token="tok-new")

    def session_closed(self):
        return self.closed_signal

    def close(self):
        self.closed = True


@pytest.fixture
def fake_backend(monkeypatch):
    sink, bus, states = FakeSink(), FakeBus(), []
    gst, parsed = _fake_gst(sink, bus, states)
    portals = []
    monkeypatch.setattr(pb, "load_gstreamer", lambda: gst)
    monkeypatch.setattr(pb, "ScreenCastPortal", lambda abort=None: portals.append(FakePortal(abort)) or portals[-1])
    tokens = []
    backend = pb.PortalBackend(restore_token="tok-old", on_restore_token=tokens.append)
    return SimpleNamespace(backend=backend, sink=sink, bus=bus, states=states, parsed=parsed,
                           portal=portals[0], tokens=tokens)


def test_backend_saves_new_token_and_builds_pipeline(fake_backend):
    fb = fake_backend
    assert fb.portal.started_with == "tok-old"
    assert fb.tokens == ["tok-new"]
    assert f"fd={fb.portal.fd} path=57" in fb.parsed[0]
    assert fb.states == ["playing"]


def test_backend_grab_returns_newest_frame_or_none(fake_backend):
    fb = fake_backend
    assert fb.backend.grab(10) is None  # static screen: nothing new
    fb.sink.samples.append(_sample(160, 90, (0, 0, 255, 0)))
    frame = fb.backend.grab(10)
    assert frame.image.shape == (90, 160, 3)
    np.testing.assert_array_equal(frame.image[0, 0], [1.0, 0.0, 0.0])
    assert frame.source_size == (3840, 2160)  # read from the PipeWire caps


def test_backend_reports_pipeline_errors_and_stopped_sharing(fake_backend):
    fb = fake_backend
    err = SimpleNamespace(type=1, parse_error=lambda: (SimpleNamespace(message="node gone"), "dbg"))
    fb.bus.messages.append(err)
    with pytest.raises(CaptureError, match="node gone") as exc:
        fb.backend.grab(10)
    assert exc.value.retry is True

    fb.bus.messages.append(SimpleNamespace(type=2))
    with pytest.raises(CaptureError, match="ended") as exc:
        fb.backend.grab(10)
    assert exc.value.retry is False

    fb.portal.closed_signal = True
    fb.backend._last_poll = 0.0
    with pytest.raises(CaptureError, match="stopped") as exc:
        fb.backend.grab(10)
    assert exc.value.retry is False


def test_backend_close_releases_everything(fake_backend):
    fb = fake_backend
    fd = fb.portal.fd
    fb.backend.close()
    assert fb.states[-1] == "null" and fb.portal.closed
    with pytest.raises(OSError):
        os.fstat(fd)  # our copy of the PipeWire fd is closed
    fb.backend.close()  # idempotent


def test_create_backend_stores_token_in_settings(monkeypatch):
    seen = {}

    class Fake:
        name = "portal"

        def __init__(self, restore_token, on_restore_token, abort):
            seen.update(token=restore_token, abort=abort)
            on_restore_token("tok-new")

    monkeypatch.setattr(pb, "PortalBackend", Fake)
    s = SyncSettings(capture_backend="portal", portal_restore_token="tok-old")
    abort = lambda: False  # noqa: E731
    assert create_backend(s, abort=abort).name == "portal"
    assert seen == {"token": "tok-old", "abort": abort}
    assert s.portal_restore_token == "tok-new"


def test_cli_bench_saves_the_new_token(monkeypatch, tmp_path, capsys):
    import espsync.capture as capture
    from espsync.cli import main
    from espsync.config import load_config, save_config

    path = tmp_path / "config.json"
    save_config(AppConfig(sync=SyncSettings(portal_restore_token="tok-old", target_fps=33)), path)

    class Fake:
        name = "portal"

        def grab(self, timeout_ms=100):
            time.sleep(0.01)

        def close(self):
            pass

    def fake_create(settings, abort=None):
        assert settings.portal_restore_token == "tok-old"
        settings.portal_restore_token = "tok-new"
        return Fake()

    monkeypatch.setattr(capture, "create_backend", fake_create)
    assert main(["--config", str(path), "bench", "--backend", "portal", "--seconds", "0.05"]) == 0
    cfg = load_config(path)
    assert cfg.sync.portal_restore_token == "tok-new"
    assert cfg.sync.target_fps == 33 and cfg.sync.capture_backend == "auto"  # nothing else changed


# ------------------------------------------------------------------- engine
def _wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_engine_does_not_retry_declined_capture_until_settings_change(monkeypatch):
    import espsync.engine as engine_mod

    calls = []

    def declined(settings, abort=None):
        calls.append(settings.monitor)
        raise CaptureError("screen sharing was cancelled", retry=False)

    monkeypatch.setattr(engine_mod, "create_backend", declined)
    cfg = AppConfig()
    engine = engine_mod.SyncEngine(cfg)
    engine.start(send=False)
    try:
        assert _wait_for(lambda: calls)
        time.sleep(0.4)  # must not keep re-opening the share dialog
        assert len(calls) == 1
        assert "cancelled" in engine.stats.error
        cfg.sync.monitor = 1  # changed capture settings -> try again straight away
        assert _wait_for(lambda: len(calls) == 2)
    finally:
        engine.shutdown()


def test_engine_stop_aborts_a_backend_waiting_on_the_user(monkeypatch):
    import espsync.engine as engine_mod

    waiting = threading.Event()

    def dialog_open(settings, abort=None):
        waiting.set()
        while not abort():
            time.sleep(0.01)
        raise CaptureError("abandoned", retry=False)

    monkeypatch.setattr(engine_mod, "create_backend", dialog_open)
    engine = engine_mod.SyncEngine(AppConfig())
    engine.start(send=False)
    try:
        assert waiting.wait(2)
        thread = engine._capture_thread
        t0 = time.monotonic()
        engine.stop()
        assert not thread.is_alive() and time.monotonic() - t0 < 1.0
    finally:
        engine.shutdown()


def test_engine_stale_capture_thread_exits_after_restart(monkeypatch):
    import espsync.engine as engine_mod

    gate = threading.Event()
    aborts = []

    def slow(settings, abort=None):
        aborts.append(abort)
        if len(aborts) == 1:
            gate.wait(5)  # stuck in the share dialog past stop()'s 3 s join
        raise CaptureError("x", retry=False)

    monkeypatch.setattr(engine_mod, "create_backend", slow)
    engine = engine_mod.SyncEngine(AppConfig())
    engine.start(send=False)
    try:
        assert _wait_for(lambda: aborts)
        first = engine._capture_thread
        engine._running.clear()  # simulate stop() giving up on the join
        engine._capture_thread = None
        engine.start(send=False)
        assert _wait_for(lambda: len(aborts) == 2)
        assert aborts[0]() is True and aborts[1]() is False  # old thread knows it is stale
        gate.set()
        first.join(2)
        assert not first.is_alive()
    finally:
        engine.shutdown()


# ---------------------------------------------------------------------- GUI
def test_settings_panel_repick_button_only_for_portal():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from espsync.gui.settings_panel import SettingsPanel

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    cfg = AppConfig(sync=SyncSettings(capture_backend="portal", portal_restore_token="tok"))
    panel = SettingsPanel(cfg)
    assert panel._capture_form.isRowVisible(panel.repick)
    assert not panel.monitor.isEnabled()
    emitted = []
    panel.changed.connect(emitted.append)
    panel.repick.click()
    assert cfg.sync.portal_restore_token == "" and emitted == ["capture"]

    cfg2 = AppConfig(sync=SyncSettings(capture_backend="mss"))
    panel2 = SettingsPanel(cfg2)
    assert not panel2._capture_form.isRowVisible(panel2.repick)
    assert panel2.monitor.isEnabled()
