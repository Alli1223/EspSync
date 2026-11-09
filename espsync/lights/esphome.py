"""Direct ESPHome native-API connection (the protocol Home Assistant uses).

We talk to the ESP over its own TCP API (port 6053, Noise-encrypted) instead of
going through Home Assistant, which removes HA's event loop, service-call
handling and a second network hop from the path.  The ESP accepts several API
clients at once, so Home Assistant stays connected alongside us.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    ColorMode,
    LightColorCapability,
    LightInfo,
    LightState,
    ReconnectLogic,
)

from ..config import DeviceConfig, LightConfig, SyncSettings
from .base import LightOutput

log = logging.getLogger(__name__)

CLIENT_INFO = "EspSync"


class EspHomeDevice:
    """One connection per ESP; shared by all lights on that device."""

    def __init__(self, cfg: DeviceConfig, zeroconf: Any | None = None) -> None:
        self.cfg = cfg
        self.client = APIClient(
            cfg.host,
            cfg.port,
            cfg.password or None,
            client_info=CLIENT_INFO,
            noise_psk=cfg.encryption_key or None,
            zeroconf_instance=zeroconf,
        )
        self.lights: dict[str, LightInfo] = {}
        self.states: dict[int, LightState] = {}
        self.connected = asyncio.Event()
        self.error: str | None = None
        self._reconnect: ReconnectLogic | None = None
        self._zeroconf = zeroconf

    @property
    def is_connected(self) -> bool:
        return self.connected.is_set()

    async def _on_connect(self) -> None:
        entities, _ = await self.client.list_entities_services()
        self.lights = {}
        for e in entities:
            if isinstance(e, LightInfo):
                self.lights[e.object_id or str(e.key)] = e
        self.client.subscribe_states(self._on_state)
        self.error = None
        self.connected.set()
        log.info("Connected to %s (%s): %d light(s)", self.cfg.name, self.cfg.host, len(self.lights))

    async def _on_disconnect(self, expected: bool) -> None:
        self.connected.clear()
        if not expected:
            log.warning("Lost connection to %s", self.cfg.name or self.cfg.host)

    async def _on_connect_error(self, err: Exception) -> None:
        self.error = str(err) or type(err).__name__
        log.warning("Connecting to %s failed: %s", self.cfg.name or self.cfg.host, self.error)

    def _on_state(self, state: Any) -> None:
        if isinstance(state, LightState):
            self.states[state.key] = state

    async def start(self) -> None:
        host = self.cfg.host
        name = host[: -len(".local")] if host.endswith(".local") else None
        self._reconnect = ReconnectLogic(
            client=self.client,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            on_connect_error=self._on_connect_error,
            zeroconf_instance=self._zeroconf,
            name=name,
        )
        await self._reconnect.start()

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def stop(self) -> None:
        if self._reconnect is not None:
            await self._reconnect.stop()
            self._reconnect = None
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.connected.clear()

    def find_light(self, entity: str) -> LightInfo | None:
        if entity in self.lights:
            return self.lights[entity]
        for info in self.lights.values():
            if entity in (str(info.key), info.name):
                return info
        return None


def pick_rgb_mode(info: LightInfo) -> int | None:
    """Best colour mode with RGB support, preferring plain RGB."""
    modes = [int(m) for m in info.supported_color_modes]
    for preferred in (ColorMode.RGB, ColorMode.RGB_WHITE, ColorMode.RGB_COLOR_TEMPERATURE,
                      ColorMode.RGB_COLD_WARM_WHITE):
        if int(preferred) in modes:
            return int(preferred)
    rgb = [m for m in modes if m & LightColorCapability.RGB]
    return rgb[0] if rgb else None


def split_brightness(rgb: np.ndarray, min_brightness: float) -> tuple[float, tuple[float, float, float]]:
    """ESPHome normalises RGB so the max channel is 1 and applies brightness
    separately.  Return (brightness, normalised rgb)."""
    v = float(rgb.max())
    if v <= 1e-4:
        return max(min_brightness, 0.0), (1.0, 1.0, 1.0)
    norm = tuple(float(c) / v for c in rgb)
    return max(v, min_brightness), norm  # type: ignore[return-value]


class ApiLightOutput(LightOutput):
    """Single-colour light driven by `light_command` over the native API."""

    def __init__(self, cfg: LightConfig, settings: SyncSettings, device: EspHomeDevice) -> None:
        super().__init__(cfg, settings, settings.api_rate_hz)
        self.device = device
        self.info: LightInfo | None = None
        self.mode: int | None = None
        self._saved: LightState | None = None
        self._is_off = False
        self._last_hue = (1.0, 1.0, 1.0)

    async def start(self) -> None:
        self.status = "connecting"
        if not await self.device.wait_connected(10.0):
            self.status = "error"
            self.error = self.device.error or "device not reachable"
            return
        info = self.device.find_light(self.cfg.entity)
        if info is None:
            self.status = "error"
            self.error = f"light '{self.cfg.entity}' not found on {self.device.cfg.name}"
            return
        self.info = info
        self.mode = pick_rgb_mode(info)
        self._saved = self.device.states.get(info.key)
        self.status = "ready"

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.device.is_connected

    def encode(self, colors: np.ndarray) -> bytes:
        rgb = np.clip(colors[0], 0.0, 1.0)
        return (rgb * 255.0 + 0.5).astype(np.uint8).tobytes()

    def transmit(self, colors: np.ndarray, payload: bytes) -> None:
        assert self.info is not None
        s = self.settings
        rgb = np.frombuffer(payload, dtype=np.uint8).astype(np.float32) / 255.0
        transition = s.transition_ms / 1000.0
        if rgb.max() < max(s.min_brightness, 1 / 255) and s.black_turns_off:
            if not self._is_off:
                self.device.client.light_command(
                    self.info.key, state=False, transition_length=transition,
                    device_id=self.info.device_id,
                )
                self._is_off = True
            return
        brightness, hue = split_brightness(rgb, s.min_brightness)
        if rgb.max() > 1e-3:
            self._last_hue = hue
        else:
            hue = self._last_hue  # keep the last colour when dimming to the floor
        kwargs: dict[str, Any] = {
            "state": True,
            "brightness": brightness,
            "transition_length": transition,
            "device_id": self.info.device_id,
        }
        if self.mode is not None:
            kwargs["color_mode"] = self.mode
            kwargs["rgb"] = hue
            kwargs["color_brightness"] = 1.0
            if self.mode & LightColorCapability.WHITE:
                kwargs["white"] = 0.0
            if self.mode & LightColorCapability.COLD_WARM_WHITE:
                kwargs["cold_white"] = 0.0
                kwargs["warm_white"] = 0.0
        self.device.client.light_command(self.info.key, **kwargs)
        self._is_off = False

    async def stop(self, restore: bool) -> None:
        await super().stop(restore)
        if restore and self.info is not None and self._saved is not None and self.device.is_connected:
            try:
                restore_light(self.device.client, self.info, self._saved)
            except APIConnectionError:
                pass


def restore_light(client: APIClient, info: LightInfo, st: LightState, transition: float = 0.4) -> None:
    if not st.state:
        client.light_command(info.key, state=False, transition_length=transition, device_id=info.device_id)
        return
    kwargs: dict[str, Any] = {
        "state": True,
        "brightness": st.brightness,
        "transition_length": transition,
        "device_id": info.device_id,
    }
    mode = int(st.color_mode)
    if mode:
        kwargs["color_mode"] = mode
        if mode & LightColorCapability.RGB:
            kwargs["rgb"] = (st.red, st.green, st.blue)
            kwargs["color_brightness"] = st.color_brightness
        if mode & LightColorCapability.WHITE:
            kwargs["white"] = st.white
        if mode & LightColorCapability.COLOR_TEMPERATURE:
            kwargs["color_temperature"] = st.color_temperature
        if mode & LightColorCapability.COLD_WARM_WHITE:
            kwargs["cold_white"] = st.cold_white
            kwargs["warm_white"] = st.warm_white
    if info.effects:
        kwargs["effect"] = st.effect or "None"
    client.light_command(info.key, **kwargs)


class EffectController:
    """Switches a strip's UDP effect (WLED / E1.31) on for the session and back after."""

    def __init__(self, device: EspHomeDevice, entity: str, effect: str) -> None:
        self.device = device
        self.entity = entity
        self.effect = effect
        self.info: LightInfo | None = None
        self._saved: LightState | None = None

    async def start(self) -> str | None:
        if not await self.device.wait_connected(10.0):
            return self.device.error or "device not reachable"
        info = self.device.find_light(self.entity)
        if info is None:
            return f"light '{self.entity}' not found"
        match = next((e for e in info.effects if e.lower() == self.effect.lower()), None)
        if match is None:
            return f"effect '{self.effect}' not configured (available: {', '.join(info.effects) or 'none'})"
        self.info = info
        self._saved = self.device.states.get(info.key)
        self.device.client.light_command(
            info.key, state=True, brightness=1.0, effect=match, transition_length=0.0,
            device_id=info.device_id,
        )
        return None

    async def stop(self, restore: bool) -> None:
        if self.info is None or not self.device.is_connected:
            return
        if restore and self._saved is not None:
            restore_light(self.device.client, self.info, self._saved)
        else:
            self.device.client.light_command(self.info.key, effect="None", device_id=self.info.device_id)


async def identify_light(cfg: DeviceConfig, entity: str, timeout: float = 8.0) -> None:
    """Flash a light so the user can tell which one it is."""
    client = APIClient(
        cfg.host, cfg.port, cfg.password or None, client_info=CLIENT_INFO,
        noise_psk=cfg.encryption_key or None,
    )
    await asyncio.wait_for(client.connect(login=True), timeout)
    try:
        entities, _ = await client.list_entities_services()
        info = next(
            (e for e in entities if isinstance(e, LightInfo) and entity in (e.object_id, str(e.key), e.name)),
            None,
        )
        if info is None:
            raise LookupError(f"light '{entity}' not found")
        client.light_command(info.key, state=True, flash_length=1.5, brightness=1.0, device_id=info.device_id)
        await asyncio.sleep(0.2)
    finally:
        await client.disconnect()


async def probe_device(cfg: DeviceConfig, timeout: float = 8.0) -> tuple[str, list[LightInfo]]:
    """Connect once and list the device's lights (used by the 'add device' UI)."""
    client = APIClient(
        cfg.host, cfg.port, cfg.password or None, client_info=CLIENT_INFO,
        noise_psk=cfg.encryption_key or None,
    )
    await asyncio.wait_for(client.connect(login=True), timeout)
    try:
        info = await client.device_info()
        entities, _ = await client.list_entities_services()
        return info.friendly_name or info.name, [e for e in entities if isinstance(e, LightInfo)]
    finally:
        await client.disconnect()
