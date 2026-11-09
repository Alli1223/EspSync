"""Headless command line interface.

    espsync-cli discover                 # find ESPHome devices on the LAN
    espsync-cli hue-pair                 # pair a Philips Hue bridge (press its link button)
    espsync-cli hue-areas                # list its entertainment areas
    espsync-cli hue-import AREA          # stream to that area and add its lights
    espsync-cli hue-test                 # colour chase on the area, to check streaming
    espsync-cli lights HOST --key KEY    # list lights on a device
    espsync-cli add-device ...           # edit the config without the GUI
    espsync-cli add-light ...
    espsync-cli show                     # print the current config
    espsync-cli monitors                 # list displays (and HDR state)
    espsync-cli bench                    # measure capture + processing speed
    espsync-cli run                      # sync until Ctrl+C
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

from .config import (
    AppConfig,
    DeviceConfig,
    HueBridgeConfig,
    LightConfig,
    default_config_path,
    load_config,
    save_config,
)
from .mapping import SCREEN_PRESETS

BACKEND_CHOICES = ["auto", "dxgi", "portal", "mss"]


def _cfg_path(args: argparse.Namespace) -> Path:
    return Path(args.config) if args.config else default_config_path()


def cmd_discover(args: argparse.Namespace) -> int:
    from .discovery import discover

    devices = asyncio.run(discover(args.timeout))
    if not devices:
        print("No ESPHome devices found (mDNS may be blocked on this network).")
        return 1
    for d in devices:
        enc = "encrypted" if d.encrypted else "plaintext"
        print(f"{d.friendly_name:32} {d.host:40} {d.address:15} {enc:9} v{d.version}")
    return 0


def cmd_lights(args: argparse.Namespace) -> int:
    from .lights import probe_device

    dev = DeviceConfig(host=args.host, port=args.port, encryption_key=args.key or "", password=args.password or "")
    try:
        name, lights = asyncio.run(probe_device(dev))
    except ConnectionError as exc:
        print(f"{args.host}: {exc}", file=sys.stderr)
        return 1
    print(f"{name}:")
    if not lights:
        print("  (no lights)")
    for light in lights:
        modes = ",".join(str(int(m)) for m in light.supported_color_modes)
        effects = ", ".join(light.effects) or "-"
        print(f"  {light.object_id:30} name={light.name!r} modes=[{modes}] effects=[{effects}]")
    return 0


def cmd_monitors(args: argparse.Namespace) -> int:
    from .capture import list_monitors

    for m in list_monitors(args.backend):
        print(m.label())
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    cfg = load_config(path)
    print(f"# {path}")
    data = cfg.to_dict()
    for d in data["devices"]:
        if d.get("encryption_key"):
            d["encryption_key"] = "***"
    for b in data["hue_bridges"]:
        for k in ("app_key", "client_key"):
            if b.get(k):
                b[k] = "***"
    print(json.dumps(data, indent=2))
    return 0


def _find_device(cfg: AppConfig, ref: str) -> DeviceConfig | None:
    return next((d for d in cfg.devices if ref in (d.id, d.name, d.host)), None)


def cmd_add_device(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    cfg = load_config(path)
    dev = DeviceConfig(name=args.name or args.host, host=args.host, port=args.port,
                       encryption_key=args.key or "", password=args.password or "")
    cfg.devices.append(dev)
    save_config(cfg, path)
    print(f"Added device {dev.name} ({dev.id})")
    return 0


def cmd_add_light(args: argparse.Namespace) -> int:
    path = _cfg_path(args)
    cfg = load_config(path)
    light = LightConfig(name=args.name, kind=args.kind, transport=args.transport, entity=args.entity or "",
                        x=args.x, y=args.y, z=args.z, led_count=args.leds, udp_host=args.udp_host or "",
                        udp_effect=args.effect or "", universe=args.universe)
    if args.device:
        dev = _find_device(cfg, args.device)
        if dev is None:
            print(f"Unknown device {args.device!r}", file=sys.stderr)
            return 2
        light.device_id = dev.id
    if args.kind == "strip":
        light.region_size = 0.12
        if args.preset:
            light.strip_layout = "screen"
            light.screen_path = SCREEN_PRESETS[args.preset]
    cfg.lights.append(light)
    save_config(cfg, path)
    print(f"Added light {light.name} ({light.id})")
    return 0


# ------------------------------------------------------------------- Hue
def _find_bridge(cfg: AppConfig, ref: str | None) -> HueBridgeConfig | None:
    if ref:
        r = ref.lower()
        return next((b for b in cfg.hue_bridges if r in (b.id.lower(), b.name.lower(), b.host.lower(),
                                                          b.bridge_id.lower())), None)
    return cfg.hue_bridges[0] if len(cfg.hue_bridges) == 1 else None


def _need_bridge(cfg: AppConfig, ref: str | None) -> HueBridgeConfig | None:
    bridge = _find_bridge(cfg, ref)
    if bridge is None:
        if ref:
            print(f"Unknown Hue bridge {ref!r}", file=sys.stderr)
        elif cfg.hue_bridges:
            print("Several bridges are paired: pick one with --bridge", file=sys.stderr)
        else:
            print("No Hue bridge paired yet: run `espsync-cli hue-pair`", file=sys.stderr)
    elif not bridge.paired:
        print(f"{bridge.name} is not paired: run `espsync-cli hue-pair {bridge.host}`", file=sys.stderr)
        return None
    return bridge


def cmd_hue_discover(args: argparse.Namespace) -> int:
    from .hue import discover_bridges

    bridges = asyncio.run(discover_bridges(args.timeout))
    if not bridges:
        print("No Hue bridges found (mDNS may be blocked). Pair by IP: espsync-cli hue-pair <ip>")
        return 1
    for b in bridges:
        print(f"{b.name:28} {b.host:15} id={b.bridge_id} model={b.model}")
    return 0


def cmd_hue_pair(args: argparse.Namespace) -> int:
    from .hue import HueError, bridge_info, discover_bridges, pair

    path = _cfg_path(args)
    cfg = load_config(path)
    host = args.host
    if not host:
        found = asyncio.run(discover_bridges(3.0))
        if len(found) != 1:
            print(f"Found {len(found)} bridges; give the IP address: espsync-cli hue-pair <ip>", file=sys.stderr)
            return 2
        host = found[0].host
    try:
        info = asyncio.run(bridge_info(host))
    except HueError as exc:
        print(exc, file=sys.stderr)
        return 1
    name = args.name or info.get("name") or "Hue Bridge"
    print(f"{name} at {host} (id {info.get('bridgeid')}, API {info.get('apiversion')}).")
    print(f"Press the round link button on the bridge now (waiting up to {args.timeout:.0f} s)...", flush=True)
    try:
        app_key, client_key = asyncio.run(pair(host, args.timeout))
    except HueError as exc:
        print(f"Pairing failed: {exc}", file=sys.stderr)
        return 1
    bid = str(info.get("bridgeid", "")).lower()
    bridge = next((b for b in cfg.hue_bridges if (bid and b.bridge_id == bid) or b.host == host), None)
    if bridge is None:
        bridge = HueBridgeConfig()
        cfg.hue_bridges.append(bridge)
    bridge.name, bridge.host, bridge.bridge_id = name, host, bid
    bridge.app_key, bridge.client_key = app_key, client_key
    save_config(cfg, path)
    print("Paired. Next: espsync-cli hue-areas, then espsync-cli hue-import <area>.")
    return 0


def _print_area(area, current: bool) -> None:  # noqa: ANN001
    from .hue import hue_to_room

    mark = "*" if current else " "
    print(f"{mark} {area.name!r:30} id={area.id} type={area.kind} status={area.status} "
          f"channels={len(area.channels)}")
    for c in area.channels:
        x, y, z = hue_to_room(c.x, c.y, c.z)
        print(f"     ch {c.channel_id:2}  {c.label:34} hue=({c.x:+.2f},{c.y:+.2f},{c.z:+.2f})"
              f"  room=({x:+.2f},{y:+.2f},{z:.2f})")


def cmd_hue_areas(args: argparse.Namespace) -> int:
    from .hue import HueApi, HueError

    cfg = load_config(_cfg_path(args))
    bridge = _need_bridge(cfg, args.bridge)
    if bridge is None:
        return 2
    try:
        areas = asyncio.run(HueApi(bridge.host, bridge.app_key).areas())
    except HueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not areas:
        print("No entertainment areas. Create one in the Hue app (Settings > Entertainment areas).")
        return 1
    print(f"{bridge.name} ({bridge.host}); * = the area EspSync streams to")
    for a in areas:
        _print_area(a, a.id == bridge.entertainment_id)
    return 0


def cmd_hue_import(args: argparse.Namespace) -> int:
    from .hue import HueApi, HueError, lights_for_area

    path = _cfg_path(args)
    cfg = load_config(path)
    bridge = _need_bridge(cfg, args.bridge)
    if bridge is None:
        return 2
    try:
        areas = asyncio.run(HueApi(bridge.host, bridge.app_key).areas())
    except HueError as exc:
        print(exc, file=sys.stderr)
        return 1
    ref = args.area.lower()
    area = next((a for a in areas if ref in (a.id.lower(), a.name.lower())), None)
    if area is None:
        print(f"No entertainment area {args.area!r}; have: {', '.join(a.name for a in areas)}", file=sys.stderr)
        return 2
    bridge.entertainment_id, bridge.entertainment_name = area.id, area.name
    if args.replace:
        cfg.lights = [l for l in cfg.lights if not (l.transport == "hue" and l.hue_bridge == bridge.id)]
    existing = {tuple(l.hue_channels) for l in cfg.lights if l.transport == "hue" and l.hue_bridge == bridge.id}
    new = [l for l in lights_for_area(bridge, area, cfg.room, args.group_gradients)
           if tuple(l.hue_channels) not in existing]
    cfg.lights.extend(new)
    save_config(cfg, path)
    print(f"Streaming area: {area.name}. Added {len(new)} light(s):")
    for l in new:
        print(f"  {l.name:36} channels={l.hue_channels} room=({l.x:+.2f},{l.y:+.2f},{l.z:.2f})")
    return 0


def cmd_hue_test(args: argparse.Namespace) -> int:
    """Stream test colours straight to the area (no screen capture).

    chase     : a rainbow running across the channels
    positions : first half front red / back blue, second half left green /
                right magenta, as the bridge has the lights placed
    """
    import colorsys

    import numpy as np

    from .config import SyncSettings
    from .engine import _set_timer_resolution
    from .hue import HueApi
    from .lights.hue import HueEntertainmentStream

    cfg = load_config(_cfg_path(args))
    bridge = _need_bridge(cfg, args.bridge)
    if bridge is None:
        return 2

    def positions(area, second_half: bool) -> np.ndarray:  # noqa: ANN001
        out = []
        for c in area.channels:
            v = c.x if second_half else c.y
            if v > 0.2:
                out.append((1.0, 0.0, 1.0) if second_half else (1.0, 0.0, 0.0))
            elif v < -0.2:
                out.append((0.0, 1.0, 0.0) if second_half else (0.0, 0.0, 1.0))
            else:
                out.append((0.15, 0.15, 0.15))
        return np.array(out, np.float32)

    async def run() -> int:
        stream = HueEntertainmentStream(bridge, SyncSettings(hue_rate_hz=args.rate))
        stream.start()
        if not await stream.wait_ready(15):
            print(f"Stream did not start: {stream.status_text()}", file=sys.stderr)
            await stream.stop()
            return 1
        area = stream.area
        assert area is not None
        chans = [c.channel_id for c in area.channels]
        print(f"Streaming to {area.name!r}: {len(chans)} channels at up to {args.rate:.0f} Hz")
        t0 = time.monotonic()
        next_print = t0 + 1.0
        frames = 0
        half = None
        try:
            while (now := time.monotonic()) - t0 < args.seconds:
                if args.pattern == "positions":
                    second = now - t0 >= args.seconds / 2
                    if second != half:
                        half = second
                        print("  left = green, right = magenta" if second else
                              "  front (screen side) = red, back = blue", flush=True)
                    colors = positions(area, second)
                else:
                    phase = (now - t0) / 4.0
                    colors = np.array([colorsys.hsv_to_rgb((phase + i / max(len(chans), 1)) % 1.0, 1.0, 1.0)
                                       for i in range(len(chans))], np.float32)
                stream.write(chans, colors)
                frames += 1
                if now >= next_print:
                    next_print += 1.0
                    print(f"  {stream.status_text()}  (packets sent {stream.sent})", flush=True)
                await asyncio.sleep(1 / 60)  # like a 60 fps capture loop
        finally:
            await stream.stop()
        elapsed = max(time.monotonic() - t0, 1e-3)
        print(f"Stopped. {stream.sent} packets in {elapsed:.1f} s ({stream.sent / elapsed:.1f}/s), {frames} frames.")
        after = await HueApi(bridge.host, bridge.app_key).area(bridge.entertainment_id)
        print(f"Area status now: {after.status}")
        return 0

    _set_timer_resolution(True)  # 1 ms sleeps on Windows, as while syncing
    try:
        return asyncio.run(run())
    finally:
        _set_timer_resolution(False)


def _print_stats(engine) -> None:  # noqa: ANN001
    st = engine.stats
    status = ", ".join(f"{engine.config.light(k).name if engine.config.light(k) else k}={v}" for k, v in st.lights.items())
    if st.streams:
        status += " | " + ", ".join(f"Hue {k}: {v}" for k, v in st.streams.items())
    print(
        f"\r[{st.backend}{' HDR' if st.hdr else ''}] screen {st.capture_fps:5.1f} fps | loop {st.loop_fps:5.1f} | "
        f"process {st.process_ms:4.1f} ms | to-network {st.latency_ms:4.1f} ms | {status}"
        + (f" | {st.error}" if st.error else ""),
        end="   ",
        flush=True,
    )


def _save_portal_token(path: Path, token: str) -> None:
    """Persist a new Wayland portal restore token (they are single use).

    Reloads the file so command-line overrides (--fps, --monitor) aren't saved.
    """
    cfg = load_config(path)
    cfg.sync.portal_restore_token = token
    save_config(cfg, path)


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import SyncEngine

    path = _cfg_path(args)
    cfg = load_config(path)
    saved_token = cfg.sync.portal_restore_token
    if args.fps:
        cfg.sync.target_fps = args.fps
    if args.monitor is not None:
        cfg.sync.monitor = args.monitor
    if not cfg.lights:
        print("No lights configured. Use the GUI or `espsync-cli add-light`.", file=sys.stderr)
        return 2
    engine = SyncEngine(cfg)
    engine.want_preview = False
    stop = False

    def _sig(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    engine.start()
    t0 = time.monotonic()
    try:
        while not stop and (args.duration <= 0 or time.monotonic() - t0 < args.duration):
            time.sleep(1.0)
            _print_stats(engine)
            if cfg.sync.portal_restore_token != saved_token:
                saved_token = cfg.sync.portal_restore_token
                _save_portal_token(path, saved_token)
    finally:
        print("\nStopping...")
        engine.shutdown()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    import numpy as np

    from .capture import create_backend
    from .color import ColorProcessor
    from .config import SyncSettings

    path = _cfg_path(args)
    token = load_config(path).sync.portal_restore_token
    s = SyncSettings(capture_backend=args.backend, monitor=args.monitor or 0, portal_restore_token=token)
    backend = create_backend(s)
    if s.portal_restore_token != token:
        _save_portal_token(path, s.portal_restore_token)
    proc = ColorProcessor(s)
    regions = np.random.default_rng(0).uniform(0, 1, (args.regions, 4)).astype(np.float32)
    regions[:, 2:] = np.maximum(regions[:, :2] + 0.05, regions[:, 2:])
    regions = np.clip(regions, 0, 1)
    frames, proc_ms = 0, []
    frame = None
    t_end = time.perf_counter() + args.seconds
    while time.perf_counter() < t_end:
        got = backend.grab(100)
        if got is None:
            continue
        frame = got
        t1 = time.perf_counter()
        proc.process(frame, regions)
        proc_ms.append((time.perf_counter() - t1) * 1000)
        frames += 1
    backend.close()
    hdr = " HDR" if frame is not None and frame.hdr else ""
    print(f"backend={backend.name}{hdr} frames={frames} ({frames / args.seconds:.1f}/s, only changed frames count)")
    if proc_ms:
        print(f"colour processing for {args.regions} regions: median {np.median(proc_ms):.2f} ms, "
              f"p95 {np.percentile(proc_ms, 95):.2f} ms")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="espsync-cli", description="EspSync headless control")
    p.add_argument("--config", help=f"config file (default {default_config_path()})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="find ESPHome devices via mDNS")
    s.add_argument("--timeout", type=float, default=3.0)
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("lights", help="list the lights on a device")
    s.add_argument("host")
    s.add_argument("--port", type=int, default=6053)
    s.add_argument("--key", help="API encryption key (base64)")
    s.add_argument("--password")
    s.set_defaults(func=cmd_lights)

    s = sub.add_parser("hue-discover", help="find Philips Hue bridges via mDNS")
    s.add_argument("--timeout", type=float, default=3.0)
    s.set_defaults(func=cmd_hue_discover)

    s = sub.add_parser("hue-pair", help="pair a Hue bridge (press its link button)")
    s.add_argument("host", nargs="?", help="bridge IP (default: the one bridge found via mDNS)")
    s.add_argument("--name")
    s.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the link button")
    s.set_defaults(func=cmd_hue_pair)

    s = sub.add_parser("hue-areas", help="list a bridge's entertainment areas")
    s.add_argument("--bridge", help="bridge name/id/host (default: the only one)")
    s.set_defaults(func=cmd_hue_areas)

    s = sub.add_parser("hue-import", help="stream to an entertainment area and add its lights")
    s.add_argument("area", help="area name or id")
    s.add_argument("--bridge", help="bridge name/id/host (default: the only one)")
    s.add_argument("--group-gradients", action="store_true",
                   help="one strip per gradient light instead of one lamp per channel")
    s.add_argument("--replace", action="store_true", help="remove this bridge's existing Hue lights first")
    s.set_defaults(func=cmd_hue_import)

    s = sub.add_parser("hue-test", help="stream a colour chase to the area (checks the Entertainment stream)")
    s.add_argument("--bridge", help="bridge name/id/host (default: the only one)")
    s.add_argument("--seconds", type=float, default=10.0)
    s.add_argument("--rate", type=float, default=50.0, help="max packets per second")
    s.add_argument("--pattern", choices=["chase", "positions"], default="chase",
                   help="positions: front red / back blue, then left green / right magenta")
    s.set_defaults(func=cmd_hue_test)

    s = sub.add_parser("monitors", help="list displays")
    s.add_argument("--backend", default="auto", choices=BACKEND_CHOICES)
    s.set_defaults(func=cmd_monitors)

    s = sub.add_parser("show", help="print the config")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("add-device", help="add an ESPHome device to the config")
    s.add_argument("--host", required=True)
    s.add_argument("--name")
    s.add_argument("--port", type=int, default=6053)
    s.add_argument("--key")
    s.add_argument("--password")
    s.set_defaults(func=cmd_add_device)

    s = sub.add_parser("add-light", help="add a light to the config")
    s.add_argument("--name", required=True)
    s.add_argument("--device", help="device name/id/host")
    s.add_argument("--entity", help="light object_id on the device")
    s.add_argument("--kind", choices=["lamp", "strip"], default="lamp")
    s.add_argument("--transport", choices=["api", "wled", "e131", "virtual"], default="api",
                   help="Hue lights: use hue-import")
    s.add_argument("--x", type=float, default=0.0, help="-1 left wall .. 1 right wall")
    s.add_argument("--y", type=float, default=-0.8, help="-1 screen wall .. 1 back wall")
    s.add_argument("--z", type=float, default=0.5, help="0 floor .. 1 ceiling")
    s.add_argument("--leds", type=int, default=30)
    s.add_argument("--preset", choices=list(SCREEN_PRESETS), help="strip screen layout")
    s.add_argument("--udp-host")
    s.add_argument("--effect", help="ESPHome effect to enable for UDP (e.g. WLED, E1.31)")
    s.add_argument("--universe", type=int, default=1)
    s.set_defaults(func=cmd_add_light)

    s = sub.add_parser("run", help="run the sync headless")
    s.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl+C)")
    s.add_argument("--fps", type=int)
    s.add_argument("--monitor", type=int)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("bench", help="benchmark capture + colour processing")
    s.add_argument("--seconds", type=float, default=3.0)
    s.add_argument("--regions", type=int, default=300)
    s.add_argument("--backend", default="auto", choices=BACKEND_CHOICES)
    s.add_argument("--monitor", type=int)
    s.set_defaults(func=cmd_bench)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
