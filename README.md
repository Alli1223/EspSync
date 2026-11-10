# EspSync

Screen-to-light sync (think Philips Hue Sync / ambilight) for **ESPHome** lights,
and **Philips Hue** lights alongside them, on **Windows and Linux**.

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
* **Philips Hue too**, over the Hue Entertainment API (the low-latency stream
  Hue Sync uses), so Hue and ESPHome lights sync together. See
  [Philips Hue](#philips-hue).
* Live preview of what every light is sampling; letterbox detection; vivid or
  average colour modes; smoothing presets; lights restored when you stop.
* **World of Warcraft effects.** With the small EspSync addon, the lights pulse
  red when your health is low and a gold sweep circles the room when you level
  up. See [integrations/wow](integrations/wow/README.md).
* **Overwatch effects**, no addon needed: a red flash when you take damage, a
  heartbeat at critical health, a gold wave when you are healed, and the room
  draining to grey when you are eliminated. See [Overwatch](#overwatch).
* **Scenes.** One click on *Warm white*, *Relax*, *Daylight*… stops the sync and
  puts every light back on a normal white. See [Scenes](#scenes).

## Install

### Windows installer

Download `EspSync-Setup-<version>.exe` from the
[Releases page](https://github.com/Alli1223/EspSync/releases). For the latest
build from `main`, open the newest
[windows-installer run](https://github.com/Alli1223/EspSync/actions/workflows/windows-installer.yml)
and download it under *Artifacts*. Run it; no Python needed.

* By default it installs just for you, in `%LOCALAPPDATA%\Programs\EspSync`, with
  no admin prompt. The wizard can install for all users instead.
* Optional desktop shortcut, and an option to start EspSync in the tray when you sign in.
* Installs both `EspSync.exe` (the app) and `espsync-cli.exe` (command line).
* Upgrading: run a newer installer over the old one. Your settings in
  `%APPDATA%\EspSync` are kept, and are also kept on uninstall.
* The installer isn't code-signed, so SmartScreen may say "Windows protected
  your PC": click *More info → Run anyway*.
* The app logs to `%LOCALAPPDATA%\EspSync\Logs\espsync.log`.

**Automatic updates.** The installed app checks GitHub Releases shortly after it
starts and then every 6 hours. When there's a newer release, it downloads the
installer and checks it against the SHA-256 GitHub publishes for the file. It
then closes (restoring your lights), updates in place and starts again.
* It never updates in the middle of a sync session. It waits until you stop syncing.
* If the window is open you get a 30-second countdown with *Install now / Skip
  this version / Later*. From the tray it just updates and shows a notification.
* Turn it off, or switch to "ask first", under Setup → Updates. *Check now* is
  there and in the tray menu.
* From the command line: `espsync-cli update` (or `update --check`).

To publish a release, bump `__version__` in `espsync/__init__.py`, commit, and
push a version tag. CI builds the installer and attaches it, and installed copies
pick it up on their next check:

```bash
git tag v0.2.0
```

```bash
git push origin v0.2.0
```

To build the installer locally, you need [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```bash
pip install . PySide6-Essentials pyinstaller
```

```bash
pyinstaller packaging/espsync.spec --noconfirm
```

```bash
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

### From source

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

Linux (X11):

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
* **Rate**: Setup → *API rate* caps commands per light (default 30/s).
  ESP32s cope with 30-50/s; keep ESP8266s at ≤ 25.
* **Smoothing** adds perceived lag by design. Use *Instant* or *Fast* for games,
  *Smooth* for films.
* While a game is running, EspSync asks Windows for high GPU scheduling priority
  so its tiny capture job isn't stuck behind the game's frames. Measured on an
  RTX 3070 under load, this cut the capture wait from ~30 ms (p95 ~140 ms) to
  ~10 ms (p95 ~20 ms).

### Keep Home Assistant's database small

The ESP reports every colour change back to all connected clients, Home
Assistant included. At 30 updates a second, HA's recorder would store thousands
of state rows per minute for each synced light, so exclude those lights:

```yaml
# Home Assistant configuration.yaml
recorder:
  exclude:
    entities:
      - light.living_room_lamp
      - light.tv_backlight
```

Strips driven over UDP (below) don't have this problem: while the effect runs,
the light's state doesn't change.

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

## Philips Hue

EspSync streams to Hue lights with the **Entertainment API**: one encrypted UDP
packet per frame (DTLS 1.2, ~50 a second) carries every Hue light's colour, the
same way Hue Sync does. Ordinary REST commands would be limited to about 10 a
second for the whole bridge.

1. In the **Hue app**, create an **entertainment area** (Settings → Entertainment
   areas) with the lights you want, placed roughly where they are.
2. In EspSync: **Devices… → Philips Hue**. Your bridge shows up under *Found on
   your network* (or use *Pair by IP address…*). Click **Pair selected…** and press
   the round **link button** on the bridge.
3. Choose the **entertainment area**, then **Add the area's lights to the room**.
   Lights land where the Hue app has them (Hue's "front" is the screen side). A
   gradient lightstrip becomes one light per segment, or one strip if you tick
   *Gradient lightstrips as one strip*.
4. **Start sync.** The status bar shows `Hue <bridge>: streaming 50 Hz`.

Notes:

* A bridge streams **one entertainment area at a time, for one app**. If Hue
  Sync (or a TV, or a Sync Box) is streaming, EspSync says so and waits; it
  picks the stream up as soon as the other app lets go.
* When sync stops, EspSync puts the Hue lights back the way they were (the
  bridge itself leaves them on the last streamed colour). Untick *Restore lights to
  how they were when sync stops* in Settings to keep the last colours.
* Each Hue light's *Output* is **Philips Hue (Entertainment)**, with the bridge
  and the entertainment channels it drives. A lamp sends one colour to all ticked
  channels; a strip sends one LED per channel. *Setup → Hue rate* sets the
  packets per second (the bridge drives the lights at about 25 Hz; 50 rides
  out lost packets).
* The pairing key is stored in the config file, like ESPHome keys. EspSync
  doesn't check the bridge's HTTPS certificate (Hue bridges use their own CA);
  the stream itself is authenticated with the pairing key.

## Scenes

The row of scene buttons at the top of the window (also under *Scene* in the
tray menu) sets every synced light to a steady colour:

| Scene | Colour | Brightness |
| --- | --- | --- |
| Warm white | 2700 K | 100% |
| Relax | 2200 K | 55% |
| Neutral | 4000 K | 100% |
| Daylight | 5500 K | 100% |
| Night light | 2000 K | 10% |
| Off | lights off | |

Clicking one while syncing stops the sync first; the scene then takes the place
of "restore lights when sync stops". ESPHome lights with white channels use them
(colour temperature, cold/warm white or plain white), RGB-only lights get the
nearest RGB colour, and a running effect (including a strip's WLED / E1.31
effect) is switched off. Hue lights get the colour temperature over the bridge's
REST API. A strip configured with only a UDP address has no API to set a colour
with, so it is skipped: give it its ESPHome device and light.

From the command line: `espsync-cli scene warm` (`espsync-cli scene` lists them).

## Overwatch

Turn it on under *Games → Overwatch* (it is on by default). It is active only
while `Overwatch.exe` runs.

| In the game | In the room |
| --- | --- |
| You take damage | A sharp red flash, stronger the bigger the hit |
| Health below the *Critical below* setting | A red double-beat heartbeat that quickens as health drops |
| You are healed | A warm gold wave from the back of the room to the screen |
| You are eliminated | A red flash, then the room drains to a dim grey-red until you are back |
| You respawn | A cold blue-white burst from the screen wall to the back of the room |

The *Try it* buttons play each effect without the game.

**How it reads the game.** Overwatch has no addon or telemetry API, so EspSync
reads the health bar in the bottom-left of the HUD from the screen capture it
already makes. It never touches the game's process or memory. Health, armour and
shields all count; the bar's length over its full width is your health.

* Keep the HUD at its default size and position. The reader finds the bar by
  its shape (thickness, tilt, the name plate under it), locks on to that spot
  after seeing it a few times with at least 40% health, and from then on reads
  only that spot. The status bar shows `Overwatch: health 76%` once it has it.
* An elimination is inferred: the bar disappears right after you took damage, or
  while your health was critical. Opening a menu at full health is not one. A
  one-shot from full health that never shows a damaged bar is missed.
* It was built from measurements of 16:9 and 21:9 gameplay footage and is tested
  against drawn copies of the HUD, not yet against every hero, resolution and
  HUD setting. To check it on your setup, run `espsync-cli overwatch-hud` while
  playing: it prints what it reads and where it found the bar.
* Works in SDR and HDR, borderless or windowed-fullscreen. Exclusive fullscreen
  can interrupt capture (see [Platform notes](#platform-notes)).

## How it works

```
 Windows: DXGI Desktop Duplication ──► GPU mipmaps ──► ~200 px frame (FP16 if HDR)
 X11:     mss                     ──► subsample  ──►         "
                                                     │
                       tone map (HDR) ─► letterbox detect ─► summed-area table
                                                     │
                    per-light regions (from room layout) ─► mean / vivid colour
                                                     │
                          smoothing ─► game effects (WoW, Overwatch) ─► latest-value-wins per light
                                                     │
   ESPHome native API (TCP, Noise)  /  WLED or E1.31 UDP  /  Hue Entertainment (DTLS)
```

* **Room → screen mapping.** Room `x` (left ↔ right wall) maps to screen
  horizontal, height maps to screen vertical, and distance from the screen wall
  widens the sample area. *Horizontal spread* (Setup → Room) pushes side
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
espsync-cli hue-pair                              # pair the Hue bridge (press its link button)
espsync-cli hue-areas                             # entertainment areas, channels, positions
espsync-cli hue-import "TV area"                  # stream to that area, add its lights
espsync-cli hue-test --pattern positions          # front red / back blue, left green / right magenta
espsync-cli monitors
espsync-cli bench                                 # capture + processing speed
espsync-cli run                                   # sync until Ctrl+C, prints live stats
espsync-cli scene warm                            # set every light to a scene (no name: list them)
espsync-cli overwatch-hud                         # print what the Overwatch health reader sees
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
| Wayland | — | Not supported at the moment: run an X11 session, or apps under XWayland |

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
