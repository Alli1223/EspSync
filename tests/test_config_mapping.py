import json

import pytest

from espsync.config import AppConfig, DeviceConfig, LightConfig, RoomConfig, load_config, save_config
from espsync.mapping import (
    SCREEN_PRESETS,
    lamp_region,
    light_regions,
    room_to_screen,
    sample_polyline,
)


def test_config_roundtrip(tmp_path):
    cfg = AppConfig()
    cfg.devices.append(DeviceConfig(name="desk", host="10.0.0.5", encryption_key="abc"))
    cfg.lights.append(LightConfig(name="Left lamp", device_id=cfg.devices[0].id, entity="left", x=-0.9))
    cfg.lights.append(LightConfig(name="TV strip", kind="strip", transport="wled", led_count=60))
    cfg.sync.target_fps = 45
    path = save_config(cfg, tmp_path / "config.json")
    loaded = load_config(path)
    assert loaded == cfg
    assert loaded.light(cfg.lights[0].id).x == -0.9
    assert loaded.device(cfg.devices[0].id).host == "10.0.0.5"


def test_config_ignores_unknown_keys(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"future": 1, "lights": [{"name": "a", "bogus": True}], "sync": {"nope": 2}}))
    cfg = load_config(path)
    assert cfg.lights[0].name == "a"
    assert cfg.sync.target_fps == 60


def test_missing_config_gives_defaults(tmp_path):
    assert load_config(tmp_path / "missing.json") == AppConfig()


def test_room_to_screen_left_right_and_height():
    room = RoomConfig()
    assert room_to_screen(-1, 0, 0.5, room) == (0.0, 0.5)
    assert room_to_screen(1, 0, 0.5, room) == (1.0, 0.5)
    assert room_to_screen(0, 0, 1.0, room) == (0.5, 0.0)  # ceiling -> top
    assert room_to_screen(0, 0, 0.0, room) == (0.5, 1.0)  # floor -> bottom


def test_horizontal_spread_pushes_to_edges():
    room = RoomConfig(horizontal_spread=0.5)
    u, _ = room_to_screen(-0.5, 0, 0.5, room)
    assert u == 0.0


def test_lamp_regions_follow_room_side():
    room = RoomConfig()
    left = lamp_region(LightConfig(x=-1.0, y=-0.8, z=0.5), room)
    right = lamp_region(LightConfig(x=1.0, y=-0.8, z=0.5), room)
    assert left.u0 == 0.0 and left.u1 < 0.5
    assert right.u1 == pytest.approx(1.0) and right.u0 > 0.5


def test_lamp_region_stays_on_screen_and_grows_with_depth():
    room = RoomConfig()
    near = lamp_region(LightConfig(x=0.9, y=-1.0, z=0.0), room)
    far = lamp_region(LightConfig(x=0.9, y=1.0, z=0.0), room)
    for r in (near, far):
        assert 0 <= r.u0 < r.u1 <= 1 and 0 <= r.v0 < r.v1 <= 1
    assert far.width > near.width


def test_manual_region_override():
    light = LightConfig(region=[0.8, 0.9, 0.1, 0.2])
    r = lamp_region(light, RoomConfig())
    assert (r.u0, r.v0, r.u1, r.v1) == (0.1, 0.2, 0.8, 0.9)


def test_polyline_even_spacing():
    pts, spacing = sample_polyline([(0, 1), (1, 1)], 4, aspect=1.0)
    assert [p[0] for p in pts] == pytest.approx([0.125, 0.375, 0.625, 0.875])
    assert spacing == pytest.approx(0.25)


def test_strip_around_tv_regions():
    light = LightConfig(
        kind="strip",
        strip_layout="screen",
        screen_path=SCREEN_PRESETS["Around TV - U shape (left, top, right)"],
        led_count=50,
        region_size=0.1,
    )
    regions = light_regions(light, RoomConfig())
    assert len(regions) == 50
    first, last = regions[0], regions[-1]
    assert first.u0 == 0.0 and first.center[1] > 0.8  # starts bottom-left
    assert last.u1 == pytest.approx(1.0) and last.center[1] > 0.8  # ends bottom-right
    top = regions[25]
    assert top.v0 == 0.0  # middle LEDs along the top edge
    reversed_regions = light_regions(
        LightConfig(**{**light.__dict__, "reverse": True}), RoomConfig()
    )
    assert reversed_regions[0] == last
