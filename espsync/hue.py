"""Philips Hue bridge: discovery, pairing and entertainment areas (CLIP v2 REST).

Only setup and start/stop go over REST (the bridge allows ~10 REST commands a
second).  Colours are streamed with the Entertainment API, see
``espsync.lights.hue``.

Hue entertainment positions use x, y, z in -1..1 with the user facing the TV:
x -1 left .. +1 right, y -1 back of the room .. +1 front (the TV), z -1 floor ..
+1 ceiling.  EspSync's room has y -1 at the screen wall, and z 0 floor .. 1
ceiling, so y flips and z rescales (`hue_to_room`).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import HueBridgeConfig, LightConfig, RoomConfig
from .mapping import room_to_screen

log = logging.getLogger(__name__)

HUE_SERVICE = "_hue._tcp.local."
STREAM_PORT = 2100
REST_TIMEOUT = 6.0


class HueError(Exception):
    """A REST call failed; the message is meant for the user."""


class LinkButtonNotPressed(HueError):
    def __init__(self) -> None:
        super().__init__("press the round link button on the bridge first")


class HueAuthError(HueError):
    def __init__(self) -> None:
        super().__init__("the bridge no longer accepts this app key: pair it again")


# ------------------------------------------------------------------------ HTTP
def _ssl_context() -> ssl.SSLContext:
    # Bridges present a certificate from Signify's private CA (older firmware:
    # self-signed) for their bridge id, not a host name we could verify; like
    # the official apps on a LAN, don't validate it.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL = _ssl_context()


def _request(host: str, method: str, path: str, body: Any = None, key: str = "",
             timeout: float = REST_TIMEOUT) -> tuple[int, dict[str, str], Any]:
    conn = http.client.HTTPSConnection(host, 443, context=_SSL, timeout=timeout)
    headers = {"Accept": "application/json"}
    if key:
        headers["hue-application-key"] = key
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
    except (OSError, http.client.HTTPException) as exc:
        raise HueError(f"could not reach the bridge at {host} ({exc})") from exc
    finally:
        conn.close()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    return resp.status, resp_headers, payload


async def request(host: str, method: str, path: str, body: Any = None, key: str = "",
                  timeout: float = REST_TIMEOUT) -> tuple[int, dict[str, str], Any]:
    return await asyncio.to_thread(_request, host, method, path, body, key, timeout)


def _v2_errors(status: int, payload: Any) -> str:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    texts = [e.get("description", "") for e in errors or [] if isinstance(e, dict)]
    return "; ".join(t for t in texts if t) or f"HTTP {status}"


# ------------------------------------------------------------------- discovery
@dataclass
class DiscoveredBridge:
    name: str
    host: str  # IPv4 address
    bridge_id: str = ""
    model: str = ""


async def discover_bridges(timeout: float = 3.0) -> list[DiscoveredBridge]:
    """Find Hue bridges with mDNS (``_hue._tcp``)."""
    from .discovery import browse

    found: dict[str, DiscoveredBridge] = {}
    for name, info in await browse(HUE_SERVICE, timeout):
        props = info.props
        bid = props.get("bridgeid", "").lower()
        v4 = [a for a in info.addresses if ":" not in a]
        if not v4:
            continue
        key = bid or v4[0]
        found[key] = DiscoveredBridge(
            name=name.removesuffix("." + HUE_SERVICE), host=v4[0], bridge_id=bid, model=props.get("modelid", "")
        )
    return sorted(found.values(), key=lambda b: b.name.lower())


async def bridge_info(host: str) -> dict[str, Any]:
    """Unauthenticated bridge description: name, bridgeid, modelid, swversion, apiversion."""
    status, _, payload = await request(host, "GET", "/api/0/config")
    if status != 200 or not isinstance(payload, dict) or "bridgeid" not in payload:
        raise HueError(f"{host} doesn't look like a Hue bridge")
    return payload


# --------------------------------------------------------------------- pairing
def devicetype() -> str:
    """'<app>#<device>' as the bridge wants it (app <= 20, device <= 19 chars)."""
    pc = (socket.gethostname() or "pc").split(".")[0][:19]
    return f"espsync#{pc}"


async def pair_once(host: str) -> tuple[str, str]:
    """One pairing attempt -> (application key, client key)."""
    status, _, payload = await request(
        host, "POST", "/api", {"devicetype": devicetype(), "generateclientkey": True}
    )
    item = payload[0] if isinstance(payload, list) and payload else {}
    if "success" in item:
        ok = item["success"]
        if not ok.get("clientkey"):
            raise HueError("the bridge returned no client key (firmware too old for Entertainment?)")
        return ok["username"], ok["clientkey"]
    err = item.get("error", {}) if isinstance(item, dict) else {}
    if err.get("type") == 101:
        raise LinkButtonNotPressed()
    raise HueError(err.get("description") or f"pairing failed (HTTP {status})")


async def pair(
    host: str,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
    on_wait: Callable[[float], None] | None = None,
) -> tuple[str, str]:
    """Poll until the link button is pressed (or `timeout` s pass)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await pair_once(host)
        except LinkButtonNotPressed:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HueError("timed out: the link button wasn't pressed") from None
            if cancel is not None and cancel.is_set():
                raise HueError("pairing cancelled") from None
            if on_wait is not None:
                on_wait(left)
            await asyncio.sleep(1.0)


# --------------------------------------------------------- entertainment areas
@dataclass
class HueChannel:
    channel_id: int
    x: float  # Hue coordinates, see the module docstring
    y: float
    z: float
    name: str = ""  # light (device) name
    service: str = ""  # entertainment service id; channels sharing one are segments of one light
    index: int = 0  # the light's own segment index (a 7-segment strip may use 0, 1, 4 in an area)
    segments: int = 1  # channels that light has in this area
    ordinal: int = 0  # 0-based position among those channels, by segment index
    light_id: str = ""  # CLIP v2 light resource (for identify)

    @property
    def label(self) -> str:
        if self.segments > 1:
            return f"{self.name} ({self.ordinal + 1}/{self.segments})"
        return self.name or f"Channel {self.channel_id}"


@dataclass
class HueArea:
    id: str
    name: str
    kind: str = ""  # screen, monitor, music, 3dspace, other
    status: str = "inactive"
    streamer: str = ""  # application id of the app streaming, when active
    channels: list[HueChannel] = field(default_factory=list)
    light_ids: list[str] = field(default_factory=list)  # CLIP v2 lights in the area


def parse_area(raw: dict[str, Any], names: dict[str, tuple[str, str]] | None = None) -> HueArea:
    """Build a HueArea from a CLIP v2 entertainment_configuration object.

    `names` maps entertainment service id -> (light name, light resource id).
    """
    names = names or {}
    channels = []
    for ch in raw.get("channels", []):
        pos = ch.get("position") or {}
        members = ch.get("members") or []
        svc = members[0].get("service", {}).get("rid", "") if members else ""
        idx = int(members[0].get("index", 0)) if members else 0
        channels.append(HueChannel(
            channel_id=int(ch.get("channel_id", 0)),
            x=float(pos.get("x", 0.0)), y=float(pos.get("y", 0.0)), z=float(pos.get("z", 0.0)),
            name=names.get(svc, ("", ""))[0], service=svc, index=idx, light_id=names.get(svc, ("", ""))[1],
        ))
    per_service: dict[str, list[HueChannel]] = {}
    for c in channels:
        if c.service:
            per_service.setdefault(c.service, []).append(c)
    for group in per_service.values():
        group.sort(key=lambda c: (c.index, c.channel_id))
        for i, c in enumerate(group):
            c.segments, c.ordinal = len(group), i
    streamer = raw.get("active_streamer") or {}
    return HueArea(
        id=raw.get("id", ""),
        name=(raw.get("metadata") or {}).get("name", "") or raw.get("name", ""),
        kind=raw.get("configuration_type", ""),
        status=raw.get("status", "inactive"),
        streamer=streamer.get("rid", "") if isinstance(streamer, dict) else "",
        channels=sorted(channels, key=lambda c: c.channel_id),
        light_ids=[l.get("rid", "") for l in raw.get("light_services") or [] if l.get("rid")],
    )


def restore_payload(light: dict[str, Any], fade_ms: int = 400) -> dict[str, Any]:
    """PUT body that puts a light back the way a CLIP v2 GET described it."""
    on = bool((light.get("on") or {}).get("on"))
    body: dict[str, Any] = {"on": {"on": on}, "dynamics": {"duration": fade_ms}}
    if not on:
        return body
    if "brightness" in (light.get("dimming") or {}):
        body["dimming"] = {"brightness": light["dimming"]["brightness"]}
    points = (light.get("gradient") or {}).get("points") or []
    ct = light.get("color_temperature") or {}
    xy = (light.get("color") or {}).get("xy")
    if points:
        body["gradient"] = {"points": [{"color": {"xy": p["color"]["xy"]}} for p in points if "color" in p]}
    elif ct.get("mirek_valid") and ct.get("mirek"):
        body["color_temperature"] = {"mirek": ct["mirek"]}
    elif xy:
        body["color"] = {"xy": {"x": xy["x"], "y": xy["y"]}}
    effect = (light.get("effects") or {}).get("status")
    if effect and effect != "no_effect":
        body["effects"] = {"effect": effect}
    return body


class HueApi:
    """Authenticated CLIP v2 calls for one bridge."""

    def __init__(self, host: str, app_key: str) -> None:
        self.host = host
        self.app_key = app_key
        self._app_id: str | None = None

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        status, headers, payload = await request(self.host, method, path, body, self.app_key)
        if status in (401, 403):
            raise HueAuthError()
        if status == 404:
            raise HueError(f"not found on the bridge: {path}")
        if status >= 300 or (isinstance(payload, dict) and payload.get("errors")):
            raise HueError(_v2_errors(status, payload))
        return payload

    async def get(self, resource: str) -> list[dict[str, Any]]:
        payload = await self._call("GET", f"/clip/v2/resource/{resource}")
        return payload.get("data", []) if isinstance(payload, dict) else []

    async def application_id(self) -> str:
        """The id the bridge shows as `active_streamer` while we stream."""
        if self._app_id is None:
            status, headers, _ = await request(self.host, "GET", "/auth/v1", key=self.app_key)
            if status in (401, 403):
                raise HueAuthError()
            self._app_id = headers.get("hue-application-id", "")
        return self._app_id

    async def light_names(self) -> dict[str, tuple[str, str]]:
        """entertainment service id -> (name of its light/device, light resource id)."""
        ents, devices = await asyncio.gather(self.get("entertainment"), self.get("device"))
        dev_names = {d.get("id", ""): (d.get("metadata") or {}).get("name", "") for d in devices}
        return {
            e.get("id", ""): (
                dev_names.get((e.get("owner") or {}).get("rid", ""), ""),
                (e.get("renderer_reference") or {}).get("rid", ""),
            )
            for e in ents
        }

    async def identify(self, light_id: str) -> None:
        """Make a light blink (ignored by lights in a running entertainment stream)."""
        await self._call("PUT", f"/clip/v2/resource/light/{light_id}", {"identify": {"action": "identify"}})

    async def areas(self, with_names: bool = True) -> list[HueArea]:
        if with_names:
            raw, names = await asyncio.gather(self.get("entertainment_configuration"), self.light_names())
        else:
            raw, names = await self.get("entertainment_configuration"), None
        return sorted((parse_area(a, names) for a in raw), key=lambda a: a.name.lower())

    async def area(self, area_id: str, with_names: bool = False) -> HueArea:
        raw = await self.get(f"entertainment_configuration/{area_id}")
        if not raw:
            raise HueError("entertainment area not found (deleted in the Hue app?)")
        return parse_area(raw[0], await self.light_names() if with_names else None)

    async def light_states(self, light_ids: list[str]) -> dict[str, dict[str, Any]]:
        wanted = set(light_ids)
        return {l["id"]: l for l in await self.get("light") if l.get("id") in wanted}

    async def put_light(self, light_id: str, body: dict[str, Any]) -> None:
        await self._call("PUT", f"/clip/v2/resource/light/{light_id}", body)

    async def set_streaming(self, area_id: str, active: bool) -> None:
        await self._call(
            "PUT", f"/clip/v2/resource/entertainment_configuration/{area_id}",
            {"action": "start" if active else "stop"},
        )


# -------------------------------------------------------------------- mapping
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def hue_to_room(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Hue entertainment position -> EspSync room position."""
    return _clamp(x, -1.0, 1.0), _clamp(-y, -1.0, 1.0), _clamp((z + 1.0) / 2.0, 0.0, 1.0)


def room_to_hue(x: float, y: float, z: float) -> tuple[float, float, float]:
    return x, -y, z * 2.0 - 1.0


def lights_for_area(
    bridge: HueBridgeConfig,
    area: HueArea,
    room: RoomConfig | None = None,
    group_gradients: bool = False,
) -> list[LightConfig]:
    """LightConfigs for an area's channels, placed where Hue has them.

    Every channel becomes a lamp, so each segment of a gradient lightstrip gets
    its own spot in the room.  With `group_gradients`, a multi-channel light
    becomes one strip instead, sampled along the screen path through its
    channels (LED i -> channel i).
    """
    room = room or RoomConfig()
    out: list[LightConfig] = []
    grouped: dict[str, list[HueChannel]] = {}
    for ch in area.channels:
        if group_gradients and ch.segments > 1 and ch.service:
            grouped.setdefault(ch.service, []).append(ch)
            continue
        x, y, z = hue_to_room(ch.x, ch.y, ch.z)
        out.append(LightConfig(
            name=ch.label, kind="lamp", transport="hue", hue_bridge=bridge.id,
            hue_channels=[ch.channel_id], x=x, y=y, z=z,
        ))
    for chans in grouped.values():
        chans.sort(key=lambda c: c.index)
        pts = [hue_to_room(c.x, c.y, c.z) for c in chans]
        path = [list(room_to_screen(px, py, pz, room)) for px, py, pz in pts]
        (x, y, z), (x2, y2, z2) = pts[0], pts[-1]
        out.append(LightConfig(
            name=chans[0].name or "Gradient strip", kind="strip", transport="hue", hue_bridge=bridge.id,
            hue_channels=[c.channel_id for c in chans], led_count=len(chans), region_size=0.12,
            strip_layout="screen", screen_path=path, x=x, y=y, z=z, x2=x2, y2=y2, z2=z2,
        ))
    return out
