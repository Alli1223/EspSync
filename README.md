# EspSync

Screen-to-light sync (think Philips Hue Sync / ambilight) for **ESPHome** lights,
on **Windows and Linux**.

* **Direct and fast.** Talks to each ESP over its own native API (the protocol
  Home Assistant uses), or over UDP for addressable strips. Home Assistant is
  not in the path, and stays connected alongside EspSync.
* **Lights placed in your room.** Drag lights onto a top-down plan of the room:
  a lamp on the right takes the right of the screen, a floor lamp the bottom,
  a lamp behind the sofa a wider, softer area.
* **HDR that isn't washed out** (Windows). The desktop is captured as FP16 scRGB
  and tone-mapped, instead of the clipped 8-bit image most capture tools get.
* **Strips get per-LED colour** — e.g. a strip around the back of the TV, with
  presets for bottom/top/side edges, U-shapes and full loops.
* Live preview of what every light is sampling; letterbox detection; vivid or
  average colour modes; smoothing presets; lights restored when you stop.

## Install

Python 3.11+.

```bash
git clone https://github.com/Alli1223/EspSync.git
cd EspSync
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\pip install -e ".[gui]"
.venv\Scripts\espsync
```

Linux:

```bash
.venv/bin/pip install -e ".[gui]"
.venv/bin/espsync
```

(`python -m espsync` works too.)

## Quick start

1. **Devices… → Scan.** Your ESPHome nodes appear. Pick one → *Add selected…*,
   paste its **API encryption key** (from the device's YAML: `api: encryption: key:`),
   *Test connection*, OK.
2. Select the device → **Add its lights to the room**.
3. **Drag each light** to where it really is. Scroll over a lamp to set its
   height (floor ↔ ceiling). The screen preview shows the area each light samples;
   drag a box there if you want to override it (double-click to go back to automatic).
4. Hit **Start sync**.

`Identify (flash)` on the Light tab blinks the selected light so you can tell them apart.

## Getting the lowest latency

EspSync's side is fast. On Windows, a new frame takes about 3.5-6 ms to become
light updates on the wire, measured with an HDR game holding an RTX 3070 at 100%
(GPU readback, tone mapping and colour processing included). The rest is the
network and the ESP itself, and a few ESPHome settings matter a lot:

```yaml
wifi:
  power_save_mode: none   # ESP32's default modem sleep can delay each command by 100 ms or more

logger:
  level: INFO             # DEBUG logs every light update and slows the ESP down

api:
  encryption:
    key: !secret api_key
```

* EspSync sends every update with a **0 ms transition**, so the light's
  `default_transition_length` doesn't matter.
* **Rate**: Settings → *API rate* caps commands per light (default 30/s).
  ESP32s cope with 30-50/s; keep ESP8266s at ≤ 25.
* **Smoothing** adds perceived lag by design. Use *Instant* or *Fast* for games,
  *Smooth* for films.
* While a game is running, EspSync asks Windows for high GPU scheduling priority
  so its tiny capture job isn't stuck behind the game's frames. Measured on an
  RTX 3070 under load, this cut the capture wait from ~30 ms (p95 ~140 ms) to
  ~10 ms (p95 ~20 ms).

### Addressable strips (per-LED, UDP)

For strips, UDP realtime protocols skip the API entirely. Each frame is a single
datagram written straight to the LEDs. Add one of these effects to the light:

```yaml
light:
  - platform: esp32_rmt_led_strip   # or neopixelbus, fastled_clockless, ...
    id: tv_strip
    name: TV strip
    # ...pin, num_leds, chipset...
    effects:
      - wled:                        # WLED realtime protocol, UDP 21324
```

or E1.31/sACN:

```yaml
e131:
  method: unicast

light:
  - platform: esp32_rmt_led_strip
    # ...
    effects:
      - e131:
          universe: 1
          channels: RGB
```

In EspSync set the light's *Type* to **Addressable strip**, *Output* to **WLED
realtime UDP** (or **E1.31**), choose the device and light, and set *Effect* to
`WLED` / `E1.31`. EspSync then switches the effect on when sync starts and puts
the light back afterwards. The *LED count* must match the strip, and *Strip
layout* says where the LEDs sit: either along a screen edge preset, or between
two points you drag in the room.

## How it works

```
 Windows: DXGI Desktop Duplication ──► GPU mipmaps ──► ~200 px frame (FP16 if HDR)
 Linux:   X11 (mss)               ──► subsample  ──►         "
                                                     │
                       tone map (HDR) ─► letterbox detect ─► summed-area table
                                                     │
                    per-light regions (from room layout) ─► mean / vivid colour
                                                     │
                          smoothing ─► latest-value-wins per light (no queues)
                                                     │
              ESPHome native API (TCP, Noise)  /  WLED or E1.31 UDP
```

* **Room → screen mapping.** Room `x` (left ↔ right wall) maps to screen
  horizontal, height maps to screen vertical, and distance from the screen wall
  widens the sample area. *Horizontal spread* (Settings → Room) pushes side
  lights further toward the screen edges.
* **HDR.** With Windows HDR on, the capture is linear scRGB. EspSync divides by
  Windows' *SDR content brightness* (read live from the display config), so SDR
  white is 1.0. Highlights up to the display's peak are then rolled off with a
  hue-preserving soft knee. SDR content passes through untouched. Override the
  white level and peak in Settings if your display reports odd values.
* **Never behind.** Each light keeps only the newest colour. If a light (or the
  Wi-Fi) is slow, intermediate frames are dropped instead of queued, so lag
  can't build up.

## Command line / headless

```bash
espsync-cli discover                              # find ESPHome devices
espsync-cli lights lamp.local --key <base64key>   # list a device's lights
espsync-cli add-device --name Lamp --host lamp.local --key <key>
espsync-cli add-light --name "Left lamp" --device Lamp --entity rgb_lamp --x -0.9 --y -0.5 --z 0.6
espsync-cli add-light --name "TV" --kind strip --transport wled --device Strip \
    --entity tv_strip --effect WLED --leds 120 --preset "Around TV - U shape (left, top, right)"
espsync-cli monitors
espsync-cli bench                                 # capture + processing speed
espsync-cli run                                   # sync until Ctrl+C, prints live stats
```

GUI flags: `espsync --start --minimized` (start syncing straight away into the tray).

The config lives in `%APPDATA%\EspSync\config.json` on Windows and
`~/.config/EspSync/config.json` on Linux. Set `ESPSYNC_CONFIG` or pass `--config`
to use another file. API keys are stored there in plain text.

## Platform notes

| | Windows | Linux |
|---|---|---|
| Capture | DXGI Desktop Duplication (GPU downscale, HDR) | X11 via `mss` |
| HDR | Yes (FP16 + tone mapping) | No (SDR) |
| Wayland | — | Not yet: run an X11 session, or apps under XWayland |

* DRM-protected video (e.g. Netflix in a browser) is captured as black by Windows.
  Use a player or browser setting that turns hardware DRM off, or accept black.
* Exclusive-fullscreen games that switch display modes briefly interrupt capture.
  EspSync recovers automatically.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests cover mapping, colour/HDR maths, packet formats, rate limiting, the
CLI, and the GUI widgets (headless Qt).
