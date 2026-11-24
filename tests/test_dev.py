"""Tests for scripts/dev.py: worktree watching, mirroring, live web pages, dev log, launch agents, and retry logic."""
import base64
import contextlib
import io
import json
import pathlib
import plistlib
import subprocess

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
