"""Unit and real-tmux integration tests for scripts/term_bridge.py and standby session filtering."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ade_basement  # noqa: E402
import term_bridge  # noqa: E402


def test_live_session_attaches_read_write_with_our_options_and_no_run_file(tmp_path):
    bridge = object.__new__(term_bridge.TermBridge)
    bridge.tmux_bin = "/usr/bin/tmux"
    bridge.socket = "greenlight"
    bridge.env = {"SHELL": "/bin/sh"}
    bridge.pool = SimpleNamespace(has_session=lambda _session: True, opts_argv=[";", "set", "-g", "status", "off"],
                                  claim=lambda *_a: pytest.fail("a live session is never a pooled claim"))
    bridge.tmux_mark = term_bridge.DEFAULT_TMUX_MARK
    bridge.run_mark = term_bridge.DEFAULT_RUN_MARK
    bridge._write_run_file = lambda _run: pytest.fail("a live session's run is ignored by -A: no temp file")
    argv, warm, marks, run_file = bridge._build_launch_argv("mc-agent", str(tmp_path), "echo ignored")
    new = argv.index("new-session")
    assert argv[:5] == ["/usr/bin/tmux", "-L", "greenlight", "-f", "/dev/null"] and "status" in argv[:new]
    assert argv[new:new + 4] == ["new-session", "-A", "-s", "mc-agent"] and "attach" not in argv and "-r" not in argv
    assert warm is False and marks == [bridge.run_mark.encode(), bridge.tmux_mark.encode()]
    assert run_file is None and argv[-1].endswith("/dev/null/live")


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
    assert term_bridge.resolve_cwd(f"{tmp_path}/") == str(tmp_path)
    assert term_bridge.resolve_cwd("/nonexistent/gl/dir") == os.path.expanduser("~")
    assert term_bridge.resolve_cwd(f"{tmp_path}/../etc") == os.path.expanduser("~")

    assert term_bridge.sanitize_text("a\x00b\ud800c") == "ab?c"
    assert term_bridge.sanitize_text(123) == ""
    assert term_bridge.default_shell({"SHELL": "/usr/bin/fish"}) == "/usr/bin/fish"
    assert term_bridge.default_shell({}) in ("/bin/zsh", "/bin/sh")
    assert term_bridge._run_shell_cmd("/usr/bin/fish", "$1", "echo hi").startswith("exec /bin/sh -c ")
    assert term_bridge._run_shell_cmd("/bin/zsh", "$1", "echo hi").startswith("exec /bin/zsh -lic ")

    env = term_bridge.build_env({"PATH": "/usr/bin:/bin", "GREENLIGHT_RUN": "leaked-run-id", "TMUX": "outer-client"})
    assert "GREENLIGHT_RUN" not in env and "TMUX" not in env
    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert env["TERM_PROGRAM"] == "Greenlight"
    assert env["LANG"] == "en_US.UTF-8"
    assert "/opt/homebrew/bin" in env["PATH"].split(":")
    # diag.install's bins.ensure_path appends the same folders: the terminal still puts them before /usr/bin, once each
    env = term_bridge.build_env({"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"})
    parts = env["PATH"].split(":")
    assert parts.index("/opt/homebrew/bin") < parts.index("/usr/bin") and parts.count("/opt/homebrew/bin") == 1
    # custom user prefixes (venv, pyenv, mise) stay ahead of FAST_PATH_DIRS, while empty PATH keeps /usr/bin
    env = term_bridge.build_env({"PATH": "/my/venv/bin:/usr/bin:/bin:/opt/homebrew/bin"})
    parts = env["PATH"].split(":")
    assert parts[0] == "/my/venv/bin" and parts.index("/opt/homebrew/bin") < parts.index("/usr/bin")
    assert "/usr/bin" in term_bridge.build_env({"PATH": ""})["PATH"].split(":")

    toks = term_bridge._split_tmux_cmds(["set -g status off", "bind -n WheelUpPane 'send -M'", "   ", "unclosed 'quote"])
    assert toks == [";", "set", "-g", "status", "off", ";", "bind", "-n", "WheelUpPane", "send -M"]


def test_term_bridge_honours_bins_tmux(gl_home, tmp_path, monkeypatch):
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    try:
        fake_tmux = tmp_path / "custom-tmux"
        fake_tmux.write_text("#!/bin/sh\nexit 0\n")
        fake_tmux.chmod(0o755)
        (gl_home / "config.json").write_text(json.dumps({"bins": {"tmux": str(fake_tmux)}}))
        b = term_bridge.TermBridge(in_fd=r_in, out_fd=w_out)
        assert b.tmux_bin == str(fake_tmux)
        b.sel.close()

        # a directory override is rejected (tmux disabled)
        bad_dir = tmp_path / "not-tmux"
        bad_dir.mkdir()
        (gl_home / "config.json").write_text(json.dumps({"bins": {"tmux": str(bad_dir)}}))
        b2 = term_bridge.TermBridge(in_fd=r_in, out_fd=w_out)
        assert b2.tmux_bin is None
        b2.sel.close()
    finally:
        for fd in (r_in, w_in, r_out, w_out):
            os.close(fd)


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
    run_tmp = tmp_path / "run-tmp"
    run_tmp.mkdir()
    env = os.environ.copy()
    env.update(
        GREENLIGHT_HOME=str(tmp_path / "gl-home"),
        GREENLIGHT_TMUX_SOCKET=sock,
        TMPDIR=str(run_tmp),
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
        # Verify the temporary greenlight-run.* file was removed after execution
        deadline = time.monotonic() + 2.0
        while list(run_tmp.glob("greenlight-run.*")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not list(run_tmp.glob("greenlight-run.*")), "run temp file leaked"

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


def test_stale_select_event_surrogates_backpressure_and_spawn_failure(tmp_path, monkeypatch):
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    try:
        bridge = term_bridge.TermBridge(socket=f"gl-adv-{os.getpid()}", in_fd=r_in, out_fd=w_out)
        bridge.tmux_bin = None
        bridge.pool.tmux_bin = None

        # 1. Stale select() event for an old PaneProc must NOT drop a newly reopened pane with same id
        class _DummyProc:
            pid = 999999

            def poll(self) -> int | None:
                return 0

            def wait(self, timeout: float = 0.0) -> int:
                return 0

        old_pane = term_bridge.PaneProc("0:1", "mc-1", -1, _DummyProc())  # type: ignore[arg-type]
        new_pane = term_bridge.PaneProc("0:1", "mc-1", 999, _DummyProc())  # type: ignore[arg-type]
        bridge.panes["0:1"] = new_pane
        fake_key = type("K", (), {"data": old_pane})()
        bridge._dispatch_event(fake_key, term_bridge.selectors.EVENT_READ | term_bridge.selectors.EVENT_WRITE)  # type: ignore[arg-type]
        assert bridge.panes.get("0:1") is new_pane, "stale PaneProc event must not kill reopened pane"
        bridge.panes.pop("0:1", None)

        # 2. Lone surrogate + NUL byte in run/config do not crash bridge or leak temp files
        bad_tmp = tmp_path / "no-such-dir"
        bridge.env["TMPDIR"] = str(bad_tmp)
        path = bridge._write_run_file("ok\x00\ud800text")
        assert path and os.path.isfile(path)
        assert Path(path).read_text(encoding="utf-8") == "ok?text"
        os.unlink(path)

        # 3. _spawn_pty failure unlinks tmp_file and does NOT emit run_mark before spawn
        bridge.tmux_bin = "/bin/true"
        bridge.pool.tmux_bin = "/bin/true"
        created: list[str] = []
        orig_write = bridge._write_run_file

        def _track_write(run: str) -> str | None:
            p = orig_write(run)
            if p:
                created.append(p)
            return p

        monkeypatch.setattr(bridge.pool, "has_session", lambda _s: False)
        monkeypatch.setattr(bridge, "_write_run_file", _track_write)
        monkeypatch.setattr(bridge, "_spawn_pty", lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))
        bridge._handle_open({"op": "open", "id": "0:9", "session": "mc-fail", "cwd": str(tmp_path), "run": "echo hi"})
        out_raw = os.read(r_out, 8192).decode()
        assert '"e":"exit","id":"0:9","code":127' in out_raw
        assert "greenlight-run" not in out_raw, "run_mark must not emit before _spawn_pty succeeds"
        assert created and not os.path.exists(created[0]), "tmp_file must be unlinked on _spawn_pty failure"

        # 4. Oversized line without newline is dropped cleanly, subsequent message still works
        bridge._in_buf.extend(b"x" * (term_bridge.MAX_IN_BUF + 16))
        os.write(w_in, b"tail\n" + json.dumps({"op": "ping"}).encode() + b"\n")
        bridge._read_stdin()
        assert '"e":"pong"' in os.read(r_out, 4096).decode()

        # 5. Output backpressure toggles _paused_read cleanly (with and without an active pane)
        p_r, p_w = os.pipe()
        try:
            live_pane = term_bridge.PaneProc("0:2", "mc-2", p_r, _DummyProc())  # type: ignore[arg-type]
            bridge.panes["0:2"] = live_pane
            bridge.sel.register(p_r, term_bridge.selectors.EVENT_READ, live_pane)
            bridge._out_buf.extend(b"a" * term_bridge.OUT_HIGH_WATER)
            with monkeypatch.context() as mp:
                mp.setattr(os, "write", lambda _fd, _buf: (_ for _ in ()).throw(BlockingIOError()))
                bridge._flush_out()
            assert bridge._paused_read is True and bridge._out_registered is True
            bridge._out_buf.clear()
            bridge._flush_out()
            assert bridge._paused_read is False and bridge._out_registered is False
            bridge.sel.unregister(p_r)
            bridge.panes.pop("0:2", None)
        finally:
            os.close(p_r)
            os.close(p_w)

        # 6. _handle_message exception is caught in _read_stdin without crashing bridge; wake dispatch drains
        monkeypatch.setattr(bridge, "_handle_message", lambda _m: (_ for _ in ()).throw(RuntimeError("boom")))
        os.write(w_in, json.dumps({"op": "ping"}).encode() + b"\n")
        bridge._read_stdin()
        wake_r, wake_w = os.pipe()
        try:
            bridge.wake_fd = wake_r
            os.write(wake_w, b"x")
            bridge._dispatch_event(type("K", (), {"data": "wake"})(), term_bridge.selectors.EVENT_READ)  # type: ignore[arg-type]
            bridge._dispatch_event(type("K", (), {"data": "stdout"})(), term_bridge.selectors.EVENT_WRITE)  # type: ignore[arg-type]
            bridge.wake_fd = None
        finally:
            os.close(wake_r)
            os.close(wake_w)

        # 7. PaneProc.terminate_group / kill_group / stop and _reap_exiting reap children cleanly
        sleeper = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], preexec_fn=os.setsid)
        m_fd, s_fd = os.openpty()
        os.close(s_fd)
        p_sleep = term_bridge.PaneProc("0:3", "mc-3", m_fd, sleeper)
        p_sleep.stop()
        assert sleeper.poll() is not None

        sleeper2 = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM HUP; sleep 30"], preexec_fn=os.setsid)
        m2, s2 = os.openpty()
        os.close(s2)
        p_sleep2 = term_bridge.PaneProc("0:4", "mc-4", m2, sleeper2)
        p_sleep2.close_master()
        bridge._reaping.append((p_sleep2, time.monotonic() - 1.0))
        bridge._reap_exiting()
        sleeper2.wait(timeout=3)
        bridge._reap_exiting()
        assert not bridge._reaping

        # 8. StandbyPool failed warm_proc reap, claim rc != 0 / timeout, and retired proc cleanup
        fake_pool = term_bridge.StandbyPool("/bin/true", f"gl-fake-{os.getpid()}", bridge.env)
        killed: list[str] = []
        monkeypatch.setattr(fake_pool, "has_session", lambda _s: True)
        monkeypatch.setattr(fake_pool, "kill_session", killed.append)

        class _FailProc:
            def __init__(self, rc: int | None) -> None:
                self._rc = rc

            def poll(self) -> int | None:
                return self._rc

            def wait(self, timeout: float = 0.0) -> int:
                if self._rc is None:
                    raise subprocess.TimeoutExpired(["tmux"], timeout)
                return self._rc

            def terminate(self) -> None:
                self._rc = 0

            def kill(self) -> None:
                self._rc = -9

        fake_pool.warm_name = "gl-warm-1-aaaa"
        fake_pool.warm_cwd = str(tmp_path)
        fake_pool.warm_proc = _FailProc(1)  # type: ignore[assignment]
        fake_pool._reap_proc()
        assert fake_pool.warm_name is None and "gl-warm-1-aaaa" in killed

        fake_pool.warm_name = "gl-warm-1-bbbb"
        fake_pool.warm_cwd = str(tmp_path)
        fake_pool.warm_proc = _FailProc(None)  # type: ignore[assignment]
        assert fake_pool.claim(str(tmp_path), "mc-x") is False
        fake_pool.warm_proc = _FailProc(1)  # type: ignore[assignment]
        assert fake_pool.claim(str(tmp_path), "mc-x") is False
        assert "gl-warm-1-bbbb" in killed

        ret_proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
        fake_pool._retired.append((ret_proc, "gl-warm-1-cccc"))
        fake_pool.cleanup()
        assert ret_proc.poll() is not None and "gl-warm-1-cccc" in killed
        bridge.shutdown()
    finally:
        for fd in (r_in, w_in, r_out, w_out):
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")
def test_standby_sweep_stale_claim_collision_and_pipe_hook(tmp_path):
    sock = f"gl-tb-pool-{os.getpid()}"
    env = term_bridge.build_env({"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SHELL": "/bin/sh"})
    tmux_bin = shutil.which("tmux", path=env["PATH"])
    assert tmux_bin
    pool = term_bridge.StandbyPool(tmux_bin, sock, env)
    pipe_marker = tmp_path / "pipe_attached.txt"
    try:
        # 1. Dead-PID gl-warm-* session is swept on startup while mc-* survives
        subprocess.run([tmux_bin, "-L", sock, "-f", "/dev/null", "new-session", "-d", "-s", "gl-warm-99999999-dead"], check=True)
        subprocess.run([tmux_bin, "-L", sock, "new-session", "-d", "-s", "mc-keep1"], check=True)
        pool.sweep_stale()
        now = _tmux_sessions(sock)
        assert "gl-warm-99999999-dead" not in now and "mc-keep1" in now

        # 2. Failed claim (duplicate session name) kills gl-warm-* instead of leaking it
        pool.set_size(100, 30)
        pool.warm(str(tmp_path))
        assert pool.warm_proc is not None
        pool.warm_proc.wait(timeout=5)
        pool.tick()
        assert pool.claim(str(tmp_path), "mc-keep1") is False
        assert not any(s.startswith("gl-warm-") for s in _tmux_sessions(sock))

        # 3. Rapid warm(ws1) -> warm(ws2) retires first proc cleanly and leaves at most 1 gl-warm-*
        ws1 = tmp_path / "w1"
        ws2 = tmp_path / "w2"
        ws1.mkdir()
        ws2.mkdir()
        pool.warm(str(ws1))
        pool.warm(str(ws2))
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            pool.tick()
            if not pool._retired and pool.warm_proc is None:
                break
            time.sleep(0.05)
        warms = [s for s in _tmux_sessions(sock) if s.startswith("gl-warm-")]
        assert len(warms) == 1 and not pool._retired

        # 4. Real App.paneRecorderHooks pattern skips gl-warm-* and attaches pipe-pane on client-attached after claim
        hook_val = f'if -F "#{{||:#{{pane_pipe}},#{{m:gl-warm-*,#{{session_name}}}}}}" "" "pipe-pane -O \'cat >> {pipe_marker}\'"'
        pool.set_opts(
            ["set -g status off"],
            [f"set-hook -g 'after-new-session[1]' {shlex.quote(hook_val)}", f"set-hook -g 'client-attached[1]' {shlex.quote(hook_val)}"],
        )
        pool.warm(str(ws1))
        assert pool.warm_proc is not None
        pool.warm_proc.wait(timeout=5)
        pool.tick()
        assert not pipe_marker.exists(), "pipe-pane must NOT attach to gl-warm-* standby"
        assert pool.claim(str(ws1), "mc-hooked") is True
        # Attach briefly via a PTY so client-attached[1] fires on renamed mc-hooked
        m_fd, s_fd = os.openpty()
        att = subprocess.Popen([tmux_bin, "-L", sock, "attach-session", "-t", "=mc-hooked"], stdin=s_fd, stdout=s_fd, stderr=s_fd, env=env)
        os.close(s_fd)
        try:
            deadline = time.monotonic() + 3.0
            while not pipe_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert pipe_marker.exists(), "client-attached[1] must attach pipe-pane after standby rename-session"
        finally:
            os.close(m_fd)
            att.terminate()
            att.wait(timeout=3)
    finally:
        pool.cleanup()
        subprocess.run(["tmux", "-L", sock, "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_app_swift_wires_term_bridge_and_skips_warm_in_pane_hooks():
    src = (ROOT / "app/Sources/App.swift").read_text()
    assert "final class TermBridgePool" in src
    assert 'case "focusWs":' in src
    hook_body = re.search(r"static func paneRecorderHooks\([^)]*\) -> \[String\] \{.*?\n    \}\n", src, re.S)
    assert hook_body and "gl-warm-*" in hook_body.group(0) and "hostPaths(for: targetHost)" in hook_body.group(0)
    assert "static func isBase64(" in src
    assert "func retireAll(primaryHost:" in src
    assert "ptyPendingIn" in src


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
        argv_plain, warm, marks_plain, tmp_plain = bridge._build_launch_argv("mc-1", str(tmp_path), "")
        assert warm is False and argv_plain[-1] == "-l" and marks_plain == [] and tmp_plain is None
        argv_run, warm2, marks_run, tmp_run = bridge._build_launch_argv("mc-1", str(tmp_path), "echo hi")
        assert warm2 is False and argv_run[:2] == ["/bin/sh", "-c"] and marks_run == [b"RM"] and tmp_run is None

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

