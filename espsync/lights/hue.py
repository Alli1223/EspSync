"""Philips Hue output: the Entertainment API stream (HueStream v2 over DTLS).

One `HueEntertainmentStream` per bridge owns the REST start/stop and the DTLS
session.  Hue lights don't send on their own: each `HueLightOutput` writes its
channel colours into the stream, and the stream sends *one* packet with every
channel per frame.  The flush is scheduled with ``call_soon`` from the first
write, so it runs right after the engine has handed this frame to all outputs.
Like the other outputs it is latest-value-wins: at most ``hue_rate_hz`` packets
a second, never a queue.

Stream lifecycle (all on the engine's network loop):

1. check the area isn't streamed by another app (Hue Sync, a TV, ...),
2. ``PUT entertainment_configuration/{id} {"action": "start"}``,
3. DTLS 1.2 PSK handshake to UDP 2100,
4. stream; resend the last packet when idle so the bridge keeps the session;
   poll the area every few seconds and on loss (another app took over, the
   bridge ended it) reconnect when the area is free again,
5. on stop: close_notify, ``{"action": "stop"}``, then put the lights back.

The bridge does *not* restore lights when a stream ends (checked on a BSB002,
firmware 1978545030): they keep the last streamed colour.  So before the first
start the stream reads the area's lights over REST and writes them back after
the stop.  While an area streams, the REST light state keeps the pre-stream
values, so a snapshot taken after an EspSync restart is still the original.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

import numpy as np

from ..config import HueBridgeConfig, LightConfig, SyncSettings
from ..hue import STREAM_PORT, HueApi, HueArea, HueAuthError, HueError, restore_payload
from .base import LightOutput
from .dtls import DtlsAlert, DtlsError, DtlsPskClient

log = logging.getLogger(__name__)

HEADER = b"HueStream"
VERSION = b"\x02\x00"
COLOR_SPACE_RGB = 0x00
MAX_CHANNELS = 20  # per packet
KEEPALIVE_S = 1.0  # resend the last packet after this long without one (bridge drops the session after ~10 s)
WATCH_S = 5.0  # poll the area's status this often while streaming
RETRY_S = 5.0  # wait between reconnect attempts
CONNECT_TIMEOUT = 8.0
RESTORE_GAP_S = 0.1  # between light commands: the bridge takes ~10 REST commands a second


def huestream_packet(area_id: str, channels: Iterable[tuple[int, bytes]], sequence: int = 0) -> bytes:
    """HueStream v2 packet: 16-byte header, the 36-char area id, then 7 bytes per
    channel (id + big-endian 16-bit R, G, B, as ``bytes`` of length 6)."""
    aid = area_id.encode("ascii")
    if len(aid) != 36:
        raise ValueError("entertainment area id must be a 36-character UUID")
    head = HEADER + VERSION + struct.pack("!BHBB", sequence & 0xFF, 0, COLOR_SPACE_RGB, 0) + aid
    return head + b"".join(struct.pack("!B", cid) + rgb for cid, rgb in channels)


def to_rgb16(colors: np.ndarray) -> list[bytes]:
    """(n, 3) floats 0..1 -> n big-endian 16-bit RGB triplets."""
    vals = (np.clip(colors, 0.0, 1.0) * 65535.0 + 0.5).astype(">u2")
    return [row.tobytes() for row in vals]


class StreamTransport(Protocol):
    def send(self, data: bytes) -> None: ...
    def close(self) -> None: ...


Connector = Callable[["HueEntertainmentStream"], Awaitable[StreamTransport]]


async def dtls_connect(stream: HueEntertainmentStream) -> StreamTransport:
    b = stream.bridge
    try:
        psk = bytes.fromhex(b.client_key)
    except ValueError as exc:
        raise HueError("the stored client key is not hex: pair the bridge again") from exc
    client = DtlsPskClient(b.host, STREAM_PORT, b.app_key.encode(), psk)
    client.on_close = stream.connection_lost
    try:
        await client.connect(CONNECT_TIMEOUT)
    except DtlsAlert as exc:
        if exc.description in (20, 51, 115):  # bad_record_mac, decrypt_error, unknown_psk_identity
            raise HueAuthError() from exc
        raise HueError(f"the bridge refused the stream ({exc})") from exc
    except TimeoutError as exc:
        raise HueError(f"no DTLS answer from {b.host}:{STREAM_PORT} (firewall?)") from exc
    except (DtlsError, OSError) as exc:
        raise HueError(f"stream connection failed: {exc}") from exc
    return client


class HueEntertainmentStream:
    """The streaming session to one bridge, shared by all its lights."""

    def __init__(
        self,
        bridge: HueBridgeConfig,
        settings: SyncSettings,
        api: HueApi | None = None,
        connector: Connector = dtls_connect,
    ) -> None:
        self.bridge = bridge
        self.settings = settings
        self.api = api or HueApi(bridge.host, bridge.app_key)
        self.connector = connector
        self.status = "idle"
        self.error: str | None = None
        self.area: HueArea | None = None
        self.sent = 0
        self.rate_hz = 0.0  # measured packets/second

        self._transport: StreamTransport | None = None
        self._frame: dict[int, bytes] = {}
        self._sequence = 0
        # Rate limit on perf_counter: asyncio's clock on Windows (CPython 3.12)
        # ticks every 15.6 ms and fires timers up to a tick early, so pacing by
        # the time of the last send overshoots (~58 Hz for 50).  Instead each
        # packet takes the next slot on a fixed grid, which caps the average.
        self._next_slot = 0.0
        self._last_send = -1e9
        self._last_packets: list[bytes] = []
        self._flush_handle: asyncio.Handle | None = None
        self._tick_handle: asyncio.TimerHandle | None = None
        self._rate_t0 = 0.0
        self._rate_n = 0
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._lost = asyncio.Event()
        self._stopping = False
        self._activated = False  # we sent {"action": "start"} and not yet "stop"
        self._snapshot: dict[str, dict] | None = None  # light id -> CLIP v2 state before streaming

    # ------------------------------------------------------------------ state
    @property
    def ready(self) -> bool:
        return self._transport is not None and self._ready.is_set()

    @property
    def interval(self) -> float:
        return 1.0 / max(self.settings.hue_rate_hz, 1.0)

    def status_text(self) -> str:
        if self.ready:
            rate = self.rate_hz
            if not rate and self._rate_n:  # first second: measure what we have so far
                rate = self._rate_n / max(time.perf_counter() - self._rate_t0, 1e-3)
            return f"streaming {rate:.0f} Hz"
        return f"{self.status}: {self.error}" if self.error else self.status

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def has_channel(self, channel_id: int) -> bool:
        return self.area is not None and any(c.channel_id == channel_id for c in self.area.channels)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._open()
                await self._watch()
            except asyncio.CancelledError:
                raise
            except HueError as exc:
                self._set_error(str(exc))
            except Exception as exc:  # noqa: BLE001 - keep retrying
                log.exception("Hue stream to %s failed", self.bridge.name)
                self._set_error(str(exc) or type(exc).__name__)
            self._drop_transport()
            if not self._stopping:
                await asyncio.sleep(RETRY_S)

    def _set_error(self, msg: str) -> None:
        if msg != self.error:
            log.warning("Hue %s: %s", self.bridge.name or self.bridge.host, msg)
        self.status = "error"
        self.error = msg

    async def _open(self) -> None:
        b = self.bridge
        if not b.paired:
            raise HueError("bridge not paired")
        if not b.entertainment_id:
            raise HueError("no entertainment area chosen for this bridge")
        if self.status != "error":
            self.status = "connecting"
        app_id = await self.api.application_id()
        areas = await self.api.areas(with_names=False)
        area = next((a for a in areas if a.id == b.entertainment_id), None)
        if area is None:
            raise HueError("entertainment area not found (deleted in the Hue app?)")
        self.area = area
        # A bridge streams one area at a time: never start ours over another
        # app's stream (the bridge would hand the lights to us mid-session).
        busy = next((a for a in areas if a.status == "active" and a.streamer and a.streamer != app_id), None)
        if busy is not None:
            raise HueError(f"'{busy.name}' is in use by another app (Hue Sync?): stop it there first")
        if self._snapshot is None:
            try:
                self._snapshot = await self.api.light_states(area.light_ids)
            except HueError as exc:
                log.warning("Hue %s: couldn't read the lights to restore later: %s", b.name, exc)
        await self.api.set_streaming(b.entertainment_id, True)
        self._activated = True
        self._lost.clear()
        transport = await self.connector(self)
        if self._stopping:
            transport.close()
            return
        self._transport = transport
        self.status = "ready"
        self.error = None
        self._rate_t0 = time.perf_counter()
        self._rate_n = 0
        self._ready.set()
        self._schedule_tick()
        if self._frame:
            self._schedule_flush()
        log.info("Hue %s: streaming to '%s' (%d channels)", b.name or b.host, area.name, len(area.channels))

    async def _watch(self) -> None:
        """Return (raising HueError) when the stream is gone."""
        app_id = await self.api.application_id()
        while not self._stopping:
            try:
                await asyncio.wait_for(self._lost.wait(), WATCH_S)
                raise HueError("the bridge closed the stream")
            except TimeoutError:
                pass
            area = await self.api.area(self.bridge.entertainment_id)
            if area.status != "active":
                raise HueError("the bridge ended the stream")
            if area.streamer and app_id and area.streamer != app_id:
                raise HueError(f"another app took over '{area.name}' (Hue Sync?)")

    def connection_lost(self, exc: Exception | None) -> None:
        """DTLS peer closed the session (close_notify / fatal alert)."""
        log.info("Hue %s: DTLS session closed (%s)", self.bridge.name, exc)
        self._lost.set()

    def _drop_transport(self) -> None:
        self._ready.clear()
        for h in (self._flush_handle, self._tick_handle):
            if h is not None:
                h.cancel()
        self._flush_handle = self._tick_handle = None
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
            self._transport = None
        self.rate_hz = 0.0

    async def stop(self, deactivate: bool = True, restore: bool | None = None) -> None:
        """Stop streaming.

        deactivate=False leaves the area active (restarts: the lights hold their
        colour until the next session takes over).  restore (default: same as
        deactivate) puts the lights back the way they were before streaming.
        """
        restore = deactivate if restore is None else restore
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._drop_transport()
        if deactivate and self._activated:
            try:
                await asyncio.wait_for(self.api.set_streaming(self.bridge.entertainment_id, False), 5.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("Hue %s: stopping the stream failed: %s", self.bridge.name, exc)
            self._activated = False
            if restore:
                try:
                    await asyncio.wait_for(self._restore(), 10.0)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Hue %s: restoring the lights failed: %s", self.bridge.name, exc)
        self.status = "idle"

    async def _restore(self) -> None:
        snapshot, self._snapshot = self._snapshot, None
        for i, (light_id, state) in enumerate((snapshot or {}).items()):
            if i:
                await asyncio.sleep(RESTORE_GAP_S)
            try:
                await self.api.put_light(light_id, restore_payload(state))
            except HueError as exc:
                log.warning("Hue %s: restoring %s failed: %s", self.bridge.name, light_id, exc)

    # ---------------------------------------------------------------- frames
    def write(self, channel_ids: list[int], colors: np.ndarray) -> None:
        """Set channel colours ((len(channel_ids), 3) floats 0..1) for the next packet."""
        for cid, rgb in zip(channel_ids, to_rgb16(colors)):
            self._frame[cid] = rgb
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None or not self.ready:
            return
        loop = asyncio.get_running_loop()
        wait = self._next_slot - time.perf_counter()
        if wait <= 0:
            # call_soon: run after every output has written this frame.
            self._flush_handle = loop.call_soon(self._flush)
        else:
            self._flush_handle = loop.call_later(wait, self._flush)

    def packets(self) -> list[bytes]:
        items = sorted(self._frame.items())
        out = []
        for i in range(0, len(items), MAX_CHANNELS):
            out.append(huestream_packet(self.bridge.entertainment_id, items[i : i + MAX_CHANNELS], self._sequence))
            self._sequence = (self._sequence + 1) & 0xFF
        return out

    def _flush(self) -> None:
        self._flush_handle = None
        if not self.ready or not self._frame:
            return
        self._send(self.packets())

    def _send(self, packets: list[bytes]) -> None:
        assert self._transport is not None
        try:
            for p in packets:
                self._transport.send(p)
        except Exception as exc:  # noqa: BLE001 - e.g. network down; the watchdog reconnects
            log.debug("Hue send failed: %s", exc)
            self._lost.set()
            return
        self._last_packets = packets
        now = time.perf_counter()
        self._last_send = now
        self._next_slot = max(self._next_slot, now - self.interval) + self.interval
        self.sent += 1
        self._rate_n += 1

    def _schedule_tick(self) -> None:
        self._tick_handle = asyncio.get_running_loop().call_later(KEEPALIVE_S / 2, self._tick)

    def _tick(self) -> None:
        """Keep-alive and rate measurement."""
        self._tick_handle = None
        if not self.ready:
            return
        if self._last_packets and time.perf_counter() - self._last_send >= KEEPALIVE_S:
            self._send(self._last_packets)
        now = time.perf_counter()
        if now - self._rate_t0 >= 1.0:
            self.rate_hz = self._rate_n / (now - self._rate_t0)
            self._rate_t0, self._rate_n = now, 0
        self._schedule_tick()


class HueLightOutput(LightOutput):
    """A light (or gradient strip) driven through its bridge's shared stream."""

    def __init__(self, cfg: LightConfig, settings: SyncSettings, stream: HueEntertainmentStream) -> None:
        super().__init__(cfg, settings, settings.hue_rate_hz)
        self.stream = stream
        self._checked = False
        self._last_hue: np.ndarray | None = None

    async def start(self) -> None:
        if not self.cfg.hue_channels:
            self.status = "error"
            self.error = "no Hue channel selected"
            return
        self.status = "connecting"
        if await self.stream.wait_ready(CONNECT_TIMEOUT + 4):
            self._check()

    def _check(self) -> bool:
        missing = [c for c in self.cfg.hue_channels if not self.stream.has_channel(c)]
        if missing:
            self.status = "error"
            area = self.stream.area.name if self.stream.area else "the area"
            self.error = f"channel {', '.join(map(str, missing))} is not in '{area}'"
            return False
        self.status = "ready"
        self.error = None
        self._checked = True
        return True

    @property
    def ready(self) -> bool:
        if not self.stream.ready or not self.cfg.hue_channels:
            return False
        if not self._checked:
            return self._check()
        return self.status == "ready"

    def status_text(self) -> str:
        if self.status == "error" and self.error:
            return f"error: {self.error}"
        if not self.stream.ready:
            st = self.stream
            return f"{st.status}: {st.error}" if st.error else ("connecting" if st.status != "idle" else st.status)
        return super().status_text()

    def submit(self, colors: np.ndarray) -> None:
        """Write straight into the bridge's frame; the stream rate-limits."""
        if not self.ready:
            return
        chans = self.cfg.hue_channels
        rgb = np.asarray(colors, dtype=np.float32).reshape(-1, 3)
        if len(rgb) != len(chans):
            # lamp: one colour for every channel; strip: resample LEDs onto channels
            idx = (np.arange(len(chans)) * len(rgb)) // len(chans)
            rgb = rgb[idx]
        self.stream.write(chans, self._floor(rgb))
        self.sent += 1

    def _floor(self, rgb: np.ndarray) -> np.ndarray:
        """Min-brightness / black-turns-off handling, as for ESPHome lights."""
        s = self.settings
        peak = rgb.max(axis=1)
        if s.black_turns_off:
            return np.where((peak < max(s.min_brightness, 1 / 255))[:, None], 0.0, rgb)
        floor = s.min_brightness
        if floor <= 0:
            return rgb
        lit = peak > 1e-3
        hue = np.where(lit[:, None], rgb / np.maximum(peak, 1e-6)[:, None], 1.0)
        if self._last_hue is not None and len(self._last_hue) == len(rgb):
            hue = np.where(lit[:, None], hue, self._last_hue)  # keep the last colour when dimming to the floor
        self._last_hue = hue
        return np.where((peak < floor)[:, None], hue * floor, rgb)

    def encode(self, colors: np.ndarray) -> bytes:  # unused: submit() is overridden
        return b""

    def transmit(self, colors: np.ndarray, payload: bytes) -> None:  # unused
        pass
