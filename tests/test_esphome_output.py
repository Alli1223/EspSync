import asyncio

import numpy as np
import pytest
from aioesphomeapi import LightInfo, LightState

from espsync.config import DeviceConfig, LightConfig, SyncSettings
from espsync.lights.esphome import ApiLightOutput, EspHomeDevice


class FakeClient:
    def __init__(self):
        self.calls = []

    def light_command(self, key, **kwargs):
        self.calls.append((key, kwargs))


def make_device(connected=True):
    dev = EspHomeDevice(DeviceConfig(name="dev", host="127.0.0.1"))
    dev.client = FakeClient()
    dev.lights = {"lamp": LightInfo(object_id="lamp", key=7, name="Lamp", supported_color_modes=[11, 35])}
    if connected:
        dev.connected.set()
    return dev


ORIGINAL = LightState(key=7, state=True, brightness=0.4, color_mode=11, color_temperature=370.0)


def test_sends_normalised_rgb_with_zero_transition_and_restores():
    async def run():
        dev = make_device()
        dev._on_state(ORIGINAL)  # state the light had before sync
        out = ApiLightOutput(LightConfig(entity="lamp"), SyncSettings(min_brightness=0.0), dev)
        await out.start()
        assert out.ready
        out.submit(np.array([[0.5, 0.25, 0.0]], np.float32))
        # The ESP echoes our command back as a state; it must not replace the original.
        dev._on_state(LightState(key=7, state=True, brightness=0.5, color_mode=35, red=1, green=0.5))
        await out.stop(restore=True)
        return dev.client.calls

    calls = asyncio.run(run())
    key, sent = calls[0]
    assert key == 7
    assert sent["state"] is True
    assert sent["brightness"] == pytest.approx(0.5, abs=0.01)
    assert sent["rgb"] == pytest.approx((1.0, 0.5, 0.0), abs=0.01)
    assert sent["color_mode"] == 35
    assert sent["transition_length"] == 0.0
    _, restored = calls[-1]
    assert restored["color_mode"] == 11
    assert restored["brightness"] == pytest.approx(0.4)
    assert restored["color_temperature"] == pytest.approx(370.0)


def test_black_floor_and_turn_off():
    async def run(settings):
        dev = make_device()
        out = ApiLightOutput(LightConfig(entity="lamp"), settings, dev)
        await out.start()
        out.submit(np.zeros((1, 3), np.float32))
        return dev.client.calls

    floor = asyncio.run(run(SyncSettings(min_brightness=0.05)))
    assert floor[0][1]["state"] is True and floor[0][1]["brightness"] == pytest.approx(0.05)
    off = asyncio.run(run(SyncSettings(black_turns_off=True)))
    assert off[0][1]["state"] is False


def test_device_coming_online_later_is_picked_up():
    async def run():
        dev = make_device(connected=False)

        async def not_yet(timeout):
            return False

        dev.wait_connected = not_yet
        out = ApiLightOutput(LightConfig(entity="lamp"), SyncSettings(), dev)
        await out.start()
        assert out.status == "connecting" and not out.ready
        out.submit(np.array([[1, 0, 0]], np.float32))
        assert dev.client.calls == []
        dev.connected.set()  # reconnect logic got through
        await asyncio.sleep(0.05)
        out.submit(np.array([[0, 1, 0]], np.float32))
        return out, dev.client.calls

    out, calls = asyncio.run(run())
    assert out.status == "ready"
    assert calls and calls[-1][1]["rgb"] == pytest.approx((0.0, 1.0, 0.0))


def test_running_effect_is_switched_off_and_restored():
    async def run():
        dev = make_device()
        dev.lights["lamp"] = LightInfo(object_id="lamp", key=7, name="Lamp", supported_color_modes=[35],
                                       effects=["Rainbow", "Pulse"])
        dev._on_state(LightState(key=7, state=True, brightness=0.8, color_mode=35, effect="Rainbow"))
        out = ApiLightOutput(LightConfig(entity="lamp"), SyncSettings(min_brightness=0.0), dev)
        await out.start()

        def frame(rgb):
            out._last_send = -1e9  # each frame gets its own command (no rate-limit merging)
            out.submit(np.array([rgb], np.float32))

        frame([1.0, 0.0, 0.0])
        dev._on_state(LightState(key=7, state=True, brightness=1.0, color_mode=35, effect="None"))  # ESP echo
        frame([0.0, 1.0, 0.0])
        # Home Assistant switches an effect on mid-sync: switched off again.
        dev._on_state(LightState(key=7, state=True, brightness=1.0, color_mode=35, effect="Pulse"))
        frame([0.0, 0.0, 1.0])
        await out.stop(restore=True)
        return dev.client.calls

    calls = asyncio.run(run())
    first, second, third, restored = (c[1] for c in calls)
    assert first["effect"] == "None" and first["rgb"] == pytest.approx((1.0, 0.0, 0.0))
    assert "effect" not in second
    assert third["effect"] == "None"
    assert restored["effect"] == "Rainbow"  # the pre-sync effect comes back on stop


def test_light_without_effects_gets_no_effect_field():
    async def run():
        dev = make_device()
        out = ApiLightOutput(LightConfig(entity="lamp"), SyncSettings(), dev)
        await out.start()
        out.submit(np.array([[1.0, 0.0, 0.0]], np.float32))
        return dev.client.calls

    (_, sent), = asyncio.run(run())
    assert "effect" not in sent


def test_unknown_entity_reports_error():
    async def run():
        dev = make_device()
        out = ApiLightOutput(LightConfig(entity="nope"), SyncSettings(), dev)
        await out.start()
        return out

    out = asyncio.run(run())
    assert out.status == "error" and "nope" in out.status_text()
