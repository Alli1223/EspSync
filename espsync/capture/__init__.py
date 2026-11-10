"""Screen capture backends.

* ``dxgi`` - Windows Desktop Duplication, GPU downscaling, HDR (FP16) aware.
* ``mss``  - portable fallback (Windows GDI, Linux X11), SDR only.
"""

from __future__ import annotations

import logging
import sys

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
    "list_monitors",
    "resolve_backend",
]

BACKENDS = ("dxgi", "mss")


def available_backends(platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    names = []
    if platform == "win32":
        names.append("dxgi")
    names.append("mss")
    return names


def resolve_backend(name: str, platform: str | None = None) -> str:
    """Map "auto" (or a backend this version doesn't have) to the best one for this machine."""
    if name in BACKENDS:
        return name
    # "auto", or e.g. "portal" saved by 0.1.x (Wayland capture was removed in 0.2.0).
    platform = platform or sys.platform
    return "dxgi" if platform == "win32" else "mss"


def list_monitors(backend: str = "auto") -> list[MonitorInfo]:
    name = resolve_backend(backend)
    if name == "dxgi":
        try:
            from .dxgi import list_monitors as dxgi_list

            return dxgi_list()
        except Exception:  # noqa: BLE001 - fall back to mss enumeration
            log.exception("DXGI monitor enumeration failed")
    from .mss_backend import list_monitors as mss_list

    return mss_list()


def create_backend(settings: SyncSettings) -> CaptureBackend:
    name = resolve_backend(settings.capture_backend)
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
