---
# Greenlight design tokens (the single source).
# Colours are OKLCH "L C H". Every scale has 12 steps and each step has one job (section 2).
# The sRGB hex under each row is the gamut-mapped fallback: chroma is lowered, never clipped.
# The generator (tokens.py, PR 1) emits dashboard/terminal/stage CSS and Swift's Theme from this file.
version: 1
theme:
  # A theme is these inputs and nothing else. Everything below the primitives is derived from them.
  inputs:
    neutral_hue: 0        # unused while the chroma is 0
    neutral_chroma: 0     # true grey, like the system's own windows. 0 to 0.02; above about 0.02 the greys turn muddy.
    accent: signal        # signal | teal | graphite (section 2.3)
    contrast: standard    # standard | increased (it follows the macOS Increase Contrast setting)
  appearance: system      # system | light | dark. The system is the default and the others are an opt-in override.

primitives:
  # Steps 1–12. Measured Radix lightness curves, a chroma peak at steps 9–10, and text chroma below the fill.
  # Dark steps 1–8 start at the macOS window grey (L 0.235, #1E1E1E) on every scale, so a tint never sits darker than its surface.
  neutral:
    light: ["1.000 0 0", "0.982 0 0", "0.962 0 0", "0.938 0 0", "0.914 0 0", "0.889 0 0", "0.852 0 0", "0.790 0 0", "0.640 0 0", "0.600 0 0", "0.500 0 0", "0.235 0 0"]
    # light sRGB: #FFFFFF #F9F9F9 #F2F2F2 #EAEAEA #E2E2E2 #DADADA #CECECE #BABABA #8C8C8C #808080 #636363 #1E1E1E
    dark:  ["0.235 0 0", "0.277 0 0", "0.310 0 0", "0.340 0 0", "0.370 0 0", "0.405 0 0", "0.450 0 0", "0.530 0 0", "0.580 0 0", "0.620 0 0", "0.800 0 0", "0.950 0 0"]
    # dark sRGB:  #1E1E1E #282828 #303030 #383838 #404040 #494949 #555555 #6C6C6C #7A7A7A #868686 #BEBEBE #EEEEEE
  signal:   # the brand accent: go-green
    light: ["0.993 0.009 150", "0.982 0.018 150", "0.962 0.037 150", "0.938 0.054 150", "0.914 0.069 150", "0.889 0.084 150", "0.852 0.102 150", "0.790 0.128 150", "0.530 0.146 150", "0.490 0.135 150", "0.500 0.128 150", "0.235 0.057 150"]
    # light sRGB: #F9FFFA #F1FDF3 #E2FAE6 #D2F5D8 #C3F0CB #B3EBBD #9EE2AB #7AD28E #00823A #007433 #177739 #05250F
    dark:  ["0.235 0.018 150", "0.277 0.024 150", "0.310 0.045 150", "0.340 0.063 150", "0.370 0.075 150", "0.405 0.087 150", "0.450 0.102 150", "0.530 0.123 150", "0.550 0.150 150", "0.605 0.150 150", "0.800 0.105 150", "0.950 0.048 150"]
    # dark sRGB:  #18211A #202C22 #1F3724 #1D4126 #1E4B2A #1F562F #216535 #297F43 #05893E #289A4E #8BD19A #D9F8DE
    # dark step 10 sits at L 0.605, not 0.590: Phosphor inks its dim text in it on neutral.1, which needs 4.5:1 (4.62)
  amber:    # status: needs you
    light: ["0.993 0.004 62", "0.982 0.010 62", "0.962 0.023 62", "0.938 0.039 62", "0.914 0.056 62", "0.889 0.074 62", "0.852 0.102 62", "0.790 0.145 62", "0.760 0.170 62", "0.720 0.165 62", "0.500 0.114 62", "0.235 0.054 62"]
    # light sRGB: #FFFCFA #FEF8F2 #FEEFE3 #FEE5D1 #FFDBBD #FFD0A9 #FFC088 #FCA34D #FC9417 #EC8904 #905203 #301700
    dark:  ["0.235 0.020 62", "0.277 0.027 62", "0.310 0.051 62", "0.340 0.064 62", "0.370 0.071 62", "0.405 0.080 62", "0.450 0.092 62", "0.530 0.112 62", "0.780 0.160 62", "0.820 0.127 62", "0.800 0.119 62", "0.950 0.031 62"]
    # dark sRGB:  #251C14 #32251A #432A13 #4F3010 #5A3612 #673E14 #794815 #995B17 #FF9D36 #FFB169 #F5AC6A #FEEADA
  yellow:   # status: your move
    light: ["0.993 0.010 95", "0.982 0.020 95", "0.962 0.041 95", "0.938 0.059 95", "0.914 0.076 95", "0.889 0.092 95", "0.852 0.112 95", "0.790 0.140 95", "0.880 0.165 95", "0.840 0.165 95", "0.500 0.102 95", "0.235 0.048 95"]
    # light sRGB: #FFFDF5 #FDF9EA #FBF3D4 #F7EBBE #F2E3A9 #EDDA94 #E5CE75 #D7B93F #FAD53B #ECC825 #766202 #251D00
    dark:  ["0.235 0.020 95", "0.277 0.026 95", "0.310 0.050 95", "0.340 0.057 95", "0.370 0.063 95", "0.405 0.071 95", "0.450 0.082 95", "0.530 0.100 95", "0.880 0.165 95", "0.920 0.127 95", "0.800 0.115 95", "0.950 0.053 95"]
    # dark sRGB:  #211E13 #2C2819 #38300F #413711 #4A3F13 #554815 #645415 #7E6B17 #FAD53B #FFE47D #D5BD62 #F9EFC7
  blue:     # status: working
    light: ["0.993 0.003 252", "0.982 0.008 252", "0.962 0.018 252", "0.938 0.030 252", "0.914 0.042 252", "0.889 0.055 252", "0.852 0.074 252", "0.790 0.109 252", "0.550 0.164 252", "0.510 0.152 252", "0.500 0.149 252", "0.235 0.068 252"]
    # light sRGB: #FBFDFF #F5FAFF #EAF4FF #DDECFF #CFE5FF #C1DEFF #ACD2FE #86BFFF #0172CD #0167B9 #0164B4 #011E3D
    dark:  ["0.235 0.022 252", "0.277 0.029 252", "0.310 0.054 252", "0.340 0.076 252", "0.370 0.090 252", "0.405 0.104 252", "0.450 0.119 252", "0.530 0.146 252", "0.560 0.167 252", "0.600 0.179 252", "0.800 0.103 252", "0.950 0.024 252"]
    # dark sRGB:  #171F28 #1E2936 #1B324B #16395E #16416D #154A7F #175795 #186DBD #0275D2 #0281E6 #8CC2FE #E3F0FF
  red:      # status: crashed / failed
    light: ["0.993 0.003 25", "0.982 0.008 25", "0.962 0.018 25", "0.938 0.031 25", "0.914 0.044 25", "0.889 0.058 25", "0.852 0.080 25", "0.790 0.121 25", "0.570 0.200 25", "0.530 0.200 25", "0.500 0.170 25", "0.235 0.076 25"]
    # light sRGB: #FFFCFC #FFF7F6 #FFEEEC #FFE3E0 #FFD8D4 #FFCCC7 #FFBBB4 #FF9B93 #D42F34 #C51D28 #B02A2D #3B0A0A
    dark:  ["0.235 0.024 25", "0.277 0.032 25", "0.310 0.060 25", "0.340 0.084 25", "0.370 0.100 25", "0.405 0.116 25", "0.450 0.136 25", "0.530 0.164 25", "0.570 0.200 25", "0.610 0.200 25", "0.800 0.114 25", "0.950 0.024 25"]
    # dark sRGB:  #281A18 #362220 #4A2321 #5C2321 #6B2523 #7C2927 #922D2C #B83838 #D42F34 #E23F40 #FFA098 #FEE9E6
  clay:     # the Claude agent mark only (a graphic, so 3:1 is enough). Never used as UI colour.
    light: ["0.993 0.003 40", "0.982 0.008 40", "0.962 0.019 40", "0.938 0.032 40", "0.914 0.045 40", "0.889 0.060 40", "0.852 0.082 40", "0.790 0.102 40", "0.640 0.120 40", "0.600 0.120 40", "0.500 0.102 40", "0.235 0.046 40"]
    dark:  ["0.235 0.014 40", "0.277 0.019 40", "0.310 0.036 40", "0.340 0.050 40", "0.370 0.060 40", "0.405 0.070 40", "0.450 0.082 40", "0.530 0.098 40", "0.680 0.120 40", "0.720 0.120 40", "0.800 0.084 40", "0.950 0.025 40"]

contrast_checks:   # measured on the values above; tokens.py re-checks them in CI (section 13)
  step11_on_step2: "APCA Lc 61–77 on every scale, both modes (WCAG 5.3–8.2:1)"
  step12_on_step2: "APCA Lc 92–100 on every scale, both modes (WCAG ≥ 12.7:1)"
  on_solid9:
    signal: "white, 4.94:1 light / 4.51:1 dark"
    blue:   "white, 4.90 / 4.69"
    red:    "white, 4.93 / 4.93"
    amber:  "ink (neutral.light.12), 7.45 / 8.04"
    yellow: "ink, 11.6 / 11.6"

semantic:          # name → primitive step. The light and dark columns differ where depth reads differently.
  bg:
    canvas:   { light: neutral.2,  dark: neutral.1 }   # the window behind everything
    chrome:   { light: neutral.2,  dark: neutral.1 }   # sidebars and bars: they recede, so never lighter than the canvas
    surface:  { light: neutral.1,  dark: neutral.2 }   # cards, panels, list areas
    raised:   { light: neutral.1,  dark: neutral.3 }   # popovers, menus, sheets (plus elevation)
    sunken:   { light: neutral.3,  dark: neutral.1 }   # wells, code blocks, the input track
  element:
    rest:     neutral.3
    hover:    neutral.4
    active:   neutral.5            # pressed and selected
  border:
    subtle:   neutral.6            # card edges and dividers; prefer spacing to a line
    default:  neutral.7            # inputs and interactive edges
    hover:    neutral.8
  fg:
    default:  neutral.12
    muted:    neutral.11           # metadata and secondary labels
    faint:    neutral.9            # placeholders, disabled text, inactive icons
    on_solid: "#FFFFFF"
  accent:                          # a hue from the accent scale; under 3% of any screen
    solid:    accent.9             # the one primary button per view
    hover:    accent.10
    fg:       accent.11            # links
    subtle:   accent.3             # the selected row in a nav list
    ring:     accent.9             # the focus ring
  status:                          # one meaning each, never decoration, never the accent
    needs:    { hue: amber,  bg: amber.3,  border: amber.6,  solid: amber.9,  fg: amber.11,  on_solid: ink,   glyph: needs }
    your_move: { hue: yellow, bg: yellow.3, border: yellow.6, solid: yellow.9, fg: yellow.11, on_solid: ink,   glyph: your-move }
    working:  { hue: blue,   bg: blue.3,   border: blue.6,   solid: blue.9,   fg: blue.11,   on_solid: white, glyph: working }
    crashed:  { hue: red,    bg: red.3,    border: red.6,    solid: red.9,    fg: red.11,    on_solid: white, glyph: crashed }
    queued:   { hue: neutral, bg: neutral.3, border: neutral.6, solid: neutral.9, fg: neutral.11, glyph: queued }
    waiting:  { hue: neutral, bg: neutral.3, border: neutral.6, solid: neutral.9, fg: neutral.11, glyph: waiting }   # waiting on others (was purple)
    done:     { hue: neutral, bg: neutral.3, border: neutral.6, solid: neutral.9, fg: neutral.11, glyph: done }      # finished work recedes (was green)
  diff:                            # fixed whatever the accent
    add:    { bg: signal.3, fg: signal.11 }
    remove: { bg: red.3,    fg: red.11 }
  increased_contrast:              # applied with contrast: increased
    fg.muted: "neutral.12 mixed 82% over bg.surface (tokens.py bakes it as a solid colour)"
    border.subtle: neutral.8
    border.default: neutral.9
    focus_ring_width: 3px

type:
  family:
    ui:   "SF Pro (system). CSS: -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    mono: "SF Mono. CSS: ui-monospace, 'SF Mono', Menlo, monospace. The terminal keeps the user's own font."
  weights: [400, 500, 600, 700]    # nothing else: no 550/650, nothing under 400
  roles:                           # size/line-height in pt or px, weight, CSS tracking in em (SF tracks itself natively)
    caption:   { size: 11, line: 14, weight: 400, track: "+0.006" }   # timestamps, kbd hints
    label-sm:  { size: 11, line: 14, weight: 500, track: "+0.006" }   # chips, pills, counts
    label:     { size: 12, line: 16, weight: 500, track: "0" }        # buttons, row meta, tabs
    body:      { size: 13, line: 16, weight: 400, track: "-0.006" }   # single-line UI text (the macOS default)
    body-copy: { size: 13, line: 19, weight: 400, track: "-0.006" }   # multi-line prose: chat, why, goals
    strong:    { size: 13, line: 16, weight: 600, track: "-0.006" }   # row titles
    title-sm:  { size: 15, line: 20, weight: 600, track: "-0.016" }   # panel and card titles
    title:     { size: 17, line: 22, weight: 600, track: "-0.026" }   # sheet and page titles
    title-lg:  { size: 22, line: 26, weight: 700, track: "-0.012" }   # the workspace page hero
    display:   { size: 28, line: 34, weight: 700, track: "+0.010" }   # the one big number on a view
    code:      { size: 12, line: 18, weight: 400, family: mono }
  numerals: tabular                # every count, timer, cost, ref and step number
  user_scale: "Settings › Text size scales every role by size/13; line heights scale with it"

space: [2, 4, 8, 12, 16, 24, 32, 48, 64]   # px or pt. 2 is only for icon-to-label gaps inside a control.
radius:                                  # the "precise" family: tight, concentric
  xs: 4     # tags, kbd, checkboxes
  sm: 6     # controls: buttons, inputs, segments, rows
  md: 8     # cards, panels, hover cards, menus
  lg: 12    # sheets, dialogs, popovers, palette
  full: 999 # status pills, avatars, dots only
  rule: "inner = outer − padding, never more than the parent's"

elevation:   # one light source straight above; layered and translucent; never animated
  "0": none
  "1":       # cards on hover, the dragged item
    light: "0 0 0 1px oklch(0.235 0 0 / .06), 0 1px 2px -1px oklch(0.235 0 0 / .08), 0 2px 4px oklch(0.235 0 0 / .05)"
    dark:  "0 0 0 1px oklch(1 0 0 / .06), 0 1px 2px oklch(0 0 0 / .4), inset 0 1px 0 oklch(1 0 0 / .04)"
  "2":       # menus, hover cards, tooltips, toasts
    light: "0 0 0 1px oklch(0.235 0 0 / .07), 0 2px 4px oklch(0.235 0 0 / .06), 0 8px 16px -4px oklch(0.235 0 0 / .10)"
    dark:  "0 0 0 1px oklch(1 0 0 / .08), 0 4px 8px oklch(0 0 0 / .35), 0 12px 24px -6px oklch(0 0 0 / .45), inset 0 1px 0 oklch(1 0 0 / .05)"
  "3":       # sheets, dialogs, the palette
    light: "0 0 0 1px oklch(0.235 0 0 / .08), 0 4px 8px oklch(0.235 0 0 / .06), 0 24px 48px -12px oklch(0.235 0 0 / .18)"
    dark:  "0 0 0 1px oklch(1 0 0 / .09), 0 8px 16px oklch(0 0 0 / .4), 0 32px 64px -16px oklch(0 0 0 / .6), inset 0 1px 0 oklch(1 0 0 / .06)"
  scrim: { light: "oklch(0.235 0 0 / .24)", dark: "oklch(0 0 0 / .5)" }

motion:
  duration: { instant: 0, press: 120ms, fast: 150ms, base: 200ms, slow: 260ms, drawer: 320ms }
  easing:
    out:    "cubic-bezier(.23, 1, .32, 1)"       # enter and exit (the default)
    in_out: "cubic-bezier(.77, 0, .175, 1)"      # something moving on screen
    drawer: "cubic-bezier(.32, .72, 0, 1)"       # the sidebar, sheets
    color:  "ease"                               # colour and opacity on hover
    linear: "linear"                             # progress and spinners only
  press_scale: 0.97
  enter_scale: 0.96          # never from 0
  stagger: 30ms              # 6 items at most, then everything at once
  spinner: { delay: 200ms, min_visible: 400ms, period: 900ms }
  reduced_motion: "every movement becomes a 150ms opacity fade; loops stop; nothing is left hidden"
  properties: [transform, opacity]   # plus colour, background-color and border-color for hover; never `transition: all`

layout:
  grid: 4
  hit_target: { min: 24, primary: 28 }     # the visible control can be smaller only if the hit area is padded out to this
  control_height: { sm: 24, md: 28, lg: 32 }
  bar_height: 44             # top bar and terminal tab strip
  footer_height: 28
  sidebar: { min: 200, default: 264, max: 480 }
  measure: 68ch              # longest line for prose
---

# Greenlight design spec

**Status:** draft v1, 2026-11-23. It replaces the current look in `brand/README.md`. The evidence behind each rule is in `brand/RESEARCH.md`.

This file is the source of truth for every pixel Greenlight draws: the dashboard (`app/dashboard.html`), the Basement and the Pipe (`app/terminal.html`), the voice stage (`app/stage.html`), every SwiftUI/AppKit view, the app icon, the Dock tile, the menu bar item and the in-app icon set. An agent building UI here reads this file first, uses only these tokens and recipes, and passes the gate in section 13.

## 0. Surface inventory

This is everything the redesign touches, taken from a full read of the code. Each row names the recipe it follows.

### Native (SwiftUI / AppKit)

| Surface | Code | Today | Recipe |
|---|---|---|---|
| Greenlight Center window | `App.window` App.swift:692 | `NSWindow`, black background, dashboard web view and a chat split with a 1px `NSBox` edge | §10.1 window |
| Extra L1 windows | `makeL1` App.swift:1417 | same as above | §10.1 |
| Basement / Pipe window | `makeTermWindow` App.swift:1551 | transparent titlebar, background follows the page's band colour, forced `NSAppearance` | §10.1, §10.12 |
| Detached Agents window | App.swift:611 | 900×420, black | §10.1 |
| Popped-out chat window | `chatWindow` App.swift:1295 | 440×680 | §10.1, §10.17 |
| Settings | `SettingsView` Views.swift:995 | 52pt header, 228pt sidebar, 12 panes, footer with Done | §10.20 |
| Floor bar and project chip | `Floors` App.swift:55–128 | uppercase "L1 · GREENLIGHT" segments, a glow, an Auto/Light/Dark track | §10.19 |
| Orb | `termOrb`, `LevelFace` App.swift:22, 1509 | a 64pt panel with three faces (matte, traffic light, terminal disc) | §10.19 |
| Ride animation | `rideWin`, `RideView` App.swift:166, 1970 | a full-screen shrink, coin-flip and grow | §7 rare moments |
| Hover hint | `HintTab` Views.swift:927 | a borderless panel | §10.14 tooltip |
| Inbox overlay | `InboxOverlay` Views.swift:955 | a 420pt column, no scrim | §10.13 |
| Context popover | `CtxPop` Views.swift:2701 | `.popover`, 240pt | §10.13 |
| Chat | `ChatView` and the `chatLook` looks, VoiceViews.swift:821 | 7 looks, each with its own palette and font | §10.17 |
| Voice stage | `StageView` VoiceViews.swift:971, `stage.html` | 7 looks: Ledger, conversation, onair, phosphor, editorial, hud, zen | §10.18 |
| Menu bar item | App.swift:467, 2142 | template `star` / `star.fill`, elapsed time | §8.4 |
| Alerts, open/save panels, notifications | App.swift:540, 1138, 2212 | system | system controls; nothing custom |

### dashboard.html

| Region | Today (lines) | Recipe |
|---|---|---|
| Top bar | `.bar` 85–107: title, Overview/List segment, search, bell, chat toggle; workspace mode adds breadcrumb, fix buttons, Open in GitLab | §10.11 bar |
| Footer | `.foot` 110–116: live dot, scan age, rescan, per-state counts | §10.11 |
| Agents strip and agent card | `section.agents`, `.acard` 120–201 | §10.7 card |
| Hex field, stack fan, shelf | `.fieldwrap`, `.fan`, `.shelf` 210–378 | §10.9 hex |
| List view | `.list` 380–426 | §10.8 row |
| Workspace page | `.wp` 475–529: hero, now panel, lifecycle rail, insights tiles, MR facts, activity | §10.10 lifecycle, §10.7 |
| Answer and ping boxes, answer sheet | 531–571 | §10.15 sheet, §10.4 input |
| New-task sheet | 572–610 | §10.15 |
| All-output overlay | `#aout` 171–187 | §10.16 log |
| Hover card | `#hcard` 294–348 | §10.13 |
| Context dock and park rail | `#dock`, `#park` 258–293 | §10.9 |
| Widgets `W.*` (20 kinds) | 762–852 | §10.10 |
| Toast, empty states, open-projects dialog | 597, 358, 612 | §10.14, §10.21, §10.15 |

### terminal.html

| Region | Today (lines) | Recipe |
|---|---|---|
| Sidebar head, ⌘J jump, tree | `#side`, `#shead`, `#tree` 10–57: sections tinted teal, orange, blue, yellow and green; uppercase section heads; `.ws`, `.tm` and host rows | §10.12 sidebar |
| Tab strip | `#bar` 102: amber top line on the active tab | §10.6 tabs |
| Stage: panes, splits, find bar, drop overlay | `#stage` 117–137, 330 | §10.12 |
| Link popover, toast, adopt prompt, context menu | 58–64, 141 | §10.13, §10.14 |
| Workspace hover card | `#card` 148–309 | §10.13 |
| ⌘K palette with preview | `#pal` 68–98 | §10.15 palette |
| Dialogs: ask, New workspace, Open projects, Projects, Colours, Diff, Delete | `#dlg` 192–328 | §10.15 |

### Icons and brand

| Asset | Today | Replacement |
|---|---|---|
| App icon | `Orb.icns` (bundle), plus two Dock options `MergeLight` and `ThreeFloors`; flat SVGs with glow and specular baked in; prebuilt `.icns` checked in with no build script | §8: one new layered icon |
| UI glyphs | 3 sets: dashboard `ICONS` (16 grid, 1.75 stroke); terminal `I`/`IC`/`G`/`MI` (16 and 24 grids, 1.5–2 stroke); SF Symbols natively; 6 stroke widths in all | §9: one set |
| Agent marks | Claude asterisk filled `#D97757`; a hexagon standing in for Codex; an OpenAI mark in the dashboard | §9.5 |

### What the inventory found (the debts this spec retires)

- **Token drift.** Literal values instead of tokens:

  | File | Hex | Font sizes | Radii | Shadow recipes |
  |---|---|---|---|---|
  | dashboard.html | — | 68 | ~75 | — |
  | terminal.html | 121 (89 unique) | 14 sizes, half-pixels included | 21 distinct | 8 |
  | Swift (App.swift, Views.swift, VoiceViews.swift) | ~63 | 61 `.system(size:)` | — | — |
- **Two accent systems.** The UI uses blue `--accent` and `#0A84FF` for focus, plus Basement amber `#FFB340`.
- **Status hues with no meaning.** Purple and teal are status colours, and coloured section headers turn the sidebar into a rainbow.
- **Uppercase.** Tracked uppercase labels appear in about 30 places across three files.
- **Missing states.** A general disabled style, `:active` in terminal.html, keyboard focus on tree rows (hover-only controls can't be reached by keyboard), loading and error states for widgets and the sidebar, and enter/exit motion for menus and dialogs.
- **Motion gaps.** Reduced motion misses several loops, and the `spin` keyframes are defined twice.
- **Appearance.** It is forced per window every tick. The chat and stage looks don't use Theme at all.

## 1. Overview

### What Greenlight is
An agent manager. It watches every coding-agent workspace and its merge request, works out what happens next and who it's waiting on, and asks you only what only you can answer. People keep it open all day beside their editor and glance at it more than they click it.

**The main job of the UI:** in under a second, show what needs you. Everything else is there on demand.

### Character: quiet precision
Calm, exact, fast and confident. The greys carry the interface. Colour shows up only when something is asking for you. It should feel like a precision instrument that sits at rest: exact type, tight corners, no ornament, nothing moving without a reason.

**References** (named on purpose; agents aim here):
1. **Linear (2026 refresh):** chrome that recedes, a dimmed sidebar, structure you feel rather than see, three-input themes.
2. **Raycast 2.0:** you can't tell where native ends and web begins; keyboard first; no flicker.
3. **Things 3:** calm density, generous but exact spacing, delight held back for rare moments.

**We are not:** a neon cockpit, a glassy showcase, a marketing page, or a rainbow of status colours.

### Principles
1. **Colour means "look here".** A screen with nothing to do is almost entirely neutral. Amber, yellow, blue and red each mean one thing, and each one has a shape as well as a colour. Done and waiting work stays grey and recedes.
2. **Chrome recedes, work comes forward.** Sidebars, bars and footers sit darker or quieter than the content. Navigation never competes with a workspace that needs you.
3. **Structure is felt, not seen.** Use spacing and surface steps first and a hairline last. Draw each border once. Cards don't nest more than two levels.
4. **Frequent means instant.** Anything done 100+ times a day doesn't animate. Motion is for orientation and for rare moments.
5. **One system, two runtimes.** Swift and CSS are generated from the same tokens. A user should never be able to tell a web view from native.
6. **Every state is designed.** Rest, hover, pressed, focus, selected, disabled, loading, empty, error. If a state is missing, the component is unfinished.

## 2. Colour

### 2.1 The model
Each theme is generated from four inputs: neutral hue, neutral chroma, accent and contrast. Every surface, text, border and state colour derives from them, following Linear's three-input approach in OKLCH. There are seven 12-step scales (front-matter `primitives`), and every step has one job:

| Steps | Job |
|---|---|
| 1–2 | backgrounds (app, subtle) |
| 3–5 | element background: rest, hover, active/selected |
| 6–8 | borders: subtle, interactive, hover |
| 9–10 | solid fill and its hover |
| 11 | low-contrast text (APCA Lc ≥ 60 on step 2) |
| 12 | high-contrast text (Lc ≥ 90) |

Only use semantic tokens in UI code: `bg.*`, `element.*`, `border.*`, `fg.*`, `accent.*`, `status.*`. Primitives are for the generator. A literal hex or `rgba()` in UI code fails the gate. The exceptions are artwork (the icons, agent marks) and the terminal canvas (it uses Ghostty's colours).

### 2.2 Light and dark
- **Follow the system by default.** Settings › Appearance offers System / Light / Dark as an override. This replaces the forced light L1 and dark B1/B2, and the Settings-only light/dark toggle goes away. Floors are told apart by name and glyph, never by appearance.
- **Dark is its own palette**, not light mode inverted:
  - surfaces get lighter as they rise (canvas 0.235 → surface 0.277 → raised 0.31 L): the canvas is the macOS window grey `#1E1E1E`, so a web view and the AppKit window beside it are the same colour;
  - text sits at L 0.95 / 0.80;
  - chrome is never lighter than the canvas;
  - there's no pure black and no pure white text.
- **Light mode:** the canvas is neutral.2 and cards are neutral.1 (white, the system's content background), so cards lift by being paler, not by a shadow.
- **The greys are true greys** (chroma 0), as in every system window. The brand is the accent and the light, not a tint in the chrome.
- **Switching appearance is instant.** Disable transitions for one frame (Paco's trick), then re-enable. Never cross-fade the whole UI.
- **Increase Contrast** (`contrast: increased`): muted text becomes near-default, borders go up one step, and the focus ring is 3px. Test it under Reduce Transparency too.

### 2.3 Accent
- The accent is for the one primary action per view, links, focus rings and the selected item in a nav list. That's all: under 3% of any screen.
- **Choices:**

  | Accent | Hue | Notes |
  |---|---|---|
  | **Signal** (default) | 150 | the brand go-green |
  | **Teal** | 190 | — |
  | **Graphite** | — | the accent is neutral.12/neutral.11, a monochrome UI for people who want no colour at all |

  Each accent is a 12-step scale with Signal's L and C at its own hue, so contrast holds without re-checking.
- **Guard:** no accent may come within 25° of a status hue (62, 95, 252, 25) or fall in 270–330 (violet and lavender are banned). That's why blue, amber and pink stop being accents. They now mean working, needs-you and nothing.
- **Migrating today's 8 swatches:**
  - Green and Cyan → Signal and Teal.
  - Blue and Purple → Teal.
  - Amber, Orange, Rose and Pink → Signal.
  - The Settings swatches become three.

### 2.4 Status colours
| State | Meaning | Hue | Glyph (§9.4) | Where it's solid |
|---|---|---|---|---|
| needs | needs you | amber 62 | diamond with ! | hex fill, list dot, Dock badge |
| your_move | your move (raised) | yellow 95 | arrow in a circle | hex outline + tint |
| working | an agent is working | blue 252 | open ring (spins only when it's the focus) | dot, progress |
| crashed | crashed or failed | red 25 | octagon with × | hex fill, banner |
| queued | queued | neutral | dotted circle | — |
| waiting | waiting on others | neutral | hourglass | — |
| done | done or merged | neutral | check in a circle | — |

- **Colour and shape together.** A status is always colour and glyph, and when there's room, a text label too. Amber against blue and red against green are the classic colour-blind pairs: the shapes carry them.
- **Solid vs tinted.** Solid fills (step 9) are only for the states asking for you: needs and crashed. Working and your_move use the tinted pair (step 3 background, step 6 border, step 11 text). Queued, waiting and done are neutral.
- **CI states** (success, failed, running, pending, canceled, skipped, manual) map to done, crashed, working and queued plus their glyphs. Merge-request draft/open/merged use neutral text and glyphs, not colour.
- **Terminal sidebar sections** (Hosts, Needs you, Working, Workspaces, Waiting on others, Done) lose their tints. The headers are neutral, and only the row's status dot is coloured.

### 2.5 Themes, repo looks, terminal palettes
- **Named terminal themes** (Dracula, Nord, Tokyo Night and the other 18 in `Config.swift themes`) style the terminal canvas only.
- **Settings › Appearance › Match terminal** (off by default) derives the chrome's neutral hue and chroma from the terminal background: take its OKLCH hue, and chroma clamped to 0.02. The accent comes from the theme only if it passes the §2.3 guard; otherwise Signal.
- **Local Lights:** the `look` key in `.greenlight.json` now gives the same inputs, `{neutral_hue, neutral_chroma, accent, contrast}`. The 12 legacy skin keys map onto them, and invalid input falls back to the default. `/design` and the Colours dialog offer theme inputs, not free CSS.
- **Floor colours** go away (L1 sage `#2E6F43`, B1 amber `#FFB340`, B2 copper `#A8661C`).

## 3. Typography

- **SF Pro everywhere, in both runtimes.** This is a deliberate exception to the "avoid the default font" advice: on macOS the system face is what makes a native app feel native, and Raycast and Things both use it. Greenlight's character comes from colour, spacing and the icon, not the font. CSS uses `-apple-system`, which gets optical sizing automatically. CSS applies the tracking in the token table; SwiftUI doesn't need it.
- **Use the type roles only** (front-matter `type.roles`): caption, label-sm, label, body, body-copy, strong, title-sm, title, title-lg, display. Five sizes in normal use: 11, 12, 13, 15, 17. Use 22 and 28 once per view at most. Nothing under 11, and no half-pixels.
- **Hierarchy comes from weight and colour first, size second.** A row title is `strong` (13/600) in `fg.default`, and its meta line is `label` (12/500) in `fg.muted`. Labels stay quieter than values: "3 failing", not "CI: 3 failing".
- **Weights** are 400, 500, 600 and 700 only.
- **Case:** sentence case everywhere: buttons, headers, sections, floor names. **No uppercase with tracking.** The only exceptions are keycap glyphs (⌘K) and MR refs as they're written (`!123`).
- **Numbers:**
  - Tabular figures for every count, elapsed timer, cost, step number and ref (`font-variant-numeric: tabular-nums`; `.monospacedDigit()` in Swift).
  - Times are relative and short: `4m`, `2h`, `3d`.
  - Use `…` (the single character), never three dots.
- **Mono** is for code, branch names, paths, logs and the terminal. It's not for small data labels.
- **Prose:** chat, goals and "why" text use `body-copy` (13/19), at most `68ch` wide.

## 4. Layout, spacing, density

- **Space** comes from the scale only: 4, 8, 12, 16, 24, 32, 48, 64, with 2 allowed inside a control. Padding inside a component is smaller than the gap between components.
- **Density:**
  - Rows are 28px (sm) or 32px (md); the sidebar tree uses 28.
  - Cards have 12px padding (16 on the workspace page).
  - Panels are separated by 16–24px of space, not by lines.
- **Alignment:**
  - Shared left edges.
  - Icons optically centred (nudge 0.5–1px where needed).
  - Numbers right-aligned in tables.
  - Baselines aligned across a row.
- **Controls:**
  - Heights are 24, 28 or 32.
  - The hit area is at least 24; primary controls are at least 28.
  - Hover-only controls (row actions) must also appear on keyboard focus.
- **Sizes to test:**
  - The dashboard at 480, 900 and 1280 wide.
  - The sidebar at 200, 264 and 480.
  - The chat at 320 and 440.
  - Long names, 0 items, 1 item and 200 items.
- **Footer.** macOS advice is to keep critical controls out of the bottom edge. The dashboard footer holds status only (live state, scan age, counts) and no actions apart from rescan.

## 5. Depth and materials

- **Four elevation levels** (front matter). Use them by role:

  | Level | Role |
  |---|---|
  | 0 | the content plane |
  | 1 | a hovered card, a dragged item |
  | 2 | menus, hover cards, tooltips, toasts |
  | 3 | sheets, dialogs, the palette |

  Shadows are layered and translucent from one light source, with a 1px ring built in. In dark mode, height comes mostly from lighter surfaces plus an inner top highlight.
- **Never animate a shadow.** To lift something, change the token on hover with no transition, or move it with `transform`.
- **Liquid Glass (macOS 26+) is for the native control layer only.** That means:
  - the titlebar floor bar;
  - the Settings sidebar (`NavigationSplitView` with no custom background);
  - the Basement sidebar: the web view is transparent over an `NSVisualEffectView` `.sidebar` material, falling back to an opaque `bg.chrome`.

  It's never used for content, never stacked glass on glass, and never in the dashboard canvas. The unused `--glass`, `--glass-hi` and `--gl-*` tokens and the 14 SVG glass gradients on hexes are deleted.
- **Scrims** sit under sheets and dialogs only (`elevation.scrim`). Overlays like the inbox have no scrim but take elevation 2.

## 6. Shape

- **Radii** are 4 (tags, kbd), 6 (controls, rows), 8 (cards, menus), 12 (sheets, popovers), and full only for pills, avatars and dots. Corners are concentric: inner = outer − padding.
- **Hairlines** are 1px, `border.subtle`, drawn once. Where two surfaces meet, one of them owns the edge.
- **Focus ring:** `0 0 0 2px bg.canvas, 0 0 0 4px accent.ring`, via `:focus-visible` only. In Swift use the system focus ring tinted with the accent.
- **The hexagon** is Greenlight's one signature shape. It's used only for the workspace cell on the board, its mini version in list rows, and the app mark's geometry (§8). It never decorates anything else.

## 7. Motion

| How often | Examples | Motion |
|---|---|---|
| 100+ times a day, or from the keyboard | ⌘K, ⌘J, switching tabs, selecting rows, list navigation, typing | none (instant) |
| tens of times a day | hover, pressing a button, chevrons | colour only, `fast`; press `scale(.97)` over `press` |
| occasional | menus and hover cards (150–200ms, `out`, from scale .96 plus opacity, from their trigger); sheets and dialogs (`slow`, `out`); toasts (`base`, rising 8px); the sidebar (`drawer`/320ms) | standard |
| rare | the first launch, a merge that lands (one ring pulse on the hex), switching floors | can delight, 400ms or less |

- **Properties:** animate `transform` and `opacity` only, plus colour on hover. Never `transition: all`. Every animation is interruptible.
- **Loops:**
  - The only infinite loops allowed are the working spinner (only when it's the focused item; otherwise a static open ring), the needs pulse, and the voice stage's oscillator field (§10.18), which runs while Chief works and as a subtle voice-reactive floor in open whitespace below short cards (staying completely quiet when a card fills the viewport, when muted, or when turned off).
  - The needs pulse runs 3 cycles of 2.4s when a workspace becomes needs-you, then rests on a static halo. No breathing segments, no ambient pulses.
- **Spinners** wait 200ms before appearing and stay at least 400ms. Updates are optimistic with Undo.
- **The ride between floors** stays, as the rare moment:
  - 400ms or less by default (today it's 600ms);
  - in it, a 300ms shrink/grow on `in_out` and a 120ms flip;
  - Settings › Ride animation 0 turns it into a cut;
  - Reduce Motion turns it into a 150ms crossfade.
- **Reduced motion:** every movement becomes a 150ms opacity fade, every loop stops, nothing stays hidden. This applies in all three HTML files and in Swift (`accessibilityReduceMotion`). The gate checks it.
- **Theme and appearance changes** never animate.

## 8. Identity and the app icon

### 8.1 Name, mark and wordmark
- **Name:** Greenlight. **Tagline:** "Your agents, cleared to go." It's used in About and the README only, never in the UI.
- **The mark** is the new icon's foreground on its own: a single geometric form, chosen in §8.3. It appears in About, the README, the menu bar (as a template) and the orb.
- **Wordmark:** "Greenlight" set in SF Pro Display Semibold, −0.01em, sentence case. Its one custom detail comes from the mark (Stripe's approach), for example the dot of the "i" replaced by the mark's lit element.
- **Clear space** is the mark's height. Use the full lockup when there's room and the mark alone when space is tight.

### 8.2 Icon brief
- **The idea:** a green light, reinvented. It means "permission to proceed" and "the way is clear". Not a traffic light and not a checkmark.
- **Hard constraints:**
  - 1024 canvas, the system squircle (never a custom outline, which gets "icon jail").
  - At most 4 layer groups: background, then 1–3 foreground groups.
  - Flat vector layers, with no glow, shadow, blur or specular baked in. Icon Composer adds glass.
  - No UI, no screenshots, no words. Few shapes, no thin lines.
  - Six appearances: default, dark, clear light, clear dark, tinted light, tinted dark. In mono and tinted modes, part of the foreground is near-white.
  - Specular set inside when the foreground is darker than the background, outside when it's lighter.
- **Colour:** Signal green is the identity colour, and the light is the brightest element. The default background is deep neutral-green (neutral hue 155, L 0.20–0.28, two layers) rather than black, so it stands out on a light Dock. The dark appearance tints the background darker; the light appearance can invert it to a pale ground with a green light.
- **Tests:**
  - It reads at 16px.
  - A glance at a grid of the top concepts at 32px.
  - It stays distinct against Linear, Raycast, Terminal, Ghostty, Orca, Xcode and Finder in a real Dock, on both a light and a dark wallpaper.
  - It renders correctly on macOS 26 and on 27 (sharper, with less glass).
  - It still works in greyscale, because shape carries it, not colour.

### 8.3 Concepts to explore
Each concept gets a greyscale sketch at 32px. Pick three, render them in Icon Composer, then choose one.

1. **Aperture:** an iris opening inside the squircle, with green light through the centre. Agents are the blades; "clearing the way".
2. **Slit of light:** a dark tile split by one horizontal band of green light, like a door opening.
3. **The lit cell:** the board's hexagon as a lens lit from inside, with one lit cell among faint siblings.
4. **Lane clear:** two converging lanes resolving into one lit lane (merging, then go).
5. **Beacon:** one point of light with two concentric arcs on one side only (signal sent, no Wi-Fi look).
6. **G-lamp:** a geometric "G" whose open counter holds the lit light. A single mnemonic letter is allowed.
7. **Signal column:** a vertical column of 3 dots where only the top one is lit green. Risk: it reads as a traffic light, so test it.
8. **Constellation:** a cluster of small dark nodes (agents) with one bright green node in front.
9. **Horizon:** a green light rising over a flat horizon line (dawn as "go").
10. **Switch:** a toggle thumb moved to "on", made of light. Risk: it reads as a settings icon, so test it.

**Selection criteria:**
- Unique among dev tools.
- Readable at 16px.
- Survives tinted and clear modes.
- The mark works on its own as a 16pt template.
- It suggests "clear to proceed", not "success".

### 8.4 Where the identity shows up
- **App icon:** one icon. `MergeLight`, `ThreeFloors` and the Dock-tile chooser go away. Settings › General › Dock icon becomes Default / Debug, where Debug is the same icon with a neutral-grey light and is automatic for dev copies (Raycast's approach).
- **Menu bar item:** a 16pt template glyph of the mark, replacing `star`. While agents work it fills to its "lit" variant. The needs count shows as a number in `label` tabular figures. It is never coloured, because the menu bar stays monochrome.
- **The orb** becomes the mark on a 40pt `bg.raised` disc with elevation 2:
  - The mark's light is Signal on L1 and the terminal glyph on B1/B2.
  - Hover raises it to elevation 2+ with no animation; pressed is scale .97.
  - The three faces (matte, traffic light, terminal disc) are retired.
- **Floor badges:** "Greenlight Center", "Basement" and "Pipe" in sentence case, each with a 16px glyph (center = the mark, basement = terminal, pipe = a single pane). No colour of their own: the selected floor uses `element.active` with `fg.default`.

### 8.5 Production pipeline
1. Draw the icon layers as vector in `brand/icon/layers/NN-name.svg`, numbered back to front, square and unmasked. Text is outlined.
2. Build the `.icon` in Icon Composer (`brand/icon/Greenlight.icon`), annotating dark, clear and tinted.
3. **For the swiftc build and macOS before 26:** a checked-in script, `scripts/build_icons.sh`, renders the default appearance with a squircle mask through `rsvg-convert` into the PNG sizes iconutil needs (16 to 512, @1x and @2x), then runs `iconutil -c icns`. It replaces today's undocumented prebuilt `.icns`.
4. **On macOS 26+:** whether `actool` can compile the `.icon` from the command line, for `build.sh` on the mini, has to be checked on the Mac. If it can't, ship the `.icns` and track the gap.

## 9. Interface icons

### 9.1 The set
- **One custom SVG set replaces all three sets** (dashboard `ICONS`, terminal `I`/`IC`/`G`/`MI`, and SF Symbols in custom views).
- **Native views use the same SVGs** as template images, so native and web match exactly. SF Symbols stay only inside system controls (NSMenu, NSAlert, open panels) and as a fallback while the set grows.
- **Source:** `brand/icons/ui/<name>.svg`, plus the agent marks in `brand/icons/marks/`. `scripts/icons.py generate` inlines them into dashboard.html and terminal.html as a `<script id="ds-icons">` block (`dsIcon`, `dsIconHTML`; the pages' CSP allows no external scripts), and into `app/Sources/Icons.swift` (`Image(ds:size:medium:)`, template NSImages from the same SVG). `icons.py check` (in `make lint`) validates every file and that every icon is used and every name used exists.

### 9.2 Drawing spec
- **One drawing, any size:** every icon is drawn once on the 16 grid. Both renderers keep the on-screen stroke at 1.5px at 16px and above (1.75px for Medium) by scaling the stroke width against the size, and let it thin with the icon below 16. A hand-fitted 20 drawing is added only where the scaled 16 looks wrong at toolbar size (none needed so far).
- **16 grid:**
  - 1px padding, giving a 14px live area.
  - Circles have a 13px diameter, squares are 12×12, wide rectangles 14×10.
- **Stroke:**
  - 1.5px on both sizes, matched to SF Symbols Regular next to 12–13pt text.
  - Next to `strong` or 600-weight text, use the Medium weight at 1.75px. These are the only two weights.
- **Geometry:**
  - Round caps and joins.
  - 1.5px radius on shape corners; 0.5px under 6px.
  - Gaps at least 1.5px, and 1px around modifiers (badge and slash cut-outs).
- **Pixel fitting:**
  - Outer edges on the pixel grid at 1×.
  - Odd stroke widths centred on .5 coordinates.
  - Check at 1× as well as 2×.
- **Outline and fill:**
  - Outline by default.
  - A **filled** variant only for the selected or active state (a selected nav item, a toggled tab, a lit bell) and for status glyphs at 12px and under.
  - Duotone isn't allowed.
- **Colour and perspective:**
  - Always `currentColor`. The icon takes the text colour beside it, or a status `fg`.
  - Icons never get a coloured background tile.
  - Flat front view only: no perspective or 3D.
- **Agent marks** are the only multi-colour artwork (§9.5).

### 9.3 Canonical list (from the inventory)
- **Navigation:** chevron-right, chevron-down, chevron-up, back, forward, external, expand, collapse, sidebar, panel, window, search, filter, more (…), close.
- **Actions:**
  - plus, edit, copy, trash, refresh/rescan, undo, download, attach (clip), send (up), split-right, split-down, zoom, find, clear, settings (gear), brush (look), folder, file, diff, lock, lightbulb, spark (AI suggestion).
  - Delete the duplicates: x/close, svg16 search vs ICONS search, and three spinner glyphs.
- **Domain:** workspace (the hex cell), mr (merge request), branch, merge, rebase, conflict, threads, review, ci, flag (milestone), deploy, host, terminal (shell), bell, bell-filled, keyboard, mic, waveform, play, pause, stop.
- **Status** (§9.4): needs, your-move, working, crashed, queued, waiting, done, plus the CI-specific ci-pass, ci-fail, ci-run and ci-skip (dashed circle).
- **Floors:** center (the mark), basement, pipe.

### 9.4 Status glyphs (shape carries the meaning)
| Glyph | Shape |
|---|---|
| needs | a diamond (a square turned 45°, 1.5px radius) with a short bar and a dot |
| your-move | a circle with a right arrow |
| working | an open ring, 270°; it spins at the `spinner` period only on the focused item |
| crashed | an octagon with × |
| queued | a dotted circle |
| waiting | an hourglass |
| done | a check in a circle |

At 12px and under, use the filled variants, where the cut-out carries the shape.

### 9.5 Agent and product marks
- **Claude:** Anthropic's published Claude mark, used under their brand guidelines, in `clay.9`.
- **Codex:** OpenAI's published mark, monochrome (`fg.default`). Delete the hexagon stand-in.
- **GitLab and GitHub:** their official marks, monochrome, only in "Open in …" buttons.
- **Unknown agents:** a neutral monogram in a circle (the first letter, in `label-sm`, on `element.rest`).
- Marks are never redrawn and never recoloured beyond what each brand's guidelines allow. If a licence doesn't allow use, use the monogram.

## 10. Components

Each recipe lists its tokens, sizes and every state. **R** rest · **H** hover · **P** pressed · **F** focus-visible · **S** selected · **D** disabled · **L** loading · **E** error/empty.

### 10.1 Window
- **Background:** `bg.canvas`. Never `.black`.
- **Titlebar:** transparent, with the floor bar as a native accessory (glass on macOS 26).
- **The chat split:** the divider is a 1px `border.subtle`, drawn by one side only.
- **Web views:** `drawsBackground = false`, with the under-page colour `bg.canvas`. Show the window only after the web view's first presentation update, so it never flashes white or black.

### 10.2 Buttons
| Variant | R | H | P | F | D | L |
|---|---|---|---|---|---|---|
| **primary** (1 per view) | `accent.solid` bg, `fg.on_solid`, `label` 12/500 | `accent.hover` | scale .97 | ring | `element.rest` bg, `fg.faint`, no hover | label stays; a 12px spinner replaces the icon after 200ms; width locked |
| **secondary** | `element.rest`, `fg.default` | `element.hover` | `element.active`, scale .97 | ring | `fg.faint` | same |
| **quiet** (toolbar, row actions) | transparent, `fg.muted` | `element.rest` bg, `fg.default` | `element.hover` | ring | `fg.faint` | same |
| **danger** (only in a confirm step) | `status.crashed.solid`, white | red.10 | scale .97 | ring | — | — |
| **icon** (quiet, square) | 24 or 28 square, 16 icon, `fg.muted` | `element.rest` | `element.hover` | ring | `fg.faint` | spinner |

- **Sizes:** sm 24 (8px horizontal padding), md 28 (12), lg 32 (16).
- **Shape and spacing:** radius `sm`, icon-to-label gap 4.
- **Labels:** a verb plus an object ("Open MR", "Answer", "Merge"). Add `…` when the button opens another step.
- **Cursor:** no `cursor: pointer` on buttons (it's native behaviour); links keep it.
- **Two-step confirm** (merge, delete): the first press arms the button ("Confirm merge"), and it drains back over 5s with a `linear` progress underline. Escape cancels.
- **Gone:** `.go`, `.ans`, `.nact`, `.pri`, `.lbtn.raised` and the hcard tones `.or`/`.pu`/`.rd`/`.bl`. Everything maps to the five variants above.

### 10.3 Segmented control and tabs-as-filter
- **Track and segments:** the track is `bg.sunken` (dark: `element.rest`, since `bg.sunken` is the bar's own colour there), 28 tall, radius `sm`, with 2px padding. The selected segment is `bg.surface` with elevation 1 (light) or `element.active` (dark), in `fg.default`.
- **States:**
  - Hover on an unselected segment: `fg.default`.
  - Focus: a ring on the track.
  - Disabled: the whole control at `fg.faint`.
- **Motion:** switching is instant. No sliding thumb, because it's frequent.

### 10.4 Inputs: text, search, textarea
- **Base:** 28 tall (textarea min 3 lines, using `body-copy`), `bg.surface` (light: the text background, since grey reads as disabled) or `element.rest` (dark), 1px `border.default` drawn inside, radius `sm`. In a Settings form it is 24, the height of the controls beside it.
- **States:**
  - H: `border.hover`.
  - F: border `accent.ring` plus the ring.
  - D: `fg.faint`, no border.
  - Invalid: border `status.crashed.border` with a `caption` message below in `status.crashed.fg`. The colour is never the only signal.
- **Placeholder:** `fg.faint`. Text is 13px or more, and real inputs follow the user's text-size scale.
- **Search** has a leading search icon (16, `fg.faint`), a ⌘ hint chip on the right while it's empty, and a clear button while it has text. The ⌘J jump and the dashboard search share this recipe.

### 10.5 Pills, chips and counts
- **Status pill:** full radius, 20 tall, 6px horizontal padding, a 12px status glyph, then the label in `label-sm`. Background `status.*.bg`, text `status.*.fg`, with no border. Neutral states use `element.rest` / `fg.muted`. Pills are static: no hover.
- **Filter chip** (list filters, ping choices): 24 tall, radius `sm`.
  - R: `element.rest`, `fg.muted`.
  - H: `element.hover`.
  - S: `accent.subtle` bg, `accent.fg` text, plus the leading check icon.
  - F: ring. D: `fg.faint`.
- **Tag** (Draft, step, project): radius `xs`, 18 tall, `label-sm`, `element.rest`, `fg.muted`.
- **Count badge:** full radius, 16 tall, minimum 16 wide, `label-sm` tabular figures. Needs counts use `status.needs.solid` with ink; other counts use `element.active`. Rename the dashboard's two colliding `.badge` classes to `count` and `stack-count`.
- **Kbd:** radius `xs`, `caption`, 1px `border.default`, `fg.muted`.

### 10.6 Tabs (terminal tab strip)
- **Strip:** 44 tall, `bg.chrome`. Each tab is 28 tall, radius `sm`, 16 icon, `label` text, at most 200 wide with truncation.
- **States:**
  - R: `fg.muted`, transparent.
  - H: `element.rest`.
  - S (active): `bg.surface`, `fg.default`, elevation 1 in light. **No coloured top line.**
  - F: ring.
  - Busy: a 12 working glyph replaces the icon. Needs: the needs glyph, solid.
- **Close button:** a 16 quiet icon button. It shows on hover, on focus and on the active tab.
- **Dragging:** the dragged tab gets elevation 1. Tabs reorder instantly, with no slide.

### 10.7 Cards and panels
- **Card:** `bg.surface`, radius `md`, 12 padding, 1px `border.subtle` (light uses the elevation-1 ring instead). Only an interactive card has hover: `border.hover` in light, `element.rest` tint in dark, and the cursor stays default. No lift on hover in dense grids. Selected is `accent.subtle` plus a 1px `accent.ring` border. Focus is a ring.
- **Agent card** (dashboard strip):
  - **Rows:**
    1. Avatar (agent mark, 20), title `strong`, elapsed time `caption` tabular figures, right-aligned.
    2. Meta in `label` `fg.muted`: ref · project · goal.
    3. The current line: the question for needs, the tool line for working, in `body` with 2 lines at most. A longer question gets a quiet "Full question" beside it. The strip is at most 240 tall and scrolls, so the board stays in view.
    4. The lifecycle bar (§10.10) and the MR's facts, only when there are any.
  - **Actions:** one action button. Only the first needs-you card's is primary: one primary per view. The others are secondary.
  - **Status:** shown by the status pill, never by tinting the whole card. A crashed card gets a 2px `status.crashed.solid` leading edge as the only tint.
  - **Empty / loading:** while scanning there's one skeleton card per expected agent, the same shape as a real card. With no agents the strip collapses to a single `caption` line, "No agents running".
  - **Error:** a card for an agent that can't be reached keeps its last state, dimmed to 60%, with a `caption` "Lost contact 2m ago".
- **Panel** (workspace page sections): the same as a card but with 16 padding, a `title-sm` header and optional quiet actions on the right. Panels never nest inside cards.

### 10.8 Rows: list, tree, menu
- **Row:** 28 (tree, menu) or 32 (list), radius `sm`, inset 4 from the container edge, 8 horizontal padding.
- **Workspace list row:** flat, never a card. A mini hex, the title in `strong` with a neutral tag, then one `label` line of name · project and what it says; the agent and a secondary `sm` action on the right. The state shows as the hex's colour plus the step word, never colour alone.
- **States:**
  - H: `element.rest`.
  - P: `element.hover`.
  - S: `element.active`, text `fg.default`. A nav sidebar is the same: the accent stays on the one primary action.
  - F: an inset ring.
  - D: `fg.faint`.
- **Tree rows** get real keyboard support: `tabindex` with arrow-key handling, ←/→ to fold, Return to open. Row actions (+, close) show on hover, on focus and on the selected row.
- **Sections:**
  - The header is `label` 12/500 in `fg.muted`, sentence case, with a count in tabular figures and a chevron.
  - Spacing above a section header is 16.
  - No coloured section tints.
- **List row** (dashboard list view), left to right: mini hex (16), title `strong` with tags, meta `label` muted, then right-aligned signals (CI glyph, conflicts, threads x/y, approval pips) and age `caption` tabular figures. Needs-you rows lead with the needs glyph; there's no row background tint.
- **Menus** (context menus, the ▾ menus): `bg.raised`, radius `md`, 4 padding, elevation 2, opening from their trigger at `fast`/`out`. Dangerous items use `status.crashed.fg` text and sit last, after a separator. A disabled item is `fg.faint` with no hover.

### 10.9 The board: hex cells, stack fan, dock
The board is Greenlight's signature view: one hex per workspace, sized by urgency.

- **Hex body:** flat. No glass gradients, halo layers or lit-edge layers.

  | State | Fill | Border | Text |
  |---|---|---|---|
  | needs, crashed | `status.solid` | — | `on_solid` |
  | working, your_move | `status.bg` | 1.5px `status.border` | `status.fg` |
  | queued, waiting, done | `bg.surface` | 1.5px `border.subtle` | `fg.muted` |
  | no MR | `bg.surface` | dashed 4 3 | `fg.muted` |
  | needs, crashed with no MR | `status.solid` | dashed 4 3 `border.default` | `on_solid` |
  | working with no MR | `status.working.bg` | dashed 4 3 `status.working.border` | `status.working.fg` |
- **Labels inside:** the ref in `strong` tabular figures, the task name in `label` (only when R ≥ 50), and the sub-label in `caption`.
- **Status glyph:** 12, top centre, so the hex never depends on colour alone.
- **Progress:** a 2px track around the inside edge in `border.subtle`, with completed segments in `status.fg` (or `fg.muted` when neutral).
- **States:**
  - H: elevation 1 plus the hover card after a 350ms intent delay.
  - S (picked): a 2px `accent.ring` outline offset 2.
  - F: the same as S, plus the ring.
  - Dim (filtered out): 35% opacity.
- **Motion:**
  - A needs hex pulses 3 times when it arrives (scale 1 → 1.06 on `out`, 2.4s), then rests.
  - Hexes reflow instantly when sizes change. The stack fan opens at `base`/`out` with a 30ms stagger (6 at most).
- **Bell and agent dot:** the "Greenlight asked" bell (12, `status.needs.fg`) sits top-left and the working-agent dot top-right. Both use glyphs from the set.
- **Shelf** (waiting / done): chips by the §10.5 filter-chip recipe, neutral.
- **Dock and park rail:**
  - A drop socket is a 1.5px dashed `border.default` hex. While dragging, it is `accent.subtle` with an `accent.ring` dashed border.
  - Edge glows are replaced by a 2px `accent.ring` inner edge on the target rail.
  - The drag ghost is the hex at 90% with elevation 2.

### 10.10 Lifecycle and data widgets
- **Lifecycle bar:** segments 4 tall with 2 gaps, radius `xs`.
  - Done segments are `fg.muted`, the current segment is the workspace's status `solid` (neutral for queued), and future segments are `element.active`.
  - The labels below are `caption`.
  - **No breathing segment.**
- **Lifecycle rail** (workspace page): 20 nodes with a 1.5px connecting line, using the status glyphs and icons from the set. A step's owner is shown with an avatar (16). "After merge" is a `label` divider.
- **Widgets** (`W.*`, 20 kinds) share one frame: a panel with a `title-sm` or `label` header and a body.
  - **Each widget must render:** a loading skeleton (the widget's own shape), empty ("No data yet" in `caption` `fg.faint`) and error ("Couldn't load", plus a retry quiet button). Today a failed widget returns null.
  - **Charts follow the dataviz skill.** Neutrals for series, accent for the one highlighted series, status colours only when the series *is* a status. Tabular figures; no chart junk.
- **Stat tile:** the value in `display` or `title-lg` tabular figures, with the label below in `label` `fg.muted`. Labels stay quieter than values.

### 10.11 Bars: top bar, footer
- **Top bar:** 44 tall, `bg.chrome`, a hairline below only when content scrolls under it (the scroll-edge effect). No backdrop blur.
  - **Left:** the title `title-sm`, then the attention count as a needs count badge.
  - **Centre:** the Overview/List segmented control.
  - **Right:** search (200 wide, 320 when focused), bell, chat toggle.
  - **Workspace mode:** back (quiet icon), breadcrumb in `label` (the last crumb `fg.default`), fix buttons (quiet, with a status glyph), and "Open in GitLab" (secondary). It is **not** primary, because the view's primary is Answer when there's a question.
- **Footer:** 28 tall, `bg.chrome`, `caption` `fg.muted`, tabular figures.
  - Contents: the live dot (working glyph on connecting, crashed glyph on bad), "Scanned 2m ago", the rescan quiet icon, and per-state counts with glyphs.
  - It collapses in steps by dropping labels, then counts. It never holds primary actions.

### 10.12 The Basement: sidebar, panes, terminal
- **Sidebar:** `bg.chrome`, or the native sidebar material on macOS 26. Resizable from 200 to 480; the drag handle has a 4px hit area with no visible line.
- **Head:** the project picker (a quiet button with the title in `strong`), host and waiting pills, and quiet icons for new, edit, look and refresh. Refresh has a loading state.
- **Jump field:** §10.4 search.
- **Sections and rows:** §10.8. A workspace row has two lines:
  1. agent glyph · name `strong` · status pill or "waiting on …" in `fg.muted`;
  2. branch (mono `caption`) · MR ref, CI, threads and conflict glyphs.

  A status shows once: when the pill says what the agent glyph would, the glyph is the quiet ring. A project's workspaces sit 16 in from its header, and only the open terminal's row is selected.
  A workspace row is one height with a pill or without, and its MR ref ends the line, so the refs make one column. CI that hasn't started is the queued glyph, never the working one.
- **Hosts:** a host with shells open is a tree row with its shells under it. The idle ones are chips wrapped under those rows, so hosts with nothing running don't take a row each: a small secondary button (§10.2: 24 tall, `element.rest`, radius `sm`, `label`) with the host's dot and name.
- **Empty, loading, error:**
  - Loading is a skeleton of 3 rows per section.
  - With no workspaces: "No workspaces yet", plus a secondary "New workspace…" button.
  - A host that can't be reached: a `status.crashed.fg` caption on the host row with a Retry quiet button.
- **Panes:**
  - The active pane is shown by a 1px `accent.ring` inset border and only appears with 2+ panes. **No blue line.**
  - The pane header shows only in multi-pane layouts: 24 tall, `bg.chrome`, `label`.
  - Dividers are 1px `border.subtle` with a 6px hit area.
- **Terminal canvas:** Ghostty's colours and font, untouched. The chrome around it never takes its colours unless Match terminal is on.
- **Sliding the sidebar:** 320ms `drawer`, using the user's slide setting. 0 or Reduce Motion makes it a cut.
- **Find bar and drop overlay:**
  - The find bar is an input plus quiet icons, `bg.raised`, elevation 2.
  - The drop overlay is a `bg.canvas` scrim at 80% with a 1.5px dashed `accent.ring` frame and a 20 icon with a `title-sm` label.

### 10.13 Hover card, popover, inbox
- **Popover:** `bg.raised`, radius `lg`, 12 padding, elevation 2.
  - 320 wide (hover card) or 240 (context popover).
  - Opens from the trigger at `fast`/`out` (scale .96 plus opacity).
  - Hover intent is 350ms to open (180ms while aiming between cards) and 250ms grace to close. Escape closes it and it stays closed until the pointer leaves.
- **Hover card content, in order:**
  1. Header: status pill, ref, title `strong`, author avatar.
  2. Recommendation in `body-copy`.
  3. Moves: at most 1 primary and secondaries.
  4. The lifecycle checklist.
  5. Stat tiles in a 4-up grid of `label` and value.
  6. Footer with quiet links.
- **Sizing:** it never scrolls inside itself. If content overflows, drop the tiles first.
- **Inbox overlay:** a 400-wide column anchored top-right under the bar, `bg.raised`, elevation 2, radius `lg`, no scrim. Items are rows (§10.8) with the needs glyph, never cards; an open item's answers are secondary buttons (§10.2) one step up, on `element.hover`, since `bg.raised` is `element.rest` in dark. Empty state: "No questions waiting" (the bar may still count workspaces that need you). Escape or clicking outside closes it.

### 10.14 Toast and tooltip
- **Toast:**
  - Bottom-centre, 8px above the footer. `bg.raised` inverted, i.e. `fg.default` background with `bg.canvas` text: the one inverted surface, so it reads as transient.
  - Radius `md`, `label`, at most 1 quiet action plus close. Enters at `base`/`out`, rising 8px.
  - Lasts 5s, or 10s with an action, paused on hover. The error tone adds the crashed glyph; it never turns the toast red.
- **Tooltip:**
  - Native `HintTab`, or a web one with the same recipe: `bg.raised`, `caption`, 6/8 padding, radius `sm`, elevation 2.
  - The first one in a group opens after 500ms; neighbours open instantly within 300ms. Shortcuts show as kbd chips.
  - Tooltips are owned natively where possible, so they can extend past the web view (Raycast's approach).

### 10.15 Sheet, dialog, palette
- **Sheet and dialog:**
  - `bg.raised`, radius `lg`, elevation 3, scrim.
  - 480 wide (small ask), 640 (forms) or 880 (Colours, Diff, Projects).
  - Header: `title` plus a close quiet icon. Body: 16 padding. Footer: the right-aligned primary, secondary to its left; danger only in a confirm step.
  - Enters at `slow`/`out` from scale .96. Escape closes it, unless there's unsaved input, in which case ask first.
  - Focus is trapped and returns to the trigger.
- **Forms:**
  - Labels are `label` above the field, help text `caption` `fg.muted`, errors inline (§10.4).
  - Submit stays enabled until the submit starts, then goes to loading.
- **Palette (⌘K):**
  - 640 wide (with the preview: 880; the preview drops below 640), at 20% of the window height, `bg.raised`, radius `lg`, elevation 3, no scrim: it floats on its shadow, like Spotlight.
  - The input is 44 tall in `title-sm`. Results are rows (§10.8, 32 tall): titles in `body` `fg.default` with the fuzzy match in weight 600, meta and group headers in `label` `fg.muted`, the selected row `element.active`. The search glyph, row icons and footer share one left edge, and the input and titles another.
  - A status shows once per row: under "Needs you" there is no pill. The preview sits on the results' plane behind a hairline and shows only what the row lacks: its kind, a few facts (a value wraps to two lines), then the last lines in a `bg.sunken` well that hugs them.
  - **No animation on open:** it's frequent and keyboard-driven, so it appears instantly.
  - Empty state: "No matches for "…"", plus one suggestion.
- **Ask, New workspace, Projects, Colours, Diff, Delete** are all this recipe.
  - The Colours dialog edits theme inputs (§2.5): neutral hue slider, accent picker, contrast. A live mini-preview shows the dashboard and sidebar in light and dark side by side.
  - Delete lists what will be lost, then a two-step danger confirm.

### 10.16 Logs and output
- **The all-output overlay and the diff use `code`** (mono 12/18) on `bg.sunken`. Entries are separated by 8 of space, not lines.
- **A pane log is dense:** rows are 18 high with no space between them. Its time gutter shows a time on the first row of each minute of output and on every tool call's and prompt's row; the rows after it leave the gutter empty (hovering one shows its time). The terminal's redraws never take a row: blank lines are dropped, and a spinner's run of status lines and stray letters keeps only its latest status line, in `fg.muted`. A short line on its own is output and stays. Search still finds a hidden row and shows it. The All count and a call's line count leave the redraws out.
- **Speakers:** "you" entries use `fg.default` with a 2px `accent.ring` leading edge, tool entries `fg.muted`, and spoken entries `body-copy`.
- **Diff:** added lines are `diff.add` (bg signal.3, text signal.11) and removed lines are `diff.remove` (bg red.3, text red.11). Both stay fixed whatever the accent. The `+`/`−` gutter glyph is always present, so colour is never the only signal. Hunk headers are `label` `fg.muted`.

### 10.17 Chat
- **Messages:**
  - The chat pane uses the system tokens. You: right-aligned bubble in `element.rest`, radius `md`, `body-copy`. Greenlight: no bubble, full width, `body-copy`.
  - Tool and status lines are `caption` `fg.muted` with a glyph.
  - A turn holds together: 12 between your bubble, the reply and its status line, 24 before the next bubble.
- **Header:** a bar like the top bar (§10.11): 44 on `bg.chrome` with its hairline, so the band and the line continue across the window.
  - The name is `title-sm`; where it runs follows in `caption` `fg.muted`. A state shows only while working or offline; idle says nothing.
  - Its actions are 28 icon buttons on the right: New chat, look, voice, pop-out.
- **Surface:** the conversation is `bg.surface` in light and `bg.canvas` in dark, where the reading surface is the window's darkest plane and the bubbles and the composer sit on it.
- **Empty:** the §10.21 empty state, centred in the conversation. Its line names the first action. Once there are sent prompts, a `caption` line under it shows ⌃R as a Kbd; the pane has no key hints elsewhere.
- **Composer:** a §10.4 textarea with radius `md`, like the bubbles, and no rule over it: the text runs the full width, two lines growing to ten, over a row of controls: attach (quiet icon) on the left, mic and send on the right. The placeholder is `fg.faint`. Send is primary only when there's text; otherwise it's a bare `fg.faint` arrow.
- **Footer:** a §10.11 footer on the dashboard footer's line: the model on the left margin, the context on the right.
- **Looks:** the 7 chat looks become token-override sets on the same components, not separate palettes and fonts. Phase one ships **Default** (this spec) and **Phosphor** (a mono, green-on-near-black homage that keeps the terminal's spirit). The other five are parked until someone asks for them back. See §14.

### 10.18 Voice stage (`stage.html`)
- **Base:** the stage uses the same tokens as the chat look.
  - Title is `title-lg`, sub is `body-copy` `fg.muted`, the stat is `display` tabular figures.
  - Items are rows. Tables use tabular figures with tone shown by a glyph (bad / now / ok), not by row tint.
- **Eyebrow:** a `label` in `fg.muted`, sentence case. No uppercase, no tracking.
- **Screen text:** Chief's `[screen]` detail, under the spoken gist. The gist is a `title-sm` lead; the detail reads as a page at `layout.measure`, top-aligned: headings `strong`, lists with `fg.muted` markers, inline code `code` on `element.rest`, fenced code on `bg.sunken`. Markdown is read for structure only: everything lands as text, links as their words.
- **Streaming** (#208): the screen text draws while Chief is still speaking. Settled blocks never re-flow; the last one updates in place and new ones append, with no scroll reset. A 2×13px caret in `accent.solid` sits on the block being written and goes when the turn ends. A half-written marker (`**`, a backtick, a link) waits for its close. A new block or list item rises in once (opacity and a 4px rise at `fast`/`out`), with no motion under reduced motion. Every card is top-aligned. The voice window shows no "said" caption under the stage: Chief's words are on it.
- **MR status chips** (#208): beside the first mention of each MR the snapshot knows (not in code, a path or the spoken lead), its tone's glyph and a short label ("CI running", "Approved") in `label-sm`, coloured by the tone (bad, now, ok, needs), `fg.muted` with no tone. They follow the snapshot: a change updates the chip in place, nothing else on the card moves, and no model is asked. A card that comes again with the same kind and title changes only what differs.
- **Links and options** (#208): a web link in the screen text is a link (`accent.fg`, a `border.subtle` underline that turns solid on hover) and opens in the browser; nothing else is. The reply's Options line is a row of **secondary** buttons (§10.2, md) under the card; a click sends that option as if it were said. A new turn takes them away. Phosphor dashes their outline.
- **Idle board** (#208): in place of a bare "Listening", what needs you and then what's working, from the snapshot with no model call: a `label` section head in `fg.muted` after its tone's glyph, then rows of the ref (`strong`), what it is (one line, ellipsis), its age (`caption`, tabular) and one line of why in `fg.muted`, on `border.subtle` rules. Each snapshot updates it in place. With nothing in either, the stage just listens.
- **Card history** (#208): the last six cards are kept as they were. "Go back" and the quiet icon buttons at the stage's foot (`chevron-left`/`chevron-right`, "2 of 5" in `caption`) step through them; the latest says "Say “go back” for the previous card". An earlier card steps down to `fg.muted` under a `label` "Earlier" in `fg.faint`, and its options are hidden. A new card is the latest again.
- **Canvas in typed chat** (#208): the stage's own page in a panel over the transcript, drawn when a typed reply has a ```` ```stage ```` block or a table (the transcript can't draw a table). Its header is a quiet row (`panel` glyph, "Canvas" in `label`, a chevron) that collapses it; the choice is kept. The reply that drew it says "Updated the canvas" under it, in `caption` `fg.muted` after the `panel` glyph. A new conversation starts without it.
- **Charts:** follow §10.10 and the dataviz skill. The 8-hex hard-coded `PAL` becomes neutral steps plus the accent.
- **Motion:** content enters at `base`/`out` with a 4px rise when drawn cold, or fades cleanly in place (`.emerge`, zero vertical jump) when materializing out of the oscillator field. Direct card-to-card replacements cross-fade (`fast`/`out`); history navigation uses a subtle 6px directional slide (`fast`/`out`, opacity fade only under reduced motion). The listening cursor blinks only while listening and stops under reduced motion.
- **Thinking & reactive field:** a single continuous grid of coupled oscillator needles (Kuramoto: `dθᵢ/dt = ωᵢ + K Σ sin(θⱼ − θᵢ)` over its four neighbours) carries Chief's visual presence across every live-stage transition.
  - Ink is `fg.default` across 16 alpha steps, brighter when a needle is in step with the field. No accent, no glow. Default uses rounded caps; Phosphor uses crisp square (`butt`) dashes. Resizes preserve oscillator phases smoothly instead of re-randomizing.
  - **Display → Thinking (scatter):** when a new turn begins, the outgoing display cross-fades out in place while nearby needles lift off its lines (top-to-bottom wave over 360ms) and decouple into the full-stage thinking field. Earlier cards remain in card history (`‹ ›` / "go back"). Status updates send a subtle top-to-bottom coherence wave through the field.
  - **Thinking → Static card (reveal):** nearby needles (locally assigned by spatial proximity, never cross-screen streaks) lock in step (180ms) and converge onto the incoming card's lines, bar tracks, chart bar tops, or horizontal guide bands for iframes (420ms); the card fades in directly in place (zero jump) as the needles dissolve (540ms).
  - **Thinking → Streaming text (rolling frontier):** streaming text never kills or blocks the field. Opening lines emerge in place while a smoothly tracked frontier follows the growing bottom edge: needles at the edge align horizontally and condense into each arriving block over 240ms while needles below keep drifting until the stream settles.
  - **Subtle reactive state (listening / speaking):** in live voice mode (never on typed chat's canvas), needles breathe softly in open canvas whitespace at least 24px below short cards (or around "Listening"), responding gently to voice RMS energy (`stage.voice(state, energy)`). When a card fills the viewport, or when muted, the field stays completely quiet and stops its loop.
  - **Reduced motion & Settings:** Reduce Motion disables the field completely (150ms opacity fades only). Settings › Voice mode › Thinking motion turns it off (on by default, sent as `data-motion`). With the field off, `stage.hold()` falls back to a still skeleton of bars on `element.active` (12px lead, 8px lines, radius `xs`, no shimmer) so the stage is never blank.
- **Iframes:** each one gets the theme variables, as today.
- **Status line:** from a turn's start until it speaks or draws, and again while Chief works without speaking (about 20 s of quiet), a strip pinned to the stage's foot says what it's doing in one sentence of `label` in `status.working.fg` (under 12 words, ellipsis on overflow), after the working glyph spinning at the `spinner` period (still under reduced motion). It sits on `--bg` with a `border.subtle` top rule, updates in place, and clears when Chief speaks or the turn ends. It is never spoken. Phosphor drops the glyph for a `$ ` prompt and dashes the rule.

### 10.19 Floor bar and orb
- **Floor bar:** a native segmented control (glass in the titlebar on macOS 26).
  - Segments: "Center", "Basement" and "Pipe", each with a 16 glyph, sentence case, the selected one in `fg.default`. No floor colours, no glow.
  - The Auto/Light/Dark track moves to Settings and the View menu. It doesn't belong in the titlebar.
  - The dev-copy tag becomes a `tag` "Dev" with the debug icon.
  - **Project chip:** up to 4 project colour squares are replaced by the project's monogram tag, its name in `label` and a chevron, as a quiet button.
- **Orb:** see §8.4. Draggable within its area; hover raises its elevation; pressed is .97; the ride animation is §7.

### 10.20 Settings
- **Window:** `NavigationSplitView`. The sidebar (220) uses the system sidebar material, and its items are SF-matched icons from the set plus a `body` label. The selected item is `element.active` with `fg.default` and the heavier icon. There are no 22pt coloured icon tiles.
- **Header:** the window title plus a search field (§10.4). The Settings-only sun/moon toggle goes away (appearance is a setting now).
- **Panes:** the pane's title is `title-sm` in the title bar's band, at one position on every pane, and the only 15 on the page. Under it, a 640-wide form column, centred, with grouped sections. Each section has a `strong` header with an optional `caption` description under it, then rows of label (left, `body`) and control (right).
  - Controls are system SwiftUI controls tinted with the accent. Two or three options are a segmented control; four or more are a pop-up button, as wide as its longest option. Every control ends on the row's right edge, one control to a row, and a hint wraps at 420.
  - A setting that can never be turned on shows its value in `fg.muted` with the lock icon, not a disabled switch.
  - A row that depends on the one above is indented 16, with no connector. A row's divider runs from its label to its control's edge (16 in at both ends), no stronger than the card's ring.
  - Text fields follow §10.4: `element.rest` in dark, never darker than the window. A multi-line field is a well under its section header, never inside a card.
  - A list pane (Projects, Repositories, Memory) with nothing to list is its heading and one §10.21 empty state that carries the create button: no empty list column, and no zero count in the sidebar. With rows but nothing picked, the empty state sits in the detail column. The "+ New" footer is `fg.muted`, not the accent.
  - A shortcut list is one column: the label left, the keycaps (§10.5) on the right edge.
  - Banners use the §10.21 recipe.
- **Footer:** gone. Changes apply immediately, the Mac way. The Done button is removed (⌘W closes).
- **Appearance pane:**
  - Appearance (System / Light / Dark).
  - Accent: three swatches, using `AccentDot` with a ring and check.
  - Neutral tint: a slider from 0 to 0.02, with a "Match terminal" toggle.
  - Contrast: follows the system, plus an override.
  - Text size, Chat look, Terminal theme.
  - A live preview card.

### 10.21 Empty, loading, error, banner
- **Empty state:**
  - Centred in its container: a 20 icon in `fg.faint`, a `title-sm` line, a `body` line in `fg.muted`, and at most one secondary button.
  - The copy says what will appear and how to make it happen ("No workspaces yet. Start one with New workspace…").
  - In the sidebar it's compact: one `label` line in `fg.muted` at the rows' text edge and a 24 secondary button under it, left-aligned. No icon and no title.
- **Loading:** skeletons match the final layout exactly, in `element.rest` with no shimmer (reduced-motion-safe by default). The spinner timing rules apply.
- **Error:** what happened, plus the next action, in plain words. Show the code location or detail behind a "Details" disclosure in `code`.
- **Banner** (inline notice):
  - Radius `md`, 12 padding, `status.*.bg` with the status glyph in `status.*.fg` (neutral for info), `body` text in `fg.default`, plus one quiet action. A failure is the crashed pair; needs keeps its diamond for "needs you".
  - It sits 8 under the card it's about, never as a row inside one. One subject gets one banner.
  - Banners stack in this order: crashed, then needs, then info.

## 11. Voice and content

- **Tone:** plain, short, second person, present tense. "Needs you", "Your move", "Agent working", "Agent done", "Waiting on review".
- **Case:** sentence case everywhere, with no all-caps labels. Buttons are a verb plus an object; a label that opens another step ends in `…`.
- **Numbers:** tabular figures, relative short times (`4m`), and real counts. Never round "1 item" up to "items".
- **Confirmations:** ask once before anything that can't be undone, naming what will happen ("Merge !123 into main? This can't be undone.").
- **Banned:**
  - the words seamless, powerful, robust, elevate, unlock, empower, effortless, delve;
  - exclamation marks in UI copy;
  - emoji as icons;
  - "Oops";
  - "Please" in error messages;
  - em dashes as decoration;
  - invented numbers.

## 12. Do and don't

Every ban comes with the move to make instead. The critique agent (§13) checks screenshots against this table, and any match fails the rubric.

| Area | Don't | Do |
|---|---|---|
| Colour | A status colour as decoration, or a coloured section header or tint | Neutral chrome; colour only on the item that needs you, with its glyph |
| Colour | Show a status with colour alone | Colour + glyph shape (+ label when there's room) |
| Colour | Blue, amber, pink or violet as the accent; any hue in 270–330 | Signal, Teal or Graphite, through the §2.3 guard |
| Colour | A literal hex, `rgba()` or `#fff` in UI code | A semantic token (`fg.on_solid`, `bg.raised`, …) |
| Colour | Pure black, or pure white text | `neutral.1` / `neutral.12` |
| Colour | A primary button in solid `#0A84FF`-style blue with white text below 4.5:1 | `accent.solid` (checked at ≥ 4.5:1) |
| Light/dark | Force an appearance per window or floor | Follow the system; the Settings override applies to every window |
| Light/dark | Invert the light palette to make dark | Use the dark scale: lighter as surfaces rise, chrome never lighter than the canvas |
| Light/dark | Cross-fade the UI on a theme switch | Switch instantly with transitions disabled for a frame |
| Type | Tracked all-caps eyebrow labels, uppercase section heads or floor badges | Sentence case `label` in `fg.muted` |
| Type | Half-pixel sizes, sizes outside the role list, weights 550/650 or under 400 | The 10 type roles; weights 400/500/600/700 |
| Type | Proportional figures in counts and timers | Tabular figures everywhere a number changes or lines up |
| Type | Mono for small data labels | Mono only for code, branches, paths and logs |
| Type | "Label: value" pairs everywhere | Values that explain themselves ("3 failing"); the label quieter than the value |
| Layout | Ad-hoc spacing (5, 7, 9, 10, 14px…) | The space scale: 2, 4, 8, 12, 16, 24, 32, 48, 64 |
| Layout | Hover-only controls the keyboard can't reach | Show them on hover, on focus and on selection |
| Layout | Primary actions in the footer or the bottom edge | The footer holds status; actions go in the bar or the content |
| Depth | Glass, blur or SVG glass gradients in content; glass on glass | Flat content; Liquid Glass only in native chrome |
| Depth | One soft grey shadow on every card; opaque or animated shadows | The four elevation levels, by role; lift with a token swap or `transform` |
| Depth | A card inside a card inside a card | Two levels at most; panels never inside cards |
| Depth | Doubled borders where two surfaces meet | One surface owns the edge |
| Shape | Radii outside 4/6/8/12/full; a child rounder than its parent | The radius tokens, concentric (inner = outer − padding) |
| Shape | Hexagons as decoration | The hex only for the workspace cell and the mark |
| Motion | Animating frequent or keyboard actions (⌘K, tabs, row selection) | Instant |
| Motion | Infinite breathing, pulsing or glowing | The needs pulse (3 cycles, then rest), the spinner on the focused item, and the stage's thinking field while Chief works (§10.18) only |
| Motion | `transition: all`, animating layout properties, ease-in, starting from `scale(0)` | `transform` and `opacity` with the `out` curve, from scale .96 |
| Motion | Ignoring Reduce Motion in any file | A 150ms fade, loops stopped, nothing hidden |
| Buttons | More than one primary per view; ghost, gradient or shadowed buttons; one-off variants | The five button variants (§10.2), one primary |
| Buttons | Vague labels ("OK", "Submit", "Go") | A verb plus an object, with `…` when another step follows |
| Buttons | `cursor: pointer` on ordinary controls | The default cursor; pointer only on links |
| Icons | Mixed stroke widths or grids; a hand-drawn one-off glyph | The shared set: 16 grid, 1.5 or 1.75 stroke, round joins |
| Icons | Coloured icon background tiles; duotone; emoji as icons | `currentColor` on the bare glyph; filled only when selected |
| Icons | A redrawn or stand-in brand mark (the Codex hexagon) | The official mark, or a neutral monogram |
| App icon | Baked glow, shadow or specular; a custom outline; words; UI screenshots | Flat layers in Icon Composer, the system squircle, one idea |
| App icon | A checkmark or literal traffic light | A §8.3 concept that suggests "clear to proceed" |
| States | Shipping a component without disabled, loading, empty or error states | Every state in its §10 recipe |
| States | A spinner that flashes for 50ms; a null where content failed | A 200ms delay and 400ms minimum; a skeleton or a "Couldn't load" with Retry |
| Native/web | A flash of white or black while a web view loads | Show the window after the first presentation update |
| Native/web | A web tooltip clipped by the web view | Native-owned tooltips and popovers |
| Copy | The banned words and tells in §11 and RESEARCH §1 | Plain, short, second-person copy |

## 13. Acceptance gate

A UI change is done only when every step below passes. Agents run it themselves and attach the evidence to the PR.

1. **Tokens.**
   - `tokens.py check` regenerates the CSS and Swift from this file's front matter and fails on any diff (parity).
   - It re-runs the contrast checks: step 11 ≥ Lc 60 and step 12 ≥ Lc 90 on step 2, text on solid ≥ 4.5:1, and every `fg.*`/`status.*.fg` on every `bg.*` ≥ WCAG AA.
   - Gamut mapping is by chroma reduction only.
2. **Drift ratchet.** `check_safety.py` counts, per file:
   - in the three HTML files, outside the generated `ds-tokens` block: literal colours (hex, `rgb()`, `hsl()`, `oklch()`), `font-size`/`font` in px, `border-radius` and `box-shadow`/`drop-shadow`;
   - in Swift, outside `Config.swift` and the generated `Tokens.swift`: quoted hex strings, `Color(white:)`-style constructors, `.system(size:)` and literal `cornerRadius`.

   No count may rise above `scripts/drift_baseline.json`. When counts fall, `check_safety.py --update-drift` lowers the baseline. The redesign PRs bring it to zero.
3. **Icons.** `icons.py check`: every `brand/icons/ui/*.svg` has a viewBox of 16 or 20, stroke 1.5 or 1.75, round caps and joins, `currentColor` only, and no transforms. Every name used in code exists. Every file is used somewhere.
4. **Existing gates.** `make lint`, `make test-py`, `make test-js`, `make typecheck`, `make check-diff` and `make test-ui`, plus `scripts/audit.py review` before the PR.
5. **Screenshot matrix** (`scripts/uitest.py`, saved as PR artifacts):
   - Each surface in §0, in light and dark, and with Increase Contrast.
   - **Dashboard:** widths 480, 900 and 1280.
   - **Sidebar:** widths 200, 264 and 480.
   - **States:** empty, loading, one item, dense (40+ workspaces), error, and long names.
   - **Each component in §10:** every state in its recipe.
   - Reduced motion on: no movement in a 2s capture.
6. **Fresh-eyes critique.** A separate agent that didn't write the change sees only the screenshots, this file and the rubric below. It answers each item pass/fail with a one-line reason.
   - **Rubric:**
     - Squint test: is what needs you the most prominent thing?
     - Is there one primary action per view?
     - Is the screen at least 90% neutral when nothing needs you?
     - Is every state double-encoded (colour plus shape)?
     - Is the type limited to the role list, with no uppercase tracking?
     - Are spacing, radius and elevation on-token and concentric?
     - Is it indistinguishable from native (no flicker, no pointer cursors, native-feeling focus)?
     - Does any §11 banned word or slop tell appear (RESEARCH §1)?
     - What one element can be removed?
   - Refine only the top 3 findings per round. **At most 3 rounds.** Anything still failing is either fixed or waived with a written reason in the PR.
7. **Human sign-off.** The owner reviews the screenshot matrix before merge. Design PRs don't automerge.

## 14. Rollout

Each step is one PR with its own screenshots.

1. **Tokens:** `scripts/tokens.py` (stdlib) generates a `ds-tokens` block of `--ds-*` custom properties in each HTML file, and `DS` in `app/Sources/Tokens.swift`, from this front matter. Add the parity, contrast and drift checks. No visual change yet: the old variable names keep their values. `<html data-tokens="v2">` points them at the new tokens to preview the redesign. The dashboard and stage do this now; terminal.html, whose variables are set inline, gets it in step 4. Later steps move each surface onto `--ds-*` and `DS` directly.
2. **Icon set:** draw `brand/icons/ui/` (§9.3) and add `icons.py check`, `app/icons.js` and the Swift loader. Swap the glyphs in all three pages and the native views.
3. **Dashboard:** bars, cards, the board, list view, workspace page, hover card, sheets, widgets, empty, loading and error states.
4. **Basement and Pipe:** sidebar, tabs, panes, palette, dialogs, keyboard support for the tree.
5. **Chat and voice stage:** the Default and Phosphor looks, plus the stage.
6. **Native:** windows, floor bar, orb, Settings, inbox, popovers, menu bar glyph, follow-the-system appearance.
7. **App icon:** concept sketches, then three in Icon Composer, then one, then the `.icon` and `scripts/build_icons.sh`. Retire `Orb`, `MergeLight` and `ThreeFloors`.
8. **Docs:** rewrite `brand/README.md` from this file, and point AGENTS.md's "visual design" rule at `brand/DESIGN.md`.

### Resolved questions
The owner answered these after the first review, keeping the spec as written:
- **Chat and stage looks:** Default and Phosphor only. The other five are parked.
- **Accents:** three, Signal, Teal and Graphite. Old choices migrate per §2.3.
- **Floor names:** Center, Basement and Pipe in the floor bar. "Greenlight Center" stays in the window title.
- **Native icons:** the shared SVG set everywhere. SF Symbols stay only inside system controls.
