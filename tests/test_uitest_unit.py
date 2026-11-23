"""Unit tests for scripts/uitest.py (cross-platform: RPC framing, session lifecycle, CLI, fixtures)."""
import json
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import uitest  # noqa: E402


def _start_unix_echo_server(sock_path: Path, responder):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(4)

    def serve():
        try:
            while True:
                conn, _ = srv.accept()
                with conn:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        req = json.loads(data.decode())
                        resp = responder(req)
                        conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv


def test_rpc_roundtrip_and_wait_for(tmp_path):
    # Use /tmp short path for macOS AF_UNIX 104-byte sun_path limit
    with tempfile.TemporaryDirectory(prefix="gl-u-", dir="/tmp") as sdir:
        sock = Path(sdir) / "ctl.sock"
        calls = {"n": 0}
        def respond(req):
            calls["n"] += 1
            if req.get("cmd") == "eval":
                return {"ok": True, "result": calls["n"] >= 2}
            return {"ok": True, "echo": req}

        srv = _start_unix_echo_server(sock, respond)
        try:
            assert uitest.rpc(sock, {"cmd": "ping"})["echo"] == {"cmd": "ping"}
            sess = uitest.UITestSession(fixture="default", binary=tmp_path / "fake")
            sess.socket_path = sock
            assert sess.wait_for("dashboard", "window.ready", timeout=2.0, interval=0.01) is True
        finally:
            srv.close()


def test_wait_for_timeout_raises(tmp_path):
    sess = uitest.UITestSession(fixture="default", binary=tmp_path / "fake")
    with mock.patch.object(sess, "eval", return_value={"ok": True, "result": False}), pytest.raises(TimeoutError, match="timed out"):
        sess.wait_for("dashboard", "false", timeout=0.05, interval=0.01)


def test_session_start_and_stop_lifecycle(tmp_path):
    fake_bin = tmp_path / "Greenlight-fake"
    fake_bin.write_text("#!/bin/sh\nsleep 1\n")
    fake_bin.chmod(0o755)
    sess = uitest.UITestSession(fixture="empty", binary=fake_bin)
    fake_proc = mock.MagicMock()
    fake_proc.pid = 99999
    fake_proc.poll.return_value = None
    fake_proc.wait.side_effect = [subprocess.TimeoutExpired("gl", 5), 0]

    def fake_popen(*_a, **_kw):
        sess.socket_path.write_text("")
        return fake_proc

    with (
        mock.patch.object(uitest.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(uitest, "rpc", return_value={"ok": True}),
        mock.patch.object(uitest.subprocess, "run", return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")),
    ):
        assert sess.start() is sess
        assert (sess.root / "meta.json").is_file()
        sess.stop()
    fake_proc.kill.assert_called_once()
    assert not sess.root.exists()


def test_keep_defaults_relaunch_reuses_one_suite_and_stop_removes_it(tmp_path, monkeypatch):
    """keep_defaults: every launch gets the same GREENLIGHT_UITEST_SUITE, relaunch() keeps the home, stop() deletes the suite;
    without it, a suite inherited from the caller's shell is dropped."""
    monkeypatch.setenv("GREENLIGHT_UITEST_SUITE", "dev.greenlight.uitest.inherited")
    fake_bin = tmp_path / "Greenlight-fake"
    fake_bin.write_text("#!/bin/sh\nsleep 1\n")
    fake_bin.chmod(0o755)
    envs, runs = [], []

    def session(**kw):
        sess = uitest.UITestSession(fixture="projects", binary=fake_bin, **kw)

        def fake_popen(*_a, **k):
            envs.append(k["env"])
            sess.socket_path.write_text("")
            p = mock.MagicMock(pid=99999)
            p.poll.return_value = None
            return p
        return sess, fake_popen

    def no_group(_pgid, sig):  # the fake pid is no process group of ours: never signalled, and gone when probed
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(uitest.os, "killpg", no_group)
    sess, fake_popen = session(keep_defaults=True)
    with (
        mock.patch.object(uitest.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(uitest, "rpc", return_value={"ok": True}),
        mock.patch.object(uitest.subprocess, "run", side_effect=lambda a, **_k: runs.append(a) or subprocess.CompletedProcess(a, 0, "", "")),
    ):
        sess.start()
        projects = json.loads((sess.home / "app-support" / "projects.json").read_text())
        assert [p["id"] for p in projects] == ["pauth", "pmetrics"], projects
        home = sess.home
        sess.relaunch()
        assert sess.home == home and (home / "app-support" / "projects.json").is_file(), "relaunch keeps the home"
        sess.stop()
    assert len(envs) == 2 and envs[0]["GREENLIGHT_UITEST_SUITE"] == envs[1]["GREENLIGHT_UITEST_SUITE"] == sess.suite
    assert sess.suite.startswith("dev.greenlight.uitest.s")
    assert ["defaults", "delete", sess.suite] in runs, runs
    assert not sess.root.exists()

    plain, fake_popen = session()
    with (
        mock.patch.object(uitest.subprocess, "Popen", side_effect=fake_popen),
        mock.patch.object(uitest, "rpc", return_value={"ok": True}),
        mock.patch.object(uitest.subprocess, "run", return_value=subprocess.CompletedProcess(["x"], 0, "", "")),
    ):
        plain.start()
        with pytest.raises(AssertionError):
            plain.relaunch()  # relaunch needs keep_defaults
        plain.stop()
    assert "GREENLIGHT_UITEST_SUITE" not in envs[-1], "an inherited suite must not reach a fresh session"


def test_cli_subcommands(tmp_path, monkeypatch, capsys):
    sock = tmp_path / "ctl.sock"
    (tmp_path / "meta.json").write_text(json.dumps({"pid": 0, "tmux_socket": "gl-test", "root": str(tmp_path / "gl-uitest-x")}))

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["uitest.py", *args])
        rc = uitest.main()
        return rc, capsys.readouterr().out

    with (
        mock.patch.object(uitest, "rpc", return_value={"ok": True, "result": True}) as m_rpc,
        mock.patch.object(uitest.subprocess, "run", return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")),
    ):
        for cmd in (
            ("ping", "--socket", str(sock)),
            ("state", "--socket", str(sock)),
            ("action", "--socket", str(sock), "floor", "--to", "center"),
            ("eval", "--socket", str(sock), "--target", "dashboard", "1+1"),
            ("click", "--socket", str(sock), "#btn"),
            ("type", "--socket", str(sock), "#inp", "hello"),
            ("screenshot", "--socket", str(sock), "--out", str(tmp_path / "s.png")),
            ("wait", "--socket", str(sock), "true", "--timeout", "1"),
            ("stop", "--socket", str(sock)),
        ):
            rc, out = run(*cmd)
            assert rc == 0 and json.loads(out)["ok"] is True
        assert m_rpc.call_count >= 9

    # wait timeout returns 1
    with mock.patch.object(uitest, "rpc", return_value={"ok": True, "result": False}):
        rc, out = run("wait", "--socket", str(sock), "false", "--timeout", "0.02")
        assert rc == 1 and json.loads(out)["error"] == "timeout"
