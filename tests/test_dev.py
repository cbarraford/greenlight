"""Tests for scripts/dev.py: worktree watching, mirroring, live web pages, dev log, launch agents, and retry logic."""
import base64
import contextlib
import io
import json
import pathlib
import plistlib
import subprocess
from unittest import mock

import pytest

import dev


def _sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def _events(main: pathlib.Path, **kw):
    out = io.StringIO()
    dev.watch(str(main), out=out, passes=2, period=0, **kw)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.fixture()
def dev_workspace(gl_dev_home, tmp_path):
    """A repo with two worktrees: 'new' has a build.sh that knows GREENLIGHT_DEV, 'old' does not."""
    root = (tmp_path / "wt-root").resolve()
    main = root / "main"
    (main / "app/vendor").mkdir(parents=True)
    (main / "brand/icons").mkdir(parents=True)
    (main / "scripts").mkdir()
    (main / "app/build.sh").write_text("echo old\n")
    (main / "app/Greenlight.swift").write_text("let a = 1\n")
    (main / "app/dashboard.html").write_text("<p>1</p>")
    (main / "app/vendor/t.js").write_text("1")
    (main / "brand/icons/Greenlight.icns").write_bytes(b"\x00\xff")
    (main / "scripts/dev.py").write_text("# main's\n")
    _sh("git", "init", "-q", "-b", "main", cwd=main)
    _sh("git", "add", "-A", cwd=main)
    _sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "1", cwd=main)
    _sh("git", "worktree", "add", "-q", str(root / "old"), cwd=main)
    _sh("git", "worktree", "add", "-q", str(root / "new"), cwd=main)
    _sh("git", "worktree", "add", "-q", "-b", "bad", str(root / "bad name"), cwd=main)
    new = root / "new"
    (new / "app/build.sh").write_text("echo $GREENLIGHT_APP $GREENLIGHT_DEV\n")
    return root, main, new


def test_watch_mirror_live_web_and_log(dev_workspace, monkeypatch):
    root, main, new = dev_workspace

    ev = _events(main)
    assert ev[0] == {"ev": "trees", "names": ["new", "old"]}, ev[0]  # not main, not a name that can't be an app's
    assert ev[1]["ev"] == "tool" and ev[1]["path"] == str(main / "scripts/dev.py") and ev[1]["sha"] == dev.sha1(b"# main's\n")
    ws = {e["name"]: e for e in ev if e["ev"] == "ws"}
    assert ws["new"]["ok"] and ws["new"]["ui"] and not ws["old"]["ok"] and not ws["old"]["ui"], ws
    assert sorted(ws["new"]["put"]) == ["app/Greenlight.swift", "app/build.sh", "app/dashboard.html", "app/vendor/t.js", "brand/icons/Greenlight.icns"]
    assert base64.b64decode(ws["new"]["put"]["brand/icons/Greenlight.icns"]) == b"\x00\xff" and ws["new"]["rm"] == []
    assert ws["new"]["path"] == str(new) and ws["new"]["git"] == _sh("git", "rev-parse", "--absolute-git-dir", cwd=new).strip()
    assert ws["new"]["sig"] == dev.sig(dev.Tree(new).scan()) != ws["old"]["sig"]

    # the Mac's side: the mirror matches, and a reader that has it is sent only what moved
    k = dev.Copy("new")
    dev.mirror(k, ws["new"])
    have = {"new": dev.Tree(k.src).scan()}
    assert dev.sig(have["new"]) == ws["new"]["sig"]
    (new / "app/dashboard.html").write_text("<p>2</p>")
    (new / "app/vendor/t.js").unlink()
    (new / "app/.swp").write_text("x")
    e = next(e for e in _events(main, have=have) if e["ev"] == "ws" and e["name"] == "new")
    assert list(e["put"]) == ["app/dashboard.html"] and e["rm"] == ["app/vendor/t.js"], e
    dev.mirror(k, e)
    assert (k.src / "app/dashboard.html").read_text() == "<p>2</p>" and not (k.src / "app/vendor/t.js").exists()
    e = next(e for e in _events(main, have={"new": dev.Tree(k.src).scan()}) if e["ev"] == "ws" and e["name"] == "new")
    assert e["put"] == {} and e["rm"] == [] and e["sig"] == dev.sig(dev.Tree(k.src).scan())  # still said once: the reader builds what it lacks
    for bad in ("../x", "/etc/x", "scripts/x", "app/../../x"):
        with pytest.raises(ValueError):
            dev.mirror(k, {"put": {bad: ""}})

    # a page is live (no rebuild); the Swift is not. The live pages are what the bundle ships
    (k.app / "Contents/Resources").mkdir(parents=True)
    for n in ("dashboard.html", "Greenlight.icns"):
        (k.app / "Contents/Resources" / n).write_text("")
    dev.lay_web(k)
    assert [p.name for p in k.web.iterdir()] == ["dashboard.html"] and (k.web / "dashboard.html").read_text() == "<p>2</p>"
    key = dev.build_key(k, k.src)
    (k.src / "app/dashboard.html").write_text("<p>3</p>")
    assert dev.build_key(k, k.src) == key
    (k.src / "app/Greenlight.swift").write_text("let a = 2\n")
    assert dev.build_key(k, k.src) != key

    # a worktree that goes away is said so; the tool is the install's worktree while it has one
    _sh("git", "worktree", "remove", "--force", str(root / "old"), cwd=main)
    (new / "scripts/dev.py").write_text("# new's\n")
    ev = _events(main, tool=str(new))
    assert ev[0] == {"ev": "trees", "names": ["new"]} and ev[1] == {
        "ev": "tool",
        "path": str(new / "scripts/dev.py"),
        "sha": dev.sha1(b"# new's\n"),
    }, ev[:2]
    (new / "scripts/dev.py").unlink()
    assert _events(main, tool=str(new))[1]["path"] == str(main / "scripts/dev.py")  # else main's
    assert dev.worktrees(str(root / "nowhere")) is None

    # dev.py log: the Mac's answer, only when it's for the sources as they are
    monkeypatch.chdir(new)

    def read_log():
        with contextlib.redirect_stdout(io.StringIO()) as o:
            return dev.log(0), o.getvalue()

    assert read_log()[0] == 2
    rep = pathlib.Path(ws["new"]["git"], dev.REPORT)
    rep.write_text(json.dumps({"sig": dev.sig(dev.Tree(new).scan()), "ok": False, "what": "build", "at": 0, "errors": "app/Greenlight.swift:1:1: error: nope"}))
    rc, out = read_log()
    assert rc == 1 and "error: nope" in out, out
    rep.write_text(json.dumps({"sig": dev.sig(dev.Tree(new).scan()), "ok": True, "what": "built", "secs": 15, "at": 0}))
    assert read_log()[0] == 0
    (new / "app/Greenlight.swift").write_text("let a = 3\n")
    assert read_log()[0] == 2  # that answer was for older sources
    assert _sh("git", "status", "--porcelain", cwd=new).count(dev.REPORT) == 0


def test_compiler_error_extraction():
    # the compiler's errors, without its warnings and code frames
    assert dev.errors("a.swift:1:2: warning: w\n1 | x\n  |  `- error: e\na.swift:3:4: error: e\nb: error: e2\n") == "app/a.swift:3:4: error: e\nb: error: e2"
    assert dev.errors("Sources/App.swift:42:10: error: boom\n") == "app/Sources/App.swift:42:10: error: boom"


def test_launch_agents_and_on_host_ssh(gl_dev_home):
    # the launch agents: ours, and the tunnel (a socket to the host's sshd) only when there's a host to reach
    pl = dev.plists({"host": "box", "sshd": "localhost:2222"})
    assert pl[dev.LABEL]["ProgramArguments"] == ["/usr/bin/python3", str(dev.ME), "run"] and pl[dev.LABEL]["KeepAlive"]
    t = pl[dev.TUNNEL]["ProgramArguments"]
    assert t[0] == "/usr/bin/ssh" and t[-4:] == ["-L", f"{dev.HOME}/ssh/box.sock:localhost:2222", "--", "box"] and "ControlPath=none" in t
    assert all(plistlib.loads(plistlib.dumps(p)) == p for p in pl.values()) and list(dev.plists({"host": "local"})) == [dev.LABEL]
    argv = dev.on_host({"host": "box"}, "true")  # everything of ours rides that socket
    assert argv[0] == "/usr/bin/ssh" and f"ProxyCommand=/usr/bin/nc -U '{dev.HOME}/ssh/box.sock'" in argv and argv[-3:] == ["--", "box", "true"]
    assert dev.on_host({"host": "local"}, "true") == ["/bin/sh", "-c", "true"]


def test_make_retries_signing_but_skips_same_compiler_error(gl_dev_home, monkeypatch):
    # a failed build: a compiler error waits for the sources to change; any other failure (build.sh's signing) is tried again
    banners = []
    monkeypatch.setattr(dev, "banner", lambda *a: banners.append(a))
    r = dev.Copy("retry")
    (r.src / "app").mkdir(parents=True)
    runs = r.dir / "runs"
    (r.src / "app/Greenlight.swift").write_text("let a = 1\n")
    (r.src / "app/build.sh").write_text(f'echo x >> {runs}; echo "build.sh: error: signing with \\"Apple Development: T (X)\\" failed" >&2; exit 1\n')
    for _ in range(2):
        dev.make({}, r, {})
    assert runs.read_text().count("x") == 2 and len(banners) == 1, (runs.read_text(), banners)  # built again, one banner

    (r.src / "app/build.sh").write_text(f'echo x >> {runs}; echo "Greenlight.swift:1:5: error: nope"; exit 1\n')
    for _ in range(2):
        dev.make({}, r, {})
    assert runs.read_text().count("x") == 3 and len(banners) == 1, (runs.read_text(), banners)  # the same sources aren't built again


def test_swift_sources_keep_dev_copy_hooks():
    # guard against merge-conflict regressions dropping dev-copy tunneling/live pages from Swift sources
    swift = "\n".join(p.read_text() for p in sorted((pathlib.Path(__file__).resolve().parent.parent / "app/Sources").glob("*.swift")))
    for needle in (
        'forInfoDictionaryKey: "GreenlightDev"',
        "func devSsh(",
        "ProxyCommand=/usr/bin/nc -U",
        "devSsh(host)",
        "devSsh(at.host)",
        "func watchPages(",
        'page("dashboard")',
        'page("terminal")',
        'page("stage")',
        '.init(name: "dev", value: devName ?? "")',
    ):
        assert needle in swift, f"app/Sources/*.swift is missing dev-copy hook: {needle}"


def test_make_builds_caches_and_handles_failure(gl_dev_home, monkeypatch):
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


def test_fetch_tool_validates_sha_and_syntax(gl_dev_home):
    dev.ME.write_bytes(b"print('old')\n")
    new_src = b"print('new')\n"
    new_sha = dev.sha1(new_src)

    assert dev.fetch_tool({"host": "local"}, {"sha": dev.sha1(b"print('old')\n"), "path": "/repo/dev.py"}) is False

    bad_src = b"def broken(:\n"
    with mock.patch.object(dev.subprocess, "run", return_value=subprocess.CompletedProcess(["sh"], 0, stdout=bad_src, stderr=b"")):
        assert dev.fetch_tool({"host": "local"}, {"sha": dev.sha1(bad_src), "path": "/repo/dev.py"}) is False

    with mock.patch.object(dev.subprocess, "run", return_value=subprocess.CompletedProcess(["sh"], 0, stdout=new_src, stderr=b"")):
        assert dev.fetch_tool({"host": "local"}, {"sha": new_sha, "path": "/repo/dev.py"}) is True
    assert dev.ME.read_bytes() == new_src


def test_install_status_and_uninstall(gl_dev_home, monkeypatch, capsys):
    home, agents = gl_dev_home["home"], gl_dev_home["agents"]
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

    with mock.patch.object(dev.Path, "exists", return_value=True), pytest.raises(SystemExit):
        dev.install("-bad", "/repo")


def test_copy_process_helpers_run_loop_and_main_cli(gl_dev_home):
    home = gl_dev_home["home"]
    k = dev.Copy("feat-2")
    k.app.mkdir(parents=True)
    (k.app / "Contents").mkdir()
    (k.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.greenlight.app.feat-2"}))
    assert k.bundle_id() == "dev.greenlight.app.feat-2"

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

    with mock.patch.object(dev, "status") as m_st:
        dev.main(["status"])
        m_st.assert_called_once()
    with mock.patch.object(dev, "log", return_value=0), pytest.raises(SystemExit) as ex:
        dev.main(["log", "--wait", "1"])
    assert ex.value.code == 0
    with pytest.raises(SystemExit):
        dev.main(["--unknown"])
