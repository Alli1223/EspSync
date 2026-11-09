"""Screen capture backends.

* ``dxgi`` - Windows Desktop Duplication, GPU downscaling, HDR (FP16) aware.
* ``mss``  - portable fallback (Linux X11, Windows GDI), SDR only.
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
]


def available_backends() -> list[str]:
    names = []
    if sys.platform == "win32":
        names.append("dxgi")
    names.append("mss")
    return names


def _resolve(name: str) -> str:
    if name == "auto":
        return "dxgi" if sys.platform == "win32" else "mss"
    return name


def list_monitors(backend: str = "auto") -> list[MonitorInfo]:
    name = _resolve(backend)
    if name == "dxgi":
        try:
            from .dxgi import list_monitors as dxgi_list

            return dxgi_list()
        except Exception:  # noqa: BLE001 - fall back to mss enumeration
            log.exception("DXGI monitor enumeration failed")
    from .mss_backend import list_monitors as mss_list

    return mss_list()


def create_backend(settings: SyncSettings) -> CaptureBackend:
    name = _resolve(settings.capture_backend)
    if name == "dxgi":
        try:
            from .dxgi import DxgiBackend

            return DxgiBackend(
                monitor=settings.monitor,
                hdr_mode=settings.hdr,
                sdr_white_override=settings.sdr_white_nits,
                peak_override=settings.hdr_peak_nits,
            )
        except Exception:
            if settings.capture_backend == "dxgi":
                raise
            log.exception("DXGI capture unavailable, falling back to mss")
    from .mss_backend import MssBackend

    return MssBackend(monitor=settings.monitor)
