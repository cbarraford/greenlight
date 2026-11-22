#!/usr/bin/env python3
"""Compile Greenlight Swift sources with LLVM coverage mapping, run offline Swift suites, and report coverage.

Runs:
  - `--unittest` (deterministic offline Swift unit suite)
  - `--vimtest` (Vim engine unit suite)
  - `--attachtest` (isolated attachment suite with stubbed claude binary)
  - `--uitest` (optional, via `--include-ui`)

Usage:
    python3 scripts/swift_coverage.py [--include-ui] [--json out.json] [--lcov out.info] [--fail-under 80]
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CORE_LOGIC_FILES = frozenset({
    "Config.swift",
    "Models.swift",
    "Vim.swift",
    "Core.swift",
    "VoiceEngine.swift",
    "UITest.swift",
})
EXCLUDE_PROD = frozenset({"SelfTest.swift"})


def _find_tool(name: str) -> list[str] | None:
    if shutil.which("xcrun"):
        p = subprocess.run(["xcrun", "--find", name], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return ["xcrun", name]
    found = shutil.which(name)
    return [found] if found else None


def _arch_prefix() -> list[str]:
    if platform.machine() == "arm64":
        return []
    p = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
    return ["arch", "-arm64"] if p.returncode == 0 and p.stdout.strip() == "1" else []


def _setup_stub_home(base: Path) -> dict[str, str]:
    home = base / "home"
    bindir = base / "bin"
    home.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claude"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "prompt = sys.stdin.read()\n"
        "if 'Say one word.' in prompt:\n"
        "    time.sleep(2.0)\n"
        "reply = 'Codeword is FENNEL-4821, square.png is red, got it.'\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': reply}]}}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': reply, 'duration_ms': 10}), flush=True)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    (home / "config.json").write_text(json.dumps({"harnesses": {"claude": {"bin": str(stub)}}}) + "\n", encoding="utf-8")
    return {
        **os.environ,
        "GREENLIGHT_HOST": "local",
        "GREENLIGHT_CODE": str(ROOT),
        "GREENLIGHT_HOME": str(home),
    }


def parse_llvm_cov_export(data: dict[str, Any]) -> dict[str, Any]:
    """Parse `llvm-cov export -summary-only` JSON into per-file and logic/all totals."""
    files: dict[str, dict[str, Any]] = {}
    all_cov = all_tot = 0
    logic_cov = logic_tot = 0
    for block in data.get("data") or []:
        for fobj in block.get("files") or []:
            fpath = Path(fobj.get("filename", ""))
            fname = fpath.name
            if not fname.endswith(".swift") or fname in EXCLUDE_PROD or fpath.parent.name == "Tests":
                continue
            lines = (fobj.get("summary") or {}).get("lines") or {}
            covered = int(lines.get("covered", 0))
            count = int(lines.get("count", 0))
            pct = round((covered / count * 100.0) if count else 0.0, 2)
            files[f"app/Sources/{fname}"] = {
                "covered": covered,
                "total": count,
                "pct": pct,
                "core_logic": fname in CORE_LOGIC_FILES,
            }
            all_cov += covered
            all_tot += count
            if fname in CORE_LOGIC_FILES:
                logic_cov += covered
                logic_tot += count
    return {
        "line_pct": round((all_cov / all_tot * 100.0) if all_tot else 0.0, 2),
        "lines_covered": all_cov,
        "lines_valid": all_tot,
        "core_logic_pct": round((logic_cov / logic_tot * 100.0) if logic_tot else 0.0, 2),
        "core_logic_covered": logic_cov,
        "core_logic_valid": logic_tot,
        "files": dict(sorted(files.items())),
    }


def _run_step(label: str, cmd: list[str], env: dict[str, str]) -> bool:
    print(f"=== {label} ===", flush=True)
    p = subprocess.run(cmd, cwd=ROOT, env=env)
    if p.returncode != 0:
        print(f"FAIL {label} (exit {p.returncode})", file=sys.stderr)
        return False
    return True


def _build_runs(binary: Path, prof_dir: Path, stub_env: dict[str, str], skip_attach: bool, include_ui: bool) -> list[tuple[str, list[str], dict[str, str]]]:
    runs: list[tuple[str, list[str], dict[str, str]]] = [
        ("Swift --unittest", [str(binary), "--unittest"], {**stub_env, "LLVM_PROFILE_FILE": str(prof_dir / "unit-%p.profraw")}),
        ("Swift --vimtest", [str(binary), "--vimtest"], {**stub_env, "LLVM_PROFILE_FILE": str(prof_dir / "vim-%p.profraw")}),
    ]
    if not skip_attach:
        runs.append(("Swift --attachtest", [str(binary), "--attachtest"], {**stub_env, "LLVM_PROFILE_FILE": str(prof_dir / "attach-%p.profraw")}))
    if include_ui:
        runs.append((
            "Swift --uitest",
            [sys.executable, str(ROOT / "tests" / "test_uitest.py")],
            {**os.environ, "UITEST_BINARY": str(binary), "LLVM_PROFILE_FILE": str(prof_dir / "ui-%p.profraw")},
        ))
    return runs


def _check_swift_thresholds(summary: dict[str, Any], fail_under: float | None, fail_under_logic: float | None) -> list[str]:
    errs: list[str] = []
    if fail_under is not None and summary["line_pct"] < fail_under:
        errs.append(f"Swift total coverage {summary['line_pct']:.2f}% < required {fail_under:.2f}%")
    if fail_under_logic is not None and summary["core_logic_pct"] < fail_under_logic:
        errs.append(f"Swift core logic coverage {summary['core_logic_pct']:.2f}% < required {fail_under_logic:.2f}%")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run instrumented Swift tests and report llvm-cov coverage.")
    ap.add_argument("--include-ui", action="store_true", help="Also run tests/test_uitest.py with coverage")
    ap.add_argument("--skip-attach", action="store_true", help="Skip --attachtest (only run --unittest and --vimtest)")
    ap.add_argument("--json", default=None, help="Write coverage summary JSON to this path")
    ap.add_argument("--lcov", default=None, help="Write LCOV report to this path")
    ap.add_argument("--fail-under", type=float, default=None, help="Fail if total Swift line coverage is below this %%")
    ap.add_argument("--fail-under-logic", type=float, default=None, help="Fail if core logic Swift coverage is below this %%")
    args = ap.parse_args(argv)

    if sys.platform != "darwin":
        print("skip swift_coverage.py (macOS only)")
        return 0
    swiftc, profdata, llvm_cov = _find_tool("swiftc"), _find_tool("llvm-profdata"), _find_tool("llvm-cov")
    if not (swiftc and profdata and llvm_cov):
        print("FAIL xcrun swiftc / llvm-profdata / llvm-cov required (install Xcode)", file=sys.stderr)
        return 2

    prod_srcs = sorted(str(p) for p in (ROOT / "app" / "Sources").glob("*.swift"))
    test_srcs = sorted(str(p) for p in (ROOT / "app" / "Tests").glob("*.swift")) if (ROOT / "app" / "Tests").is_dir() else []
    srcs = [*prod_srcs, *test_srcs]
    with tempfile.TemporaryDirectory(prefix="gl-swiftcov-") as tmp:
        tmp_dir = Path(tmp)
        binary, prof_dir = tmp_dir / "Greenlight-cov", tmp_dir / "prof"
        prof_dir.mkdir()
        compile_cmd = [
            *_arch_prefix(), *swiftc, "-Onone", "-DGREENLIGHT_TESTS",
            "-profile-generate", "-profile-coverage-mapping", *srcs, "-o", str(binary),
        ]
        if not _run_step("swiftc (coverage-instrumented)", compile_cmd, dict(os.environ)):
            return 1
        runs = _build_runs(binary, prof_dir, _setup_stub_home(tmp_dir), args.skip_attach, args.include_ui)
        if any(not _run_step(label, cmd, env) for label, cmd, env in runs):
            return 1

        raws = sorted(str(p) for p in prof_dir.glob("*.profraw"))
        merged = tmp_dir / "merged.profdata"
        subprocess.run([*profdata, "merge", "-sparse", *raws, "-o", str(merged)], check=True)
        exp = subprocess.run([*llvm_cov, "export", str(binary), f"-instr-profile={merged}", "-summary-only"],
                             capture_output=True, text=True, check=True)
        summary = parse_llvm_cov_export(json.loads(exp.stdout))
        if args.lcov:
            lcov = subprocess.run([*llvm_cov, "export", str(binary), f"-instr-profile={merged}", "-format=lcov", *prod_srcs],
                                  capture_output=True, text=True, check=True)
            Path(args.lcov).write_text(lcov.stdout, encoding="utf-8")

    print("\n=== Swift Line Coverage (app/Sources/*.swift) ===")
    for rel, info in summary["files"].items():
        tag = "[logic]" if info["core_logic"] else "[ui]   "
        print(f"  {tag} {rel:28s} {info['pct']:6.2f}%  ({info['covered']:4d}/{info['total']:4d})")
    print(f"  CORE LOGIC TOTAL                     {summary['core_logic_pct']:6.2f}%  ({summary['core_logic_covered']:4d}/{summary['core_logic_valid']:4d})")
    print(f"  ALL SWIFT TOTAL                      {summary['line_pct']:6.2f}%  ({summary['lines_covered']:4d}/{summary['lines_valid']:4d})")

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    errs = _check_swift_thresholds(summary, args.fail_under, args.fail_under_logic)
    for e in errs:
        print(f"COVERAGE FAIL: {e}", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
