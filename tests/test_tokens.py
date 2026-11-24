"""Tests for scripts/tokens.py (design tokens from brand/DESIGN.md) and check_safety's drift ratchet."""
import json
import re
import shutil
from pathlib import Path

import pytest

import check_safety
import tokens
from conftest import ROOT


@pytest.fixture
def spec():
    return tokens.load(ROOT)


@pytest.fixture
def repo(tmp_path):
    """A scratch copy of everything tokens.py reads and writes."""
    for rel in (tokens.SPEC, *tokens.PAGES, tokens.SWIFT):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    return tmp_path


# ---- YAML subset ----

def test_front_matter_needs_fences():
    assert tokens.front_matter("---\na: 1\n---\nbody") == "a: 1"
    with pytest.raises(ValueError):
        tokens.front_matter("no front matter")


def test_strip_comment_respects_quotes():
    assert tokens.strip_comment('a: "#FFF"  # note') == 'a: "#FFF"'
    assert tokens.strip_comment("# whole line") == ""
    assert tokens.strip_comment("a: b#c") == "a: b#c"
    assert tokens.strip_comment("a: 'x # y'") == "a: 'x # y'"


def test_split_top_ignores_nested_and_quoted_commas():
    assert tokens.split_top('a, "b, c", [d, e], {f: g, h: i}') == ["a", '"b, c"', "[d, e]", "{f: g, h: i}"]


def test_scalar_types():
    assert tokens.scalar("12") == 12
    assert tokens.scalar("0.5") == 0.5
    assert tokens.scalar('"+0.006"') == "+0.006"
    assert tokens.scalar("'x'") == "x"
    assert tokens.scalar("neutral.2") == "neutral.2"
    assert tokens.scalar("[1, 2]") == [1, 2]
    assert tokens.scalar("{ a: 1, b: c }") == {"a": 1, "b": "c"}


def test_key_value():
    assert tokens.key_value('"0": none') == ("0", "none")
    assert tokens.key_value("fg.muted: neutral.12") == ("fg.muted", "neutral.12")
    assert tokens.key_value("block:") == ("block", None)
    with pytest.raises(ValueError):
        tokens.key_value("no colon here")


def test_parse_yaml_blocks_and_errors():
    text = "a:\n  b: 1  # c\n  c:\n    d: [x, y]\ne: { f: g }\n"
    assert tokens.parse_yaml(text) == {"a": {"b": 1, "c": {"d": ["x", "y"]}}, "e": {"f": "g"}}
    with pytest.raises(ValueError):
        tokens.parse_yaml("a: 1\n    b: 2\n")


def test_parse_matches_pyyaml_when_available():
    yaml = pytest.importorskip("yaml")
    text = tokens.front_matter((ROOT / tokens.SPEC).read_text())
    ours, theirs = tokens.parse_yaml(text), yaml.safe_load(text)

    def norm(v):  # PyYAML reads bare 0 keys and numbers the same way we do; compare as strings for safety
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return str(v)
    assert norm(ours) == norm(theirs)


def test_spec_shape(spec):
    assert set(spec["primitives"]) >= {"neutral", "signal", "amber", "yellow", "blue", "red"}
    for modes in spec["primitives"].values():
        assert len(modes["light"]) == len(modes["dark"]) == 12
    assert spec["semantic"]["status"]["needs"]["solid"] == "amber.9"


# ---- colour maths ----

def test_contrast_maths():
    assert tokens.wcag("#000000", "#FFFFFF") == pytest.approx(21, abs=0.01)
    assert tokens.wcag("#777777", "#777777") == pytest.approx(1)
    assert tokens.apca("#000000", "#FFFFFF") == pytest.approx(106, abs=1)
    assert tokens.apca("#FFFFFF", "#000000") == pytest.approx(-108, abs=1)
    assert tokens.apca("#808080", "#808080") == 0.0


def test_oklch_round_trip_and_gamut():
    assert tokens.oklch_hex((1.0, 0.0, 0)) == "#FFFFFF"
    assert tokens.oklch_hex((0.0, 0.0, 0)) == "#000000"
    assert not tokens.in_gamut(0.6, 0.4, 150)
    c = tokens.gamut_map(0.6, 0.4, 150)
    assert c < 0.4 and tokens.in_gamut(0.6, c, 150)
    assert tokens.gamut_map(0.5, 0.0, 0) == 0.0


def test_mix_hex_endpoints():
    assert tokens.mix_hex("#000000", "#FFFFFF", 1.0) == "#000000"
    assert tokens.mix_hex("#000000", "#FFFFFF", 0.0) == "#FFFFFF"


# ---- resolving ----

def test_ref_forms(spec):
    pal = tokens.palette(spec)
    assert tokens.ref("#abcdef", "light", pal) == "#ABCDEF"
    assert tokens.ref("white", "dark", pal) == "#FFFFFF"
    assert tokens.ref("ink", "dark", pal) == pal["neutral"]["light"][11]
    assert tokens.ref("neutral.2", "dark", pal) == pal["neutral"]["dark"][1]
    assert tokens.ref("accent.9", "light", pal) == pal["signal"]["light"][8]
    assert tokens.ref("accent.9", "light", pal, "teal") == pal["teal"]["light"][8]
    assert tokens.ref("accent.9", "light", pal, "graphite") == pal["neutral"]["light"][11]
    for bad in ("nope.3", "neutral.13", "neutral.x"):
        with pytest.raises(ValueError):
            tokens.ref(bad, "light", pal)


def test_resolve_and_increased(spec):
    base = tokens.resolve(spec)
    assert base["bg.canvas"]["dark"] == tokens.palette(spec)["neutral"]["dark"][0]
    assert "status.needs.glyph" not in base and "status.needs.solid" in base
    assert base["accent.on_solid"]["light"] == "#FFFFFF"
    assert tokens.resolve(spec, "graphite")["accent.on_solid"]["dark"] == tokens.palette(spec)["neutral"]["dark"][0]
    hc = tokens.increased(spec, base)
    assert set(hc) == {"fg.muted", "border.subtle", "border.default"}
    assert tokens.wcag(hc["fg.muted"]["light"], base["bg.surface"]["light"]) > \
        tokens.wcag(base["fg.muted"]["light"], base["bg.surface"]["light"])


def test_teal_is_gamut_mapped(spec):
    for mode in tokens.MODES:
        for L, C, H in tokens.scales(spec)["teal"][mode]:
            assert tokens.in_gamut(L, C, H)


# ---- output ----

def test_css_block(spec):
    block = tokens.css_block(spec, "app/dashboard.html")
    assert block.startswith(tokens.OPEN) and block.endswith(tokens.CLOSE)
    for needle in ("--ds-bg-canvas:var(--ds-neutral-1)", "@media (prefers-color-scheme: light)",
                   "@media (prefers-contrast: more)", ':root[data-accent="teal"]', ':root[data-tokens="v2"]',
                   "--fs-3:var(--ds-type-body-size)", "--ds-type-body:400 13px/16px", "--ds-radius-full:999px",
                   "--ds-duration-fast:150ms", "--ds-layout-hit-target-min:24px", "--ds-layout-measure:68ch"):
        assert needle in block, needle
    assert ':root[data-tokens="v2"]' not in tokens.css_block(spec, "app/terminal.html")


def test_aliases_point_at_real_tokens(spec):
    for page in tokens.ALIASES:
        block = tokens.css_block(spec, page)
        defined = set(re.findall(r"(--ds-[\w-]+):", block))
        for target in re.findall(r"var\((--ds-[\w-]+)\)", tokens.alias_rule(page)):
            assert target in defined, (page, target)


def test_every_referenced_token_is_defined(spec):
    for page in tokens.PAGES:
        block = tokens.css_block(spec, page)
        defined = set(re.findall(r"(--ds-[\w-]+):", block))
        missing = set(re.findall(r"var\((--ds-[\w-]+)\)", block)) - defined
        assert not missing, (page, sorted(missing))


def test_apply_block_inserts_then_replaces():
    html = "<head><style>a{}</style></head>"
    once = tokens.apply_block(html, tokens.OPEN + "x" + tokens.CLOSE)
    assert once.index(tokens.OPEN) < once.index("<style>a{}")
    twice = tokens.apply_block(once, tokens.OPEN + "y" + tokens.CLOSE)
    assert "x" + tokens.CLOSE not in twice and twice.count(tokens.OPEN) == 1


def test_swift_source(spec):
    src = tokens.swift_source(spec)
    for needle in ("struct DSColor: Sendable", "static let bgCanvas = DSColor(light: 0x", "enum AccentTeal",
                   "enum AccentGraphite", "static let body = DSType(size: 13, line: 16, weight: 400",
                   "static let s12: CGFloat = 12", "static let fast: Double = 0.15",
                   "static let out: (Double, Double, Double, Double) = (0.23, 1.0, 0.32, 1.0)",
                   "static let hitTargetMin: CGFloat = 24"):
        assert needle in src, needle
    assert tokens.camel("status.your_move.on_solid") == "statusYourMoveOnSolid"


# ---- checks ----

def test_repo_is_clean():
    assert tokens.run_check(ROOT) == []


def test_check_stale_and_generate(repo):
    page = repo / "app/stage.html"
    page.write_text(page.read_text().replace("--ds-bg-canvas", "--ds-bg-canvaz", 1))
    (repo / tokens.SWIFT).unlink()
    errs = tokens.run_check(repo)
    assert any("stage.html" in e for e in errs) and any("Tokens.swift" in e for e in errs)
    assert sorted(tokens.generate(repo)) == sorted(["app/stage.html", tokens.SWIFT])
    assert tokens.generate(repo) == []
    assert tokens.run_check(repo) == []


def test_check_gamut_flags_out_of_gamut(spec):
    spec["primitives"]["signal"]["light"][8] = "0.53 0.40 150"
    assert any("signal.light.9" in e for e in tokens.check_gamut(spec))


def test_check_scales_flags_weak_text(spec):
    spec["primitives"]["neutral"]["light"][10] = "0.80 0.009 155"
    assert any("neutral.light: step 11" in e for e in tokens.check_scales(spec))


def test_check_pairs_flags_low_contrast(spec):
    spec["semantic"]["fg"]["muted"] = "neutral.8"
    assert any("fg.muted on bg." in e for e in tokens.check_pairs(spec))


def test_check_accents(monkeypatch):
    assert tokens.check_accents() == []
    monkeypatch.setitem(tokens.ACCENT_HUES, "violet", 290)
    monkeypatch.setitem(tokens.ACCENT_HUES, "sky", 240)
    errs = tokens.check_accents()
    assert any("violet" in e for e in errs) and any("sky" in e for e in errs)


def test_check_hex_notes(spec):
    text = (ROOT / tokens.SPEC).read_text()
    assert tokens.check_hex_notes(text, spec) == []
    bad = text.replace("# light sRGB: #FAFEFB", "# light sRGB: #000000", 1)
    assert tokens.check_hex_notes(bad, spec)


def test_main(repo, capsys):
    assert tokens.main(["check", "--root", str(repo)]) == 0
    (repo / tokens.SWIFT).write_text("// stale\n")
    assert tokens.main(["check", "--root", str(repo)]) == 1
    assert "TOKENS FAIL" in capsys.readouterr().err
    assert tokens.main(["generate", "--root", str(repo)]) == 0
    assert "wrote app/Sources/Tokens.swift" in capsys.readouterr().out
    assert tokens.main(["generate", "--root", str(repo)]) == 0
    assert "already up to date" in capsys.readouterr().out


# ---- drift ratchet (check_safety) ----

def _drift_repo(tmp: Path):
    (tmp / "app/Sources").mkdir(parents=True)
    (tmp / "scripts").mkdir()
    (tmp / "app/page.html").write_text(
        f'{tokens.OPEN}:root{{--x:#123456}}{tokens.CLOSE}<style>a{{color:#fff;font-size:12px;border-radius:4px;'
        "box-shadow:0 1px 2px rgba(0,0,0,.2)}b{box-shadow:none;color:var(--x)}</style><a href='#add'></a>")
    (tmp / "app/Sources/View.swift").write_text('let a = hex("FF9F0A"); Text("x").font(.system(size: 12)).cornerRadius: 8')
    (tmp / "app/Sources/Config.swift").write_text('let theme = hex("123456")')
    return tmp


def test_drift_counts(tmp_path):
    counts = check_safety.drift_counts(_drift_repo(tmp_path))
    assert counts["app/page.html"] == {"colour": 3, "font_size": 1, "radius": 1, "shadow": 1}
    assert counts["app/Sources/View.swift"] == {"colour": 1, "font_size": 1, "radius": 1}
    assert "app/Sources/Config.swift" not in counts


def test_drift_ratchet(tmp_path):
    root = _drift_repo(tmp_path)
    assert check_safety.check_drift(root) == [] and check_safety.drift_fell(root) == []  # no baseline: skipped
    check_safety.update_drift(root)
    assert check_safety.check_drift(root) == []
    swift = root / "app/Sources/View.swift"
    swift.write_text(swift.read_text() + '\nlet b = hex("00FF00")')
    errs = check_safety.check_drift(root)
    assert len(errs) == 1 and "View.swift: 2 literal colour values (baseline 1)" in errs[0]
    swift.write_text("let nothing = 0")
    assert check_safety.check_drift(root) == []
    assert "app/Sources/View.swift colour" in check_safety.drift_fell(root)


def test_repo_drift_baseline_holds():
    assert check_safety.check_drift(ROOT) == []
    base = json.loads((ROOT / check_safety.DRIFT_BASELINE).read_text())
    assert set(base) == set(check_safety.drift_counts(ROOT))
