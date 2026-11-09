"""Configuration model and JSON persistence.

Coordinate conventions used throughout EspSync
-----------------------------------------------
Room (top-down, viewer facing the screen):
    x  -1 (left wall) .. +1 (right wall)
    y  -1 (screen wall) .. +1 (back wall)
    z   0 (floor) .. 1 (ceiling); 0.5 is roughly the screen's centre height

Screen (normalised image coordinates):
    u   0 (left) .. 1 (right)
    v   0 (top)  .. 1 (bottom)
"""

from __future__ import annotations

import dataclasses
import json
import os
import typing
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_config_dir

APP_NAME = "EspSync"

Transport = Literal["api", "wled", "e131", "virtual"]
LightKind = Literal["lamp", "strip"]
StripLayout = Literal["room", "screen"]


def new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class DeviceConfig:
    """An ESPHome node reachable over the native API (TCP 6053)."""

    id: str = field(default_factory=new_id)
    name: str = ""
    host: str = ""
    port: int = 6053
    # Base64 noise PSK from the device's `api: encryption: key:`. Empty = plaintext.
    encryption_key: str = ""
    # Legacy API password (deprecated in ESPHome, still supported).
    password: str = ""


@dataclass
class LightConfig:
    id: str = field(default_factory=new_id)
    name: str = "Light"
    enabled: bool = True
    kind: LightKind = "lamp"

    # --- how colours are delivered -------------------------------------------
    # api     : ESPHome native API light_command (any ESPHome light, one colour)
    # wled    : WLED realtime UDP (ESPHome `wled` light effect), per-LED
    # e131    : sACN/E1.31 UDP (ESPHome `e131` light effect), per-LED
    # virtual : nothing is sent; preview only (handy for testing layouts)
    transport: Transport = "api"
    device_id: str = ""  # DeviceConfig.id for transport=api (and effect control for UDP)
    entity: str = ""  # ESPHome light object_id on that device
    udp_host: str = ""  # defaults to the device host when empty
    udp_port: int = 0  # 0 = protocol default (wled 21324, e131 5568)
    universe: int = 1  # E1.31 start universe
    # Name of the light effect to switch on for UDP transports ("WLED", "E1.31").
    # Empty = don't touch the effect (configure it yourself in HA).
    udp_effect: str = ""

    # --- placement in the room -------------------------------------------------
    x: float = 0.0
    y: float = -0.8
    z: float = 0.5
    # Strip end point in the room (kind=strip, layout=room)
    x2: float = 0.3
    y2: float = -0.8
    z2: float = 0.5

    # --- screen sampling -------------------------------------------------------
    # Fraction of the screen width the sample region covers (lamps), or the
    # sample depth / size per LED for strips.
    region_size: float = 0.3
    # Manual override [u0, v0, u1, v1]; None = derived from the room position.
    region: list[float] | None = None

    # Strip settings
    led_count: int = 30
    strip_layout: StripLayout = "room"
    # Polyline in screen coordinates [[u, v], ...] used when strip_layout=screen.
    screen_path: list[list[float]] = field(default_factory=lambda: [[0.0, 1.0], [1.0, 1.0]])
    reverse: bool = False

    # --- per-light colour tweaks ----------------------------------------------
    brightness: float = 1.0
    gains: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])


@dataclass
class RoomConfig:
    width_m: float = 4.0
    depth_m: float = 4.0
    # Fraction of the room width the TV/monitor occupies (drawing + strip presets).
    tv_width: float = 0.35
    # How much of the room width maps onto the screen width. 1.0 = left wall is
    # the left screen edge. Smaller values push side lights to the screen edges.
    horizontal_spread: float = 1.0


@dataclass
class SyncSettings:
    monitor: int = 0  # 0-based display index
    capture_backend: Literal["auto", "dxgi", "portal", "mss"] = "auto"
    # Wayland: token from the screen-cast portal that re-opens the screen picked
    # last time without showing the share dialog again.  Single use; the portal
    # backend replaces it on every start and the GUI/CLI save it.
    portal_restore_token: str = ""
    target_fps: int = 60
    # Raise GPU scheduling priority (Windows) so capture isn't stuck behind a
    # game's queued frames.
    gpu_priority: bool = True

    # Colour processing
    mode: Literal["average", "vivid"] = "vivid"
    saturation: float = 1.15  # 1.0 = unchanged
    brightness: float = 1.0
    min_brightness: float = 0.02  # floor while the screen is dark (0 = allow off)
    black_turns_off: bool = False
    smoothing_ms: float = 80.0  # EMA time constant; 0 = instant
    letterbox_detection: bool = True

    # HDR (Windows DXGI backend only)
    hdr: Literal["auto", "on", "off"] = "auto"
    sdr_white_nits: float = 0.0  # 0 = read Windows' "SDR content brightness"
    hdr_peak_nits: float = 0.0  # 0 = use the display's reported peak
    hdr_saturation: float = 1.1

    # Output
    api_rate_hz: float = 30.0  # max commands/second per native-API light
    udp_rate_hz: float = 60.0
    transition_ms: float = 0.0  # ESPHome transition per update (0 = immediate)
    restore_on_stop: bool = True

    # Game effects (World of Warcraft + the EspSync addon, integrations/wow).
    # Active only while the game runs and its signal strip is on screen.
    wow_effects: bool = True
    wow_low_health: float = 0.35  # red pulse below this share of health


@dataclass
class UpdateConfig:
    check_automatically: bool = True  # look at GitHub Releases on start and every few hours
    install_automatically: bool = True  # installed app: update without asking (never mid-sync)
    skipped_version: str = ""  # "Skip this version" in the update prompt


@dataclass
class AppConfig:
    devices: list[DeviceConfig] = field(default_factory=list)
    lights: list[LightConfig] = field(default_factory=list)
    room: RoomConfig = field(default_factory=RoomConfig)
    sync: SyncSettings = field(default_factory=SyncSettings)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    version: int = 1

    # -- lookups ---------------------------------------------------------------
    def device(self, device_id: str) -> DeviceConfig | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def light(self, light_id: str) -> LightConfig | None:
        return next((l for l in self.lights if l.id == light_id), None)

    # -- (de)serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        return _from_dict(cls, data)


def _from_dict(cls: type, data: Any) -> Any:
    """Build dataclass `cls` from a dict, ignoring unknown keys (forward compat)."""
    if not isinstance(data, dict):
        return cls()
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        hint = hints[f.name]
        origin = typing.get_origin(hint)
        if dataclasses.is_dataclass(hint):
            value = _from_dict(hint, value)
        elif origin is list:
            (item_t,) = typing.get_args(hint) or (Any,)
            if dataclasses.is_dataclass(item_t) and isinstance(value, list):
                value = [_from_dict(item_t, v) for v in value]
        kwargs[f.name] = value
    return cls(**kwargs)


def default_config_path() -> Path:
    override = os.environ.get("ESPSYNC_CONFIG")
    if override:
        return Path(override)
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    if not path.exists():
        return AppConfig()
    with path.open("r", encoding="utf-8") as fh:
        return AppConfig.from_dict(json.load(fh))


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(config.to_dict(), fh, indent=2)
    os.replace(tmp, path)
    return path
