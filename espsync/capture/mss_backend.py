"""Portable capture via `mss` (X11 on Linux, GDI on Windows, Quartz on macOS).

Used on Linux/X11 and as a fallback on Windows.  It always returns full-size
8-bit frames, so we subsample on the CPU.  It is SDR only: on an HDR Windows
desktop prefer the DXGI backend.
"""

from __future__ import annotations

import time

import numpy as np

from .base import TARGET_WIDTH, CaptureBackend, CaptureError, Frame, MonitorInfo, bgra_to_linear


def list_monitors() -> list[MonitorInfo]:
    import mss

    with mss.mss() as sct:
        mons = sct.monitors[1:]
    out = []
    for i, m in enumerate(mons):
        out.append(
            MonitorInfo(
                index=i,
                name=f"Display {i + 1}",
                x=m["left"],
                y=m["top"],
                width=m["width"],
                height=m["height"],
                primary=(m["left"] == 0 and m["top"] == 0),
                hdr=False,
            )
        )
    return out


class MssBackend(CaptureBackend):
    name = "mss"

    def __init__(self, monitor: int = 0, min_interval_s: float = 0.0) -> None:
        super().__init__(monitor)
        self._sct = None  # created lazily on the capture thread (X11 is thread-bound)
        self._region: dict | None = None
        self._min_interval = min_interval_s
        self._last = 0.0

    def _ensure(self) -> None:
        if self._sct is not None:
            return
        import mss

        self._sct = mss.mss()
        mons = self._sct.monitors[1:]
        if not mons:
            raise CaptureError("no monitors found")
        self._region = mons[min(self.monitor, len(mons) - 1)]

    def grab(self, timeout_ms: int = 100) -> Frame | None:
        self._ensure()
        assert self._sct is not None and self._region is not None
        wait = self._min_interval - (time.perf_counter() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.perf_counter()
        try:
            shot = self._sct.grab(self._region)
        except Exception as exc:  # mss raises ScreenShotError on X errors etc.
            raise CaptureError(str(exc)) from exc
        w, h = shot.width, shot.height
        raw = np.frombuffer(shot.raw, dtype=np.uint8).reshape(h, w, 4)
        step = max(1, w // TARGET_WIDTH)
        small = raw[step // 2 :: step, step // 2 :: step]
        return Frame(image=bgra_to_linear(small), source_size=(w, h))

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None
