"""ade.py: which backend is in use, its one contract, and the Orca-shaped dispatcher on top of it (#195)."""
import json
import subprocess
import sys
import types
from unittest import mock

import pytest

import ade
import ade_basement
import ade_orca
import greenlight


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path))
    monkeypatch.delenv("GREENLIGHT_ADE", raising=False)
    return tmp_path


def test_settle_new_and_existing_installs(home):
    assert ade.name() == "native"  # a new install: native, written once
    assert json.loads((home / "config.json").read_text())["ade"] == "native"
    (home / "last.json").write_text("{}")  # its own scans later never flip it
    assert ade.name() == "native"
    (home / "config.json").write_text(json.dumps({"agents": ["codex"]}))  # an install that ran (on Orca) and never chose
    assert ade.name() == "orca"
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["ade"] == "orca" and cfg["agents"] == ["codex"] and "ade_set_because" in cfg  # its other settings stay
    (home / "config.json").write_text("[1]")  # not settings: decided, nothing written
    assert ade.name() == "orca" and (home / "config.json").read_text() == "[1]"


def test_name_resolution(home, monkeypatch):
    assert ade.name() == ade.DEFAULT == "native" and ade.is_native() and not ade.is_orca()  # a new install: native (#195)
    (home / "config.json").write_text(json.dumps({"ade": "Orca"}))  # Orca: opt-in
    assert ade.name() == "orca" and ade.is_orca()
    (home / "config.json").write_text(json.dumps({"ade": "Native"}))
    assert ade.name() == "native" and ade.is_native()
    (home / "config.json").write_text(json.dumps({"ade": "basement"}))  # the old name
    assert ade.name() == "native"
    (home / "config.json").write_text(json.dumps({"ade": "nope"}))  # an unknown config value: the default
    assert ade.name() == "native"
    (home / "config.json").write_text("{not json")
    assert ade.name() == "native" and ade.deprecated() is None
    (home / "config.json").write_text(json.dumps({"ade": "native"}))
    monkeypatch.setenv("GREENLIGHT_ADE", "t3")  # the env wins over a valid config
    assert ade.name() == "t3"
    assert ade.deprecated() == "t3"  # a legacy live driver still answers, flagged
    monkeypatch.setenv("GREENLIGHT_ADE", "rex")  # an env naming no backend is an error, not a silent default
    with pytest.raises(RuntimeError, match="not one of"):
        ade.name()


def test_get_modules(home, monkeypatch):
    assert ade.get("basement") is ade_basement and ade.get("native") is ade_basement and ade.get("orca") is ade_orca
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    assert ade.get() is ade_basement
    with pytest.raises(KeyError):
        ade.get("rex")
    for n in ade.NAMES:  # every backend implements the whole contract and names itself
        m = ade.get(n)
        assert n == m.NAME
        for fn in ("worktrees", "terminals", "read", "send", "launch", "set_goal", "remove", "create", "repos", "call"):
            assert callable(getattr(m, fn)), (n, fn)


def fake():
    calls = []
    rec = lambda name, ret=None: (lambda *a, **k: (calls.append((name, a, k)), ret)[1])
    b = types.SimpleNamespace(
        NAME="fake", worktrees=rec("worktrees", [{"id": "w"}]), terminals=rec("terminals", [{"handle": "t"}]),
        read=rec("read", {"terminal": {"tail": []}}), send=rec("send"), launch=rec("launch", {"terminal": {"handle": "h"}}),
        set_goal=rec("set_goal"), remove=rec("remove"), create=rec("create", {"worktree": {"id": "n", "path": "/n"}}),
        repos=rec("repos", {"repos": []}))
    return b, calls


def test_dispatch_maps_orca_flags_to_the_contract(home):
    b, calls = fake()
    assert ade.dispatch(b, "worktree", "ps", "--limit", "10000") == {"worktrees": [{"id": "w"}]}
    assert ade.dispatch(b, "terminal", "list") == {"terminals": [{"handle": "t"}]}
    ade.dispatch(b, "terminal", "read", "--terminal", "%1", "--limit", "30")
    ade.dispatch(b, "terminal", "send", "--terminal", "%1", "--text", "--weird", "--enter")  # a value that looks like a flag
    ade.dispatch(b, "agent", "launch", "--worktree", "id:/w", "--agent", "codex", "--prompt", "go")
    ade.dispatch(b, "worktree", "set", "--worktree", "id:/w", "--comment", "Ship it")
    ade.dispatch(b, "worktree", "set", "--worktree", "id:/w", "--display-name", "x")  # no comment: no goal change
    assert ade.dispatch(b, "worktree", "rm", "--worktree", "id:/w", "--force") == {"removed": "/w"}
    ade.dispatch(b, "worktree", "create", "--repo", "id:/r", "--name", "feat", "--base-branch", "main",
                 "--comment", "Goal", "--agent", "claude", "--prompt", "brief")
    ade.dispatch(b, "worktree", "create", "--repo", "/r", "--name", "bare")
    ade.dispatch(b, "repo", "list")
    assert calls == [
        ("worktrees", (), {}), ("terminals", (), {}), ("read", ("%1", "30"), {}), ("send", ("%1", "--weird", True), {}),
        ("launch", ("/w", "codex", "go"), {}), ("set_goal", ("/w", "Ship it"), {}), ("remove", ("/w",), {"force": True}),
        ("create", ("/r", "feat", "main", "Goal", "claude", "brief"), {}), ("create", ("/r", "bare", None, "", None, ""), {}),
        ("repos", (), {})]
    with pytest.raises(RuntimeError, match="no fake equivalent"):
        ade.dispatch(b, "host", "list")


def test_dispatch_default_agent(home):
    b, calls = fake()
    with mock.patch.object(greenlight, "CONFIG", {**greenlight.CONFIG, "agents": ["agy", "claude"]}):
        ade.dispatch(b, "agent", "launch", "--worktree", "id:/w")
    assert calls == [("launch", ("/w", "agy", ""), {})]


def test_greenlight_orca_routes_through_the_backend(home, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    with mock.patch.object(ade_basement, "worktrees", return_value=[{"id": "/w"}]), \
            mock.patch.object(greenlight, "orca_cli", side_effect=AssertionError("Orca called under native")):
        assert greenlight.orca("worktree", "ps", "--limit", "10000") == {"worktrees": [{"id": "/w"}]}
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    with mock.patch.object(greenlight, "orca_cli", return_value={"worktrees": []}) as oc, \
            mock.patch.object(greenlight, "place_here", side_effect=lambda a: (list(a), None)):
        assert greenlight.orca("worktree", "ps") == {"worktrees": []}
        oc.assert_called_once_with("worktree", "ps", timeout=120)


def test_orca_adapter_contract(home):
    with mock.patch.object(greenlight, "orca_cli", return_value={"worktrees": [1], "terminals": [2]}) as oc, \
            mock.patch.object(greenlight, "place_here", side_effect=lambda a: (list(a), None)):
        assert ade_orca.worktrees() == [1] and ade_orca.terminals() == [2]
        ade_orca.set_goal("abc", "Goal")
        ade_orca.remove("abc", force=True)
        ade_orca.create("r1", "feat", "main", "Goal", "claude", "go")
        ade_orca.launch("abc", "codex", "")
    assert [c.args for c in oc.call_args_list][2:] == [
        ("worktree", "set", "--worktree", "id:abc", "--comment", "Goal"),
        ("worktree", "rm", "--worktree", "id:abc", "--force"),
        ("worktree", "create", "--repo", "id:r1", "--name", "feat", "--base-branch", "main", "--comment", "Goal",
         "--agent", "claude", "--prompt", "go"),
        ("agent", "launch", "--worktree", "id:abc", "--agent", "codex")]
    with mock.patch.object(greenlight, "place_here", return_value=(None, "elsewhere")), pytest.raises(greenlight.PlacementRefused):
        ade_orca.create("r1", "feat")


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "widgets"
    r.mkdir()
    git("init", "-q", "-b", "main", cwd=r)
    git("-c", "user.email=tester", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=r)
    return r


def test_native_registry_and_create_launches_agent(home, repo, monkeypatch):
    monkeypatch.setattr(ade_basement, "REPOS", home / "repos.json")
    monkeypatch.setattr(ade_basement.ade_common, "GOALS", home / "goals.json")
    (repo / "sub").mkdir()
    assert ade_basement.register(str(repo / "sub")) == str(repo.resolve())  # any path inside: its main checkout
    assert ade_basement.saved() == [str(repo.resolve())]
    with pytest.raises(RuntimeError, match="not a git checkout"):
        ade_basement.register(str(home / "nowhere"))
    with mock.patch.object(ade_basement, "launch", return_value={"terminal": {"handle": "%9", "session": "mc-1"}}) as la:
        out = ade_basement.create("widgets", "feat-x", None, "Ship X", "codex", "do it")  # a registered repo by name
    path = out["worktree"]["path"]
    assert path.endswith("widgets-feat-x") and out["terminal"]["handle"] == "%9"
    la.assert_called_once_with(path, "codex", "do it")
    assert ade_basement.ade_common.goals() == {path: "Ship X"}
    with mock.patch.object(ade_basement, "panes", return_value=[]):
        assert path in [w["path"] for w in ade_basement.worktrees()]
        ade_basement.ade_common.set_goal(str(repo.resolve()), "Main goal")
        with pytest.raises(RuntimeError, match="main checkout"):
            ade_basement.remove(str(repo.resolve()))  # never: and its goal stays
        assert ade_basement.ade_common.goals()[str(repo.resolve())] == "Main goal"
        ade_basement.ade_common.set_goal(str(repo.resolve()), "")
        ade_basement.remove(path)
        assert path not in [w["path"] for w in ade_basement.worktrees()]
        with pytest.raises(RuntimeError, match="no native workspace"):
            ade_basement.remove(path)
    assert ade_basement.ade_common.goals() == {}  # its goal goes with it
    with mock.patch.object(ade_basement, "launch", side_effect=FileNotFoundError("tmux")):
        out = ade_basement.create(str(repo), "feat-z", None, "Ship Z", "claude", "go")  # no tmux: the workspace stays
    assert "didn't start" in out["error"] and "terminal" not in out
    bad = ade_basement.create(str(repo), "feat-u", None, "", "no-such-agent", "go")  # harness.py raises ValueError
    assert "didn't start" in bad["error"]
    assert ade_basement.ade_common.goals()[out["worktree"]["path"]] == "Ship Z"
    gadgets = repo.parent / "gadgets"  # a git repo in the cwd, never registered: a bare name is still not it
    gadgets.mkdir()
    git("init", "-q", "-b", "main", cwd=gadgets)
    monkeypatch.chdir(repo.parent)
    with pytest.raises(RuntimeError, match="names no registered repo"):
        ade_basement.create("gadgets", "feat-q")
    assert not (repo.parent / "gadgets-feat-q").exists() and str(gadgets.resolve()) not in ade_basement.saved()
    with pytest.raises(RuntimeError, match="not a git checkout"):
        ade_basement.create(str(home / "nowhere"), "feat-q")
    other = home / "b" / "widgets"
    other.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=other)
    ade_basement.register(str(other))
    with pytest.raises(RuntimeError, match="names 2 registered repos"):
        ade_basement.create("widgets", "feat-w")
    ade_basement.forget(str(other.resolve()))
    ade_basement.forget(str(repo.resolve()))
    assert ade_basement.saved() == []


def test_repos_cli(home, repo, monkeypatch, capsys):
    monkeypatch.setattr(ade_basement, "REPOS", home / "repos.json")
    assert greenlight.repos_cli(["add", str(repo), str(home / "nope")]) == 1  # one good, one not a repo
    capsys.readouterr()
    greenlight.repos_cli(["list"])
    assert json.loads(capsys.readouterr().out) == [{"path": str(repo.resolve()), "exists": True}]
    assert greenlight.repos_cli(["rm", str(repo)]) == 0
    assert greenlight.repos_cli(["rm", str(repo)]) == 1  # not registered any more
    assert greenlight.repos_cli(["frob"]) == 2 and greenlight.repos_cli(["add"]) == 2


def test_cli_ade_native_end_to_end(home, repo):
    """`greenlight.py ade` under GREENLIGHT_ADE=native: a real worktree, its goal, listed, removed. No tmux needed."""
    env = {"GREENLIGHT_HOME": str(home), "GREENLIGHT_ADE": "native", "GREENLIGHT_TMUX_SOCKET": "gl-test-none",
           "GREENLIGHT_ME": "tester", "PATH": "/usr/bin:/bin", "HOME": str(home)}
    (home / "imported.json").write_text("{}")  # never this machine's real ADEs (the first-run import)
    run = lambda *a: subprocess.run([sys.executable, "scripts/greenlight.py", *a], capture_output=True, text=True, env=env, timeout=60)
    assert run("repos", "add", str(repo)).returncode == 0
    # --change: greenlight's own flag, the create's reason (a read-only GREENLIGHT_REQUEST would refuse it otherwise)
    made = run("ade", "worktree", "create", "--repo", str(repo), "--name", "feat-y", "--comment", "Goal Y", "--change", "test")
    assert made.returncode == 0, made.stderr
    path = json.loads(made.stdout)["worktree"]["path"]
    ps = json.loads(run("ade", "worktree", "ps").stdout)["worktrees"]
    assert {w["path"]: w["comment"] for w in ps}[path] == "Goal Y"
    assert json.loads(run("ade", "worktree", "rm", "--worktree", f"id:{path}").stdout) == {"removed": path}
    assert json.loads((home / "goals.json").read_text()) == {}  # its goal went with it


def test_dash_backend_order(home, monkeypatch):
    import dash
    assert dash.ades() == ("orca", "native")  # the default: Orca's own workspaces still go through Orca, git finds the rest
    monkeypatch.setenv("GREENLIGHT_ADE", "basement")
    assert dash.ades() == ("orca", "native")
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    assert dash.ades() == ("orca", "cmux", "herdr", "t3")  # an Orca user's sidebar as before: every installed ADE
    monkeypatch.setenv("GREENLIGHT_ADE", "cmux")
    assert dash.ades() == ("orca", "cmux", "herdr", "t3")
    monkeypatch.setenv("GREENLIGHT_ADE", "bogus")
    assert dash.ades() == ("orca", "cmux", "herdr", "t3")
    monkeypatch.setattr(dash, "ADES", ("orca",))
    assert dash.ades() == ("orca",)
    assert dash.ade_call("orca") is greenlight.orca_cli  # tree_new knows an Orca create by it (placement as the user's pick)
    assert dash.ade_call("native") == ade_basement.call


def test_native_without_tmux(home, monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr(ade_basement.subprocess, "run", missing)
    assert ade_basement.panes() == []  # a listing goes on: nothing of ours runs
    with pytest.raises(RuntimeError, match="tmux isn't installed"):
        ade_basement.tmux("new-session", "-d")  # starting an agent says why it can't


def test_dash_under_native_writes_goals_and_creates_natively(home, repo, monkeypatch):
    import dash
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    monkeypatch.setattr(ade_basement.ade_common, "GOALS", home / "goals.json")
    orca_calls = []
    monkeypatch.setattr(dash, "tree", lambda p, every=False: ((lambda *a: orca_calls.append(a) or {}), {"worktreeId": "o1", "repoId": "opaque"}, [{}])
                        if every else ((lambda *a: orca_calls.append(a) or {}), {"worktreeId": "o1"}))
    dash.tree_goal(str(repo), "  Ship   it ")
    assert orca_calls[-1][-1] == "Ship it" and ade_basement.ade_common.goals()[str(repo)] == "Ship it"  # Orca's row, and the native scan's file
    native = []
    monkeypatch.setattr(dash, "ade_call", lambda n: (lambda *a: native.append(a) or {"worktree": {"path": "/srv/new", "id": "/srv/new"}}) if n == "native" else None)
    monkeypatch.setattr(dash, "main_of", lambda p: str(repo))
    monkeypatch.setattr(dash, "repo_cfg", lambda p: None)
    n = len(orca_calls)
    dash.tree_new(str(repo), {"name": "feat-n", "goal": "G"})
    assert native and native[0][:4] == ("worktree", "create", "--repo", f"id:{repo}") and len(orca_calls) == n  # Greenlight's own


def test_native_refuses_beside_an_outside_agent(home, monkeypatch):
    import cleanup
    monkeypatch.setattr(cleanup, "outside_agent", lambda path, fresh=False, paths=None: "codex")
    monkeypatch.setattr(ade_basement, "worktrees", lambda: [{"id": "/w/x", "path": "/w/x", "isMainWorktree": False}])
    with pytest.raises(RuntimeError, match="codex runs in /w/x outside Greenlight"):
        ade_basement.remove("/w/x")  # `greenlight.py ade worktree rm` never removes under it
    with pytest.raises(RuntimeError, match="no second agent beside it"):
        ade_basement.launch("/w/x", "claude", "go")  # nor starts a second agent beside it


def test_native_listing_runs_the_first_run_import(home, monkeypatch):
    import importers
    calls = []
    monkeypatch.setattr(importers, "first_run", lambda: calls.append(1))
    monkeypatch.setattr(ade_basement, "panes", lambda cmd=False: [])
    monkeypatch.setattr(ade_basement, "saved", list)
    ade_basement.worktrees()
    assert calls == [1]  # every native listing asks; first_run itself makes it once (its own tests)
