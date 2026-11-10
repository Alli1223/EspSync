"""World of Warcraft integration: strip decoder, effect maths, engine wiring."""

import math
import threading
import time

import numpy as np
import pytest

from espsync import effects as fx
from espsync import wow
from espsync.capture.base import SRGB_TO_LINEAR, Frame, bgra_to_linear
from espsync.color import linear_to_srgb, srgb_to_linear
from espsync.config import AppConfig, LightConfig, RoomConfig, SyncSettings
from espsync.wow import PROBE_SIZE, WowSignal, decode_signal, hide_strip, render_strip


def corner(strip: np.ndarray, background: float | np.ndarray = 0.05, size=PROBE_SIZE, y: int = 0) -> np.ndarray:
    """A full-resolution top-left corner (linear light) with `strip` drawn at (0, y)."""
    w, h = size
    img = np.empty((h, w, 3), np.float32)
    img[...] = background
    sh, sw = strip.shape[:2]
    img[y : y + sh, :sw] = strip
    return img


def linear_strip(*args, **kwargs) -> np.ndarray:
    """render_strip() gives sRGB-encoded UI colours; the capture is linear light."""
    return srgb_to_linear(render_strip(*args, **kwargs))


def resample(img: np.ndarray, scale: float) -> np.ndarray:
    """Box-filter resize (area average), like a GPU/compositor scaling the game."""
    h, w = img.shape[:2]
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    ys = np.linspace(0, h, nh + 1)
    xs = np.linspace(0, w, nw + 1)
    out = np.zeros((nh, nw, 3), np.float32)
    # integrate with fine supersampling: exact enough for a test
    fine = np.repeat(np.repeat(img, 8, axis=0), 8, axis=1)
    for i in range(nh):
        for j in range(nw):
            block = fine[int(ys[i] * 8) : max(int(ys[i + 1] * 8), int(ys[i] * 8) + 1),
                         int(xs[j] * 8) : max(int(xs[j + 1] * 8), int(xs[j] * 8) + 1)]
            out[i, j] = block.reshape(-1, 3).mean(axis=0)
    return out


# ----------------------------------------------------------------- decoder
@pytest.mark.parametrize("health", [0.0, 0.01, 0.2, 0.349, 0.5, 0.77, 1.0])
def test_decoder_reads_health_to_one_pixel(health):
    sig = decode_signal(corner(linear_strip(health, levelups=3)))
    assert sig is not None
    assert sig.health == pytest.approx(health, abs=1 / 64 + 1e-6)
    assert sig.cell == pytest.approx(wow.CELL_PX)


@pytest.mark.parametrize("levelups", range(16))
@pytest.mark.parametrize(("dead", "test"), [(False, False), (True, False), (False, True), (True, True)])
def test_decoder_reads_every_counter_value_and_flag(levelups, dead, test):
    sig = decode_signal(corner(linear_strip(0.5, levelups, dead, test)))
    assert sig is not None
    assert (sig.levelups, sig.dead, sig.test) == (levelups, dead, test)


def test_counter_wraps_mod_16():
    assert wow.encode_cells(16) == wow.encode_cells(0)
    assert decode_signal(corner(linear_strip(1.0, levelups=17))).levelups == 1


@pytest.mark.parametrize("ui_nits", [80.0, 160.0, 240.0, 480.0, 1000.0])
def test_decoder_survives_the_dxgi_hdr_path(ui_nits):
    """HDR desktop: DXGI hands over FP16 scRGB, which the backend divides by
    Windows' SDR white (240 nits here).  The game draws its UI at its own
    brightness, so the strip may come out far dimmer or brighter than 1.0."""
    white_nits = 240.0
    scrgb = linear_strip(0.3, levelups=7) * (ui_nits / 80.0)
    captured = (scrgb.astype(np.float16).astype(np.float32)) * (80.0 / white_nits)
    sig = decode_signal(corner(captured, background=0.4))
    assert sig is not None
    assert sig.levelups == 7 and sig.health == pytest.approx(0.3, abs=1 / 64)


def test_decoder_survives_the_dxgi_sdr_path():
    """SDR desktop: 8-bit BGRA through the backend's sRGB lookup table."""
    enc = (render_strip(0.62, levelups=2) * 255 + 0.5).astype(np.uint8)
    bgra = np.concatenate([enc[..., ::-1], np.full(enc.shape[:2] + (1,), 255, np.uint8)], axis=2)
    sig = decode_signal(corner(bgra_to_linear(bgra)))
    assert sig is not None and sig.levelups == 2
    assert sig.health == pytest.approx(0.62, abs=1 / 64)


@pytest.mark.parametrize(
    "transform",
    [
        lambda x: x ** 1.4,  # game gamma slider
        lambda x: x * 0.85 + 0.03,  # lifted blacks / contrast
        lambda x: x[..., [0, 1, 2]] @ np.array([[0.9, 0.05, 0.05], [0.05, 0.9, 0.05], [0.05, 0.05, 0.9]], np.float32),
    ],
    ids=["gamma", "lifted-blacks", "channel-crosstalk"],
)
def test_decoder_tolerates_colour_changes(transform):
    strip = transform(linear_strip(0.25, levelups=9, dead=False)).astype(np.float32)
    sig = decode_signal(corner(strip))
    assert sig is not None and sig.levelups == 9
    assert sig.health == pytest.approx(0.25, abs=2 / 64)


def test_decoder_tolerates_noise():
    rng = np.random.default_rng(1)
    strip = linear_strip(0.4, levelups=5) + rng.normal(0, 0.03, (4, 88, 3)).astype(np.float32)
    sig = decode_signal(corner(np.clip(strip, 0, None)))
    assert sig is not None and sig.levelups == 5 and sig.health == pytest.approx(0.4, abs=2 / 64)


@pytest.mark.parametrize("scale", [1.5, 2.0, 2.5])
def test_decoder_handles_a_scaled_strip(scale):
    """The game rendered below the display resolution and scaled up."""
    strip = resample(linear_strip(0.6, levelups=11), scale)
    sig = decode_signal(corner(strip))
    assert sig is not None and sig.levelups == 11
    assert sig.cell == pytest.approx(wow.CELL_PX * scale, abs=0.3)
    assert sig.health == pytest.approx(0.6, abs=0.03)


def test_decoder_finds_a_strip_one_pixel_down():
    sig = decode_signal(corner(linear_strip(0.5, levelups=1), y=1))
    assert sig is not None and sig.levelups == 1


def test_downscaled_frame_is_too_small_which_is_why_backends_send_a_probe():
    """DXGI's mip readback (1/16 on a 3440 px wide screen) blurs the 4 px strip."""
    full = corner(linear_strip(0.5, levelups=1), size=(256, 64))
    assert decode_signal(full) is not None
    assert decode_signal(resample(full, 1 / 16)) is None


@pytest.mark.parametrize(
    "image",
    [
        None,
        np.zeros((16, 256, 3), np.float32),
        np.ones((16, 256, 3), np.float32),
        np.random.default_rng(0).random((16, 256, 3), dtype=np.float32),
        np.zeros((16, 20, 3), np.float32),  # too narrow
        corner(np.tile(np.array([1, 0, 1], np.float32), (4, 88, 1))),  # all magenta
    ],
    ids=["none", "black", "white", "noise", "narrow", "magenta"],
)
def test_decoder_rejects_non_strips(image):
    assert decode_signal(image) is None


def test_decoder_rejects_a_bad_check_cell():
    strip = render_strip(0.5, levelups=4)
    strip[:, 2 * 4 : 3 * 4] = wow.code_rgb(wow.encode_cells(5)[0])  # data cell from another value
    assert decode_signal(corner(srgb_to_linear(strip))) is None


def test_decoder_rejects_a_missing_end_marker():
    strip = render_strip(0.5)
    strip[:, -4:] = 0.0
    assert decode_signal(corner(srgb_to_linear(strip))) is None


def test_decoder_rejects_a_bar_that_is_not_filled_from_the_left():
    strip = render_strip(0.5)
    strip[:, 20:40] = wow.code_rgb(wow.BLUE)
    strip[:, 40:60] = wow.code_rgb(wow.YELLOW)
    assert decode_signal(corner(srgb_to_linear(strip))) is None


def test_decoder_is_fast():
    img = corner(linear_strip(0.5))
    t0 = time.perf_counter()
    for _ in range(200):
        decode_signal(img)
    assert (time.perf_counter() - t0) / 200 < 0.002  # well under a frame


def test_hide_strip_paints_over_the_strip_in_a_downscaled_frame():
    image = np.full((90, 215, 3), 0.2, np.float32)
    image[0, :6] = (0.5, 0.1, 0.5)  # what a 1/16 mip of the strip leaves behind
    image[1:, :] = 0.2
    sig = WowSignal(health=1.0, levelups=0, dead=False, test=False, cell=4.0)
    hide_strip(image, (3440, 1440), sig)
    np.testing.assert_allclose(image, 0.2)


# ---------------------------------------------------------------- pulse maths
def test_no_pulse_at_or_above_the_threshold():
    for h in (0.35, 0.5, 1.0):
        assert fx.pulse_rate(h, 0.35) == 0 and fx.pulse_strength(h, 0.35) == 0
    assert fx.pulse_rate(0.1, 0.0) == 0  # threshold 0 = off


def test_pulse_gets_faster_and_stronger_as_health_drops():
    healths = np.linspace(0.349, 0.0, 30)
    rates = [fx.pulse_rate(h, 0.35) for h in healths]
    strengths = [fx.pulse_strength(h, 0.35) for h in healths]
    assert all(b > a for a, b in zip(rates, rates[1:]))
    assert all(b > a for a, b in zip(strengths, strengths[1:]))
    assert rates[0] == pytest.approx(fx.PULSE_MIN_HZ, abs=0.01)
    assert rates[-1] == pytest.approx(fx.PULSE_MAX_HZ)
    assert strengths[-1] == pytest.approx(fx.PULSE_MAX_MIX)
    assert fx.pulse_rate(0.175, 0.35) == pytest.approx((fx.PULSE_MIN_HZ + fx.PULSE_MAX_HZ) / 2)


def test_pulse_wave_shape():
    assert fx.pulse_wave(0.0) == pytest.approx(0.0)
    assert fx.pulse_wave(0.5) == pytest.approx(1.0)
    assert fx.pulse_wave(1.0) == pytest.approx(0.0, abs=1e-12)
    assert fx.pulse_wave(0.25) == pytest.approx(fx.pulse_wave(0.75))


# ---------------------------------------------------------------- sweep maths
def test_sweep_starts_at_the_screen_and_turns_at_constant_speed():
    assert fx.sweep_angle(0.0) == pytest.approx(-math.pi / 2)
    turns = (fx.sweep_angle(fx.SWEEP_S) - fx.sweep_angle(0.0)) / (2 * math.pi)
    assert turns == pytest.approx(fx.SWEEP_TURNS)
    a = [fx.sweep_angle(t) for t in np.linspace(0, fx.SWEEP_S, 7)]
    steps = np.diff(a)
    np.testing.assert_allclose(steps, steps[0])
    # quarter turn later it points at the right wall (+x)
    t_quarter = fx.SWEEP_S / fx.SWEEP_TURNS / 4
    assert math.cos(fx.sweep_angle(t_quarter)) == pytest.approx(1.0)


def test_wedge_weight():
    w = fx.wedge_weight(np.array([0.0, 0.3, -0.3, fx.WEDGE_HALF_ANGLE, math.pi]), 0.0)
    assert w[0] == pytest.approx(1.0)
    assert w[1] == pytest.approx(w[2]) and 0 < w[1] < 1
    assert w[3] == pytest.approx(0.0, abs=1e-6) and w[4] == pytest.approx(0.0, abs=1e-6)
    # wraps round +-pi
    assert fx.wedge_weight(np.array([math.pi - 0.1]), -math.pi + 0.1)[0] > 0.9


def test_wedge_reaches_lights_in_angle_order():
    # screen (-90 deg), right wall (0), back (90), left wall (180)
    angles = np.array([-math.pi / 2, 0.0, math.pi / 2, math.pi], np.float32)
    turn = fx.SWEEP_S / fx.SWEEP_TURNS
    ts = np.linspace(0, turn, 400, endpoint=False)  # first turn
    weights = np.array([fx.wedge_weight(angles, fx.sweep_angle(t)) for t in ts])
    peak_times = ts[np.argmax(weights, axis=0)]
    np.testing.assert_allclose(peak_times, [0, turn / 4, turn / 2, 3 * turn / 4], atol=turn / 200)
    # and the colour mix follows the wedge once it has faded in
    mixes = np.array([fx.levelup_mix(angles, t) for t in ts])
    assert mixes[200, 2] > 0.95 and mixes[200, 0] < 0.3  # half a turn: back lit, screen dim


def test_levelup_glows_then_fades_and_ends():
    angles = np.array([0.0, 2.0], np.float32)
    end_of_sweep = fx.levelup_mix(angles, fx.SWEEP_S)
    assert np.all(end_of_sweep >= fx.GLOW_PEAK - 1e-6)  # whole room gold
    half = fx.levelup_mix(angles, fx.SWEEP_S + fx.GLOW_S / 2)
    assert np.all(half < end_of_sweep) and np.all(half > 0)
    assert np.all(fx.levelup_mix(angles, fx.SWEEP_S + fx.GLOW_S * 0.999) < 0.01)
    assert fx.levelup_mix(angles, fx.SWEEP_S + fx.GLOW_S) is None
    assert np.all(fx.levelup_mix(angles, 0.0) == 0)  # fades in


# ---------------------------------------------------------- light positions
def test_element_angles_for_lamps_and_strips():
    room = RoomConfig(width_m=4, depth_m=4)
    lamp = LightConfig(x=1.0, y=0.0)
    assert fx.element_angles(lamp, room)[0] == pytest.approx(0.0)
    strip = LightConfig(kind="strip", led_count=4, x=-1.0, y=-1.0, x2=1.0, y2=-1.0)
    a = fx.element_angles(strip, room)
    assert len(a) == 4 and a[0] < -math.pi / 2 < a[-1]  # left of the screen -> right
    strip.reverse = True
    np.testing.assert_allclose(fx.element_angles(strip, room), a[::-1])
    tv = LightConfig(kind="strip", led_count=10, strip_layout="screen", screen_path=[[0, 1], [1, 1]])
    pos = fx.element_positions(tv, room)
    assert pos.shape == (10, 2) and np.all(pos[:, 1] == -1.0)
    assert pos[0, 0] == pytest.approx(-room.tv_width, abs=0.05) and pos[-1, 0] > 0


def test_element_angles_use_metres():
    wide = RoomConfig(width_m=8, depth_m=2)
    lamp = LightConfig(x=1.0, y=1.0)  # back-right corner
    assert fx.element_angles(lamp, wide)[0] == pytest.approx(math.atan2(2, 8))


# ------------------------------------------------------------ effect state
def sig(health=1.0, levelups=0, dead=False, test=False):
    return WowSignal(health=health, levelups=levelups, dead=dead, test=test, cell=4.0)


def lamp_setup():
    light = LightConfig(id="a", x=0.0, y=-1.0)
    return light, [(light, fx.element_angles(light, RoomConfig()))]


def test_first_sight_does_not_count_as_a_level_up():
    e = fx.GameEffects()
    e.observe(sig(levelups=5), 0.0)
    assert not e.levelup_running
    e.observe(sig(levelups=6), 0.1)
    assert e.levelup_running


def test_counter_wrap_is_a_level_up_but_a_reset_after_reload_is_not():
    e = fx.GameEffects()
    e.observe(sig(levelups=15), 0.0)
    e.observe(sig(levelups=0), 0.1)
    assert e.levelup_running
    e = fx.GameEffects()
    e.observe(sig(levelups=7), 0.0)
    e.observe(None, 0.1)  # loading screen for /reload ...
    e.observe(None, 2.0)
    assert e.signal is None
    e.observe(sig(levelups=0), 2.1)  # ... counter starts over
    assert not e.levelup_running


def test_a_brief_gap_keeps_the_signal():
    e = fx.GameEffects()
    e.observe(sig(health=0.2), 0.0)
    e.observe(None, 0.1)
    e.observe(None, 0.5)
    assert e.signal is not None
    e.observe(None, 0.1 + fx.LOST_AFTER_S + 0.01)
    assert e.signal is None


def run_effects(e, light, lights, base, seconds, fps=60, t0=0.0):
    out = []
    for i in range(int(seconds * fps)):
        t = t0 + i / fps
        res = e.apply({light.id: base}, lights, SyncSettings(), t)
        out.append(res[light.id][0].copy())
    return np.array(out)


def measured_rate(series, fps=60):
    red = series[:, 0] - series[:, 1]
    peaks = [i for i in range(1, len(red) - 1) if red[i] > red[i - 1] and red[i] >= red[i + 1] and red[i] > 0.1]
    return (len(peaks) - 1) / ((peaks[-1] - peaks[0]) / fps)


def test_pulse_rate_in_the_output_follows_health():
    light, lights = lamp_setup()
    base = np.array([[0.2, 0.4, 0.6]], np.float32)
    rates = {}
    for h in (0.3, 0.05):
        e = fx.GameEffects()
        e.observe(sig(health=h), 0.0)
        series = run_effects(e, light, lights, base, 6.0)
        rates[h] = measured_rate(series[60:])  # skip the fade-in
        assert rates[h] == pytest.approx(fx.pulse_rate(h, 0.35), rel=0.05)
        # strength: peak share of red approaches the target
        peak_mix = (series[60:, 0] - base[0, 0]) / (1 - base[0, 0])
        assert peak_mix.max() == pytest.approx(fx.pulse_strength(h, 0.35), abs=0.03)
    assert rates[0.05] > rates[0.3]


def test_no_pulse_when_healthy_or_dead_and_inputs_are_untouched():
    light, lights = lamp_setup()
    base = np.array([[0.2, 0.4, 0.6]], np.float32)
    for s in (sig(health=0.9), sig(health=0.0, dead=True)):
        e = fx.GameEffects()
        e.observe(s, 0.0)
        series = run_effects(e, light, lights, base, 2.0)
        np.testing.assert_allclose(series, np.repeat(base, len(series), axis=0))
    np.testing.assert_allclose(base, [[0.2, 0.4, 0.6]])


def test_pulse_fades_out_when_health_recovers():
    light, lights = lamp_setup()
    base = np.zeros((1, 3), np.float32)
    e = fx.GameEffects()
    e.observe(sig(health=0.1), 0.0)
    run_effects(e, light, lights, base, 2.0)
    e.observe(sig(health=1.0), 2.0)
    later = run_effects(e, light, lights, base, 2.0, t0=2.0)
    assert later[-1][0] < 0.01  # gone after a couple of seconds, no hard cut needed


def test_effect_colours_respect_brightness_and_gains():
    light = LightConfig(id="a", brightness=0.5, gains=[1.0, 1.0, 0.5])
    lights = [(light, fx.element_angles(light, RoomConfig()))]
    e = fx.GameEffects()
    e.trigger_levelup(0.0)
    res = e.apply({"a": np.zeros((1, 3), np.float32)}, lights, SyncSettings(), fx.SWEEP_S)
    np.testing.assert_allclose(res["a"][0], fx.GOLD * np.array([0.5, 0.5, 0.25]) * res["a"][0][0] / (0.5 * fx.GOLD[0]))
    assert res["a"][0][0] <= 0.5 + 1e-6


def test_strip_elements_get_their_own_sweep_weight():
    strip = LightConfig(id="s", kind="strip", led_count=8, x=-1.0, y=0.0, x2=1.0, y2=0.0)
    lights = [(strip, fx.element_angles(strip, RoomConfig()))]
    e = fx.GameEffects()
    e.trigger_levelup(0.0)
    t = fx.SWEEP_S / fx.SWEEP_TURNS / 4  # wedge points at the right wall
    res = e.apply({"s": np.zeros((8, 3), np.float32)}, lights, SyncSettings(), t)["s"]
    assert res[-1, 0] > 0.9 and res[0, 0] < 0.5  # right end lit, left end only tinted


def test_status_text():
    e = fx.GameEffects()
    assert e.status_text() == "addon not seen"
    e.observe(sig(health=0.42, test=True), 0.0)
    assert e.status_text() == "health 42% (test)"
    e.observe(sig(dead=True), 0.1)
    assert e.status_text() == "dead"


# ----------------------------------------------------------- engine wiring
class FakeGameBackend:
    """A 1920x1080 'screen': flat colour, WoW's strip in the corner.

    The downscaled image carries the smear the strip leaves in a GPU
    downscale; the probe holds the strip at full resolution when asked for.
    """

    name = "fake"
    background = np.array([0.05, 0.3, 0.05], np.float32)

    def __init__(self) -> None:
        self.probe_rect = None
        self.health = 1.0
        self.levelups = 0
        self.probe_requests: list = []

    def grab(self, timeout_ms=100):
        time.sleep(0.01)
        self.probe_requests.append(self.probe_rect)
        image = np.empty((90, 160, 3), np.float32)
        image[...] = self.background
        image[0, :8] = 0.75 * self.background + 0.25 * np.array([1, 0, 1], np.float32)
        probe = None
        if self.probe_rect:
            assert self.probe_rect[:2] == (0, 0)
            probe = corner(linear_strip(self.health, self.levelups), background=self.background,
                           size=self.probe_rect[2:])
        return Frame(image=image, source_size=(1920, 1080), probe=probe)

    def close(self):
        pass


@pytest.fixture
def game_engine(monkeypatch):
    import espsync.engine as engine_mod

    backend = FakeGameBackend()
    monkeypatch.setattr(engine_mod, "create_backend", lambda settings: backend)

    class Watcher:
        running = True

        def __init__(self, names, *args, **kwargs):
            if "overwatch.exe" in names:
                self.running = False  # only WoW "runs" here; the class attribute is WoW's
            else:
                assert "wow.exe" in names and "wowclassic.exe" in names

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(engine_mod, "ProcessWatcher", Watcher)
    cfg = AppConfig(
        lights=[LightConfig(id="corner", transport="virtual", region=[0.0, 0.0, 0.08, 0.08], x=-0.9, y=-0.9)],
        sync=SyncSettings(smoothing_ms=0, mode="average", saturation=1.0),
    )
    engine = engine_mod.SyncEngine(cfg)
    yield engine, backend, Watcher
    engine.shutdown()


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def background_srgb():
    return linear_to_srgb(FakeGameBackend.background[None])[0]


def test_engine_keeps_the_strip_out_of_the_light_colours(game_engine):
    engine, backend, _ = game_engine
    engine.start(send=False)
    assert wait_for(lambda: engine.game_effects.signal is not None)
    assert wait_for(lambda: "corner" in engine.colors())
    time.sleep(0.1)
    np.testing.assert_allclose(engine.colors()["corner"][0], background_srgb(), atol=1e-4)
    assert backend.probe_requests[-1] == (0, 0, *PROBE_SIZE)


def test_engine_ignores_the_corner_while_the_game_is_not_running(game_engine):
    engine, backend, watcher = game_engine
    watcher.running = False
    engine.start(send=False)
    assert wait_for(lambda: "corner" in engine.colors())
    time.sleep(0.1)
    assert engine.game_effects.signal is None
    assert set(backend.probe_requests) == {None}  # no extra copy per frame
    # without the game the corner is sampled as it is (a little magenta here)
    assert np.abs(engine.colors()["corner"][0] - background_srgb()).max() > 1e-3


def test_engine_pulses_red_at_low_health(game_engine):
    engine, backend, _ = game_engine
    backend.health = 0.1
    engine.start(send=False)
    assert wait_for(lambda: engine.game_effects.signal is not None)
    reds = []
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        c = engine.colors().get("corner")
        if c is not None:
            reds.append(float(c[0, 0] - c[0, 1]))
        time.sleep(0.01)
    base = background_srgb()
    assert max(reds) > 0.3  # red takes over at the peak ...
    assert min(reds) < (base[0] - base[1]) + 0.05  # ... and lets go in between
    assert wait_for(lambda: engine.stats.game.startswith("WoW: health "))
    assert float(engine.stats.game.split()[-1].rstrip("%")) == pytest.approx(10, abs=1.6)


def test_engine_plays_the_sweep_when_the_counter_changes(game_engine):
    engine, backend, _ = game_engine
    engine.start(send=False)
    assert wait_for(lambda: engine.game_effects.signal is not None)
    assert not engine.game_effects.levelup_running
    backend.levelups = 1
    assert wait_for(lambda: engine.game_effects.levelup_running, timeout=1.0)
    gold = []
    end = time.monotonic() + fx.SWEEP_S
    while time.monotonic() < end:
        gold.append(engine.colors()["corner"][0].copy())
        time.sleep(0.02)
    gold = np.array(gold)
    assert gold[:, 0].max() > 0.8 and gold[:, 0].max() > gold[:, 2].max() + 0.5  # gold, not white


def test_engine_test_buttons_work_without_the_game(game_engine):
    engine, backend, watcher = game_engine
    watcher.running = False
    engine.start(send=False)
    assert wait_for(lambda: "corner" in engine.colors())
    engine.test_levelup()
    assert wait_for(lambda: engine.colors()["corner"][0, 0] > 0.8, timeout=fx.SWEEP_S)


def test_settings_round_trip():
    cfg = AppConfig(sync=SyncSettings(wow_effects=False, wow_low_health=0.2))
    back = AppConfig.from_dict(cfg.to_dict())
    assert back.sync.wow_effects is False and back.sync.wow_low_health == 0.2
    assert AppConfig.from_dict({}).sync.wow_effects is True


def test_settings_panel_game_effects(app):
    from espsync.gui.settings_panel import SettingsPanel

    cfg = AppConfig()
    panel = SettingsPanel(cfg)
    seen = []
    panel.changed.connect(seen.append)
    tests = []
    panel.testEffectRequested.connect(tests.append)
    assert panel.wow.isChecked() and panel.wow_threshold.value() == 35
    panel.wow_threshold.setValue(20)
    assert cfg.sync.wow_low_health == pytest.approx(0.2) and seen[-1] == "sync"
    panel.wow.setChecked(False)
    assert cfg.sync.wow_effects is False and not panel.wow_threshold.isEnabled()
    panel.test_levelup.click()
    panel.test_low_health.click()
    assert tests == ["levelup", "lowhealth"]
    panel.deleteLater()


def test_process_watcher_polls_in_the_background():
    from espsync.procwatch import ProcessWatcher

    names = [{"explorer.exe"}, {"explorer.exe", "wow.exe"}]
    calls = threading.Semaphore(0)

    def lister():
        calls.release()
        return names[0] if len(names) == 1 else names.pop(0)

    w = ProcessWatcher({"Wow.exe"}, interval=0.01, lister=lister)
    assert not w.running
    w.start()
    try:
        assert wait_for(lambda: w.running)
    finally:
        w.stop()
    assert not w.running


def test_process_watcher_survives_listing_errors():
    from espsync.procwatch import ProcessWatcher

    def broken():
        raise OSError("nope")

    assert ProcessWatcher({"wow.exe"}, lister=broken).check() is False


def test_process_names_lists_this_process():
    import os
    import sys

    from espsync.procwatch import process_names

    names = process_names()
    if sys.platform == "win32":
        assert os.path.basename(sys.executable).lower() in names
    elif os.path.isdir("/proc"):
        assert names


def test_process_listing_declares_its_ctypes_types_once():
    # It runs every 2 s on a background thread: new ctypes classes per call
    # leaked into ctypes' pointer-type cache and left the rest to the GC.
    import ctypes
    import sys

    if sys.platform != "win32":
        pytest.skip("Toolhelp32 is Windows-only")
    from espsync import procwatch

    procwatch.process_names()
    api = procwatch._toolhelp_api()
    cached = len(ctypes._pointer_type_cache)
    for _ in range(5):
        procwatch.process_names()
    assert procwatch._toolhelp_api() is api
    assert len(ctypes._pointer_type_cache) == cached


def test_srgb_lut_matches_the_colour_module():
    # the tests build strips with color.srgb_to_linear; backends use the LUT
    np.testing.assert_allclose(SRGB_TO_LINEAR[[0, 255]], srgb_to_linear(np.array([0.0, 1.0])), atol=1e-6)
