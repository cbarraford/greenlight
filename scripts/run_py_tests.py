#!/usr/bin/env python3
"""Unified Python test runner for Greenlight (`make test-py`, `make coverage`, and CI).

Prevents the silent-pass trap where an agent writes a pytest-style `def test_*()` file
that exits 0 without running any assertions when executed as `python3 tests/test_foo.py`.

For every `tests/test_*.py` (excluding `test_uitest.py` unless `--include-ui`):
  - If the file defines top-level `def test_*` functions or `class Test*` / `unittest.TestCase`,
    execute it with `pytest -q` (and only run it as a standalone script too if it has
    top-level executable assertions outside `if __name__ == "__main__"`).
  - Otherwise, execute it directly as a standalone script (`python3 <file>`).
  - Syntax errors or empty test files fail loudly without aborting the remaining suites.
  - Under `--coverage`, enables subprocess coverage (`COVERAGE_PROCESS_START` + temporary
    `sitecustomize.py`) so CLI subprocess invocations are measured automatically.
"""
import argparse
import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_main_guard(node):
    return isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)


def classify_test_file(path):
    """Return 'pytest', 'script', 'both', or 'empty' for a Python test file."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    has_pytest_items = False
    has_top_exec = False
    has_main = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_"):
            has_pytest_items = True
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test") or any("TestCase" in ast.unparse(b) for b in node.bases):
                has_pytest_items = True
        elif _is_main_guard(node):
            has_main = True
            has_top_exec = True
        elif isinstance(node, ast.Assert | ast.Expr | ast.For | ast.With | ast.If | ast.Try):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue  # module docstring
            has_top_exec = True
    if has_pytest_items and has_main:
        return "both"
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


def _cov_rc_args():
    cfg = ROOT / "pyproject.toml"
    return ["--rcfile", str(cfg)] if cfg.is_file() else []


def pytest_cmd(files, use_cov=False):
    prefix = _tool_prefix(needs_pytest=True, needs_cov=use_cov)
    if use_cov:
        return [*prefix, "-m", "coverage", "run", *_cov_rc_args(), "--parallel-mode", "-m", "pytest", "-q", *[str(f) for f in files]]
    return [*prefix, "-m", "pytest", "-q", *[str(f) for f in files]]


def script_cmd(path, use_cov=False):
    prefix = _tool_prefix(needs_cov=use_cov)
    if use_cov:
        return [*prefix, "-m", "coverage", "run", *_cov_rc_args(), "--parallel-mode", str(path)]
    return [*prefix, str(path)]


def _prepare_cov_env(cov_dir: Path) -> dict[str, str]:
    """Build an env dict with subprocess coverage auto-start via a temporary sitecustomize.py."""
    site_dir = cov_dir / "site"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "sitecustomize.py").write_text(
        "try:\n"
        "    import coverage\n"
        "    coverage.process_startup()\n"
        "except Exception:\n"
        "    pass\n"
    )
    cfg = ROOT / "pyproject.toml"
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(cov_dir / ".coverage")
    if cfg.is_file():
        env["COVERAGE_PROCESS_START"] = str(cfg)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{site_dir}{os.pathsep}{existing_pp}" if existing_pp else str(site_dir)
    return env


def _rel(path):
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _run_grouped(label, cmd, gh, env=None):
    if gh:
        print(f"::group::{label}", flush=True)
    # as conftest.py does for pytest: without a username, `import greenlight` asks the host's glab/gh (network, 20 s each)
    p = subprocess.run(cmd, cwd=ROOT, env={"GREENLIGHT_ME": "octo", **(env or os.environ)})
    if p.returncode != 0:
        if gh:
            print(f"::error::{label} failed", flush=True)
        else:
            print(f"FAIL {label}", file=sys.stderr)
    if gh:
        print("::endgroup::", flush=True)
    return p.returncode == 0


def _collect_suites(tests_dir, include_ui):
    scripts: list[Path] = []
    pytests: list[Path] = []
    bad: list[tuple[Path, str]] = []
    for path in sorted(Path(tests_dir).glob("test_*.py")):
        if path.name == "test_uitest.py" and not include_ui:
            continue
        try:
            kind = classify_test_file(path)
        except SyntaxError as e:
            bad.append((path, f"syntax error: {e}"))
            continue
        if kind == "empty":
            bad.append((path, "no executable tests found"))
        elif kind == "pytest":
            pytests.append(path)
        elif kind == "both":
            scripts.append(path)
            pytests.append(path)
        else:
            scripts.append(path)
    return scripts, pytests, bad


def parse_coverage_xml(xml_path: Path) -> dict:
    """Parse Cobertura XML into line/branch totals and per-file percentages."""
    root = ET.parse(xml_path).getroot()  # noqa: S314 - local Cobertura XML generated by coverage.py
    files: dict[str, dict[str, float | int]] = {}
    for cls in root.findall(".//class"):
        fn = cls.attrib["filename"]
        lines = cls.findall("./lines/line")
        total = len(lines)
        cov = sum(1 for ln in lines if int(ln.attrib.get("hits", 0)) > 0)
        files[fn] = {
            "covered": cov,
            "total": total,
            "pct": round((cov / total * 100.0) if total else 100.0, 2),
        }
    return {
        "line_pct": round(float(root.attrib.get("line-rate", 0.0)) * 100.0, 2),
        "branch_pct": round(float(root.attrib.get("branch-rate", 0.0)) * 100.0, 2),
        "lines_covered": int(root.attrib.get("lines-covered", 0)),
        "lines_valid": int(root.attrib.get("lines-valid", 0)),
        "branches_covered": int(root.attrib.get("branches-covered", 0)),
        "branches_valid": int(root.attrib.get("branches-valid", 0)),
        "files": files,
    }


def _emit_coverage(xml_path, env, json_path=None, fail_under=None, min_file=None) -> list[str]:
    prefix = _tool_prefix(needs_cov=True)
    rc = _cov_rc_args()
    out_xml = Path(xml_path).resolve()
    report_env = {k: v for k, v in (env or os.environ).items() if k != "COVERAGE_PROCESS_START"}
    subprocess.run([*prefix, "-m", "coverage", "combine", *rc], cwd=ROOT, env=report_env, check=False)
    subprocess.run([*prefix, "-m", "coverage", "xml", *rc, "--ignore-errors", "-o", str(out_xml)], cwd=ROOT, env=report_env, check=True)
    subprocess.run([*prefix, "-m", "coverage", "report", *rc, "--ignore-errors", "--skip-covered"], cwd=ROOT, env=report_env, check=False)
    summary = parse_coverage_xml(out_xml)
    if json_path:
        Path(json_path).write_text(json.dumps(summary, indent=2) + "\n")
    errs: list[str] = []
    if fail_under is not None and summary["line_pct"] < fail_under:
        errs.append(f"Python line coverage {summary['line_pct']:.2f}% < required {fail_under:.2f}%")
    if min_file is not None:
        low = sorted((v["pct"], k) for k, v in summary["files"].items() if v["pct"] < min_file)
        for pct, fn in low:
            errs.append(f"Python file {fn} coverage {pct:.2f}% < per-file floor {min_file:.2f}%")
    return errs


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run all Greenlight Python test suites (scripts + pytest).")
    ap.add_argument("--include-ui", action="store_true", help="Also run tests/test_uitest.py")
    ap.add_argument("--tests-dir", default=str(ROOT / "tests"), help="Directory containing test_*.py files")
    ap.add_argument("--coverage", default=None, help="Write combined Cobertura XML coverage report to this path")
    ap.add_argument("--json", default=None, help="Write coverage summary JSON to this path (requires --coverage)")
    ap.add_argument("--fail-under", type=float, default=None, help="Fail if total line coverage is below this %%")
    ap.add_argument("--min-file", type=float, default=None, help="Fail if any tracked file is below this %%")
    args = ap.parse_args(argv)

    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    use_cov = bool(args.coverage)
    scripts, pytests, bad = _collect_suites(args.tests_dir, args.include_ui)
    failed = []
    for path, why in bad:
        rel = _rel(path)
        print(f"FAIL {rel}: {why}", file=sys.stderr)
        failed.append(rel)

    cov_errs: list[str] = []
    with tempfile.TemporaryDirectory(prefix="gl-cov-") as cov_dir:
        env = _prepare_cov_env(Path(cov_dir)) if use_cov else None
        for path in scripts:
            rel = _rel(path)
            if not _run_grouped(rel, script_cmd(path, use_cov=use_cov), gh, env=env):
                failed.append(rel)
        if pytests:
            rels = [_rel(p) for p in pytests]
            if not _run_grouped(f"pytest ({', '.join(rels)})", pytest_cmd(pytests, use_cov=use_cov), gh, env=env):
                failed.extend(rels)
        if use_cov and not failed:
            cov_errs = _emit_coverage(args.coverage, env, json_path=args.json, fail_under=args.fail_under, min_file=args.min_file)

    if failed:
        print(f"FAILED {len(failed)} test suite(s): {', '.join(failed)}", file=sys.stderr)
        return 1
    if cov_errs:
        for e in cov_errs:
            print(f"COVERAGE FAIL: {e}", file=sys.stderr)
        return 1
    print(f"ok  {len(scripts)} script suite(s) + {len(pytests)} pytest suite(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
