"""UDP realtime outputs for addressable strips (per-LED colour).

These skip Home Assistant *and* the native API's protobuf/TCP path: each frame
is one datagram the ESP applies directly to the LEDs.  The ESPHome light needs
the matching effect configured and active:

    light:
      - platform: neopixelbus   # or esp32_rmt_led_strip, fastled, ...
        effects:
          - wled:               # WLED realtime protocol, port 21324
          - e131:               # requires the top-level `e131:` component
              universe: 1
              channels: RGB
"""

from __future__ import annotations

import socket
import struct
import uuid

import numpy as np

from ..config import LightConfig, SyncSettings
from .base import LightOutput, to_bytes

WLED_PORT = 21324
E131_PORT = 5568

WLED_DRGB = 2
WLED_DNRGB = 4
WLED_DRGB_MAX = 490
WLED_DNRGB_MAX = 489


def wled_packets(rgb: bytes, timeout_s: int = 2) -> list[bytes]:
    """WLED realtime UDP packets for raw RGB bytes (3 per LED)."""
    count = len(rgb) // 3
    timeout_s = max(1, min(int(timeout_s), 254))
    if count <= WLED_DRGB_MAX:
        return [bytes((WLED_DRGB, timeout_s)) + rgb]
    packets = []
    for start in range(0, count, WLED_DNRGB_MAX):
        chunk = rgb[start * 3 : (start + WLED_DNRGB_MAX) * 3]
        packets.append(bytes((WLED_DNRGB, timeout_s)) + struct.pack(">H", start) + chunk)
    return packets


class E131Packetizer:
    """Builds E1.31 (sACN) data packets, 170 RGB LEDs per universe."""

    LEDS_PER_UNIVERSE = 170

    def __init__(self, source_name: str = "EspSync", priority: int = 100) -> None:
        self.cid = uuid.uuid4().bytes
        self.source = source_name.encode("utf-8")[:63].ljust(64, b"\0")
        self.priority = priority
        self.sequence = 0

    def packet(self, universe: int, dmx: bytes) -> bytes:
        n = len(dmx)
        root = (
            struct.pack(">HH", 0x0010, 0x0000)
            + b"ASC-E1.17\x00\x00\x00"
            + struct.pack(">HI", 0x7000 | (110 + n), 0x00000004)
            + self.cid
        )
        framing = (
            struct.pack(">HI", 0x7000 | (88 + n), 0x00000002)
            + self.source
            + struct.pack(">BHBBH", self.priority, 0, self.sequence, 0, universe)
        )
        dmp = struct.pack(">HBBHHH", 0x7000 | (11 + n), 0x02, 0xA1, 0x0000, 0x0001, n + 1) + b"\x00" + dmx
        return root + framing + dmp

    def packets(self, start_universe: int, rgb: bytes) -> list[bytes]:
        per = self.LEDS_PER_UNIVERSE * 3
        out = []
        for i, offset in enumerate(range(0, max(len(rgb), 1), per)):
            out.append(self.packet(start_universe + i, rgb[offset : offset + per]))
        self.sequence = (self.sequence + 1) & 0xFF
        return out


class UdpOutput(LightOutput):
    keepalive_s = 1.0

    def __init__(self, cfg: LightConfig, settings: SyncSettings, host: str) -> None:
        super().__init__(cfg, settings, settings.udp_rate_hz)
        self.host = host
        self.port = cfg.udp_port or (WLED_PORT if cfg.transport == "wled" else E131_PORT)
        self._sock: socket.socket | None = None
        self._addr: tuple[str, int] | None = None
        self._e131 = E131Packetizer() if cfg.transport == "e131" else None

    async def start(self) -> None:
        if not self.host:
            self.status = "error"
            self.error = "no UDP host configured"
            return
        import asyncio

        infos = await asyncio.get_running_loop().getaddrinfo(
            self.host, self.port, family=socket.AF_INET, type=socket.SOCK_DGRAM
        )
        self._addr = infos[0][4][:2]
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        # Mark as low-latency traffic (DSCP EF); ignored where unsupported.
        try:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
        except OSError:
            pass
        self.status = "ready"

    def encode(self, colors: np.ndarray) -> bytes:
        return to_bytes(colors)

    def transmit(self, colors: np.ndarray, payload: bytes) -> None:
        assert self._sock is not None and self._addr is not None
        if self._e131 is not None:
            packets = self._e131.packets(self.cfg.universe, payload)
        else:
            packets = wled_packets(payload)
        for pkt in packets:
            self._sock.sendto(pkt, self._addr)

    def blank(self) -> None:
        if self._sock is not None and self._addr is not None:
            zeros = bytes(3 * max(1, self.cfg.led_count))
            self.transmit(np.zeros((0, 3)), zeros)

    async def stop(self, restore: bool) -> None:
        await super().stop(restore)
        if self._sock is not None:
            try:
                self.blank()
            except OSError:
                pass
            self._sock.close()
            self._sock = None
