#!/usr/bin/env python3
"""Unified multi-language coverage summary across Python, JavaScript, and Swift (`make coverage-all`)."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_json(cmd: list[str], json_path: Path) -> tuple[bool, dict | None]:
    p = subprocess.run(cmd, cwd=ROOT)
    data = json.loads(json_path.read_text(encoding="utf-8")) if json_path.is_file() else None
    return p.returncode == 0, data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run Python, JS, and Swift coverage harnesses and print a unified summary.")
    ap.add_argument("--skip-swift", action="store_true", help="Skip Swift llvm-cov run")
    ap.add_argument("--include-ui", action="store_true", help="Include --uitest in Swift coverage")
    args = ap.parse_args(argv)

    failed = False
    rows: list[tuple[str, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="gl-covall-") as tmp:
        td = Path(tmp)
        py_json = td / "py.json"
        ok_py, py_d = _run_json(
            [sys.executable, str(ROOT / "scripts/run_py_tests.py"), "--coverage", str(td / "py.xml"), "--json", str(py_json), "--fail-under", "80"],
            py_json,
        )
        failed = failed or not ok_py
        if py_d:
            rows.append(("Python (scripts + app)", f"{py_d['line_pct']:.2f}% line / {py_d['branch_pct']:.2f}% branch", f"{py_d['lines_covered']}/{py_d['lines_valid']}"))

        js_json = td / "js.json"
        ok_js, js_d = _run_json(
            [sys.executable, str(ROOT / "scripts/js_coverage.py"), "--json", str(js_json), "--fail-under", "80", "--min-file", "80"],
            js_json,
        )
        failed = failed or not ok_js
        if js_d:
            rows.append(("JavaScript (app/*.html)", f"{js_d['line_pct']:.2f}% line", f"{js_d['lines_covered']}/{js_d['lines_valid']}"))

        if sys.platform == "darwin" and not args.skip_swift:
            sw_json = td / "sw.json"
            cmd = [sys.executable, str(ROOT / "scripts/swift_coverage.py"), "--json", str(sw_json)]
            if args.include_ui:
                cmd.append("--include-ui")
            ok_sw, sw_d = _run_json(cmd, sw_json)
            failed = failed or not ok_sw
            if sw_d:
                rows.append(("Swift (core logic)", f"{sw_d['core_logic_pct']:.2f}% line", f"{sw_d['core_logic_covered']}/{sw_d['core_logic_valid']}"))
                rows.append(("Swift (all prod)", f"{sw_d['line_pct']:.2f}% line", f"{sw_d['lines_covered']}/{sw_d['lines_valid']}"))

    print("\n================ UNIFIED COVERAGE SUMMARY ================")
    for layer, pct, ratio in rows:
        print(f"  {layer:26s} {pct:30s} ({ratio})")
    print("==========================================================")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
