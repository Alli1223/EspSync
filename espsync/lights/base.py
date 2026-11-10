"""Light output interface with latest-value-wins rate limiting.

All methods run on the network asyncio loop.  The engine calls `submit()` every
frame; an output transmits immediately if its rate budget allows, otherwise it
keeps only the newest colours and flushes them when the budget frees up.
Nothing ever queues, so a slow light can never fall behind the screen.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

import numpy as np

from ..config import LightConfig, SyncSettings

SETTLE_S = 0.5  # a change smaller than SyncSettings.min_change is sent after this long anyway


class LightOutput(ABC):
    keepalive_s: float | None = None  # resend unchanged colours this often (UDP effects time out)

    def __init__(self, cfg: LightConfig, settings: SyncSettings, rate_hz: float) -> None:
        self.cfg = cfg
        self.settings = settings
        self.interval = 1.0 / max(rate_hz, 1.0)
        self.status = "idle"
        self.error: str | None = None
        self.sent = 0
        self._last_send = -1e9
        self._last_payload: bytes | None = None
        self._pending: np.ndarray | None = None
        self._timer: asyncio.TimerHandle | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def status_text(self) -> str:
        return f"{self.status}: {self.error}" if self.error else self.status

    async def start(self) -> None:
        self.status = "ready"

    async def stop(self, restore: bool) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.status = "idle"

    def submit(self, colors: np.ndarray) -> None:
        """Offer new colours ((n, 3) gamma-encoded 0..1)."""
        self._pending = colors
        if self._timer is not None:
            return
        loop = asyncio.get_running_loop()
        wait = self._last_send + self.interval - loop.time()
        if wait <= 0:
            self._flush()
        else:
            self._timer = loop.call_later(wait, self._flush)

    def _flush(self) -> None:
        self._timer = None
        colors, self._pending = self._pending, None
        if colors is None or not self.ready:
            return
        payload = self.encode(colors)
        now = asyncio.get_running_loop().time()
        if payload == self._last_payload:
            if self.keepalive_s is None or now - self._last_send < self.keepalive_s:
                return
        elif now - self._last_send < SETTLE_S and self._small_change(payload):
            return  # the engine submits every frame, so this is retried
        try:
            self.transmit(colors, payload)
        except Exception as exc:  # noqa: BLE001 - keep the sync loop alive
            self.error = str(exc)
            return
        self._last_payload = payload
        self._last_send = now
        self.sent += 1

    def _small_change(self, payload: bytes) -> bool:
        """True when no channel moved more than `min_change` since the last send."""
        limit = int(self.settings.min_change * 255.0 + 0.5)
        last = self._last_payload
        if limit <= 0 or last is None or len(last) != len(payload):
            return False
        a = np.frombuffer(payload, np.uint8).astype(np.int16)
        return int(np.abs(a - np.frombuffer(last, np.uint8)).max(initial=0)) <= limit

    @abstractmethod
    def encode(self, colors: np.ndarray) -> bytes:
        """Quantise colours into a comparable payload (used for change detection)."""

    @abstractmethod
    def transmit(self, colors: np.ndarray, payload: bytes) -> None:
        """Send to the light. Must not block."""


def to_bytes(colors: np.ndarray) -> bytes:
    return (np.clip(colors, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).tobytes()


class VirtualOutput(LightOutput):
    """Preview-only light: nothing is sent anywhere."""

    def encode(self, colors: np.ndarray) -> bytes:
        return to_bytes(colors)

    def transmit(self, colors: np.ndarray, payload: bytes) -> None:
        pass
