"""World of Warcraft integration: read the EspSync addon's signal strip.

The addon (``integrations/wow/EspSync``) draws a thin strip of solid colours in
the top-left corner of the screen; keep the layout here in sync with
``EspSync.lua``.  Measured in cells (4 physical pixels each; the strip is one
cell tall)::

    0      magenta        marker
    1      green          marker
    2, 3   data           3 bits each: red = 1, green = 2, blue = 4
                          bits 0-3 level-up counter (mod 16),
                          bit 4 dead or ghost, bit 5 test health active
    4      check          cell2 ^ cell3 ^ 5
    5-20   health bar     yellow fill on blue, filled from the left
    21     cyan           end marker

Every colour channel is either fully on or off, and the decoder compares each
channel with the marker's own brightness, so the strip reads the same in SDR,
in HDR at any "SDR content brightness" or game UI brightness, and after gamma
changes.  Health is a length, not a colour level, for the same reason (and
because in Midnight the addon can only hand health to a status bar).

The capture backends downscale frames to ~160 px wide on the GPU, which would
blur a 4 px strip away, so they also return the corner at full resolution
(:attr:`Frame.probe`, :data:`PROBE_SIZE`) and the decoder runs on that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

CELL_PX = 4
HEADER_CELLS = 5
BAR_CELLS = 16
TOTAL_CELLS = HEADER_CELLS + BAR_CELLS + 1  # 22 cells, 88 x 4 px at 1:1

# Full-resolution top-left corner the capture backends read: room for the strip
# at up to ~2.9x (WoW rendering below the display's resolution, upscaled).
PROBE_SIZE = (256, 16)  # (width, height) in screen pixels

PROCESS_NAMES = frozenset(
    {"wow.exe", "wowclassic.exe", "wowb.exe", "wowt.exe", "wowclassicb.exe", "wowclassict.exe"}
)

# colour codes: one bit per channel
RED, GREEN, BLUE = 1, 2, 4
MAGENTA, YELLOW, CYAN = RED | BLUE, RED | GREEN, GREEN | BLUE
CHECK_XOR = 5

MIN_LEVEL = 0.02  # linear-light brightness the marker must reach (~sRGB 40/255)
MIN_CELL = 2.0  # below this many pixels per cell the colours blur together
MAX_BAD_BAR_PIXELS = 3  # blended pixels allowed at the bar's ends and fill edge


@dataclass(frozen=True)
class WowSignal:
    health: float  # 0..1
    levelups: int  # counter, mod 16; a change of +1 is a level-up
    dead: bool
    test: bool  # "/espsync test lowhp" is overriding the health bar
    cell: float  # cell size in pixels of the decoded image


def encode_cells(levelups: int, dead: bool = False, test: bool = False) -> tuple[int, int, int]:
    """Colour codes of the two data cells and the check cell (as the addon draws them)."""
    value = (levelups % 16) | (16 if dead else 0) | (32 if test else 0)
    lo, hi = value & 7, value >> 3
    return lo, hi, lo ^ hi ^ CHECK_XOR


def code_rgb(code: int) -> tuple[float, float, float]:
    return (float(code & RED != 0), float(code & GREEN != 0), float(code & BLUE != 0))


def render_strip(
    health: float, levelups: int = 0, dead: bool = False, test: bool = False, cell: int = CELL_PX
) -> np.ndarray:
    """The strip as the addon draws it, float32 (cell, 22 * cell, 3) in 0..1.

    Used by the tests; also documents the layout.  The fill rounds to whole
    pixels like a status bar does on screen.
    """
    cells = [MAGENTA, GREEN, *encode_cells(levelups, dead, test)]
    img = np.zeros((cell, TOTAL_CELLS * cell, 3), np.float32)
    for i, code in enumerate(cells):
        img[:, i * cell : (i + 1) * cell] = code_rgb(code)
    x0, x1 = HEADER_CELLS * cell, (HEADER_CELLS + BAR_CELLS) * cell
    fill = x0 + round(min(max(health, 0.0), 1.0) * (x1 - x0))
    img[:, x0:fill] = code_rgb(YELLOW)
    img[:, fill:x1] = code_rgb(BLUE)
    img[:, x1:] = code_rgb(CYAN)
    return img


def decode_signal(img: np.ndarray | None) -> WowSignal | None:
    """Find the strip at the top-left of `img` (float (h, w, 3), any scale/brightness).

    Returns None unless the markers, the check cell and the bar all agree, so
    ordinary game pictures don't produce signals.
    """
    if img is None or img.ndim != 3 or img.shape[0] < 1 or img.shape[1] < TOTAL_CELLS * MIN_CELL:
        return None
    for y in (1, 0, 2):  # a row through the middle of a 4 px strip first
        if y < img.shape[0]:
            sig = _decode_row(np.asarray(img[y, :, :3], np.float32))
            if sig is not None:
                return sig
    return None


def _decode_row(row: np.ndarray) -> WowSignal | None:
    r0, g0, b0 = (float(v) for v in row[0])
    level = max(r0, b0)
    # Pixel 0 must be magenta: red and blue on, green off.
    if not level > MIN_LEVEL or min(r0, b0) < 0.5 * level or g0 > 0.3 * level:
        return None
    half = 0.5 * level
    on = row > half
    codes = on[:, 0] * RED + on[:, 1] * GREEN + on[:, 2] * BLUE

    # Cell size from the magenta -> green edge, to a fraction of a pixel:
    # interpolate between the centres of the pixels either side of it.
    rising = np.flatnonzero(row[:, 1] > half)
    if not rising.size:
        return None
    x = int(rising[0])
    if x < 1:
        return None
    ga, gb = float(row[x - 1, 1]), float(row[x, 1])
    cell = (x - 0.5) + (half - ga) / max(gb - ga, 1e-9)
    if cell < MIN_CELL or cell * TOTAL_CELLS > len(row) + 0.5:
        return None

    def code_at(index: int) -> int:
        return int(codes[min(int((index + 0.5) * cell), len(codes) - 1)])

    if code_at(1) != GREEN or code_at(TOTAL_CELLS - 1) != CYAN:
        return None
    lo, hi, check = code_at(2), code_at(3), code_at(4)
    if check != lo ^ hi ^ CHECK_XOR:
        return None

    # Health bar: pixels whose centres lie inside it.
    x0, x1 = HEADER_CELLS * cell, (HEADER_CELLS + BAR_CELLS) * cell
    i0, i1 = math.ceil(x0 - 0.5), math.floor(x1 - 0.5)
    seg = codes[i0 : i1 + 1]
    if seg.size < BAR_CELLS:
        return None
    yellow = seg == YELLOW
    blue = seg == BLUE
    if int(seg.size - yellow.sum() - blue.sum()) > MAX_BAD_BAR_PIXELS:
        return None
    ys, bs = np.flatnonzero(yellow), np.flatnonzero(blue)
    if ys.size and bs.size and int((ys > bs[0]).sum()) > 1:
        return None  # fill must run from the left
    # Partly covered pixels at the fill edge count by their red level,
    # relative to the fill's own red (HDR may not keep it equal to magenta's).
    red = row[i0 : i1 + 1, 0]
    full = float(np.median(red[ys])) if ys.size else level
    empty = float(np.median(red[bs])) if bs.size else 0.0
    fill = float(np.clip((red - empty) / max(full - empty, 1e-9), 0.0, 1.0).sum())
    health = min(max(fill / seg.size, 0.0), 1.0)

    value = lo | (hi << 3)
    return WowSignal(
        health=health,
        levelups=value & 15,
        dead=bool(value & 16),
        test=bool(value & 32),
        cell=cell,
    )


def hide_strip(image: np.ndarray, source_size: tuple[int, int], sig: WowSignal) -> None:
    """Paint over the strip in a downscaled frame (in place) so lights don't pick it up.

    `sig.cell` is in source (screen) pixels; the strip's pixels are replaced
    with the row just below it.
    """
    h, w = image.shape[:2]
    sw, sh = source_size
    if not (sw and sh):
        return
    cols = min(w, math.ceil(sig.cell * TOTAL_CELLS * w / sw))
    rows = math.ceil(sig.cell * h / sh)
    if rows >= h or cols <= 0:
        return
    image[:rows, :cols] = image[rows, :cols]
