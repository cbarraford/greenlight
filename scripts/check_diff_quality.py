#!/usr/bin/env python3
"""Diff-scoped quality ratchet for Greenlight (`make check-diff` and PR CI).

Enforces stricter quality rules on *newly added or modified* code without
requiring a massive legacy rewrite:
  1. Stricter Ruff rules (`UP,SIM,PERF,PIE,T10,C901,PLR0912`) on changed Python lines.
  2. Function length cap (`--max-func-lines`, default 100) on newly added Python functions in `scripts/`.
  3. No new raw `evaluateJavaScript(` calls in `app/Sources/*.swift` (prefer `callJS(_:_:)`,
     or annotate intentionally raw expressions with `// safety-ok`).
  4. `shellcheck -S warning` on modified `.sh` scripts.
"""
import argparse
import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRICT_RUFF_SELECT = "UP,SIM,PERF,PIE,T10,C901,PLR0912"
STRICT_RUFF_IGNORE = "SIM108,SIM102"  # allow concise ternaries / readable nested ifs


def git_diff(root, base=None, staged=False):
    cmd = ["git", "-C", str(root), "diff", "--unified=0", "--no-color"]
    if staged:
        cmd.append("--cached")
    else:
        if not base:
            for candidate in ("origin/main", "main", "HEAD~1"):
                chk = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"],
                                     capture_output=True, text=True)
                if chk.returncode == 0:
                    base = candidate
                    break
        if base:
            mb = subprocess.run(["git", "-C", str(root), "merge-base", base, "HEAD"], capture_output=True, text=True)
            cmd.append(mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else base)
        else:
            cmd.append("HEAD")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else ""


def parse_added_lines(diff_text):
    """Return {rel_path: {lineno: line_text}} for every added line in a unified=0 diff."""
    added: dict[str, dict[int, str]] = {}
    cur_file = None
    cur_line = 0
    for row in (diff_text or "").splitlines():
        if row.startswith("+++ b/"):
            cur_file = row[6:]
            added.setdefault(cur_file, {})
        elif row.startswith("+++ /dev/null"):
            cur_file = None
        elif row.startswith("@@ ") and cur_file is not None:
            m = re.search(r"\+(\d+)(?:,(\d+))?", row)
            if m:
                cur_line = int(m.group(1))
        elif cur_file is not None and row.startswith("+") and not row.startswith("+++"):
            added[cur_file][cur_line] = row[1:]
            cur_line += 1
    return {k: v for k, v in added.items() if v}


def check_ruff_on_added(root, added):
    ruff = shutil.which("ruff")
    if not ruff:
        return []
    py_files = [
        rel for rel in sorted(added)
        if rel.endswith(".py") and (Path(root) / rel).is_file()
    ]
    if not py_files:
        return []
    p = subprocess.run(
        [ruff, "check", "--no-cache", "--output-format", "json",
         "--select", STRICT_RUFF_SELECT, "--ignore", STRICT_RUFF_IGNORE,
         "--config", "lint.mccabe.max-complexity=15",
         *py_files],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    if not p.stdout.strip():
        return []
    try:
        items = json.loads(p.stdout)
    except ValueError:
        return []
    errs = []
    root_resolved = Path(root).resolve()
    for item in items:
        fpath = Path(item.get("filename", "")).resolve()
        try:
            rel = str(fpath.relative_to(root_resolved))
        except ValueError:
            rel = item.get("filename", "")
        line = (item.get("location") or {}).get("row")
        code = item.get("code", "")
        if rel.startswith("tests/") and code in ("C901", "PLR0912"):
            continue
        if line in added.get(rel, {}):
            errs.append(f"{rel}:{line}: {code} {item.get('message', '')}")
    return errs


def check_new_func_lengths(root, added, max_lines=100):
    errs = []
    for rel, lines_map in sorted(added.items()):
        if not rel.endswith(".py") or not rel.startswith("scripts/"):
            continue
        path = Path(root) / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            # Only flag functions whose definition line itself was added in this diff
            if node.lineno not in lines_map:
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > max_lines:
                errs.append(
                    f"{rel}:{node.lineno}: new function `{node.name}` is {length} lines (limit {max_lines}; split into helpers)"
                )
    return errs


def check_new_swift_eval_js(added):
    errs = []
    for rel, lines_map in sorted(added.items()):
        if not rel.startswith("app/Sources/") or not rel.endswith(".swift"):
            continue
        if rel == "app/Sources/Config.swift":
            continue  # defines WKWebView.callJS
        for lineno, text in sorted(lines_map.items()):
            stripped = text.lstrip()
            if stripped.startswith("//") or "evaluateJavaScript(" not in text:
                continue
            if "// safety-ok" in text:
                continue
            # Allow simple zero-argument calls like `term.focus()` or `term.closeTab()`
            if re.search(r'evaluateJavaScript\(\s*"[^"\\]*\(\s*\)"\s*\)', text):
                continue
            errs.append(
                f"{rel}:{lineno}: new raw evaluateJavaScript(...) call (use `callJS(_:_:)` or mark `// safety-ok`)"
            )
    return errs


def _mypy_cmd(root, py_files):
    flags = ["--python-version", "3.12", "--ignore-missing-imports", "--check-untyped-defs",
             "--show-column-numbers", "--no-error-summary", "--hide-error-context"]
    cfg = Path(root) / "pyproject.toml"
    if cfg.is_file():
        flags = ["--config-file", str(cfg), "--show-column-numbers", "--no-error-summary", "--hide-error-context"]
    if importlib.util.find_spec("mypy") is not None:
        return [sys.executable, "-m", "mypy", *flags, *py_files]
    if shutil.which("uv"):
        return ["uv", "run", "--no-project", "--python", "3.12", "--with", "mypy", "mypy", *flags, *py_files]
    return None


def check_mypy_on_added(root, added):
    """Flag mypy type errors only on lines added/modified in scripts/ or app/kokoro.py."""
    py_files = [
        rel for rel in sorted(added)
        if rel.endswith(".py") and (rel.startswith("scripts/") or rel == "app/kokoro.py")
        and (Path(root) / rel).is_file()
    ]
    if not py_files:
        return []
    cmd = _mypy_cmd(root, py_files)
    if not cmd:
        return []
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=90)
    err_re = re.compile(r"^([^:]+):(\d+):(?:\d+:)? error: (.+)$")
    errs = []
    for row in (p.stdout or "").splitlines():
        m = err_re.match(row.strip())
        if not m:
            continue
        rel, lineno, msg = m.group(1), int(m.group(2)), m.group(3)
        if lineno in added.get(rel, {}):
            errs.append(f"{rel}:{lineno}: mypy: {msg}")
    return errs


def check_changed_shell(root, added):
    sc = shutil.which("shellcheck")
    if not sc:
        return []
    sh_files = [
        rel for rel in sorted(added)
        if rel.endswith(".sh") and (Path(root) / rel).is_file()
    ]
    if not sh_files:
        return []
    p = subprocess.run([sc, "-S", "warning", "-f", "gcc", *sh_files], cwd=root, capture_output=True, text=True, timeout=30)
    if p.returncode == 0:
        return []
    return [line for line in (p.stdout or p.stderr).splitlines() if line.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diff-scoped code quality ratchet for Greenlight.")
    ap.add_argument("--base", default=None, help="Base git ref to diff against (default: origin/main)")
    ap.add_argument("--staged", action="store_true", help="Inspect staged changes instead of branch diff")
    ap.add_argument("--skip-mypy", action="store_true", help="Skip diff-scoped mypy type checking")
    ap.add_argument("--path", default=str(ROOT), help="Repository root path")
    ap.add_argument("--max-func-lines", type=int, default=100, help="Max line count for newly added Python functions")
    args = ap.parse_args(argv)

    root = Path(args.path).resolve()
    diff_text = git_diff(root, base=args.base, staged=args.staged)
    added = parse_added_lines(diff_text)
    if not added:
        print("ok  diff quality checks passed (no added lines)")
        return 0

    errs = []
    errs.extend(check_ruff_on_added(root, added))
    if not args.skip_mypy:
        errs.extend(check_mypy_on_added(root, added))
    errs.extend(check_new_func_lengths(root, added, max_lines=args.max_func_lines))
    errs.extend(check_new_swift_eval_js(added))
    errs.extend(check_changed_shell(root, added))

    if errs:
        for e in errs:
            print(f"DIFF QUALITY FAIL: {e}", file=sys.stderr)
        return 1
    print(f"ok  diff quality checks passed ({len(added)} changed file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
