"""Common types for screen capture backends."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

# Target width of the image handed to the colour pipeline.  Colours are averaged
# over regions anyway, so a small image is plenty and keeps processing ~1 ms.
TARGET_WIDTH = 160


class CaptureError(RuntimeError):
    """The backend cannot capture right now.

    `retry=False` means trying again on its own won't help (missing
    dependency, the user declined or stopped screen sharing): the engine then
    waits until the capture settings change or sync is restarted.
    """

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass
class MonitorInfo:
    index: int
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    hdr: bool | None = None  # None = unknown

    def label(self) -> str:
        hdr = " HDR" if self.hdr else ""
        prim = " (primary)" if self.primary else ""
        return f"{self.index + 1}: {self.name} {self.width}x{self.height}{hdr}{prim}"


@dataclass
class Frame:
    """A downscaled capture in *linear light*.

    `image` is float32 (h, w, 3).  1.0 is SDR reference white; HDR content may
    exceed 1.0 up to `peak` (also relative to SDR white).

    `probe` is a small part of the screen at full resolution (same units,
    never tone-mapped), when the engine asked for it via
    `CaptureBackend.probe_rect` and the backend can provide it.  Game
    integrations read pixel-sized signals there (see espsync.wow and
    espsync.overwatch).
    """

    image: np.ndarray
    source_size: tuple[int, int]
    hdr: bool = False
    peak: float = 1.0
    timestamp: float = field(default_factory=time.perf_counter)
    mapped: bool = False  # set once tone mapping/clipping has been applied in place
    probe: np.ndarray | None = None

    @property
    def aspect(self) -> float:
        w, h = self.source_size
        return w / h if h else 16 / 9


def _srgb_to_linear_lut() -> np.ndarray:
    c = np.arange(256, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin.astype(np.float32)


SRGB_TO_LINEAR = _srgb_to_linear_lut()


def bgra_to_linear(bgra: np.ndarray) -> np.ndarray:
    """uint8 BGRA/BGRX (h, w, 4) -> float32 linear RGB (h, w, 3)."""
    return SRGB_TO_LINEAR[bgra[..., 2::-1]]


def clip_rect(rect: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int] | None:
    """`rect` (x, y, w, h) clipped to a width x height screen; None if nothing is left."""
    x, y, w, h = (int(v) for v in rect)
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, width), min(y + h, height)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


class CaptureBackend(ABC):
    name = "base"

    def __init__(self, monitor: int = 0) -> None:
        self.monitor = monitor
        # (x, y, width, height) in screen pixels of the full-resolution area to
        # return as Frame.probe; None = not needed.  Set by the engine at any
        # time; backends clip it to the screen.
        self.probe_rect: tuple[int, int, int, int] | None = None

    @abstractmethod
    def grab(self, timeout_ms: int = 100) -> Frame | None:
        """Return the newest frame, or None if the screen has not changed."""

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def __enter__(self) -> CaptureBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
