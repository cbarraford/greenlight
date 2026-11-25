"""importers.py: read-only one-shot imports of another ADE's repos and goals into the native engine (#195)."""
import json
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
    (code / "wt").mkdir()
    (code / "wt" / ".git").write_text("gitdir: /elsewhere")  # a linked worktree, not a main checkout
    repos, goals = importers.from_folder(str(code))
    assert sorted(repos) == sorted([str(a.resolve()), str(b.resolve())]) and goals == {}
    with pytest.raises(RuntimeError, match="not a folder"):
        importers.from_folder(str(home / "missing"))


def test_from_backend_reads_repos_and_goals(home, monkeypatch):
    r = git_init(home / "r")
    w = home / "r-feat"
    w.mkdir()
    rows = [{"path": str(w), "repoId": str(r), "comment": "Merge !4: ship it"},
            {"path": str(r), "repoId": str(r), "comment": "Rebase !1 — dispatched 2026-01-13"},  # a status line, not a goal
            {"path": "/srv/other-host/x", "repoId": "/srv/other-host", "comment": "elsewhere"},     # not on this machine
            {"path": str(w), "repoId": str(r), "comment": "  ", "isArchived": True}]
    other = git_init(home / "no-worktrees")
    monkeypatch.setattr(importers.ade, "get", lambda n: fake_backend(rows, [str(other), "/srv/gone"]))
    repos, goals = importers.from_backend("orca")
    assert repos == [str(r), str(r), str(other)] and goals == {str(w): "Merge !4: ship it"}
    repos, _ = importers.from_backend("cmux")  # only Orca lists repos with no worktree
    assert str(other) not in repos


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
    assert p["sources"]["herdr"] == {"error": "FileNotFoundError: herdr"} and p["sources"]["orca"] == {"repos": 3, "goals": 2}
    assert ade_basement.saved() == [str(r1.resolve())]  # a plan changes nothing
    ade_common.set_goal("/w/c", "Set meanwhile")  # between preview and apply: it stands
    importers.apply(p)
    assert ade_basement.saved() == [str(r1.resolve()), str(r2.resolve())]
    assert ade_common.goals() == {"/w/a": "Mine", "/w/b": "Ship B", "/w/c": "Set meanwhile"}
    assert importers.plan(found)["repos"] == [] and importers.plan(found)["goals"] == {}  # run again: nothing new


def test_gather_reports_a_failing_source(home, monkeypatch):
    def get(n):
        if n == "t3":
            raise RuntimeError("t3 server not running")
        return fake_backend([])
    monkeypatch.setattr(importers.ade, "get", get)
    got = importers.gather(["orca", "t3", "folder"], [str(home)])
    assert got["orca"] == {"repos": [], "goals": {}} and "not running" in got["t3"]["error"] and "repos" in got["folder"]


def test_run_cli_and_dash_ops(home, monkeypatch):
    git_init(home / "code" / "x")
    monkeypatch.setattr(importers, "detect", lambda: {"orca": False, "t3": False, "cmux": False, "herdr": False, "folder": True})
    assert importers.run(["preview"])["repos"] == []  # folder needs a --path: nothing detected to read
    with pytest.raises(SystemExit, match="unknown source"):
        importers.run(["preview", "--from", "rex"])
    with pytest.raises(SystemExit, match="needs --path"):
        importers.run(["preview", "--from", "folder"])
    p = importers.run(["preview", "--path", str(home / "code")])
    assert p["repos"] == [str((home / "code" / "x").resolve())] and ade_basement.saved() == []
    ask = lambda verb, o: dash.settings(f"import-{verb}", "-", json.dumps(o))
    assert ask("detect", {})["sources"]["folder"] is True
    assert "bad source" in ask("preview", {"from": ["rex"]})["error"]
    assert "full path" in ask("preview", {"from": ["folder"], "paths": ["code"]})["error"]
    assert "full path" in ask("preview", {"from": ["folder"], "paths": ["/srv/../etc"]})["error"]
    assert "pick a source" in ask("preview", {"from": []})["error"]
    assert "needs --path" in ask("preview", {"from": ["folder"]})["error"]
    assert "no op" in ask("frob", {})["error"]
    out = ask("apply", {"from": ["folder"], "paths": [str(home / "code")]})
    assert out["repos"] == ade_basement.saved() == [str((home / "code" / "x").resolve())]
