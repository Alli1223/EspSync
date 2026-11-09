"""The sync engine: capture -> colours -> lights.

Threads
-------
* capture thread : grabs frames, runs the colour pipeline, applies smoothing.
* network thread : one asyncio loop owning every device connection/socket.

The capture thread hands colours to the network loop with
``call_soon_threadsafe`` (which wakes the loop immediately, no timer tick) and
only the newest colours are kept, so a stall anywhere drops frames instead of
building latency.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .capture import CaptureBackend, CaptureError, create_backend
from .color import ColorProcessor, Smoother, linear_to_srgb
from .config import AppConfig, LightConfig
from .lights import ApiLightOutput, EffectController, EspHomeDevice, LightOutput, UdpOutput, VirtualOutput
from .mapping import DEFAULT_ASPECT, light_regions

log = logging.getLogger(__name__)


@dataclass
class EngineStats:
    running: bool = False
    backend: str = ""
    hdr: bool = False
    capture_fps: float = 0.0  # new screen frames per second
    loop_fps: float = 0.0  # pipeline iterations per second
    process_ms: float = 0.0  # frame acquired -> colours computed
    latency_ms: float = 0.0  # frame acquired -> handed to the network loop
    bars: tuple[float, float] = (0.0, 0.0)
    error: str | None = None
    lights: dict[str, str] = field(default_factory=dict)  # light id -> status text


class _NetLoop:
    """A background asyncio loop shared by all outputs."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="espsync-net", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: float | None = None):  # noqa: ANN001
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


def _set_timer_resolution(enable: bool) -> None:
    """1 ms Windows timer resolution while syncing (asyncio timers/sleeps)."""
    if sys.platform != "win32":
        return
    try:
        winmm = ctypes.WinDLL("winmm")
        (winmm.timeBeginPeriod if enable else winmm.timeEndPeriod)(1)
    except OSError:
        pass


class SyncEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stats = EngineStats()
        self._net: _NetLoop | None = None
        self._capture_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._regions_dirty = True
        self._colors: dict[str, np.ndarray] = {}
        self._preview: np.ndarray | None = None
        self._preview_time = 0.0
        self.want_preview = True

        # network-loop state
        self._devices: dict[str, EspHomeDevice] = {}
        self._outputs: dict[str, LightOutput] = {}
        self._effects: list[EffectController] = []
        self._zeroconf = None
        self._deliver_pending = False
        self._latest: dict[str, np.ndarray] = {}
        self._latest_stamp = 0.0
        self._start_future = None
        self.sending = False

    # ------------------------------------------------------------------ public
    @property
    def running(self) -> bool:
        return self._running.is_set()

    def mark_dirty(self) -> None:
        """Light positions/regions changed: recompute sample regions next frame."""
        self._regions_dirty = True

    def start(self, send: bool = True) -> None:
        """Start capturing.  With send=False nothing is sent to lights (preview
        mode: colours are still computed for the GUI, at a lower frame rate)."""
        if self.running:
            return
        self.sending = send
        self.stats = EngineStats(running=True)
        if self._net is None:
            self._net = _NetLoop()
        if send:
            _set_timer_resolution(True)
            # Devices connect in the background; lights join as they become ready.
            self._start_future = asyncio.run_coroutine_threadsafe(self._start_outputs(), self._net.loop)
        self._running.set()
        self._regions_dirty = True
        self._capture_thread = threading.Thread(target=self._capture_main, name="espsync-capture", daemon=True)
        self._capture_thread.start()

    def stop(self, restore: bool | None = None) -> None:
        if not self.running:
            return
        self._running.clear()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None
        if self.sending and self._net is not None:
            restore = self.config.sync.restore_on_stop if restore is None else restore
            # Don't wait for devices that are still connecting (could be 10 s
            # for an offline one): cancel; _stop_outputs tidies up what exists.
            if self._start_future is not None and not self._start_future.done():
                self._start_future.cancel()
            try:
                self._net.run(self._stop_outputs(restore), timeout=15)
            except Exception:  # noqa: BLE001
                log.exception("error while stopping outputs")
            _set_timer_resolution(False)
        self._start_future = None
        self.stats.running = False

    def shutdown(self) -> None:
        self.stop()
        if self._net is not None:
            self._net.close()
            self._net = None

    def restart(self) -> None:
        if self.running:
            send = self.sending
            self.stop(restore=False)
            self.start(send=send)

    def colors(self) -> dict[str, np.ndarray]:
        with self._lock:
            return dict(self._colors)

    def preview(self) -> np.ndarray | None:
        """Latest captured frame as uint8 sRGB (h, w, 3), tone-mapped."""
        with self._lock:
            return self._preview

    def light_status(self) -> dict[str, str]:
        out = {}
        for lid, o in list(self._outputs.items()):
            out[lid] = o.status_text()
        return out

    # ----------------------------------------------------------- network side
    async def _start_outputs(self) -> None:
        from zeroconf.asyncio import AsyncZeroconf

        cfg = self.config
        lights = [l for l in cfg.lights if l.enabled]
        needed = {l.device_id for l in lights if l.device_id and (l.transport == "api" or l.udp_effect or not l.udp_host)}
        if needed and self._zeroconf is None:
            self._zeroconf = AsyncZeroconf()
        for dev_id in needed:
            dcfg = cfg.device(dev_id)
            if dcfg is None:
                continue
            dev = EspHomeDevice(dcfg, self._zeroconf)
            self._devices[dev_id] = dev
            await dev.start()

        for light in lights:
            self._outputs[light.id] = self._make_output(light)
        await asyncio.gather(*(self._start_one(l) for l in lights), return_exceptions=True)

    def _make_output(self, light: LightConfig) -> LightOutput:
        s = self.config.sync
        if light.transport == "api":
            dev = self._devices.get(light.device_id)
            if dev is None:
                out: LightOutput = VirtualOutput(light, s, 60)
                out.error = "no device selected"
                return out
            return ApiLightOutput(light, s, dev)
        if light.transport in ("wled", "e131"):
            return UdpOutput(light, s, light.udp_host)
        return VirtualOutput(light, s, 60)

    async def _start_one(self, light: LightConfig) -> None:
        out = self._outputs[light.id]
        try:
            if isinstance(out, UdpOutput):
                dev = self._devices.get(light.device_id)
                if dev is not None and (light.udp_effect or not out.host):
                    await dev.wait_connected(10.0)
                    if not out.host:
                        out.host = dev.client.connected_address or dev.cfg.host
                    if light.udp_effect:
                        ctl = EffectController(dev, light.entity, light.udp_effect)
                        err = await ctl.start()
                        if err:
                            out.error = err
                        else:
                            self._effects.append(ctl)
            await out.start()
        except Exception as exc:  # noqa: BLE001
            out.status = "error"
            out.error = str(exc) or type(exc).__name__
            log.warning("Light %s failed to start: %s", light.name, out.error)

    async def _stop_outputs(self, restore: bool) -> None:
        for out in self._outputs.values():
            try:
                await out.stop(restore)
            except Exception:  # noqa: BLE001
                log.exception("stopping %s", out.cfg.name)
        for ctl in self._effects:
            try:
                await ctl.stop(restore)
            except Exception:  # noqa: BLE001
                log.exception("restoring effect")
        await asyncio.sleep(0.15)  # let final commands leave the socket
        for dev in self._devices.values():
            await dev.stop()
        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None
        self._outputs.clear()
        self._devices.clear()
        self._effects.clear()

    def _deliver(self) -> None:
        """Runs on the network loop: push the newest colours to every output."""
        self._deliver_pending = False
        latest = self._latest
        self.stats.latency_ms = (time.perf_counter() - self._latest_stamp) * 1000.0
        for lid, colors in latest.items():
            out = self._outputs.get(lid)
            if out is not None:
                out.submit(colors)

    # ----------------------------------------------------------- capture side
    def _build_plan(self, aspect: float) -> tuple[np.ndarray, list[tuple[LightConfig, slice]]]:
        plan = []
        regions = []
        offset = 0
        for light in self.config.lights:
            if not light.enabled:
                continue
            rs = light_regions(light, self.config.room, aspect)
            plan.append((light, slice(offset, offset + len(rs))))
            regions.extend(r.as_list() for r in rs)
            offset += len(rs)
        arr = np.array(regions, dtype=np.float32).reshape(-1, 4)
        return arr, plan

    def _capture_main(self) -> None:
        backend: CaptureBackend | None = None
        processor = ColorProcessor(self.config.sync)
        smoother = Smoother()
        targets: dict[str, np.ndarray] = {}
        regions = np.zeros((0, 4), np.float32)
        plan: list[tuple[LightConfig, slice]] = []
        aspect = DEFAULT_ASPECT
        backend_key = None
        last_frame = None
        last_tick = time.perf_counter()
        frame_count = loop_count = 0
        rate_t0 = last_tick
        retry_at = 0.0

        try:
            while self._running.is_set():
                s = self.config.sync
                fps = s.target_fps if self.sending else min(s.target_fps, 20)
                interval = 1.0 / max(5, min(fps, 240))
                tick_start = time.perf_counter()

                key = (s.capture_backend, s.monitor, s.hdr, s.sdr_white_nits, s.hdr_peak_nits)
                if backend is not None and key != backend_key:
                    backend.close()
                    backend = None
                if backend is None:
                    if tick_start < retry_at:
                        time.sleep(0.05)
                        continue
                    try:
                        backend = create_backend(s)
                        backend_key = key
                        self.stats.backend = backend.name
                        self.stats.error = None
                    except Exception as exc:  # noqa: BLE001
                        self.stats.error = f"capture: {exc}"
                        log.warning("capture backend failed: %s", exc)
                        retry_at = tick_start + 2.0
                        continue

                try:
                    frame = backend.grab(timeout_ms=max(1, int(interval * 1000)))
                except CaptureError as exc:
                    self.stats.error = f"capture: {exc}"
                    backend.close()
                    backend = None
                    retry_at = time.perf_counter() + 1.0
                    continue

                fresh = frame is not None
                if frame is None and self._regions_dirty and last_frame is not None:
                    frame = last_frame  # layout edited while the screen is static
                if frame is not None:
                    last_frame = frame
                    frame_count += fresh
                    if abs(frame.aspect - aspect) > 1e-3:
                        aspect = frame.aspect
                        self._regions_dirty = True
                    if self._regions_dirty:
                        self._regions_dirty = False
                        regions, plan = self._build_plan(aspect)
                    colors = processor.process(frame, regions) if len(regions) else np.zeros((0, 3), np.float32)
                    self.stats.hdr = frame.hdr
                    self.stats.bars = processor.letterbox.bars
                    for light, sl in plan:
                        gain = np.asarray(light.gains[:3], np.float32) * (s.brightness * light.brightness)
                        targets[light.id] = np.clip(colors[sl] * gain, 0.0, 1.0)
                    self.stats.process_ms = (time.perf_counter() - frame.timestamp) * 1000.0
                    if self.want_preview and tick_start - self._preview_time > 0.1:
                        self._preview_time = tick_start
                        img = frame.image
                        enc = (linear_to_srgb(img.reshape(-1, 3)).reshape(img.shape) * 255 + 0.5).astype(np.uint8)
                        with self._lock:
                            self._preview = enc

                now = time.perf_counter()
                dt = now - last_tick
                last_tick = now
                tau = s.smoothing_ms / 1000.0
                out: dict[str, np.ndarray] = {}
                for light, _ in plan:
                    t = targets.get(light.id)
                    if t is not None:
                        out[light.id] = smoother.apply(light.id, t, dt, tau)
                with self._lock:
                    self._colors = out
                self._latest = out
                self._latest_stamp = frame.timestamp if fresh and frame is not None else now
                if self.sending and not self._deliver_pending and self._net is not None:
                    self._deliver_pending = True
                    self._net.loop.call_soon_threadsafe(self._deliver)

                loop_count += 1
                if now - rate_t0 >= 1.0:
                    self.stats.capture_fps = frame_count / (now - rate_t0)
                    self.stats.loop_fps = loop_count / (now - rate_t0)
                    self.stats.lights = self.light_status()
                    frame_count = loop_count = 0
                    rate_t0 = now

                # Frame pacing: backends that don't block (mss) need a sleep.
                remaining = interval - (time.perf_counter() - tick_start)
                if remaining > 0.001 and fresh:
                    time.sleep(remaining)
        except Exception as exc:  # noqa: BLE001
            log.exception("capture loop crashed")
            self.stats.error = str(exc)
        finally:
            if backend is not None:
                backend.close()
