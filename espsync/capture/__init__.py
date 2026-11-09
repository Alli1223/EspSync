"""Screen capture backends.

* ``dxgi``   - Windows Desktop Duplication, GPU downscaling, HDR (FP16) aware.
* ``portal`` - Linux Wayland (GNOME, KDE, wlroots): xdg-desktop-portal
  ScreenCast + PipeWire.  Optional extra: ``pip install espsync[wayland]``.
* ``mss``    - portable fallback (Linux X11, Windows GDI), SDR only.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping

from ..config import SyncSettings
from .base import CaptureBackend, CaptureError, Frame, MonitorInfo

log = logging.getLogger(__name__)

__all__ = [
    "CaptureBackend",
    "CaptureError",
    "Frame",
    "MonitorInfo",
    "available_backends",
    "create_backend",
    "is_wayland_session",
    "list_monitors",
    "resolve_backend",
]


def is_wayland_session(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    session = env.get("XDG_SESSION_TYPE", "").lower()
    if session in ("wayland", "x11"):
        return session == "wayland"
    return bool(env.get("WAYLAND_DISPLAY"))  # e.g. started from a tty/ssh shell


def available_backends(platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    names = []
    if platform == "win32":
        names.append("dxgi")
    if platform.startswith("linux"):
        names.append("portal")
    names.append("mss")
    return names


def resolve_backend(name: str, platform: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Map "auto" to the best backend for this machine."""
    if name != "auto":
        return name
    platform = platform or sys.platform
    if platform == "win32":
        return "dxgi"
    if platform.startswith("linux") and is_wayland_session(env):
        # mss only sees XWayland windows (black elsewhere) on Wayland, so a
        # missing dependency is reported rather than silently falling back.
        return "portal"
    return "mss"


def list_monitors(backend: str = "auto") -> list[MonitorInfo]:
    name = resolve_backend(backend)
    if name == "portal":
        from .portal_backend import list_monitors as portal_list

        return portal_list()
    if name == "dxgi":
        try:
            from .dxgi import list_monitors as dxgi_list

            return dxgi_list()
        except Exception:  # noqa: BLE001 - fall back to mss enumeration
            log.exception("DXGI monitor enumeration failed")
    from .mss_backend import list_monitors as mss_list

    return mss_list()


def create_backend(settings: SyncSettings, abort: Callable[[], bool] | None = None) -> CaptureBackend:
    """Open the configured backend.

    `abort` is polled while a backend waits on the user (the Wayland share
    dialog); returning True gives up.  The portal backend writes the new
    restore token into ``settings.portal_restore_token`` - the caller saves it.
    """
    name = resolve_backend(settings.capture_backend)
    if name == "portal":
        from .portal_backend import PortalBackend

        def keep_token(token: str) -> None:
            settings.portal_restore_token = token

        return PortalBackend(restore_token=settings.portal_restore_token, on_restore_token=keep_token, abort=abort)
    if name == "dxgi":
        try:
            from .dxgi import DxgiBackend

            return DxgiBackend(
                monitor=settings.monitor,
                hdr_mode=settings.hdr,
                sdr_white_override=settings.sdr_white_nits,
                peak_override=settings.hdr_peak_nits,
                gpu_priority=settings.gpu_priority,
            )
        except Exception:
            if settings.capture_backend == "dxgi":
                raise
            log.exception("DXGI capture unavailable, falling back to mss")
    from .mss_backend import MssBackend

    return MssBackend(monitor=settings.monitor)
