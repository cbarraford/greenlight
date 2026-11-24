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
    assert [x["id"] for x in lines] == [f"mc-s1.g0:{i}" for i in range(6)]
    assert pane_log.detect_tool(lines) == ("Bash", "pytest -q")


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
    # Also matches when a live tmux pane cd'd into /w/other/sub.
    assert pane_log.workspace_sessions("/w/other", live_panes=[{"session": "mc-ws1", "cwd": "/w/other/sub"}], log_dir=log_dir) == ["mc-ws1"]

    p1 = pane_log.read_workspace("/w/app", live_panes=[], limit=5, log_dir=log_dir)
    assert [e["text"] for e in p1["entries"]] == [f"out {i}" for i in range(10, 15)]
    assert p1["more"] == "mc-ws1.g0:10"

    p2 = pane_log.read_workspace("/w/app", live_panes=[], before=p1["more"], limit=5, log_dir=log_dir)
    assert [e["text"] for e in p2["entries"]] == [f"out {i}" for i in range(5, 10)]
    assert p2["more"] == "mc-ws1.g0:5"

    p3 = pane_log.read_workspace("/w/app", live_panes=[], before=p2["more"], limit=10, log_dir=log_dir)
    assert [e["text"] for e in p3["entries"]] == [f"out {i}" for i in range(5)]
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
        time.sleep(0.5)
        return fd

    try:
        fd1 = _open_pane()
        os.write(fd1, b"printf 'BEFORE_VIEW_LINE\\n'\r")
        time.sleep(0.5)
        subprocess.run(["tmux", "-L", sock, "detach-client", "-s", "mc-live1"], capture_output=True, timeout=10)
        time.sleep(0.2)
        os.close(fd1)

        # Reattach: #{pane_pipe} must stay 1 (not toggle off) and keep recording.
        fd2 = _open_pane()
        pipe_flag = subprocess.run(["tmux", "-L", sock, "display-message", "-p", "-t", "=mc-live1:", "#{pane_pipe}"], capture_output=True, text=True, timeout=10).stdout.strip()
        assert pipe_flag == "1", "reattach kept pipe-pane active"
        time.sleep(0.2)
        os.write(fd2, b"printf 'AFTER_REATTACH_LINE\\n'\r")
        time.sleep(0.5)
        os.close(fd2)

        res = pane_log.read_workspace(str(tmp_path), live_panes=[], log_dir=gl_home / "pane-logs")
        assert res is not None and res["source"] == "pane"
        texts = [e["text"] for e in res["entries"]]
        i1 = next(i for i, t in enumerate(texts) if "BEFORE_VIEW_LINE" in t)
        i2 = next(i for i, t in enumerate(texts) if "AFTER_REATTACH_LINE" in t)
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
            time.sleep(0.5)
            det = pane_log.read_session("mc-det1", log_dir=gl_home / "pane-logs")
            assert any("DETACHED_OK" in e["text"] for e in det["entries"])
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
    hook_noop = 'if -F "#{pane_pipe}" "" ' + tmux_dq("pipe-pane -O " + tmux_dq(missing_shell))
    try:
        subprocess.run(
            ["tmux", "-L", sock_noop, "-f", "/dev/null", "start-server", ";", "set-hook", "-g", "after-new-session[1]", hook_noop, ";", "new-session", "-d", "-s", "mc-noop", "-c", str(tmp_path), "/bin/sh"],
            check=True,
            timeout=10,
        )
        subprocess.run(["tmux", "-L", sock_noop, "send-keys", "-t", "=mc-noop:", "echo STILL_ALIVE", "Enter"], check=True, timeout=10)
        time.sleep(0.3)
        out = subprocess.run(["tmux", "-L", sock_noop, "capture-pane", "-p", "-t", "=mc-noop:"], capture_output=True, text=True, timeout=10).stdout
        assert re.search(r"STILL_ALIVE", out)
    finally:
        subprocess.run(["tmux", "-L", sock_noop, "kill-server"], capture_output=True, timeout=10)
