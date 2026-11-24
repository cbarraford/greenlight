"""Tests for scripts/pane_log.py: timestamped tmux pipe-pane recorder, rotation, parser, and live tmux hooks."""
from __future__ import annotations

import json
import os
import pty
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("GREENLIGHT_HOME", tempfile.mkdtemp(prefix="gl-plog-import-"))
import pane_log  # noqa: E402


def test_valid_session_rejects_unsafe_names():
    assert pane_log.valid_session("mc-abc123") == "mc-abc123"
    assert pane_log.valid_session("sess_1.test") == "sess_1.test"
    for bad in ("", ".", "..", ".hidden", "a..b", "a/b", "a b", "s;rm", "x" * 200):
        with pytest.raises(ValueError):
            pane_log.valid_session(bad)


def test_parse_chunks_preserves_timestamps_strips_ansi_and_collapses_spinners():
    chunks = [
        (1000, "\x1b]0;✳ Claude\x07\x1b[32mStarting build\x1b[0m\r\n"),
        (2000, "⠋ Thinking…\r⠙ Thinking…\r⠹ Done thinking\r\n"),
        (5000, "● Bash(pytest -q)\r\n"),
        (6000, "abc\x08d\r\n"),
        (7000, "same spinner"),
        (7500, "\nsame spinner\n"),  # rapid duplicate within 2s collapses
        (9000, "final line"),
    ]
    lines = pane_log.parse_chunks(chunks, "mc-s1", gen=0)
    assert [(x["text"], x["ts"]) for x in lines] == [
        ("Starting build", 1000),
        ("⠹ Done thinking", 2000),
        ("● Bash(pytest -q)", 5000),
        ("abd", 6000),
        ("same spinner", 7500),
        ("final line", 9000),
    ]
    assert [x["id"] for x in lines[:-1]] == [f"mc-s1.g0:{i}" for i in range(5)]
    assert lines[-1]["id"] == "mc-s1.g0:tail" and lines[-1].get("partial") is True
    assert pane_log.detect_tool(lines) == ("Bash", "pytest -q")


def test_partial_line_split_crlf_split_escapes_and_tui_redraws():
    # Unfinished trailing line gets :tail, then becomes committed :0 once newline arrives
    p1 = pane_log.parse_chunks([(1000, "Running tests")], "mc-p", gen=0)
    assert len(p1) == 1 and p1[0]["id"] == "mc-p.g0:tail" and p1[0].get("partial") is True
    p2 = pane_log.parse_chunks([(1000, "Running tests"), (2000, " ... 42 passed\r\n")], "mc-p", gen=0)
    assert len(p2) == 1 and p2[0]["id"] == "mc-p.g0:0" and p2[0]["text"] == "Running tests ... 42 passed"
    assert not p2[0].get("partial")

    # Split \r\n and split ANSI/OSC escapes across chunks never erase lines or leak control text
    split_chunks = [
        (1000, "important line\r"),
        (1100, "\nnext \x1b[38;2;120"),
        (1200, ";200;80mgreen\x1b[0m \x1b]0;secret"),
        (1300, " title\x07end\r\n"),
        (1400, "left\x1b[40Gright\x1b[2Ctail\r\n"),
    ]
    lines = pane_log.parse_chunks(split_chunks, "mc-sp", gen=0)
    assert [x["text"] for x in lines] == ["important line", "next green end", "left right tail"]

    # Ink/ratatui multi-line cursor-up redraws overwrite recent frame lines instead of spamming
    ink_chunks = [(1000, "⠋ Thinking (1s)\n> prompt")] + [
        (1000 + k * 500, f"\x1b[2K\x1b[1A\x1b[2K\x1b[G⠙ Thinking ({k}s)\n> prompt") for k in range(2, 8)
    ]
    ink_lines = pane_log.parse_chunks(ink_chunks, "mc-ink", gen=0)
    assert [x["text"] for x in ink_lines] == ["⠙ Thinking (7s)", "> prompt"]
    assert ink_lines[0]["id"] == "mc-ink.g0:0" and ink_lines[1]["id"] == "mc-ink.g0:tail"


def test_detect_tool_handles_codex_and_empty():
    assert pane_log.detect_tool([{"text": "• Ran make test-py"}]) == ("Shell", "make test-py")
    assert pane_log.detect_tool([{"text": "plain output"}]) == ("", "")


def test_record_permissions_utf8_and_bounded_rotation(tmp_path):
    log_dir = tmp_path / "pane-logs"
    r_fd, w_fd = os.pipe()
    # Feed multi-byte UTF-8 split across writes and enough bytes to trigger rotation (> 4096 half cap).
    payload = b"hello \xe2\x9c\x93\n" + (b"line-" + b"x" * 180 + b"\n") * 40

    def _writer():
        for i in range(0, len(payload), 7):
            os.write(w_fd, payload[i : i + 7])
        os.close(w_fd)

    import threading

    th = threading.Thread(target=_writer)
    th.start()
    try:
        written = pane_log.record(session="mc-rot1", cwd="/w/proj", max_bytes=8192, log_dir=log_dir, in_fd=r_fd)
    finally:
        os.close(r_fd)
        th.join(timeout=5)

    assert written > 0
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    cur = log_dir / "mc-rot1.jsonl"
    prev = log_dir / "mc-rot1.jsonl.1"
    assert cur.is_file() and prev.is_file(), "rotation created .jsonl.1"
    assert stat.S_IMODE(cur.stat().st_mode) == 0o600
    assert stat.S_IMODE(prev.stat().st_mode) == 0o600
    assert cur.stat().st_size + prev.stat().st_size <= 8192 + 4096

    # Reading spans .jsonl.1 (gen N) + .jsonl (gen N+1) with distinct stable generation IDs.
    res = pane_log.read_session("mc-rot1", limit=500, log_dir=log_dir)
    gens = {pane_log.CURSOR_RE.fullmatch(e["id"]).group(2) for e in res["entries"]}
    assert len(gens) == 2
    assert res["source"] == "pane" and res["last_output_ts"] > 0


def test_record_refuses_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "pane-logs"
    link.symlink_to(real)
    r_fd, w_fd = os.pipe()
    os.close(w_fd)
    try:
        with pytest.raises(OSError):
            pane_log.record(session="mc-sym", log_dir=link, in_fd=r_fd)
    finally:
        os.close(r_fd)


def test_pagination_and_workspace_lookup(tmp_path):
    log_dir = tmp_path / "pane-logs"
    log_dir.mkdir(mode=0o700)
    f = log_dir / "mc-ws1.jsonl"
    rows = [{"ts": 1000, "meta": {"session": "mc-ws1", "cwd": "/w/app", "gen": 0}}]
    rows.extend({"ts": 2000 + i * 1000, "d": f"out {i}\n"} for i in range(15))
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))

    # Matches workspace path and nested subdirectory via meta header (even with no live tmux panes).
    assert pane_log.workspace_sessions("/w/app", live_panes=[], log_dir=log_dir) == ["mc-ws1"]
    assert pane_log.workspace_sessions("/w/other", live_panes=[], log_dir=log_dir) == []
    # Live pane cd from /w/app into /w/other/sub does NOT leak /w/app's log into /w/other.
    assert pane_log.workspace_sessions("/w/other", live_panes=[{"session": "mc-ws1", "cwd": "/w/other/sub"}], log_dir=log_dir) == []
    # Nested worktree ownership assigns child worktree logs only to the child worktree.
    child_f = log_dir / "mc-child.jsonl"
    child_f.write_text(json.dumps({"ts": 1000, "meta": {"session": "mc-child", "cwd": "/w/app/.worktrees/feat", "gen": 0}}) + "\n" + json.dumps({"ts": 2000, "d": "child\n"}) + "\n")
    wts = ["/w/app", "/w/app/.worktrees/feat"]
    assert pane_log.workspace_sessions("/w/app", live_panes=[], log_dir=log_dir, worktree_paths=wts) == ["mc-ws1"]
    assert pane_log.workspace_sessions("/w/app/.worktrees/feat", live_panes=[], log_dir=log_dir, worktree_paths=wts) == ["mc-child"]

    # Empty meta-only pane log returns None from read_workspace and last_output_ts=None from read_session.
    (log_dir / "mc-empty.jsonl").write_text(json.dumps({"ts": 9999, "meta": {"session": "mc-empty", "cwd": "/w/empty", "gen": 0}}) + "\n")
    assert pane_log.read_session("mc-empty", log_dir=log_dir)["last_output_ts"] is None
    assert pane_log.read_workspace("/w/empty", live_panes=[], log_dir=log_dir) is None

    # Multi-session pagination pages seamlessly from newer session into older session.
    older_f = log_dir / "mc-ws0.jsonl"
    older_f.write_text(
        json.dumps({"ts": 500, "meta": {"session": "mc-ws0", "cwd": "/w/app", "gen": 0}}) + "\n"
        + "".join(json.dumps({"ts": 600 + i * 10, "d": f"old {i}\n"}) + "\n" for i in range(3))
    )
    os.utime(older_f, (1000, 1000))
    os.utime(f, (2000, 2000))

    p1 = pane_log.read_workspace("/w/app", live_panes=[], limit=5, log_dir=log_dir, worktree_paths=wts)
    assert [e["text"] for e in p1["entries"]] == [f"out {i}" for i in range(10, 15)]
    assert p1["more"] == "mc-ws1.g0:10"

    p2 = pane_log.read_workspace("/w/app", live_panes=[], before=p1["more"], limit=5, log_dir=log_dir, worktree_paths=wts)
    assert [e["text"] for e in p2["entries"]] == [f"out {i}" for i in range(5, 10)]
    assert p2["more"] == "mc-ws1.g0:5"

    p3 = pane_log.read_workspace("/w/app", live_panes=[], before=p2["more"], limit=10, log_dir=log_dir, worktree_paths=wts)
    assert [e["text"] for e in p3["entries"]] == ["old 0", "old 1", "old 2"] + [f"out {i}" for i in range(5)]
    assert p3["more"] is None


def test_prune_stale_removes_old_and_excess_sessions(tmp_path):
    log_dir = tmp_path / "pane-logs"
    log_dir.mkdir(mode=0o700)
    now = 1_000_000.0
    for name, age in (("mc-old", 10 * 86400), ("mc-keep", 10 * 86400), ("mc-new1", 10), ("mc-new2", 20), ("mc-new3", 30)):
        p = log_dir / f"{name}.jsonl"
        p.write_text("{}\n")
        os.utime(p, (now - age, now - age))
    removed = pane_log.prune_stale(log_dir, keep={"mc-keep"}, max_age=7 * 86400, max_sessions=2, now=now)
    assert removed >= 2
    remaining = sorted(p.stem for p in log_dir.glob("*.jsonl"))
    assert "mc-keep" in remaining and "mc-old" not in remaining and "mc-new1" in remaining

    # Locked (live) sessions are never pruned even if older than max_age, and duplicate recorders exit cleanly.
    locked_p = log_dir / "mc-locked.jsonl"
    locked_p.write_text("{}\n")
    os.utime(locked_p, (now - 30 * 86400, now - 30 * 86400))
    lock_fd = pane_log._acquire_session_lock(log_dir, "mc-locked")
    assert lock_fd is not None
    try:
        assert pane_log._acquire_session_lock(log_dir, "mc-locked") is None
        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        try:
            assert pane_log.record(session="mc-locked", log_dir=log_dir, in_fd=r_fd) == 0
        finally:
            os.close(r_fd)
        pane_log.prune_stale(log_dir, max_age=7 * 86400, max_sessions=1, now=now)
        assert locked_p.is_file(), "locked live session must not be pruned"
    finally:
        os.close(lock_fd)


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.04):
    deadline = time.monotonic() + timeout
    while True:
        val = pred()
        if val:
            return val
        if time.monotonic() >= deadline:
            return val
        time.sleep(interval)


def test_sigterm_wakes_idle_recorder_and_valid_socket(tmp_path):
    for bad in ("", ".", "..", ".hidden", "-flag", "a..b", "a;b"):
        with pytest.raises(ValueError):
            pane_log.valid_socket(bad)
    assert pane_log.valid_socket("greenlight") == "greenlight"

    # Subprocess blocked on idle stdin pipe must exit promptly on SIGTERM (PEP 475 check)
    gl_home = tmp_path / "sig-home"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "pane_log.py"), "record", "--session", "mc-sig1", "--cwd", str(tmp_path)],
        stdin=subprocess.PIPE,
        env={**os.environ, "GREENLIGHT_HOME": str(gl_home)},
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(b"before term\n")
        proc.stdin.flush()
        assert _wait_until(lambda: any(e["text"] == "before term" for e in pane_log.read_session("mc-sig1", log_dir=gl_home / "pane-logs")["entries"]))
        proc.terminate()
        rc = proc.wait(timeout=3)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdin:
            proc.stdin.close()
    res = pane_log.read_session("mc-sig1", log_dir=gl_home / "pane-logs")
    assert [e["text"] for e in res["entries"]] == ["before term"]


def test_pipe_command_and_app_swift_hooks():
    cmd = pane_log.pipe_command("/opt/gl", "/tmp/gl-home", "greenlight")
    assert "[ -f /opt/gl/scripts/pane_log.py ]" in cmd
    assert "--pane #{pane_id}" in cmd
    with pytest.raises(ValueError):
        pane_log.pipe_command("/opt/gl", "/tmp/gl-home", "bad;sock")
    swift = (ROOT / "app/Sources/App.swift").read_text()
    assert "func paneRecorderHooks()" in swift
    assert "after-new-session[1]" in swift
    assert "client-attached[1]" in swift
    assert "#{pane_pipe}" in swift
    assert "pane_log.py" in swift


def test_csi_cursor_moves_resuming_log_and_corrupt_jsonl(tmp_path):
    assert pane_log.default_log_dir(tmp_path) == tmp_path / "pane-logs"
    # CSI row jumps (H/B/E) split lines; G / 2K overwrite current line; consecutive blanks collapse
    chunks = [
        (1000, "row1\x1b[2;1Hrow2\x1b[2Krow2-fixed\n\n\n\nrow3\x1b[1Goverwritten\n"),
    ]
    lines = pane_log.parse_chunks(chunks, "mc-csi", gen=2)
    assert [x["text"] for x in lines] == ["row1", "row2-fixed", "", "overwritten"]
    assert lines[0]["id"] == "mc-csi.g2:0"

    # Malformed lines, non-dict JSON, and missing files are tolerated cleanly
    log_dir = tmp_path / "pane-logs"
    log_dir.mkdir(mode=0o700)
    f = log_dir / "mc-corrupt.jsonl"
    f.write_text('not-json\n[]\n{"ts": 1000, "meta": {"session": "mc-corrupt", "cwd": "/w/c", "gen": 0}}\n{"ts": 2000, "d": "ok\\n"}\n')
    res = pane_log.read_session("mc-corrupt", log_dir=log_dir)
    assert [e["text"] for e in res["entries"]] == ["ok"]
    assert pane_log.read_session("mc-missing", log_dir=log_dir)["entries"] == []

    # Resuming an existing non-empty session log appends without resetting generation
    r_fd, w_fd = os.pipe()
    os.write(w_fd, b"resumed line\n")
    os.close(w_fd)
    try:
        pane_log.record(session="mc-corrupt", cwd="/w/c", log_dir=log_dir, in_fd=r_fd)
    finally:
        os.close(r_fd)
    res2 = pane_log.read_session("mc-corrupt", log_dir=log_dir)
    assert [e["text"] for e in res2["entries"]] == ["ok", "resumed line"]


def test_resolve_pane_validation_and_cli_commands(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError):
        pane_log._resolve_pane("bad", "greenlight")
    with pytest.raises(ValueError):
        pane_log._resolve_pane("%1", "bad;sock")

    # Mocked tmux display-message success & failure
    class _P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    monkeypatch.setattr(pane_log.subprocess, "run", lambda *a, **kw: _P(0, "mc-p1\t/w/p1\n"))
    assert pane_log._resolve_pane("%1", "greenlight") == ("mc-p1", "/w/p1")
    monkeypatch.setattr(pane_log.subprocess, "run", lambda *a, **kw: _P(1, ""))
    with pytest.raises(RuntimeError):
        pane_log._resolve_pane("%1", "greenlight")
    assert pane_log.attach("mc-p1", socket="bad;sock") is False

    # CLI: read, workspace, and usage error
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path))
    log_dir = tmp_path / "pane-logs"
    log_dir.mkdir(mode=0o700)
    (log_dir / "mc-cli.jsonl").write_text(
        json.dumps({"ts": 1000, "meta": {"session": "mc-cli", "cwd": "/w/cli", "gen": 0}}) + "\n"
        + json.dumps({"ts": 2000, "d": "cli output\n"}) + "\n"
    )
    monkeypatch.setattr(sys, "argv", ["pane_log.py", "read", "mc-cli", "--limit", "10"])
    pane_log.main()
    out = json.loads(capsys.readouterr().out)
    assert [e["text"] for e in out["entries"]] == ["cli output"]

    monkeypatch.setattr(sys, "argv", ["pane_log.py", "workspace", "/w/cli"])
    pane_log.main()
    out_ws = json.loads(capsys.readouterr().out)
    assert out_ws["session"] == "mc-cli" and [e["text"] for e in out_ws["entries"]] == ["cli output"]

    monkeypatch.setattr(sys, "argv", ["pane_log.py", "unknown"])
    with pytest.raises(SystemExit):
        pane_log.main()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")
def test_live_tmux_hooks_record_across_detach_reattach_and_remote_noop(tmp_path):
    sock = f"gl-plog-test-{os.getpid()}"
    gl_home = tmp_path / "home"
    script = str(ROOT / "scripts" / "pane_log.py")
    pipe_shell = f"[ -f {shlex.quote(script)} ] && GREENLIGHT_HOME={shlex.quote(str(gl_home))} exec {shlex.quote(sys.executable)} {shlex.quote(script)} record --pane #{{pane_id}} --socket {shlex.quote(sock)}"
    tmux_dq = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    hook_val = 'if -F "#{pane_pipe}" "" ' + tmux_dq("pipe-pane -O " + tmux_dq(pipe_shell))
    cmd = (
        f"exec tmux -L {sock} -f /dev/null start-server \\; set -g status off "
        f"\\; set-hook -g 'after-new-session[1]' {shlex.quote(hook_val)} "
        f"\\; set-hook -g 'client-attached[1]' {shlex.quote(hook_val)} "
        f"\\; new-session -A -s mc-live1 -c {shlex.quote(str(tmp_path))}"
    )

    def _open_pane():
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", PS1="$ ", SHELL="/bin/sh", ENV="/dev/null")
            os.execvp("sh", ["sh", "-c", cmd])
        assert _wait_until(
            lambda: subprocess.run(
                ["tmux", "-L", sock, "display-message", "-p", "-t", "=mc-live1:", "#{pane_pipe}"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() == "1"
        )
        return fd

    try:
        fd1 = _open_pane()
        os.write(fd1, b"printf 'BEFORE_VIEW_LINE\\n'\r")
        assert _wait_until(
            lambda: any(e["text"] == "BEFORE_VIEW_LINE" for e in pane_log.read_session("mc-live1", log_dir=gl_home / "pane-logs")["entries"])
        )
        subprocess.run(["tmux", "-L", sock, "detach-client", "-s", "mc-live1"], capture_output=True, timeout=10)
        os.close(fd1)

        # Reattach: #{pane_pipe} must stay 1 (not toggle off) and keep recording.
        fd2 = _open_pane()
        pipe_flag = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "=mc-live1:", "#{pane_pipe}"], capture_output=True, text=True, timeout=10).stdout.strip()
        assert pipe_flag == "1", "reattach kept pipe-pane active"
        time.sleep(0.1)
        os.write(fd2, b"printf 'AFTER_REATTACH_LINE\\n'\r")
        assert _wait_until(
            lambda: any(e["text"] == "AFTER_REATTACH_LINE" for e in pane_log.read_session("mc-live1", log_dir=gl_home / "pane-logs")["entries"])
        )
        os.close(fd2)

        res = pane_log.read_workspace(str(tmp_path), live_panes=[], log_dir=gl_home / "pane-logs")
        assert res is not None and res["source"] == "pane"
        texts = [e["text"] for e in res["entries"]]
        i1 = texts.index("BEFORE_VIEW_LINE")
        i2 = texts.index("AFTER_REATTACH_LINE")
        assert res["entries"][i2]["ts"] > res["entries"][i1]["ts"], "timestamps advance across time"

        # Detached session via attach(): idempotent on repeated calls.
        subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "mc-det1", "-c", str(tmp_path), "/bin/sh"], check=True, timeout=10)
        old_home = os.environ.get("GREENLIGHT_HOME")
        os.environ["GREENLIGHT_HOME"] = str(gl_home)
        try:
            assert pane_log.attach("mc-det1", cwd=str(tmp_path), socket=sock)
            assert pane_log.attach("mc-det1", cwd=str(tmp_path), socket=sock)  # idempotent
            pipe_flag2 = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "=mc-det1:", "#{pane_pipe}"], capture_output=True, text=True, timeout=10).stdout.strip()
            assert pipe_flag2 == "1"
            subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "=mc-det1:", "printf 'DETACHED_OK\\n'", "Enter"], check=True, timeout=10)
            assert _wait_until(
                lambda: any("DETACHED_OK" in e["text"] for e in pane_log.read_session("mc-det1", log_dir=gl_home / "pane-logs")["entries"])
            )
        finally:
            if old_home is None:
                os.environ.pop("GREENLIGHT_HOME", None)
            else:
                os.environ["GREENLIGHT_HOME"] = old_home
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True, timeout=10)

    # Remote-host safety: missing pane_log.py no-ops cleanly without breaking the pane.
    sock_noop = f"gl-plog-noop-{os.getpid()}"
    missing_shell = pane_log.pipe_command(str(tmp_path / "nope"), str(gl_home), sock_noop)
    hook_noop = 'if -F "#{pane_pipe}" "" ' + pane_log.tmux_quote("pipe-pane -O " + pane_log.tmux_quote(missing_shell))
    try:
        subprocess.run(
            ["tmux", "-L", sock_noop, "-f", "/dev/null", "start-server", ";", "set-hook", "-g", "after-new-session[1]", hook_noop, ";", "new-session", "-d", "-s", "mc-noop", "-c", str(tmp_path), "/bin/sh"],
            check=True,
            timeout=10,
        )
        subprocess.run(["tmux", "-L", sock_noop, "send-keys", "-t", "=mc-noop:", "echo STILL_ALIVE", "Enter"], check=True, timeout=10)
        assert _wait_until(
            lambda: re.search(
                r"STILL_ALIVE",
                subprocess.run(["tmux", "-L", sock_noop, "capture-pane", "-p", "-t", "=mc-noop:"], capture_output=True, text=True, timeout=10).stdout,
            )
        )
    finally:
        subprocess.run(["tmux", "-L", sock_noop, "kill-server"], capture_output=True, timeout=10)

    # Exact App.swift hook shape with ~/ ("$HOME"/...), $, and # in paths against live tmux.
    fake_home = tmp_path / "home"
    code_dir = fake_home / "gl$code#1"
    (code_dir / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "pane_log.py", code_dir / "scripts" / "pane_log.py")
    shutil.copy2(ROOT / "scripts" / "diag.py", code_dir / "scripts" / "diag.py")
    sock_swift = f"gl-swift-{os.getpid()}"
    sp_code = '"$HOME"/' + shlex.quote("gl$code#1")
    sp_home = '"$HOME"/' + shlex.quote(".gl#home$1")
    script_esc = pane_log.tmux_format_escape(f"{sp_code}/scripts/pane_log.py")
    home_esc = pane_log.tmux_format_escape(sp_home)
    sock_esc = pane_log.tmux_format_escape(shlex.quote(sock_swift))
    swift_pipe = f"[ -f {script_esc} ] && GREENLIGHT_HOME={home_esc} exec {shlex.quote(sys.executable)} {script_esc} record --pane #{{pane_id}} --socket {sock_esc}"
    swift_hook = 'if -F "#{pane_pipe}" "" ' + pane_log.tmux_quote("pipe-pane -O " + pane_log.tmux_quote(swift_pipe))
    swift_cmd = f"tmux -L {shlex.quote(sock_swift)} -f /dev/null start-server \\; set-hook -g 'after-new-session[1]' {shlex.quote(swift_hook)} \\; new-session -d -s mc-sw1 -c {shlex.quote(str(tmp_path))} /bin/sh"
    env_sw = {**os.environ, "HOME": str(fake_home)}
    try:
        subprocess.run(["sh", "-c", swift_cmd], env=env_sw, check=True, timeout=10)
        subprocess.run(["tmux", "-L", sock_swift, "send-keys", "-t", "=mc-sw1:", "printf 'SWIFT_HOOK_OK\\n'", "Enter"], env=env_sw, check=True, timeout=10)
        assert _wait_until(
            lambda: any("SWIFT_HOOK_OK" in e["text"] for e in pane_log.read_session("mc-sw1", log_dir=fake_home / ".gl#home$1" / "pane-logs")["entries"])
        )
    finally:
        subprocess.run(["tmux", "-L", sock_swift, "kill-server"], env=env_sw, capture_output=True, timeout=10)
