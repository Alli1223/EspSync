"""Philips Hue: packet builder, position mapping, area parsing, config, stream logic."""

import asyncio
import json
import struct

import numpy as np
import pytest

import espsync.hue as hue
import espsync.lights.hue as hue_out
from espsync.config import AppConfig, HueBridgeConfig, LightConfig, SyncSettings
from espsync.hue import (
    HueArea,
    HueChannel,
    HueError,
    LinkButtonNotPressed,
    hue_to_room,
    lights_for_area,
    parse_area,
    restore_payload,
    room_to_hue,
)
from espsync.lights.hue import HueEntertainmentStream, HueLightOutput, huestream_packet, to_rgb16

AREA_ID = "1a8d99cc-967b-44f2-9202-43f976c0fa6b"


# --------------------------------------------------------------------- packets
def test_huestream_packet_layout():
    pkt = huestream_packet(AREA_ID, [(0, b"\xff\xff\x00\x00\x12\x34"), (7, b"\x00\x01\x00\x02\x00\x03")], 5)
    assert pkt[:9] == b"HueStream"
    assert pkt[9:11] == b"\x02\x00"  # version 2.0
    assert pkt[11] == 5  # sequence
    assert pkt[12:14] == b"\x00\x00"  # reserved
    assert pkt[14] == 0x00  # colour space RGB
    assert pkt[15] == 0x00  # reserved
    assert pkt[16:52] == AREA_ID.encode()
    assert len(pkt) == 52 + 2 * 7
    assert pkt[52] == 0 and struct.unpack(">HHH", pkt[53:59]) == (0xFFFF, 0, 0x1234)
    assert pkt[59] == 7 and struct.unpack(">HHH", pkt[60:66]) == (1, 2, 3)


def test_huestream_packet_rejects_bad_area_id():
    with pytest.raises(ValueError):
        huestream_packet("not-a-uuid", [])


def test_to_rgb16_scales_and_clips():
    out = to_rgb16(np.array([[1.0, 0.5, 0.0], [2.0, -1.0, 1 / 65535]]))
    assert [struct.unpack(">HHH", b) for b in out] == [(65535, 32768, 0), (65535, 0, 1)]


# --------------------------------------------------------------------- mapping
@pytest.mark.parametrize(
    "hue_pos, room",
    [
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.5)),  # centre of the room, mid-height
        ((-1.0, 1.0, -1.0), (-1.0, -1.0, 0.0)),  # front (TV) left, floor
        ((1.0, -1.0, 1.0), (1.0, 1.0, 1.0)),  # back right, ceiling
        ((0.5, 0.8, 0.2), (0.5, -0.8, 0.6)),  # right of the TV, a bit above centre
        ((3.0, -3.0, 5.0), (1.0, 1.0, 1.0)),  # clamped
    ],
)
def test_hue_to_room(hue_pos, room):
    assert hue_to_room(*hue_pos) == pytest.approx(room)


def test_room_to_hue_inverts():
    for pos in [(-0.3, 0.7, 0.25), (0.9, -1.0, 1.0)]:
        assert hue_to_room(*room_to_hue(*pos)) == pytest.approx(pos)


# --------------------------------------------------------------- area parsing
def _raw_area(status="inactive", streamer=None):
    raw = {
        "id": AREA_ID,
        "type": "entertainment_configuration",
        "metadata": {"name": "TV area"},
        "configuration_type": "screen",
        "status": status,
        "light_services": [{"rid": "light-1", "rtype": "light"}, {"rid": "light-2", "rtype": "light"}],
        "channels": [
            {"channel_id": 0, "position": {"x": -0.8, "y": 0.8, "z": 0.0},
             "members": [{"service": {"rid": "svc-lamp", "rtype": "entertainment"}, "index": 0}]},
            {"channel_id": 1, "position": {"x": -0.4, "y": 1.0, "z": 0.4},
             "members": [{"service": {"rid": "svc-grad", "rtype": "entertainment"}, "index": 0}]},
            {"channel_id": 3, "position": {"x": 0.4, "y": 1.0, "z": 0.4},
             "members": [{"service": {"rid": "svc-grad", "rtype": "entertainment"}, "index": 4}]},
            {"channel_id": 2, "position": {"x": 0.0, "y": 1.0, "z": 0.6},
             "members": [{"service": {"rid": "svc-grad", "rtype": "entertainment"}, "index": 1}]},
        ],
    }
    if streamer:
        raw["active_streamer"] = {"rid": streamer, "rtype": "auth_v1"}
    return raw


NAMES = {"svc-lamp": ("Floor lamp", "light-1"), "svc-grad": ("Play gradient", "light-2")}


def test_parse_area_names_segments_and_order():
    area = parse_area(_raw_area("active", "other-app"), NAMES)
    assert (area.id, area.name, area.kind, area.status, area.streamer) == (
        AREA_ID, "TV area", "screen", "active", "other-app")
    assert [c.channel_id for c in area.channels] == [0, 1, 2, 3]
    assert area.light_ids == ["light-1", "light-2"]
    lamp, g0, g1, g2 = area.channels
    assert lamp.label == "Floor lamp" and lamp.segments == 1 and lamp.light_id == "light-1"
    # Member index 4 is the light's 5th segment, but the 3rd of the three in this area.
    assert (g0.label, g1.label, g2.label) == ("Play gradient (1/3)", "Play gradient (2/3)", "Play gradient (3/3)")
    assert (g2.index, g2.ordinal) == (4, 2)
    assert (g0.x, g0.y, g0.z) == (-0.4, 1.0, 0.4)


def test_lights_for_area_one_lamp_per_channel():
    bridge = HueBridgeConfig(name="Bridge")
    lights = lights_for_area(bridge, parse_area(_raw_area(), NAMES))
    assert [l.hue_channels for l in lights] == [[0], [1], [2], [3]]
    assert all(l.transport == "hue" and l.kind == "lamp" and l.hue_bridge == bridge.id for l in lights)
    lamp = lights[0]
    assert lamp.name == "Floor lamp" and (lamp.x, lamp.y, lamp.z) == pytest.approx((-0.8, -0.8, 0.5))


def test_lights_for_area_groups_gradient_into_strip():
    lights = lights_for_area(HueBridgeConfig(), parse_area(_raw_area(), NAMES), group_gradients=True)
    assert len(lights) == 2
    strip = lights[1]
    assert strip.kind == "strip" and strip.name == "Play gradient"
    assert strip.hue_channels == [1, 2, 3]  # ordered by segment index, not channel id
    assert strip.led_count == 3 and strip.strip_layout == "screen" and len(strip.screen_path) == 3
    (u0, v0), (u1, v1), (u2, v2) = strip.screen_path
    assert u0 < u1 < u2 and v1 < v0  # left to right, middle segment higher up


# ---------------------------------------------------------------------- config
def test_config_round_trip_with_hue():
    bridge = HueBridgeConfig(name="Hue Bridge", host="10.0.0.2", bridge_id="001788fffe000000", app_key="k" * 40,
                             client_key="ab" * 16, entertainment_id=AREA_ID, entertainment_name="TV area")
    light = LightConfig(name="Lamp", transport="hue", hue_bridge=bridge.id, hue_channels=[3, 4])
    cfg = AppConfig(hue_bridges=[bridge], lights=[light])
    cfg.sync.hue_rate_hz = 40.0
    back = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert back == cfg
    assert back.hue_bridge(bridge.id).paired and back.lights[0].hue_channels == [3, 4]


def test_old_config_without_hue_still_loads():
    old = {"devices": [{"id": "d1", "name": "ESP", "host": "esp.local"}],
           "lights": [{"id": "l1", "name": "Lamp", "transport": "api", "device_id": "d1"}],
           "sync": {"target_fps": 50}, "version": 1}
    cfg = AppConfig.from_dict(old)
    assert cfg.hue_bridges == [] and cfg.lights[0].hue_channels == [] and cfg.lights[0].hue_bridge == ""
    assert cfg.sync.hue_rate_hz == 50.0 and cfg.sync.target_fps == 50


# --------------------------------------------------------------------- pairing
def _fake_request(responses):
    calls = []

    async def request(host, method, path, body=None, key="", timeout=6.0):
        calls.append((method, path, body))
        return responses.pop(0) if len(responses) > 1 else responses[0]

    return request, calls


def test_pair_once_success_and_link_button(monkeypatch):
    req, calls = _fake_request([(200, {}, [{"error": {"type": 101, "description": "link button not pressed"}}])])
    monkeypatch.setattr(hue, "request", req)
    with pytest.raises(LinkButtonNotPressed):
        asyncio.run(hue.pair_once("h"))
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/api") and body["generateclientkey"] is True
    assert body["devicetype"].startswith("espsync#") and len(body["devicetype"]) <= 28

    req, _ = _fake_request([(200, {}, [{"success": {"username": "user", "clientkey": "AB" * 16}}])])
    monkeypatch.setattr(hue, "request", req)
    assert asyncio.run(hue.pair_once("h")) == ("user", "AB" * 16)


def test_pair_polls_until_pressed(monkeypatch):
    req, calls = _fake_request([
        (200, {}, [{"error": {"type": 101}}]),
        (200, {}, [{"success": {"username": "u", "clientkey": "00" * 16}}]),
    ])
    monkeypatch.setattr(hue, "request", req)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(hue.asyncio, "sleep", no_sleep)
    waits = []
    assert asyncio.run(hue.pair("h", timeout=30, on_wait=waits.append)) == ("u", "00" * 16)
    assert len(calls) == 2 and len(waits) == 1


def test_api_maps_errors(monkeypatch):
    req, _ = _fake_request([(403, {}, {"errors": [{"description": "unauthorized user"}], "data": []})])
    monkeypatch.setattr(hue, "request", req)
    with pytest.raises(hue.HueAuthError):
        asyncio.run(hue.HueApi("h", "k").get("light"))
    req, _ = _fake_request([(400, {}, {"errors": [{"description": "invalid action"}]})])
    monkeypatch.setattr(hue, "request", req)
    with pytest.raises(HueError, match="invalid action"):
        asyncio.run(hue.HueApi("h", "k").set_streaming(AREA_ID, True))


# ---------------------------------------------------------------------- stream
class FakeApi:
    def __init__(self, area: HueArea, app_id: str = "me") -> None:
        self._area = area
        self.app_id = app_id
        self.streaming_calls: list[bool] = []
        self.lights = {lid: {"id": lid, "on": {"on": True}, "dimming": {"brightness": 40.0},
                             "color": {"xy": {"x": 0.3, "y": 0.3}}} for lid in area.light_ids}
        self.snapshots = 0
        self.others: list[HueArea] = []  # other areas on the bridge
        self.puts: list[tuple[str, dict]] = []
        self.log: list[str] = []

    async def light_states(self, ids):
        self.snapshots += 1
        return {i: self.lights[i] for i in ids if i in self.lights}

    async def put_light(self, light_id, body):
        self.log.append(f"put {light_id}")
        self.puts.append((light_id, body))

    async def application_id(self) -> str:
        return self.app_id

    async def area(self, area_id: str, with_names: bool = False) -> HueArea:
        return self._area

    async def areas(self, with_names: bool = True) -> list[HueArea]:
        return [self._area, *self.others]

    async def set_streaming(self, area_id: str, active: bool) -> None:
        self.log.append(f"streaming {active}")
        self.streaming_calls.append(active)
        self._area.status = "active" if active else "inactive"
        self._area.streamer = self.app_id if active else ""


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


def _area(channels=(0, 1, 2), status="inactive", streamer=""):
    return HueArea(id=AREA_ID, name="TV area", status=status, streamer=streamer,
                   channels=[HueChannel(c, 0, 0, 0, name=f"L{c}") for c in channels],
                   light_ids=[f"light-{c}" for c in channels])


def _bridge():
    return HueBridgeConfig(name="Bridge", host="127.0.0.1", app_key="user", client_key="00" * 16,
                           entertainment_id=AREA_ID)


def _stream(api, rate=50.0):
    transports: list[FakeTransport] = []

    async def connector(stream):
        t = FakeTransport()
        transports.append(t)
        return t

    s = HueEntertainmentStream(_bridge(), SyncSettings(hue_rate_hz=rate, min_brightness=0.0), api=api,
                               connector=connector)
    return s, transports


def _channels(packet: bytes) -> dict[int, tuple[int, int, int]]:
    body = packet[52:]
    return {body[i]: struct.unpack(">HHH", body[i + 1 : i + 7]) for i in range(0, len(body), 7)}


def test_stream_sends_one_packet_per_frame_for_all_lights():
    async def main():
        api = FakeApi(_area())
        stream, transports = _stream(api)
        stream.start()
        assert await stream.wait_ready(2)
        a = HueLightOutput(LightConfig(transport="hue", hue_channels=[0]), stream.settings, stream)
        b = HueLightOutput(LightConfig(transport="hue", hue_channels=[1, 2]), stream.settings, stream)
        await a.start()
        await b.start()
        assert a.ready and b.ready
        # One engine frame: both outputs write, then the loop runs the flush once.
        a.submit(np.array([[1.0, 0.0, 0.0]]))
        b.submit(np.array([[0.0, 0.0, 1.0]]))
        await asyncio.sleep(0)
        (t,) = transports
        assert len(t.sent) == 1
        assert _channels(t.sent[0]) == {0: (65535, 0, 0), 1: (0, 0, 65535), 2: (0, 0, 65535)}
        assert t.sent[0][16:52] == AREA_ID.encode()
        await stream.stop()
        assert t.closed and api.streaming_calls == [True, False]

    asyncio.run(main())


def test_stream_is_latest_wins_at_the_rate_limit():
    async def main():
        stream, transports = _stream(FakeApi(_area()), rate=20.0)  # one packet per 50 ms
        stream.start()
        assert await stream.wait_ready(2)
        out = HueLightOutput(LightConfig(transport="hue", hue_channels=[0]), stream.settings, stream)
        await out.start()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for i in range(40):  # 40 frames, much faster than 20 Hz
            out.submit(np.array([[i / 39, 0.0, 0.0]]))
            await asyncio.sleep(0.005)
        elapsed = loop.time() - t0
        await asyncio.sleep(0.08)  # let the pending (newest) frame go out
        sent = transports[0].sent
        assert 2 <= len(sent) <= elapsed * 20 + 3 and len(sent) < 40
        assert _channels(sent[-1])[0] == (65535, 0, 0)  # the newest colour wins
        assert stream.sent == len(sent)
        await stream.stop()

    asyncio.run(main())


def test_stream_keepalive_resends_last_packet(monkeypatch):
    monkeypatch.setattr(hue_out, "KEEPALIVE_S", 0.1)

    async def main():
        stream, transports = _stream(FakeApi(_area()))
        stream.start()
        assert await stream.wait_ready(2)
        stream.write([0], np.array([[0.2, 0.4, 0.6]]))
        await asyncio.sleep(0.45)  # no new frames
        sent = transports[0].sent
        assert len(sent) >= 3
        assert all(_channels(p) == _channels(sent[0]) for p in sent)
        await stream.stop()

    asyncio.run(main())


def test_stream_refuses_area_held_by_another_app():
    async def main():
        api = FakeApi(_area(status="active", streamer="hue-sync"))
        stream, transports = _stream(api)
        stream.start()
        assert not await stream.wait_ready(0.3)
        assert stream.status == "error" and "another app" in stream.error and "Hue Sync" in stream.error
        assert api.streaming_calls == [] and transports == []
        out = HueLightOutput(LightConfig(transport="hue", hue_channels=[0]), stream.settings, stream)
        assert "another app" in out.status_text()
        await stream.stop()
        assert api.streaming_calls == []  # we never started it, so we don't stop it

    asyncio.run(main())


def test_stream_refuses_while_another_area_is_streamed_by_another_app():
    async def main():
        api = FakeApi(_area())
        api.others = [HueArea(id="other", name="Music area", status="active", streamer="hue-sync")]
        stream, transports = _stream(api)
        stream.start()
        assert not await stream.wait_ready(0.3)
        assert "'Music area' is in use by another app" in stream.error
        assert api.streaming_calls == [] and transports == []
        await stream.stop()

    asyncio.run(main())


def test_stream_refuses_unknown_area():
    async def main():
        api = FakeApi(_area())
        api._area = HueArea(id="something-else", name="x")
        stream, _ = _stream(api)
        stream.start()
        assert not await stream.wait_ready(0.3)
        assert "not found" in stream.error
        await stream.stop()

    asyncio.run(main())


def test_stream_reconnects_after_takeover_and_resumes(monkeypatch):
    monkeypatch.setattr(hue_out, "WATCH_S", 0.05)
    monkeypatch.setattr(hue_out, "RETRY_S", 0.05)

    async def main():
        api = FakeApi(_area())
        stream, transports = _stream(api)
        stream.start()
        assert await stream.wait_ready(2)
        api._area.streamer = "hue-sync"  # another app takes the area over
        await asyncio.sleep(0.2)
        assert not stream.ready and "another app" in stream.error and transports[0].closed
        api._area.status, api._area.streamer = "inactive", ""  # ...and lets go of it
        assert await stream.wait_ready(2)
        assert len(transports) == 2 and stream.error is None
        await stream.stop()

    asyncio.run(main())


def test_stream_stop_without_deactivate_keeps_area_active():
    async def main():
        api = FakeApi(_area())
        stream, _ = _stream(api)
        stream.start()
        assert await stream.wait_ready(2)
        await stream.stop(deactivate=False)
        assert api.streaming_calls == [True] and api.puts == []  # no restore on a restart

    asyncio.run(main())


def test_stream_restores_lights_after_stop(monkeypatch):
    monkeypatch.setattr(hue_out, "RESTORE_GAP_S", 0.0)

    async def main():
        api = FakeApi(_area((0, 1)))
        api.lights["light-1"] = {"id": "light-1", "on": {"on": False}}
        stream, _ = _stream(api)
        stream.start()
        assert await stream.wait_ready(2)
        assert api.snapshots == 1
        await stream.stop()
        # The bridge doesn't restore: stop the stream first, then put each light back.
        assert api.log == ["streaming True", "streaming False", "put light-0", "put light-1"]
        assert dict(api.puts) == {
            "light-0": {"on": {"on": True}, "dynamics": {"duration": 400}, "dimming": {"brightness": 40.0},
                        "color": {"xy": {"x": 0.3, "y": 0.3}}},
            "light-1": {"on": {"on": False}, "dynamics": {"duration": 400}},
        }

    asyncio.run(main())


def test_stream_stop_without_restore_still_frees_the_area():
    async def main():
        api = FakeApi(_area())
        stream, _ = _stream(api)
        stream.start()
        assert await stream.wait_ready(2)
        await stream.stop(restore=False)
        assert api.streaming_calls == [True, False] and api.puts == []

    asyncio.run(main())


def test_snapshot_is_taken_once_across_reconnects(monkeypatch):
    monkeypatch.setattr(hue_out, "RETRY_S", 0.05)

    async def main():
        api = FakeApi(_area())
        stream, transports = _stream(api)
        stream.start()
        assert await stream.wait_ready(2)
        stream.connection_lost(None)
        await asyncio.sleep(0.2)
        assert len(transports) == 2 and api.snapshots == 1  # not re-read while lights show stream colours
        await stream.stop()

    asyncio.run(main())


def test_restore_payload_variants():
    ct = {"on": {"on": True}, "dimming": {"brightness": 55.0},
          "color_temperature": {"mirek": 366, "mirek_valid": True}, "color": {"xy": {"x": 0.45, "y": 0.41}}}
    assert restore_payload(ct) == {"on": {"on": True}, "dynamics": {"duration": 400},
                                   "dimming": {"brightness": 55.0}, "color_temperature": {"mirek": 366}}
    xy = dict(ct, color_temperature={"mirek": None, "mirek_valid": False})
    body = restore_payload(xy)
    assert body["color"] == {"xy": {"x": 0.45, "y": 0.41}} and "color_temperature" not in body
    points = [{"color": {"xy": {"x": 0.1, "y": 0.2}}}, {"color": {"xy": {"x": 0.3, "y": 0.4}}}]
    body = restore_payload(dict(xy, gradient={"points": points, "points_capable": 5}))
    assert body["gradient"] == {"points": points} and "color" not in body
    assert restore_payload(dict(xy, effects={"status": "candle"}))["effects"] == {"effect": "candle"}
    assert restore_payload({"on": {"on": False}, "dimming": {"brightness": 9.0}}) == {
        "on": {"on": False}, "dynamics": {"duration": 400}}


def test_dtls_close_triggers_reconnect(monkeypatch):
    monkeypatch.setattr(hue_out, "RETRY_S", 0.05)

    async def main():
        stream, transports = _stream(FakeApi(_area()))
        stream.start()
        assert await stream.wait_ready(2)
        stream.connection_lost(None)
        await asyncio.sleep(0.2)
        assert len(transports) == 2 and stream.ready
        await stream.stop()

    asyncio.run(main())


# ---------------------------------------------------------------------- output
def _ready_output(cfg: LightConfig, settings: SyncSettings):
    stream = HueEntertainmentStream(_bridge(), settings, api=FakeApi(_area((0, 1, 2, 3))))
    stream.area = stream.api._area
    stream._transport = FakeTransport()
    written = []
    stream.write = lambda chans, rgb: written.append((list(chans), np.array(rgb)))
    stream._ready = type("E", (), {"is_set": lambda self: True})()
    out = HueLightOutput(cfg, settings, stream)
    return out, written


def test_output_lamp_and_strip_channel_mapping():
    s = SyncSettings(min_brightness=0.0)
    lamp, w = _ready_output(LightConfig(transport="hue", hue_channels=[0, 1]), s)
    lamp.submit(np.array([[0.5, 0.25, 0.0]]))
    chans, rgb = w[-1]
    assert chans == [0, 1] and rgb.tolist() == [[0.5, 0.25, 0.0]] * 2

    strip, w = _ready_output(LightConfig(transport="hue", kind="strip", hue_channels=[3, 1, 2]), s)
    strip.submit(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1], [0, 0, 0], [0.5, 0.5, 0.5]], np.float32))
    chans, rgb = w[-1]
    assert chans == [3, 1, 2] and rgb.tolist() == [[1, 0, 0], [0, 0, 1], [0, 0, 0]]


def test_output_min_brightness_and_black_off():
    out, w = _ready_output(LightConfig(transport="hue", hue_channels=[0]), SyncSettings(min_brightness=0.1))
    out.submit(np.array([[0.04, 0.02, 0.0]]))
    assert w[-1][1][0] == pytest.approx([0.1, 0.05, 0.0])  # raised to the floor, same hue
    out.submit(np.array([[0.0, 0.0, 0.0]]))
    assert w[-1][1][0] == pytest.approx([0.1, 0.05, 0.0])  # black keeps the last hue, dimmed

    off, w = _ready_output(LightConfig(transport="hue", hue_channels=[0]),
                           SyncSettings(min_brightness=0.1, black_turns_off=True))
    off.submit(np.array([[0.05, 0.0, 0.0]]))
    assert w[-1][1][0].tolist() == [0, 0, 0]


def test_output_reports_channel_missing_from_area():
    out, w = _ready_output(LightConfig(transport="hue", hue_channels=[9]), SyncSettings())
    assert not out.ready and "channel 9" in out.status_text()
    out.submit(np.array([[1.0, 1.0, 1.0]]))
    assert w == []


# ---------------------------------------------------------------------- engine
def test_engine_streams_hue_lights(monkeypatch):
    from espsync import engine as engine_mod

    api = FakeApi(_area((0, 1)))
    transports: list[FakeTransport] = []

    async def connector(stream):
        transports.append(FakeTransport())
        return transports[-1]

    class Stream(HueEntertainmentStream):
        def __init__(self, bridge, settings):
            super().__init__(bridge, settings, api=api, connector=connector)

    monkeypatch.setattr(engine_mod, "HueEntertainmentStream", Stream)
    bridge = _bridge()
    unpaired = HueBridgeConfig(name="New")
    lights = [
        LightConfig(name="a", transport="hue", hue_bridge=bridge.id, hue_channels=[0]),
        LightConfig(name="b", transport="hue", hue_bridge=bridge.id, hue_channels=[1]),
        LightConfig(name="c", transport="hue", hue_bridge=unpaired.id, hue_channels=[0]),
        LightConfig(name="d", transport="hue", hue_channels=[0]),
    ]
    cfg = AppConfig(hue_bridges=[bridge, unpaired], lights=lights)
    eng = engine_mod.SyncEngine(cfg)

    async def main():
        await eng._start_outputs()
        a, b, c, d = (eng._outputs[l.id] for l in lights)
        assert isinstance(a, HueLightOutput) and a.ready and b.ready
        assert "not paired" in c.status_text() and "no Hue bridge" in d.status_text()
        eng._latest = {lights[0].id: np.array([[1.0, 0.0, 0.0]]), lights[1].id: np.array([[0.0, 1.0, 0.0]])}
        eng._deliver()
        await asyncio.sleep(0)
        (t,) = transports
        assert len(t.sent) == 1 and _channels(t.sent[0]) == {0: (65535, 0, 0), 1: (0, 65535, 0)}
        assert "streaming" in eng.stream_status()["Bridge"]
        await eng._stop_outputs(restore=True)
        assert t.closed and api.streaming_calls == [True, False] and not eng._hue
        assert [p[0] for p in api.puts] == ["light-0", "light-1"]  # restored

    asyncio.run(main())


def test_engine_restart_keeps_hue_area_streaming(monkeypatch):
    from espsync import engine as engine_mod

    stops = []

    class Stream:
        def __init__(self, bridge, settings):
            self.bridge = bridge

        def start(self):
            pass

        async def stop(self, deactivate=True, restore=None):
            stops.append((deactivate, restore))

    eng = engine_mod.SyncEngine(AppConfig())

    async def main():
        eng._hue = {"b": Stream(_bridge(), None)}
        eng._restarting = True
        await eng._stop_outputs(restore=False)
        eng._hue = {"b": Stream(_bridge(), None)}
        eng._restarting = False
        await eng._stop_outputs(restore=False)  # user stop with restore_on_stop off
        eng._hue = {"b": Stream(_bridge(), None)}
        await eng._stop_outputs(restore=True)

    asyncio.run(main())
    assert stops == [(False, False), (True, False), (True, True)]
