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
  8. No dynamic JS execution (`eval`, `new Function`, `document.write`) or `debugger` in app/*.html.
"""
import os
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


def _extract_swift_call_arg(text, open_paren_idx):
    """Return the argument slice inside `(...)` starting at `open_paren_idx`, respecting Swift string literals."""
    i = open_paren_idx + 1
    n = len(text)
    depth = 1
    in_str = False
    in_triple = False
    while i < n and depth > 0:
        if in_triple:
            if text.startswith('"""', i):
                in_triple = False
                i += 3
                continue
            i += 2 if text[i] == "\\" else 1
            continue
        if in_str:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                in_str = False
            i += 1
            continue
        if text.startswith('"""', i):
            in_triple = True
            i += 3
        elif text[i] == '"':
            in_str = True
            i += 1
        elif text[i] == "(":
            depth += 1
            i += 1
        elif text[i] == ")":
            depth -= 1
            i += 1
        else:
            i += 1
    return text[open_paren_idx:i], i


def _has_quoted_swift_interp(call_text):
    """Detect quoted Swift interpolation (`\"\\(`, `'\\(`, or unescaped `\"\\(` inside multiline `\"\"\"...\"\"\"`)."""
    if re.search(r'(?:\\"|(?<!\\)\')\\\(', call_text):
        return True
    for m in re.finditer(r'"""(.*?)"""', call_text, re.S):
        if re.search(r'["\']\\\(', m.group(1)):
            return True
    return False


def check_swift_eval_js(root=ROOT):
    errs = []
    root = Path(root)
    call_re = re.compile(r"evaluateJavaScript\(")
    for path in swift_files(root):
        text = path.read_text()
        for m in call_re.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if text[line_start:m.start()].lstrip().startswith("//"):
                continue
            call_text, end_idx = _extract_swift_call_arg(text, m.end() - 1)
            line_end = text.find("\n", end_idx)
            tail = text[end_idx : len(text) if line_end == -1 else line_end]
            if _has_quoted_swift_interp(call_text) and "// safety-ok" not in (call_text + tail):
                lineno = text[: m.start()].count("\n") + 1
                errs.append(f"{path.relative_to(root)}:{lineno}: raw quoted interpolation in evaluateJavaScript (use callJS)")
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
                or (path.name == "App.swift" and "NSWindow Frame" in line)  # AppKit keeps frames there, --uitest or not
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
        if re.search(r"\bstageWeb\b[^\n]*userContentController\.add", text):
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
            if re.search(r"\bos\.(?:system|popen)\s*\(", line) and "noqa: S605" not in line:
                errs.append(f"{path.relative_to(root)}:{i}: unapproved os.system/os.popen")
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
        m = re.search(r'<meta http-equiv="Content-Security-Policy"\s+content="([^"]+)"', text)
        if not m:
            errs.append(f"{path.relative_to(root)}: missing Content-Security-Policy meta tag")
        elif "'unsafe-eval'" in m.group(1):
            errs.append(f"{path.relative_to(root)}: CSP must not allow 'unsafe-eval'")
        for i, line in enumerate(text.splitlines(), 1):
            cleaned = line.replace('onerror="this.remove()"', "")
            if inline_ev.search(cleaned):
                errs.append(f"{path.relative_to(root)}:{i}: inline HTML event handler detected")
    return errs


def check_js_syntax(root=ROOT):
    """Verify inline <script> blocks in app/*.html and JS test files parse cleanly with `node --check`."""
    node = shutil.which("node")
    if not node:
        return ["node is required in CI to check JS syntax"] if os.environ.get("CI") == "true" else []
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
