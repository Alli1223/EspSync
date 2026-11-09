# World of Warcraft integration

While you play, EspSync can mix two effects over the normal screen colours:

* **Low health:** the lights pulse red when your health drops below a threshold
  (35% by default). The lower your health, the faster and stronger the pulse.
* **Level-up:** a gold wedge sweeps round the room for about 3 seconds, then the
  room glows gold and fades back to the screen colours.

The effects work for every light EspSync drives, and they use each light's
position in the room layout.

## How it works

WoW addons can't open network connections or write files while you play, so
the small **EspSync addon** draws an 88 × 4 pixel strip in the top-left corner
of the screen instead. EspSync reads the strip from its screen capture and keeps
it out of the normal light colours. The strip holds:

| Part            | Colour                    | Meaning                                       |
|-----------------|---------------------------|-----------------------------------------------|
| marker          | magenta, green            | "this is the EspSync strip"                   |
| 2 data cells    | red/green/blue on or off  | level-up counter, dead/ghost flag, test flag  |
| check cell      | red/green/blue on or off  | catches misreads                              |
| health bar      | yellow fill on blue       | your health, filled from the left             |
| end marker      | cyan                      |                                               |

Health is shown as a status bar rather than a number. In Midnight (12.x) and
Forever, `UnitHealth("player")` is a *secret value*: addons can't do maths on it,
but status bars can still display it.

The addon is output only. It reads only your own character's health, level-ups
and dead/ghost state, never acts on the game, and has no network or file access.

## Install the addon

1. Quit WoW, or be ready to type `/reload` afterwards.
2. Copy the `EspSync` folder from this directory (`integrations/wow/EspSync`)
   into the `Interface/AddOns` folder of the game version you play:

   | Game version           | Folder                                               |
   |------------------------|------------------------------------------------------|
   | Retail (Midnight)      | `World of Warcraft/_retail_/Interface/AddOns/`       |
   | Forever                | `World of Warcraft/_forever_/Interface/AddOns/`      |
   | Classic (progression)  | `World of Warcraft/_classic_/Interface/AddOns/`      |
   | Classic Era            | `World of Warcraft/_classic_era_/Interface/AddOns/`  |
   | Anniversary            | `World of Warcraft/_anniversary_/Interface/AddOns/`  |

   Create `Interface/AddOns` if it doesn't exist yet. The result should be
   `.../Interface/AddOns/EspSync/EspSync.toc`.
3. Start WoW. On the character screen, open **AddOns** and check that EspSync is
   ticked. If it's listed as *out of date*, tick **Load out of date AddOns**.
4. In game, type `/espsync`. It should print the strip's size, and you should see a
   thin coloured line at the very top-left of the screen.

## Set up EspSync

* In EspSync's **Settings → Game effects**, keep **World of Warcraft** ticked and
  choose the low-health threshold.
* Capture the display WoW runs on. Use **Windowed (Fullscreen)** or
  **Fullscreen** display mode: in a smaller window the strip isn't in the
  screen's corner and EspSync won't find it.
* The effects switch on only while `Wow.exe` or `WowClassic.exe` is running
  *and* EspSync can see the strip. The status bar shows `WoW` with your health
  once the strip is found.
* This needs the DXGI capture backend (Windows default) or mss. The Wayland
  portal backend hands over a downscaled picture, which is too small to read the
  strip.

## Test it

| Command                        | What it does                                               |
|--------------------------------|------------------------------------------------------------|
| `/espsync test levelup`        | plays the level-up sweep                                   |
| `/espsync test lowhp [percent]`| shows `percent` health (default 20) for 15 s, so the pulse starts |
| `/espsync test off`            | ends the health test                                       |
| `/espsync hide`, `/espsync show` | hides or shows the strip (hidden = effects off)          |

For a real low-health test, take some falling damage or fight a target dummy.

## Troubleshooting

* **No effects, status says "addon not seen":** check that the strip is visible at
  the top-left corner. UI-replacement addons that move or shrink `UIParent` (some
  ultrawide setups do this) move the strip too. Hiding the UI (Alt+Z) and
  cinematics hide the strip, which pauses the effects. That's intended.
* **Wrong display:** EspSync looks at the top-left corner of the display chosen in
  **Settings → Capture → Display**.
