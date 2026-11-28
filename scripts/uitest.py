#!/usr/bin/env python3
"""uitest: parallel-safe background UI test driver for Greenlight (--uitest mode).

Launches isolated Greenlight instances in the background (no Dock activation, no focus steal,
click-through windows, isolated GREENLIGHT_HOME / UserDefaults / tmux socket) and drives them
over an owner-only (0600) AF_UNIX JSON-RPC socket.

CLI usage:
    python3 scripts/uitest.py start [--fixture default|empty|adversarial|scale|chaos|corrupt] [--binary PATH]
    python3 scripts/uitest.py ping       --socket SOCK
    python3 scripts/uitest.py state      --socket SOCK
    python3 scripts/uitest.py action     --socket SOCK <name> [--to FLOOR] [--path P] [--text T] [--width W] [--height H]
    python3 scripts/uitest.py eval       --socket SOCK --target dashboard|terminal|stage "<js>"
    python3 scripts/uitest.py click      --socket SOCK --target dashboard|terminal "<css-selector>"
    python3 scripts/uitest.py type       --socket SOCK --target dashboard|terminal "<css-selector>" "<text>"
    python3 scripts/uitest.py screenshot --socket SOCK --window dashboard|terminal|settings|active --out FILE.png
    python3 scripts/uitest.py wait       --socket SOCK --target dashboard|terminal "<js-predicate>" [--timeout 10]
    python3 scripts/uitest.py stop       --socket SOCK
"""
import argparse
import contextlib
import hashlib
import json
import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_CACHE = Path(tempfile.gettempdir()) / f"greenlight-uitest-bin-{os.getuid()}"
FIXTURES = ("default", "empty", "adversarial", "scale", "chaos", "corrupt", "projects")


def swift_sources(include_tests: bool = True):
    d = ROOT / "app" / "Sources"
    if d.is_dir():
        srcs = sorted(d.glob("*.swift"))
        tests_dir = ROOT / "app" / "Tests"
        if include_tests and tests_dir.is_dir():
            srcs.extend(sorted(tests_dir.glob("*.swift")))
        return srcs
    return [ROOT / "app" / "Greenlight.swift"]


def build_test_binary():
    """Compile an unoptimized scratch Greenlight test binary cached by Swift source hash."""
    srcs = swift_sources(include_tests=True)
    h = hashlib.sha256(b"GREENLIGHT_TESTS\n")
    for p in srcs:
        rel = str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name
        h.update(rel.encode())
        h.update(p.read_bytes())
    key = h.hexdigest()[:16]
    BIN_CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BIN_CACHE, 0o700)
    out = BIN_CACHE / f"Greenlight-{key}"
    if out.is_file() and os.access(out, os.X_OK):
        return out
    tmp = BIN_CACHE / f"Greenlight-{key}.{os.getpid()}"
    cmd = ["swiftc", "-Onone", "-DGREENLIGHT_TESTS", *[str(p) for p in srcs], "-o", str(tmp)]
    if platform.machine() == "arm64":
        cmd = ["arch", "-arm64", *cmd]
    env = {**os.environ, "DEVELOPER_DIR": "/Library/Developer/CommandLineTools"}
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"swiftc failed:\n{p.stderr or p.stdout}")
    os.replace(tmp, out)
    return out


def rpc(sock_path, payload, timeout=30.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = bytearray()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        line = bytes(buf).split(b"\n", 1)[0]
        if not line:
            raise ConnectionResetError(f"EOF from --uitest socket {sock_path}")
        try:
            return json.loads(line.decode(errors="replace"))
        except ValueError as e:
            raise ConnectionResetError(f"malformed JSON from --uitest socket {sock_path}: {e}") from e
    finally:
        s.close()


def _install_stub_harnesses(bindir: Path):
    """Install deterministic offline stub agent CLIs so UI fuzzing never spends real API tokens."""
    # Mode-aware Claude stub: emits stream-json when --output-format stream-json is requested (chat.py),
    # valid Insights layout JSON when asked for JSON layout (dash.py design), JSON array for ask_user,
    # and concise 2-line label+prompt for dash.py suggest.
    claude_stub = bindir / "claude"
    claude_stub.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        "  *' stream-json '*)\n"
        "    BODY=$(cat 2>/dev/null || true)\n"
        "    case \"$BODY\" in\n"  # a prompt asking for a table gets one (the chat canvas, #208)
        "      *uitest-table*) R='Stub Chief reply with a table.\\n\\n| MR | CI |\\n|---|---|\\n| !12 | passed |\\n| !13 | failed |' ;;\n"
        "      *) R='Stub Chief reply (uitest offline).' ;;\n"
        "    esac\n"
        "    printf '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"%s\"}]}}\\n' \"$R\"\n"
        "    printf '{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"%s\",\"session_id\":\"uitest-stub-sid\"}\\n' \"$R\"\n"
        "    ;;\n"
        "  *)\n"
        "    BODY=$(cat 2>/dev/null || true)\n"
        "    case \"$BODY\" in\n"
        "      *'Return ONLY JSON'*)\n"
        "        printf '{\"puck\":null,\"tiles\":[{\"widget\":\"note\",\"title\":\"Status\",\"props\":{\"text\":\"Stub insight (uitest offline).\",\"tone\":\"muted\"}}],\"wide\":null}\\n'\n"
        "        ;;\n"
        "      *'JSON array of strings'*)\n"
        "        printf '[\"Yes, proceed\",\"Hold off\"]\\n'\n"
        "        ;;\n"
        "      *)\n"
        "        printf 'Check workspace\\nReview the latest changes and run tests.\\n'\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n"
    )
    claude_stub.chmod(0o755)

    codex_stub = bindir / "codex"
    codex_stub.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null 2>&1 || true\n"
        "printf '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Stub Codex reply (uitest offline).\"}}\\n'\n"
        "printf '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":10,\"output_tokens\":5}}\\n'\n"
    )
    codex_stub.chmod(0o755)

    for name in ("gemini", "opencode", "pi", "agy"):
        p = bindir / name
        p.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null 2>&1 || true\n"
            f"printf 'Stub {name} reply (uitest offline).\\n'\n"
        )
        p.chmod(0o755)


def _native_worktrees(root, home, worktrees, rows):
    """Give the UI fixtures real, isolated Git worktrees for the native backend."""
    root = Path(root).resolve()
    mains, goals, ids = {}, {}, {}
    for i, w in enumerate(worktrees):
        path = Path(w["path"])
        if not path.is_dir():
            continue  # the corrupt fixture intentionally includes a missing checkout
        path = path.resolve()
        name = str(w.get("repo") or "widgets")
        if name not in mains:
            repos_dir = (root / "repos").resolve()
            main = (repos_dir / name).resolve()
            if main == repos_dir or not main.is_relative_to(repos_dir):
                raise ValueError(f"fixture repo escapes repos dir: {name!r}")
            main.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(main), "remote", "add", "origin", f"https://github.com/acme/{name}.git"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(main), "-c", "user.name=uitest", "-c", "user.email=uitest@localhost",
                            "commit", "-q", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
            mains[name] = main
        branch = str(w.get("branch") or "").removeprefix("refs/heads/")
        valid = bool(branch) and branch != "@" and subprocess.run(
            ["git", "check-ref-format", "--branch", branch], capture_output=True
        ).returncode == 0
        branch = branch if valid else f"fixture-{i}"
        path.rmdir()
        subprocess.run(["git", "-C", str(mains[name]), "worktree", "add", "-q", "-b", branch, "--", str(path), "main"],
                       check=True, capture_output=True)
        path.mkdir(parents=True, exist_ok=True)  # a mocked git in a unit test does not create it
        real = str(path.resolve())
        ids[w["worktreeId"]] = real
        w.update(id=real, worktreeId=real, path=real, repoId=str(mains[name]), branch=branch)
        if isinstance(w.get("comment"), str) and w["comment"]:
            goals[real] = w["comment"]
    for row in rows:
        if row.get("id") in ids:
            real = ids[row["id"]]
            row["id"] = real
            row["path"] = real
    (home / "repos.json").write_text(json.dumps([str(p) for p in mains.values()]))
    (home / "goals.json").write_text(json.dumps(goals))
    (home / "imported.json").write_text("{}")


def _make_fixture(root: Path, fixture: str):
    root = Path(root).resolve()
    home = root / "home"
    bindir = root / "bin"
    webdir = root / "web"
    wsdir = root / "worktrees"
    for d in (home, bindir, webdir, wsdir):
        d.mkdir(parents=True, exist_ok=True)

    # Copy bundled HTML pages & ghostty-web.js side by side so terminal.html finds ghostty-web.js
    for name in ("dashboard.html", "terminal.html", "stage.html"):
        shutil.copy2(ROOT / "app" / name, webdir / name)
    shutil.copy2(ROOT / "app" / "vendor" / "ghostty-web.js", webdir / "ghostty-web.js")

    # Fixture workspace data
    worktrees: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    if fixture == "empty":
        worktrees, rows = [], []
    elif fixture == "adversarial":
        xss = '"><img src=x onerror="window.__pwned=1">'
        ws_path = str(wsdir / f"xss-{xss}")
        Path(ws_path).mkdir(exist_ok=True)
        worktrees = [{
            "worktreeId": "wt-xss", "displayName": f"xss-{xss}", "path": ws_path,
            "repo": "widgets", "branch": f"feat/{xss}", "comment": f"Test {xss}",
            "agents": [{"state": "waiting", "agentType": "claude", "lastAssistantMessage": xss}],
        }]
        rows = [{
            "id": "wt-xss", "name": f"xss-{xss}", "path": ws_path, "project": "acme/widgets",
            "goal": f"Test {xss}", "branch": f"feat/{xss}",
            "agent": {"state": "waiting", "type": "claude", "last": xss, "since": 1700000000000},
            "mr": {"ref": "!99", "title": xss, "state": "opened", "web_url": "https://example.invalid/mr/99"},
            "next": {"step": "review", "owner": "me", "action": xss, "cmd": []},
        }]
    elif fixture == "scale":
        repos = ("widgets", "payments", "infra", "console")
        states = ("waiting", "working", "finished", "idle", "rate_limited")
        steps = ("review", "ci", "merge", "draft", "address", "deploy")
        pipes = ("passed", "running", "failed", "pending")
        worktrees, rows = [], []
        for i in range(50):
            repo = repos[i % len(repos)]
            st = states[i % len(states)]
            step = steps[i % len(steps)]
            pipe = pipes[i % len(pipes)]
            name = f"ws-{repo}-{i:02d}"
            ws_path = str(wsdir / name)
            Path(ws_path).mkdir(exist_ok=True)
            agent_type = "claude" if i % 2 == 0 else "codex"
            last_msg = f"Completed step {step} on {name}; " + ("checking edge cases and logs. " * ((i % 4) + 1))
            goal = f"Ship {name} and merge !{100 + i}" + (" (parked)" if i % 11 == 0 else "")
            worktrees.append({
                "worktreeId": f"wt-{i:02d}", "displayName": name, "path": ws_path,
                "repo": repo, "branch": f"feat/{name}", "comment": goal,
                "agents": [{"state": st, "agentType": agent_type, "lastAssistantMessage": last_msg}],
            })
            rows.append({
                "id": f"wt-{i:02d}", "name": name, "path": ws_path, "project": f"acme/{repo}",
                "goal": goal,
                "branch": f"feat/{name}",
                "agent": {"state": st, "type": agent_type, "last": last_msg, "since": 1700000000000 + i * 60000},
                "mr": {"ref": f"!{100 + i}", "title": f"Change {i:02d}: {name}", "state": "opened",
                       "pipeline": pipe, "draft": step == "draft", "web_url": f"https://example.invalid/mr/{100 + i}"},
                "next": {"step": step, "owner": "me" if i % 3 == 0 else "agent",
                         "action": f"Handle {step} for !{100 + i}", "cmd": []},
            })
    elif fixture == "corrupt":
        rtl_name = "rtl-\u202e-test-\U0001f680-" + ("x" * 90)
        ws_ok = str(wsdir / rtl_name)
        Path(ws_ok).mkdir(exist_ok=True)
        worktrees = [
            {
                "worktreeId": "wt-ok", "displayName": rtl_name, "path": ws_ok,
                "repo": "widgets", "branch": "feat/unicode-\u00e9-\u4e2d\u6587", "comment": "",
                "agents": [{"state": "waiting", "agentType": "claude", "lastAssistantMessage": None}],
            },
            {
                "worktreeId": "wt-missing", "displayName": "ghost-dir", "path": str(wsdir / "does-not-exist"),
                "repo": "widgets", "branch": "", "comment": None, "agents": [],
            },
        ]
        rows = [
            {
                "id": "wt-ok", "name": rtl_name, "path": ws_ok, "project": "acme/widgets",
                "goal": None, "branch": "feat/unicode-\u00e9-\u4e2d\u6587",
                "agent": {"state": "waiting", "type": "claude", "last": None},
                "mr": None,
                "next": {"step": "unknown", "owner": None, "action": ""},
            },
            {
                "id": "wt-missing", "name": "ghost-dir", "path": str(wsdir / "does-not-exist"),
                "project": "", "goal": "broken", "branch": "",
            },
        ]
        state_dir = home / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "wt-ok.json").write_text("{truncated json")
        (state_dir / "ghost-dir.json").write_text("[]")
    else:  # default, chaos or projects
        ws_a = str(wsdir / "fix-auth")
        ws_b = str(wsdir / "add-metrics")
        Path(ws_a).mkdir(exist_ok=True)
        Path(ws_b).mkdir(exist_ok=True)
        worktrees = [
            {
                "worktreeId": "wt-a", "displayName": "fix-auth", "path": ws_a, "repo": "widgets",
                "branch": "fix-auth", "comment": "Fix OAuth token refresh and merge !42",
                "agents": [{"state": "waiting", "agentType": "claude", "lastAssistantMessage": "Tests pass. Ready to mark MR !42 ready?"}],
            },
            {
                "worktreeId": "wt-b", "displayName": "add-metrics", "path": ws_b, "repo": "widgets",
                "branch": "add-metrics", "comment": "Add Prometheus counters for queue latency",
                "agents": [{"state": "working", "agentType": "codex", "toolName": "Bash", "toolInput": "pytest -q", "lastAssistantMessage": "Running test suite"}],
            },
        ]
        rows = [
            {
                "id": "wt-a", "name": "fix-auth", "path": ws_a, "project": "acme/widgets",
                "goal": "Fix OAuth token refresh and merge !42", "branch": "fix-auth",
                "agent": {"state": "waiting", "type": "claude", "last": "Tests pass. Ready to mark MR !42 ready?", "since": 1700000000000},
                "mr": {"ref": "!42", "title": "Fix OAuth token refresh", "state": "opened", "pipeline": "passed", "web_url": "https://example.invalid/mr/42"},
                "next": {"step": "review", "owner": "me", "action": "Approve and merge !42", "cmd": []},
            },
            {
                "id": "wt-b", "name": "add-metrics", "path": ws_b, "project": "acme/widgets",
                "goal": "Add Prometheus counters for queue latency", "branch": "add-metrics",
                "agent": {"state": "working", "type": "codex", "tool": "Bash", "input": "pytest -q", "last": "Running test suite", "since": 1700000000000},
                "mr": {"ref": "!43", "title": "Add queue latency metrics", "state": "opened", "pipeline": "running", "web_url": "https://example.invalid/mr/43"},
                "next": {"step": "ci", "owner": "agent", "action": "Wait for CI", "cmd": []},
            },
        ]

    if fixture == "projects":  # default's two workspaces, each in its own project (by folder), for per-window project picks
        support = home / "app-support"
        support.mkdir(parents=True, exist_ok=True)
        (support / "projects.json").write_text(json.dumps([
            {"id": "pauth", "name": "Auth", "repos": [], "dirs": [ws_a]},
            {"id": "pmetrics", "name": "Metrics", "repos": [], "dirs": [ws_b]},
        ]))

    _install_stub_harnesses(bindir)
    stub_harnesses = {n: {"bin": str(bindir / n)} for n in ("claude", "codex", "gemini", "opencode", "pi", "agy")}
    (home / "config.json").write_text(json.dumps({
        "me": "uitest-user",
        "agents": ["claude", "codex"],
        "llm_harness": "claude",
        "harnesses": stub_harnesses,
    }))
    _native_worktrees(root, home, worktrees, rows)
    (home / "last.json").write_text(json.dumps({"ts": int(time.time()), "rows": rows}))

    # Ensure every worktree entry has id & repoId so both `orca worktree ps` and `orca worktree list` work cleanly.
    for wt in worktrees:
        wt.setdefault("id", wt.get("worktreeId", "wt-0"))
        wt.setdefault("repoId", f"repo-{wt.get('repo', 'widgets')}")

    orca_data = root / "orca-ps.json"
    orca_data.write_text(json.dumps({"ok": True, "result": {"worktrees": worktrees}}))
    orca_terms = root / "orca-terms.json"
    orca_terms.write_text(json.dumps({"ok": True, "result": {"terminals": []}}))
    repo_names = sorted({str(wt.get("repo") or "widgets") for wt in worktrees} or {"widgets"})
    orca_repos = root / "orca-repos.json"
    orca_repos.write_text(json.dumps({
        "ok": True,
        "result": {
            "repos": [
                {"id": f"repo-{rn}", "displayName": rn, "gitRemoteIdentity": {"canonicalKey": f"github.com/acme/{rn}"}}
                for rn in repo_names
            ]
        },
    }))

    orca_bin = bindir / "orca"
    dispatch_ok = (
        f"case \"$1 $2\" in\n"
        f"  'terminal list'*) cat {shlex.quote(str(orca_terms))};;\n"
        f"  'repo list'*) cat {shlex.quote(str(orca_repos))};;\n"
        f"  *) cat {shlex.quote(str(orca_data))};;\n"
        f"esac\n"
    )
    if fixture == "chaos":
        # Deterministically cycles: ok -> slow + mutated worktrees -> malformed JSON -> non-zero exit -> ok
        orca_alt = root / "orca-ps-alt.json"
        ws_c = str(wsdir / "churn-ws")
        Path(ws_c).mkdir(exist_ok=True)
        alt_wts = [worktrees[0], {
            "id": "wt-c", "worktreeId": "wt-c", "displayName": "churn-ws", "path": ws_c,
            "repo": "widgets", "repoId": "repo-widgets", "branch": "feat/churn", "comment": "Transient churn workspace",
            "agents": [{"state": "rate_limited", "agentType": "claude", "lastAssistantMessage": "Rate limit hit"}],
        }]
        orca_alt.write_text(json.dumps({"ok": True, "result": {"worktrees": alt_wts}}))
        counter = root / "chaos-counter"
        orca_bin.write_text(
            "#!/bin/sh\n"
            f"if [ \"$1\" = 'terminal' ]; then cat {shlex.quote(str(orca_terms))}; exit 0; fi\n"
            f"if [ \"$1\" = 'repo' ]; then cat {shlex.quote(str(orca_repos))}; exit 0; fi\n"
            f"CFILE={shlex.quote(str(counter))}\n"
            "N=$(cat \"$CFILE\" 2>/dev/null || echo 0)\n"
            "N=$((N + 1))\n"
            "echo \"$N\" > \"$CFILE\"\n"
            "MOD=$((N % 5))\n"
            f"if [ \"$MOD\" -eq 1 ]; then cat {shlex.quote(str(orca_data))}; exit 0; fi\n"
            f"if [ \"$MOD\" -eq 2 ]; then sleep 0.35; cat {shlex.quote(str(orca_alt))}; exit 0; fi\n"
            "if [ \"$MOD\" -eq 3 ]; then echo '{broken-json'; exit 0; fi\n"
            "if [ \"$MOD\" -eq 4 ]; then echo 'transient orca error' >&2; exit 1; fi\n"
            f"cat {shlex.quote(str(orca_data))}\n"
        )
    else:
        orca_bin.write_text("#!/bin/sh\n" + dispatch_ok)
    orca_bin.chmod(0o755)

    for stub in ("glab", "gh"):
        p = bindir / stub
        if fixture == "chaos":
            cfile = shlex.quote(str(root / "chaos-counter"))
            p.write_text(
                "#!/bin/sh\n"
                f"CFILE={cfile}\n"
                "N=$(cat \"$CFILE\" 2>/dev/null || echo 0)\n"
                "N=$((N + 1))\n"
                "echo \"$N\" > \"$CFILE\"\n"
                "MOD=$((N % 4))\n"
                "if [ \"$MOD\" -eq 2 ]; then sleep 0.2; echo '[]'; exit 0; fi\n"
                "if [ \"$MOD\" -eq 3 ]; then echo 'rate limit 429' >&2; exit 1; fi\n"
                "echo '[]'\n"
            )
        else:
            p.write_text("#!/bin/sh\necho '[]'\n")
        p.chmod(0o755)

    return home, bindir, webdir


def _find_session_pids(root_dir: Path) -> set[int]:
    """Find PIDs (and their descendants) whose argv references this isolated `gl-uitest-*` root."""
    if not root_dir.name.startswith("gl-uitest-"):
        return set()
    p = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,command="], capture_output=True, text=True, timeout=5)
    if p.returncode != 0:
        return set()
    marker = str(root_dir)
    children: dict[int, list[int]] = {}
    ppid_of: dict[int, int] = {}
    seeds: list[int] = []
    for line in p.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        ppid_of[pid] = ppid
        children.setdefault(ppid, []).append(pid)
        if marker in parts[2]:
            seeds.append(pid)
    # Never signal ourselves or any ancestor up to PID 1
    protected: set[int] = set()
    cur = os.getpid()
    while cur > 1 and cur not in protected:
        protected.add(cur)
        cur = ppid_of.get(cur, 0)
    found: set[int] = set()
    stack = list(seeds)
    while stack:
        pid = stack.pop()
        if pid in found or pid in protected or pid <= 1:
            continue
        found.add(pid)
        stack.extend(children.get(pid, ()))
    return found


def _reap_session_processes(root_dir: Path, pgid: int | None = None) -> None:
    """Terminate the app's process group plus detached child sessions (e.g. chat.py) before rmtree."""
    my_pgrp = os.getpgrp()
    if pgid and pgid > 1 and pgid != my_pgrp:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
    pids = _find_session_pids(root_dir)
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        pg_alive = False
        if pgid and pgid > 1 and pgid != my_pgrp:
            try:
                os.killpg(pgid, 0)
                pg_alive = True
            except OSError:
                pass
        pids = _find_session_pids(root_dir)
        if not pg_alive and not pids:
            return
        time.sleep(0.05)
    if pgid and pgid > 1 and pgid != my_pgrp:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
    for pid in _find_session_pids(root_dir):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    time.sleep(0.05)


class UITestSession:
    """Context manager for an isolated background Greenlight --uitest instance."""

    def __init__(self, fixture="default", binary=None, timeout=20.0, record_trace=True, keep_defaults=False):
        self.fixture = fixture
        self.binary = Path(binary) if binary else None
        self.timeout = timeout
        self.record_trace = record_trace
        self.trace = []
        self.id = uuid.uuid4().hex[:8]
        self.root = Path(tempfile.mkdtemp(prefix=f"gl-uitest-{self.id}-")).resolve()
        self.socket_path = self.root / "ui.sock"
        self.tmux_socket = f"gl-uitest-{self.id}"
        self.proc = None
        self.home = None
        # keep_defaults: one UserDefaults suite for every launch of this session, so relaunch() sees what the last run saved
        self.suite = f"dev.greenlight.uitest.s{self.id}" if keep_defaults else None

    def start(self):
        try:
            binary = self.binary = self.binary or build_test_binary()
            self.home, bindir, webdir = _make_fixture(self.root, self.fixture)
            self._seed_fixture_agents()
            return self._launch(binary, bindir, webdir)
        except BaseException:
            self.stop()
            raise

    def _seed_pane(self, session: str, ws_path: str, title: str, body: str, wait_for: str | None = None) -> None:
        cmd = f"printf '\\033]2;{title}\\007{body}'; exec sleep 600"
        subprocess.run([
            "tmux", "-f", "/dev/null", "-L", self.tmux_socket, "new-session", "-d", "-s", session, "-c", ws_path,
            "sh", "-c", cmd,
        ], check=True, capture_output=True)
        subprocess.run(["tmux", "-L", self.tmux_socket, "select-pane", "-t", session, "-T", title],
                       check=True, capture_output=True)
        if not wait_for:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cap = subprocess.run(["tmux", "-L", self.tmux_socket, "capture-pane", "-p", "-t", session],
                                 capture_output=True, text=True)
            if wait_for in cap.stdout:
                break
            time.sleep(0.02)

    def _seed_fixture_agents(self):
        """Start isolated tmux agent panes needed after native rescan (adversarial and scale)."""
        if self.fixture not in ("adversarial", "scale") or not self.home or not shutil.which("tmux"):
            return
        rows = json.loads((self.home / "last.json").read_text()).get("rows") or []
        if not rows or not Path(rows[0]["path"]).is_dir():
            return
        self._seed_pane("uitest-wait", rows[0]["path"], "✳ Claude", "  ❯ 1. Proceed\\n    2. Hold\\n", wait_for="❯ 1.")
        if self.fixture == "scale" and len(rows) > 1 and Path(rows[1]["path"]).is_dir():
            self._seed_pane("uitest-work", rows[1]["path"], "⠋ Claude", "Working on changes...\\n")

    def relaunch(self):
        """Quit the app (its windows save what they show) and start it again on the same home, fixture and defaults."""
        assert self.suite, "relaunch needs keep_defaults=True"
        self._quit()
        if self.socket_path.exists():
            self.socket_path.unlink()
        return self._launch(self.binary, self.root / "bin", self.root / "web")

    def _launch(self, binary, bindir, webdir):
        env = {
            **os.environ,
            "GREENLIGHT_HOST": "local",
            "GREENLIGHT_HOME": str(self.home),
            "GREENLIGHT_CODE": str(ROOT),
            "GREENLIGHT_WEB_DIR": str(webdir),
            "GREENLIGHT_TMUX_SOCKET": self.tmux_socket,
            "GREENLIGHT_ADE": "native",
            "GREENLIGHT_USAGE": os.environ.get("GREENLIGHT_USAGE", "off"),
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        }
        env.pop("GREENLIGHT_UITEST_SUITE", None)  # never one inherited from the caller's shell: that would share its defaults
        if self.suite:
            env["GREENLIGHT_UITEST_SUITE"] = self.suite
        log = open(self.root / "app.log", "ab")  # noqa: SIM115 -- the app's stdout for its whole run; _quit() closes it
        self._log: Any = log
        self.proc = subprocess.Popen(
            [str(binary), "--uitest", "--uitest-socket", str(self.socket_path)],
            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.time() + self.timeout
        last_err = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                rc = self.proc.returncode
                log.flush()
                tail = (self.root / "app.log").read_text(errors="replace")[-1000:]
                self.stop()
                raise RuntimeError(f"Greenlight --uitest exited early ({rc}):\n{tail}")
            if self.socket_path.exists():
                try:
                    r = self.ping()
                    if r.get("ok"):
                        # Save metadata beside socket for CLI `stop` cleanup
                        (self.root / "meta.json").write_text(json.dumps({
                            "pid": self.proc.pid, "pgid": self.proc.pid, "root": str(self.root),
                            "tmux_socket": self.tmux_socket, "socket": str(self.socket_path),
                        }))
                        return self
                except (OSError, ValueError) as e:
                    last_err = e
            time.sleep(0.1)
        self.stop()
        raise TimeoutError(f"timed out waiting for --uitest socket {self.socket_path}: {last_err}")

    def _record(self, op, args, res, secs):
        if self.record_trace:
            self.trace.append({
                "seq": len(self.trace),
                "ts": round(time.time(), 3),
                "op": op,
                "args": args,
                "ok": bool(res.get("ok")) if isinstance(res, dict) else False,
                "secs": round(secs, 4),
            })

    def save_trace(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w") as f:
            f.write(json.dumps({"meta": True, "fixture": self.fixture}) + "\n")
            for step in self.trace:
                f.write(json.dumps(step) + "\n")
        return p

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ping(self):
        return rpc(self.socket_path, {"cmd": "ping"})

    def state(self):
        return rpc(self.socket_path, {"cmd": "state"})

    def action(self, name, **kwargs):
        t0 = time.monotonic()
        res = rpc(self.socket_path, {"cmd": "action", "name": name, **kwargs})
        self._record("action", {"name": name, **kwargs}, res, time.monotonic() - t0)
        return res

    def eval(self, target, js, record=True):
        t0 = time.monotonic()
        res = rpc(self.socket_path, {"cmd": "eval", "target": target, "js": js})
        if record:
            self._record("eval", {"target": target, "js": js}, res, time.monotonic() - t0)
        return res

    def click(self, target, selector):
        t0 = time.monotonic()
        js = f"(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return false; el.click(); return true; }})()"
        res = self.eval(target, js, record=False)
        self._record("click", {"target": target, "selector": selector}, res, time.monotonic() - t0)
        return res

    def type_text(self, target, selector, text):
        t0 = time.monotonic()
        js = (f"(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return false; "
              f"el.focus(); el.value = {json.dumps(text)}; "
              f"el.dispatchEvent(new Event('input', {{bubbles: true}})); "
              f"el.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }})()")
        res = self.eval(target, js, record=False)
        self._record("type", {"target": target, "selector": selector, "text": text}, res, time.monotonic() - t0)
        return res

    def screenshot(self, out_path, window="active"):
        return rpc(self.socket_path, {"cmd": "screenshot", "window": window, "path": str(Path(out_path).resolve())})

    def wait_for(self, target, js_predicate, timeout=15.0, interval=0.15):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            r = self.eval(target, js_predicate, record=False)
            last = r
            if r.get("ok") and r.get("result"):
                return r["result"]
            time.sleep(interval)
        raise TimeoutError(f"wait_for({target}, {js_predicate!r}) timed out; last={last}")

    def _reap_process_group(self, pgid: int | None) -> None:
        """Terminate any lingering child processes (e.g. dash.py stream, chat.py) before deleting tempdir."""
        _reap_session_processes(self.root, pgid)

    def stop(self):
        self._quit()
        with contextlib.suppress(FileNotFoundError):
            subprocess.run(["tmux", "-L", self.tmux_socket, "kill-server"], capture_output=True, timeout=5)
        if self.suite:  # the kept defaults: macOS keeps a plist per suite
            subprocess.run(["defaults", "delete", self.suite], capture_output=True, timeout=5)
            (Path.home() / "Library" / "Preferences" / f"{self.suite}.plist").unlink(missing_ok=True)  # `defaults delete` leaves it, empty
        shutil.rmtree(self.root, ignore_errors=True)

    def _quit(self):
        pgid = self.proc.pid if self.proc is not None else None
        if self.socket_path.exists():
            try:
                rpc(self.socket_path, {"cmd": "action", "name": "quit"}, timeout=3.0)
            except (OSError, ValueError):
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
            self.proc = None
        self._reap_process_group(pgid)
        if hasattr(self, "_log") and self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def main():
    p = argparse.ArgumentParser(description="Background Greenlight --uitest driver")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("start")
    st.add_argument("--fixture", default="default", choices=FIXTURES)
    st.add_argument("--binary", default=None)

    for name in ("ping", "state", "stop"):
        sp = sub.add_parser(name)
        sp.add_argument("--socket", required=True)

    act = sub.add_parser("action")
    act.add_argument("--socket", required=True)
    act.add_argument("name")
    act.add_argument("--to", default=None)
    act.add_argument("--path", default=None)
    act.add_argument("--text", default=None)
    act.add_argument("--window", default=None)
    act.add_argument("--width", type=float, default=None)
    act.add_argument("--height", type=float, default=None)

    ev = sub.add_parser("eval")
    ev.add_argument("--socket", required=True)
    ev.add_argument("--target", default="dashboard")
    ev.add_argument("js")

    cl = sub.add_parser("click")
    cl.add_argument("--socket", required=True)
    cl.add_argument("--target", default="dashboard")
    cl.add_argument("selector")

    ty = sub.add_parser("type")
    ty.add_argument("--socket", required=True)
    ty.add_argument("--target", default="dashboard")
    ty.add_argument("selector")
    ty.add_argument("text")

    sh = sub.add_parser("screenshot")
    sh.add_argument("--socket", required=True)
    sh.add_argument("--window", default="active")
    sh.add_argument("--out", required=True)

    wt = sub.add_parser("wait")
    wt.add_argument("--socket", required=True)
    wt.add_argument("--target", default="dashboard")
    wt.add_argument("js")
    wt.add_argument("--timeout", type=float, default=10.0)

    a = p.parse_args()
    if a.cmd == "start":
        sess = UITestSession(fixture=a.fixture, binary=a.binary).start()
        print(json.dumps({
            "ok": True, "socket": str(sess.socket_path), "pid": sess.proc.pid,
            "home": str(sess.home), "tmux_socket": sess.tmux_socket, "root": str(sess.root),
        }))
        return 0
    if a.cmd == "ping":
        print(json.dumps(rpc(a.socket, {"cmd": "ping"})))
        return 0
    if a.cmd == "state":
        print(json.dumps(rpc(a.socket, {"cmd": "state"})))
        return 0
    if a.cmd == "action":
        extra = {k: v for k, v in (("to", a.to), ("path", a.path), ("text", a.text), ("window", a.window), ("width", a.width), ("height", a.height)) if v is not None}
        print(json.dumps(rpc(a.socket, {"cmd": "action", "name": a.name, **extra})))
        return 0
    if a.cmd == "eval":
        print(json.dumps(rpc(a.socket, {"cmd": "eval", "target": a.target, "js": a.js})))
        return 0
    if a.cmd == "click":
        js = f"(() => {{ const el = document.querySelector({json.dumps(a.selector)}); if (!el) return false; el.click(); return true; }})()"
        print(json.dumps(rpc(a.socket, {"cmd": "eval", "target": a.target, "js": js})))
        return 0
    if a.cmd == "type":
        js = (f"(() => {{ const el = document.querySelector({json.dumps(a.selector)}); if (!el) return false; "
              f"el.focus(); el.value = {json.dumps(a.text)}; "
              f"el.dispatchEvent(new Event('input', {{bubbles: true}})); "
              f"el.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }})()")
        print(json.dumps(rpc(a.socket, {"cmd": "eval", "target": a.target, "js": js})))
        return 0
    if a.cmd == "screenshot":
        print(json.dumps(rpc(a.socket, {"cmd": "screenshot", "window": a.window, "path": str(Path(a.out).resolve())})))
        return 0
    if a.cmd == "wait":
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            r = rpc(a.socket, {"cmd": "eval", "target": a.target, "js": a.js})
            if r.get("ok") and r.get("result"):
                print(json.dumps(r))
                return 0
            time.sleep(0.15)
        print(json.dumps({"ok": False, "error": "timeout"}))
        return 1
    if a.cmd == "stop":
        meta_path = Path(a.socket).parent / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        try:
            rpc(a.socket, {"cmd": "action", "name": "quit"}, timeout=3.0)
        except (OSError, ValueError):
            pass
        pid = meta.get("pid")
        pgid = meta.get("pgid") or pid
        if isinstance(pid, int) and pid > 0:
            time.sleep(0.3)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        root_str = meta.get("root")
        if root_str and Path(root_str).name.startswith("gl-uitest-"):
            _reap_session_processes(Path(root_str), pgid if isinstance(pgid, int) else None)
        if meta.get("tmux_socket"):
            subprocess.run(["tmux", "-L", meta["tmux_socket"], "kill-server"], capture_output=True, timeout=5)
        if root_str and Path(root_str).name.startswith("gl-uitest-"):
            shutil.rmtree(root_str, ignore_errors=True)
        print(json.dumps({"ok": True}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
