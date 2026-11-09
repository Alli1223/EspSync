"""Colour pipeline: HDR tone mapping, region averaging, letterbox detection.

All heavy work happens on the small (~160-320 px wide) linear-light frame from
the capture backend, so a full pass costs about a millisecond.
"""

from __future__ import annotations

import time

import numpy as np

from .capture.base import Frame
from .config import SyncSettings

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


# ------------------------------------------------------------------ transfer fns


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(
        np.float32
    )


def tonemap(img: np.ndarray, peak: float, knee: float = 0.85) -> np.ndarray:
    """Compress linear HDR values in [0, peak] into [0, 1] (in place).

    Hue-preserving: every pixel is scaled by f(m)/m where m is its brightest
    channel.  Below `knee` values are untouched (SDR content stays exact); above
    it a rational curve with a continuous slope rolls highlights off so that
    `peak` lands exactly on 1.0.  Compressing per pixel *before* averaging
    keeps small specular highlights from dominating a region's colour.
    """
    if peak <= 1.0 + 1e-3:
        np.clip(img, 0.0, 1.0, out=img)
        return img
    m = img.max(axis=2)
    over = m > knee
    if not over.any():
        return img
    c = (1.0 - knee) / (peak - 1.0)
    mo = m[over]
    x = np.clip((mo - knee) / (peak - knee), 0.0, 1.0)
    f = knee + (1.0 - knee) * (x * (1.0 + c) / (x + c))
    img[over] *= (f / mo)[:, None]
    return img


def saturate(enc: np.ndarray, amount: float) -> np.ndarray:
    """Scale saturation of (N, 3) gamma-encoded colours around their luma."""
    if abs(amount - 1.0) < 1e-3:
        return enc
    lum = enc @ LUMA
    out = lum[:, None] + (enc - lum[:, None]) * amount
    return np.clip(out, 0.0, 1.0)


# ------------------------------------------------------------------- letterbox


class LetterboxDetector:
    """Detects black bars so lights follow the picture, not the bars.

    Bars that shrink (content appeared where we thought there was a bar) are
    applied immediately; bars that grow must be stable for `settle_s` first, so
    dark scenes don't make the crop jump around.
    """

    def __init__(self, threshold: float = 0.006, settle_s: float = 1.5, max_bar: float = 0.3):
        self.threshold = threshold
        self.settle_s = settle_s
        self.max_bar = max_bar
        self.bars = (0.0, 0.0)  # (vertical bar fraction top/bottom, horizontal left/right)
        self._candidate: tuple[float, float] | None = None
        self._since = 0.0

    @staticmethod
    def _bar(mask: np.ndarray) -> float | None:
        n = mask.size
        if not mask.any():
            return None
        start = int(np.argmax(mask))
        end = int(np.argmax(mask[::-1]))
        bar = min(start, end) / n  # symmetric: subtitles in the lower bar don't break it
        return bar if bar >= 0.02 else 0.0

    def update(self, img: np.ndarray, now: float | None = None) -> tuple[float, float]:
        now = time.monotonic() if now is None else now
        bright = img.max(axis=2) > self.threshold
        v = self._bar(bright.any(axis=1))
        h = self._bar(bright.any(axis=0))
        if v is None or h is None:  # all-black frame: keep the current crop
            return self.bars
        v = min(v, self.max_bar)
        h = min(h, self.max_bar)
        cur_v, cur_h = self.bars
        # Shrinking bars take effect immediately.
        new_v, new_h = min(cur_v, v), min(cur_h, h)
        if (v, h) != (new_v, new_h):  # some bar wants to grow
            if self._candidate is None or not np.allclose(self._candidate, (v, h), atol=0.02):
                self._candidate = (v, h)
                self._since = now
            elif now - self._since >= self.settle_s:
                new_v, new_h = v, h
                self._candidate = None
        else:
            self._candidate = None
        self.bars = (new_v, new_h)
        return self.bars


# ---------------------------------------------------------------- region means


def region_means(
    img: np.ndarray,
    regions: np.ndarray,
    bars: tuple[float, float] = (0.0, 0.0),
    vivid: bool = False,
) -> np.ndarray:
    """Mean linear colour of each region -> (N, 3).

    `regions` is (N, 4) [u0, v0, u1, v1] in the *active picture* (inside any
    detected black bars).  Uses summed-area tables, so cost is independent of
    region size and hundreds of LED regions are cheap.
    """
    h, w = img.shape[:2]
    if regions.size == 0:
        return np.zeros((0, 3), np.float32)
    bv, bh = bars
    r_lo, r_hi = bv * h, h - bv * h
    c_lo, c_hi = bh * w, w - bh * w

    c0 = np.floor(c_lo + regions[:, 0] * (c_hi - c_lo)).astype(np.int64)
    c1 = np.ceil(c_lo + regions[:, 2] * (c_hi - c_lo)).astype(np.int64)
    r0 = np.floor(r_lo + regions[:, 1] * (r_hi - r_lo)).astype(np.int64)
    r1 = np.ceil(r_lo + regions[:, 3] * (r_hi - r_lo)).astype(np.int64)
    c0 = np.clip(c0, 0, w - 1)
    r0 = np.clip(r0, 0, h - 1)
    c1 = np.clip(np.maximum(c1, c0 + 1), 1, w)
    r1 = np.clip(np.maximum(r1, r0 + 1), 1, h)

    if vivid:
        mx = img.max(axis=2)
        mn = img.min(axis=2)
        sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
        weight = (0.05 + sat * np.sqrt(np.minimum(mx, 1.0))).astype(np.float32)
        stack = np.concatenate([img, img * weight[..., None], weight[..., None]], axis=2)
    else:
        stack = img

    sat_tab = np.zeros((h + 1, w + 1, stack.shape[2]), dtype=np.float64)
    np.cumsum(stack, axis=0, out=sat_tab[1:, 1:])
    np.cumsum(sat_tab[1:, 1:], axis=1, out=sat_tab[1:, 1:])
    sums = sat_tab[r1, c1] - sat_tab[r0, c1] - sat_tab[r1, c0] + sat_tab[r0, c0]
    area = ((r1 - r0) * (c1 - c0)).astype(np.float64)[:, None]
    plain = sums[:, :3] / area
    if not vivid:
        return plain.astype(np.float32)

    weighted = sums[:, 3:6] / np.maximum(sums[:, 6:7], 1e-9)
    v_plain = plain.max(axis=1)
    v_weighted = weighted.max(axis=1)
    # Hue/saturation from the weighted mean; brightness between the two means so
    # a small saturated object on a dark background shows its colour without
    # blasting the light to full brightness.
    v = np.sqrt(v_plain * v_weighted)
    scale = np.where(v_weighted > 1e-9, v / np.maximum(v_weighted, 1e-9), 0.0)
    return (weighted * scale[:, None]).astype(np.float32)


# -------------------------------------------------------------------- pipeline


class ColorProcessor:
    """Frame + regions -> gamma-encoded RGB per region (0..1)."""

    def __init__(self, settings: SyncSettings) -> None:
        self.settings = settings
        self.letterbox = LetterboxDetector()

    def process(self, frame: Frame, regions: np.ndarray) -> np.ndarray:
        s = self.settings
        img = frame.image
        if frame.hdr:
            tonemap(img, frame.peak)
        else:
            np.clip(img, 0.0, 1.0, out=img)
        bars = self.letterbox.update(img) if s.letterbox_detection else (0.0, 0.0)
        linear = region_means(img, regions, bars, vivid=(s.mode == "vivid"))
        enc = linear_to_srgb(linear)
        amount = s.saturation * (s.hdr_saturation if frame.hdr else 1.0)
        return saturate(enc, amount)


class Smoother:
    """Per-key exponential moving average with a time constant (frame-rate independent)."""

    def __init__(self) -> None:
        self._state: dict[str, np.ndarray] = {}

    def reset(self) -> None:
        self._state.clear()

    def apply(self, key: str, colors: np.ndarray, dt: float, tau: float) -> np.ndarray:
        prev = self._state.get(key)
        if prev is None or prev.shape != colors.shape or tau <= 0:
            out = colors.copy()
        else:
            alpha = 1.0 - np.exp(-max(dt, 0.0) / tau)
            out = prev + (colors - prev) * alpha
        self._state[key] = out
        return out
