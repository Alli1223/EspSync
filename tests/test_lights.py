import asyncio
import socket
import struct

import numpy as np
import pytest

from espsync.config import LightConfig, SyncSettings
from espsync.lights.base import LightOutput, to_bytes
from espsync.lights.esphome import pick_rgb_mode, split_brightness
from espsync.lights.udp import E131Packetizer, UdpOutput, wled_packets


def test_wled_drgb_small_strip():
    rgb = bytes(range(30))
    (pkt,) = wled_packets(rgb, timeout_s=2)
    assert pkt[:2] == bytes((2, 2)) and pkt[2:] == rgb


def test_wled_dnrgb_large_strip():
    rgb = bytes(3 * 1000)
    pkts = wled_packets(rgb)
    assert len(pkts) == 3
    assert all(p[0] == 4 for p in pkts)
    starts = [struct.unpack(">H", p[2:4])[0] for p in pkts]
    assert starts == [0, 489, 978]
    assert sum(len(p) - 4 for p in pkts) == len(rgb)


def test_e131_packet_layout():
    pk = E131Packetizer(source_name="test")
    dmx = bytes(range(9))
    pkt = pk.packet(7, dmx)
    assert len(pkt) == 126 + len(dmx)
    assert pkt[4:16] == b"ASC-E1.17\x00\x00\x00"
    assert struct.unpack(">H", pkt[16:18])[0] & 0x0FFF == len(pkt) - 16
    assert struct.unpack(">H", pkt[38:40])[0] & 0x0FFF == len(pkt) - 38
    assert struct.unpack(">H", pkt[113:115])[0] == 7  # universe
    assert struct.unpack(">H", pkt[115:117])[0] & 0x0FFF == len(pkt) - 115
    assert struct.unpack(">H", pkt[123:125])[0] == len(dmx) + 1
    assert pkt[125] == 0 and pkt[126:] == dmx


def test_e131_splits_universes():
    pk = E131Packetizer()
    pkts = pk.packets(1, bytes(3 * 200))
    universes = [struct.unpack(">H", p[113:115])[0] for p in pkts]
    assert universes == [1, 2]
    assert len(pkts[0]) == 126 + 510 and len(pkts[1]) == 126 + 90


def test_split_brightness_normalises():
    b, rgb = split_brightness(np.array([0.5, 0.25, 0.0]), 0.0)
    assert b == pytest.approx(0.5) and rgb == pytest.approx((1.0, 0.5, 0.0))
    b, _ = split_brightness(np.array([0.0, 0.0, 0.0]), 0.05)
    assert b == 0.05


class _Info:
    def __init__(self, modes):
        self.supported_color_modes = modes


def test_pick_rgb_mode():
    assert pick_rgb_mode(_Info([11, 35])) == 35
    assert pick_rgb_mode(_Info([39, 11])) == 39
    assert pick_rgb_mode(_Info([3])) is None


class _Recorder(LightOutput):
    def __init__(self, rate):
        super().__init__(LightConfig(), SyncSettings(), rate)
        self.sent_payloads = []

    def encode(self, colors):
        return to_bytes(colors)

    def transmit(self, colors, payload):
        self.sent_payloads.append(payload)


def test_rate_limit_latest_wins_and_dedupes():
    async def run():
        out = _Recorder(rate=20)  # 50 ms interval
        await out.start()
        red, green, blue = (np.array([[c]], np.float32).reshape(1, 3) for c in ([1, 0, 0], [0, 1, 0], [0, 0, 1]))
        out.submit(red)  # sent immediately
        out.submit(green)  # within the interval -> pending
        out.submit(blue)  # replaces green
        await asyncio.sleep(0.08)
        out.submit(blue)  # unchanged -> not resent
        await asyncio.sleep(0.08)
        return out.sent_payloads

    sent = asyncio.run(run())
    assert sent == [bytes((255, 0, 0)), bytes((0, 0, 255))]


def test_udp_output_sends_wled_datagram():
    async def run():
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        cfg = LightConfig(kind="strip", transport="wled", led_count=2, udp_port=rx.getsockname()[1])
        out = UdpOutput(cfg, SyncSettings(), "127.0.0.1")
        await out.start()
        out.submit(np.array([[1, 0, 0], [0, 0, 1]], np.float32))
        data = rx.recv(1024)
        await out.stop(restore=False)
        blank = rx.recv(1024)
        rx.close()
        return data, blank

    data, blank = asyncio.run(run())
    assert data == bytes((2, 2, 255, 0, 0, 0, 0, 255))
    assert blank == bytes((2, 2, 0, 0, 0, 0, 0, 0))


def test_small_changes_are_held_back_then_settle(monkeypatch):
    import espsync.lights.base as base

    monkeypatch.setattr(base, "SETTLE_S", 0.15)

    async def run():
        out = _Recorder(rate=200)
        out.settings.min_change = 0.02  # 5 of 255
        await out.start()
        level = lambda v: np.full((1, 3), v / 255, np.float32)  # noqa: E731
        out.submit(level(100))
        await asyncio.sleep(0.02)
        out.submit(level(103))  # small: held back
        await asyncio.sleep(0.02)
        out.submit(level(120))  # big: sent at once
        await asyncio.sleep(0.02)
        out.submit(level(122))  # small again
        held = list(out.sent_payloads)
        await asyncio.sleep(0.2)
        out.submit(level(122))  # ...but still there after the settle time: sent
        return held, out.sent_payloads

    held, sent = asyncio.run(run())
    assert held == [bytes((100,) * 3), bytes((120,) * 3)]
    assert sent == [*held, bytes((122,) * 3)]


def test_total_cap_slows_wifi_lights_only():
    from espsync.engine import apply_total_cap
    from espsync.lights.base import VirtualOutput

    s = SyncSettings()
    strips = [UdpOutput(LightConfig(transport="wled"), s, "127.0.0.1") for _ in range(4)]  # 60 Hz each
    virtual = VirtualOutput(LightConfig(transport="virtual"), s, 60)
    apply_total_cap([*strips, virtual], 0)  # no limit
    assert strips[0].interval == pytest.approx(1 / 60)
    apply_total_cap([*strips, virtual], 100)  # 4 lights share 100 updates/s
    assert [o.interval for o in strips] == pytest.approx([1 / 25] * 4)
    assert virtual.interval == pytest.approx(1 / 60)
    apply_total_cap(strips, 1000)  # a generous limit never speeds a light up
    assert strips[0].interval == pytest.approx(1 / 25)
