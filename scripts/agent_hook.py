#!/usr/bin/env python3
"""Deterministic lifecycle hook runner for coding agents (Antigravity & Claude Code).

Modes:
  --agy-pre-tool     Antigravity PreToolUse (run_command): blocks `--no-verify` bypasses.
  --agy-stop         Antigravity Stop: if tracked edits exist and fast lint/safety fails,
                     asks the agent to fix them before stopping (never loops when continuationActive).
  --claude-pre-bash  Claude Code PreToolUse (Bash): blocks `--no-verify` bypasses.
  --claude-post-edit Claude Code PostToolUse (Edit|Write): fast lint/safety on the edited file.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NO_VERIFY_RE = re.compile(r"\bgit\s+(?:commit|push)\b[^\n;|&]*--no-verify\b")


def _read_payload():
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _run_cmd(cmd, cwd, timeout=25):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stderr or p.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def _has_dirty_tracked(cwd):
    code, out = _run_cmd(["git", "status", "--porcelain", "-uno"], cwd, timeout=10)
    return code == 0 and bool(out.strip())


def _fast_lint(cwd):
    errs = []
    if shutil.which("ruff"):
        targets = [t for t in ("scripts/", "tests/", "app/kokoro.py") if (Path(cwd) / t).exists()]
        if targets:
            code, out = _run_cmd(["ruff", "check", *targets], cwd)
            if code != 0:
                errs.append(f"ruff failed:\n{out[:600]}")
    safety = Path(cwd) / "scripts" / "check_safety.py"
    if safety.is_file():
        code, out = _run_cmd([sys.executable, str(safety)], cwd)
        if code != 0:
            errs.append(f"check_safety failed:\n{out[:600]}")
    if shutil.which("shellcheck"):
        sh_files = [str(p) for p in [Path(cwd) / "app" / "build.sh", *sorted((Path(cwd) / "scripts").glob("*.sh"))] if p.is_file()]
        if sh_files:
            code, out = _run_cmd(["shellcheck", "-S", "warning", *sh_files], cwd)
            if code != 0:
                errs.append(f"shellcheck failed:\n{out[:600]}")
    return errs


def handle_agy_pre_tool(payload):
    args = ((payload.get("toolCall") or {}).get("args")) or {}
    cmd = str(args.get("CommandLine") or "")
    if NO_VERIFY_RE.search(cmd):
        return {"decision": "deny", "reason": "Never bypass git hooks with --no-verify; fix the failing check instead."}
    return {"decision": "allow"}


def handle_agy_stop(payload, root=ROOT):
    if payload.get("continuationActive") or payload.get("terminationReason") in ("USER_CANCELED", "ERROR"):
        return {}
    cwd = Path(payload.get("cwd") or root)
    if not (cwd / "scripts" / "check_safety.py").is_file() or not _has_dirty_tracked(cwd):
        return {}
    errs = _fast_lint(cwd)
    if errs:
        return {
            "decision": "continue",
            "reason": "Local lint/safety checks failed after workspace edits. Fix them before finishing:\n\n" + "\n\n".join(errs),
        }
    return {}


def handle_claude_pre_bash(payload):
    cmd = str(((payload.get("tool_input") or {}).get("command")) or "")
    if NO_VERIFY_RE.search(cmd):
        return 2, "Blocked: never bypass git hooks with --no-verify; fix the failing check instead."
    return 0, ""


def handle_claude_post_edit(payload, root=ROOT):
    fpath = str(((payload.get("tool_input") or {}).get("file_path")) or "")
    if not fpath:
        return 0, ""
    p = Path(fpath)
    if not p.is_file():
        return 0, ""
    cwd = Path(payload.get("cwd") or root)
    errs = []
    if p.suffix == ".py" and shutil.which("ruff"):
        code, out = _run_cmd(["ruff", "check", str(p)], cwd)
        if code != 0:
            errs.append(out[:600])
    elif p.suffix == ".sh" and shutil.which("shellcheck"):
        code, out = _run_cmd(["shellcheck", "-S", "warning", str(p)], cwd)
        if code != 0:
            errs.append(out[:600])
    if p.suffix in (".py", ".swift", ".html", ".cjs", ".js") and (cwd / "scripts" / "check_safety.py").is_file():
        code, out = _run_cmd([sys.executable, str(cwd / "scripts" / "check_safety.py")], cwd)
        if code != 0:
            errs.append(out[:600])
    if errs:
        return 2, "\n".join(errs)
    return 0, ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Greenlight coding-agent lifecycle hook runner.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--agy-pre-tool", action="store_true")
    group.add_argument("--agy-stop", action="store_true")
    group.add_argument("--claude-pre-bash", action="store_true")
    group.add_argument("--claude-post-edit", action="store_true")
    args = ap.parse_args(argv)
    payload = _read_payload()

    if args.agy_pre_tool:
        print(json.dumps(handle_agy_pre_tool(payload)))
        return 0
    if args.agy_stop:
        print(json.dumps(handle_agy_stop(payload)))
        return 0
    if args.claude_pre_bash:
        code, msg = handle_claude_pre_bash(payload)
        if msg:
            print(msg, file=sys.stderr)
        return code
    code, msg = handle_claude_post_edit(payload)
    if msg:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
