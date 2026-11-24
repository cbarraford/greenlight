# Redesign research

What the best current sources say about top-quality UI, collected (Nov 2026) as the input to the redesign spec. Each rule names its source. This file is the evidence; the spec (`brand/DESIGN.md`, next) is the decision. Where the sources disagree, that's said and a pick is made for Greenlight: a dense macOS developer tool, a SwiftUI shell plus WKWebView HTML.

## 1. Why AI-made UI looks generic, and the fix

- **Distributional convergence.** If you leave a choice open, a model picks the most likely answer: Inter, a purple gradient on white, the same rounded card everywhere, no motion. The fix is to steer each dimension separately (type, colour, motion, backgrounds) and to name the defaults to avoid. Typography alone gives "significant improvement". ([Anthropic blog](https://claude.com/blog/improving-frontend-design-through-skills), [cookbook](https://github.com/anthropics/claude-cookbooks/blob/main/coding/prompting_for_frontend_aesthetics.ipynb))
- **Ban lists go stale.** Once Inter was banned, models moved on to Space Grotesk. The current frontend-design skill also bans the new defaults: cream + serif + terracotta, near-black + one acid accent, zero-radius hairline "broadsheet", the SaaS card kit. ([frontend-design SKILL.md](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md))
- **Specs beat vibes.** Write the decisions down as tokens and one recipe per component, then check them with a gate. Don't rely on the agent's taste. ([Taste Skill](https://github.com/Leonxlnx/taste-skill), [MengTo/Skills](https://github.com/MengTo/Skills), [Google DESIGN.md](https://github.com/google-labs-code/design.md))
- **Plan before code.** Write a token plan and wireframes, critique the plan for defaults, build, critique the screenshots, then remove one element. ([frontend-design SKILL.md](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md))
- **Gate on pass/fail, not self-judgement.** The Taste Skill's `gate.mjs` screenshots at two widths and checks:
  - empty area
  - overlap
  - tap size
  - overflow
  - accessibility
  - copy tells

  It allows 3 rounds at most, and every waiver needs a written reason. Claude Code's best practices: give the agent a mockup, make it show screenshot evidence, and have a fresh agent review. ([gate.md](https://github.com/Leonxlnx/taste-skill/blob/main/skills/taste-skill/references/gate.md), [Claude Code best practices](https://code.claude.com/docs/en/best-practices))

**Slop tells to ban** (Anthropic, Taste Skill):
- Visual:
  - tracked ALL-CAPS eyebrow labels
  - one radius and one soft grey shadow on every card
  - card inside a card
  - glass content panels
  - gradient, ghost or shadowed buttons, or a third button style
  - monospace for small data labels
  - decorative numbering
  - arrows appended to every link
  - fade-and-slide on every section
  - hover effects on every card
  - violet or lavender
- Copy:
  - seamless, powerful, robust, elevate, unlock, empower
  - em dashes everywhere
  - emoji bullets
  - "Not X but Y"
  - invented numbers and testimonials

## 2. Hierarchy, space, density

- **Quieten the secondary instead of shouting the primary.** Labels are a last resort: "3 failing" beats "Failing: 3". Where users scan by label, flip the emphasis. ([Refactoring UI](https://www.refactoringui.com/previews/labels-are-a-last-resort))
- **Chrome recedes so the data wins.** Linear 2026:
  - "Don't compete for attention you haven't earned": the sidebar is dimmed a few notches.
  - "Structure should be felt not seen": fewer, softer separators.
  - Smaller icons, and no coloured icon backgrounds.

  ([Linear refresh](https://linear.app/now/behind-the-latest-design-refresh))
- **One system, no exceptions.**
  - Spacing 4/8/12/16/24/32/48/64 only.
  - One button shape, one radius family, one card recipe, one hairline.
  - At most one filled button per view.
  - Cards nest two levels at most.
  - The interface is about 90–95% neutral; the accent covers under 3% (primary action, links, focus).

  ([Taste Skill system.md](https://github.com/Leonxlnx/taste-skill))
- **Alignment.** Everything sits on a grid, a baseline, an edge or an optical centre; nudge ±1px where the eye disagrees with the maths. A child's radius is ≤ its parent's and concentric with it (inner = outer − padding). ([Vercel guidelines](https://vercel.com/design/guidelines), [Apple: adopting Liquid Glass](https://developer.apple.com/documentation/technologyoverviews/adopting-liquid-glass))
- **Design every state**: empty, sparse, dense, error, very long content. Skeletons must match the final layout exactly. ([Vercel guidelines](https://vercel.com/design/guidelines))

## 3. Colour

- **Work in OKLCH.** Equal L looks equally light across hues, and changing chroma doesn't shift hue. Hover is L ±0.1. Ship sRGB plus a P3 override. Gamut-map by lowering chroma, never by clipping. ([Evil Martians](https://evilmartians.com/chronicles/oklch-in-css-why-quit-rgb-hsl), [Tailwind v4 colours](https://tailwindcss.com/docs/colors))
- **Generate themes from a few inputs.** Linear went from 98 variables per theme to 3: base, accent, contrast. Elevations and text, icon and border colours are all derived, and the accent is limited in how far it bleeds into neutrals. ([Linear UI redesign](https://linear.app/now/how-we-redesigned-the-linear-ui))
- **Give each step of a scale one job.** This is the Radix 12-step model ([Radix](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale)):

  | Steps | Job |
  |---|---|
  | 1–2 | app and subtle backgrounds |
  | 3–5 | element background at rest, hover, active |
  | 6–8 | borders: subtle, interactive, hover |
  | 9–10 | solid fill and its hover |
  | 11 | low-contrast text (APCA Lc 60 on step 2) |
  | 12 | high-contrast text (Lc 90) |

  Geist and Stripe follow the same idea. In Stripe's scale, steps 5 apart pass 4.5:1. ([Geist](https://vercel.com/geist/colors), [Stripe](https://stripe.com/blog/accessible-color-systems))
- **Dark mode is its own palette, not an inversion.**
  - Elevated surfaces are lighter. Backgrounds step about +0.03–0.04 L each.
  - Text sits around L 0.77 and 0.95, with less chroma than the fills.
  - Never pure black or pure white text.
  - Linear warmed and desaturated its greys in 2026, but warning that too warm reads muddy.

  ([Apple dark mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode); Radix values converted to OKLCH; [Linear refresh](https://linear.app/now/behind-the-latest-design-refresh))
- **Contrast.** Design to APCA and ship with WCAG AA as the floor ([APCA](https://git.apcacontrast.com/documentation/APCA_in_a_Nutshell.html)). Apple asks for 4.5:1, aims for 7:1 on small custom text, and wants testing under Increase Contrast and Reduce Transparency.

  | APCA Lc | Use |
  |---|---|
  | 90 | body text |
  | 75 | minimum for body text |
  | 60 | other text |
  | 45 | headlines and small icons |
  | 30 | disabled text |
  | 15 | dividers |
- **Status colour.** Orange/blue and red/green are confusable pairs, so always double-encode with a shape per state (Apple's example: check-in-circle vs X-in-octagon). Spread the states across lightness as well as hue. ([Apple accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility), [Smashing](https://www.smashingmagazine.com/2024/02/designing-for-colorblindness/))
- **Token layers**: primitive (`green-9`), then semantic (`bg.subtle`, `fg.muted`, `status.needs.fg`), then a few component tokens. Use DTCG-style JSON as the single source and generate both CSS and Swift from it. ([Primer](https://primer.style/foundations/color/overview), [DTCG](https://www.designtokens.org/tr/drafts/format/))

## 4. Type

- **macOS text styles** ([Apple typography](https://developer.apple.com/design/human-interface-guidelines/typography)):

  | Style | pt / line height |
  |---|---|
  | Large Title | 26/32 |
  | Title 1 | 22/26 |
  | Title 2 | 17/22 |
  | Title 3 | 15/20 |
  | Body | 13/16 |
  | Callout | 12/15 |
  | Subheadline | 11/14 |
  | Caption | 10/13 |

  Below 10 is off-limits; avoid the Ultralight, Thin and Light weights. Apple moved section headers from all caps to title case.
- **Tracking.** SF Pro tracks itself natively. In CSS, use Apple's table:

  | Size | 11 | 13 | 15 | 17 | 22 | 26 |
  |---|---|---|---|---|---|---|
  | Tracking | +0.006 | −0.006 | −0.016 | −0.026 | −0.012 | +0.008 em |

  Inter tightens steadily as it grows: −0.003 at 13, −0.017 at 20, −0.022 at 32 ([Inter dynmetrics](https://d.rsms.me/inter-website/v3/dynmetrics/)).
- **Contrast between weights and sizes.** The Anthropic cookbook pushes extremes: weights 100/200 against 800/900, size jumps of 3×. That fits marketing pages more than a dense Mac app, where Apple's floor is 400. Greenlight's pick: weights 400/500/600/700, and hierarchy from a deliberate jump in size and colour, not from many sizes in between.
- **Label vs copy roles.** Single-line UI labels take tight leading (~1.2); multi-line copy takes ~1.45 ([Geist typography](https://vercel.com/geist/typography)).
- **Numbers.** Use `tabular-nums` (SwiftUI `.monospacedDigit()`) on every count, timer and cost, and the real `…` character ([Vercel guidelines](https://vercel.com/design/guidelines)). Linear sets headings in Inter Display and body in Inter; Vercel uses Geist Sans with Geist Mono.

## 5. Depth and materials

- **Layered, translucent shadows from one light source.**
  - Keep a fixed x:y ratio, and as elevation rises let the blur grow while the opacity falls.
  - Tint shadows toward the background hue.
  - Don't animate heavy layered shadows.

  ([Josh Comeau](https://www.joshwcomeau.com/css/designing-shadows/))
- **A shadow can be the border** (light mode): `0 0 0 1px rgb(0 0 0/.06), 0 1px 2px -1px rgb(0 0 0/.06), 0 2px 4px rgb(0 0 0/.04)`, raised to .08/.08/.06 on hover. Draw each border once, because overlapping alpha borders darken. ([Jakub Krehel](https://jakub.kr/work/shadows), [Geist](https://vercel.com/geist/colors))
- **Liquid Glass is only for the control and navigation layer.**
  - Use it on toolbars and sidebars, through the system components. Never in content, and never glass on glass.
  - Use regular glass behind text. Clear glass goes only over rich media, with a 35% dim.
  - Remove custom backgrounds from toolbars and split views.

  Linear built its own glass from blur, gradient and a specular highlight, without refraction, because refraction hurts readability in dense UI. ([Apple materials](https://developer.apple.com/design/human-interface-guidelines/materials), [Linear liquid glass](https://linear.app/blog/linear-liquid-glass))
- **Hybrid native and web** (Raycast 2.0):
  - Users must never be able to tell a WebView is there.
  - The web view is transparent over the native material.
  - No flicker.
  - No `cursor:pointer` or hover highlight on ordinary controls.
  - Native code owns tooltips and popovers, so they can extend past the window.

  ([Raycast deep dive](https://www.raycast.com/blog/a-technical-deep-dive-into-the-new-raycast))

## 6. Motion

- **Animate by frequency** ([Emil Kowalski STANDARDS](https://raw.githubusercontent.com/emilkowalski/skills/main/skills/review-animations/STANDARDS.md)):

  | How often it happens | Motion |
  |---|---|
  | 100+ times a day, or from the keyboard | none |
  | tens of times a day (hover, list nav) | little or none |
  | occasional (modals, toasts) | standard |
  | rare moments | can delight |

  Removing animation from frequent commands made them feel faster ([Rauno](https://rauno.me/craft/interaction-design)).
- **Easing.** Enter and exit use ease-out `cubic-bezier(.23,1,.32,1)`. Movement on screen uses `cubic-bezier(.77,0,.175,1)`. Drawers use `cubic-bezier(.32,.72,0,1)`. Colour changes use `ease`, progress uses linear, and nothing uses ease-in. (Emil)
- **Durations.**

  | Element | Duration |
  |---|---|
  | press | 100–160 ms |
  | tooltip | 125–200 ms |
  | dropdown | 150–250 ms |
  | modal or drawer | 200–500 ms |
  | general UI | under 300 ms |
  | stagger | 30–80 ms |

  Pressed state is `scale(.97)`; enter from scale .9–.97 plus opacity, never from 0. Springs bounce 0.1–0.3. (Emil)
- **Only transform and opacity**, never `transition: all`, and animations must be interruptible. Reduced motion keeps a 0.2 s fade and drops the movement. Theme switches happen instantly, with no transition. ([Vercel](https://vercel.com/design/guidelines), [Paco](https://paco.me/writing/disable-theme-transitions))

## 7. Interaction polish

- **Hit targets.** Apple says 44pt, even for a pointer. Vercel says 24px or more on desktop. Greenlight's pick: at least 24px visible plus a padded hit area, 28px for primary controls. ([Apple buttons](https://developer.apple.com/design/human-interface-guidelines/buttons), [Vercel](https://vercel.com/design/guidelines))
- **Spinners.** Show one only after 150–300 ms, and once shown keep it at least 300–500 ms. Update optimistically and offer Undo. Delay the first tooltip in a group; its neighbours then open instantly. Use `:focus-visible`. Give every custom control a press state. Keep to 1–2 prominent buttons per view, and add `…` to labels that open another view. (Vercel, Apple)

## 8. App icon (macOS 26 / 27)

- **The file.**
  - A 1024 canvas, layered, at most 4 groups, built in Icon Composer as a `.icon` file.
  - Export square, unmasked SVG layers with no baked shadows, glows, blur or squircle.
  - Six appearances: default, dark, clear light/dark, tinted light/dark.
  - A non-squircle icon gets shrunk into a grey box ("icon jail").

  ([Apple app icons](https://developer.apple.com/design/human-interface-guidelines/app-icons), [Icon Composer](https://developer.apple.com/documentation/xcode/creating-your-app-icon-using-icon-composer), [Rogue Amoeba](https://weblog.rogueamoeba.com/2026/06/26/free-the-icons/))
- **The design.**
  - One idea, few shapes, no thin lines, no UI, no words.
  - Mono and tinted variants need a white or near-white foreground part.
  - Set specular inside when the foreground is darker, outside when it's lighter.
  - Compare 26 vs 27 rendering; 27 is sharper, with less glass.

  ([WWDC25-361](https://developer.apple.com/videos/play/wwdc2025/361), [WWDC26-8012](https://developer.apple.com/videos/play/wwdc2026/8012/))
- **Memorable icons** (Michael Flarup):
  - Scalable, recognisable, consistent, unique, no words.
  - Survey the category first. "The world doesn't need another checkmark icon."
  - Strip details until the concept breaks, then add back the last one.
  - Grid-glance the options at 32px. Test on light wallpapers, where uniform specular muddies detail.
  - With the squircle forced, colour is the main differentiator, so back it with strong form.

  ([ApplyPixels](https://applypixels.com/blog/how-to-design-better-app-icons), [flarup.email](https://www.flarup.email/p/through-the-liquid-glass))
- **Identity carried through.**
  - Stripe's 2025 mark is a detail from its own wordmark, so mark and wordmark share a gene.
  - Vercel uses the full logo when there's room and the symbol alone when it's tight, with clear space equal to the symbol's height.
  - Raycast's keycap icon is the product's idea (keyboard-first), and it ships a separate debug-build icon.

  ([Vercel brands](https://vercel.com/geist/brands), [Raycast](https://www.raycast.com/blog/a-fresh-look-and-feel))

## 9. UI icon set

- **Pick one canvas, one stroke, one radius rule, round caps and joins, and never mix.** The main sets disagree:

  | Set | Canvas | Stroke | Radius |
  |---|---|---|---|
  | Lucide | 24 | 2 | 2 (1 under 8px) |
  | Octicons | separate 16 and 24 drawings | 1.5 | 1, outer edges pixel-snapped |
  | Heroicons | 24 | 1.5 | — |
  | IBM (snippets only) | — | steps up: 1 @16, 1.25 @20, 1.5 @24 | — |

  ([Lucide guide](https://lucide.dev/contribute/icon-design-guide), [Octicons guide](https://primer.style/octicons/design-guidelines))
- **Matching and placement.** Match icon weight to the text weight next to it, and optically centre asymmetric glyphs. Use outline in toolbars and lists, and fill for the selected or emphasised state. SF Symbols are for native chrome; custom symbols start from an exported SF template. ([Apple icons](https://developer.apple.com/design/human-interface-guidelines/icons), [SF Symbols](https://developer.apple.com/design/human-interface-guidelines/sf-symbols))
- **Pixel fitting.** Even strokes go on even dimensions and odd on odd; work at 1× on whole points ([Bjango](https://bjango.com/articles/icondesignworkflow/)).

## 10. What this means for today's Greenlight

Gaps between the current look (`brand/README.md`) and the research, for the spec to settle:

1. **Floors with a fixed appearance.** L1 is always light and B1/B2 are always dark. This conflicts with Apple: follow the system appearance, with any override opt-in. If floors keep their own identity, the spec has to make that a deliberate choice.
2. **All-caps 700 tracked headers and floor badges.** These are a slop tell, and Apple dropped them.
3. **The colour system.**
   - Colours are hand-picked hex values. There is no generated scale, no APCA check, and no single source shared by Swift and CSS (they are kept in step by hand today).
   - The accent swatches include violet (`#BD93F9`) and pink.
   - There are 8 state hues where only 6 have a meaning.
4. **State is colour only in places.** Orange (needs you) next to blue (working) is a confusable pair, so each state needs its own glyph shape.
5. **Three competing app icons** (Orb, Merge Light, Three Floors), drawn as flat SVGs with baked glows and specular. That is the opposite of the layered Icon Composer model. The orb and traffic-light ideas are also the obvious metaphor for "greenlight", which is Flarup's checkmark warning.
6. **Two hand-rolled inline glyph sets** (`terminal.html`, `dashboard.html`) at mixed 1.5–2px strokes, plus SF Symbols in the native panes. That makes three sets.
7. **The Codex hexagon stand-in and the Claude asterisk.** The research says to use real product marks where the licence allows.
8. **Glass tokens (`--glass`, `--gl-*`) in the content layer.** Apple and Taste Skill both say glass belongs only in controls and navigation.

## 11. Proposed spec outline (`brand/DESIGN.md`)

The outline follows the DESIGN.md section order:

0. Tokens in YAML: colour roles, type roles, spacing, radius, elevation, motion, components with their states.
1. Overview: users, the main job, 3–5 principles, 2–3 named references.
2. Colour.
3. Type.
4. Layout and density.
5. Depth and materials.
6. Shape.
7. Motion.
8. Iconography: the app icon and the UI set.
9. Voice.
10. Components: one recipe each, every state.
11. Do / don't.
12. Acceptance gate: `make test-ui` screenshots in light and dark plus every state, an APCA check, a token-parity check between Swift and CSS, and a review by a fresh agent against a rubric, at most 3 rounds.
