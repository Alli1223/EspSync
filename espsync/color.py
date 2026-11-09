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
    m = channel_max(img)
    if float(m.max()) <= knee:
        return img
    c = (1.0 - knee) / (peak - 1.0)
    x = np.clip((m - knee) / (peak - knee), 0.0, 1.0)
    f = knee + (1.0 - knee) * (x * (1.0 + c) / (x + c))
    scale = np.where(m > knee, f / np.maximum(m, 1e-9), 1.0).astype(np.float32)
    img *= scale[..., None]
    return img


def channel_max(img: np.ndarray) -> np.ndarray:
    """Per-pixel max over RGB (much faster than img.max(axis=2) for 3 channels)."""
    return np.maximum(np.maximum(img[..., 0], img[..., 1]), img[..., 2])


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

    def update(
        self, img: np.ndarray, now: float | None = None, mx: np.ndarray | None = None
    ) -> tuple[float, float]:
        now = time.monotonic() if now is None else now
        bright = (channel_max(img) if mx is None else mx) > self.threshold
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


class IntegralImage:
    """Summed-area table of a frame; region means in O(1) per region.

    Built once per captured frame (planar float32, 3 channels for plain
    averaging, 5 for vivid), then any number of regions can be looked up -
    hundreds of LED regions cost about the same as one.
    """

    def __init__(self, img: np.ndarray, vivid: bool, mx: np.ndarray | None = None) -> None:
        h, w = img.shape[:2]
        self.h, self.w, self.vivid = h, w, vivid
        planar = img.transpose(2, 0, 1)
        if vivid:
            mx = channel_max(img) if mx is None else mx
            mn = np.minimum(np.minimum(img[..., 0], img[..., 1]), img[..., 2])
            sat = (mx - mn) / np.maximum(mx, 1e-6)
            weight = 0.05 + sat * np.sqrt(np.minimum(mx, 1.0))
            stack = np.empty((5, h, w), np.float32)
            np.multiply(planar, weight, out=stack[:3])
            stack[3] = weight
            stack[4] = mx
        else:
            stack = np.ascontiguousarray(planar, dtype=np.float32)
        table = np.zeros((stack.shape[0], h + 1, w + 1), np.float32)
        cs = np.cumsum(stack, axis=2)
        np.cumsum(cs, axis=1, out=table[:, 1:, 1:])
        self.table = table

    def means(self, regions: np.ndarray, bars: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
        """Mean linear colour of each region -> (N, 3).

        `regions` is (N, 4) [u0, v0, u1, v1] relative to the *active picture*
        (inside any detected black bars).
        """
        if regions.size == 0:
            return np.zeros((0, 3), np.float32)
        h, w = self.h, self.w
        bv, bh = bars
        r_lo, r_hi = bv * h, h - bv * h
        c_lo, c_hi = bh * w, w - bh * w
        c0 = np.clip(np.floor(c_lo + regions[:, 0] * (c_hi - c_lo)).astype(np.intp), 0, w - 1)
        r0 = np.clip(np.floor(r_lo + regions[:, 1] * (r_hi - r_lo)).astype(np.intp), 0, h - 1)
        c1 = np.ceil(c_lo + regions[:, 2] * (c_hi - c_lo)).astype(np.intp)
        r1 = np.ceil(r_lo + regions[:, 3] * (r_hi - r_lo)).astype(np.intp)
        c1 = np.clip(np.maximum(c1, c0 + 1), 1, w)
        r1 = np.clip(np.maximum(r1, r0 + 1), 1, h)

        t = self.table
        sums = (t[:, r1, c1] - t[:, r0, c1] - t[:, r1, c0] + t[:, r0, c0]).astype(np.float64)
        area = ((r1 - r0) * (c1 - c0)).astype(np.float64)
        if not self.vivid:
            return (sums / area).T.astype(np.float32)

        weighted = (sums[:3] / np.maximum(sums[3], 1e-9)).T
        v_plain = sums[4] / area  # mean brightness of the region
        v_weighted = weighted.max(axis=1)
        # Hue/saturation from the saturation-weighted mean; brightness between
        # the two so a small saturated object on a dark background shows its
        # colour without blasting the light to full brightness.
        v = np.sqrt(np.maximum(v_plain * v_weighted, 0.0))
        scale = np.where(v_weighted > 1e-9, v / np.maximum(v_weighted, 1e-9), 0.0)
        return np.clip(weighted * scale[:, None], 0.0, None).astype(np.float32)


def region_means(
    img: np.ndarray,
    regions: np.ndarray,
    bars: tuple[float, float] = (0.0, 0.0),
    vivid: bool = False,
) -> np.ndarray:
    """Convenience wrapper: mean linear colour per region -> (N, 3)."""
    return IntegralImage(img, vivid).means(regions, bars)


# -------------------------------------------------------------------- pipeline


class ColorProcessor:
    """Frame + regions -> gamma-encoded RGB per region (0..1)."""

    def __init__(self, settings: SyncSettings) -> None:
        self.settings = settings
        self.letterbox = LetterboxDetector()
        self._integral: IntegralImage | None = None
        self._integral_key: tuple | None = None

    def process(self, frame: Frame, regions: np.ndarray) -> np.ndarray:
        s = self.settings
        img = frame.image
        vivid = s.mode == "vivid"
        key = (id(frame), frame.timestamp, vivid)
        if key != self._integral_key:
            if not frame.mapped:
                if frame.hdr:
                    tonemap(img, frame.peak)
                else:
                    np.clip(img, 0.0, 1.0, out=img)
                frame.mapped = True
            mx = channel_max(img)
            if s.letterbox_detection:
                self.letterbox.update(img, mx=mx)
            self._integral = IntegralImage(img, vivid, mx)
            self._integral_key = key
        bars = self.letterbox.bars if s.letterbox_detection else (0.0, 0.0)
        assert self._integral is not None
        linear = self._integral.means(regions, bars)
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
