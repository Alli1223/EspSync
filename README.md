# EspSync

Screen-to-light sync (think Philips Hue Sync / ambilight) for **ESPHome** lights,
running on **Windows and Linux**.

* Talks to lights **directly** over the ESPHome native API (the same protocol
  Home Assistant uses) or over UDP for addressable strips — Home Assistant is
  not in the hot path, so latency is as low as your LAN allows.
* Lights are **positioned in a room layout** — a light on the right of the room
  takes its colour from the right of the screen, a light near the floor from
  the bottom, and so on.
* **HDR aware** on Windows: captures the desktop in FP16 scRGB and tone-maps it,
  so colours don't look washed out on an HDR display.

> Work in progress — see the sections below as they fill in.
