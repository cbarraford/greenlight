"""Tests for scripts/icons.py: Greenlight's icon set (brand/icons) and its generated JS and Swift."""
import shutil

import pytest

import icons
from conftest import ROOT

STROKE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
          'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">')


@pytest.fixture
def repo(tmp_path):
    """A scratch copy of the icon sources and everything that uses them."""
    for rel in [*icons.SETS, "app/Sources"]:
        shutil.copytree(ROOT / rel, tmp_path / rel)
    for rel in [*icons.PAGES, "brand/DESIGN.md"]:
        shutil.copy(ROOT / rel, tmp_path / rel)
    return tmp_path


def _svg(tmp_path, name, body, head=STROKE):
    p = tmp_path / f"{name}.svg"
    p.write_text(f"{head}{body}</svg>")
    return p


def test_parse_good_stroked_and_filled(tmp_path):
    name, rec, errs = icons.parse_icon(_svg(tmp_path, "tick", '<path d="M3 8l3 3 7-7"/>'), 16)
    assert (name, errs) == ("tick", []) and rec == [16, "s", [["path", {"d": "M3 8l3 3 7-7"}]]]
    head = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" stroke="none">'
    _, rec, errs = icons.parse_icon(_svg(tmp_path, "dot-fill", '<circle cx="8" cy="8" r="3"/>', head), 16)
    assert errs == [] and rec[1] == "f"


@pytest.mark.parametrize("name, head, body, why", [
    ("Bad_Name", STROKE, '<path d="M1 1"/>', "lowercase words"),
    ("thick", STROKE.replace('"1.5"', '"2"'), '<path d="M1 1"/>', "must carry exactly"),
    ("big", STROKE.replace("0 0 16 16", "0 0 24 24"), '<path d="M1 1"/>', "must carry exactly"),
    ("moved", STROKE, '<path d="M1 1" transform="rotate(4)"/>', "disallowed attributes"),
    ("styled", STROKE, '<path d="M1 1" style="stroke:red"/>', "disallowed attributes"),
    ("red", STROKE, '<path d="M1 1" stroke="#f00"/>', "currentColor or none"),
    ("text", STROKE, "<text>x</text>", "isn't allowed"),
    ("group", STROKE, '<g><path d="M1 1"/></g>', "isn't allowed"),
    ("empty", STROKE, "", "draws nothing"),
])
def test_parse_rejects(tmp_path, name, head, body, why):
    _, _, errs = icons.parse_icon(_svg(tmp_path, name, body, head), 16)
    assert any(why in e for e in errs), errs


def test_parse_rejects_broken_xml(tmp_path):
    p = tmp_path / "broken.svg"
    p.write_text("<svg")
    assert "not valid SVG" in icons.parse_icon(p, 16)[2][0]


def test_load_repo_set():
    found, errs = icons.load(ROOT)
    assert errs == []
    assert {"question", "working", "crashed", "done", "claude", "openai"} | icons.reserved() <= set(found)
    assert found["claude"][0] == 24 and found["claude"][1] == "f"


def test_load_flags_duplicates(repo):
    shutil.copy(repo / "brand/icons/ui/check.svg", repo / "brand/icons/marks/check.svg")
    (repo / "brand/icons/marks/check.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M1 1"/></svg>')
    assert any("defined twice" in e for e in icons.load(repo)[1])


def test_js_block_and_apply(repo):
    found, _ = icons.load(repo)
    block = icons.js_block(found)
    assert block.startswith(icons.OPEN) and block.endswith(icons.CLOSE)
    assert "function dsIcon(" in block and "function dsIconHTML(" in block and "LICENSE.lobe-icons" in block
    page = "<head><script>let a</script>"
    once = icons.apply_block(page, block)
    assert once.index(icons.OPEN) < once.index("<script>let a")
    assert icons.apply_block(once, block) == once
    broken = once.replace(icons.CLOSE, "</script>")
    with pytest.raises(ValueError, match="end marker"):
        icons.apply_block(broken, block)
    with pytest.raises(ValueError, match="more than one"):
        icons.apply_block(once + block, block)


def test_check_flags_a_lost_end_marker(repo):
    page = repo / "app/terminal.html"
    page.write_text(page.read_text().replace(icons.CLOSE, "</script>"))
    assert any("end marker" in e for e in icons.run_check(repo))


def test_reserved_status_glyphs(repo):
    assert {"needs", "your-move", "queued", "waiting", "done"} <= icons.reserved(repo)
    _svg(repo / "brand/icons/ui", "reserved-x", '<path d="M1 1"/>')
    found = icons.load(repo)[0]
    assert any("'reserved-x' is never used" in e for e in icons.check_usage(found, repo))
    spec = repo / "brand/DESIGN.md"
    text = spec.read_text()
    status = "  status:"
    assert status in text
    spec.write_text(text.replace(status, f"{status}\n    extra:    {{ hue: neutral, glyph: reserved-x }}", 1)
                    + "\nProse after the front matter, e.g. `glyph: not-a-status`, is not the spec.\n")
    errs = icons.check_usage(found, repo)
    assert not any("reserved-x" in e or "not-a-status" in e for e in errs), errs
    spec.write_text(spec.read_text().replace("glyph: reserved-x", "glyph: no-such-glyph"))
    assert any("status glyph 'no-such-glyph' has no icon" in e for e in icons.check_usage(found, repo))


def test_swift_source(repo):
    src = icons.swift_source(icons.load(repo)[0])
    assert 'static let all: [String: (Int, Bool, String)]' in src
    assert '"check": (16, true, "<path d=\\"' in src
    assert "currentColor" not in src and "extension Image" in src and "init(ds name: String" in src


def test_repo_is_clean():
    assert icons.run_check(ROOT) == []


def test_usage_errors(repo):
    page = repo / "app/terminal.html"
    page.write_text(page.read_text() + '\n<script>ico("no-such-icon")</script>')
    swift = repo / "app/Sources/Views.swift"
    swift.write_text(swift.read_text() + '\nlet x = Image(ds: "also-missing")\n')
    _svg(repo / "brand/icons/ui", "orphan", '<path d="M1 1"/>')
    errs = icons.check_usage(icons.load(repo)[0], repo)
    assert any("no icon named 'no-such-icon'" in e for e in errs)
    assert any("no icon named 'also-missing'" in e for e in errs)
    assert any("'orphan' is never used" in e for e in errs)


@pytest.mark.parametrize("code", [
    'Image(ds: on ? "mic" : "mic-of", size: 12)',      # either arm of a ternary
    'dsIcon(on ? "bell-fill" : "bel", 14)',
    'dsIcon(`nope`, 14)',                               # a template literal
    'banner("nope", "text")',                           # Swift helpers that take an icon name
    'listItem("t", "m", on: true, icon: "nope")',
    'pathRow("nope", "text") { EmptyView() }',
    'knob("nope", (help: "h", label: "l")) {}',
    'const MI = {review: "review", ci: "nope"};',       # the pages' name maps
    'const G = {ok: "done", q: "nope"};',
    'const GLYPH = {idle: "nope"};',
    'icon(t ? "a\\",b" : "nope", 14)',                  # an escaped quote doesn't end the string
])
def test_usage_follows_ternaries_helpers_and_maps(repo, code):
    swift = repo / "app/Sources/Views.swift"
    swift.write_text(swift.read_text() + f"\n{code}\n")
    errs = icons.check_usage(icons.load(repo)[0], repo)
    assert any(e.startswith("app/Sources/Views.swift:") and "no icon named" in e for e in errs), errs


def test_named_skips_non_names():
    assert [n for _, n in icons.named('SettingsView.icon("General") x.ico("nope")')] == []  # methods, not icon calls
    assert [n for _, n in icons.named('icon(x ? "a b" : `${t}`, 14) icon(k)')] == []
    assert [n for _, n in icons.named('icon(f("x", y) ? "done" : "ring", 14)')] == ["x", "done", "ring"]
    assert [n for _, n in icons.named('icon("a` + \'b", 14) const G = {a: `zz-top`}')] == ["zz-top"]  # pairs must match


def test_check_stale_generate_and_question_fallback(repo):
    (repo / icons.SWIFT).unlink()
    assert any("Icons.swift is out of date" in e for e in icons.run_check(repo))
    assert icons.generate(repo) == [icons.SWIFT]
    assert icons.generate(repo) == [] and icons.run_check(repo) == []
    (repo / "brand/icons/ui/question.svg").unlink()
    assert any("fallback" in e for e in icons.run_check(repo))


def test_generate_refuses_bad_icons(repo):
    _svg(repo / "brand/icons/ui", "bad", '<path d="M1 1" style="x"/>')
    with pytest.raises(ValueError):
        icons.generate(repo)


def test_sheet():
    out = icons.sheet(icons.load(ROOT)[0])
    assert out.count("<figure>") == 2 * len(icons.load(ROOT)[0])
    assert 'class="light"' in out and 'class="dark"' in out and 'stroke-width="1.5"' in out


def test_main(repo, tmp_path, capsys):
    assert icons.main(["check", "--root", str(repo)]) == 0
    (repo / icons.SWIFT).write_text("// stale\n")
    assert icons.main(["check", "--root", str(repo)]) == 1
    assert "ICONS FAIL" in capsys.readouterr().err
    assert icons.main(["generate", "--root", str(repo)]) == 0
    assert "wrote app/Sources/Icons.swift" in capsys.readouterr().out
    assert icons.main(["generate", "--root", str(repo)]) == 0
    assert "already up to date" in capsys.readouterr().out
    out = tmp_path / "sheet.html"
    assert icons.main(["sheet", str(out), "--root", str(repo)]) == 0
    assert out.read_text().startswith("<!doctype html>")
