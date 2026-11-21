#!/usr/bin/env python3
"""Static safety & security guardrails for Greenlight.

Run locally (`make lint`) and in CI to prevent AI-introduced security regressions:
  1. No raw quoted string interpolation inside Swift `evaluateJavaScript(...)`.
  2. No unapproved `shell=True` in `scripts/*.py` without `# noqa: S602`.
  3. Content-Security-Policy present in dashboard.html, terminal.html, and stage.html.
  4. `stageWeb` stays handler-less (no WKScriptMessageHandler on untrusted voice stage).
  5. No inline event handlers inside JS template literals in app/*.html (except avatar fallback).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def swift_files():
    src_dir = ROOT / "app" / "Sources"
    if src_dir.is_dir():
        files = sorted(src_dir.glob("*.swift"))
        if files:
            return files
    single = ROOT / "app" / "Greenlight.swift"
    return [single] if single.is_file() else []


def check_swift_eval_js():
    errs = []
    # Disallow quoted Swift string interpolation inside evaluateJavaScript(...), e.g.
    # evaluateJavaScript("mc.openPage(\"\(name)\")") or evaluateJavaScript("term.opens('\(f)')")
    # Safe patterns pass JSON via callJS(...) or pass an already-JSON-encoded expression.
    bad_quoted = re.compile(r'evaluateJavaScript\([^)]*(?:\\"|(?<!\\)\')\\\(')
    for path in swift_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            if bad_quoted.search(line) and "// safety-ok" not in line:
                errs.append(f"{path.relative_to(ROOT)}:{i}: raw quoted interpolation in evaluateJavaScript (use callJS)")
    return errs


def check_stage_web_isolated():
    errs = []
    for path in swift_files():
        text = path.read_text()
        m = re.search(r"lazy var stageWeb\s*:\s*WKWebView\s*=\s*\{(.*?)\}\(\)", text, re.S)
        if m and "userContentController.add" in m.group(1):
            errs.append(f"{path.relative_to(ROOT)}: stageWeb must not attach any WKScriptMessageHandler")
    return errs


def check_python_shell_true():
    errs = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        if path.name == "check_safety.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bshell\s*=\s*True\b", line) and "noqa: S602" not in line:
                errs.append(f"{path.relative_to(ROOT)}:{i}: unapproved shell=True (use argv list or document `# noqa: S602`)")
    return errs


def check_html_csp_and_handlers():
    errs = []
    inline_ev = re.compile(r'\bon(?:click|error|load|focus|blur|mouseover|mouseenter)\s*=\s*["\']', re.I)
    for name in ("dashboard.html", "terminal.html", "stage.html"):
        path = ROOT / "app" / name
        text = path.read_text()
        if '<meta http-equiv="Content-Security-Policy"' not in text:
            errs.append(f"{path.relative_to(ROOT)}: missing Content-Security-Policy meta tag")
        for i, line in enumerate(text.splitlines(), 1):
            if "onerror=\"this.remove()\"" in line and line.count("onerror=") == 1:
                continue
            if inline_ev.search(line):
                errs.append(f"{path.relative_to(ROOT)}:{i}: inline HTML event handler detected")
    return errs


def main():
    errs = []
    errs.extend(check_swift_eval_js())
    errs.extend(check_stage_web_isolated())
    errs.extend(check_python_shell_true())
    errs.extend(check_html_csp_and_handlers())
    if errs:
        for e in errs:
            print(f"SAFETY FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"ok  safety checks passed ({len(swift_files())} Swift file(s), 3 HTML pages, scripts/)")


if __name__ == "__main__":
    main()
