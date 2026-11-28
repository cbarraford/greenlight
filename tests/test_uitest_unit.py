"""Unit tests for scripts/uitest.py (cross-platform: RPC framing, session lifecycle, CLI, fixtures)."""
import json
import os
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
        prefs = tmp_path / "user" / "Library" / "Preferences"
        prefs.mkdir(parents=True)
        (prefs / f"{sess.suite}.plist").write_text("{}")
        monkeypatch.setenv("HOME", str(tmp_path / "user"))
        sess.stop()
        assert not (prefs / f"{sess.suite}.plist").exists(), "stop() removes the empty plist `defaults delete` leaves"
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


def test_make_fixture_resolves_symlinked_root_for_native_worktrees(tmp_path):
    """On macOS /var is a symlink to /private/var; fixture ids/paths must match git worktree realpaths."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    home, _, _ = uitest._make_fixture(link, "default")
    repos = json.loads((home / "repos.json").read_text())
    goals = json.loads((home / "goals.json").read_text())
    rows = json.loads((home / "last.json").read_text())["rows"]
    resolved_prefix = str(real.resolve())
    assert repos and all(r.startswith(resolved_prefix) for r in repos), repos
    assert goals and all(k.startswith(resolved_prefix) for k in goals), goals
    assert len(rows) == 2
    assert all(r["id"].startswith(resolved_prefix) and r["path"] == r["id"] for r in rows), rows
    porcelain = subprocess.run(["git", "-C", repos[0], "worktree", "list", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout
    listed = {ln[9:] for ln in porcelain.splitlines() if ln.startswith("worktree ")}
    assert {r["id"] for r in rows} <= listed, (rows, listed)
    origin = subprocess.run(["git", "-C", repos[0], "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert origin == "https://github.com/acme/widgets.git", origin


def test_default_fixture_survives_native_rescan_with_visible_pucks(tmp_path):
    """After greenlight.py scan runs in --uitest, default fixture pucks must remain active (not shelved quiet)."""
    home, bindir, _ = uitest._make_fixture(tmp_path, "default")
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "GREENLIGHT_HOST": "local",
        "GREENLIGHT_HOME": str(home),
        "GREENLIGHT_CODE": str(ROOT),
        "GREENLIGHT_TMUX_SOCKET": f"gl-unit-none-{tmp_path.name}",
        "GREENLIGHT_ADE": "native",
        "GREENLIGHT_USAGE": "off",
    }
    scan = subprocess.run([sys.executable, str(ROOT / "scripts" / "greenlight.py"), "scan"],
                          env=env, capture_output=True, text=True)
    assert scan.returncode == 0, (scan.stdout, scan.stderr)
    snap = subprocess.run([sys.executable, str(ROOT / "scripts" / "dash.py"), "snapshot"],
                          env=env, capture_output=True, text=True, check=True)
    pucks = json.loads(snap.stdout)["pucks"]
    by_name = {p["name"]: p for p in pucks}
    assert set(by_name) >= {"fix-auth", "add-metrics"}, by_name
    for name in ("fix-auth", "add-metrics"):
        assert by_name[name]["project"] == "acme/widgets", by_name[name]
        assert by_name[name]["state"] not in ("quiet", "done"), by_name[name]


def test_adversarial_fixture_seeds_waiting_agent_and_keeps_xss_name_across_rescan(tmp_path):
    """Adversarial fixture must keep its XSS workspace name and waiting agent state after native rescan."""
    sess = uitest.UITestSession(fixture="adversarial", binary=tmp_path / "fake")
    try:
        sess.home, bindir, _ = uitest._make_fixture(sess.root, "adversarial")
        sess._seed_fixture_agents()
        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "GREENLIGHT_HOST": "local",
            "GREENLIGHT_HOME": str(sess.home),
            "GREENLIGHT_CODE": str(ROOT),
            "GREENLIGHT_TMUX_SOCKET": sess.tmux_socket,
            "GREENLIGHT_ADE": "native",
            "GREENLIGHT_USAGE": "off",
        }
        scan = subprocess.run([sys.executable, str(ROOT / "scripts" / "greenlight.py"), "scan"],
                              env=env, capture_output=True, text=True)
        assert scan.returncode == 0, (scan.stdout, scan.stderr)
        snap = subprocess.run([sys.executable, str(ROOT / "scripts" / "dash.py"), "snapshot"],
                              env=env, capture_output=True, text=True, check=True)
        pucks = json.loads(snap.stdout)["pucks"]
        assert len(pucks) == 1, pucks
        assert 'onerror="window.__pwned=1"' in pucks[0]["name"], pucks[0]
        assert pucks[0]["state"] not in ("quiet", "done"), pucks[0]
        if uitest.shutil.which("tmux"):
            assert pucks[0]["agent"]["state"] == "waiting", pucks[0]
    finally:
        sess.stop()


def test_seed_fixture_agents_handles_missing_rows(tmp_path):
    sess = uitest.UITestSession(fixture="adversarial", binary=tmp_path / "fake")
    try:
        sess.home = sess.root / "home"
        sess.home.mkdir(parents=True, exist_ok=True)
        (sess.home / "last.json").write_text(json.dumps({"rows": []}))
        with mock.patch.object(uitest.subprocess, "run") as m_run:
            sess._seed_fixture_agents()
        m_run.assert_not_called()
    finally:
        sess.stop()
