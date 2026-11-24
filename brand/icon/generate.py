#!/usr/bin/env python3
"""Greenlight's app icon, Aperture (brand/DESIGN.md §8): the layers, the appearance previews and the mark.

    python3 brand/icon/generate.py                     # rewrite layers/, layers-light/, previews/ and the marks
    python3 brand/icon/generate.py icns default|debug  # print the macOS-grid SVG that scripts/build_icons.sh renders

Six deep-green blades open on a hexagon of light. The hexagon is the board's own cell (pointy-top), so the icon,
the menu bar mark and the orb share one form.

- layers/: the default appearance as square 1024 SVGs, unmasked and numbered back to front (background, light, blades).
  Icon Composer applies the squircle and the glass, so nothing here is glowing, shadowed or blurred.
- layers-light/: the same groups in the light appearance (a pale ground with a Signal-green light).
- previews/: every appearance (and Debug) composited in an approximate squircle, for review only.
- mark.svg: the foreground alone on a 16 grid in currentColor (blades at half alpha, the light solid): the About
  box, the orb glyph, and the menu bar's lit state while agents work. mark-idle.svg: the blades alone, the opening
  empty: the menu bar at rest.
- `icns VARIANT` reads layers/ back from disk, so those files stay the source of truth for the .icns too.
"""
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import tokens  # noqa: E402

S, C = 1024, 512
R_OUT, R_OPEN, GAP = 372, 196, 24  # the iris, the opening's circumradius, the gap between blades


def ok(L, Ch, H):
    """An OKLCH colour as sRGB hex, chroma lowered (never clipped) to fit the gamut."""
    return tokens.oklch_hex((L, tokens.gamut_map(L, Ch, H), H))


# bg is the (top, bottom) of the background's vertical gradient; blades and light are (colour, opacity).
APPEARANCES = {
    "default": {"bg": (ok(0.285, 0.034, 155), ok(0.205, 0.028, 155)),
                "blades": (ok(0.43, 0.08, 152), 1), "light": (ok(0.85, 0.19, 150), 1)},
    "dark": {"bg": (ok(0.19, 0.022, 155), ok(0.13, 0.016, 155)),
             "blades": (ok(0.36, 0.07, 152), 1), "light": (ok(0.83, 0.19, 150), 1)},
    "light": {"bg": (ok(0.975, 0.012, 155), ok(0.925, 0.016, 155)),
              "blades": (ok(0.80, 0.06, 152), 1), "light": (ok(0.62, 0.17, 150), 1)},
    # Tinted / mono: the system tints a greyscale image, so the light is the white part and the blades sit well below.
    "mono": {"bg": ("#2A2A2C", "#19191B"), "blades": ("#FFFFFF", 0.36), "light": ("#FFFFFF", 1)},
    "clear": {"bg": ("rgba(255,255,255,0.20)", "rgba(255,255,255,0.10)"),
              "blades": ("#FFFFFF", 0.40), "light": ("#FFFFFF", 1)},
    # Debug (dev copies): the same icon with a neutral-grey light.
    "debug": {"bg": (ok(0.285, 0.034, 155), ok(0.205, 0.028, 155)),
              "blades": (ok(0.43, 0.08, 152), 1), "light": (ok(0.80, 0.008, 155), 1)},
}


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _unit(x, y):
    d = math.hypot(x, y)
    return x / d, y / d


def _intersect(p, d, q, e):
    """The point where line p + t·d meets line q + s·e."""
    den = d[0] * e[1] - d[1] * e[0]
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return p[0] + t * d[0], p[1] + t * d[1]


def _to_circle(p, d, r):
    """The forward hit of the ray p + t·d on the circle of radius r about the centre."""
    px, py = p[0] - C, p[1] - C
    b = px * d[0] + py * d[1]
    t = -b + math.sqrt(b * b - (px * px + py * py - r * r))
    return p[0] + t * d[0], p[1] + t * d[1]


def _shift(p, d, amount, inside):
    """Move the line through p along d by amount, toward the point `inside`."""
    n = (-d[1], d[0])
    s = 1 if n[0] * (inside[0] - p[0]) + n[1] * (inside[1] - p[1]) > 0 else -1
    return p[0] + n[0] * amount * s, p[1] + n[1] * amount * s


def blades():
    """Six blades tiling the iris outside a pointy-top hexagon: each one runs along a side of the opening and ends
    where the next side's extension cuts it. Each blade is [corner, outer point, arc end, sweep flag]."""
    verts = [polar(C, C, R_OPEN, -90 + 60 * k) for k in range(6)]
    dirs = [_unit(verts[(k + 1) % 6][0] - verts[k][0], verts[(k + 1) % 6][1] - verts[k][1]) for k in range(6)]
    out = []
    for k in range(6):
        v1, v2 = verts[(k + 1) % 6], verts[(k + 2) % 6]
        d0, d1 = dirs[k], dirs[(k + 1) % 6]
        inside = ((v1[0] + v2[0]) / 2 + (d0[0] + d1[0]) * 90, (v1[1] + v2[1]) / 2 + (d0[1] + d1[1]) * 90)
        a = _intersect(_shift(v1, d0, GAP / 2, inside), d0, _shift(v1, d1, GAP / 2, inside), d1)
        b, c = _to_circle(a, d0, R_OUT), _to_circle(a, d1, R_OUT)
        turn = math.degrees(math.atan2(c[1] - C, c[0] - C) - math.atan2(b[1] - C, b[0] - C))
        out.append((a, b, c, 1 if (turn + 540) % 360 - 180 > 0 else 0))
    return out


def opening():
    """The light: exactly the hexagon the blades leave open (plus 1 so no seam shows), so a translucent blade in the
    tinted and clear appearances never shows the light through it."""
    r = (R_OPEN * math.cos(math.pi / 6) + GAP / 2 + 1) / math.cos(math.pi / 6)
    return [polar(C, C, r, -90 + 60 * k) for k in range(6)]


def fmt(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def pt(p, k=1.0, off=0.0):
    return f"{fmt((p[0] - off) * k)} {fmt((p[1] - off) * k)}"


def blades_d(k=1.0, off=0.0):
    """The six blades as one path; k and off map the 1024 canvas onto another grid."""
    r = fmt(R_OUT * k)
    return "".join(f"M{pt(a, k, off)}L{pt(b, k, off)}A{r} {r} 0 0 {sw} {pt(c, k, off)}Z" for a, b, c, sw in blades())


def light_d(k=1.0, off=0.0):
    return "M" + "L".join(pt(p, k, off) for p in opening()) + "Z"


SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {0}" width="{0}" height="{0}">{1}</svg>\n'


def paint(colour, alpha):
    return f'fill="{colour}"' + ("" if alpha == 1 else f' fill-opacity="{alpha}"')


def layer_files(pal):
    top, bot = pal["bg"]
    bg = (f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/>'
          f'<stop offset="1" stop-color="{bot}"/></linearGradient></defs>'
          f'<rect width="{S}" height="{S}" fill="url(#bg)"/>')
    return {"01-background.svg": SVG.format(S, bg),
            "02-light.svg": SVG.format(S, f'<path d="{light_d()}" {paint(*pal["light"])}/>'),
            "03-blades.svg": SVG.format(S, f'<path d="{blades_d()}" {paint(*pal["blades"])}/>')}


def squircle(x0, size, n=5.0, steps=360):
    """A superellipse close to the system's continuous-corner squircle."""
    h = size / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        pts.append(f"{fmt(x0 + h + h * math.copysign(abs(ct) ** (2 / n), ct))} "
                   f"{fmt(x0 + h + h * math.copysign(abs(st) ** (2 / n), st))}")
    return "M" + "L".join(pts) + "Z"


def inner(svg_text):
    """A layer file's contents without its <svg> wrapper."""
    return re.sub(r"^<svg[^>]*>|</svg>\s*$", "", svg_text.strip())


def composite(files, inset, uid):
    """The layers inside a squircle. inset 0 fills the canvas (a preview); inset 100 is the macOS icon grid (an 824
    tile in 1024), which the .icns uses."""
    size = S - 2 * inset
    body = "".join(inner(files[k]).replace('id="bg"', f'id="bg-{uid}"').replace("url(#bg)", f"url(#bg-{uid})")
                   for k in sorted(files))
    k = size / S
    return SVG.format(S, f'<defs><clipPath id="sq-{uid}"><path d="{squircle(inset, size)}"/></clipPath></defs>'
                         f'<g clip-path="url(#sq-{uid})"><g transform="translate({inset} {inset}) scale({fmt(k)})">'
                         f'{body}</g></g>')


def on_disk(variant):
    """layers/ as committed; the Debug variant swaps in the neutral-grey light."""
    files = {p.name: p.read_text() for p in sorted((HERE / "layers").glob("*.svg"))}
    if variant == "debug":
        files["02-light.svg"] = files["02-light.svg"].replace(APPEARANCES["default"]["light"][0],
                                                              APPEARANCES["debug"]["light"][0])
    return files


def marks():
    """The 16-grid marks: the iris fills the grid with a half-unit margin."""
    k = 15 / (2 * R_OUT)
    off = C - R_OUT - 0.5 / k
    lit = (f'<path d="{blades_d(k, off)}" fill="currentColor" fill-opacity="0.5"/>'
           f'<path d="{light_d(k, off)}" fill="currentColor"/>')
    idle = f'<path d="{blades_d(k, off)}" fill="currentColor"/>'
    return {"mark.svg": SVG.format(16, lit), "mark-idle.svg": SVG.format(16, idle)}


def write():
    for folder, app in (("layers", "default"), ("layers-light", "light")):
        d = HERE / folder
        d.mkdir(exist_ok=True)
        for name, text in layer_files(APPEARANCES[app]).items():
            (d / name).write_text(text)
    prev = HERE / "previews"
    prev.mkdir(exist_ok=True)
    for app, pal in APPEARANCES.items():
        (prev / f"{app}.svg").write_text(composite(layer_files(pal), 0, app))
    for name, text in marks().items():
        (HERE / name).write_text(text)


def main(argv):
    if argv[:1] == ["icns"] and len(argv) == 2 and argv[1] in ("default", "debug"):
        sys.stdout.write(composite(on_disk(argv[1]), 100, argv[1]))
    elif not argv:
        write()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
