#!/usr/bin/env python3
"""The comparison sheet for the icon concepts (brand/DESIGN.md §8.2 tests).

    python3 brand/icon/concepts/sheet.py                # write sheet.html beside this file
    node brand/icon/concepts/render.js                  # render it to sheet.png with headless Chromium (see there)

Every concept at 1024 (shown at 256), 128, 32 and 16 px on a light and a dark wallpaper, in greyscale, in its other
appearances, as the 16pt menu bar template, and in a mock Dock beside neutral placeholder tiles.
"""
import re

from generate import CONCEPTS, HERE

NOTES = {
    "01-aperture": "An iris of six deep-green blades opening on a hexagon of light: the agents step aside and the way clears.",
    "02-slit": "One band of light crosses the tile and widens as it goes, like a door swinging open.",
    "03-lit-cell": "The board's own hexagon, one lit among six faint siblings.",
    "04-lane-clear": "Two lanes merge and carry on as one lit lane: merge, then go.",
    "05-beacon": "One point of light with two arcs on one side: a signal sent.",
    "06-g-lamp": "A geometric G whose open counter holds the light.",
    "07-signal-column": "Three dots in a column, only the top one lit.",
    "08-constellation": "A flock of dim agent nodes following one lit node out in front.",
    "09-horizon": "A light rising over a flat horizon: dawn as go.",
    "10-switch": "A toggle thumb moved to on, made of light.",
}

CSS = """
:root{--canvas:#F6FAF7;--surface:#FAFEFB;--line:#D5DCD7;--fg:#1B1F1C;--muted:#5F6561;
  --wall-light:linear-gradient(135deg,#E9EEF3 0%,#F4EFE6 55%,#E6EEE9 100%);
  --wall-dark:linear-gradient(135deg,#1A2030 0%,#10141C 60%,#151B17 100%);
  --wall-clear:linear-gradient(135deg,#3B5C7A 0%,#7A5C6E 50%,#C79A6A 100%)}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--fg);font:13px/18px -apple-system,BlinkMacSystemFont,"SF Pro Text",
  "Inter","Helvetica Neue",Arial,sans-serif;padding:32px;width:1720px}
h1{font-size:22px;line-height:26px;font-weight:600;margin:0 0 4px}
h2{font-size:15px;line-height:20px;font-weight:600;margin:32px 0 12px}
p.sub{color:var(--muted);margin:0 0 16px}
.wall{border-radius:12px;padding:16px;display:flex;align-items:center;gap:16px}
.wall.light{background:var(--wall-light)} .wall.dark{background:var(--wall-dark)} .wall.clear{background:var(--wall-clear)}
.wall.grey{background:#8A8F8B}
.glance{display:flex;gap:24px}
.glance .wall{gap:20px}
.glance figure{margin:0;display:flex;flex-direction:column;align-items:center;gap:4px;font-size:11px;color:var(--muted)}
.wall.dark figure{color:#B3B9B5}
img{display:block}
.grey img{filter:grayscale(1)}
table{border-collapse:separate;border-spacing:0 12px;width:100%}
td{vertical-align:middle;padding:0 12px 0 0}
.name{width:170px}
.name b{display:block;font-size:15px;line-height:20px;font-weight:600}
.name span{display:block;color:var(--muted);font-size:12px;line-height:16px;margin-top:4px}
.master{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px}
.sizes{gap:12px;padding:12px;align-items:flex-end}
.cap{font-size:11px;color:var(--muted);text-align:center;margin-top:4px}
.wall.dark .cap{color:#B3B9B5}
.apps{display:grid;grid-template-columns:repeat(2,64px);gap:8px;padding:12px;border-radius:12px;background:#C9D0CB}
.apps .clear{background:var(--wall-clear);border-radius:16px;padding:0}
.menubars{display:flex;flex-direction:column;gap:8px}
.bar{height:24px;width:200px;border-radius:6px;display:flex;align-items:center;justify-content:flex-end;gap:14px;
  padding:0 12px}
.bar.l{background:rgba(246,246,246,.92);color:#000;border:1px solid #D5DCD7}
.bar.d{background:rgba(40,40,42,.92);color:#fff}
.bar svg{width:16px;height:16px;display:block}
.ph{width:14px;height:14px;opacity:.85}
.docks{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.dock{display:flex;gap:8px;padding:8px 10px;border-radius:22px;background:rgba(255,255,255,.35);
  border:1px solid rgba(255,255,255,.5);width:max-content}
.wall.dark .dock{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.18)}
.dockrow{display:flex;align-items:center;gap:12px}
.dockrow .n{width:24px;font-size:11px;color:var(--muted);text-align:right}
.wall.dark .dockrow .n{color:#B3B9B5}
.wall.docks-wall{flex-direction:column;align-items:flex-start;gap:10px}
"""

# Neutral placeholder tiles for the Dock: other dev tools, no real logos. (tile fill, glyph fill, glyph)
PLACEHOLDERS = [
    ("#1E1F22", "#9DA3A0", "chevron"), ("#2B2D31", "#C8CDCA", "ring"), ("#F2F3F2", "#5F6561", "grid"),
    ("#121314", "#7C827E", "bars"), ("#3A3D42", "#E4E7E5", "square"), ("#0E1014", "#B3B9B5", "tri"),
]
GLYPHS = {
    "chevron": '<path d="M22 20l12 12-12 12M36 44h10" fill="none" stroke="{c}" stroke-width="5" '
               'stroke-linecap="round" stroke-linejoin="round"/>',
    "ring": '<circle cx="32" cy="32" r="14" fill="none" stroke="{c}" stroke-width="6"/>',
    "grid": '<rect x="18" y="18" width="12" height="12" rx="3" fill="{c}"/><rect x="34" y="18" width="12" height="12" '
            'rx="3" fill="{c}"/><rect x="18" y="34" width="12" height="12" rx="3" fill="{c}"/>'
            '<rect x="34" y="34" width="12" height="12" rx="3" fill="{c}" opacity=".5"/>',
    "bars": '<rect x="18" y="20" width="28" height="6" rx="3" fill="{c}"/><rect x="18" y="30" width="20" height="6" '
            'rx="3" fill="{c}"/><rect x="18" y="40" width="24" height="6" rx="3" fill="{c}"/>',
    "square": '<rect x="20" y="20" width="24" height="24" rx="6" fill="none" stroke="{c}" stroke-width="5"/>',
    "tri": '<path d="M32 18l15 26H17z" fill="{c}"/>',
}
SQ = ("M32 0C52 0 64 6 64 32S58 64 32 64 0 58 0 32 6 0 32 0Z")


def placeholder(fill, ink, glyph, size):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 64 64"><path d="{SQ}" fill="{fill}"/>'
            f'{GLYPHS[glyph].format(c=ink)}</svg>')


def inline_mark(slug):
    svg = (HERE / slug / "mark.svg").read_text()
    return re.sub(r'\s(width|height)="16"', "", svg, count=2)


def img(slug, size, which="preview.svg"):
    return f'<img src="{slug}/{which}" width="{size}" height="{size}" alt="">'


def sized(slug, sizes=(128, 32, 16)):
    return "".join(f'<div>{img(slug, s)}<div class="cap">{s}</div></div>' for s in sizes)


def menubars(slug):
    ph = ('<svg class="ph" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" '
          'stroke-width="1.5"/></svg><svg class="ph" viewBox="0 0 16 16"><rect x="2.5" y="4" width="11" height="8" '
          'rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>')
    m = inline_mark(slug)
    return f'<div class="menubars"><div class="bar l">{ph}{m}</div><div class="bar d">{ph}{m}</div></div>'


def row(slug, name):
    apps = "".join(f'<div class="{"clear" if a == "clear" else ""}" title="{a}">{img(slug, 64, f"appearances/{a}.svg")}'
                   f'</div>' for a in ("dark", "light", "mono", "clear"))
    return (f'<tr><td class="name"><b>{slug[:2]} · {name}</b><span>{NOTES[slug]}</span></td>'
            f'<td><div class="master">{img(slug, 256)}</div></td>'
            f'<td><div class="wall light sizes">{sized(slug)}</div></td>'
            f'<td><div class="wall dark sizes">{sized(slug)}</div></td>'
            f'<td><div class="wall grey sizes">{sized(slug)}</div></td>'
            f'<td><div class="apps">{apps}</div></td>'
            f'<td>{menubars(slug)}</td></tr>')


def dock(slug, size=56):
    tiles = [placeholder(*p, size) for p in PLACEHOLDERS]
    tiles.insert(3, img(slug, size))
    return f'<div class="dockrow"><span class="n">{slug[:2]}</span><div class="dock">{"".join(tiles)}</div></div>'


def glance(cls):
    figs = "".join(f'<figure>{img(slug, 32)}<figcaption>{slug[:2]}</figcaption></figure>' for slug, *_ in CONCEPTS)
    return f'<div class="wall {cls}">{figs}</div>'


def build():
    rows = "".join(row(slug, name) for slug, name, *_ in CONCEPTS)
    docks = "".join(dock(slug) for slug, *_ in CONCEPTS)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Icon concepts</title>
<style>{CSS}</style></head><body>
<h1>Greenlight icon concepts</h1>
<p class="sub">The ten §8.3 directions. Columns: the 1024 master (shown at 256); 128, 32 and 16 px on a light and a dark
wallpaper and in greyscale; the dark, light, mono (tinted) and clear appearances at 64; the mark as the 16pt menu bar
template. Squircle is an approximation for review: Icon Composer applies the real one.</p>
<h2>Grid glance at 32 px</h2>
<div class="glance">{glance("light")}{glance("dark")}</div>
<h2>Each concept</h2>
<table>{rows}</table>
<h2>In the Dock, beside other dev tools (neutral placeholders)</h2>
<div class="docks"><div class="wall light docks-wall">{docks}</div><div class="wall dark docks-wall">{docks}</div></div>
</body></html>
"""


if __name__ == "__main__":
    (HERE / "sheet.html").write_text(build())
    print("wrote", HERE / "sheet.html")
