# Greenlight brand

What Greenlight looks and sounds like, in brief. The source of truth is [`DESIGN.md`](DESIGN.md): its YAML front matter holds the tokens, and its sections hold the rules, the component recipes and the acceptance gate. The evidence behind them is in [`RESEARCH.md`](RESEARCH.md). When this page and DESIGN.md disagree, DESIGN.md wins.

## Character

**Quiet precision.** Calm, exact, fast. The greys carry the interface, and colour appears only where something asks for you. The references are Linear, Raycast and Things. See DESIGN.md §1.

## Name and places

| Name | What it is |
|---|---|
| **Greenlight** | The product: the skill (`SKILL.md`, `scripts/`) and the macOS menubar app (`app/`). "An agent manager for coding-agent workspaces." |
| **Center** (Greenlight Center) | The dashboard and the chat. The full name stays in the window title. |
| **Basement** | The terminal window with its sidebar: Hosts, Needs you, Working, Workspaces, Waiting on others, Done. |
| **Pipe** | The same window without its sidebar. |
| **the orb** | The raised disc at the bottom right that rides between floors. Going up, it shows the mark; going down, the terminal glyph. |
| **No Man's Land** | Repos that belong to no project. |
| **Local Lights** | The look a repo checks in (`.greenlight.json`). |

Every floor follows the system's light or dark appearance; Settings › Appearance can override it. Floors are told apart by name and glyph, never by colour.

## Mark and app icon

**Aperture:** six deep-green blades open on a hexagon of light, the board's own cell. The agents step aside and the way clears.

| File | Use |
|---|---|
| `icon/layers/` | The icon's flat, numbered layers for Icon Composer. Settings are in [`icon/README.md`](icon/README.md). |
| `icon/mark.svg`, `mark-idle.svg`, `mark-light.svg` | The 16-grid mark: lit, the blades alone, and the light alone. The menu bar uses it as a template (idle at rest, lit while agents work), and so do the orb and the floor control's Center. |
| `icons/Greenlight.icns` | The bundle icon and the Dock tile. Built on a Mac by `scripts/build_icons.sh`. |
| `icons/GreenlightDebug.icns` | The same icon with a grey light. Dev copies use it, and so does Settings › General › Dock icon › Debug. |

The rules are in DESIGN.md §8.

## Colour

Colour comes from generated OKLCH scales (DESIGN.md §2). Each scale has 12 steps, and each step has one job. A theme comes from four inputs: neutral hue, neutral chroma, accent and contrast.

- **Signal green**, the brand accent: `#00823A` light, `#05893E` dark. It marks the one primary action per view, links, focus and selection, and covers under 3% of any screen.
- **Accents:** Signal (the default), Teal or Graphite. A guard keeps any accent away from the status hues and from violet.
- **Status colours:** one meaning each, never the accent, and always paired with a glyph.

| State | Colour | Glyph |
|---|---|---|
| needs you | amber | diamond with ! |
| your move | yellow | arrow in a circle |
| working | blue | open ring |
| crashed / failed | red | octagon with × |
| queued | neutral | dotted circle |
| waiting on others | neutral | hourglass |
| done / merged | neutral | check in a circle |

- **Agents:** the Claude mark in clay; Codex (the OpenAI mark) in the text colour. Both sit on a neutral tile.
- **Terminal canvas:** the terminal keeps its Ghostty theme. Named terminal themes colour the canvas only.

## Type, shape, space, motion

- **Type:** SF Pro, plus SF Mono for code, branches and logs, using ten roles (DESIGN.md §3). Body is 13/16 and nothing goes under 11. Weights are 400, 500, 600 and 700. Sentence case everywhere, with no tracked capitals. Every changing number uses tabular figures.
- **Radius:** 4 (tags), 6 (controls), 8 (cards, menus), 12 (sheets, popovers), and full (pills, dots). Corners nest concentrically.
- **Space:** 2, 4, 8, 12, 16, 24, 32, 48 and 64.
- **Elevation:** four layered levels by role. Liquid Glass appears only in native chrome.
- **Motion:** anything frequent or keyboard-driven is instant. Otherwise motion eases out under 300ms. Theme switches never animate, and Reduce Motion turns every movement into a fade (DESIGN.md §7).

## Icons

There is one set, in `icons/ui/`: a 16 grid, a 1.5 stroke, round joins, `currentColor`, and `-fill` variants for selected states. The agent marks are in `icons/marks/`. The pages draw them with `dsIcon` and the native views with `Image(ds:)`. Drawing rules are in DESIGN.md §9.

## How the values reach the code

| Source | Generator | Output |
|---|---|---|
| DESIGN.md front matter | `scripts/tokens.py generate` | a `<style id="ds-tokens">` block in each page (`--ds-*`) and `app/Sources/Tokens.swift` (`DS`) |
| `brand/icons/{ui,marks}/*.svg` | `scripts/icons.py generate` | a `<script id="ds-icons">` block (`dsIcon`) and `app/Sources/Icons.swift` (`Image(ds:)`) |
| `brand/icon/` | `scripts/build_icons.sh` (on a Mac) | `brand/icons/Greenlight*.icns` |

`make lint` runs `tokens.py check` and `icons.py check`. It also runs `check_safety.py`, whose drift baseline (`scripts/drift_baseline.json`) counts literal colours, sizes, radii and shadows, and may only go down.

## Voice

- Plain and short, in second person: "Needs you", "Your move", "Agent working", "Agent done".
- Buttons are a verb plus an object. A label that opens another step ends in "…" (the single character).
- Ask once before anything that can't be undone ("Merge !123 into main? This can't be undone.").
- The banned words and AI-slop tells are listed in DESIGN.md §11 and §12.
