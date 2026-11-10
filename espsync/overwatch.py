"""Overwatch integration: read the player's health bar off the screen.

Overwatch has no addon or telemetry API, so unlike World of Warcraft (see
``espsync.wow``) there is nothing to draw a machine-readable strip.  EspSync
only ever looks at the captured picture - it never touches the game's process
or memory - and reads the health bar in the bottom-left of the HUD.

The bar (measured on 16:9 and 21:9 footage, everything in units of the screen
height ``h``):

* a row of segments, 25 HP each, ``BAR_WIDTH`` wide in total whatever the
  hero's maximum health: white health, then orange armour, blue shields and
  green overhealth, filled from the left.  Missing health is a faint
  see-through box; health lost a moment ago shows red after the fill.
* ``BAR_THICKNESS`` tall and tilted with the rest of the HUD: it rises by
  ``BAR_SLOPE`` px per px to the right.
* its left end sits about 0.88 h down the screen.  How far in from the left
  edge depends on the aspect ratio (0.15 h at 16:9, 0.28 h at 21:9), so the
  reader finds the bar inside a search window and then locks on to that spot.

Reading rules, all chosen so that a wrong guess means "no reading" rather
than a wrong health value:

* nothing is reported until the same bar (right thickness, right tilt, dark
  name plate below it, at least ``LOCK_MIN_FILL`` full) was found in the same
  place several times in a row;
* afterwards only that place is read; menus, kill cams and loading screens
  fail the checks and read as "HUD not visible".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

PROCESS_NAMES = frozenset({"overwatch.exe"})

# ---- HUD geometry, in units of the screen height
BAR_WIDTH = 0.226
BAR_THICKNESS = 0.025
BAR_SLOPE = 0.068  # tan(3.9 deg); the bar rises to the right
BAR_Y_LEFT = 0.879  # centre line at the bar's left end
BAR_X_16_9 = 0.151  # left end at 16:9 ...
BAR_X_21_9 = 0.276  # ... and at 21:9

# Full-resolution area the capture backends return as Frame.probe:
# (x0, y0, x1, y1) with x measured from the left edge.
PROBE = (0.06, 0.82, 0.62, 0.935)
SEARCH_X = (0.09, 0.37)  # where the bar's left end may be
SEARCH_Y_TOL = 0.016  # ... and how far its centre line may be from BAR_Y_LEFT

MIN_LEVEL = 0.05  # linear-light brightness the first segment must reach
GAP = 0.0045  # widest separator between two filled segments
LOCK_MIN_FILL = 0.4  # health needed to lock on (long enough to check the tilt)
LOCK_HITS = 3  # same place this many searches in a row
FULL_ABOVE = 0.97  # the last segment's edge is soft: read this as full

NONE, FILL, RED = 0, 1, 2


@dataclass(frozen=True)
class OverwatchSignal:
    health: float  # 0..1 of the bar: health + armour + shields over their maximum
    x: int  # the bar's left end in probe pixels
    row: int  # its centre row in the de-skewed probe


def probe_rect(source_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Screen rectangle (x, y, w, h) in pixels that holds the health bar."""
    _w, h = source_size
    x0, y0, x1, y1 = (round(v * h) for v in PROBE)
    return x0, y0, x1 - x0, y1 - y0


def classify(px: np.ndarray, level: float) -> np.ndarray:
    """FILL / RED / NONE for each (..., 3) linear RGB pixel, given the bar's own brightness.

    The HUD's colours are flat and fully saturated, so the classes are narrow:
    a blue sky or a fire behind the see-through missing segments must not
    count as shields or armour.
    """
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    v = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    white = (mn >= 0.8 * v) & (v >= 0.75 * level)
    lit = v >= 0.5 * level
    shield = (b >= v) & (g >= 0.45 * v) & (g <= 0.95 * v) & (r <= 0.12 * v)  # sRGB ~(3, 189, 222)
    armour = (r >= v) & (g >= 0.2 * v) & (g <= 0.7 * v) & (b <= 0.15 * v)  # orange
    overhealth = (g >= v) & (r <= 0.35 * v) & (b <= 0.35 * v)  # green
    red = (r >= v) & (g < 0.12 * v) & (b <= 0.15 * v)  # sRGB ~(242, 0, 31)
    out = np.zeros(px.shape[:-1], np.uint8)
    out[white | (lit & (shield | armour | overhealth))] = FILL
    out[lit & red] = RED
    return out


def deskew(img: np.ndarray, slope: float = BAR_SLOPE, cols: tuple[int, int] | None = None,
           rows: tuple[int, int] | None = None) -> np.ndarray:
    """Shear the probe so that the tilted bar lies along a row.

    Row ``r`` of the result holds, in column ``x``, source row
    ``r + round(slope * (W - 1 - x))``.  `cols` / `rows` limit the work to
    part of the result (half-open ranges of its columns and rows); the part
    is returned, so index it relative to their starts.
    """
    h, w = img.shape[:2]
    c0, c1 = cols if cols is not None else (0, w)
    c0, c1 = max(c0, 0), min(c1, w)
    xs = np.arange(c0, c1)
    shift = np.rint(slope * (w - 1 - xs)).astype(np.intp)
    r0, r1 = rows if rows is not None else (0, h)
    r0, r1 = max(r0, 0), min(r1, h - int(shift.max(initial=0)))
    if r1 <= r0 or c1 <= c0:
        return img[:0, :0]
    return img[np.arange(r0, r1)[:, None] + shift[None, :], xs[None, :]]


def bar_window(x: int, row: int, h: float) -> tuple[tuple[int, int], tuple[int, int]]:
    """Columns and rows of the de-skewed probe that :func:`read_at` looks at for a bar at (x, row)."""
    t = BAR_THICKNESS * h
    return (
        (x - max(3, round(0.008 * h)), x + math.ceil(1.06 * BAR_WIDTH * h)),
        (row - round(0.3 * t) - 1, row + round(0.8 * t) + 2),
    )


def _run(codes: np.ndarray, gap: int) -> tuple[int, int]:
    """Length of the fill from index 0, and of fill plus the red after it."""
    fill = red = 0
    in_red = False
    missing = 0
    for i, c in enumerate(codes.tolist()):
        if c == FILL and not in_red:
            fill = red = i + 1
            missing = 0
        elif c == RED:
            in_red = True
            red = i + 1
            missing = 0
        else:
            missing += 1
            if missing > gap:
                break
    return fill, red


def _band(d: np.ndarray, x: int, row: int, level: float, reach: int) -> tuple[int, int]:
    """Top and bottom rows of the lit band through (row, x)."""
    lo, hi = max(0, row - reach), min(d.shape[0], row + reach + 1)
    codes = classify(d[lo:hi, x], level) != NONE
    centre = row - lo
    if not codes[centre]:
        return row, row - 1
    top = centre
    while top > 0 and codes[top - 1]:
        top -= 1
    bottom = centre
    while bottom < len(codes) - 1 and codes[bottom + 1]:
        bottom += 1
    return lo + top, lo + bottom


def read_at(d: np.ndarray, x: int, row: int, h: float, check_tilt: bool = False) -> float | None:
    """Health of the bar whose left end is at column `x`, centred on `row` of the de-skewed probe `d`."""
    t = BAR_THICKNESS * h
    full = BAR_WIDTH * h
    inner = max(1, round(0.2 * t))
    margin_far, margin_near = max(3, round(0.008 * h)), max(1, round(0.002 * h))
    below = row + round(0.8 * t)
    span = math.ceil(1.06 * full)
    if x - margin_far < 0 or row - inner < 0 or below >= d.shape[0] or x + 4 >= d.shape[1]:
        return None
    end = min(d.shape[1], x + span)
    line = np.median(d[[row - inner, row, row + inner], x - margin_far : end], axis=0)
    seg = line[margin_far:]
    first = seg[1 : max(4, round(0.008 * h))]
    level = float(np.median(first.max(axis=1)))
    if not level > MIN_LEVEL:
        return None
    codes = classify(seg, level)
    if codes[0] == NONE and codes[1] == NONE:
        return None
    fill, red = _run(codes, max(2, round(GAP * h)))
    if red < max(3, round(0.004 * h)):
        return None
    # Left of the bar there is a gap before the hero portrait.
    if (classify(line[: margin_far - margin_near], level) == FILL).mean() > 0.5:
        return None
    # The band is BAR_THICKNESS thick ...
    edge = max(1, round(0.3 * t))
    for r in (row - edge, row + edge):
        if r < 0 or (classify(d[r, x : x + red], level) != NONE).mean() < 0.6:
            return None
    # ... and under it is the dark name plate, not more white.
    if (classify(d[below, x : x + red], level) == FILL).mean() > 0.4:
        return None
    if check_tilt:
        # In the de-skewed probe the band must stay on the same rows from end
        # to end; a level bar (a menu's progress bar, say) drifts across them.
        reach = round(1.2 * t)
        lit = np.flatnonzero(codes[:red] != NONE)
        a, b = int(lit[np.searchsorted(lit, 0.12 * red)]), int(lit[np.searchsorted(lit, 0.88 * red, side="right") - 1])
        ta, ba = _band(d, x + a, row, level, reach)
        tb, bb = _band(d, x + b, row, level, reach)
        tol = max(2, round(0.004 * h))
        thick = (ba - ta + 1, bb - tb + 1)
        if min(thick) < 0.6 * t or max(thick) > 1.4 * t or abs(ta - tb) > tol or abs(ba - bb) > tol:
            return None
    health = (fill if fill else red) / full
    return 1.0 if health >= FULL_ABOVE else float(min(max(health, 0.0), 1.0))


def find_bar(d: np.ndarray, h: float, x_offset: int, y_offset: int) -> tuple[int, int, float] | None:
    """Search the de-skewed probe for the bar: (x, row, health) of the longest valid one.

    `x_offset` / `y_offset` are the probe's position on the screen in pixels.
    """
    t = BAR_THICKNESS * h
    w = d.shape[1]
    x_lo, x_hi = (round(v * h) - x_offset for v in SEARCH_X)
    best: tuple[int, int, float] | None = None
    step = max(1, round(0.003 * h))
    min_run = round(0.02 * h)
    for row in range(round(0.3 * t), d.shape[0] - round(0.8 * t), step):
        line = d[row]
        v = line.max(axis=1)
        level = max(MIN_LEVEL, float(np.percentile(v, 98)))
        mask = classify(line, level) == FILL
        edges = np.flatnonzero(mask[1:] & ~mask[:-1]) + 1
        for x in edges.tolist():
            if not x_lo <= x <= x_hi or not mask[x : x + min_run].mean() > 0.6:
                continue
            # Where would this bar's left end be on the screen?
            y_left = y_offset + row + BAR_SLOPE * (w - 1 - x)
            if abs(y_left / h - BAR_Y_LEFT) > SEARCH_Y_TOL + BAR_THICKNESS / 2:
                continue
            # a few pixels in: inside the first segment whatever its width
            top, bottom = _band(d, x + max(1, round(0.003 * h)), row, level, round(1.2 * t))
            if not 0.6 * t <= bottom - top + 1 <= 1.4 * t:
                continue
            centre = (top + bottom) // 2
            y_left = y_offset + centre + BAR_SLOPE * (w - 1 - x)
            if abs(y_left / h - BAR_Y_LEFT) > SEARCH_Y_TOL:
                continue
            health = read_at(d, x, centre, h, check_tilt=True)
            if health is not None and (best is None or health > best[2]):
                best = (x, centre, health)
    return best


class HudReader:
    """Finds the health bar, locks on to its position and reads it every frame."""

    SEARCH_EVERY_S = 0.1

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.lock: tuple[int, int] | None = None  # (x, row) in the de-skewed probe
        self._size: tuple[int, int] | None = None
        self._candidate: tuple[int, int] | None = None
        self._hits = 0
        self._searched = -math.inf

    def read(self, probe: np.ndarray | None, source_size: tuple[int, int], now: float) -> OverwatchSignal | None:
        """Health from this frame's probe (see :func:`probe_rect`), or None while the bar isn't showing."""
        if probe is None or probe.ndim != 3:
            return None
        if source_size != self._size:
            self.reset()
            self._size = source_size
        h = float(source_size[1])
        x_off, y_off, pw, ph = probe_rect(source_size)
        if h < 300 or probe.shape[0] != ph or probe.shape[1] != pw:
            return None  # clipped or unexpected probe: positions wouldn't line up
        img = np.asarray(probe[..., :3], np.float32)
        if self.lock is not None:
            # Only the bar's own pixels: de-skewing the whole probe costs milliseconds.
            x, row = self.lock
            cols, rows = bar_window(x, row, h)
            if cols[0] >= 0 and rows[0] >= 0:
                health = read_at(deskew(img, cols=cols, rows=rows), x - cols[0], row - rows[0], h)
                if health is not None:
                    return OverwatchSignal(health, x, row)
        if now - self._searched < self.SEARCH_EVERY_S:
            return None
        self._searched = now
        found = find_bar(deskew(img), h, x_off, y_off)
        if found is None or found[2] < LOCK_MIN_FILL:
            self._hits = 0
            return None
        x, row, health = found
        tol = max(2, round(0.004 * h))
        near = self._candidate is not None and abs(self._candidate[0] - x) <= tol and abs(self._candidate[1] - row) <= tol
        self._hits = self._hits + 1 if near else 1
        self._candidate = (x, row)
        if self._hits < LOCK_HITS:
            return None
        self.lock = (x, row)
        self._hits = 0
        return OverwatchSignal(health, x, row)


# --------------------------------------------------------------------- drawing
WHITE = (1.0, 1.0, 1.0)
ARMOUR = (0.96, 0.33, 0.02)
SHIELD = (0.02, 0.5, 0.75)
DAMAGE = (0.9, 0.0, 0.015)


def render_hud(
    source_size: tuple[int, int],
    health: float,
    *,
    segments: int = 8,
    armour: float = 0.0,
    shields: float = 0.0,
    recent_damage: float = 0.0,
    x_left: float | None = None,
    background: tuple[float, float, float] = (0.05, 0.07, 0.12),
    level: float = 1.0,
    slope: float = BAR_SLOPE,
) -> np.ndarray:
    """The probe area as Overwatch draws it (linear light), for tests and as documentation.

    `health`, `armour`, `shields` and `recent_damage` are shares of the whole
    bar, drawn in that order from the left.  `x_left` is the bar's left end in
    screen heights (default: where it is at this aspect ratio).
    """
    sw, sh = source_size
    x0, y0, pw, ph = probe_rect(source_size)
    if x_left is None:
        x_left = BAR_X_21_9 if sw / sh > 2.0 else BAR_X_16_9
    img = np.empty((ph, pw, 3), np.float32)
    img[...] = background
    xs = np.arange(pw) + x0 - x_left * sh  # distance along the bar, per column
    ys = np.arange(ph)[:, None] + y0
    centre = BAR_Y_LEFT * sh - slope * xs[None, :]
    inside = np.abs(ys - centre) <= BAR_THICKNESS * sh / 2
    full = BAR_WIDTH * sh
    pitch = full / segments
    gap = max(1.0, 0.002 * sh)
    in_segment = (xs >= 0) & (xs < full) & ((xs % pitch) < pitch - gap)
    # name plate under the bar, hero portrait frame to its left
    plate = (ys - centre > BAR_THICKNESS * sh * 0.62) & (ys - centre < BAR_THICKNESS * sh * 1.6)
    img[plate & ((xs >= 0) & (xs < full))[None, :]] = (0.012, 0.014, 0.03)
    frame = (xs > -0.02 * sh) & (xs < -0.012 * sh)
    img[(np.abs(ys - centre) < 0.04 * sh) & frame[None, :]] = np.multiply(SHIELD, level)
    share = np.clip(xs / full, 0.0, 1.0)
    edges = np.cumsum([health, armour, shields, recent_damage])
    colours = (WHITE, ARMOUR, SHIELD, DAMAGE)
    lo = 0.0
    for hi, colour in zip(edges, colours, strict=True):
        cols = in_segment & (xs >= lo * full) & (xs < hi * full)
        img[inside & cols[None, :]] = np.multiply(colour, level)
        lo = hi
    # missing health: a faint box that lets the scene through
    empty = in_segment & (share >= lo)
    box = inside & empty[None, :]
    img[box] = img[box] * 0.6 + 0.04 * level
    return img
