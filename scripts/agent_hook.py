#!/usr/bin/env python3
"""Deterministic lifecycle hook runner for coding agents (Antigravity & Claude Code).

Modes:
  --agy-pre-tool     Antigravity PreToolUse (run_command): blocks `--no-verify` / `core.hooksPath` bypasses.
  --agy-stop         Antigravity Stop: if tracked edits exist and fast lint/safety fails,
                     asks the agent to fix them before stopping (never loops when continuationActive).
  --claude-pre-bash  Claude Code PreToolUse (Bash): blocks `--no-verify` / `core.hooksPath` bypasses.
  --claude-post-edit Claude Code PostToolUse (Edit|Write): fast lint/safety on the edited file.
"""
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_GLOBAL_VAL_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
GIT_SUB_VAL_FLAGS = {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
                     "--fixup", "--squash", "--author", "--date", "-t", "--template", "--trailer",
                     "-o", "--push-option", "--repo", "--receive-pack", "--exec"}
SHORT_VAL_CHARS = set("mFcCtuo")


def _split_shell_segments(cmd):
    """Split a shell command string into token lists per simple command (respecting quotes and continuations)."""
    cleaned = re.sub(r"\\\r?\n", " ", cmd or "")
    lex = shlex.shlex(cleaned, posix=True, punctuation_chars="|&;\n")
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        tokens = cleaned.split()
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in ("|", "||", "&", "&&", ";", "\n"):
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _strip_env_wrappers(seg):
    """Strip leading env assignments and wrapper commands (env, command, sudo) before checking for `git`."""
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok in ("env", "command", "nohup", "time"):
            i += 1
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
            continue
        break
    return seg[i:]


def _parse_git_segment(seg):
    """Return (subcommand, sub_args, bypass_reason) for a `git` segment, or (None, [], None)."""
    seg = _strip_env_wrappers(seg)
    if not seg or Path(seg[0]).name != "git":
        return None, [], None
    i = 1
    while i < len(seg):
        tok = seg[i]
        if tok == "-c" and i + 1 < len(seg):
            if seg[i + 1].lower().startswith("core.hookspath"):
                return None, [], "Never disable git hooks via `-c core.hooksPath=...`."
            i += 2
            continue
        if tok.startswith("-c") and tok[2:].lower().startswith("core.hookspath"):
            return None, [], "Never disable git hooks via `-c core.hooksPath=...`."
        if tok in GIT_GLOBAL_VAL_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok, seg[i + 1:], None
    return None, [], None


def _short_flags_contain_n(tok):
    """True if a combined short option like `-n` or `-anm` includes `-n` before any value-taking flag."""
    for ch in tok[1:]:
        if ch == "n":
            return True
        if ch in SHORT_VAL_CHARS:
            return False
    return False


def detect_hook_bypass(cmd):
    """Return a denial reason if `cmd` attempts to bypass git hooks (`--no-verify`, `commit -n`, `core.hooksPath`)."""
    for seg in _split_shell_segments(cmd):
        sub, args, reason = _parse_git_segment(seg)
        if reason:
            return reason
        if sub not in ("commit", "push"):
            continue
        i = 0
        while i < len(args):
            tok = args[i]
            if tok == "--":
                break
            if tok == "--no-verify":
                return "Never bypass git hooks with --no-verify; fix the failing check instead."
            if sub == "commit" and tok.startswith("-") and not tok.startswith("--") and _short_flags_contain_n(tok):
                return "Never bypass git hooks with `git commit -n` (--no-verify); fix the failing check instead."
            if tok in GIT_SUB_VAL_FLAGS:
                i += 2
                continue
            i += 1
    return None


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
        combined = "\n".join(part for part in (p.stdout.strip(), p.stderr.strip()) if part)
        return p.returncode, combined
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def _has_dirty_tracked(cwd):
    code, out = _run_cmd(["git", "status", "--porcelain", "-uno"], cwd, timeout=10)
    return code == 0 and bool(out.strip())


def _fast_lint(cwd):
    errs = []
    cwd = Path(cwd)
    if shutil.which("ruff"):
        targets = [t for t in ("scripts/", "tests/", "app/kokoro.py") if (cwd / t).exists()]
        if targets:
            code, out = _run_cmd(["ruff", "check", *targets], cwd)
            if code != 0:
                errs.append(f"ruff failed:\n{out[:600]}")
    safety = cwd / "scripts" / "check_safety.py"
    if safety.is_file():
        code, out = _run_cmd([sys.executable, str(safety)], cwd)
        if code != 0:
            errs.append(f"check_safety failed:\n{out[:600]}")
    if shutil.which("shellcheck"):
        sh_candidates = [cwd / "app" / "build.sh", *sorted((cwd / "scripts").glob("*.sh")),
                         cwd / ".githooks" / "pre-commit", cwd / ".githooks" / "pre-push"]
        sh_files = [str(p) for p in sh_candidates if p.is_file()]
        if sh_files:
            code, out = _run_cmd(["shellcheck", "-S", "warning", *sh_files], cwd)
            if code != 0:
                errs.append(f"shellcheck failed:\n{out[:600]}")
    return errs


def handle_agy_pre_tool(payload):
    args = ((payload.get("toolCall") or {}).get("args")) or {}
    reason = detect_hook_bypass(str(args.get("CommandLine") or ""))
    if reason:
        return {"decision": "deny", "reason": reason}
    # Never auto-approve arbitrary commands; defer to permission prompts/cache.
    return {"decision": "ask"}


def handle_agy_stop(payload, root=ROOT):
    if payload.get("continuationActive"):
        return {}
    if payload.get("terminationReason") not in (None, "", "NO_TOOL_CALL"):
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
    reason = detect_hook_bypass(cmd)
    if reason:
        return 2, f"Blocked: {reason}"
    return 0, ""


def handle_claude_post_edit(payload, root=ROOT):
    fpath = str(((payload.get("tool_input") or {}).get("file_path")) or "")
    if not fpath:
        return 0, ""
    cwd = Path(payload.get("cwd") or root).resolve()
    p = Path(fpath)
    if not p.is_absolute():
        p = cwd / p
    p = p.resolve()
    if not p.is_file() or not p.is_relative_to(cwd):
        return 0, ""
    errs = []
    if p.suffix == ".py" and shutil.which("ruff"):
        code, out = _run_cmd(["ruff", "check", str(p)], cwd)
        if code != 0:
            errs.append(out[:600])
    elif (p.suffix == ".sh" or p.parent.name == ".githooks") and shutil.which("shellcheck"):
        code, out = _run_cmd(["shellcheck", "-S", "warning", str(p)], cwd)
        if code != 0:
            errs.append(out[:600])
    if p.suffix in (".py", ".swift", ".html", ".cjs", ".js") and (cwd / "scripts" / "check_safety.py").is_file():
        code, out = _run_cmd([sys.executable, str(cwd / "scripts" / "check_safety.py")], cwd)
        if code != 0:
            errs.append(out[:600])
    return (2, "\n".join(errs)) if errs else (0, "")


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
    code, msg = handle_claude_pre_bash(payload) if args.claude_pre_bash else handle_claude_post_edit(payload)
    if msg:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
