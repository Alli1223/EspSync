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
import math
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .capture import CaptureBackend, CaptureError, create_backend
from .color import ColorProcessor, Smoother, linear_to_srgb
from .config import AppConfig, LightConfig
from . import overwatch
from .effects import GameEffects, element_angles, element_positions
from .lights import ApiLightOutput, EffectController, EspHomeDevice, LightOutput, UdpOutput, VirtualOutput
from .lights.hue import HueEntertainmentStream, HueLightOutput
from .mapping import DEFAULT_ASPECT, light_regions
from .overwatch_effects import OverwatchEffects
from .procwatch import ProcessWatcher
from .wow import PROBE_SIZE, PROCESS_NAMES, decode_signal, hide_strip

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
    game: str = ""  # game integration status, "" while no supported game runs
    streams: dict[str, str] = field(default_factory=dict)  # Hue bridge name -> "streaming 50 Hz" / error


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
        self._generation = 0  # bumped per start(); a stale capture thread sees it and exits
        self._restarting = False  # stop() is half of restart(): keep Hue areas streaming
        self._lock = threading.Lock()
        self._regions_dirty = True
        self._colors: dict[str, np.ndarray] = {}
        self._preview: np.ndarray | None = None
        self._preview_time = 0.0
        self.want_preview = True
        self.game_effects = GameEffects()  # World of Warcraft; owned by the capture thread
        self.overwatch_effects = OverwatchEffects()  # likewise
        self.overwatch_hud = overwatch.HudReader()

        # network-loop state
        self._devices: dict[str, EspHomeDevice] = {}
        self._hue: dict[str, HueEntertainmentStream] = {}  # HueBridgeConfig.id -> stream
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
        self._generation += 1
        self._capture_thread = threading.Thread(
            target=self._capture_main, args=(self._generation,), name="espsync-capture", daemon=True
        )
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
            self._restarting = True
            try:
                self.stop(restore=False)
            finally:
                self._restarting = False
            self.start(send=send)

    def colors(self) -> dict[str, np.ndarray]:
        with self._lock:
            return dict(self._colors)

    def preview(self) -> np.ndarray | None:
        """Latest captured frame as uint8 sRGB (h, w, 3), tone-mapped."""
        with self._lock:
            return self._preview

    def test_levelup(self) -> None:
        """Play the level-up effect now (shown in the preview, sent while syncing)."""
        self.game_effects.trigger_levelup(time.perf_counter())

    def test_low_health(self, seconds: float = 5.0) -> None:
        """Pulse as if health were well below the threshold for a few seconds."""
        health = 0.4 * self.config.sync.wow_low_health
        self.game_effects.test_low_health(time.perf_counter(), health, seconds)

    def test_overwatch(self, kind: str) -> None:
        """Play an Overwatch effect now: "damage", "heal", "lowhealth" or "elimination"."""
        fx, now = self.overwatch_effects, time.perf_counter()
        if kind == "damage":
            fx.test_damage(now)
        elif kind == "heal":
            fx.test_heal(now)
        elif kind == "lowhealth":
            fx.test_low_health(now, 0.35 * self.config.sync.ow_low_health)
        elif kind == "elimination":
            fx.test_elimination(now)

    def light_status(self) -> dict[str, str]:
        out = {}
        for lid, o in list(self._outputs.items()):
            out[lid] = o.status_text()
        return out

    def stream_status(self) -> dict[str, str]:
        return {s.bridge.name or s.bridge.host: s.status_text() for s in list(self._hue.values())}

    # ----------------------------------------------------------- network side
    async def _start_outputs(self) -> None:
        from zeroconf.asyncio import AsyncZeroconf

        cfg = self.config
        lights = [l for l in cfg.lights if l.enabled]
        needed = {
            l.device_id for l in lights
            if l.device_id and l.transport in ("api", "wled", "e131")
            and (l.transport == "api" or l.udp_effect or not l.udp_host)
        }
        if needed and self._zeroconf is None:
            self._zeroconf = AsyncZeroconf()
        for dev_id in needed:
            dcfg = cfg.device(dev_id)
            if dcfg is None:
                continue
            dev = EspHomeDevice(dcfg, self._zeroconf)
            self._devices[dev_id] = dev
            await dev.start()
        # Hue: one Entertainment stream per bridge; it connects in the background.
        for bridge_id in {l.hue_bridge for l in lights if l.transport == "hue" and l.hue_bridge}:
            bcfg = cfg.hue_bridge(bridge_id)
            if bcfg is not None and bcfg.paired:
                stream = HueEntertainmentStream(bcfg, cfg.sync)
                self._hue[bridge_id] = stream
                stream.start()

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
        if light.transport == "hue":
            stream = self._hue.get(light.hue_bridge)
            if stream is None:
                out = VirtualOutput(light, s, 60)
                bridge = self.config.hue_bridge(light.hue_bridge)
                out.error = "no Hue bridge selected" if bridge is None else "Hue bridge not paired"
                return out
            return HueLightOutput(light, s, stream)
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
        # Hue: close the DTLS session, end the stream and put the lights back.
        # On a restart the area stays active so the lights hold their colour
        # until the new session takes over.
        keep = self._restarting
        await asyncio.gather(
            *(s.stop(deactivate=not keep, restore=restore and not keep) for s in self._hue.values()),
            return_exceptions=True,
        )
        await asyncio.sleep(0.15)  # let final commands leave the socket
        for dev in self._devices.values():
            await dev.stop()
        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None
        self._outputs.clear()
        self._devices.clear()
        self._hue.clear()
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
    def _build_plan(self, aspect: float) -> tuple[
        np.ndarray,
        list[tuple[LightConfig, slice]],
        list[tuple[LightConfig, np.ndarray]],
        list[tuple[LightConfig, np.ndarray]],
    ]:
        """Sample regions, which of them belong to which light, and each light's room angles and positions."""
        plan = []
        regions = []
        angles = []
        positions = []
        offset = 0
        for light in self.config.lights:
            if not light.enabled:
                continue
            rs = light_regions(light, self.config.room, aspect)
            plan.append((light, slice(offset, offset + len(rs))))
            regions.extend(r.as_list() for r in rs)
            angles.append((light, element_angles(light, self.config.room, aspect)))
            positions.append((light, element_positions(light, self.config.room, aspect)))
            offset += len(rs)
        arr = np.array(regions, dtype=np.float32).reshape(-1, 4)
        return arr, plan, angles, positions

    def _capture_main(self, generation: int) -> None:
        def stale() -> bool:
            # stop() only waits 3 s for this thread; it may still be blocked
            # opening a backend when a new one starts.
            return not self._running.is_set() or self._generation != generation

        backend: CaptureBackend | None = None
        processor = ColorProcessor(self.config.sync)
        smoother = Smoother()
        targets: dict[str, np.ndarray] = {}
        regions = np.zeros((0, 4), np.float32)
        plan: list[tuple[LightConfig, slice]] = []
        angles: list[tuple[LightConfig, np.ndarray]] = []
        positions: list[tuple[LightConfig, np.ndarray]] = []
        effects = self.game_effects
        ow_effects = self.overwatch_effects
        # Is a supported game running?  One watcher per game, only while its effects are on.
        watchers: dict[str, ProcessWatcher | None] = {"wow": None, "overwatch": None}
        game_names = {"wow": PROCESS_NAMES, "overwatch": overwatch.PROCESS_NAMES}
        source_size: tuple[int, int] | None = None
        aspect = DEFAULT_ASPECT
        backend_key = None
        last_frame = None
        last_tick = time.perf_counter()
        frame_count = loop_count = 0
        rate_t0 = last_tick
        retry_at = 0.0

        try:
            while not stale():
                s = self.config.sync
                fps = s.target_fps if self.sending else min(s.target_fps, 20)
                interval = 1.0 / max(5, min(fps, 240))
                tick_start = time.perf_counter()

                game = ""
                for name, enabled in (("wow", s.wow_effects), ("overwatch", s.ow_effects)):
                    watcher = watchers[name]
                    if enabled and watcher is None:
                        watcher = watchers[name] = ProcessWatcher(game_names[name])
                        watcher.start()
                    elif not enabled and watcher is not None:
                        watcher.stop()
                        watcher = watchers[name] = None
                    if not game and watcher is not None and watcher.running:
                        game = name

                key = (s.capture_backend, s.monitor, s.hdr, s.sdr_white_nits, s.hdr_peak_nits)
                if backend is not None and key != backend_key:
                    backend.close()
                    backend = None
                if backend is None:
                    # After a failure, wait before retrying - unless the capture
                    # settings changed, which is worth an immediate attempt.
                    if tick_start < retry_at and key == backend_key:
                        time.sleep(0.05)
                        continue
                    backend_key = key
                    try:
                        backend = create_backend(s)
                    except Exception as exc:  # noqa: BLE001
                        if stale():
                            break
                        self.stats.error = f"capture: {exc}"
                        log.warning("capture backend failed: %s", exc)
                        # retry=False: the user said no / a dependency is missing.
                        retry_at = tick_start + 2.0 if getattr(exc, "retry", True) else math.inf
                        continue
                    if stale():
                        break
                    self.stats.backend = backend.name
                    self.stats.error = None

                # A full-resolution piece of the screen, only while a game runs:
                # the WoW addon's signal strip in the corner, or Overwatch's health bar.
                if game == "wow":
                    backend.probe_rect = (0, 0, *PROBE_SIZE)
                elif game == "overwatch" and source_size is not None:
                    backend.probe_rect = overwatch.probe_rect(source_size)
                else:
                    backend.probe_rect = None
                try:
                    frame = backend.grab(timeout_ms=max(1, int(interval * 1000)))
                except CaptureError as exc:
                    self.stats.error = f"capture: {exc}"
                    backend.close()
                    backend = None
                    retry_at = time.perf_counter() + 1.0 if exc.retry else math.inf
                    continue

                fresh = frame is not None
                if frame is None and self._regions_dirty and last_frame is not None:
                    frame = last_frame  # layout edited while the screen is static
                if game != "wow":
                    effects.observe(None, tick_start)
                elif fresh and frame is not None:
                    sig = decode_signal(frame.probe)
                    if sig is not None:
                        hide_strip(frame.image, frame.source_size, sig)  # keep it out of the lights
                    effects.observe(sig, frame.timestamp)
                if game != "overwatch":
                    ow_effects.observe(None, tick_start)
                elif fresh and frame is not None:
                    reading = self.overwatch_hud.read(frame.probe, frame.source_size, frame.timestamp)
                    ow_effects.observe(reading, frame.timestamp)
                if frame is not None:
                    last_frame = frame
                    source_size = frame.source_size
                    frame_count += fresh
                    if abs(frame.aspect - aspect) > 1e-3:
                        aspect = frame.aspect
                        self._regions_dirty = True
                    if self._regions_dirty:
                        self._regions_dirty = False
                        regions, plan, angles, positions = self._build_plan(aspect)
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
                if effects.active:
                    out = effects.apply(out, angles, s, now)
                if ow_effects.active:
                    out = ow_effects.apply(out, positions, s, now)
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
                    self.stats.streams = self.stream_status()
                    if game == "wow":
                        self.stats.game = f"WoW: {effects.status_text()}"
                    elif game == "overwatch":
                        self.stats.game = f"Overwatch: {ow_effects.status_text()}"
                    else:
                        self.stats.game = ""
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
            for watcher in watchers.values():
                if watcher is not None:
                    watcher.stop()
            self.stats.game = ""
            if backend is not None:
                backend.close()
