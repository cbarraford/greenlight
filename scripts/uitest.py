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
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_CACHE = Path(tempfile.gettempdir()) / "greenlight-uitest-bin"
FIXTURES = ("default", "empty", "adversarial", "scale", "chaos", "corrupt")


def swift_sources():
    d = ROOT / "app" / "Sources"
    if d.is_dir():
        return sorted(d.glob("*.swift"))
    return [ROOT / "app" / "Greenlight.swift"]


def build_test_binary():
    """Compile an unoptimized scratch Greenlight binary cached by Swift source hash."""
    srcs = swift_sources()
    h = hashlib.sha256()
    for p in srcs:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    key = h.hexdigest()[:16]
    BIN_CACHE.mkdir(parents=True, exist_ok=True)
    out = BIN_CACHE / f"Greenlight-{key}"
    if out.is_file() and os.access(out, os.X_OK):
        return out
    tmp = BIN_CACHE / f"Greenlight-{key}.{os.getpid()}"
    cmd = ["swiftc", "-Onone", *[str(p) for p in srcs], "-o", str(tmp)]
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
        return json.loads(line.decode())
    finally:
        s.close()


def _install_stub_harnesses(bindir: Path):
    """Install deterministic offline stub agent CLIs so UI fuzzing never spends real API tokens."""
    # Claude stream-json stub: emits a tiny valid assistant message + result envelope.
    claude_stub = bindir / "claude"
    claude_stub.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null 2>&1 || true\n"
        "printf '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"Stub Chief reply (uitest offline).\"}]}}\\n'\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\",\"result\":\"Stub Chief reply (uitest offline).\",\"session_id\":\"uitest-stub-sid\"}\\n'\n"
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


def _make_fixture(root: Path, fixture: str):
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
        ws_path = str(wsdir / "xss-ws")
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
            worktrees.append({
                "worktreeId": f"wt-{i:02d}", "displayName": name, "path": ws_path,
                "repo": repo, "branch": f"feat/{name}", "comment": f"Ship {name} and merge !{100 + i}",
                "agents": [{"state": st, "agentType": agent_type, "lastAssistantMessage": last_msg}],
            })
            rows.append({
                "id": f"wt-{i:02d}", "name": name, "path": ws_path, "project": f"acme/{repo}",
                "goal": f"Ship {name} and merge !{100 + i}" + (" (parked)" if i % 11 == 0 else ""),
                "branch": f"feat/{name}",
                "agent": {"state": st, "type": agent_type, "last": last_msg, "since": 1700000000000 + i * 60000},
                "mr": {"ref": f"!{100 + i}", "title": f"Change {i:02d}: {name}", "state": "opened",
                       "pipeline": pipe, "draft": step == "draft", "web_url": f"https://example.invalid/mr/{100 + i}"},
                "next": {"step": step, "owner": "me" if i % 3 == 0 else "agent",
                         "action": f"Handle {step} for !{100 + i}", "cmd": []},
            })
    elif fixture == "corrupt":
        ws_ok = str(wsdir / "valid-ws")
        Path(ws_ok).mkdir(exist_ok=True)
        rtl_name = "rtl-\u202e-test-\U0001f680-" + ("x" * 90)
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
    else:  # default or chaos
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

    _install_stub_harnesses(bindir)
    stub_harnesses = {n: {"bin": str(bindir / n)} for n in ("claude", "codex", "gemini", "opencode", "pi", "agy")}
    (home / "config.json").write_text(json.dumps({
        "me": "uitest-user",
        "agents": ["claude", "codex"],
        "llm_harness": "claude",
        "harnesses": stub_harnesses,
    }))
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
        f"  'terminal list'*) cat {json.dumps(str(orca_terms))};;\n"
        f"  'repo list'*) cat {json.dumps(str(orca_repos))};;\n"
        f"  *) cat {json.dumps(str(orca_data))};;\n"
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
            f"if [ \"$1\" = 'terminal' ]; then cat {json.dumps(str(orca_terms))}; exit 0; fi\n"
            f"if [ \"$1\" = 'repo' ]; then cat {json.dumps(str(orca_repos))}; exit 0; fi\n"
            f"CFILE={json.dumps(str(counter))}\n"
            "N=$(cat \"$CFILE\" 2>/dev/null || echo 0)\n"
            "N=$((N + 1))\n"
            "echo \"$N\" > \"$CFILE\"\n"
            "MOD=$((N % 5))\n"
            f"if [ \"$MOD\" -eq 1 ]; then cat {json.dumps(str(orca_data))}; exit 0; fi\n"
            f"if [ \"$MOD\" -eq 2 ]; then sleep 0.35; cat {json.dumps(str(orca_alt))}; exit 0; fi\n"
            "if [ \"$MOD\" -eq 3 ]; then echo '{broken-json'; exit 0; fi\n"
            "if [ \"$MOD\" -eq 4 ]; then echo 'transient orca error' >&2; exit 1; fi\n"
            f"cat {json.dumps(str(orca_data))}\n"
        )
    else:
        orca_bin.write_text("#!/bin/sh\n" + dispatch_ok)
    orca_bin.chmod(0o755)

    for stub in ("glab", "gh"):
        p = bindir / stub
        if fixture == "chaos":
            p.write_text(
                "#!/bin/sh\n"
                f"N=$(cat {json.dumps(str(root / 'chaos-counter'))} 2>/dev/null || echo 0)\n"
                "if [ $((N % 4)) -eq 3 ]; then echo 'rate limit 429' >&2; exit 1; fi\n"
                "echo '[]'\n"
            )
        else:
            p.write_text("#!/bin/sh\necho '[]'\n")
        p.chmod(0o755)

    return home, bindir, webdir


class UITestSession:
    """Context manager for an isolated background Greenlight --uitest instance."""

    def __init__(self, fixture="default", binary=None, timeout=20.0, record_trace=True):
        self.fixture = fixture
        self.binary = Path(binary) if binary else None
        self.timeout = timeout
        self.record_trace = record_trace
        self.trace = []
        self.id = uuid.uuid4().hex[:8]
        self.root = Path(tempfile.mkdtemp(prefix=f"gl-uitest-{self.id}-"))
        self.socket_path = self.root / "ui.sock"
        self.tmux_socket = f"gl-uitest-{self.id}"
        self.proc = None
        self.home = None

    def start(self):
        binary = self.binary or build_test_binary()
        self.home, bindir, webdir = _make_fixture(self.root, self.fixture)
        env = {
            **os.environ,
            "GREENLIGHT_HOST": "local",
            "GREENLIGHT_HOME": str(self.home),
            "GREENLIGHT_CODE": str(ROOT),
            "GREENLIGHT_WEB_DIR": str(webdir),
            "GREENLIGHT_TMUX_SOCKET": self.tmux_socket,
            "GREENLIGHT_ADE": "orca",
            "ORCA_CLI_COMMAND": str(bindir / "orca"),
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        }
        log = open(self.root / "app.log", "wb")
        self._log = log
        self.proc = subprocess.Popen(
            [str(binary), "--uitest", "--uitest-socket", str(self.socket_path)],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + self.timeout
        last_err = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                log.flush()
                tail = (self.root / "app.log").read_text(errors="replace")[-1000:]
                raise RuntimeError(f"Greenlight --uitest exited early ({self.proc.returncode}):\n{tail}")
            if self.socket_path.exists():
                try:
                    r = self.ping()
                    if r.get("ok"):
                        # Save metadata beside socket for CLI `stop` cleanup
                        (self.root / "meta.json").write_text(json.dumps({
                            "pid": self.proc.pid, "root": str(self.root),
                            "tmux_socket": self.tmux_socket, "socket": str(self.socket_path),
                        }))
                        return self
                except OSError as e:
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

    def stop(self):
        if self.socket_path.exists():
            try:
                rpc(self.socket_path, {"cmd": "action", "name": "quit"}, timeout=3.0)
            except OSError:
                pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
            self.proc = None
        if hasattr(self, "_log") and self._log:
            try:
                self._log.close()
            except OSError:
                pass
        subprocess.run(["tmux", "-L", self.tmux_socket, "kill-server"], capture_output=True, timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)

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
        except OSError:
            pass
        if meta.get("tmux_socket"):
            subprocess.run(["tmux", "-L", meta["tmux_socket"], "kill-server"], capture_output=True, timeout=5)
        if meta.get("root") and Path(meta["root"]).name.startswith("gl-uitest-"):
            time.sleep(0.3)
            shutil.rmtree(meta["root"], ignore_errors=True)
        print(json.dumps({"ok": True}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
