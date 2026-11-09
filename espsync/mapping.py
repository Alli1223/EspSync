"""Map lights positioned in a room onto regions of the screen.

Lamps (single-colour lights) sample one rectangle:
  * horizontal position follows the light's left/right position in the room,
  * vertical position follows its height (floor -> bottom of screen),
  * lights further from the screen wall sample a wider area, giving a softer,
    more ambient colour (the way Hue Sync treats lights behind the couch).

Strips (addressable LEDs) sample one small rectangle per LED along a path,
either derived from the strip's start/end in the room or drawn directly on the
screen (e.g. "around the TV" ambilight layouts).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import LightConfig, RoomConfig

DEFAULT_ASPECT = 16 / 9
MIN_EXTENT = 0.004


@dataclass(frozen=True)
class Region:
    """Axis-aligned rectangle in normalised screen coordinates."""

    u0: float
    v0: float
    u1: float
    v1: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.u0 + self.u1) / 2, (self.v0 + self.v1) / 2

    @property
    def width(self) -> float:
        return self.u1 - self.u0

    @property
    def height(self) -> float:
        return self.v1 - self.v0

    def as_list(self) -> list[float]:
        return [self.u0, self.v0, self.u1, self.v1]


# Ready-made screen paths for strips ([u, v] polylines).
SCREEN_PRESETS: dict[str, list[list[float]]] = {
    "Bottom edge (left to right)": [[0.0, 1.0], [1.0, 1.0]],
    "Top edge (left to right)": [[0.0, 0.0], [1.0, 0.0]],
    "Left edge (bottom to top)": [[0.0, 1.0], [0.0, 0.0]],
    "Right edge (bottom to top)": [[1.0, 1.0], [1.0, 0.0]],
    "Around TV - U shape (left, top, right)": [[0.0, 1.0], [0.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
    "Around TV - full loop (clockwise from bottom centre)": [
        [0.5, 1.0],
        [0.0, 1.0],
        [0.0, 0.0],
        [1.0, 0.0],
        [1.0, 1.0],
        [0.5, 1.0],
    ],
}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def room_to_screen(x: float, y: float, z: float, room: RoomConfig) -> tuple[float, float]:
    """Project a room position onto the screen plane -> (u, v)."""
    del y  # depth does not move the sample point, only its size
    spread = max(room.horizontal_spread, 0.05)
    u = 0.5 + 0.5 * (x / spread)
    v = 1.0 - z
    return _clamp(u), _clamp(v)


def box(u: float, v: float, w: float, h: float) -> Region:
    """Rectangle of size w*h centred on (u, v), shifted to lie inside the screen."""
    w = _clamp(w, MIN_EXTENT, 1.0)
    h = _clamp(h, MIN_EXTENT, 1.0)
    u0 = _clamp(u - w / 2, 0.0, 1.0 - w)
    v0 = _clamp(v - h / 2, 0.0, 1.0 - h)
    return Region(u0, v0, u0 + w, v0 + h)


def normalise_region(values: list[float]) -> Region:
    u0, v0, u1, v1 = (float(v) for v in values[:4])
    u0, u1 = sorted((_clamp(u0), _clamp(u1)))
    v0, v1 = sorted((_clamp(v0), _clamp(v1)))
    if u1 - u0 < MIN_EXTENT:
        u1 = min(1.0, u0 + MIN_EXTENT)
        u0 = u1 - MIN_EXTENT
    if v1 - v0 < MIN_EXTENT:
        v1 = min(1.0, v0 + MIN_EXTENT)
        v0 = v1 - MIN_EXTENT
    return Region(u0, v0, u1, v1)


def lamp_region(light: LightConfig, room: RoomConfig, aspect: float = DEFAULT_ASPECT) -> Region:
    if light.region:
        return normalise_region(light.region)
    u, v = room_to_screen(light.x, light.y, light.z, room)
    depth = (_clamp(light.y, -1.0, 1.0) + 1.0) / 2.0  # 0 at the screen wall, 1 at the back
    w = _clamp(light.region_size * (1.0 + depth), 0.02, 1.0)
    h = _clamp(w * aspect, 0.02, 1.0)  # roughly square in pixels
    return box(u, v, w, h)


def _strip_path(light: LightConfig, room: RoomConfig) -> list[tuple[float, float]]:
    if light.strip_layout == "screen" and len(light.screen_path) >= 2:
        return [(_clamp(float(p[0])), _clamp(float(p[1]))) for p in light.screen_path]
    return [
        room_to_screen(light.x, light.y, light.z, room),
        room_to_screen(light.x2, light.y2, light.z2, room),
    ]


def sample_polyline(
    path: list[tuple[float, float]], count: int, aspect: float = DEFAULT_ASPECT
) -> tuple[list[tuple[float, float, float, float]], float]:
    """Evenly place `count` points along a polyline.

    Distances are measured in screen-height units (u is scaled by the aspect
    ratio) so LEDs are evenly spaced in pixels.  Returns ([(u, v, du, dv)], spacing)
    where (du, dv) is the local direction of the path.
    """
    count = max(1, int(count))
    segs = []
    total = 0.0
    for (u0, v0), (u1, v1) in zip(path, path[1:]):
        length = math.hypot((u1 - u0) * aspect, v1 - v0)
        if length > 0:
            segs.append((u0, v0, u1, v1, length))
            total += length
    if not segs:
        u, v = path[0]
        return [(u, v, 1.0, 0.0)] * count, 0.0

    spacing = total / count
    points = []
    seg_i, seg_start = 0, 0.0
    for i in range(count):
        target = (i + 0.5) * spacing
        while seg_i < len(segs) - 1 and target > seg_start + segs[seg_i][4]:
            seg_start += segs[seg_i][4]
            seg_i += 1
        u0, v0, u1, v1, length = segs[seg_i]
        t = _clamp((target - seg_start) / length)
        points.append((u0 + (u1 - u0) * t, v0 + (v1 - v0) * t, u1 - u0, v1 - v0))
    return points, spacing


def strip_regions(
    light: LightConfig, room: RoomConfig, aspect: float = DEFAULT_ASPECT
) -> list[Region]:
    points, spacing = sample_polyline(_strip_path(light, room), light.led_count, aspect)
    depth = max(light.region_size, 0.01)  # in screen-height units
    along = max(spacing, 0.01)
    regions = []
    for u, v, du, dv in points:
        if abs(du * aspect) >= abs(dv):  # mostly horizontal segment
            w, h = along / aspect, depth
        else:  # mostly vertical segment
            w, h = depth / aspect, along
        regions.append(box(u, v, w, h))
    if light.reverse:
        regions.reverse()
    return regions


def light_regions(
    light: LightConfig, room: RoomConfig, aspect: float = DEFAULT_ASPECT
) -> list[Region]:
    """All sample regions for a light: one for lamps, `led_count` for strips."""
    if light.kind == "strip":
        return strip_regions(light, room, aspect)
    return [lamp_region(light, room, aspect)]
