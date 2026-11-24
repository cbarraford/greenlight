# The app icon: Aperture

Six deep-green blades open on a hexagon of light. The agents step aside and the way clears. The opening is the board's own
cell (a pointy-top hexagon), so the app icon, the menu bar mark and the orb are one form (brand/DESIGN.md §8).

`generate.py` draws everything here from one geometry; edit it, not the SVGs, then run `python3 brand/icon/generate.py`.

| File | What it is |
|---|---|
| `layers/01-background.svg` | Default appearance: the ground, a vertical gradient `#1C2F23` → `#0C1B11` (neutral hue 155, L 0.285 → 0.205) |
| `layers/02-light.svg` | The light: the hexagon the blades leave open, Signal `#5DEF88` (OKLCH 0.85 0.19 150) |
| `layers/03-blades.svg` | The six blades, `#295C39` (OKLCH 0.43 0.08 152) |
| `layers-light/` | The same three groups in the light appearance: a pale ground, pale blades, a Signal-green light |
| `previews/*.svg` | Each appearance and the Debug variant in an approximate squircle (for review only) |
| `mark.svg` | The mark, 16 grid, `currentColor`: the blades at 50% and the light solid. The About box, the orb's glyph, and the menu bar while agents work |
| `mark-idle.svg` | The blades alone, the opening empty: the menu bar at rest |

The layers are square 1024 canvases, unmasked, with no glow, shadow, blur or specular: Icon Composer adds the squircle
and the glass. The light covers exactly the opening, so no blade overlaps it. In tinted and clear modes the blades turn
translucent, and they never show the light through them.

## Builds

- **`.icns` (the swiftc build, and macOS before 26):** `scripts/build_icons.sh` on a Mac (it needs `rsvg-convert` from
  `brew install librsvg`, plus `iconutil`). It composites `layers/` inside the macOS icon grid's squircle (an 824 tile in
  1024), renders 16–512 @1x/@2x, and writes `brand/icons/Greenlight.icns` and `brand/icons/GreenlightDebug.icns` (the same
  icon with a neutral-grey light `#BABFBB`). Commit both. `app/build.sh` copies them into the bundle. Its
  `CFBundleIconFile` is `Greenlight`, or `GreenlightDebug` for a dev copy (`GREENLIGHT_DEV`).
- **`.icon` (macOS 26+):** build `brand/icon/Greenlight.icon` in Icon Composer from `layers/` with the settings below.
  Whether `actool` can compile it from the command line on the mini is still open (DESIGN.md §8.5 step 4). Until then
  the bundle ships the `.icns`.

## Icon Composer settings

Import the three files in `layers/` as one background and two groups, back to front: **Background**, **Light**,
**Blades**. Each sits at 0,0 at 100% scale on the 1024 canvas. That's 3 of the 4 groups allowed.

| Appearance | Background | Light (group 1) | Blades (group 2) |
|---|---|---|---|
| Default | Gradient fill `#1C2F23` → `#0C1B11`, top to bottom | `#5DEF88` | `#295C39` |
| Dark | `#0C170F` → `#040905` | `#55E982` | `#1C472A` |
| Light (optional override, `layers-light/`) | `#F1F9F3` → `#DEEAE2` | `#03A14A` | `#A2C9AB` |
| Clear light / clear dark | System clear (no fill) | White, 100% | White, 40% |
| Tinted light / tinted dark | System tint | White, 100% (the near-white part the tint keys on) | White, 36% |

Per-group glass:

| Group | Specular | Translucency | Shadow | Why |
|---|---|---|---|---|
| Light | **Outside** in default, dark, clear and tinted; **inside** in the light override | Off | Chromatic, low | It's lighter than the ground everywhere except the light override, where it's darker. |
| Blades | **Outside** in default, dark, clear and tinted; **inside** in the light override | Low (about 20%) in default and dark, system in clear and tinted | Neutral | The blades are L 0.43 on a 0.25 ground (lighter), and L 0.80 on a 0.95 ground in the light override (darker). |

The rule is DESIGN.md §8.2's: specular goes inside when the group is darker than the background, and outside when it's
lighter. Turn **Blur material** off for both groups. The icon stays flat; the glass is the system's.

Check it at 16 px, in greyscale, and against a light and a dark wallpaper on both macOS 26 and 27. Look again at the
gaps between the blades on 27, which renders sharper with less glass.
