"""Overwatch lighting effects, driven by the health bar (see ``espsync.overwatch``).

Mixed over the screen colours per light, after smoothing, like the World of
Warcraft effects in ``espsync.effects`` - but with their own look:

* Damage: a sharp red flash, stronger the bigger the hit, gone in a third of
  a second.
* Critical health: a red double-beat heartbeat that quickens as health drops.
* Healing: a warm gold wave that rolls from the back of the room to the
  screen.
* Eliminated: the room flashes red, then drains to a dim, grey-red wash until
  you are back.
* Respawn: a cold blue-white burst from the screen wall to the back of the
  room.

Deaths are inferred: the health bar disappears right after damage, or while
health was critical.  Leaving to a menu at full health is not a death.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np

from .config import LightConfig, SyncSettings
from .overwatch import OverwatchSignal

RED = np.array([1.0, 0.04, 0.03], np.float32)
GOLD = np.array([1.0, 0.78, 0.22], np.float32)
ICE = np.array([0.45, 0.85, 1.0], np.float32)
ASH = np.array([0.22, 0.05, 0.05], np.float32)  # tint of the "eliminated" wash
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)

# health changes
MIN_CHANGE = 0.035  # smaller steps are reading noise (one segment of a tank is 0.036)
CONFIRM_S = 0.05  # a new value must hold this long ...
CONFIRM_TOL = 0.02  # ... within this much
# damage flash
FLASH_MIN, FLASH_GAIN, FLASH_MAX = 0.3, 2.2, 0.95
FLASH_DECAY_S = 0.16
# heartbeat
BEAT_MIN_HZ, BEAT_MAX_HZ = 1.0, 2.2
BEAT_MIN_MIX, BEAT_MAX_MIX = 0.4, 0.9
BEAT_ATTACK_S = 0.25
# heal wave
HEAL_S = 0.9
HEAL_MIN, HEAL_GAIN, HEAL_MAX = 0.25, 1.6, 0.7
WAVE_WIDTH = 0.7  # in room units (the room is 2 deep)
# elimination / respawn
DEATH_WINDOW_S = 1.5  # bar gone this soon after damage: eliminated
DEATH_AFTER_S = 0.35  # ... and gone for at least this long (a flicker isn't a death)
DEATH_FLASH_S = 0.3
DEATH_FADE_S = 1.2
DEATH_HOLD_S = 12.0  # give the lights back even if the bar never returns
DEATH_MIX = 0.85
RESPAWN_S = 1.3
LOST_AFTER_S = 3.0  # bar gone this long without a death: forget the last health


def heartbeat(phase: float) -> float:
    """Lub-dub: two beats in the first half of each cycle, rest in the second (0..1)."""
    p = phase % 1.0
    lub = math.exp(-(((p - 0.1) / 0.055) ** 2))
    dub = 0.7 * math.exp(-(((p - 0.34) / 0.07) ** 2))
    return min(1.0, lub + dub)


def severity(health: float, threshold: float) -> float:
    """0 at (or above) the threshold, 1 at zero health."""
    if threshold <= 0 or health >= threshold:
        return 0.0
    return min(1.0, (threshold - max(health, 0.0)) / threshold)


def wave(depth: np.ndarray, front: float, width: float = WAVE_WIDTH) -> np.ndarray:
    """1 where the wave front is, easing to 0 half a `width` either side."""
    d = np.abs(np.asarray(depth, np.float32) - front) / (width / 2.0)
    return (np.cos(np.minimum(d, 1.0) * (math.pi / 2.0)) ** 2).astype(np.float32)


class OverwatchEffects:
    """Turns health readings into per-light colour overlays.

    `observe()` gets the reading of every new frame (None when the bar isn't
    showing); `apply()` runs every engine tick so animations keep moving.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.health: float | None = None  # last confirmed reading
        self._pending: tuple[float, float] | None = None  # (value, first seen)
        self._missing_since: float | None = None
        self._damage_at = -math.inf
        self._flash = (0.0, -math.inf)  # (strength, started)
        self._heal = (0.0, -math.inf)
        self._dead_at: float | None = None
        self._respawn_at: float | None = None
        self._phase = 0.0
        self._rate = 0.0
        self._envelope = 0.0
        self._last_apply: float | None = None
        self._test_health: tuple[float, float] | None = None  # (health, until)
        self._test_dead_until: float | None = None

    # ------------------------------------------------------------- state
    @property
    def dead(self) -> bool:
        return self._dead_at is not None

    @property
    def active(self) -> bool:
        return (
            self.health is not None
            or self._dead_at is not None
            or self._respawn_at is not None
            or self._test_health is not None
            or self._envelope > 1e-3
            or self._flash[0] > 0
            or self._heal[0] > 0
        )

    def observe(self, sig: OverwatchSignal | None, now: float) -> None:
        if sig is None:
            if self._missing_since is None:
                self._missing_since = now
            self._pending = None
            self._check_missing(now)
            return
        self._missing_since = None
        value = sig.health
        if self._dead_at is not None and self._test_dead_until is None:
            self._dead_at = None
            self._respawn_at = now
            self.health = value
            self._pending = None
            return
        if self.health is None:
            self.health = value  # first sight: nothing to compare with
            return
        if abs(value - self.health) < MIN_CHANGE:
            self._pending = None
            if abs(value - self.health) < CONFIRM_TOL:
                self.health = value
            return
        # A real change holds; a one-frame misread doesn't.
        if self._pending is None or abs(self._pending[0] - value) > CONFIRM_TOL:
            self._pending = (value, now)
            return
        if now - self._pending[1] < CONFIRM_S:
            return
        delta = value - self.health
        self.health = value
        self._pending = None
        if delta < 0:
            self._on_damage(-delta, now)
        else:
            self._on_heal(delta, now)

    def _on_damage(self, amount: float, now: float) -> None:
        self._damage_at = now
        strength = min(FLASH_MAX, FLASH_MIN + FLASH_GAIN * amount)
        left = self._flash[0] * math.exp(-(now - self._flash[1]) / FLASH_DECAY_S)
        self._flash = (max(strength, left), now)

    def _on_heal(self, amount: float, now: float) -> None:
        if now - self._heal[1] < HEAL_S * 0.6:
            return  # one wave at a time; a healing beam ticks many times a second
        self._heal = (min(HEAL_MAX, HEAL_MIN + HEAL_GAIN * amount), now)

    def _check_missing(self, now: float, threshold: float | None = None) -> None:
        if self._missing_since is None or self.health is None:
            return
        gone = now - self._missing_since
        if self._dead_at is None and gone >= DEATH_AFTER_S:
            critical = threshold is not None and self.health < threshold
            if self._missing_since - self._damage_at <= DEATH_WINDOW_S or critical:
                self._dead_at = self._missing_since
                self.health = 0.0
                return
        if self._dead_at is None and gone > LOST_AFTER_S:
            self.health = None  # menu, loading, alt-tab: start over when the bar is back

    # ------------------------------------------------------------- tests
    def test_damage(self, now: float, amount: float = 0.3) -> None:
        self._on_damage(amount, now)

    def test_heal(self, now: float, amount: float = 0.3) -> None:
        self._heal = (min(HEAL_MAX, HEAL_MIN + HEAL_GAIN * amount), now)

    def test_low_health(self, now: float, health: float = 0.1, seconds: float = 5.0) -> None:
        self._test_health = (health, now + seconds)

    def test_elimination(self, now: float, seconds: float = 3.5) -> None:
        """Eliminated now, back in the fight `seconds` later."""
        self._dead_at = now
        self._respawn_at = None
        self._test_dead_until = now + seconds

    def status_text(self) -> str:
        if self._dead_at is not None:
            return "eliminated"
        if self.health is None or self._missing_since is not None:
            return "health bar not visible"
        return f"health {self.health * 100:.0f}%"

    # ------------------------------------------------------------- output
    def apply(
        self,
        colors: dict[str, np.ndarray],
        lights: Iterable[tuple[LightConfig, np.ndarray]],
        settings: SyncSettings,
        now: float,
    ) -> dict[str, np.ndarray]:
        """Return new colour arrays with the effects mixed in (inputs are not modified).

        `lights` pairs each light with the room positions (n, 2) of its
        elements (see :func:`espsync.effects.element_positions`).
        """
        dt = 0.0 if self._last_apply is None else min(max(now - self._last_apply, 0.0), 0.25)
        self._last_apply = now
        threshold = settings.ow_low_health
        self._check_missing(now, threshold)  # also while the screen is still
        if self._test_dead_until is not None and now >= self._test_dead_until:
            self._test_dead_until = None
            self._dead_at = None
            self._respawn_at = now
        if self._dead_at is not None and now - self._dead_at > DEATH_HOLD_S + DEATH_FADE_S:
            self._dead_at = None
            self.health = None

        # heartbeat
        health = self.health if self._dead_at is None and self._missing_since is None else None
        if self._test_health is not None:
            if now < self._test_health[1]:
                health = self._test_health[0]
            else:
                self._test_health = None
        sev = severity(health, threshold) if health is not None else 0.0
        target = BEAT_MIN_MIX + (BEAT_MAX_MIX - BEAT_MIN_MIX) * sev if sev > 0 else 0.0
        if sev > 0:
            self._rate = BEAT_MIN_HZ + (BEAT_MAX_HZ - BEAT_MIN_HZ) * sev
        elif self._envelope < 1e-3:
            self._rate, self._phase = 0.0, 0.0
        self._phase = (self._phase + self._rate * dt) % 1.0
        self._envelope += (target - self._envelope) * (1.0 - math.exp(-dt / BEAT_ATTACK_S))
        beat = self._envelope * heartbeat(self._phase)

        # damage flash
        flash = self._flash[0] * math.exp(-(now - self._flash[1]) / FLASH_DECAY_S)
        if flash < 1e-3:
            flash = 0.0
            self._flash = (0.0, -math.inf)
        red_mix = 1.0 - (1.0 - beat) * (1.0 - flash)

        # heal wave: back of the room (y = +1) to the screen wall (y = -1)
        heal_t = (now - self._heal[1]) / HEAL_S
        heal = self._heal[0] if 0.0 <= heal_t < 1.0 else 0.0
        if heal_t >= 1.0:
            self._heal = (0.0, -math.inf)
        heal_front = 1.0 + WAVE_WIDTH / 2 - heal_t * (2.0 + WAVE_WIDTH)

        # eliminated: flash, then drain
        dead_flash = dead_wash = 0.0
        if self._dead_at is not None:
            t = now - self._dead_at
            dead_flash = max(0.0, 1.0 - t / DEATH_FLASH_S) ** 2
            dead_wash = DEATH_MIX * min(1.0, t / DEATH_FADE_S)
            if t > DEATH_HOLD_S:
                dead_wash *= max(0.0, 1.0 - (t - DEATH_HOLD_S) / DEATH_FADE_S)

        # respawn burst: screen wall to the back
        respawn_t = None
        if self._respawn_at is not None:
            respawn_t = (now - self._respawn_at) / RESPAWN_S
            if respawn_t >= 1.0:
                self._respawn_at = respawn_t = None

        if red_mix < 1e-3 and heal <= 0 and dead_flash <= 0 and dead_wash <= 0 and respawn_t is None:
            return colors
        out: dict[str, np.ndarray] = {}
        for light, pos in lights:
            c = colors.get(light.id)
            if c is None:
                continue
            gain = np.asarray(light.gains[:3], np.float32) * (settings.brightness * light.brightness)
            depth = pos[:, 1] if len(pos) == len(c) else np.zeros(len(c), np.float32)
            res = np.asarray(c, np.float32)
            if heal > 0:
                mix = heal * wave(depth, heal_front)
                res = res + (np.clip(GOLD * gain, 0.0, 1.0) - res) * mix[:, None]
            if dead_wash > 0:
                grey = (res @ LUMA)[:, None] * 0.35 + np.clip(ASH * gain, 0.0, 1.0)
                res = res + (grey - res) * dead_wash
            if respawn_t is not None:
                front = -1.0 - WAVE_WIDTH / 2 + min(respawn_t / 0.6, 1.0) * (2.0 + WAVE_WIDTH)
                glow = 0.35 * max(0.0, 1.0 - respawn_t) ** 2  # the whole room lifts, then settles
                mix = np.clip(0.9 * wave(depth, front) * (1.0 - respawn_t) + glow, 0.0, 1.0)
                res = res + (np.clip(ICE * gain, 0.0, 1.0) - res) * mix[:, None]
            red = max(red_mix, dead_flash)
            if red >= 1e-3:
                res = res + (np.clip(RED * gain, 0.0, 1.0) - res) * red
            out[light.id] = np.asarray(res, np.float32)
        for lid, c in colors.items():
            out.setdefault(lid, c)
        return out
