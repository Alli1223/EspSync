"""Preset scenes: put every light back to a plain white (or off) with one click.

Sync leaves the room in whatever colour the screen last had; a scene sets all
synced lights to a steady colour instead.  It is a one-off command per light,
sent while sync is stopped:

* ESPHome lights get a ``light_command`` over the native API.  Lights with
  white channels use them (colour temperature, cold/warm white or plain
  white); RGB-only lights get the nearest RGB colour.  A running effect
  (including the WLED / E1.31 effect of a strip) is switched off.
* Philips Hue lights get a CLIP v2 REST command with the colour temperature.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig, DeviceConfig, HueBridgeConfig

log = logging.getLogger(__name__)

FADE_S = 0.6
CONNECT_TIMEOUT_S = 6.0
HUE_MIREK = (153, 500)  # what Hue lights accept


@dataclass(frozen=True)
class Scene:
    id: str
    name: str
    kelvin: int = 2700
    brightness: float = 1.0  # 0..1
    on: bool = True
    hint: str = ""

    @property
    def mireds(self) -> float:
        return 1_000_000.0 / self.kelvin

    @property
    def rgb(self) -> tuple[float, float, float]:
        """The colour as gamma-encoded RGB with the largest channel at 1."""
        return kelvin_to_rgb(self.kelvin)


SCENES: tuple[Scene, ...] = (
    Scene("warm", "Warm white", 2700, 1.0, hint="Everyday warm white, full brightness"),
    Scene("relax", "Relax", 2200, 0.55, hint="Dimmed, extra warm"),
    Scene("neutral", "Neutral", 4000, 1.0, hint="Neutral white, full brightness"),
    Scene("daylight", "Daylight", 5500, 1.0, hint="Cool daylight white for working"),
    Scene("night", "Night light", 2000, 0.1, hint="Very dim amber"),
    Scene("off", "Off", on=False, hint="Turn the lights off"),
)


def scene_by_id(scene_id: str) -> Scene | None:
    return next((s for s in SCENES if s.id == scene_id), None)


def kelvin_to_rgb(kelvin: float) -> tuple[float, float, float]:
    """Approximate sRGB colour of a black body (Tanner Helland's fit), max channel = 1."""
    t = min(max(kelvin, 1000.0), 40000.0) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    rgb = [min(max(c, 0.0), 255.0) / 255.0 for c in (r, g, b)]
    top = max(rgb) or 1.0
    return rgb[0] / top, rgb[1] / top, rgb[2] / top


def kelvin_to_xy(kelvin: float) -> tuple[float, float]:
    """CIE xy chromaticity of that colour (for Hue lights without colour temperature)."""
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in kelvin_to_rgb(kelvin)]
    x = 0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]
    y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    z = 0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]
    total = (x + y + z) or 1.0
    return round(x / total, 4), round(y / total, 4)


@dataclass
class SceneResult:
    applied: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self, scene: Scene) -> str:
        if not self.applied and not self.errors:
            return "No lights to set: add an ESPHome or Hue light first"
        text = f"{scene.name}: set {self.applied} light{'s' if self.applied != 1 else ''}"
        if self.errors:
            text += " · " + " · ".join(self.errors[:3])
        return text


# -------------------------------------------------------------------- ESPHome
# aioesphomeapi ColorMode values, best first for a white scene.
_CT, _CWWW, _RGB_CT, _RGB_CWWW, _RGBW, _WHITE, _RGB = 11, 19, 47, 51, 39, 7, 35
_MODE_ORDER = (_CT, _CWWW, _RGB_CT, _RGB_CWWW, _RGBW, _WHITE, _RGB)


def _cold_warm(mireds: float, lo: float, hi: float) -> tuple[float, float]:
    """(cold, warm) channel levels for a colour temperature, the brighter one at 1."""
    if not hi > lo > 0:
        lo, hi = 153.0, 500.0
    warm = min(max((mireds - lo) / (hi - lo), 0.0), 1.0)
    cold = 1.0 - warm
    top = max(cold, warm)
    return cold / top, warm / top


def esphome_command(info: Any, scene: Scene) -> dict[str, Any]:
    """``light_command`` arguments that put the light `info` (a LightInfo) into `scene`."""
    kwargs: dict[str, Any] = {"transition_length": FADE_S, "device_id": getattr(info, "device_id", 0)}
    if not scene.on:
        kwargs["state"] = False
        return kwargs
    kwargs["state"] = True
    kwargs["brightness"] = max(scene.brightness, 0.01)
    if getattr(info, "effects", None):
        kwargs["effect"] = "None"
    modes = [int(m) for m in getattr(info, "supported_color_modes", [])]
    mode = next((m for m in _MODE_ORDER if m in modes), None)
    if mode is None:
        return kwargs  # brightness-only or on/off light
    lo, hi = float(getattr(info, "min_mireds", 0) or 0), float(getattr(info, "max_mireds", 0) or 0)
    mireds = min(max(scene.mireds, lo), hi) if hi > lo > 0 else scene.mireds
    kwargs["color_mode"] = mode
    if mode in (_CT, _RGB_CT):
        kwargs["color_temperature"] = mireds
    if mode in (_CWWW, _RGB_CWWW):
        kwargs["cold_white"], kwargs["warm_white"] = _cold_warm(mireds, lo, hi)
    if mode in (_RGB_CT, _RGBW, _WHITE):
        kwargs["white"] = 1.0
    if mode in (_RGB_CT, _RGB_CWWW, _RGBW):
        kwargs["color_brightness"] = 0.0  # white channels only
    if mode == _RGB:
        kwargs["rgb"] = scene.rgb
        kwargs["color_brightness"] = 1.0
    return kwargs


async def _apply_esphome(dev: DeviceConfig, entities: set[str], scene: Scene) -> tuple[int, list[str]]:
    from aioesphomeapi import APIClient, LightInfo

    from .lights.esphome import CLIENT_INFO, friendly_error

    name = dev.name or dev.host
    client = APIClient(dev.host, dev.port, dev.password or None, client_info=CLIENT_INFO,
                       noise_psk=dev.encryption_key or None)
    try:
        await asyncio.wait_for(client.connect(login=True), CONNECT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        return 0, [f"{name}: {friendly_error(exc)}"]
    done, errors = 0, []
    try:
        found, _ = await client.list_entities_services()
        lights = [e for e in found if isinstance(e, LightInfo)]
        for entity in sorted(entities):
            info = next((e for e in lights if entity in (e.object_id, str(e.key), e.name)), None)
            if info is None:
                errors.append(f"{name}: light '{entity}' not found")
                continue
            client.light_command(info.key, **esphome_command(info, scene))
            done += 1
        await asyncio.sleep(0.25)  # let the commands leave the socket
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{name}: {friendly_error(exc)}")
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return done, errors


# ------------------------------------------------------------------------ Hue
def hue_body(scene: Scene, colour_temperature: bool = True) -> dict[str, Any]:
    """CLIP v2 light PUT body for `scene`."""
    body: dict[str, Any] = {"on": {"on": scene.on}, "dynamics": {"duration": round(FADE_S * 1000)}}
    if not scene.on:
        return body
    body["dimming"] = {"brightness": round(max(scene.brightness * 100.0, 1.0), 1)}
    if colour_temperature:
        body["color_temperature"] = {"mirek": int(min(max(round(scene.mireds), HUE_MIREK[0]), HUE_MIREK[1]))}
    else:
        x, y = kelvin_to_xy(scene.kelvin)
        body["color"] = {"xy": {"x": x, "y": y}}
    return body


async def _apply_hue(bridge: HueBridgeConfig, channels: set[int], scene: Scene) -> tuple[int, list[str]]:
    from .hue import HueApi, HueError

    name = bridge.name or bridge.host
    if not bridge.paired or not bridge.entertainment_id:
        return 0, [f"Hue {name}: not paired"]
    api = HueApi(bridge.host, bridge.app_key)
    try:
        area = await api.area(bridge.entertainment_id, with_names=True)
    except HueError as exc:
        return 0, [f"Hue {name}: {exc}"]
    ids = sorted({c.light_id for c in area.channels if c.channel_id in channels and c.light_id})
    done, errors = 0, []
    for light_id in ids:
        try:
            try:
                await api.put_light(light_id, hue_body(scene))
            except HueError:
                if not scene.on:
                    raise
                await api.put_light(light_id, hue_body(scene, colour_temperature=False))  # colour-only light
            done += 1
        except HueError as exc:
            errors.append(f"Hue {name}: {exc}")
            break  # the same error would repeat for every light
        await asyncio.sleep(0.1)  # the bridge takes ~10 commands a second
    return done, errors


# ----------------------------------------------------------------------- both
def scene_targets(config: AppConfig) -> tuple[dict[str, set[str]], dict[str, set[int]], int]:
    """(ESPHome device id -> entities, Hue bridge id -> channels, lights that can't be set)."""
    esphome: dict[str, set[str]] = {}
    hue: dict[str, set[int]] = {}
    unreachable = 0
    for light in config.lights:
        if not light.enabled or light.transport == "virtual":
            continue
        if light.transport == "hue":
            if light.hue_bridge and light.hue_channels:
                hue.setdefault(light.hue_bridge, set()).update(light.hue_channels)
            else:
                unreachable += 1
        elif light.device_id and light.entity:
            esphome.setdefault(light.device_id, set()).add(light.entity)
        else:
            unreachable += 1  # a UDP strip with only an address: no API to set a colour with
    return esphome, hue, unreachable


async def apply_scene(config: AppConfig, scene: Scene) -> SceneResult:
    """Set every enabled light to `scene`.  Call it while sync is stopped."""
    esphome, hue, unreachable = scene_targets(config)
    jobs = []
    for device_id, entities in esphome.items():
        dev = config.device(device_id)
        if dev is not None:
            jobs.append(_apply_esphome(dev, entities, scene))
    for bridge_id, channels in hue.items():
        bridge = config.hue_bridge(bridge_id)
        if bridge is not None:
            jobs.append(_apply_hue(bridge, channels, scene))
    result = SceneResult()
    for outcome in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(outcome, BaseException):
            result.errors.append(str(outcome) or type(outcome).__name__)
            continue
        done, errors = outcome
        result.applied += done
        result.errors.extend(errors)
    if unreachable:
        result.errors.append(f"{unreachable} light(s) have no device to send a scene to")
    log.info("scene %s: %d light(s) set, %d problem(s)", scene.id, result.applied, len(result.errors))
    return result
