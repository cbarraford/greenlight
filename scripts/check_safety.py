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
  9. Design-token drift ratchet: literal colours, font sizes, radii and shadows in app/*.html (outside the
     generated `ds-tokens` block) and in Swift (outside Config.swift and Tokens.swift) may only go down from
     scripts/drift_baseline.json. Lower the baseline with `check_safety.py --update-drift` once they fall.
"""
import json
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
                or (path.name == "Config.swift" and "removePersistentDomain" in line)  # dropDomain: test domains only
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


DRIFT_BASELINE = "scripts/drift_baseline.json"
DRIFT_HTML = {
    "colour": r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![\w-])|\b(?:rgba?|hsla?|oklch)\(",
    "font_size": r"font-size:\s*[\d.]+px|\bfont:\s*(?:\d{3}\s+)?[\d.]+px",
    "radius": r"border-radius:\s*[\d.]+(?:px|%)",
    "shadow": r"box-shadow:\s*(?!none|var\()|drop-shadow\(",
}
DRIFT_SWIFT = {
    "colour": r'"#?(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})"|\bColor\((?:red|white|hue):|NSColor\((?:srgbRed|red|white|calibrated\w*|device\w*):',
    "font_size": r"\.system\(size:",
    "radius": r"cornerRadius:\s*[\d.]+",
}
DRIFT_SWIFT_EXEMPT = {"Config.swift", "Tokens.swift"}  # where the tokens themselves live
TOKEN_BLOCK = re.compile(r'<style id="ds-tokens">.*?</style><!-- /ds-tokens -->', re.S)


def drift_counts(root=ROOT):
    """{file: {kind: count}} of literal design values that bypass the tokens."""
    root = Path(root)
    out = {}
    for page in sorted((root / "app").glob("*.html")):
        text = TOKEN_BLOCK.sub("", page.read_text(encoding="utf-8", errors="replace"))
        out[str(page.relative_to(root))] = {k: len(re.findall(p, text)) for k, p in DRIFT_HTML.items()}
    for path in swift_files(root):
        if path.name in DRIFT_SWIFT_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        out[str(path.relative_to(root))] = {k: len(re.findall(p, text)) for k, p in DRIFT_SWIFT.items()}
    return out


def check_drift(root=ROOT):
    """Fail when any file uses more literal design values than its baseline (a missing baseline skips the check)."""
    base_path = Path(root) / DRIFT_BASELINE
    if not base_path.is_file():
        return []
    base = json.loads(base_path.read_text())
    errs = []
    for rel, counts in drift_counts(root).items():
        for kind, n in counts.items():
            allowed = base.get(rel, {}).get(kind, 0)
            if n > allowed:
                errs.append(f"{rel}: {n} literal {kind} values (baseline {allowed}); use a --ds-* / DS token "
                            "from brand/DESIGN.md instead")
    return errs


def drift_fell(root=ROOT):
    """Files and kinds now below their baseline, so the baseline can be lowered."""
    base_path = Path(root) / DRIFT_BASELINE
    if not base_path.is_file():
        return []
    base = json.loads(base_path.read_text())
    return [f"{rel} {kind}" for rel, counts in drift_counts(root).items() for kind, n in counts.items()
            if n < base.get(rel, {}).get(kind, 0)]


def update_drift(root=ROOT):
    path = Path(root) / DRIFT_BASELINE
    path.write_text(json.dumps(drift_counts(root), indent=1, sort_keys=True) + "\n")
    return path


def run_all(root=ROOT):
    errs = []
    errs.extend(check_swift_eval_js(root))
    errs.extend(check_swift_user_defaults(root))
    errs.extend(check_stage_web_isolated(root))
    errs.extend(check_python_shell_true(root))
    errs.extend(check_html_csp_and_handlers(root))
    errs.extend(check_js_syntax(root))
    errs.extend(check_js_unsafe_patterns(root))
    errs.extend(check_drift(root))
    return errs


def main():
    if "--update-drift" in sys.argv[1:]:
        print(f"ok  wrote {update_drift(ROOT).relative_to(ROOT)}")
        return
    errs = run_all(ROOT)
    if errs:
        for e in errs:
            print(f"SAFETY FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"ok  safety checks passed ({len(swift_files())} Swift file(s), 3 HTML pages, JS syntax, scripts/, drift)")
    fell = drift_fell(ROOT)
    if fell:
        print(f"note: token drift fell ({', '.join(fell)}); lower the baseline with `check_safety.py --update-drift`")


if __name__ == "__main__":
    main()
