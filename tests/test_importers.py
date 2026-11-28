"""importers.py: read-only one-shot imports of another ADE's repos and goals into the native engine (#195)."""
import json
import os
import subprocess
import types

import pytest

import ade_basement
import ade_common
import dash
import importers


def git_init(p):
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True, capture_output=True)
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ade_basement, "REPOS", tmp_path / "home" / "repos.json")
    monkeypatch.setattr(ade_common, "GOALS", tmp_path / "home" / "goals.json")
    return tmp_path


def fake_backend(rows, repos=()):
    return types.SimpleNamespace(worktrees=lambda: rows, repos=lambda: {"repos": [{"path": r} for r in repos]})


def test_folder_scan(home):
    code = home / "code"
    a = git_init(code / "a")
    b = git_init(code / "group" / "b")
    git_init(code / "group" / "b" / "vendored")  # inside a repo: the repo's, not another
    git_init(code / "node_modules" / "x")         # skipped folder
    git_init(code / "1" / "2" / "3" / "deep")     # past MAX_DEPTH
    build = git_init(code / "build")              # a repo named like a skipped folder: still a repo
    git_init(code / ".nvm")                       # a hidden tool checkout: never
    (code / "wt").mkdir()
    (code / "wt" / ".git").write_text("gitdir: /elsewhere")  # a linked worktree, not a main checkout
    repos = importers.from_folder(str(code))
    assert sorted(repos) == sorted([str(a.resolve()), str(b.resolve()), str(build.resolve())])
    with pytest.raises(RuntimeError, match="not a folder"):
        importers.from_folder(str(home / "missing"))
    git_init(home / "dots")  # the folder you name is a repo itself (dotfiles in ~): it, and the checkouts below it
    git_init(home / "dots" / "code" / "app")
    assert sorted(importers.from_folder(str(home / "dots"))) == sorted([str((home / "dots").resolve()), str((home / "dots" / "code" / "app").resolve())])
    shut = home / "shut"
    shut.mkdir(mode=0o000)
    if not os.access(shut, os.R_OK):  # (root can read anything)
        with pytest.raises(RuntimeError, match="can't open"):
            importers.from_folder(str(shut))  # unreadable: says so, never "0 repos"
        inner = code / "team"
        inner.mkdir(mode=0o000)  # a subfolder that can't be opened: named, and the rest still imports
        try:
            got = importers.gather(["folder"], [str(code)])["folder"]
            assert f"can't open: {inner}" in got["error"] and str(a.resolve()) in got["repos"]  # named, never silently missed
        finally:
            inner.chmod(0o700)
    shut.chmod(0o700)
    many = importers.gather(["folder"], [str(home / f"no{i}") for i in range(5)] + [str(home / "no0")])["folder"]["error"]
    assert many.count("not a folder") == 3 and many.endswith("and 2 more")  # capped, each named once
    got = importers.gather(["folder"], [str(code), str(home / "typo")])["folder"]  # one bad folder doesn't drop the rest
    assert str(a.resolve()) in got["repos"] and "not a folder" in got["error"]


def test_from_backend_reads_repos_and_goals(home, monkeypatch):
    import greenlight
    r = git_init(home / "r")
    sub = r / "src"
    sub.mkdir()
    rows = [{"path": str(sub), "repoId": "a9075331-opaque", "comment": "Merge !4: ship it"},  # cmux: a subfolder; Orca: an id
            {"path": str(r), "repoId": "x", "comment": "Rebase !1 — dispatched 2026-01-13"},  # a status line, not a goal
            {"path": str(r), "hostId": "laptop", "comment": "the laptop's"},                  # another paired machine's
            {"path": str(home), "comment": "in a folder that isn't a checkout"},               # cmux in ~: no workspace shows it
            {"path": str(r), "comment": "  ", "isArchived": True}]
    other, remote = git_init(home / "no-worktrees"), git_init(home / "remote-here")
    monkeypatch.setattr(importers.ade, "get", lambda n: fake_backend(rows, [str(other), str(remote), "/srv/gone"]))
    monkeypatch.setattr(greenlight, "project_setups", lambda tries=2: [{"path": str(other), "hostId": "local"},
                                                                        {"path": str(remote), "hostId": "laptop"}, {"hostId": "local"}])
    repos, goals = importers.from_backend("orca")
    top = os.path.realpath(r)
    assert repos == [top, top, str(other)]  # a remote repo whose path happens to exist here is not this machine's
    assert goals == {top: "Merge !4: ship it"}  # keyed by the worktree's root, as the native engine reads it
    monkeypatch.setattr(greenlight, "project_setups", lambda tries=2: None)  # Orca can't say: no repos beyond its worktrees
    assert importers.from_backend("orca")[0] == [top, top]
    assert str(other) not in importers.from_backend("cmux")[0]  # only Orca lists repos with no worktree


def test_plan_is_non_destructive_and_apply_writes_only_new(home, monkeypatch):
    r1, r2 = git_init(home / "r1"), git_init(home / "r2")
    ade_basement.remember(str(r1.resolve()))
    ade_common.set_goal("/w/a", "Mine")
    found = {"orca": {"repos": [str(r1), str(r2), str(r2 / "sub-missing")], "goals": {"/w/a": "Theirs", "/w/b": "Ship B"}},
             "cmux": {"repos": [str(r2)], "goals": {"/w/b": "Other B", "/w/c": "Ship C"}},
             "herdr": {"error": "FileNotFoundError: herdr"}}
    p = importers.plan(found)
    assert p["repos"] == [str(r2.resolve())]
    assert p["goals"] == {"/w/b": "Ship B", "/w/c": "Ship C"}  # the first source wins a path; never over "Mine"
    assert p["kept"] == {"/w/a": {"kept": "Mine", "from": "orca"}}
    assert p["sources"]["herdr"] == {"repos": 0, "goals": 0, "error": "FileNotFoundError: herdr"}
    assert p["sources"]["orca"] == {"repos": 3, "goals": 2}
    assert ade_basement.saved() == [str(r1.resolve())]  # a plan changes nothing
    ade_common.set_goal("/w/c", "Set meanwhile")  # between preview and apply: it stands
    importers.apply(p)
    assert ade_basement.saved() == [str(r1.resolve()), str(r2.resolve())]
    assert ade_common.goals() == {"/w/a": "Mine", "/w/b": "Ship B", "/w/c": "Set meanwhile"}
    assert importers.plan(found)["repos"] == [] and importers.plan(found)["goals"] == {}  # run again: nothing new


def test_apply_reports_what_it_wrote_and_never_replaces_an_unreadable_file(home):
    ade_common.set_goal("/w/c", "Set meanwhile")
    p = {"repos": [], "goals": {"/w/b": "Ship B", "/w/c": "Theirs"}, "kept": {}, "sources": {}}
    assert importers.apply(p)["goals"] == {"/w/b": "Ship B"}  # the goal set since the preview isn't counted
    ade_common.GOALS.write_text("{half written")
    with pytest.raises(RuntimeError, match="goals.json can't be read"):
        importers.apply({"repos": [], "goals": {"/w/d": "x"}, "kept": {}, "sources": {}})
    assert ade_common.GOALS.read_text() == "{half written"  # left for the user to fix
    ade_common.GOALS.write_text("[]")  # parses, but isn't goals
    with pytest.raises(RuntimeError):
        importers.apply({"repos": [], "goals": {}, "kept": {}, "sources": {}})
    with pytest.raises(RuntimeError, match="goals.json can't be read"):  # the preview refuses too, before reading it as goals
        importers.plan({"orca": {"repos": [], "goals": {"/w/x": "Goal"}}})
    assert "can't be read" in dash.settings("import-apply", "-", json.dumps({"from": ["folder"], "paths": [str(home)]}))["error"]


def test_gather_reports_a_failing_source(home, monkeypatch):
    def get(n):
        if n == "t3":
            raise RuntimeError("t3 server not running")
        return fake_backend([])
    monkeypatch.setattr(importers.ade, "get", get)
    got = importers.gather(["orca", "t3", "folder"], [str(home)])
    assert got["orca"] == {"repos": [], "goals": {}} and "not running" in got["t3"]["error"] and "repos" in got["folder"]


def test_run_cli_and_dash_ops(home, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    git_init(home / "code" / "x")
    monkeypatch.setattr(importers, "detect", lambda: {"orca": False, "t3": False, "cmux": False, "herdr": False, "folder": True})
    assert importers.run(["preview"])["repos"] == []  # folder needs a --path: nothing detected to read
    assert importers.run(["--path", str(home / "code")])["repos"]  # the verb is optional: preview
    with pytest.raises(SystemExit, match="unknown source"):
        importers.run(["preview", "--from", "rex"])
    with pytest.raises(SystemExit, match="needs --path"):
        importers.run(["preview", "--from", "folder"])
    p = importers.run(["preview", "--path", str(home / "code")])
    assert p["repos"] == [str((home / "code" / "x").resolve())] and ade_basement.saved() == []
    ask = lambda verb, o: dash.settings(f"import-{verb}", "-", json.dumps(o))
    d = ask("detect", {})
    assert d["sources"]["folder"] is True and d["native"] is True and d["repos"] == 0
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    ade_basement.remember(str((home / "code" / "x").resolve()))
    assert ask("detect", {})["native"] is True and ask("detect", {})["repos"] == 1
    ade_basement.forget(str((home / "code" / "x").resolve()))
    assert dash.settings("import-preview", "-", "[]")["error"] == "bad request"  # not an object
    assert "bad source" in ask("preview", {"from": ["rex"]})["error"]
    assert "full path" in ask("preview", {"from": ["folder"], "paths": ["code"]})["error"]
    assert "full path" in ask("preview", {"from": ["folder"], "paths": ["/srv/../etc"]})["error"]
    assert "full path" in ask("preview", {"from": ["folder"], "paths": ["~root/x"]})["error"]  # another user's home
    assert "pick a source" in ask("preview", {"from": []})["error"]
    assert "needs --path" in ask("preview", {"from": ["folder"]})["error"]
    assert "no op" in ask("frob", {})["error"]
    out = ask("apply", {"from": ["folder"], "paths": [str(home / "code")]})
    assert out["repos"] == ade_basement.saved() == [str((home / "code" / "x").resolve())]


def test_first_run_imports_once_on_native(home, monkeypatch):
    import greenlight
    monkeypatch.setattr(greenlight, "project_setups", lambda tries=2: None)  # never this machine's real Orca
    monkeypatch.setattr(ade_basement, "HOME", home / "home")
    r = git_init(home / "r")
    monkeypatch.setattr(importers, "detect", lambda: {"orca": True, "t3": False, "cmux": False, "herdr": False, "folder": True})
    calls = []
    monkeypatch.setattr(importers.ade, "get", lambda n: calls.append(n) or fake_backend([{"path": str(r), "repoId": str(r), "comment": "Main goal"}]))
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    assert importers.first_run() == {"repos": 1, "goals": 1, "sources": {"orca": {"repos": 1, "goals": 1}}, "pending": [], "attempts": 1}
    assert calls == ["orca", "orca"] and ade_basement.saved() == [str(r.resolve())] and ade_common.goals() == {str(r): "Main goal"}
    assert json.loads((home / "home" / importers.FIRST_RUN).read_text())["repos"] == 1
    assert importers.first_run() is None and calls == ["orca", "orca"]  # once
    (home / "home" / importers.FIRST_RUN).unlink()
    assert importers.first_run()["pending"] == [] and calls == ["orca", "orca"]  # repos.json exists: already native, nothing re-added
    (home / "home" / importers.FIRST_RUN).unlink()
    ade_basement.REPOS.unlink()
    monkeypatch.setattr(importers, "gather", lambda *a: 1 / 0)  # a failing import never raises, and is tried again later
    assert importers.first_run() == {"error": "ZeroDivisionError", "pending": ["orca"], "attempts": 1}
    assert importers.first_run() is None  # not before RETRY_SECS
    mk = home / "home" / importers.FIRST_RUN
    old = json.loads(mk.read_text())
    mk.write_text(json.dumps({**old, "ts": 0}))
    monkeypatch.setattr(importers, "gather", lambda s: calls.append(tuple(s)) or {"orca": {"repos": [], "goals": {}}})
    assert importers.first_run()["pending"] == [] and calls[-1] == ("orca",)  # only the source that failed is tried again
    assert importers.first_run() is None  # nothing pending: done
    lock = (home / "home" / importers.FIRST_RUN).with_suffix(".lock")
    mk.write_text(json.dumps({"ts": 0, "pending": ["orca"], "attempts": 1}))
    lock.write_text("")  # another listing is importing right now
    assert importers.first_run() is None and json.loads(mk.read_text())["attempts"] == 1
    lock.unlink()
    mk.write_text(json.dumps({"ts": 0, "pending": ["orca"], "attempts": importers.RETRIES}))
    assert importers.first_run() is None  # gave up after a few tries (Settings → Import is still there)
    mk.write_text(json.dumps({"ts": "x", "pending": ["orca"], "attempts": "y"}))
    assert importers.first_run() is None  # a hand-mangled marker never raises into a listing
    mk.write_text(json.dumps({"ts": 0, "pending": ["orca"], "attempts": 1}))
    ade_common.GOALS.unlink(missing_ok=True)  # only repos.json is in play below
    ade_basement.remember("/srv/old")
    os.utime(ade_basement.REPOS, (1, 1))
    os.utime(mk, (2, 2))  # the marker is newer than every file: a retry is due (the control)
    assert importers.first_run() is not None
    mk.write_text(json.dumps({"ts": 0, "pending": ["orca"], "attempts": 1}))
    os.utime(mk, (2, 2))
    ade_basement.remember("/srv/x")  # the user edits repos.json after the marker
    assert importers.first_run() is None  # a retry would re-add a repo or goal they removed: none


def test_first_run_rechecks_under_the_claim(home, monkeypatch):
    import contextlib
    monkeypatch.setattr(ade_basement, "HOME", home / "home")
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    monkeypatch.setattr(importers, "detect", lambda: {"orca": True, "t3": False, "cmux": False, "herdr": False, "folder": True})
    ran = []
    monkeypatch.setattr(importers, "_first_import", lambda *a: ran.append(a))

    @contextlib.contextmanager
    def finished_meanwhile(lock):  # another listing finished its import while this one waited for the claim
        (home / "home").mkdir(exist_ok=True)
        (home / "home" / importers.FIRST_RUN).write_text(json.dumps({"ts": 1, "pending": []}))
        yield True
    monkeypatch.setattr(importers, "_claimed", finished_meanwhile)
    assert importers.first_run() is None and ran == []  # decided again under the claim: nothing left to do


def test_doctor_flags_a_deprecated_driver(home, monkeypatch):
    import diag
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    checks = {c["check"]: c for c in diag.doctor(quick=True)["checks"]}
    assert "tmux" in checks and "git" in checks and "worktrunk" in checks  # native needs all three
    monkeypatch.setattr(diag.shutil, "which", lambda n: None if n in ("tmux", "wt") else "/usr/bin/" + n)
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    checks = {c["check"]: c for c in diag.doctor(quick=True)["checks"]}
    assert checks["tmux"]["ok"] is False and checks["tmux"]["state"] == "missing"
    assert checks["worktrunk"]["ok"] is False and checks["worktrunk"]["state"] == "missing"
    monkeypatch.undo()
    monkeypatch.setenv("GREENLIGHT_ADE", "herdr")
    chk = next(c for c in diag.doctor(quick=True)["checks"] if c["check"] == "ade")
    assert chk["ok"] is False and chk["state"] == "invalid"
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    chk = next(c for c in diag.doctor(quick=True)["checks"] if c["check"] == "ade")
    assert chk["ok"] is True and chk["state"] == "native"
    assert not any(c["check"] == "ade_config" for c in diag.doctor(quick=True)["checks"])  # no value: nothing to flag
    monkeypatch.delenv("GREENLIGHT_ADE")
    (home / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("GREENLIGHT_HOME", str(home / "home"))
    (home / "home" / "config.json").write_text(json.dumps({"ade": "basment"}))
    chk = next(c for c in diag.doctor(quick=True)["checks"] if c["check"] == "ade_config")
    assert chk["ok"] is False and chk["state"] == "invalid"  # a typo the user should fix
