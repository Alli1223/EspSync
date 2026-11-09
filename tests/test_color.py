import numpy as np
import pytest

from espsync.capture.base import SRGB_TO_LINEAR, Frame, bgra_to_linear
from espsync.color import (
    ColorProcessor,
    LetterboxDetector,
    Smoother,
    linear_to_srgb,
    region_means,
    saturate,
    srgb_to_linear,
    tonemap,
)
from espsync.config import SyncSettings


def test_srgb_roundtrip():
    x = np.linspace(0, 1, 50, dtype=np.float32)
    assert linear_to_srgb(srgb_to_linear(x)) == pytest.approx(x, abs=1e-5)
    assert SRGB_TO_LINEAR[255] == pytest.approx(1.0)


def test_bgra_to_linear_swaps_channels():
    px = np.array([[[0, 0, 255, 255]]], dtype=np.uint8)  # pure red in BGRA
    assert bgra_to_linear(px)[0, 0].tolist() == pytest.approx([1.0, 0.0, 0.0])


def test_tonemap_preserves_sdr_and_hue():
    img = np.array([[[0.5, 0.25, 0.1], [4.0, 2.0, 0.0], [10.0, 10.0, 10.0]]], dtype=np.float32)
    out = tonemap(img.copy(), peak=4.0)
    assert out[0, 0] == pytest.approx([0.5, 0.25, 0.1])  # below knee untouched
    assert out[0, 1, 0] == pytest.approx(1.0)  # peak maps to 1
    assert out[0, 1, 1] / out[0, 1, 0] == pytest.approx(0.5)  # hue ratio kept
    assert out[0, 2].max() == pytest.approx(1.0)  # above peak clamps
    assert out.max() <= 1.0 + 1e-6


def test_tonemap_is_monotonic_and_continuous():
    m = np.linspace(0, 5, 2001, dtype=np.float32)
    img = np.stack([m, m * 0, m * 0], axis=1)[None]
    out = tonemap(img.copy(), peak=5.0)[0, :, 0]
    assert np.all(np.diff(out) >= -1e-6)
    assert np.max(np.abs(np.diff(out))) < 0.01  # no jumps at the knee


def test_region_means_left_right():
    img = np.zeros((10, 20, 3), np.float32)
    img[:, :10] = [1, 0, 0]
    img[:, 10:] = [0, 0, 1]
    regions = np.array([[0, 0, 0.5, 1], [0.5, 0, 1, 1], [0, 0, 1, 1]], np.float32)
    out = region_means(img, regions)
    assert out[0] == pytest.approx([1, 0, 0])
    assert out[1] == pytest.approx([0, 0, 1])
    assert out[2] == pytest.approx([0.5, 0, 0.5])


def test_region_means_respects_letterbox():
    img = np.zeros((20, 20, 3), np.float32)
    img[4:16] = [0, 1, 0]  # picture between 20% bars
    top = np.array([[0, 0, 1, 0.1]], np.float32)
    assert region_means(img, top)[0].max() == 0.0
    assert region_means(img, top, bars=(0.2, 0.0))[0] == pytest.approx([0, 1, 0])


def test_vivid_prefers_saturated_colour():
    img = np.full((10, 10, 3), 0.3, np.float32)  # grey
    img[:2, :2] = [1.0, 0.0, 0.0]  # small red patch
    full = np.array([[0, 0, 1, 1]], np.float32)
    avg = region_means(img, full)[0]
    vivid = region_means(img, full, vivid=True)[0]
    sat = lambda c: (c.max() - c.min()) / c.max()  # noqa: E731
    assert sat(vivid) > sat(avg) * 2


def test_letterbox_detection_settles():
    det = LetterboxDetector(settle_s=1.0)
    img = np.zeros((100, 100, 3), np.float32)
    img[12:88] = 0.5
    assert det.update(img, now=0.0) == (0.0, 0.0)  # growing bars wait
    assert det.update(img, now=0.5) == (0.0, 0.0)
    assert det.update(img, now=1.1)[0] == pytest.approx(0.12)
    full = np.full((100, 100, 3), 0.5, np.float32)
    assert det.update(full, now=1.2) == (0.0, 0.0)  # shrinking bars apply instantly
    black = np.zeros((100, 100, 3), np.float32)
    assert det.update(black, now=5.0) == (0.0, 0.0)  # black frames keep the crop


def test_saturate():
    grey = np.array([[0.5, 0.5, 0.5]], np.float32)
    assert saturate(grey, 2.0) == pytest.approx(grey)
    c = np.array([[0.6, 0.4, 0.4]], np.float32)
    boosted = saturate(c, 1.5)[0]
    assert boosted[0] - boosted[1] > 0.2


def test_smoother_time_constant():
    sm = Smoother()
    a = np.zeros((1, 3), np.float32)
    b = np.ones((1, 3), np.float32)
    sm.apply("k", a, 0.0, 0.1)
    out = sm.apply("k", b, 0.1, 0.1)  # one time constant -> 63%
    assert out[0, 0] == pytest.approx(1 - np.exp(-1), abs=1e-4)
    assert sm.apply("k", b, 0.01, 0.0)[0, 0] == 1.0  # tau 0 = instant


def test_processor_hdr_frame_stays_in_range():
    img = np.zeros((9, 16, 3), np.float32)
    img[:, :8] = [3.0, 0.5, 0.0]  # bright HDR orange on the left
    frame = Frame(image=img, source_size=(1920, 1080), hdr=True, peak=4.0)
    proc = ColorProcessor(SyncSettings(letterbox_detection=False))
    out = proc.process(frame, np.array([[0, 0, 0.5, 1], [0.5, 0, 1, 1]], np.float32))
    assert out.max() <= 1.0
    assert out[0, 0] > 0.9 and out[0, 2] < 0.1
    assert out[1].max() == 0.0
