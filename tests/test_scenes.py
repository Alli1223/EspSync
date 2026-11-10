import asyncio
from types import SimpleNamespace

import pytest

from espsync import scenes
from espsync.config import AppConfig, DeviceConfig, HueBridgeConfig, LightConfig
from espsync.hue import HueArea, HueChannel, HueError

WARM = scenes.scene_by_id("warm")
RELAX = scenes.scene_by_id("relax")
OFF = scenes.scene_by_id("off")
CT, CWWW, RGB_CT, RGB_CWWW, RGBW, WHITE, RGB, BRIGHTNESS = 11, 19, 47, 51, 39, 7, 35, 3


def info(*modes, effects=(), lo=153.0, hi=500.0):
    return SimpleNamespace(key=1, device_id=0, supported_color_modes=list(modes), effects=list(effects),
                           min_mireds=lo, max_mireds=hi)


def test_scene_list():
    ids = [s.id for s in scenes.SCENES]
    assert ids[0] == "warm" and "off" in ids and len(set(ids)) == len(ids)
    assert scenes.scene_by_id("nope") is None
    assert WARM.mireds == pytest.approx(370.4, abs=0.1)


def test_kelvin_to_rgb_goes_from_amber_to_blue():
    warm, neutral, cold = (scenes.kelvin_to_rgb(k) for k in (2000, 6500, 10000))
    assert warm[0] == 1.0 and warm[0] > warm[1] > warm[2]
    assert max(neutral) - min(neutral) < 0.06
    assert cold[2] == 1.0 and cold[2] > cold[0]
    x, y = scenes.kelvin_to_xy(2700)
    assert (x, y) == pytest.approx((0.46, 0.41), abs=0.03)  # on the black-body curve


def test_esphome_uses_colour_temperature_when_the_light_has_it():
    cmd = scenes.esphome_command(info(RGB, CT), RELAX)
    assert cmd["state"] is True and cmd["brightness"] == pytest.approx(0.55)
    assert cmd["color_mode"] == CT and cmd["color_temperature"] == pytest.approx(RELAX.mireds)
    assert "rgb" not in cmd and "effect" not in cmd
    # clamped to what the light can do
    assert scenes.esphome_command(info(CT, lo=250.0, hi=400.0), RELAX)["color_temperature"] == 400.0


def test_esphome_white_channels():
    cmd = scenes.esphome_command(info(RGB_CWWW), WARM)
    assert cmd["color_mode"] == RGB_CWWW and cmd["color_brightness"] == 0.0
    assert cmd["warm_white"] == 1.0 and 0.0 < cmd["cold_white"] < 1.0
    cmd = scenes.esphome_command(info(CWWW), scenes.scene_by_id("daylight"))
    assert cmd["cold_white"] == 1.0 and cmd["warm_white"] < 0.2
    cmd = scenes.esphome_command(info(RGB_CT), WARM)
    assert cmd["white"] == 1.0 and cmd["color_brightness"] == 0.0 and "color_temperature" in cmd
    cmd = scenes.esphome_command(info(RGBW), WARM)
    assert cmd["white"] == 1.0 and cmd["color_brightness"] == 0.0 and "rgb" not in cmd


def test_esphome_rgb_only_light_gets_the_nearest_colour_and_loses_its_effect():
    cmd = scenes.esphome_command(info(RGB, effects=["WLED", "Rainbow"]), WARM)
    assert cmd["color_mode"] == RGB and cmd["rgb"] == WARM.rgb and cmd["color_brightness"] == 1.0
    assert cmd["effect"] == "None"
    r, g, b = cmd["rgb"]
    assert r == 1.0 and r > g > b > 0.2


def test_esphome_plain_lights_and_off():
    assert scenes.esphome_command(info(BRIGHTNESS), RELAX) == {
        "transition_length": scenes.FADE_S, "device_id": 0, "state": True, "brightness": pytest.approx(0.55)}
    off = scenes.esphome_command(info(RGB, CT, effects=["WLED"]), OFF)
    assert off == {"transition_length": scenes.FADE_S, "device_id": 0, "state": False}


def test_hue_bodies():
    body = scenes.hue_body(WARM)
    assert body["on"] == {"on": True} and body["dimming"] == {"brightness": 100.0}
    assert body["color_temperature"] == {"mirek": 370}
    assert scenes.hue_body(scenes.scene_by_id("night"))["color_temperature"] == {"mirek": 500}
    assert scenes.hue_body(scenes.scene_by_id("night"))["dimming"] == {"brightness": 10.0}
    colour = scenes.hue_body(WARM, colour_temperature=False)
    assert "color_temperature" not in colour and set(colour["color"]["xy"]) == {"x", "y"}
    assert scenes.hue_body(OFF) == {"on": {"on": False}, "dynamics": {"duration": 600}}


def config():
    return AppConfig(
        devices=[DeviceConfig(id="d1", name="Desk", host="desk.local")],
        hue_bridges=[HueBridgeConfig(id="b1", name="Bridge", host="10.0.0.2", app_key="k", client_key="c",
                                     entertainment_id="area")],
        lights=[
            LightConfig(name="lamp", transport="api", device_id="d1", entity="lamp"),
            LightConfig(name="strip", kind="strip", transport="wled", device_id="d1", entity="strip"),
            LightConfig(name="unused", transport="api", device_id="d1", entity="spare", enabled=False),
            LightConfig(name="hue a", transport="hue", hue_bridge="b1", hue_channels=[0]),
            LightConfig(name="hue gradient", kind="strip", transport="hue", hue_bridge="b1", hue_channels=[1, 2]),
            LightConfig(name="preview", transport="virtual"),
            LightConfig(name="bare strip", kind="strip", transport="e131", udp_host="10.0.0.9"),
        ],
    )


def test_targets_are_the_enabled_lights_grouped_by_device():
    esphome, hue, unreachable = scenes.scene_targets(config())
    assert esphome == {"d1": {"lamp", "strip"}}
    assert hue == {"b1": {0, 1, 2}}
    assert unreachable == 1  # the E1.31 strip with only an address


def test_apply_scene_sends_one_command_per_light(monkeypatch):
    sent = []

    async def fake_esphome(dev, entities, scene):
        sent.append(("esphome", dev.name, sorted(entities), scene.id))
        return len(entities), []

    class FakeApi:
        def __init__(self, host, key):
            self.puts = []
            sent.append(("hue", host, self.puts))

        async def area(self, area_id, with_names=False):
            assert area_id == "area" and with_names
            return HueArea(id="area", name="TV", channels=[
                HueChannel(0, 0, 0, 0, light_id="L1"),
                HueChannel(1, 0, 0, 0, light_id="L2"),
                HueChannel(2, 0, 0, 0, light_id="L2"),  # two segments of one gradient strip
                HueChannel(3, 0, 0, 0, light_id="L3"),  # not one of ours
            ])

        async def put_light(self, light_id, body):
            if light_id == "L2" and "color_temperature" in body:
                raise HueError("colour temperature not supported")
            self.puts.append((light_id, body))

    import espsync.hue as hue_mod

    monkeypatch.setattr(scenes, "_apply_esphome", fake_esphome)
    monkeypatch.setattr(hue_mod, "HueApi", FakeApi)
    result = asyncio.run(scenes.apply_scene(config(), WARM))
    assert result.applied == 4  # two ESPHome lights, two Hue lights
    assert result.errors == ["1 light(s) have no device to send a scene to"]
    assert ("esphome", "Desk", ["lamp", "strip"], "warm") in sent
    puts = next(s[2] for s in sent if s[0] == "hue")
    assert [p[0] for p in puts] == ["L1", "L2"]
    assert "color_temperature" in puts[0][1] and "color" in puts[1][1]  # L2 fell back to an xy colour
    assert "Warm white: set 4 lights" in result.summary(WARM)


def test_apply_scene_reports_unreachable_devices(monkeypatch):
    async def refuse(*args, **kwargs):
        raise OSError("no route to host")

    from aioesphomeapi import APIClient

    monkeypatch.setattr(APIClient, "connect", refuse)
    cfg = config()
    cfg.lights = cfg.lights[:1]
    cfg.hue_bridges = []
    result = asyncio.run(scenes.apply_scene(cfg, WARM))
    assert result.applied == 0 and len(result.errors) == 1 and result.errors[0].startswith("Desk: could not reach")
    empty = asyncio.run(scenes.apply_scene(AppConfig(), WARM))
    assert empty.summary(WARM).startswith("No lights to set")


def test_cli_lists_scenes(capsys):
    from espsync.cli import main

    assert main(["scene"]) == 0
    out = capsys.readouterr().out
    assert "warm" in out and "2700 K" in out and "lights off" in out
