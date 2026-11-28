"""Unit and real-tmux integration tests for scripts/term_bridge.py and standby session filtering."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ade_basement  # noqa: E402
import term_bridge  # noqa: E402


def _read_event(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> dict[str, Any] | None:
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rem = max(0.01, end - time.monotonic())
        if select.select([fd], [], [], rem)[0]:
            line = proc.stdout.readline()
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
    return None


def _send_cmd(proc: subprocess.Popen[bytes], obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _tmux_sessions(sock: str) -> list[str]:
    r = subprocess.run(
        ["tmux", "-L", sock, "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return r.stdout.splitlines() if r.returncode == 0 else []


def test_validation_env_and_tmux_cmd_splitting(tmp_path):
    assert term_bridge.valid_session("mc-abc_123") == "mc-abc_123"
    assert term_bridge.valid_session("gl-warm-1") is None
    assert term_bridge.valid_session("mc-bad;rm") is None
    assert term_bridge.valid_session(123) is None

    assert term_bridge.valid_pane_id("0:12") == "0:12"
    assert term_bridge.valid_pane_id("bad id") is None

    assert term_bridge.valid_socket("greenlight") == "greenlight"
    for bad in ("", "-bad", ".bad", "a..b", "a/b"):
        with pytest.raises(ValueError):
            term_bridge.valid_socket(bad)

    assert term_bridge.clamp_size(120, 40) == (120, 40)
    assert term_bridge.clamp_size(2, 1) == (10, 2)
    assert term_bridge.clamp_size(True, "bad") == (80, 24)

    assert term_bridge.resolve_cwd(str(tmp_path)) == str(tmp_path)
    assert term_bridge.resolve_cwd("/nonexistent/gl/dir") == os.path.expanduser("~")
    assert term_bridge.resolve_cwd(f"{tmp_path}/../etc") == os.path.expanduser("~")

    env = term_bridge.build_env({"PATH": "/usr/bin:/bin"})
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert env["TERM_PROGRAM"] == "Greenlight"
    assert env["LANG"] == "en_US.UTF-8"
    assert "/opt/homebrew/bin" in env["PATH"].split(":")

    toks = term_bridge._split_tmux_cmds(["set -g status off", "bind -n WheelUpPane 'send -M'", "   ", "unclosed 'quote"])
    assert toks == [";", "set", "-g", "status", "off", ";", "bind", "-n", "WheelUpPane", "send -M"]


def test_ade_basement_panes_ignores_warm_standby_sessions(monkeypatch):
    sample = "\n".join(
        [
            "%1\tgl-warm-123-abcd\t/w/a\tzsh\t100\tzsh",
            "%2\tmc-real1\t/w/a\t✳ Claude\t101\tclaude",
        ]
    )
    monkeypatch.setattr(ade_basement, "tmux", lambda *a: sample)
    got = ade_basement.panes(cmd=True)
    assert [p["session"] for p in got] == ["mc-real1"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")
def test_term_bridge_multiplex_standby_claim_run_and_cleanup(tmp_path):
    sock = f"gl-tb-test-{os.getpid()}"
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    env = os.environ.copy()
    env.update(
        GREENLIGHT_HOME=str(tmp_path / "gl-home"),
        GREENLIGHT_TMUX_SOCKET=sock,
        SHELL="/bin/sh",
        PS1="$ ",
        ENV="/dev/null",
    )
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/term_bridge.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        ready = _read_event(proc)
        assert ready and ready["e"] == "ready" and ready["tmux"] is True

        _send_cmd(proc, {"op": "config", "tmux_opts": ["set -g status off", "set -g mouse on"], "hooks": []})
        _send_cmd(proc, {"op": "ping"})
        assert _read_event(proc) == {"e": "pong"}

        # 1. Cold open + input/output + resize
        _send_cmd(proc, {"op": "open", "id": "0:1", "session": "mc-tb1", "cwd": str(ws1), "cols": 90, "rows": 30})
        got_mark = False
        opened_warm = None
        while opened_warm is None:
            ev = _read_event(proc)
            assert ev is not None
            if ev.get("e") == "out" and ev.get("mark"):
                assert term_bridge.DEFAULT_TMUX_MARK.encode() in base64.b64decode(ev["d"])
                got_mark = True
            elif ev.get("e") == "opened" and ev.get("id") == "0:1":
                opened_warm = ev["warm"]
        assert got_mark and opened_warm is False

        _send_cmd(proc, {"op": "size", "id": "0:1", "cols": 100, "rows": 35})
        time.sleep(0.15)
        _send_cmd(proc, {"op": "in", "id": "0:1", "d": base64.b64encode(b"echo BRIDGE_OK_1\r").decode()})
        buf = b""
        deadline = time.monotonic() + 4.0
        while b"BRIDGE_OK_1" not in buf and time.monotonic() < deadline:
            ev = _read_event(proc, timeout=1.0)
            if ev and ev.get("e") == "out" and ev.get("id") == "0:1":
                buf += base64.b64decode(ev["d"])
        assert b"BRIDGE_OK_1" in buf

        # 2. Wait briefly for standby replenishment in ws1, then claim it on open!
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(s.startswith("gl-warm-") for s in _tmux_sessions(sock)):
                break
            time.sleep(0.05)
        assert any(s.startswith("gl-warm-") for s in _tmux_sessions(sock))

        t0 = time.monotonic()
        _send_cmd(proc, {"op": "open", "id": "0:2", "session": "mc-tb2", "cwd": str(ws1), "cols": 80, "rows": 24})
        warm_hit = None
        while warm_hit is None:
            ev = _read_event(proc)
            assert ev is not None
            if ev.get("e") == "opened" and ev.get("id") == "0:2":
                warm_hit = ev["warm"]
        dt = time.monotonic() - t0
        assert warm_hit is True and dt < 0.5, f"expected warm standby claim under 0.5s, got warm={warm_hit}, dt={dt:.3f}s"

        # 3. Switching warm cwd replaces old standby (still at most 1 gl-warm-* session)
        _send_cmd(proc, {"op": "warm", "cwd": str(ws2)})
        time.sleep(0.25)
        warms = [s for s in _tmux_sessions(sock) if s.startswith("gl-warm-")]
        assert len(warms) <= 1

        # 4. Long `run` executes whole command, emits runMark, leaves no temp files
        out_file = tmp_path / "run_out.txt"
        marker = "LONG_RUN_" + ("x" * 1200)
        run_cmd = f"printf %s {marker} > {out_file}"
        _send_cmd(proc, {"op": "open", "id": "0:3", "session": "mc-tb3", "cwd": str(ws2), "cols": 80, "rows": 24, "run": run_cmd})
        saw_run_mark = False
        deadline = time.monotonic() + 5.0
        while (not saw_run_mark or not out_file.is_file()) and time.monotonic() < deadline:
            ev = _read_event(proc, timeout=0.5)
            if ev and ev.get("e") == "out" and ev.get("id") == "0:3" and ev.get("mark"):
                if term_bridge.DEFAULT_RUN_MARK.encode() in base64.b64decode(ev["d"]):
                    saw_run_mark = True
        assert saw_run_mark and out_file.read_text() == marker

        # 5. Close with kill=True kills session; close with kill=False keeps session alive
        _send_cmd(proc, {"op": "close", "id": "0:3", "session": "mc-tb3", "kill": True})
        _send_cmd(proc, {"op": "close", "id": "0:2", "session": "mc-tb2", "kill": False})
        _send_cmd(proc, {"op": "ping"})
        while True:
            ev = _read_event(proc)
            assert ev is not None
            if ev.get("e") == "pong":
                break
        sess_now = _tmux_sessions(sock)
        assert "mc-tb3" not in sess_now and "mc-tb2" in sess_now and "mc-tb1" in sess_now

        # 6. Bridge shutdown cleans up gl-warm-* while preserving real mc-* user sessions
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=5)
        after = _tmux_sessions(sock)
        assert "mc-tb1" in after and "mc-tb2" in after
        assert not any(s.startswith("gl-warm-") for s in after), f"leaked standby sessions: {after}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
        subprocess.run(["tmux", "-L", sock, "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_app_swift_wires_term_bridge_and_skips_warm_in_pane_hooks():
    src = (ROOT / "app/Sources/App.swift").read_text()
    assert "final class TermBridgePool" in src
    assert 'case "focusWs":' in src
    assert "gl-warm-*" in re.search(r"static func paneRecorderHooks\(\).*?\n    \}\n", src, re.S).group(0)


def test_standby_pool_and_bridge_edge_paths(tmp_path):
    # No-tmux StandbyPool no-ops cleanly
    no_tmux = term_bridge.StandbyPool(None, "gl-no-tmux", term_bridge.build_env())
    assert no_tmux._run_quick(["list-sessions"]) == 127
    assert no_tmux.has_session("mc-x") is False
    no_tmux.warm(str(tmp_path))
    assert no_tmux.claim(str(tmp_path), "mc-x") is False
    no_tmux.cleanup()

    # In-process TermBridge edge handling (no tmux, bad payloads, kill op, config marks, live run)
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    try:
        bridge = term_bridge.TermBridge(socket=f"gl-edge-{os.getpid()}", in_fd=r_in, out_fd=w_out)
        bridge.tmux_bin = None
        bridge.pool.tmux_bin = None

        # Invalid payloads are ignored without crashing
        bridge._handle_message({"op": "open", "id": "bad id", "session": "mc-1"})
        bridge._handle_message({"op": "in", "id": "0:99", "d": "not-base64!!!"})
        bridge._handle_message({"op": "size", "id": "0:99", "cols": 80, "rows": 24})
        bridge._handle_message({"op": "close", "id": "0:99", "session": "mc-1", "kill": False})
        bridge._handle_message({"op": "kill", "session": "mc-1"})
        bridge._handle_message({"op": "config", "tmux_opts": ["set -g status off"], "hooks": [], "tmux_mark": "TM", "run_mark": "RM"})
        assert bridge.tmux_mark == "TM" and bridge.run_mark == "RM"

        # No-tmux launch argv (plain login shell and `run` path)
        argv_plain, warm = bridge._build_launch_argv("0:1", "mc-1", str(tmp_path), "")
        assert warm is False and argv_plain[-1] == "-l"
        argv_run, warm2 = bridge._build_launch_argv("0:1", "mc-1", str(tmp_path), "echo hi")
        assert warm2 is False and argv_run[:2] == ["/bin/sh", "-c"]

        # Read invalid JSON + ping via _read_stdin
        os.write(w_in, b"\nnot-json\n[]\n" + json.dumps({"op": "ping"}).encode() + b"\n")
        bridge._read_stdin()
        raw = os.read(r_out, 4096).decode()
        assert '"e":"pong"' in raw
        bridge.shutdown()
    finally:
        for fd in (r_in, w_in, r_out, w_out):
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")
def test_term_bridge_kill_op_live_reattach_and_sigterm(tmp_path):
    sock = f"gl-tb-sig-{os.getpid()}"
    env = os.environ.copy()
    env.update(GREENLIGHT_HOME=str(tmp_path / "gl-home"), GREENLIGHT_TMUX_SOCKET=sock, SHELL="/bin/sh", PS1="$ ", ENV="/dev/null")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/term_bridge.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        ready = _read_event(proc)
        assert ready and ready["e"] == "ready"
        _send_cmd(proc, {"op": "open", "id": "0:1", "session": "mc-sig1", "cwd": str(tmp_path), "cols": 80, "rows": 24})
        while True:
            ev = _read_event(proc)
            assert ev is not None
            if ev.get("e") == "out" and not ev.get("mark"):
                break
        assert "mc-sig1" in _tmux_sessions(sock)
        # Reattaching a live session with `run` emits runMark without running again
        sentinel = tmp_path / "should_not_exist"
        _send_cmd(proc, {"op": "open", "id": "0:1", "session": "mc-sig1", "cwd": str(tmp_path), "cols": 80, "rows": 24, "run": f"touch {sentinel}"})
        while True:
            ev = _read_event(proc)
            assert ev is not None
            if ev.get("e") == "opened":
                break
        time.sleep(0.2)
        assert not sentinel.exists()
        # Explicit kill op ends the session
        _send_cmd(proc, {"op": "kill", "session": "mc-sig1"})
        _send_cmd(proc, {"op": "ping"})
        while True:
            ev = _read_event(proc)
            assert ev is not None
            if ev.get("e") == "pong":
                break
        assert "mc-sig1" not in _tmux_sessions(sock)
        proc.terminate()
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
        subprocess.run(["tmux", "-L", sock, "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

