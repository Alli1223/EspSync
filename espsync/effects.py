"""Game effects mixed over the screen colours, per light, after smoothing.

* Low health: every light pulses red.  Below the threshold the pulse gets
  faster and stronger as health drops.
* Level-up: a gold wedge spins round the room centre for SWEEP_S seconds,
  lighting each light as it passes the light's angle in the top-down room
  layout, then the whole room glows gold and fades back to the screen.

Everything works on the gamma-encoded RGB (0..1) colours the outputs receive,
so it applies to every kind of light alike.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np

from .config import LightConfig, RoomConfig, SyncSettings
from .mapping import DEFAULT_ASPECT, sample_polyline
from .wow import WowSignal

RED = np.array([1.0, 0.0, 0.0], np.float32)
GOLD = np.array([1.0, 0.62, 0.08], np.float32)

# low-health pulse
PULSE_MIN_HZ = 0.8  # just below the threshold
PULSE_MAX_HZ = 2.5  # at zero health
PULSE_MIN_MIX = 0.35  # peak share of red just below the threshold
PULSE_MAX_MIX = 0.95  # ... and at zero health
PULSE_ATTACK_S = 0.25  # how fast the pulse fades in/out when health crosses the threshold

# level-up
SWEEP_S = 3.0
SWEEP_TURNS = 2.0
SWEEP_START = -math.pi / 2  # straight ahead: the screen wall (y = -1)
WEDGE_HALF_ANGLE = math.radians(60)
SWEEP_BASE = 0.2  # gold tint on lights outside the wedge
SWEEP_FILL_S = 0.6  # the last part of the sweep fills the room with gold ...
GLOW_PEAK = 0.85
GLOW_S = 1.5  # ... which then fades out
FADE_IN_S = 0.15

LOST_AFTER_S = 0.75  # strip missing this long: the game is gone (menu, loading, alt-tab)
COUNTER_MOD = 16


# ------------------------------------------------------------------ pulse maths
def pulse_severity(health: float, threshold: float) -> float:
    """0 at (or above) the threshold, 1 at zero health."""
    if threshold <= 0 or health >= threshold:
        return 0.0
    return min(1.0, (threshold - max(health, 0.0)) / threshold)


def pulse_rate(health: float, threshold: float) -> float:
    """Pulses per second; 0 when health is above the threshold."""
    if threshold <= 0 or health >= threshold:
        return 0.0
    return PULSE_MIN_HZ + (PULSE_MAX_HZ - PULSE_MIN_HZ) * pulse_severity(health, threshold)


def pulse_strength(health: float, threshold: float) -> float:
    """Peak share of red mixed over the screen colour; 0 above the threshold."""
    if threshold <= 0 or health >= threshold:
        return 0.0
    return PULSE_MIN_MIX + (PULSE_MAX_MIX - PULSE_MIN_MIX) * pulse_severity(health, threshold)


def pulse_wave(phase: float) -> float:
    """One beat per unit of phase: 0 at whole numbers, 1 half way, sharpened peak."""
    v = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
    return v * v


# ------------------------------------------------------------------ sweep maths
def sweep_angle(t: float) -> float:
    """Angle of the wedge centre t seconds into the sweep (radians, atan2(y, x) convention).

    It starts at the screen and turns towards +x first: screen -> right wall ->
    back -> left wall, i.e. clockwise in EspSync's top-down room view.
    """
    return SWEEP_START + 2.0 * math.pi * SWEEP_TURNS * t / SWEEP_S


def wedge_weight(angles: np.ndarray, wedge: float, half_angle: float = WEDGE_HALF_ANGLE) -> np.ndarray:
    """1 at the wedge centre, easing to 0 at +-half_angle."""
    d = np.abs((np.asarray(angles, np.float64) - wedge + math.pi) % (2.0 * math.pi) - math.pi)
    w = np.cos(np.minimum(d / half_angle, 1.0) * (math.pi / 2.0)) ** 2
    return w.astype(np.float32)


def _smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def levelup_mix(angles: np.ndarray, t: float) -> np.ndarray | None:
    """Share of gold per light element t seconds after a level-up; None once it's over."""
    if t >= SWEEP_S + GLOW_S:
        return None
    if t < 0:
        return np.zeros(len(angles), np.float32)
    ts = min(t, SWEEP_S)
    base = SWEEP_BASE + (GLOW_PEAK - SWEEP_BASE) * _smoothstep((ts - (SWEEP_S - SWEEP_FILL_S)) / SWEEP_FILL_S)
    mix = base + (1.0 - base) * wedge_weight(angles, sweep_angle(ts))
    mix *= min(1.0, t / FADE_IN_S)
    if t > SWEEP_S:
        mix *= (1.0 - (t - SWEEP_S) / GLOW_S) ** 2
    return mix.astype(np.float32)


# ------------------------------------------------------------- light positions
def element_positions(light: LightConfig, room: RoomConfig, aspect: float = DEFAULT_ASPECT) -> np.ndarray:
    """Room (x, y) of every colour a light takes, in the order the engine sends them."""
    if light.kind != "strip":
        return np.array([[light.x, light.y]], np.float32)
    n = max(1, int(light.led_count))
    if light.strip_layout == "screen" and len(light.screen_path) >= 2:
        # A strip drawn on the screen sits on the screen wall, across the TV.
        path = [(float(p[0]), float(p[1])) for p in light.screen_path]
        points, _ = sample_polyline(path, n, aspect)
        pos = np.array([[(u - 0.5) * 2.0 * room.tv_width, -1.0] for u, _v, _du, _dv in points], np.float32)
    else:
        t = (np.arange(n, dtype=np.float32) + 0.5) / n
        pos = np.stack([light.x + (light.x2 - light.x) * t, light.y + (light.y2 - light.y) * t], axis=1)
    if light.reverse:
        pos = pos[::-1]
    return np.ascontiguousarray(pos, np.float32)


def element_angles(light: LightConfig, room: RoomConfig, aspect: float = DEFAULT_ASPECT) -> np.ndarray:
    """Angle of each element round the room centre, in metres (a 6 x 3 m room isn't square)."""
    pos = element_positions(light, room, aspect)
    return np.arctan2(pos[:, 1] * room.depth_m, pos[:, 0] * room.width_m).astype(np.float32)


# ------------------------------------------------------------------ state
class GameEffects:
    """Turns decoded game signals into per-light colour overlays.

    `observe()` is fed the decoded strip of every new frame (None when it
    isn't there); `apply()` runs every engine tick, including ticks without a
    new frame, so animations keep moving while the screen is still.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.signal: WowSignal | None = None
        self._missing_since: float | None = None
        self._levelup_at: float | None = None
        self._phase = 0.0
        self._rate = 0.0
        self._envelope = 0.0
        self._last_apply: float | None = None
        self._test_health: tuple[float, float] | None = None  # (health, until)

    @property
    def active(self) -> bool:
        return (
            self.signal is not None
            or self._levelup_at is not None
            or self._test_health is not None
            or self._envelope > 1e-3
        )

    @property
    def levelup_running(self) -> bool:
        return self._levelup_at is not None

    def observe(self, sig: WowSignal | None, now: float) -> None:
        """Feed the strip decoded from a new frame, or None when it isn't there."""
        if sig is None:
            if self._missing_since is None:
                self._missing_since = now
            self._check_lost(now)
            return
        self._missing_since = None
        if self.signal is not None:
            step = (sig.levelups - self.signal.levelups) % COUNTER_MOD
            if 1 <= step <= 3:
                self._levelup_at = now
        self.signal = sig

    def _check_lost(self, now: float) -> None:
        if self.signal is not None and self._missing_since is not None and now - self._missing_since > LOST_AFTER_S:
            # Gone (loading screen, /reload, alt-tab, game closed): the counter
            # starts over next time, so its new value isn't a level-up.
            self.signal = None

    def trigger_levelup(self, now: float) -> None:
        """Play the level-up effect (settings "Test" button)."""
        self._levelup_at = now

    def test_low_health(self, now: float, health: float = 0.15, seconds: float = 5.0) -> None:
        """Pulse as if health were `health` for a few seconds (settings "Test" button)."""
        self._test_health = (health, now + seconds)

    def status_text(self) -> str:
        sig = self.signal
        if sig is None:
            return "addon not seen"
        if sig.dead:
            text = "dead"
        else:
            text = f"health {sig.health * 100:.0f}%" + (" (test)" if sig.test else "")
        if self._levelup_at is not None:
            text += " · level up!"
        return text

    def apply(
        self,
        colors: dict[str, np.ndarray],
        lights: Iterable[tuple[LightConfig, np.ndarray]],
        settings: SyncSettings,
        now: float,
    ) -> dict[str, np.ndarray]:
        """Return new colour arrays with the effects mixed in (inputs are not modified).

        `lights` pairs each light with the angles of its elements (see
        :func:`element_angles`), in the same order as its colours.
        """
        dt = 0.0 if self._last_apply is None else min(max(now - self._last_apply, 0.0), 0.25)
        self._last_apply = now
        self._check_lost(now)  # also while the screen is still and no frames arrive

        sig = self.signal
        threshold = settings.wow_low_health
        health = sig.health if sig is not None and not sig.dead else None
        if self._test_health is not None:
            if now < self._test_health[1]:
                health = self._test_health[0]
            else:
                self._test_health = None
        target = pulse_strength(health, threshold) if health is not None else 0.0
        rate = pulse_rate(health, threshold) if health is not None else 0.0
        if rate > 0:
            self._rate = rate
        elif self._envelope < 1e-3:
            self._rate, self._phase = 0.0, 0.0  # next pulse starts from the screen colour
        self._phase = (self._phase + self._rate * dt) % 1.0
        self._envelope += (target - self._envelope) * (1.0 - math.exp(-dt / PULSE_ATTACK_S))
        red_mix = self._envelope * pulse_wave(self._phase)

        levelup_t = None
        if self._levelup_at is not None:
            levelup_t = now - self._levelup_at
            if levelup_t >= SWEEP_S + GLOW_S:
                self._levelup_at = levelup_t = None

        if red_mix < 1e-3 and levelup_t is None:
            return colors
        out: dict[str, np.ndarray] = {}
        for light, angles in lights:
            c = colors.get(light.id)
            if c is None:
                continue
            gain = np.asarray(light.gains[:3], np.float32) * (settings.brightness * light.brightness)
            res = c
            if red_mix >= 1e-3:
                res = res + (np.clip(RED * gain, 0.0, 1.0) - res) * red_mix
            if levelup_t is not None:
                mix = levelup_mix(angles, levelup_t)
                if mix is not None and len(mix) == len(res):
                    res = res + (np.clip(GOLD * gain, 0.0, 1.0) - res) * mix[:, None]
            out[light.id] = np.asarray(res, np.float32)
        for lid, c in colors.items():
            out.setdefault(lid, c)
        return out
