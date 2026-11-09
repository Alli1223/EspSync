"""Wayland screen capture via the xdg-desktop-portal ScreenCast API + PipeWire.

Wayland compositors don't let applications read the screen.  Instead we ask
the compositor through ``org.freedesktop.portal.ScreenCast``: it shows a
permission dialog where the user picks a monitor, then hands us a PipeWire
remote (a file descriptor) and the node id of the screen's video stream.

* D-Bus: ``jeepney`` (pure Python, blocking I/O, can receive file descriptors).
* Video: GStreamer's ``pipewiresrc`` via PyGObject.  The pipeline downscales
  before converting colours, and the appsink keeps only the newest frame
  (``drop=true max-buffers=1``), so a slow consumer drops frames instead of
  adding latency.  The compositor only sends a frame when the screen changes,
  which maps directly onto ``grab()`` returning None for "no new frame".

With ``persist_mode=2`` the portal returns a *restore token*; passing it the
next time re-opens the same monitor without asking again.  Tokens are single
use - every successful Start returns a fresh one - so ``on_restore_token`` is
called each time and the caller must save it.

Install: ``pip install espsync[wayland]`` plus GStreamer's PipeWire plugin
(see ``INSTALL_HINT``).
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .base import TARGET_WIDTH, CaptureBackend, CaptureError, Frame, MonitorInfo, bgra_to_linear

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

SOURCE_MONITOR = 1
CURSOR_HIDDEN = 1
PERSIST_UNTIL_REVOKED = 2

# Response codes of org.freedesktop.portal.Request.Response
RESPONSE_OK = 0
RESPONSE_CANCELLED = 1

# Width of the first, nearest-neighbour downscale.  Scaling 4K straight to
# TARGET_WIDTH with a filter would read every source pixel on the CPU; picking
# pixels down to this width first and then filtering keeps the cost tiny while
# still averaging a few samples per output pixel.
PRESCALE_WIDTH = TARGET_WIDTH * 4

INSTALL_HINT = (
    "Wayland capture needs `pip install espsync[wayland]` (jeepney + PyGObject) and GStreamer's PipeWire "
    "plugin: Debian/Ubuntu `sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-pipewire`, "
    "Fedora `sudo dnf install python3-gobject gstreamer1 pipewire-gstreamer`, "
    "Arch `sudo pacman -S python-gobject gstreamer gst-plugin-pipewire`"
)

try:  # optional dependency (espsync[wayland])
    import jeepney
    from jeepney import DBusAddress, MatchRule, Properties, message_bus, new_method_call
    from jeepney.wrappers import DBusErrorResponse, unwrap_msg
except ImportError:  # pragma: no cover - exercised on systems without the extra
    jeepney = None


# --------------------------------------------------------------------- helpers
def new_token() -> str:
    return "espsync_" + secrets.token_hex(6)


def request_path(unique_name: str, token: str) -> str:
    """Object path the portal uses for a Request made with `handle_token`."""
    sender = unique_name.lstrip(":").replace(".", "_")
    return f"{PORTAL_PATH}/request/{sender}/{token}"


def unvariant(value: Any) -> Any:
    """jeepney decodes D-Bus variants as (signature, value) tuples."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


def unvariant_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {k: unvariant(v) for k, v in d.items()}


def select_sources_options(version: int, cursor_modes: int, restore_token: str) -> dict[str, tuple[str, Any]]:
    """Options for ScreenCast.SelectSources: one monitor, cursor hidden, remembered."""
    opts: dict[str, tuple[str, Any]] = {
        "types": ("u", SOURCE_MONITOR),
        "multiple": ("b", False),
    }
    if cursor_modes & CURSOR_HIDDEN:
        # The pointer would otherwise tint whatever light samples the area under it.
        opts["cursor_mode"] = ("u", CURSOR_HIDDEN)
    if version >= 4:  # persist_mode / restore_token were added in version 4
        opts["persist_mode"] = ("u", PERSIST_UNTIL_REVOKED)
        if restore_token:
            opts["restore_token"] = ("s", restore_token)
    return opts


def pipeline_description(fd: int, node_id: int, width: int = TARGET_WIDTH) -> str:
    """GStreamer pipeline: PipeWire node -> small BGRx frames in an appsink."""
    pre = max(width, PRESCALE_WIDTH)
    return (
        f"pipewiresrc name=src fd={fd} path={node_id} ! "
        "videoscale method=nearest-neighbour ! "
        f"video/x-raw,width={pre},pixel-aspect-ratio=1/1 ! "
        "videoscale method=bilinear ! videoconvert ! "
        f"video/x-raw,format=BGRx,width={width},pixel-aspect-ratio=1/1 ! "
        "appsink name=sink drop=true max-buffers=1 sync=false emit-signals=false"
    )


def frame_from_bgrx(data: bytes, width: int, height: int, source_size: tuple[int, int] | None = None) -> Frame:
    """Packed BGRx/BGRA bytes (rows may be padded) -> linear-light Frame."""
    if width <= 0 or height <= 0:
        raise CaptureError(f"bad frame size {width}x{height}")
    stride = len(data) // height
    if stride < width * 4:
        raise CaptureError(f"short frame: {len(data)} bytes for {width}x{height} BGRx")
    rows = np.frombuffer(data, dtype=np.uint8, count=stride * height).reshape(height, stride)
    bgrx = rows[:, : width * 4].reshape(height, width, 4)
    return Frame(image=bgra_to_linear(bgrx), source_size=source_size or (width, height))


def list_monitors() -> list[MonitorInfo]:
    """The portal can't enumerate outputs: the user picks one in its dialog."""
    return [MonitorInfo(index=0, name="Screen chosen in the system share dialog", x=0, y=0, width=0, height=0,
                        primary=True, hdr=False)]


# ---------------------------------------------------------------------- portal
@dataclass
class PortalStream:
    fd: int  # PipeWire remote, owned by the caller
    node_id: int
    size: tuple[int, int] | None  # stream size in compositor coordinates, if reported
    restore_token: str  # "" when the portal didn't issue one


def open_session_bus():  # noqa: ANN201 - jeepney DBusConnection
    if jeepney is None:
        raise CaptureError(INSTALL_HINT, retry=False)
    from jeepney.io.blocking import open_dbus_connection

    try:
        return open_dbus_connection(bus="SESSION", enable_fds=True)
    except Exception as exc:  # noqa: BLE001 - KeyError (no address), OSError, ...
        raise CaptureError(f"cannot connect to the D-Bus session bus: {exc}") from exc


class ScreenCastPortal:
    """Minimal blocking client for ``org.freedesktop.portal.ScreenCast``.

    The portal session lives as long as the D-Bus connection, so keep this
    object open while capturing and `close()` it afterwards.
    """

    def __init__(self, connection: Any = None, abort: Callable[[], bool] | None = None) -> None:
        if jeepney is None:
            raise CaptureError(INSTALL_HINT, retry=False)
        self.conn = connection if connection is not None else open_session_bus()
        self.abort = abort or (lambda: False)
        self.addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface=SCREENCAST_IFACE)
        self.session: str | None = None
        self.version = 0
        self._closed_filter = None
        self._closed = False

    # -- low level -------------------------------------------------------------
    def _call(self, method: str, signature: str, *args: Any, timeout: float = 10.0) -> tuple:
        msg = new_method_call(self.addr, method, signature, args)
        try:
            return unwrap_msg(self.conn.send_and_get_reply(msg, timeout=timeout))
        except DBusErrorResponse as exc:
            name = exc.name or ""
            if any(e in name for e in ("ServiceUnknown", "UnknownInterface", "UnknownMethod", "UnknownObject")):
                raise CaptureError(
                    "this desktop has no screen-cast portal (install xdg-desktop-portal and the backend for your "
                    "desktop, e.g. xdg-desktop-portal-gnome or xdg-desktop-portal-kde)",
                    retry=False,
                ) from exc
            raise CaptureError(f"screen-cast portal {method} failed: {exc.name}: {exc.data}") from exc

    def _property(self, name: str, default: int) -> int:
        try:
            reply = self.conn.send_and_get_reply(Properties(self.addr).get(name), timeout=5.0)
            return int(unvariant(unwrap_msg(reply)[0]))
        except DBusErrorResponse as exc:
            if "ServiceUnknown" in (exc.name or ""):
                raise CaptureError("this desktop has no xdg-desktop-portal service running", retry=False) from exc
            return default

    def _add_match(self, rule: Any) -> None:
        # Signals are only routed to us if the bus knows we want them.
        self.conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=5.0)

    def _request(self, method: str, signature: str, *args: Any, options: dict, timeout: float | None) -> dict:
        """Call a portal method that answers with a Request.Response signal."""
        token = new_token()
        path = request_path(self.conn.unique_name, token)
        rule = MatchRule(type="signal", interface=REQUEST_IFACE, member="Response", path=path)
        with self.conn.filter(rule, bufsize=4) as queue:
            self._add_match(rule)
            (handle,) = self._call(method, signature + "a{sv}", *args, {**options, "handle_token": ("s", token)})
            if handle != path:  # pre-0.9 portals ignore handle_token
                raise CaptureError(f"unexpected portal request path {handle}", retry=False)
            reply = self._wait(queue, method, handle, timeout)
        code, results = reply.body
        if code == RESPONSE_CANCELLED:
            raise CaptureError("screen sharing was cancelled - start sync again to choose a screen", retry=False)
        if code != RESPONSE_OK:
            raise CaptureError(f"screen sharing was refused by the desktop ({method}, code {code})", retry=False)
        return unvariant_dict(results)

    def _wait(self, queue: Any, method: str, handle: str, timeout: float | None) -> Any:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                return self.conn.recv_until_filtered(queue, timeout=0.25)
            except TimeoutError:
                pass
            if self.abort():
                self._close_object(handle, REQUEST_IFACE)
                raise CaptureError("screen sharing request abandoned", retry=False)
            if deadline is not None and time.monotonic() > deadline:
                self._close_object(handle, REQUEST_IFACE)
                raise CaptureError(f"the screen-cast portal did not answer {method}")

    def _close_object(self, path: str, interface: str) -> None:
        try:
            msg = new_method_call(DBusAddress(path, bus_name=PORTAL_BUS, interface=interface), "Close")
            self.conn.send_and_get_reply(msg, timeout=2.0)
        except Exception:  # noqa: BLE001 - best effort
            log.debug("closing %s failed", path, exc_info=True)

    # -- API ---------------------------------------------------------------------
    def start(self, restore_token: str = "") -> PortalStream:
        """CreateSession -> SelectSources -> Start -> OpenPipeWireRemote.

        Blocks while the permission dialog is open (no timeout; `abort` is
        polled every 250 ms).
        """
        self.version = self._property("version", 1)
        cursor_modes = self._property("AvailableCursorModes", 0) if self.version >= 2 else 0
        res = self._request("CreateSession", "", options={"session_handle_token": ("s", new_token())}, timeout=30)
        self.session = str(res["session_handle"])
        self._watch_session()
        self._request("SelectSources", "o", self.session,
                      options=select_sources_options(self.version, cursor_modes, restore_token), timeout=None)
        res = self._request("Start", "os", self.session, "", options={}, timeout=None)
        streams = res.get("streams") or []
        if not streams:
            raise CaptureError("the screen-cast portal returned no screen")
        node_id, props = streams[0]
        props = unvariant_dict(props)
        size = props.get("size")
        (fd,) = self._call("OpenPipeWireRemote", "oa{sv}", self.session, {})
        raw_fd = fd.to_raw_fd() if hasattr(fd, "to_raw_fd") else int(fd)
        return PortalStream(
            fd=raw_fd,
            node_id=int(node_id),
            size=(int(size[0]), int(size[1])) if size else None,
            restore_token=str(res.get("restore_token", "")),
        )

    def _watch_session(self) -> None:
        rule = MatchRule(type="signal", interface=SESSION_IFACE, member="Closed", path=self.session)
        self._closed_filter = self.conn.filter(rule, bufsize=1)
        self._add_match(rule)

    def session_closed(self) -> bool:
        """True once the compositor has ended the session (user pressed stop)."""
        if self._closed or self._closed_filter is None:
            return self._closed
        try:
            # Returns (and pops) a queued Closed signal, or reads what's pending.
            self.conn.recv_until_filtered(self._closed_filter.queue, timeout=0.001)
            self._closed = True
        except TimeoutError:
            pass
        return self._closed

    def close(self) -> None:
        if self._closed_filter is not None:
            self._closed_filter.close()
            self._closed_filter = None
        if self.session is not None:
            self._close_object(self.session, SESSION_IFACE)
            self.session = None
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------- backend
def load_gstreamer() -> Any:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise CaptureError(f"GStreamer for Python is missing. {INSTALL_HINT}", retry=False) from exc
    if not Gst.is_initialized():
        Gst.init(None)
    if Gst.ElementFactory.find("pipewiresrc") is None:
        raise CaptureError(f"GStreamer's PipeWire plugin (pipewiresrc) is missing. {INSTALL_HINT}", retry=False)
    return Gst


class PortalBackend(CaptureBackend):
    """Capture through xdg-desktop-portal + PipeWire (GNOME, KDE, wlroots...)."""

    name = "portal"

    def __init__(
        self,
        restore_token: str = "",
        on_restore_token: Callable[[str], None] | None = None,
        abort: Callable[[], bool] | None = None,
        width: int = TARGET_WIDTH,
    ) -> None:
        super().__init__(monitor=0)
        self._Gst = load_gstreamer()  # fail before showing a dialog if we couldn't use the result
        self._pipeline = None
        self._fd = -1
        self._source_size: tuple[int, int] | None = None
        self._portal: ScreenCastPortal | None = ScreenCastPortal(abort=abort)
        self._last_poll = time.monotonic()
        try:
            stream = self._portal.start(restore_token)
            if on_restore_token is not None:
                on_restore_token(stream.restore_token)  # the old token is spent either way
            self._fd = stream.fd
            self._source_size = stream.size
            log.info("portal screen cast: node %s, size %s, portal v%s", stream.node_id, stream.size,
                     self._portal.version)
            self._start_pipeline(stream, width)
        except BaseException:
            self.close()
            raise

    def _start_pipeline(self, stream: PortalStream, width: int) -> None:
        Gst = self._Gst
        try:
            self._pipeline = Gst.parse_launch(pipeline_description(stream.fd, stream.node_id, width))
        except Exception as exc:  # GLib.Error
            raise CaptureError(f"could not build the PipeWire capture pipeline: {exc}", retry=False) from exc
        self._sink = self._pipeline.get_by_name("sink")
        self._src = self._pipeline.get_by_name("src")
        self._bus = self._pipeline.get_bus()
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._check_bus()
            raise CaptureError("could not start PipeWire capture")
        self._check_bus()

    def _check_bus(self) -> None:
        Gst = self._Gst
        msg = self._bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return
        if msg.type == Gst.MessageType.EOS:
            raise CaptureError("screen sharing ended - start sync again to share", retry=False)
        err, debug = msg.parse_error()
        log.debug("pipewiresrc error: %s (%s)", err.message, debug)
        raise CaptureError(f"PipeWire capture failed: {err.message}")

    def _source_dims(self, fallback: tuple[int, int]) -> tuple[int, int]:
        if self._source_size is None:
            caps = self._src.get_static_pad("src").get_current_caps()
            if caps is not None and caps.get_size():
                st = caps.get_structure(0)
                ok_w, w = st.get_int("width")
                ok_h, h = st.get_int("height")
                if ok_w and ok_h and w > 0 and h > 0:
                    self._source_size = (w, h)
        return self._source_size or fallback

    def grab(self, timeout_ms: int = 100) -> Frame | None:
        if self._pipeline is None:
            raise CaptureError("capture is closed")
        sample = self._sink.emit("try-pull-sample", max(1, timeout_ms) * self._Gst.MSECOND)
        if sample is None:
            self._check_bus()
            now = time.monotonic()
            if now - self._last_poll > 1.0:  # static screen: is the session still alive?
                self._last_poll = now
                if self._portal is not None and self._portal.session_closed():
                    raise CaptureError("screen sharing was stopped - start sync again to share", retry=False)
            return None
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        return frame_from_bgrx(data, w, h, self._source_dims((w, h)))

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None
        if self._fd >= 0:  # pipewiresrc dup()s the fd; ours is still ours to close
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        if self._portal is not None:
            self._portal.close()
            self._portal = None
