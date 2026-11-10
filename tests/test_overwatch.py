import time

import numpy as np
import pytest

from espsync import overwatch as ow
from espsync import overwatch_effects as fx
from espsync.capture.base import Frame, clip_rect
from espsync.config import AppConfig, LightConfig, SyncSettings

SIZES = [(1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160), (2560, 1080), (1280, 720)]


def lock(size, health=0.8, **kwargs):
    """A reader that has locked on to a bar drawn with these settings."""
    reader = ow.HudReader()
    img = ow.render_hud(size, health, **kwargs)
    seen = [reader.read(img, size, i * 0.2) for i in range(ow.LOCK_HITS)]
    return reader, seen


# ------------------------------------------------------------------- reader
@pytest.mark.parametrize("size", SIZES)
def test_reader_locks_on_and_reads_health(size):
    reader, seen = lock(size)
    assert seen[:-1] == [None] * (ow.LOCK_HITS - 1)  # nothing is reported before the lock
    assert seen[-1] is not None and seen[-1].health == pytest.approx(0.8, abs=0.02)
    for health in (1.0, 0.76, 0.5, 0.31, 0.12, 0.05):
        sig = reader.read(ow.render_hud(size, health), size, 5.0)
        assert sig is not None and sig.health == pytest.approx(health, abs=0.02)


def test_probe_rect_scales_with_the_screen_height_and_fits_the_bar():
    for size in SIZES:
        x, y, w, h = ow.probe_rect(size)
        assert clip_rect((x, y, w, h), *size) == (x, y, w, h)
        for left in (ow.BAR_X_16_9, ow.BAR_X_21_9):
            assert x < left * size[1] and (left + ow.BAR_WIDTH) * size[1] < x + w


@pytest.mark.parametrize("segments", [6, 8, 10, 19, 24, 28])
def test_reader_copes_with_any_number_of_segments(segments):
    for size in ((3440, 1440), (1920, 1080)):
        reader, seen = lock(size, 0.62, segments=segments)
        assert seen[-1] is not None and seen[-1].health == pytest.approx(0.62, abs=0.03)


def test_armour_and_shields_count_and_fresh_damage_does_not():
    size = (3440, 1440)
    reader, seen = lock(size, 0.4, armour=0.2, shields=0.2, recent_damage=0.1, segments=19)
    assert seen[-1] is not None and seen[-1].health == pytest.approx(0.8, abs=0.02)
    # everything just lost: only the red is left
    sig = reader.read(ow.render_hud(size, 0.0, recent_damage=0.2), size, 5.0)
    assert sig is not None and sig.health == pytest.approx(0.2, abs=0.02)


@pytest.mark.parametrize("level", [0.3, 1.0, 2.5])
def test_reader_is_independent_of_hud_brightness(level):
    """HDR: the white of the HUD is not at SDR white."""
    reader, seen = lock((3440, 1440), 0.55, level=level)
    assert seen[-1] is not None and seen[-1].health == pytest.approx(0.55, abs=0.02)


@pytest.mark.parametrize("background", [(0.02, 0.02, 0.02), (0.5, 0.5, 0.6), (0.127, 0.35, 0.79), (0.6, 0.1, 0.05)])
def test_scene_behind_missing_segments_is_not_health(background):
    """Sky and fire show through the missing part of the bar."""
    reader, seen = lock((3440, 1440), 0.6, background=background)
    assert seen[-1] is not None and seen[-1].health == pytest.approx(0.6, abs=0.02)


def test_reader_reports_nothing_without_a_bar():
    size = (3440, 1440)
    shape = ow.render_hud(size, 1.0).shape
    rng = np.random.default_rng(3)
    pictures = [
        np.zeros(shape, np.float32),
        np.full(shape, 0.9, np.float32),
        rng.random(shape).astype(np.float32),
        ow.render_hud(size, 0.8, background=(1.0, 1.0, 1.0)),  # white on white: no way to tell
    ]
    for img in pictures:
        reader = ow.HudReader()
        assert [reader.read(img, size, i * 0.2) for i in range(6)] == [None] * 6
    reader = ow.HudReader()
    assert reader.read(None, size, 0.0) is None
    assert reader.read(np.zeros((10, 10, 3), np.float32), size, 0.0) is None  # not the probe it asked for


def test_a_level_bar_is_not_the_hud():
    """Menus have white bars too, but the one in the HUD is tilted."""
    reader, seen = lock((3440, 1440), 0.8, slope=0.0)
    assert seen == [None] * ow.LOCK_HITS and reader.lock is None


def test_reader_needs_the_same_place_several_times_and_enough_health_to_lock():
    size = (1920, 1080)
    reader = ow.HudReader()
    assert reader.read(ow.render_hud(size, 0.8), size, 0.0) is None
    assert reader.read(ow.render_hud(size, 0.8, x_left=0.3), size, 0.2) is None  # somewhere else: start over
    assert reader.read(ow.render_hud(size, 0.8, x_left=0.3), size, 0.4) is None
    assert reader.read(ow.render_hud(size, 0.8, x_left=0.3), size, 0.6) is not None
    reader = ow.HudReader()
    low = ow.render_hud(size, ow.LOCK_MIN_FILL - 0.1)
    assert [reader.read(low, size, i * 0.2) for i in range(6)] == [None] * 6


def test_locked_reader_ignores_other_bars_and_notices_the_hud_going():
    size = (2560, 1440)
    reader, _ = lock(size)
    place = reader.lock
    assert reader.read(np.full(ow.render_hud(size, 1.0).shape, 0.3, np.float32), size, 5.0) is None
    assert reader.lock == place  # still locked: the HUD comes back in the same place
    sig = reader.read(ow.render_hud(size, 0.42), size, 5.01)
    assert sig is not None and sig.health == pytest.approx(0.42, abs=0.02)
    # a new resolution starts over
    assert reader.read(ow.render_hud((1920, 1080), 0.9), (1920, 1080), 6.0) is None
    assert reader.lock is None


def test_reader_survives_noise_and_blur():
    size = (2560, 1440)
    rng = np.random.default_rng(5)
    reader, _ = lock(size)
    img = ow.render_hud(size, 0.45, background=(0.2, 0.25, 0.3))
    img = (img + np.roll(img, 1, axis=1) + np.roll(img, 1, axis=0)) / 3.0  # a soft upscale
    img = np.clip(img + rng.normal(0, 0.02, img.shape), 0, None).astype(np.float32)
    sig = reader.read(img, size, 5.0)
    assert sig is not None and sig.health == pytest.approx(0.45, abs=0.03)


def test_locked_read_is_fast():
    size = (3440, 1440)
    reader, _ = lock(size)
    img = ow.render_hud(size, 0.7)
    t0 = time.perf_counter()
    for i in range(50):
        reader.read(img, size, 10.0 + i)
    assert (time.perf_counter() - t0) / 50 < 0.01  # it runs on the capture thread, every frame (~0.6 ms here)


# ------------------------------------------------------------------ effects
def sig(health):
    return ow.OverwatchSignal(health, 100, 30)


LAMPS = [
    (LightConfig(id="front", x=0.0, y=-0.9), np.array([[0.0, -0.9]], np.float32)),
    (LightConfig(id="back", x=0.0, y=0.9), np.array([[0.0, 0.9]], np.float32)),
]
BASE = {"front": np.array([[0.1, 0.4, 0.1]], np.float32), "back": np.array([[0.1, 0.4, 0.1]], np.float32)}


def feed(e, health, t0, seconds=0.2, step=1 / 60):
    """Readings of `health` for a while; returns the time afterwards."""
    t = t0
    while t < t0 + seconds:
        e.observe(None if health is None else sig(health), t)
        t += step
    return t


def redness(colors, light="front"):
    c = colors[light][0]
    return float(c[0] - c[1])


def test_heartbeat_is_a_double_beat_that_rests():
    beats = [fx.heartbeat(p / 200) for p in range(200)]
    peaks = [i for i in range(1, 199) if beats[i] > beats[i - 1] and beats[i] >= beats[i + 1] and beats[i] > 0.3]
    assert len(peaks) == 2 and beats[peaks[0]] > beats[peaks[1]]
    assert max(beats[100:]) < 0.01


def test_damage_flashes_red_in_proportion_and_fades():
    s = SyncSettings()
    peaks = []
    for hit in (0.1, 0.4):
        e = fx.OverwatchEffects()
        t = feed(e, 1.0, 0.0)
        assert e.apply(BASE, LAMPS, s, t) is BASE  # healthy: untouched
        t = feed(e, 1.0 - hit, t, 0.1)
        peaks.append(redness(e.apply(BASE, LAMPS, s, t)))
        assert redness(e.apply(BASE, LAMPS, s, t + 1.0)) == pytest.approx(redness(BASE), abs=0.01)
    assert peaks[1] > peaks[0] > redness(BASE) + 0.2
    np.testing.assert_array_equal(BASE["front"], np.array([[0.1, 0.4, 0.1]], np.float32))  # inputs untouched


def test_a_single_misread_frame_is_not_damage():
    e = fx.OverwatchEffects()
    t = feed(e, 0.9, 0.0)
    e.observe(sig(0.3), t)  # one frame
    t = feed(e, 0.9, t + 1 / 60)
    assert e.health == pytest.approx(0.9)
    assert e.apply(BASE, LAMPS, SyncSettings(), t) is BASE


def test_small_changes_follow_quietly():
    e = fx.OverwatchEffects()
    t = feed(e, 0.9, 0.0)
    for step in range(1, 30):  # a slow drain, 1% at a time
        t = feed(e, 0.9 - step * 0.01, t, 0.05)
    assert e.health == pytest.approx(0.61, abs=0.03)


def test_critical_health_beats_faster_as_it_drops():
    s = SyncSettings(ow_low_health=0.3)
    rates = []
    for health in (0.28, 0.05):
        e = fx.OverwatchEffects()
        t = feed(e, health, 0.0)
        e.apply(BASE, LAMPS, s, t)
        values = []
        for i in range(1, 361):
            e.observe(sig(health), t + i / 60)
            values.append(redness(e.apply(BASE, LAMPS, s, t + i / 60)))
        strong = [v > 0.5 * max(values) for v in values[120:]]
        rates.append(sum(1 for a, b in zip(strong, strong[1:], strict=False) if b and not a))
        assert max(values) > redness(BASE) + 0.3 and min(values[120:]) < redness(BASE) + 0.05
    assert rates[1] > rates[0] >= 2
    e = fx.OverwatchEffects()
    t = feed(e, 0.31, 0.0)
    assert e.apply(BASE, LAMPS, s, t) is BASE  # not critical yet


def test_healing_rolls_from_the_back_of_the_room_to_the_screen():
    e = fx.OverwatchEffects()
    s = SyncSettings()
    t = feed(e, 0.4, 0.0)
    t = feed(e, 0.8, t, 0.1)
    start = e._heal[1]

    def gold(light, dt):
        c = e.apply(BASE, LAMPS, s, start + dt)[light][0]
        return float(c[0] - BASE[light][0, 0])

    assert gold("back", 0.12) > gold("front", 0.12) + 0.1
    assert gold("front", fx.HEAL_S * 0.85) > gold("back", fx.HEAL_S * 0.85) + 0.1
    assert e.apply(BASE, LAMPS, s, start + fx.HEAL_S + 0.1) is BASE


def test_bar_gone_right_after_damage_is_an_elimination_then_a_respawn():
    e = fx.OverwatchEffects()
    s = SyncSettings()
    t = feed(e, 1.0, 0.0)
    t = feed(e, 0.5, t, 0.2)
    died = t
    t = feed(e, None, t, 1.0)
    assert e.dead and e.status_text() == "eliminated"
    flash = e.apply(BASE, LAMPS, s, died + 0.05)["front"][0]
    assert flash[0] > 0.6 and flash[1] < 0.2
    washed = e.apply(BASE, LAMPS, s, died + 3.0)["front"][0]
    assert washed.max() < 0.35 and washed[0] > washed[1]  # dim, grey-red
    # back in the fight: a cold burst from the screen wall, then normal
    e.observe(sig(1.0), died + 8.0)
    assert not e.dead and e.health == 1.0
    burst = e.apply(BASE, LAMPS, s, died + 8.1)
    assert burst["front"][0, 2] > BASE["front"][0, 2] + 0.3 and burst["front"][0, 2] > burst["back"][0, 2]
    assert e.apply(BASE, LAMPS, s, died + 8.0 + fx.RESPAWN_S + 0.1) is BASE


def test_leaving_to_a_menu_at_full_health_is_not_a_death():
    e = fx.OverwatchEffects()
    s = SyncSettings()
    t = feed(e, 1.0, 0.0, 3.0)
    t = feed(e, None, t, 1.0)
    assert not e.dead and e.apply(BASE, LAMPS, s, t) is BASE
    assert e.status_text() == "health bar not visible"
    t = feed(e, None, t, fx.LOST_AFTER_S)
    assert e.health is None  # forgotten: the next reading is not compared with the old one
    e.observe(sig(0.5), t)
    assert e.apply(BASE, LAMPS, s, t + 0.01) is BASE


def test_bar_gone_while_critical_is_an_elimination():
    e = fx.OverwatchEffects()
    s = SyncSettings(ow_low_health=0.3)
    t = feed(e, 0.2, 0.0, 3.0)  # no fresh damage, but nearly dead
    for i in range(60):
        e.observe(None, t + i / 60)
        e.apply(BASE, LAMPS, s, t + i / 60)
    assert e.dead


def test_a_stuck_elimination_gives_the_lights_back():
    e = fx.OverwatchEffects()
    s = SyncSettings()
    t = feed(e, 1.0, 0.0)
    t = feed(e, 0.6, t)
    t = feed(e, None, t, 1.0)
    assert e.dead
    late = t + fx.DEATH_HOLD_S + fx.DEATH_FADE_S + 1.0
    e.apply(BASE, LAMPS, s, late)
    assert not e.dead and e.apply(BASE, LAMPS, s, late + 0.1) is BASE


def test_effect_colours_respect_brightness_and_gains():
    e = fx.OverwatchEffects()
    e.test_damage(0.0, 1.0)
    dim = [(LightConfig(id="front", brightness=0.5, gains=[1.0, 1.0, 0.0]), LAMPS[0][1])]
    out = e.apply({"front": np.zeros((1, 3), np.float32)}, dim, SyncSettings(brightness=0.8), 0.001)
    assert out["front"][0, 0] == pytest.approx(fx.FLASH_MAX * 0.4, rel=0.05) and out["front"][0, 2] == 0.0


def test_test_buttons():
    s = SyncSettings()
    e = fx.OverwatchEffects()
    e.test_elimination(0.0, seconds=2.0)
    assert e.dead and e.apply(BASE, LAMPS, s, 1.5)["front"][0].max() < 0.35
    burst = e.apply(BASE, LAMPS, s, 2.1)
    assert not e.dead and burst["front"][0, 2] > 0.4
    e = fx.OverwatchEffects()
    e.test_low_health(0.0, 0.05, seconds=2.0)
    reds = [redness(e.apply(BASE, LAMPS, s, i / 60)) for i in range(1, 120)]
    assert max(reds) > 0.5
    e = fx.OverwatchEffects()
    e.test_heal(0.0)
    assert e.apply(BASE, LAMPS, s, 0.1)["back"][0, 0] > 0.3


# ----------------------------------------------------------- engine wiring
class FakeOverwatchBackend:
    name = "fake"
    size = (1920, 1080)

    def __init__(self):
        self.probe_rect = None
        self.health = 1.0
        self.hud = True
        self.requests = []

    def grab(self, timeout_ms=100):
        time.sleep(0.005)
        self.requests.append(self.probe_rect)
        image = np.full((90, 160, 3), 0.05, np.float32)
        probe = None
        if self.probe_rect:
            assert self.probe_rect == ow.probe_rect(self.size)
            probe = ow.render_hud(self.size, self.health) if self.hud else np.full(
                (self.probe_rect[3], self.probe_rect[2], 3), 0.05, np.float32)
        return Frame(image=image, source_size=self.size, probe=probe)

    def close(self):
        pass


@pytest.fixture
def ow_engine(monkeypatch):
    import espsync.engine as engine_mod

    backend = FakeOverwatchBackend()
    monkeypatch.setattr(engine_mod, "create_backend", lambda settings: backend)

    class Watcher:
        game = "overwatch.exe"  # which game "runs"

        def __init__(self, names, *args, **kwargs):
            self.names = names

        @property
        def running(self):
            return Watcher.game in self.names

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(engine_mod, "ProcessWatcher", Watcher)
    cfg = AppConfig(
        lights=[LightConfig(id="lamp", transport="virtual", x=0.0, y=-0.9)],
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


def test_engine_reads_health_and_flashes_on_damage(ow_engine):
    engine, backend, _ = ow_engine
    engine.start(send=False)
    assert wait_for(lambda: engine.overwatch_effects.health is not None)
    assert engine.overwatch_effects.health == pytest.approx(1.0, abs=0.02)
    assert wait_for(lambda: engine.stats.game == "Overwatch: health 100%")
    calm = engine.colors()["lamp"][0].copy()
    backend.health = 0.5
    assert wait_for(lambda: engine.colors()["lamp"][0, 0] > calm[0] + 0.4, timeout=1.5)  # red flash
    assert wait_for(lambda: engine.overwatch_effects.health == pytest.approx(0.5, abs=0.02))
    backend.hud = False  # ... and then the HUD goes: eliminated
    assert wait_for(lambda: engine.overwatch_effects.dead)
    assert wait_for(lambda: engine.stats.game == "Overwatch: eliminated")


def test_engine_leaves_the_screen_alone_without_the_game_or_with_effects_off(ow_engine):
    engine, backend, watcher = ow_engine
    watcher.game = ""
    engine.start(send=False)
    assert wait_for(lambda: "lamp" in engine.colors())
    time.sleep(0.15)
    assert set(backend.requests) == {None} and engine.stats.game == ""
    engine.stop()
    watcher.game = "overwatch.exe"
    engine.config.sync.ow_effects = False
    backend.requests.clear()
    engine.start(send=False)
    assert wait_for(lambda: len(backend.requests) > 10)
    assert set(backend.requests) == {None} and engine.overwatch_effects.health is None


def test_engine_test_buttons_work_without_the_game(ow_engine):
    engine, _backend, watcher = ow_engine
    watcher.game = ""
    engine.start(send=False)
    assert wait_for(lambda: "lamp" in engine.colors())
    engine.test_overwatch("damage")
    assert wait_for(lambda: engine.colors()["lamp"][0, 0] > 0.4, timeout=1.0)
    engine.test_overwatch("elimination")
    assert wait_for(lambda: engine.overwatch_effects.dead, timeout=1.0)


def test_settings_round_trip_and_panel(app):
    cfg = AppConfig(sync=SyncSettings(ow_effects=False, ow_low_health=0.2))
    back = AppConfig.from_dict(cfg.to_dict())
    assert back.sync.ow_effects is False and back.sync.ow_low_health == 0.2
    assert AppConfig.from_dict({}).sync.ow_effects is True

    from espsync.gui.settings_panel import SettingsPanel

    cfg = AppConfig()
    panel = SettingsPanel(cfg)
    tests = []
    panel.testEffectRequested.connect(tests.append)
    assert panel.ow.isChecked() and panel.ow_threshold.value() == 30
    panel.ow_threshold.setValue(45)
    assert cfg.sync.ow_low_health == pytest.approx(0.45)
    panel.ow.setChecked(False)
    assert cfg.sync.ow_effects is False and not panel.ow_threshold.isEnabled()
    for button in panel.ow_tests.values():
        button.click()
    assert tests == ["ow:damage", "ow:heal", "ow:lowhealth", "ow:elimination"]
    assert [title for title, _ in panel.pages] == ["Picture", "Games", "Room"]
    panel.deleteLater()
