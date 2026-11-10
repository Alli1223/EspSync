"""mDNS discovery: ESPHome devices (`_esphomelib._tcp`) and, via `browse`, Hue bridges."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE = "_esphomelib._tcp.local."


@dataclass
class DiscoveredDevice:
    name: str
    friendly_name: str
    host: str
    address: str
    port: int
    encrypted: bool
    version: str = ""
    mac: str = ""


@dataclass
class ResolvedService:
    server: str
    port: int
    addresses: list[str]
    props: dict[str, str]


async def browse(service: str, timeout: float = 3.0) -> list[tuple[str, ResolvedService]]:
    """Browse `service` for `timeout` s and resolve what answers -> [(instance name, info)]."""
    found: dict[str, ResolvedService] = {}
    pending: set[asyncio.Task] = set()
    azc = AsyncZeroconf()

    async def resolve(name: str) -> None:
        info = AsyncServiceInfo(service, name)
        if not await info.async_request(azc.zeroconf, 2000):
            return
        props = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v or "")
            for k, v in (info.properties or {}).items()
        }
        found[name] = ResolvedService(
            server=(info.server or "").rstrip("."),
            port=info.port or 0,
            addresses=info.parsed_scoped_addresses(),
            props=props,
        )

    def on_change(zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            task = asyncio.ensure_future(resolve(name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(azc.zeroconf, service, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.wait(pending, timeout=2.5)
    finally:
        await browser.async_cancel()
        await azc.async_close()
    return list(found.items())


async def discover(timeout: float = 3.0) -> list[DiscoveredDevice]:
    found: dict[str, DiscoveredDevice] = {}
    for name, info in await browse(SERVICE, timeout):
        props = info.props
        v4 = [a for a in info.addresses if ":" not in a]
        short = name.removesuffix("." + SERVICE)
        found[short] = DiscoveredDevice(
            name=short,
            friendly_name=props.get("friendly_name", "") or short,
            host=info.server or f"{short}.local",
            address=(v4 or info.addresses or [""])[0],
            port=info.port or 6053,
            encrypted=bool(props.get("api_encryption")),
            version=props.get("version", ""),
            mac=props.get("mac", ""),
        )
    return sorted(found.values(), key=lambda d: d.friendly_name.lower())
