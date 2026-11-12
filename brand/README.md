# Greenlight brand

What Greenlight looks and sounds like. The values here are copied from the code; when they disagree, the code wins, so fix this file. Each section names the file to check.

## Name and places

| Name | What it is |
|---|---|
| **Greenlight** | The product: the skill (`SKILL.md`, `scripts/`) and the macOS menubar app (`app/`). "An agent manager for coding-agent workspaces". |
| **Greenlight Center** · L1 | The dashboard and the chat. Always light. |
| **the Basement** · B1 | The terminal window with its sidebar (Hosts, Workspaces, Waiting on others, Done). Always dark. |
| **the Pipe** · B2 | The same window without its sidebar: just the terminal. Always dark. |
| **the orb** | The green light at the bottom right. It rides up to Greenlight Center, and the terminal button rides back down. |
| **No Man's Land** | Repos that belong to no project. |
| **Local Lights** | The look a repo checks in (`.greenlight.json`). |

Floor badges (`Floors.levels`, `app/Greenlight.swift`):

| Floor | Badge | Fill | Ink | Glow |
|---|---|---|---|---|
| L1 · GREENLIGHT | L1 | `#2E6F43` | `#FFFFFF` | `#56E08B` |
| B1 · BASEMENT | B1 | `#FFB340` | `#1C1C1F` | `#FFB340` |
| B2 · PIPE | B2 | `#A8661C` | `#FFF3E0` | `#E08A2E` |

## App icons (`icons/`)

You edit the SVGs. The `.icns` files are built from them (`rsvg-convert` → `iconutil`), and `app/build.sh` copies them into the app.

| File | Use |
|---|---|
| `orb.svg` → `Orb.icns` | The bundle icon (Finder) and the default Dock tile. A glowing green orb on a dark green squircle. |
| `merge-light.svg` → `MergeLight.icns` | Dock tile option: the orb on top of a merge graph. |
| `three-floors.svg` → `ThreeFloors.icns` | Dock tile option: a traffic-light housing with the green orb lit, plus faint amber (Basement) and copper (Pipe) lights. |

Settings › General › Dock icon switches the Dock tile of the running app; Finder always shows the Orb. The menubar item is SF Symbol `star`, which turns into `star.fill` while agents are working.

All three icons use the same gradients:

| Part | Stops |
|---|---|
| Tile (`bg`) | `#1F3A28` → `#0B160F` |
| Tile, Merge Light (`bg2`) | `#2E6F43` → `#173A23` |
| Orb (radial) | `#E4FFEC` · `#7DF0A8` · `#2FC36C` · `#17864A` |
| Glow (radial) | `#5CF59A` at 55% → `#3DDC84` at 16% → 0 |
| Housing | `#16241A` → `#070D09` |
| Tile edge | white at 8–10%, 2px |
| Specular | white ellipse at 50–55%, rotated −28° |

Squircle: an 824 tile inside a 1024 canvas, radius 185.

## Color

### Brand
- **Greenlight green** (Sage) `#2E6F43`: the light theme's accent, the L1 badge, and the dashboard's light `--accent`.
- **Orb green** `#2FC36C` / glow `#56E08B`.
- **Basement amber** `#FFB340`: the B1 badge, and the primary buttons, checks and focus in the Basement's big dialogs (`--amb`).
- **Pipe copper** `#A8661C`.
- **Accent swatches** (Settings › Appearance): `#F5A524` `#FF7A45` `#F7768E` `#FF79C6` `#BD93F9` `#7AA2F7` `#2AC3DE` `#9ECE6A`.

### State colors: one meaning each, and never the accent
The dashboard (`dashboard.html :root`) and Swift's `Theme` share these.

| State | Meaning | Dark | Light | Ink, dark / light |
|---|---|---|---|---|
| orange | needs you | `#FF9F0A` | `#F08A00` | `#FF9F0A` / `#A85A00` |
| yellow | your move | `#FFD60A` | `#E6B800` | `#FFD60A` / `#8A6D00` |
| blue | working | `#0A84FF` | `#0A7AFF` | `#6FB4FF` / `#0062CC` |
| red | crashed / failed | `#FF453A` | `#E5392F` | `#FF6B61` / `#C62828` |
| green | done / merged | `#30D158` | `#2BA84A` | `#30D158` / `#1E7A36` |
| gray | queued | `#8E8E93` | `#8E8E93` | `#A8A8AD` / `#5E5E66` |
| purple | — | `#BF5AF2` | `#AF52DE` | `#D08CF7` / `#7E2FBF` |
| teal | — | `#64D2FF` | `#16A3CC` | `#64D2FF` / `#0A6F94` |

Tints (pill backgrounds) are the fill at 14–18%. Ink is the color for text on a card, at 4.5:1 or better. For a solid primary button use `--blue-solid` `#0062CC`, because white on `#0A84FF` is only 3.7:1.

### Surfaces
| Token | Dark (Basement, default) | Light (Greenlight Center) |
|---|---|---|
| `--bg` | `#111113` | `#FFFFFF` |
| `--bar` / `--strip` | `#19191C` | `#F5F8F4` |
| `--card` | `#1C1C1F` | `#FFFFFF` |
| `--card2` / `--sheet` | `#232327` | `#EAF0E9` |
| `--text` / `--text2` / `--text3` | `#F5F5F7` / `#C9C9CF` / `#9A9AA1` | `#18281C` / `#3D5243` / `#4E6352` |
| `--line` / `--hair` | white 12% / 8% | `#DCE5DA` / `#ECF1EB` |

The light theme is tinted green and the dark theme is neutral.

### Basement sidebar sections (`app/terminal.html paint()`)
| Key | Section | Dark | Light |
|---|---|---|---|
| `t` | Hosts | `#66D4CF` | `#0E7C77` |
| `b` | Workspaces | `#4DA3FF` | `#0969DA` |
| `y` | Waiting on others | `#FFD60A` | `#8A6A00` |
| `g` | Done | `#30D158` | `#1A7F37` |
| `o` `r` `p` | needs you, failed, other | `#FF9F0A` `#FF6259` `#C597FF` | `#B25000` `#C9372C` `#8250DF` |

A repo's `.greenlight.json` `look` can override these keys, and it changes the chrome only, never the terminals.

### Agents
- Claude `#D97757`: the asterisk mark.
- Codex `#3A3A3E` with a white glyph. The hexagon is our stand-in, not Codex's logo.

## Type
- UI: `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif`.
- Code and terminal: `ui-monospace, "SF Mono", Menlo, monospace` (Settings › Fonts; Ghostty's font wins when it is set).
- Scale (`--fs-1…6`): 11 · 12 · 13 · 15 · 18 · 22 px, with line heights 14 · 16 · 18 · 20 · 22 · 26.
- Floor badges and section headers are uppercase, 700 weight, letter-spacing .04em.

## Shape and space
- Radius (`--r-1…4`): 6 · 10 · 14 · 20. In order: controls, cards, sheets, big surfaces.
- Spacing (`--sp-1…6`), on a 4px grid: 4 · 8 · 12 · 16 · 24 · 32.
- Elevation: `0 12px 32px` at 50% black (dark) or `rgba(25,50,30,.12)` (light).
- Focus ring: `0 0 0 2px bg, 0 0 0 4px accent`.

## UI glyphs
These are inline SVGs on a 24px or 16px grid, 1.5–2px round strokes, `currentColor`. They live as code:
- `app/terminal.html`: `I` (claude, codex, shell, bell, split right/down, plus, x, chevron, sidebar, brush, window, edit, refresh, merge, check, spinner, spark), `IC` (card actions, goal target), and `G` (sidebar badges, branch).
- `app/dashboard.html`: its own inline set.
- The forges (GitLab, GitHub) show people by their avatars, with the username on hover.

## Motion
- Pipe → Basement: the sidebar slides in, ease-out `cubic-bezier(.2,.8,.2,1)`. Basement → Pipe: it slides out, ease-in `cubic-bezier(.4,0,.8,.2)`. The default is 0.25 s; Settings › General › Sidebar slide changes it, and 0 or Reduce Motion turns the slide into a cut.
- Between floors the orb flips (Settings › Animations).

## Voice
- Plain and short, in second person: "needs you", "your move", "Agent working", "Agent done".
- Orange always means *needs you* and red always means *crashed*. A state color is never decoration.
- Ask once before anything that can't be undone ("Confirm merge !N · can't be undone").
