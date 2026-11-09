"""mDNS discovery of ESPHome devices (`_esphomelib._tcp.local.`)."""

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


async def discover(timeout: float = 3.0) -> list[DiscoveredDevice]:
    found: dict[str, DiscoveredDevice] = {}
    pending: set[asyncio.Task] = set()
    azc = AsyncZeroconf()

    async def resolve(name: str) -> None:
        info = AsyncServiceInfo(SERVICE, name)
        if not await info.async_request(azc.zeroconf, 2000):
            return
        props = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v or "")
            for k, v in (info.properties or {}).items()
        }
        addrs = info.parsed_scoped_addresses()
        v4 = [a for a in addrs if ":" not in a]
        short = name.removesuffix("." + SERVICE)
        found[short] = DiscoveredDevice(
            name=short,
            friendly_name=props.get("friendly_name", "") or short,
            host=(info.server or f"{short}.local.").rstrip("."),
            address=(v4 or addrs or [""])[0],
            port=info.port or 6053,
            encrypted=bool(props.get("api_encryption")),
            version=props.get("version", ""),
            mac=props.get("mac", ""),
        )

    def on_change(zeroconf, service_type, name, state_change) -> None:  # noqa: ANN001
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            task = asyncio.ensure_future(resolve(name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(azc.zeroconf, SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.wait(pending, timeout=2.5)
    finally:
        await browser.async_cancel()
        await azc.async_close()
    return sorted(found.values(), key=lambda d: d.friendly_name.lower())
