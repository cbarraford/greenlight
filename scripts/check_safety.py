#!/usr/bin/env python3
"""Static safety & security guardrails for Greenlight.

Run locally (`make lint`) and in CI to prevent AI-introduced regressions:
  1. No raw quoted string interpolation inside Swift `evaluateJavaScript(...)` (single- or multi-line).
  2. No stray `UserDefaults.standard` in Swift (must route through `ud` so `--uitest` stays isolated).
  3. No unapproved `shell=True` in Python scripts without `# noqa: S602`.
  4. Content-Security-Policy present in dashboard.html, terminal.html, and stage.html.
  5. `stageWeb` stays handler-less (no WKScriptMessageHandler on untrusted voice stage).
  6. No inline event handlers inside app/*.html (except avatar fallback).
  7. Valid JS syntax across inline `<script>` blocks in app/*.html and `tests/*.cjs` / `tests/*.js`.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def swift_files(root=ROOT):
    src_dir = Path(root) / "app" / "Sources"
    if src_dir.is_dir():
        files = sorted(src_dir.glob("*.swift"))
        if files:
            return files
    single = Path(root) / "app" / "Greenlight.swift"
    return [single] if single.is_file() else []


def check_swift_eval_js(root=ROOT):
    errs = []
    # Disallow quoted Swift string interpolation inside evaluateJavaScript(...), e.g.
    # evaluateJavaScript("mc.openPage(\"\(name)\")") or evaluateJavaScript("term.opens('\(f)')"),
    # across both single-line and multi-line calls.
    bad_quoted = re.compile(r'evaluateJavaScript\([^)]*(?:\\"|(?<!\\)\')\\\(', re.S)
    root = Path(root)
    for path in swift_files(root):
        lines = path.read_text().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.lstrip()
            if stripped.startswith("//") or "evaluateJavaScript(" not in line:
                i += 1
                continue
            start = i
            chunk = [line]
            depth = line.count("(") - line.count(")")
            while depth > 0 and i + 1 < len(lines) and len(chunk) < 12:
                i += 1
                chunk.append(lines[i])
                depth += lines[i].count("(") - lines[i].count(")")
            joined = "\n".join(chunk)
            if bad_quoted.search(joined) and "// safety-ok" not in joined:
                errs.append(f"{path.relative_to(root)}:{start + 1}: raw quoted interpolation in evaluateJavaScript (use callJS)")
            i += 1
    return errs


def check_swift_user_defaults(root=ROOT):
    errs = []
    root = Path(root)
    for path in swift_files(root):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("//") or "UserDefaults.standard" not in line:
                continue
            allowed = (
                (path.name == "Config.swift" and "let ud:" in line)
                or (path.name == "App.swift" and "removePersistentDomain" in line)
            )
            if not allowed:
                errs.append(f"{path.relative_to(root)}:{i}: stray UserDefaults.standard (use `ud` so --uitest stays isolated)")
    return errs


def check_stage_web_isolated(root=ROOT):
    errs = []
    root = Path(root)
    for path in swift_files(root):
        text = path.read_text()
        m = re.search(r"lazy var stageWeb\s*:\s*WKWebView\s*=\s*\{(.*?)\}\(\)", text, re.S)
        if m and "userContentController.add" in m.group(1):
            errs.append(f"{path.relative_to(root)}: stageWeb must not attach any WKScriptMessageHandler")
    return errs


def check_python_shell_true(root=ROOT):
    errs = []
    root = Path(root)
    py_files = sorted((root / "scripts").glob("*.py")) if (root / "scripts").is_dir() else []
    kokoro = root / "app" / "kokoro.py"
    if kokoro.is_file():
        py_files.append(kokoro)
    for path in py_files:
        if path.name == "check_safety.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bshell\s*=\s*True\b", line) and "noqa: S602" not in line:
                errs.append(f"{path.relative_to(root)}:{i}: unapproved shell=True (use argv list or document `# noqa: S602`)")
    return errs


def check_html_csp_and_handlers(root=ROOT):
    errs = []
    root = Path(root)
    inline_ev = re.compile(r'\bon(?:click|error|load|focus|blur|mouseover|mouseenter)\s*=\s*["\']', re.I)
    for name in ("dashboard.html", "terminal.html", "stage.html"):
        path = root / "app" / name
        if not path.is_file():
            continue
        text = path.read_text()
        if '<meta http-equiv="Content-Security-Policy"' not in text:
            errs.append(f"{path.relative_to(root)}: missing Content-Security-Policy meta tag")
        for i, line in enumerate(text.splitlines(), 1):
            if 'onerror="this.remove()"' in line and line.count("onerror=") == 1:
                continue
            if inline_ev.search(line):
                errs.append(f"{path.relative_to(root)}:{i}: inline HTML event handler detected")
    return errs


def check_js_syntax(root=ROOT):
    """Verify inline <script> blocks in app/*.html and JS test files parse cleanly with `node --check`."""
    node = shutil.which("node")
    if not node:
        return []
    errs = []
    root = Path(root)
    script_re = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
    with tempfile.TemporaryDirectory(prefix="gl-js-check-") as tmp:
        tmp_dir = Path(tmp)
        for name in ("dashboard.html", "terminal.html", "stage.html"):
            path = root / "app" / name
            if not path.is_file():
                continue
            text = path.read_text()
            for idx, m in enumerate(script_re.finditer(text), 1):
                attrs, body = m.group(1), m.group(2)
                if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
                    continue
                is_module = bool(re.search(r'\btype\s*=\s*["\']module["\']', attrs, re.I))
                line_offset = text[:m.start(2)].count("\n")
                # Pad leading newlines so node --check reports exact HTML line numbers
                probe = tmp_dir / f"{path.stem}_{idx}.{'mjs' if is_module else 'js'}"
                probe.write_text("\n" * line_offset + body)
                p = subprocess.run([node, "--check", str(probe)], capture_output=True, text=True, timeout=15)
                if p.returncode != 0:
                    detail = (p.stderr or p.stdout).strip().replace(str(probe), str(path.relative_to(root)))
                    errs.append(f"{path.relative_to(root)}: JS syntax error:\n{detail[:400]}")
        tests_dir = root / "tests"
        if tests_dir.is_dir():
            for js_path in sorted(list(tests_dir.glob("*.cjs")) + list(tests_dir.glob("*.js"))):
                p = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True, timeout=15)
                if p.returncode != 0:
                    detail = (p.stderr or p.stdout).strip()
                    errs.append(f"{js_path.relative_to(root)}: JS syntax error:\n{detail[:400]}")
    return errs


def check_js_unsafe_patterns(root=ROOT):
    """Block dynamic code execution and debugger leftovers inside app/*.html <script> blocks."""
    errs = []
    root = Path(root)
    script_re = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
    bad_js = re.compile(r"(?<![.$\w])\beval\s*\(|\bnew\s+Function\s*\(|\bdocument\.write\s*\(|\bdebugger\b")
    for name in ("dashboard.html", "terminal.html", "stage.html"):
        path = root / "app" / name
        if not path.is_file():
            continue
        text = path.read_text()
        for m in script_re.finditer(text):
            attrs, body = m.group(1), m.group(2)
            if re.search(r"\bsrc\s*=", attrs, re.I) or not body.strip():
                continue
            base_line = text[:m.start(2)].count("\n")
            for off, line in enumerate(body.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("//") or "// safety-ok" in line:
                    continue
                if bad_js.search(line):
                    errs.append(f"{path.relative_to(root)}:{base_line + off}: disallowed dynamic JS or debugger statement")
    return errs


def run_all(root=ROOT):
    errs = []
    errs.extend(check_swift_eval_js(root))
    errs.extend(check_swift_user_defaults(root))
    errs.extend(check_stage_web_isolated(root))
    errs.extend(check_python_shell_true(root))
    errs.extend(check_html_csp_and_handlers(root))
    errs.extend(check_js_syntax(root))
    errs.extend(check_js_unsafe_patterns(root))
    return errs


def main():
    errs = run_all(ROOT)
    if errs:
        for e in errs:
            print(f"SAFETY FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"ok  safety checks passed ({len(swift_files())} Swift file(s), 3 HTML pages, JS syntax, scripts/)")


if __name__ == "__main__":
    main()
