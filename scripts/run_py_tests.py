#!/usr/bin/env python3
"""Unified Python test runner for Greenlight (`make test-py`, `make coverage`, and CI).

Prevents the silent-pass trap where an agent writes a pytest-style `def test_*()` file
that exits 0 without running any assertions when executed as `python3 tests/test_foo.py`.

For every `tests/test_*.py` (excluding `test_uitest.py` unless `--include-ui`):
  - If the file defines top-level `def test_*` functions or `class Test*` / `unittest.TestCase`,
    execute it with `pytest -q`.
  - Otherwise, execute it directly as a standalone script (`python3 <file>`).
"""
import argparse
import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def classify_test_file(path):
    """Return 'pytest' if path defines pytest/unittest test items, 'script' if top-level statements, else 'empty'."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    has_pytest_items = False
    has_top_exec = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            has_pytest_items = True
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test") or any("TestCase" in ast.unparse(b) for b in node.bases):
                has_pytest_items = True
        elif isinstance(node, ast.Assert | ast.Expr | ast.For | ast.With | ast.If | ast.Try):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue  # module docstring
            has_top_exec = True
    if has_pytest_items:
        return "pytest"
    return "script" if has_top_exec else "empty"


def _tool_prefix(needs_pytest=False, needs_cov=False):
    has_pytest = not needs_pytest or importlib.util.find_spec("pytest") is not None
    has_cov = not needs_cov or importlib.util.find_spec("coverage") is not None
    if has_pytest and has_cov:
        return [sys.executable]
    if shutil.which("uv"):
        pkgs = []
        if needs_pytest:
            pkgs.extend(["--with", "pytest"])
        if needs_cov:
            pkgs.extend(["--with", "coverage"])
        return ["uv", "run", "--no-project", *pkgs, "python"]
    return [sys.executable]


def pytest_cmd(files, use_cov=False):
    prefix = _tool_prefix(needs_pytest=True, needs_cov=use_cov)
    if use_cov:
        return [*prefix, "-m", "coverage", "run", "--parallel-mode", "-m", "pytest", "-q", *[str(f) for f in files]]
    return [*prefix, "-m", "pytest", "-q", *[str(f) for f in files]]


def script_cmd(path, use_cov=False):
    prefix = _tool_prefix(needs_cov=use_cov)
    if use_cov:
        return [*prefix, "-m", "coverage", "run", "--parallel-mode", str(path)]
    return [*prefix, str(path)]


def _rel(path):
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _run_grouped(label, cmd, gh, env=None):
    if gh:
        print(f"::group::{label}", flush=True)
    p = subprocess.run(cmd, env=env)
    if p.returncode != 0:
        if gh:
            print(f"::error::{label} failed", flush=True)
        else:
            print(f"FAIL {label}", file=sys.stderr)
    if gh:
        print("::endgroup::", flush=True)
    return p.returncode == 0


def _collect_suites(tests_dir, include_ui):
    groups: dict[str, list[Path]] = {"script": [], "pytest": [], "empty": []}
    for path in sorted(Path(tests_dir).glob("test_*.py")):
        if path.name == "test_uitest.py" and not include_ui:
            continue
        groups[classify_test_file(path)].append(path)
    return groups["script"], groups["pytest"], groups["empty"]


def _emit_coverage(xml_path, env):
    prefix = _tool_prefix(needs_cov=True)
    subprocess.run([*prefix, "-m", "coverage", "combine"], env=env, check=False)
    subprocess.run([*prefix, "-m", "coverage", "xml", "-o", str(xml_path)], env=env, check=True)
    subprocess.run([*prefix, "-m", "coverage", "report", "--skip-covered"], env=env, check=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run all Greenlight Python test suites (scripts + pytest).")
    ap.add_argument("--include-ui", action="store_true", help="Also run tests/test_uitest.py")
    ap.add_argument("--tests-dir", default=str(ROOT / "tests"), help="Directory containing test_*.py files")
    ap.add_argument("--coverage", default=None, help="Write combined Cobertura XML coverage report to this path")
    args = ap.parse_args(argv)

    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    use_cov = bool(args.coverage)
    scripts, pytests, empty = _collect_suites(args.tests_dir, args.include_ui)
    failed = [_rel(p) for p in empty]
    for rel in failed:
        print(f"FAIL {rel}: no executable tests found", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="gl-cov-") as cov_dir:
        env = {**os.environ, "COVERAGE_FILE": str(Path(cov_dir) / ".coverage")} if use_cov else None
        for path in scripts:
            rel = _rel(path)
            if not _run_grouped(rel, script_cmd(path, use_cov=use_cov), gh, env=env):
                failed.append(rel)
        if pytests:
            rels = [_rel(p) for p in pytests]
            if not _run_grouped(f"pytest ({', '.join(rels)})", pytest_cmd(pytests, use_cov=use_cov), gh, env=env):
                failed.extend(rels)
        if use_cov and not failed:
            _emit_coverage(args.coverage, env)

    if failed:
        print(f"FAILED {len(failed)} test suite(s): {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"ok  {len(scripts)} script suite(s) + {len(pytests)} pytest suite(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
