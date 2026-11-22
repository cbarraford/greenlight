"""Pytest unit tests for scripts/js_lint.py."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import js_lint  # noqa: E402


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "tests").mkdir(parents=True)
    for name in ("dashboard.html", "terminal.html", "stage.html"):
        (tmp_path / "app" / name).write_text(
            '<!doctype html><html><body><div id="root"></div>\n'
            '<script>\n'
            'const el = document.getElementById("root");\n'
            'if (typeof el === "object" && !Number.isNaN(1)) { el.textContent = "ok"; }\n'
            '</script></body></html>\n',
            encoding="utf-8",
        )
    (tmp_path / "tests" / "test_sample.cjs").write_text(
        'const cfg = { a: 1, b: 2 };\nconsole.log(cfg.a);\n',
        encoding="utf-8",
    )
    return tmp_path


def test_clean_synthetic_repo_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert js_lint.lint_repo(repo) == []
    assert js_lint.main(["--root", str(repo), "--strict"]) == 0


def test_real_repo_passes_with_zero_errors() -> None:
    findings = js_lint.lint_repo(ROOT)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], [f.format() for f in errors]
    assert js_lint.main(["--root", str(ROOT)]) == 0


@pytest.mark.skipif(not shutil.which("node"), reason="node binary required")
def test_syntax_errors_in_html_script_module_and_cjs(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "dashboard.html").write_text(
        '<!doctype html><script src="external.js"></script>\n'
        '<script></script>\n'
        '<script type="module">\n'
        'export const good = 1;\n'
        'function broken( {\n'
        '</script>\n',
        encoding="utf-8",
    )
    (repo / "tests" / "test_sample.cjs").write_text("const = ;\n", encoding="utf-8")
    findings = js_lint.lint_repo(repo)
    codes = [(f.path, f.code, f.line) for f in findings]
    assert any(p == "app/dashboard.html" and c == "JS001" and ln >= 5 for p, c, ln in codes), codes
    assert any(p == "tests/test_sample.cjs" and c == "JS001" for p, c, _ in codes), codes
    assert js_lint.main(["--root", str(repo)]) == 1


def test_disallowed_constructs_and_suppression(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "stage.html").write_text(
        '<!doctype html><html><body><div id="s"></div>\n'
        '<script>\n'
        'debugger;\n'
        'eval("1+1");\n'
        'const fn = new Function("return 1");\n'
        'document.write("hi");\n'
        'alert("boom");\n'
        'confirm("sure?");\n'
        'window.prompt("name?");\n'
        'const safe = { prompt: () => 1 }; safe.prompt();\n'
        '// alert("commented out") eval("nope") debugger\n'
        'const txt = "debugger eval(1) document.write(2)";\n'
        'eval("suppressed"); // lint-ok\n'
        '</script></body></html>\n',
        encoding="utf-8",
    )
    findings = js_lint.lint_repo(repo)
    codes = [f.code for f in findings]
    assert codes == ["JS101", "JS102", "JS103", "JS104", "JS105", "JS105", "JS105"], [f.format() for f in findings]
    lines = [f.line for f in findings]
    assert lines == [3, 4, 5, 6, 7, 8, 9]


def test_typeof_and_nan_comparisons(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "dashboard.html").write_text(
        '<!doctype html><script>\n'
        'const a = typeof x === "undef";\n'
        'const b = "str" !== typeof y;\n'
        'const ok1 = typeof x === "undefined";\n'
        'const ok2 = typeof (a ? b : c) === "string";\n'
        'const c = x === NaN;\n'
        'const d = NaN != y;\n'
        'const ok3 = Number.isNaN(x);\n'
        '</script>\n',
        encoding="utf-8",
    )
    findings = js_lint.lint_repo(repo)
    assert [(f.line, f.code) for f in findings] == [
        (2, "JS201"),
        (3, "JS201"),
        (6, "JS202"),
        (7, "JS202"),
    ], [f.format() for f in findings]


def test_cond_assign_detection_and_exclusions(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "terminal.html").write_text(
        '<!doctype html><script>\n'
        'if (x = 1) { }\n'
        'while (y = next()) { }\n'
        'if ((z = next())) { }\n'
        'if (arr.some(v => v.id === 1)) { }\n'
        'if (a === "k=v" && b <= 2 && c >= 0) { }\n'
        'if (fn({ a: 1, b: [2] })) { }\n'
        '</script>\n',
        encoding="utf-8",
    )
    findings = js_lint.lint_repo(repo)
    assert [(f.line, f.code) for f in findings] == [(2, "JS204"), (3, "JS204")], [f.format() for f in findings]


def test_duplicate_object_keys_detection(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "dashboard.html").write_text(
        '<!doctype html><script>\n'
        'const m = {\n'
        '  alpha: 1,\n'
        '  beta: 2,\n'
        '  alpha: 3,\n'
        '  "gamma": 4,\n'
        '  gamma: 5,\n'
        '  get val() { return 1; },\n'
        '  set val(v) { this._v = v; },\n'
        '  async run() {},\n'
        '  nested: { alpha: 9, list: [1, 2], fn: (x) => { const a = { style: `v(${x ? 1 : 2})` }; return h("b", a), h("span"); } },\n'
        '};\n'
        'const { a: x, b: y } = m;\n'
        'function block() { const z = 1; return z; }\n'
        '</script>\n',
        encoding="utf-8",
    )
    findings = js_lint.lint_repo(repo)
    assert [(f.line, f.code) for f in findings] == [(5, "JS203"), (7, "JS203")], [f.format() for f in findings]


def test_console_log_warning_vs_strict_and_suppression(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "dashboard.html").write_text(
        '<!doctype html><script>\n'
        'console.log("debug");\n'
        'console.log("intentional"); // lint-ok\n'
        '</script>\n',
        encoding="utf-8",
    )
    findings = js_lint.lint_repo(repo)
    assert len(findings) == 1
    assert findings[0].code == "JS205"
    assert findings[0].severity == "warning"
    assert "WARN JS205" in findings[0].format(strict=False)
    assert "WARN" not in findings[0].format(strict=True)
    assert js_lint.main(["--root", str(repo)]) == 0
    assert js_lint.main(["--root", str(repo), "--strict"]) == 1


def test_dom_get_element_by_id_checks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "app" / "dashboard.html").write_text(
        '<!doctype html><body><div id="static-box"></div>\n'
        '<script>\n'
        'const dyn = { id: "helper-box" };\n'
        'const d = document.createElement("div"); d.id = "assigned-box";\n'
        'd.setAttribute("id", "attr-box");\n'
        'const tpl = `<span id="tpl-box"></span>`;\n'
        'document.getElementById("static-box");\n'
        'document.getElementById("helper-box");\n'
        'document.getElementById("assigned-box");\n'
        'document.getElementById("attr-box");\n'
        'document.getElementById("tpl-box");\n'
        'document.getElementById(dyn.id);\n'
        'document.getElementById("missing-id");\n'
        'document.getElementById("suppressed-missing"); /* lint-ok */\n'
        '</script></body>\n',
        encoding="utf-8",
    )
    findings = js_lint.lint_repo(repo)
    assert [(f.line, f.code) for f in findings] == [(13, "JS206")], [f.format() for f in findings]


def test_tokenizer_handles_templates_regex_and_block_comments(tmp_path: Path) -> None:
    src = (
        '/* block\ncomment\n*/\n'
        'const re = /[a/b]+/gi;\n'
        'const ratio = 10 / 2;\n'
        'const s = `outer ${eval("bad")} mid ${`inner ${1}`} end`;\n'
        'const esc = "line1\\\nline2";\n'
    )
    toks = js_lint.tokenize_js(src, start_line=1)
    assert any(t.kind == "regex" and t.value == "/[a/b]+/gi" and t.line == 4 for t in toks)
    assert any(t.kind == "ident" and t.value == "eval" and t.line == 6 for t in toks)

    repo = _make_repo(tmp_path)
    (repo / "app" / "stage.html").write_text(f"<!doctype html><script>\n{src}</script>\n", encoding="utf-8")
    findings = js_lint.lint_repo(repo)
    assert [(f.line, f.code) for f in findings] == [(7, "JS102")], [f.format() for f in findings]


def test_cli_explicit_paths_and_missing_node_in_ci(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    single = repo / "app" / "stage.html"
    assert js_lint.main(["--root", str(repo), str(single)]) == 0
    assert js_lint.main(["--root", str(repo), str(repo / "tests" / "test_sample.cjs")]) == 0

    with mock.patch.object(js_lint.shutil, "which", return_value=None):
        with mock.patch.dict(js_lint.os.environ, {"CI": "true"}, clear=False):
            findings = js_lint.lint_repo(repo)
            assert any(f.code == "JS001" for f in findings)
        with mock.patch.dict(js_lint.os.environ, {"CI": ""}, clear=False):
            assert js_lint.lint_repo(repo) == []
