#!/usr/bin/env python3
"""Greenlight app icon concepts (brand/DESIGN.md §8.3): ten directions drawn as flat, layered SVG.

    python3 brand/icon/concepts/generate.py        # rewrite every NN-name/{layers/,preview.svg,mark.svg}

Each concept is a list of foreground layers (back to front) built from a handful of primitives, plus the shared
background. Every shape has a role (dim, dim2, mid, light, ground); the appearance palettes below turn roles into
colours, so default, dark, light and mono/tinted/clear are the same geometry. Layers are square 1024 canvases, unmasked:
Icon Composer applies the squircle and the glass. preview.svg composites them inside an approximate squircle for review
only. mark.svg is the foreground alone on a 16 grid in currentColor, with role opacity standing in for brightness, so
it works as a menu bar template image.
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "scripts"))
import tokens  # noqa: E402

S = 1024
C = 512


def ok(L, Ch, H):
    """An OKLCH colour as sRGB hex, chroma lowered (never clipped) to fit the gamut."""
    return tokens.oklch_hex((L, tokens.gamut_map(L, Ch, H), H))


# Appearances. bg is (top, bottom) of the background's vertical gradient; the other roles are (colour, opacity).
APPEARANCES = {
    "default": {
        "bg": (ok(0.285, 0.034, 155), ok(0.205, 0.028, 155)),
        "ground": (ok(0.165, 0.022, 155), 1), "dim": (ok(0.375, 0.032, 155), 1),
        "dim2": (ok(0.47, 0.04, 155), 1), "deep": (ok(0.43, 0.08, 152), 1), "mid": (ok(0.66, 0.15, 150), 1), "light": (ok(0.85, 0.19, 150), 1),
    },
    "dark": {
        "bg": (ok(0.19, 0.022, 155), ok(0.13, 0.016, 155)),
        "ground": (ok(0.10, 0.012, 155), 1), "dim": (ok(0.30, 0.026, 155), 1),
        "dim2": (ok(0.39, 0.032, 155), 1), "deep": (ok(0.36, 0.07, 152), 1), "mid": (ok(0.62, 0.15, 150), 1), "light": (ok(0.83, 0.19, 150), 1),
    },
    "light": {
        "bg": (ok(0.975, 0.012, 155), ok(0.925, 0.016, 155)),
        "ground": (ok(0.86, 0.02, 155), 1), "dim": (ok(0.84, 0.022, 155), 1),
        "dim2": (ok(0.76, 0.03, 155), 1), "deep": (ok(0.80, 0.06, 152), 1), "mid": (ok(0.72, 0.12, 150), 1), "light": (ok(0.62, 0.17, 150), 1),
    },
    # Mono (tinted dark): the system tints a greyscale image, so the light must be the near-white part.
    "mono": {
        "bg": ("#2A2A2C", "#19191B"),
        "ground": ("#000000", 0.35), "dim": ("#FFFFFF", 0.22), "dim2": ("#FFFFFF", 0.36), "deep": ("#FFFFFF", 0.36),
        "mid": ("#FFFFFF", 0.66), "light": ("#FFFFFF", 1),
    },
    # Clear: a translucent ground over the wallpaper, the foreground in white.
    "clear": {
        "bg": ("rgba(255,255,255,0.20)", "rgba(255,255,255,0.10)"),
        "ground": ("#FFFFFF", 0.10), "dim": ("#FFFFFF", 0.26), "dim2": ("#FFFFFF", 0.40), "deep": ("#FFFFFF", 0.40),
        "mid": ("#FFFFFF", 0.70), "light": ("#FFFFFF", 1),
    },
}
# Template opacity per role in mark.svg (the menu bar renders alpha only).
MARK_ALPHA = {"ground": 0.35, "dim": 0.4, "dim2": 0.5, "deep": 0.5, "mid": 0.7, "light": 1}
ROLE_ORDER = ("ground", "dim", "dim2", "deep", "mid", "light")


# ---------------------------------------------------------------- primitives

def circle(x, y, r, role):
    return {"t": "circle", "x": x, "y": y, "r": r, "role": role}


def rect(x, y, w, h, rx, role):
    return {"t": "rect", "x": x, "y": y, "w": w, "h": h, "rx": rx, "role": role}


def path(cmds, role, stroke=None):
    """cmds: ("M", x, y) ("L", x, y) ("C", x1, y1, x2, y2, x, y) ("A", r, large, sweep, x, y) ("Z",)."""
    return {"t": "path", "d": cmds, "role": role, "stroke": stroke}


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def poly(pts, role):
    return path([("M", *pts[0])] + [("L", *p) for p in pts[1:]] + [("Z",)], role)


def rounded_poly(pts, rad, role):
    """A convex polygon with each corner rounded by an arc of radius rad."""
    n, cmds = len(pts), []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1 = _unit(p0[0] - p1[0], p0[1] - p1[1])
        v2 = _unit(p2[0] - p1[0], p2[1] - p1[1])
        ang = math.acos(max(-1, min(1, v1[0] * v2[0] + v1[1] * v2[1])))
        t = rad / math.tan(ang / 2)
        a = (p1[0] + v1[0] * t, p1[1] + v1[1] * t)
        b = (p1[0] + v2[0] * t, p1[1] + v2[1] * t)
        cmds.append(("M" if i == 0 else "L", *a))
        cmds.append(("A", rad, 0, 1, *b))
    cmds.append(("Z",))
    return path(cmds, role)


def ring_arc(cx, cy, r_out, r_in, a0, a1, role, caps="butt"):
    """A thick arc from angle a0 to a1 (degrees, clockwise on screen), butt or round ends."""
    span = (a1 - a0) % 360 or 360
    large = 1 if span > 180 else 0
    o0, o1 = polar(cx, cy, r_out, a0), polar(cx, cy, r_out, a1)
    i0, i1 = polar(cx, cy, r_in, a0), polar(cx, cy, r_in, a1)
    if caps == "round":
        w = (r_out - r_in) / 2
        mid = (r_out + r_in) / 2
        m0, m1 = polar(cx, cy, mid, a0), polar(cx, cy, mid, a1)
        return path([("M", *m0), ("A", mid, large, 1, *m1)], role, stroke=(2 * w, "round"))
    return path([("M", *o0), ("A", r_out, large, 1, *o1), ("L", *i1), ("A", r_in, large, 0, *i0), ("Z",)], role)


def _unit(x, y):
    d = math.hypot(x, y)
    return x / d, y / d


def _intersect(p, d, q, e):
    """The point where line p + t·d meets line q + s·e."""
    den = d[0] * e[1] - d[1] * e[0]
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return p[0] + t * d[0], p[1] + t * d[1]


def _to_circle(p, d, r):
    """The forward hit of ray p + t·d on the circle of radius r about the centre."""
    px, py = p[0] - C, p[1] - C
    b = px * d[0] + py * d[1]
    t = -b + math.sqrt(b * b - (px * px + py * py - r * r))
    return p[0] + t * d[0], p[1] + t * d[1]


def hexagon(cx, cy, r, rad, role):
    """A pointy-top hexagon (the board's cell), corners rounded."""
    return rounded_poly([polar(cx, cy, r, -90 + 60 * k) for k in range(6)], rad, role)


# ---------------------------------------------------------------- the ten concepts

def aperture(R=372, r=196, gap=24):
    """Six blades around a pointy-top hexagonal opening; the light fills the opening exactly (no overlap, so
    translucent mono and clear blades never show it through)."""
    verts = [polar(C, C, r, -90 + 60 * k) for k in range(6)]
    dirs = [_unit(verts[(k + 1) % 6][0] - verts[k][0], verts[(k + 1) % 6][1] - verts[k][1]) for k in range(6)]
    blades = []
    for k in range(6):
        v1, v2 = verts[(k + 1) % 6], verts[(k + 2) % 6]
        d0, d1 = dirs[k], dirs[(k + 1) % 6]
        inside = ((v1[0] + v2[0]) / 2 + (d0[0] + d1[0]) * 90 - C, (v1[1] + v2[1]) / 2 + (d0[1] + d1[1]) * 90 - C)
        p0 = _shift(v1, d0, gap / 2, inside)
        p1 = _shift(v1, d1, gap / 2, inside)
        a = _intersect(p0, d0, p1, d1)
        b = _to_circle(a, d0, R)
        c = _to_circle(a, d1, R)
        sweep = 1 if _angle_between(b, c) > 0 else 0
        blades.append(path([("M", *a), ("L", *b), ("A", R, 0, sweep, *c), ("Z",)], "deep"))
    opening = (r * math.cos(math.pi / 6) + gap / 2 + 1) / math.cos(math.pi / 6)
    light = poly([polar(C, C, opening, -90 + 60 * k) for k in range(6)], "light")
    return [("light", [light]), ("blades", blades)]


def _shift(p, d, amount, toward):
    """Move the line through p along d by amount, to the side the vector `toward` (from the centre) is on."""
    n = (-d[1], d[0])
    probe = (toward[0] + C - p[0], toward[1] + C - p[1])
    s = 1 if n[0] * probe[0] + n[1] * probe[1] > 0 else -1
    return p[0] + n[0] * amount * s, p[1] + n[1] * amount * s


def _angle_between(b, c):
    a0 = math.atan2(b[1] - C, b[0] - C)
    a1 = math.atan2(c[1] - C, c[0] - C)
    return (math.degrees(a1 - a0) + 540) % 360 - 180


def slit():
    """The tile split by one band of light that widens as it crosses, like a door swinging open."""
    beam = poly([(-40, C - 12), (S + 40, C - 156), (S + 40, C + 156), (-40, C + 12)], "light")
    return [("band", [beam])]


def lit_cell(r=124, gap=26):
    """The board's hexagon lit from inside, among six faint siblings."""
    step = math.sqrt(3) * r + gap
    sib = [hexagon(*polar(C, C, step, 30 + 60 * k), r, 22, "dim") for k in range(6)]
    return [("siblings", sib), ("cell", [hexagon(C, C, r, 22, "light")])]


def lane_clear(w=100, lit=148):
    """Two lanes converge and resolve into one lit lane heading on: merge, then go. It runs left to right, the
    reading direction for "forward", and never stands up as a figure."""
    arms = [path([("M", 196, C + sy * 236), ("C", 360, C + sy * 236, 380, C, 540, C)], "dim2",
                 stroke=(w, "round")) for sy in (-1, 1)]
    lane = path([("M", 500, C), ("L", 826, C)], "light", stroke=(lit, "round"))
    return [("lanes", arms), ("lit lane", [lane])]


def beacon():
    """One point of light, its signal sent in two arcs on one side only."""
    cx = 382
    arcs = [ring_arc(cx, C, 250, 186, -48, 48, "mid", caps="round"),
            ring_arc(cx, C, 372, 308, -44, 44, "dim2", caps="round")]
    return [("arcs", arcs), ("light", [circle(cx, C, 116, "light")])]


def g_lamp(R=356, w=112):
    """A geometric G whose open counter holds the light."""
    g = [ring_arc(C, C, R, R - w, 0, -52, "dim2"),
         rect(C + R - w - 96, C, w + 96, w, 0, "dim2")]
    return [("G", g), ("light", [circle(C, C, 120, "light")])]


def signal_column(r=104, step=252):
    """Three dots in a column, only the top one lit. Risk: a traffic light."""
    dots = [circle(C, C, r, "dim"), circle(C, C + step, r, "dim")]
    return [("dots", dots), ("light", [circle(C, C - step, r, "light")])]


def constellation(r=122):
    """Agents as small dark nodes in a flock, one bright node out in front: cleared, and the rest follow."""
    pts = [(0, 0, r, "light")]
    for ang in (162, 108):
        for dist, rr in ((236, 66), (392, 54), (524, 44)):
            pts.append((*polar(0, 0, dist, ang), rr, "dim2"))
    x0, x1 = min(x - q for x, _, q, _ in pts), max(x + q for x, _, q, _ in pts)
    y0, y1 = min(y - q for _, y, q, _ in pts), max(y + q for _, y, q, _ in pts)
    dx, dy = C - (x0 + x1) / 2, C - (y0 + y1) / 2
    shapes = [circle(x + dx, y + dy, q, role) for x, y, q, role in pts]
    return [("nodes", shapes[1:]), ("light", shapes[:1])]


def horizon():
    """A light rising over a flat horizon: dawn as go."""
    sun = path([("M", C - 236, 640), ("A", 236, 0, 1, C + 236, 640), ("Z",)], "light")
    return [("light", [sun]), ("ground", [rect(-40, 640, S + 80, 424, 0, "ground")])]


def switch():
    """A toggle thumb moved to on, made of light. Risk: a settings switch."""
    track = rect(168, 340, 688, 344, 172, "dim")
    return [("track", [track]), ("thumb", [circle(684, C, 136, "light")])]


CONCEPTS = [
    ("01-aperture", "Aperture", aperture, (128, 128, 768)),
    ("02-slit", "Slit of light", slit, None),
    ("03-lit-cell", "The lit cell", lit_cell, (118, 118, 788)),
    ("04-lane-clear", "Lane clear", lane_clear, (136, 136, 752)),
    ("05-beacon", "Beacon", beacon, (158, 152, 720)),
    ("06-g-lamp", "G-lamp", g_lamp, (144, 144, 736)),
    ("07-signal-column", "Signal column", signal_column, (128, 128, 768)),
    ("08-constellation", "Constellation", constellation, (156, 156, 712)),
    ("09-horizon", "Horizon", horizon, None),
    ("10-switch", "Switch", switch, (152, 152, 720)),
]


def slit_mark():
    return [("band", [poly([(0.75, 7.25), (15.25, 2.75), (15.25, 13.25), (0.75, 8.75)], "light")])]


def horizon_mark():
    return [("light", [path([("M", 2.2, 10), ("A", 5.8, 0, 1, 13.8, 10), ("Z",)], "light")]),
            ("ground", [rect(0.5, 11.25, 15, 2.5, 1.25, "dim")])]


CUSTOM_MARKS = {"02-slit": slit_mark, "09-horizon": horizon_mark}


# ---------------------------------------------------------------- rendering

def fmt(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def _xf(x, y, xf):
    s, ox, oy = xf
    return (x - ox) * s, (y - oy) * s


def shape_svg(sh, fill, xf=(1, 0, 0)):
    s = xf[0]
    if sh["t"] == "circle":
        x, y = _xf(sh["x"], sh["y"], xf)
        return f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="{fmt(sh["r"] * s)}" fill="{fill}"/>'
    if sh["t"] == "rect":
        x, y = _xf(sh["x"], sh["y"], xf)
        rx = f' rx="{fmt(sh["rx"] * s)}"' if sh["rx"] else ""
        return (f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(sh["w"] * s)}" height="{fmt(sh["h"] * s)}"{rx}'
                f' fill="{fill}"/>')
    parts = []
    for cmd in sh["d"]:
        op = cmd[0]
        if op in "ML":
            parts.append(op + " ".join(fmt(v) for v in _xf(cmd[1], cmd[2], xf)))
        elif op == "C":
            pts = [_xf(cmd[i], cmd[i + 1], xf) for i in (1, 3, 5)]
            parts.append("C" + " ".join(fmt(v) for p in pts for v in p))
        elif op == "A":
            x, y = _xf(cmd[4], cmd[5], xf)
            r = fmt(cmd[1] * s)
            parts.append(f"A{r} {r} 0 {cmd[2]} {cmd[3]} {fmt(x)} {fmt(y)}")
        else:
            parts.append("Z")
    d = "".join(parts)
    if sh["stroke"]:
        w, cap = sh["stroke"]
        return (f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{fmt(w * s)}" stroke-linecap="{cap}"'
                f' stroke-linejoin="round"/>')
    return f'<path d="{d}" fill="{fill}"/>'


def layer_body(shapes, pal, xf=(1, 0, 0)):
    """One layer's shapes, grouped by role so a translucent role never doubles where its own shapes overlap."""
    out = []
    for role in ROLE_ORDER:
        group = [sh for sh in shapes if sh["role"] == role]
        if not group:
            continue
        colour, alpha = pal[role]
        inner = "".join(shape_svg(sh, colour, xf) for sh in group)
        out.append(inner if alpha == 1 else f'<g opacity="{alpha}">{inner}</g>')
    return "".join(out)


def background(pal, gid="bg"):
    top, bot = pal["bg"]
    return (f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/>'
            f'<stop offset="1" stop-color="{bot}"/></linearGradient></defs>'
            f'<rect width="{S}" height="{S}" fill="url(#{gid})"/>')


def squircle(n=5.0, steps=360):
    """A superellipse close to the system's continuous-corner squircle (preview only: Icon Composer owns the shape)."""
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        x = C + C * math.copysign(abs(ct) ** (2 / n), ct)
        y = C + C * math.copysign(abs(st) ** (2 / n), st)
        pts.append(f"{fmt(x)} {fmt(y)}")
    return "M" + "L".join(pts) + "Z"


SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {0}" width="{0}" height="{0}">'


def preview(layers, appearance, uid):
    pal = APPEARANCES[appearance]
    body = "".join(layer_body(shapes, pal) for _, shapes in layers)
    return (SVG_OPEN.format(S) + f'<defs><clipPath id="sq-{uid}"><path d="{squircle()}"/></clipPath></defs>'
            f'<g clip-path="url(#sq-{uid})">{background(pal, "bg-" + uid)}{body}</g></svg>\n')


def mark(slug, layers, box):
    if slug in CUSTOM_MARKS:
        mlayers, xf = CUSTOM_MARKS[slug](), (1, 0, 0)
    else:
        mlayers, xf = layers, (16 / box[2], box[0], box[1])
    out = []
    for _, shapes in mlayers:
        for role in ROLE_ORDER:
            group = [sh for sh in shapes if sh["role"] == role]
            if group:
                inner = "".join(shape_svg(sh, "currentColor", xf) for sh in group)
                a = MARK_ALPHA[role]
                out.append(inner if a == 1 else f'<g opacity="{a}">{inner}</g>')
    return SVG_OPEN.format(16) + "".join(out) + "</svg>\n"


def write(slug, fn, box):
    d = HERE / slug
    (d / "layers").mkdir(parents=True, exist_ok=True)
    for old in (d / "layers").glob("*.svg"):
        old.unlink()
    layers = fn()
    pal = APPEARANCES["default"]
    (d / "layers" / "01-background.svg").write_text(SVG_OPEN.format(S) + background(pal) + "</svg>\n")
    for i, (name, shapes) in enumerate(layers, start=2):
        fname = f"{i:02d}-{name.replace(' ', '-')}.svg"
        (d / "layers" / fname).write_text(SVG_OPEN.format(S) + layer_body(shapes, pal) + "</svg>\n")
    (d / "preview.svg").write_text(preview(layers, "default", slug[:2]))
    (d / "mark.svg").write_text(mark(slug, layers, box))
    apps = d / "appearances"
    apps.mkdir(exist_ok=True)
    for app in APPEARANCES:
        (apps / f"{app}.svg").write_text(preview(layers, app, f"{slug[:2]}{app[:2]}"))
    assert len(layers) + 1 <= 4, f"{slug}: at most 4 layer groups"


def main():
    for slug, _name, fn, box in CONCEPTS:
        write(slug, fn, box)
    print(f"wrote {len(CONCEPTS)} concepts to {HERE}")


if __name__ == "__main__":
    main()
