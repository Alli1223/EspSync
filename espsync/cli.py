"""Headless command line interface.

    espsync-cli discover                 # find ESPHome devices on the LAN
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

from .config import AppConfig, DeviceConfig, LightConfig, default_config_path, load_config, save_config
from .mapping import SCREEN_PRESETS

BACKEND_CHOICES = ["auto", "dxgi", "mss"]


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


def _print_stats(engine) -> None:  # noqa: ANN001
    st = engine.stats
    status = ", ".join(f"{engine.config.light(k).name if engine.config.light(k) else k}={v}" for k, v in st.lights.items())
    print(
        f"\r[{st.backend}{' HDR' if st.hdr else ''}] screen {st.capture_fps:5.1f} fps | loop {st.loop_fps:5.1f} | "
        f"process {st.process_ms:4.1f} ms | to-network {st.latency_ms:4.1f} ms | {status}"
        + (f" | {st.error}" if st.error else ""),
        end="   ",
        flush=True,
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import SyncEngine

    cfg = load_config(_cfg_path(args))
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
    finally:
        print("\nStopping...")
        engine.shutdown()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    import numpy as np

    from .capture import create_backend
    from .color import ColorProcessor
    from .config import SyncSettings

    s = SyncSettings(capture_backend=args.backend, monitor=args.monitor or 0)
    backend = create_backend(s)
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


def cmd_update(args: argparse.Namespace) -> int:
    from . import updater

    current = args.pretend_version or updater.current_version()
    try:
        info = updater.check(current)
    except Exception as exc:  # noqa: BLE001 - network/API errors
        print(f"Couldn't check for updates: {exc}", file=sys.stderr)
        return 1
    if info is None:
        print(f"EspSync {current} is up to date.")
        return 0
    print(f"EspSync {info.version} is available (you have {current}): {info.page_url}")
    if args.check:
        return 0
    if not (args.download_only or updater.is_installed_build()):
        print("This copy wasn't installed with the EspSync installer; download it from the release page.")
        return 0

    def progress(done: int, total: int) -> None:
        print(f"\r  downloading {done / 1e6:5.1f} / {total / 1e6:.1f} MB", end="", flush=True)

    try:
        path = updater.download(info, progress=progress)
    except updater.UpdateError as exc:
        print(f"\nUpdate failed: {exc}", file=sys.stderr)
        return 1
    print(f"\n  verified {path} (sha256 {info.sha256[:16] + '...' if info.sha256 else 'not published'})")
    if args.download_only:
        return 0
    updater.launch_installer(path, relaunch=False)
    print("Installer started; it will close EspSync if it is running.")
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
    s.add_argument("--transport", choices=["api", "wled", "e131", "virtual"], default="api")
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

    s = sub.add_parser("update", help="check GitHub for a newer release and install it")
    s.add_argument("--check", action="store_true", help="only report whether an update exists")
    s.add_argument("--download-only", action="store_true", help="download and verify, don't install")
    s.add_argument("--pretend-version", help=argparse.SUPPRESS)  # testing: act as an older version
    s.set_defaults(func=cmd_update)
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
