"""Extended pytest coverage for scripts/dev.py (make, fetch_tool, install, uninstall, status, main)."""
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dev  # noqa: E402


@pytest.fixture()
def isolated_dev(monkeypatch):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="gl-d-", dir="/tmp") as td:
        base = Path(td)
        home = base / "dev"
        agents = base / "LA"
        apps = base / "Apps"
        home.mkdir()
        agents.mkdir()
        apps.mkdir()
        monkeypatch.setattr(dev, "HOME", home)
        monkeypatch.setattr(dev, "ME", home / "dev.py")
        monkeypatch.setattr(dev, "AGENTS", agents)
        monkeypatch.setattr(dev, "APPS", apps)
        yield home, agents, apps


def test_make_builds_caches_and_handles_failure(isolated_dev, monkeypatch):
    home, _, apps = isolated_dev
    k = dev.Copy("feat-1")
    k.src.mkdir(parents=True)
    (k.src / "app").mkdir()
    (k.src / "app" / "build.sh").write_text("#!/bin/sh\nexit 0\n")
    (k.src / "app" / "dashboard.html").write_text("<html></html>")

    reports = []
    monkeypatch.setattr(dev, "report", lambda _c, _ev, **kw: reports.append(kw))
    monkeypatch.setattr(dev, "seed", lambda _c, _k, _ev: (k.dir / "id").write_text("id1"))
    monkeypatch.setattr(dev, "banner", lambda *_a: None)
    monkeypatch.setattr(dev, "say", lambda *_a: None)
    monkeypatch.setattr(k, "front", lambda: False)
    monkeypatch.setattr(k, "stop", lambda: True)
    opened = []
    monkeypatch.setattr(k, "open", lambda front=False: opened.append(front))
    monkeypatch.setattr(k, "pids", list)

    # 1. Build failure with Swift compiler error caches failed key
    fail = subprocess.CompletedProcess(["bash"], 1, stdout="", stderr="App.swift:12:5: error: boom\n")
    with mock.patch.object(dev.subprocess, "run", return_value=fail):
        dev.make({"host": "local"}, k, {"ev": "ws", "name": "feat-1"})
    assert reports[-1]["ok"] is False and "App.swift:12:5" in reports[-1]["errors"]

    # Second call with same key returns cached failure without rebuilding
    with mock.patch.object(dev.subprocess, "run") as m_run:
        dev.make({"host": "local"}, k, {"ev": "ws", "name": "feat-1"})
        m_run.assert_not_called()
    assert reports[-1]["ok"] is False

    # 2. Successful build writes built key, relaunches, and opens once on ui=True
    (k.dir / "failed").unlink()
    ok = subprocess.CompletedProcess(["bash"], 0, stdout="ok", stderr="")
    with mock.patch.object(dev.subprocess, "run", return_value=ok):
        k.app.mkdir(parents=True)
        dev.make({"host": "local"}, k, {"ev": "ws", "name": "feat-1", "ui": True})
    assert reports[-1]["ok"] is True and reports[-1]["what"] == "built"
    assert (k.dir / "opened").exists() and len(opened) == 2

    # 3. Unchanged key hits fast "live" branch
    with mock.patch.object(dev.subprocess, "run") as m_run:
        dev.make({"host": "local"}, k, {"ev": "ws", "name": "feat-1"})
        m_run.assert_not_called()
    assert reports[-1]["what"] == "live"


def test_fetch_tool_validates_sha_and_syntax(isolated_dev):
    home, _, _ = isolated_dev
    dev.ME.write_bytes(b"print('old')\n")
    new_src = b"print('new')\n"
    new_sha = dev.sha1(new_src)

    # Same sha -> False
    assert dev.fetch_tool({"host": "local"}, {"sha": dev.sha1(b"print('old')\n"), "path": "/repo/dev.py"}) is False

    # Bad syntax -> rejected
    bad_src = b"def broken(:\n"
    with mock.patch.object(dev.subprocess, "run", return_value=subprocess.CompletedProcess(["sh"], 0, stdout=bad_src, stderr=b"")):
        assert dev.fetch_tool({"host": "local"}, {"sha": dev.sha1(bad_src), "path": "/repo/dev.py"}) is False

    # Valid update -> written and returns True
    with mock.patch.object(dev.subprocess, "run", return_value=subprocess.CompletedProcess(["sh"], 0, stdout=new_src, stderr=b"")):
        assert dev.fetch_tool({"host": "local"}, {"sha": new_sha, "path": "/repo/dev.py"}) is True
    assert dev.ME.read_bytes() == new_src


def test_install_status_and_uninstall(isolated_dev, monkeypatch, capsys):
    home, agents, apps = isolated_dev
    monkeypatch.setattr(dev.sys, "platform", "darwin")
    monkeypatch.setattr(dev.time, "sleep", lambda _s: None)

    fake_out = b"/repo/tool\nworktree /repo/main\nprint('devtool')\n"
    def fake_run(cmd, **kw):
        if "git rev-parse" in " ".join(str(x) for x in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=fake_out, stderr=b"")
        if cmd[:2] == ["/usr/bin/ssh", "-G"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="port 2222\n", stderr="")
        if cmd[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="state = running\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with (
        mock.patch.object(dev.Path, "exists", return_value=True),
        mock.patch.object(dev.subprocess, "run", side_effect=fake_run),
    ):
        dev.install("devbox", "~/repo")
        assert (home / "conf.json").is_file()
        plist_data = plistlib.loads((agents / f"{dev.TUNNEL}.plist").read_bytes())
        assert "localhost:2222" in " ".join(plist_data["ProgramArguments"])

        ws_dir = home / "ws" / "feat-1"
        ws_dir.mkdir(parents=True)
        dev.status()
        out = capsys.readouterr().out
        assert "host devbox" in out and "tunnel: up" in out and "greenlight-feat-1: no build yet" in out

        dev.uninstall()
        assert "Dev copies off" in capsys.readouterr().out
    assert not home.exists()

    # Invalid host rejected
    with mock.patch.object(dev.Path, "exists", return_value=True), pytest.raises(SystemExit):
        dev.install("-bad", "/repo")


def test_copy_process_helpers_run_loop_and_main_cli(isolated_dev, monkeypatch):
    home, _, apps = isolated_dev
    k = dev.Copy("feat-2")
    k.app.mkdir(parents=True)
    (k.app / "Contents").mkdir()
    (k.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.greenlight.app.feat-2"}))
    assert k.bundle_id() == "dev.greenlight.app.feat-2"

    # pids(), stop(), front(), open()
    pid_seq = iter([[1234], [1234], []])
    killed = []
    with (
        mock.patch.object(k, "pids", side_effect=lambda: next(pid_seq, [])),
        mock.patch.object(dev.os, "kill", side_effect=lambda p, s: killed.append((p, s))),
        mock.patch.object(dev.time, "sleep"),
    ):
        assert k.stop() is True
    assert killed and killed[0][0] == 1234

    def fake_run(cmd, **kw):
        if cmd[:2] == ["/usr/bin/lsappinfo", "front"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="ASN:0x1\n", stderr="")
        if cmd[:2] == ["/usr/bin/lsappinfo", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout='"bundleid"="dev.greenlight.app.feat-2"\n', stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=f"4321 {k.app}/Contents/MacOS/greenlight-feat-2\n", stderr="")

    with mock.patch.object(dev.subprocess, "run", side_effect=fake_run):
        assert k.pids() == [4321]
        assert k.front() is True
        k.open(front=True)

    # dev.run() handles trees drop, ws ok/not-ok, and tool restart (os.execv)
    (home / "conf.json").write_text('{"host": "local", "repo": "/repo", "tool": "/repo"}')
    dev.ME.write_text("# dev.py\n")
    (home / "ws" / "stale-ws").mkdir(parents=True)
    events = [
        json.dumps({"ev": "trees", "names": ["keep-ws"]}) + "\n",
        json.dumps({"ev": "ws", "name": "old-ws", "ok": False}) + "\n",
        json.dumps({"ev": "ws", "name": "keep-ws", "ok": True, "files": {}, "ui": False}) + "\n",
        json.dumps({"ev": "tool", "sha": "abc", "path": "/repo/scripts/dev.py"}) + "\n",
    ]
    fake_proc = mock.MagicMock()
    fake_proc.stdout = iter(events)
    dropped, mirrored, reported = [], [], []
    with (
        mock.patch.object(dev.subprocess, "Popen", return_value=fake_proc),
        mock.patch.object(dev, "drop", side_effect=lambda c: dropped.append(c.name)),
        mock.patch.object(dev, "mirror", side_effect=lambda c, ev: mirrored.append(c.name)),
        mock.patch.object(dev, "lay_web"),
        mock.patch.object(dev, "make"),
        mock.patch.object(dev, "report", side_effect=lambda *a, **kw: reported.append(kw)),
        mock.patch.object(dev, "fetch_tool", return_value=True),
        mock.patch.object(dev, "say"),
        mock.patch.object(dev.os, "execv", side_effect=SystemExit(0)),
        pytest.raises(SystemExit),
    ):
        dev.run()
    assert dropped == ["stale-ws"] and mirrored == ["keep-ws"] and any(not r.get("ok") for r in reported)

    # main() CLI routing
    with mock.patch.object(dev, "status") as m_st:
        dev.main(["status"])
        m_st.assert_called_once()
    with mock.patch.object(dev, "log", return_value=0), pytest.raises(SystemExit) as ex:
        dev.main(["log", "--wait", "1"])
    assert ex.value.code == 0
    with pytest.raises(SystemExit):
        dev.main(["--unknown"])
