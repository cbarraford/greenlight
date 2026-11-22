#!/usr/bin/env python3
"""Diff-scoped quality ratchet for Greenlight (`make check-diff` and PR CI).

Enforces stricter quality rules on *newly added or modified* code without
requiring a massive legacy rewrite:
  1. Stricter Ruff rules (`UP,SIM,PERF,PIE,T10,C901,PLR0912`) on changed Python lines.
  2. Diff-scoped `mypy` type checks on changed lines in `scripts/` and `app/kokoro.py`.
  3. Function length cap (`--max-func-lines`, default 100) on newly added Python functions in `scripts/`.
  4. No new raw `evaluateJavaScript(` calls in `app/Sources/*.swift` (prefer `callJS(_:_:)`,
     or annotate intentionally raw expressions with `// safety-ok`).
  5. `shellcheck -S warning` on modified `.sh` and `.githooks/*` scripts.
"""
import argparse
import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRICT_RUFF_SELECT = "UP,SIM,PERF,PIE,T10,C901,PLR0912"
STRICT_RUFF_IGNORE = "SIM108,SIM102"  # allow concise ternaries / readable nested ifs


def _resolve_base_ref(root, base=None):
    if base:
        chk = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{base}^{{commit}}"],
                             capture_output=True, text=True)
        if chk.returncode != 0:
            raise RuntimeError(f"base git ref not found: {base}")
        return base
    for candidate in ("origin/main", "main", "HEAD~1"):
        chk = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"],
                             capture_output=True, text=True)
        if chk.returncode == 0:
            return candidate
    return "HEAD"


def git_diff(root, base=None, staged=False):
    cmd = ["git", "-C", str(root), "-c", "diff.noprefix=false", "-c", "core.quotepath=false",
           "diff", "--unified=0", "--no-color"]
    resolved_base = "HEAD"
    if staged:
        cmd.append("--cached")
    else:
        resolved_base = _resolve_base_ref(root, base)
        mb = subprocess.run(["git", "-C", str(root), "merge-base", resolved_base, "HEAD"], capture_output=True, text=True)
        diff_target = mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else resolved_base
        cmd.append(diff_target)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git diff failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()}")
    return p.stdout, resolved_base


def parse_added_lines(diff_text):
    """Return {rel_path: {lineno: line_text}} for every added line in a unified=0 diff."""
    added: dict[str, dict[int, str]] = {}
    cur_file = None
    in_hunk = False
    cur_line = 0
    hunk_rem = 0
    for row in (diff_text or "").splitlines():
        if not in_hunk or hunk_rem == 0:
            if row.startswith("diff --git "):
                cur_file, in_hunk, hunk_rem = None, False, 0
                continue
            if not in_hunk and row.startswith("+++ b/"):
                cur_file = row[6:]
                added.setdefault(cur_file, {})
                continue
            if not in_hunk and row.startswith("+++ /dev/null"):
                cur_file = None
                continue
            if row.startswith("@@ ") and cur_file is not None:
                m = re.search(r"\+(\d+)(?:,(\d+))?", row)
                if m:
                    cur_line = int(m.group(1))
                    hunk_rem = int(m.group(2)) if m.group(2) is not None else 1
                    in_hunk = True
                continue
        if in_hunk and cur_file is not None and hunk_rem > 0 and row.startswith("+"):
            added[cur_file][cur_line] = row[1:]
            cur_line += 1
            hunk_rem -= 1
    return {k: v for k, v in added.items() if v}


def _materialize_staged(root, added):
    """Write exact staged blobs (`git show :rel`) plus config files into a temp dir for `--staged` checks."""
    tmp = tempfile.TemporaryDirectory(prefix="gl-staged-check-")
    stage_root = Path(tmp.name)
    for cfg in ("ruff.toml", "pyproject.toml"):
        src = Path(root) / cfg
        if src.is_file():
            shutil.copy2(src, stage_root / cfg)
    for rel in sorted(added):
        p = subprocess.run(["git", "-C", str(root), "show", f":{rel}"], capture_output=True)
        if p.returncode != 0:
            continue
        dest = stage_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.stdout)
    return tmp, stage_root


def check_ruff_on_added(root, added):
    ruff = shutil.which("ruff")
    if not ruff:
        return []
    py_files = [rel for rel in sorted(added) if rel.endswith(".py") and (Path(root) / rel).is_file()]
    if not py_files:
        return []
    p = subprocess.run(
        [ruff, "check", "--no-cache", "--output-format", "json",
         "--select", STRICT_RUFF_SELECT, "--ignore", STRICT_RUFF_IGNORE,
         "--config", "lint.mccabe.max-complexity=15",
         *py_files],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    if p.returncode >= 2 or (p.returncode != 0 and not p.stdout.strip()):
        return [f"ruff execution failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[:400]}"]
    if not p.stdout.strip():
        return []
    try:
        items = json.loads(p.stdout)
    except ValueError as e:
        return [f"ruff returned invalid JSON: {e}"]
    errs = []
    root_resolved = Path(root).resolve()
    for item in items:
        fpath = Path(item.get("filename", "")).resolve()
        rel = str(fpath.relative_to(root_resolved)) if fpath.is_relative_to(root_resolved) else item.get("filename", "")
        line = (item.get("location") or {}).get("row")
        code = item.get("code", "")
        if rel.startswith("tests/") and code in ("C901", "PLR0912"):
            continue
        if line in added.get(rel, {}):
            errs.append(f"{rel}:{line}: {code} {item.get('message', '')}")
    return errs


def _existing_func_names(repo_root, base_ref, rel):
    """Return function names defined in `base_ref:rel` so editing an existing signature isn't treated as a new function."""
    if not base_ref:
        return set()
    p = subprocess.run(["git", "-C", str(repo_root), "show", f"{base_ref}:{rel}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        return set()
    try:
        tree = ast.parse(p.stdout, filename=rel)
    except SyntaxError:
        return set()
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def check_new_func_lengths(root, added, max_lines=100, repo_root=None, base_ref=None):
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
        existing = _existing_func_names(repo_root or root, base_ref, rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.lineno not in lines_map or node.name in existing:
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > max_lines:
                errs.append(
                    f"{rel}:{node.lineno}: new function `{node.name}` is {length} lines (limit {max_lines}; split into helpers)"
                )
    return errs


def check_new_swift_eval_js(added):
    errs = []
    call_re = re.compile(r"evaluateJavaScript\(")
    safe_zero_arg = re.compile(r'^evaluateJavaScript\(\s*"[A-Za-z_$][\w$.]*\(\s*\)"\s*\)')
    for rel, lines_map in sorted(added.items()):
        if not rel.startswith("app/Sources/") or not rel.endswith(".swift") or rel == "app/Sources/Config.swift":
            continue
        for lineno, text in sorted(lines_map.items()):
            stripped = text.lstrip()
            if stripped.startswith("//") or "// safety-ok" in text:
                continue
            for m in call_re.finditer(text):
                if not safe_zero_arg.match(text[m.start():]):
                    errs.append(
                        f"{rel}:{lineno}: new raw evaluateJavaScript(...) call (use `callJS(_:_:)` or mark `// safety-ok`)"
                    )
                    break
    return errs


def _mypy_cmd(root, py_files):
    cfg = Path(root) / "pyproject.toml"
    flags = (["--config-file", str(cfg)] if cfg.is_file() else
             ["--python-version", "3.12", "--ignore-missing-imports", "--check-untyped-defs"])
    flags += ["--show-column-numbers", "--no-error-summary", "--hide-error-context"]
    if importlib.util.find_spec("mypy") is not None:
        return [sys.executable, "-m", "mypy", *flags, *py_files]
    if shutil.which("uv"):
        return ["uv", "run", "--no-project", "--with", "mypy", "mypy", *flags, *py_files]
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
    if p.returncode >= 2 or (p.returncode != 0 and not p.stdout.strip() and p.stderr.strip()):
        return [f"mypy execution failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[:400]}"]
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
        if (rel.endswith(".sh") or rel.startswith(".githooks/")) and (Path(root) / rel).is_file()
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
    ap.add_argument("--staged", action="store_true", help="Inspect staged blobs instead of working-tree diff")
    ap.add_argument("--skip-mypy", action="store_true", help="Skip diff-scoped mypy type checking")
    ap.add_argument("--path", default=str(ROOT), help="Repository root path")
    ap.add_argument("--max-func-lines", type=int, default=100, help="Max line count for newly added Python functions")
    args = ap.parse_args(argv)

    repo_root = Path(args.path).resolve()
    try:
        diff_text, base_ref = git_diff(repo_root, base=args.base, staged=args.staged)
    except RuntimeError as e:
        print(f"DIFF QUALITY FAIL: {e}", file=sys.stderr)
        return 1
    added = parse_added_lines(diff_text)
    if not added:
        print("ok  diff quality checks passed (no added lines)")
        return 0

    tmp_ctx = _materialize_staged(repo_root, added) if args.staged else None
    check_root = tmp_ctx[1] if tmp_ctx else repo_root
    try:
        errs = []
        errs.extend(check_ruff_on_added(check_root, added))
        if not args.skip_mypy:
            errs.extend(check_mypy_on_added(check_root, added))
        errs.extend(check_new_func_lengths(check_root, added, max_lines=args.max_func_lines,
                                           repo_root=repo_root, base_ref=base_ref))
        errs.extend(check_new_swift_eval_js(added))
        errs.extend(check_changed_shell(check_root, added))
    finally:
        if tmp_ctx:
            tmp_ctx[0].cleanup()

    if errs:
        for err in errs:
            print(f"DIFF QUALITY FAIL: {err}", file=sys.stderr)
        return 1
    print(f"ok  diff quality checks passed ({len(added)} changed file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
