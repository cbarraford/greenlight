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
    bad_quoted = re.compile(r'evaluateJavaScript\((?:[^;]*?)(?:\\"|(?<!\\)\'|`)\\\(')
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
    found_def = False
    for path in swift_files():
        text = path.read_text()
        m = re.search(r"lazy var stageWeb\s*:\s*WKWebView\s*=\s*\{(.*?)\}\(\)", text, re.S)
        if m:
            found_def = True
            if "userContentController.add" in m.group(1):
                errs.append(f"{path.relative_to(ROOT)}: stageWeb must not attach any WKScriptMessageHandler")
        if re.search(r"\bstageWeb\b[^\n]*userContentController\.add", text):
            errs.append(f"{path.relative_to(ROOT)}: stageWeb must not attach any WKScriptMessageHandler")
    if not found_def:
        errs.append("stageWeb definition not found in Swift sources")
    return errs


def check_python_shell_true():
    errs = []
    py_files = sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "app").glob("*.py"))
    for path in py_files:
        if path.name == "check_safety.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bshell\s*=\s*True\b", line) and "noqa: S602" not in line:
                errs.append(f"{path.relative_to(ROOT)}:{i}: unapproved shell=True (use argv list or document `# noqa: S602`)")
            if re.search(r"\bos\.(?:system|popen)\s*\(", line) and "noqa: S605" not in line:
                errs.append(f"{path.relative_to(ROOT)}:{i}: unapproved os.system/os.popen")
    return errs


def check_html_csp_and_handlers():
    errs = []
    inline_ev = re.compile(r'\bon[a-z]{3,20}\s*=\s*(?:["\']|[^\s>]+)', re.I)
    for name in ("dashboard.html", "terminal.html", "stage.html"):
        path = ROOT / "app" / name
        text = path.read_text()
        m = re.search(r'<meta http-equiv="Content-Security-Policy"\s+content="([^"]+)"', text)
        if not m:
            errs.append(f"{path.relative_to(ROOT)}: missing Content-Security-Policy meta tag")
        else:
            csp = m.group(1)
            for req in ("default-src 'none'", "base-uri 'none'", "form-action 'none'"):
                if req not in csp:
                    errs.append(f"{path.relative_to(ROOT)}: CSP missing `{req}`")
            if "'unsafe-eval'" in csp:
                errs.append(f"{path.relative_to(ROOT)}: CSP must not allow 'unsafe-eval'")
        in_script = False
        for i, line in enumerate(text.splitlines(), 1):
            cleaned = line.replace('onerror="this.remove()"', "")
            if "<script" in line.lower():
                in_script = True
            if "</script>" in line.lower():
                in_script = False
            # Only check HTML tag attribute context (or JS template literals containing `<... on...=`)
            if not in_script or "<" in cleaned:
                for hm in re.finditer(r"<[a-z][^>]*>", cleaned, re.I):
                    if inline_ev.search(hm.group(0)):
                        errs.append(f"{path.relative_to(ROOT)}:{i}: inline HTML event handler detected")
                        break
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
