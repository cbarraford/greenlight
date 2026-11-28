"""ade.py: which backend is in use, its one contract, and the Orca-shaped dispatcher on top of it (#195)."""
import json
import os
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
    (home / "me.json").write_text('{"me": "x"}')  # importing greenlight writes it on a brand-new install too
    (home / "state").mkdir()  # and update.py makes this one
    (home / "auto").mkdir()  # and the timer's first tick this one
    assert ade.name() == "native"  # a new install: native, written once
    assert json.loads((home / "config.json").read_text())["ade"] == "native"
    (home / "last.json").write_text("{}")  # its own scans later never flip it
    assert ade.name() == "native"
    (home / "config.json").write_text(json.dumps({"agents": ["codex"]}))  # an install that ran (on Orca) and never chose
    assert ade.name() == "native"
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["ade"] == "native" and cfg["agents"] == ["codex"] and "ade_set_because" in cfg
    (home / "config.json").write_text("[1]")  # not settings: decided, nothing written
    assert ade.name() == "native" and (home / "config.json").read_text() == "[1]"
    (home / "config.json").write_text(json.dumps({"ade": True}))  # not even a string: never a crash
    assert ade.name() == "native" and not ade.config_ok()
    (home / "config.json").write_text(json.dumps({"ade": "basment"}))  # a typo stays the user's to fix
    assert ade.name() == "native" and json.loads((home / "config.json").read_text())["ade"] == "basment"
    (home / "last.json").unlink()
    (home / "config.json").unlink()
    (home / "history").mkdir()  # no scan ever finished (glab kept failing), but it ran: still an Orca install
    assert ade.name() == "native"


def test_name_resolution(home, monkeypatch):
    assert ade.name() == ade.DEFAULT == "native" and ade.is_native() and not ade.is_orca()  # a new install: native (#195)
    (home / "config.json").write_text(json.dumps({"ade": "Orca"}))
    assert ade.name() == "native" and not ade.is_orca()
    assert json.loads((home / "config.json").read_text())["ade"] == "native"
    (home / "config.json").write_text(json.dumps({"ade": "Native"}))
    assert ade.name() == "native" and ade.is_native()
    (home / "config.json").write_text(json.dumps({"ade": "basement"}))  # the old name
    assert ade.name() == "native"
    (home / "config.json").write_text(json.dumps({"ade": "nope"}))  # an unknown config value: the default
    assert ade.name() == "native"
    (home / "config.json").write_text("{not json")
    assert ade.name() == "native" and ade.deprecated() is None
    (home / "config.json").write_text(json.dumps({"ade": "native"}))
    monkeypatch.setenv("GREENLIGHT_ADE", "t3")
    with pytest.raises(RuntimeError, match="live ADE backends are retired"):
        ade.name()
    monkeypatch.setenv("GREENLIGHT_ADE", "rex")  # an env naming no backend is an error, not a silent default
    with pytest.raises(RuntimeError, match="use native"):
        ade.name()


def test_get_modules(home, monkeypatch):
    assert ade.get("basement") is ade_basement and ade.get("native") is ade_basement and ade.get("orca") is ade_orca
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    assert ade.get() is ade_basement
    with pytest.raises(KeyError):
        ade.get("rex")
    for n in ade.NAMES:
        m = ade.get(n)
        assert n == m.NAME
        for fn in (("worktrees", "terminals", "read", "send", "launch", "set_goal", "remove", "create", "repos", "call")
                   if n == "native" else ("worktrees", "repos")):
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
        with mock.patch.object(ade, "call", return_value={"worktree": {"id": "/w"}}) as native:
            assert greenlight.orca("worktree", "create", "--repo", "/repo", "--name", "feat") == {"worktree": {"id": "/w"}}
            native.assert_called_once_with("worktree", "create", "--repo", "/repo", "--name", "feat")
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    with pytest.raises(RuntimeError, match="live ADE backends are retired"):
        greenlight.orca("worktree", "ps")
    for source in ade.LEGACY:
        with pytest.raises(RuntimeError, match="import source"):
            ade.call("worktree", "create", n=source)
    with pytest.raises(KeyError, match="no ADE backend"):
        ade.call("worktree", "ps", n="bogus")


def test_name_no_migrate_and_stale_env_tolerance(home, monkeypatch):
    import dash
    import importers

    # migrate=False never creates or rewrites config.json
    assert ade.name(migrate=False) == "native"
    assert not (home / "config.json").exists()
    (home / "config.json").write_text(json.dumps({"ade": "orca"}))
    assert ade.name(migrate=False) == "native"
    assert json.loads((home / "config.json").read_text())["ade"] == "orca"

    # Stale GREENLIGHT_ADE=orca is tolerated cleanly by importers.first_run() and dash.outside_agent()
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    assert importers.first_run() is None
    assert dash.outside_agent(str(home)) is None


def test_orca_adapter_is_import_only(home):
    with mock.patch.object(greenlight, "orca_cli", side_effect=[{"worktrees": [1]}, {"repos": [2]}]) as oc:
        assert ade_orca.worktrees() == [1]
        assert ade_orca.repos() == {"repos": [2]}
    assert [c.args for c in oc.call_args_list] == [
        ("worktree", "ps", "--limit", "10000"), ("repo", "list")]
    for operation in ("call", "terminals", "read", "send", "launch", "set_goal", "remove", "create"):
        assert not hasattr(ade_orca, operation)


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "widgets"
    r.mkdir()
    git("init", "-q", "-b", "main", cwd=r)
    git("-c", "user.email=tester", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=r)
    return r


def _make_fake_wt(tmp_path):
    """Minimal executable `wt` stub backed by git worktree so tests never require Worktrunk on PATH."""
    wt = tmp_path / "wt-stub"
    wt.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "repo, i = '.', 0\n"
        "while i < len(args):\n"
        "    if args[i] == '-C':\n"
        "        repo = args[i + 1]; i += 2\n"
        "    elif args[i] in ('-y', '--yes'):\n"
        "        i += 1\n"
        "    elif args[i] == '--config-set':\n"
        "        i += 2\n"
        "    else:\n"
        "        break\n"
        "sub, rest = args[i], args[i + 1:]\n"
        "if sub == 'switch':\n"
        "    base, pos, j = None, [], 0\n"
        "    while j < len(rest):\n"
        "        if rest[j] == '--base':\n"
        "            base = rest[j + 1]; j += 2\n"
        "        elif rest[j].startswith('-'):\n"
        "            j += 1\n"
        "        else:\n"
        "            pos.append(rest[j]); j += 1\n"
        "    branch = pos[0]\n"
        "    r = Path(repo).resolve()\n"
        "    dest = r.parent / f'{r.name}.{branch.replace(\"/\", \"-\")}'\n"
        "    cmd = ['git', '-C', str(r), 'worktree', 'add', '-q', '-b', branch, str(dest)]\n"
        "    if base:\n"
        "        cmd.append(base)\n"
        "    p = subprocess.run(cmd, capture_output=True, text=True)\n"
        "    if p.returncode:\n"
        "        sys.stderr.write(p.stderr); sys.exit(p.returncode)\n"
        "    print(json.dumps({'branch': branch, 'path': str(dest)}))\n"
        "elif sub == 'list':\n"
        "    p = subprocess.run(['git', '-C', repo, 'worktree', 'list', '--porcelain'], capture_output=True, text=True, check=True)\n"
        "    items, cur = [], {}\n"
        "    for line in p.stdout.splitlines() + ['']:\n"
        "        if line.startswith('worktree '):\n"
        "            cur = {'worktree': {'path': line[9:]}}\n"
        "        elif line.startswith('branch '):\n"
        "            cur['branch'] = line[7:].removeprefix('refs/heads/')\n"
        "        elif not line and cur:\n"
        "            items.append(cur); cur = {}\n"
        "    print(json.dumps({'schema': 2, 'items': items}))\n"
        "elif sub == 'remove':\n"
        "    force = '--force' in rest or '-f' in rest\n"
        "    target = [x for x in rest if not x.startswith('-')][-1]\n"
        "    cmd = ['git', '-C', repo, 'worktree', 'remove', *(['--force'] if force else []), target]\n"
        "    p = subprocess.run(cmd, capture_output=True, text=True)\n"
        "    if p.returncode:\n"
        "        sys.stderr.write(p.stderr); sys.exit(p.returncode)\n"
        "else:\n"
        "    sys.stderr.write(f'unsupported wt subcommand: {sub}\\n'); sys.exit(2)\n"
    )
    wt.chmod(0o755)
    return wt


def test_native_registry_and_create_launches_agent(home, repo, monkeypatch):
    monkeypatch.setenv("WORKTRUNK_BIN", str(_make_fake_wt(home)))
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
    assert path.endswith("widgets.feat-x") and out["terminal"]["handle"] == "%9"
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
    assert not (repo.parent / "gadgets.feat-q").exists() and str(gadgets.resolve()) not in ade_basement.saved()
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
    """`greenlight.py ade` under GREENLIGHT_ADE=native: a real worktree, its goal, listed, removed. No tmux or host wt needed."""
    wt = str(_make_fake_wt(home))
    env = {"GREENLIGHT_HOME": str(home), "GREENLIGHT_ADE": "native", "GREENLIGHT_TMUX_SOCKET": "gl-test-none",
           "GREENLIGHT_ME": "tester", "WORKTRUNK_BIN": wt, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
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
    assert subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/feat-y"]).returncode == 0


def test_cli_ade_native_finds_wt_off_a_bare_path(home, repo):
    """The app's backend runs over ssh/launchd with a PATH that lacks ~/.local/bin: wt installed there still works."""
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    _make_fake_wt(home).rename(local_bin / "wt")
    env = {"GREENLIGHT_HOME": str(home), "GREENLIGHT_ADE": "native", "GREENLIGHT_TMUX_SOCKET": "gl-test-none",
           "GREENLIGHT_ME": "tester", "PATH": "/usr/bin:/bin", "HOME": str(home)}  # no WORKTRUNK_BIN, no ~/.local/bin
    (home / "imported.json").write_text("{}")
    run = lambda *a: subprocess.run([sys.executable, "scripts/greenlight.py", *a], capture_output=True, text=True, env=env, timeout=60)
    assert run("repos", "add", str(repo)).returncode == 0
    made = run("ade", "worktree", "create", "--repo", str(repo), "--name", "feat-z", "--comment", "Goal Z", "--change", "test")
    assert made.returncode == 0, made.stderr
    path = json.loads(made.stdout)["worktree"]["path"]
    assert json.loads(run("ade", "worktree", "rm", "--worktree", f"id:{path}").stdout) == {"removed": path}

    (local_bin / "wt").rename(home / "elsewhere-wt")  # off every searched folder: config.json's bins.wt names it
    failed = run("ade", "worktree", "create", "--repo", str(repo), "--name", "feat-q", "--change", "test")
    assert failed.returncode != 0 and "Worktrunk (`wt`) is required" in failed.stderr + failed.stdout
    (home / "config.json").write_text(json.dumps({"bins": {"wt": str(home / "elsewhere-wt")}}))
    made = run("ade", "worktree", "create", "--repo", str(repo), "--name", "feat-q", "--change", "test")
    assert made.returncode == 0, made.stderr


def test_dash_backend_order(home, monkeypatch):
    import dash
    assert dash.ades() == ("native",)
    with pytest.raises(RuntimeError, match="import source"):
        dash.ade_call("orca")
    monkeypatch.setenv("GREENLIGHT_ADE", "basement")
    assert dash.ades() == ("native",)
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    assert dash.ades() == ("native",)
    monkeypatch.setenv("GREENLIGHT_ADE", "cmux")
    assert dash.ades() == ("native",)
    monkeypatch.setenv("GREENLIGHT_ADE", "bogus")
    assert dash.ades() == ("native",)
    monkeypatch.setattr(dash, "ADES", ("orca",))
    assert dash.ades() == ("orca",)
    assert dash.ade_call("orca") is greenlight.orca_cli  # tree_new knows an Orca create by it (placement as the user's pick)
    assert dash.ade_call("native") == ade_basement.call
    with pytest.raises(RuntimeError, match="import source"):
        dash.ade_call("cmux")


def test_native_without_tmux(home, monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr(ade_basement.subprocess, "run", missing)
    assert ade_basement.panes() == []  # a listing goes on: nothing of ours runs
    with pytest.raises(RuntimeError, match="tmux isn't installed"):
        ade_basement.tmux("new-session", "-d")  # starting an agent says why it can't


def test_dash_under_native_writes_goals_and_creates_natively(home, repo, monkeypatch):
    import dash
    monkeypatch.setattr(dash.greenlight, "project_setups", lambda tries=1: None)  # never this machine's real Orca
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
    remote = {"worktreeId": "o2", "repoId": "opaque", "hostId": "laptop"}  # a row Orca lists for another host
    orca = lambda *a: orca_calls.append(a) or ({"worktree": {"path": "/srv/remote/x-feat-r", "id": "o3"}} if a[1] == "create" else {})  # noqa: E731
    monkeypatch.setattr(dash, "tree", lambda p, every=False: (orca, remote, [remote]) if every else (orca, remote))
    monkeypatch.setattr(dash.greenlight, "orca_cli", lambda *a, **k: {})
    native.clear()
    dash.tree_goal("/srv/remote/x", "Remote goal")
    assert "/srv/remote/x" not in ade_basement.ade_common.goals()  # not this machine's: no local goal
    dash.tree_new("/srv/remote/x", {"name": "feat-r"})
    assert not native and orca_calls[-1][:2] == ("worktree", "create")  # made by Orca on that host, as before
    here_too = [remote, {"worktreeId": "o4", "hostId": "local", "repo": remote.get("repo")}]  # the same path on another host (listed first) and here
    monkeypatch.setattr(dash, "tree", lambda p, every=False: (orca, remote, here_too) if every else (orca, remote))
    dash.tree_goal(str(repo), "Both")
    assert ade_basement.ade_common.goals()[str(repo)] == "Both"  # this machine has it too: the native scan's file
    native.clear()
    dash.tree_new(str(repo), {"name": "feat-b"})
    assert native and native[0][:4] == ("worktree", "create", "--repo", f"id:{repo}")  # its same-project twin here: native
    monkeypatch.setattr(dash.greenlight, "project_setups", lambda tries=1: [{"repoId": "opaque", "projectId": "p1"}, {"repoId": "other", "projectId": "p2"}])
    unrelated = [remote, {"worktreeId": "o5", "hostId": "local", "repoId": "other"}]  # a different project at that path here
    monkeypatch.setattr(dash, "tree", lambda p, every=False: (orca, remote, unrelated) if every else (orca, remote))
    native.clear()
    dash.tree_new(str(repo), {"name": "feat-u"})
    assert not native  # never in an unrelated local repo: Orca makes it on the host the user picked
    dash.tree_goal(str(repo), "Remote only")
    assert ade_basement.ade_common.goals()[str(repo)] == "Both"  # nor its goal onto the unrelated local workspace


def test_native_refuses_beside_an_outside_agent(home, monkeypatch):
    import cleanup
    asked = []
    monkeypatch.setattr(cleanup, "outside_agent", lambda path, fresh=False, paths=None: asked.append((fresh, paths)) or "codex")
    monkeypatch.setattr(ade_basement, "worktrees", lambda: [{"id": "/w/x", "path": "/w/x", "isMainWorktree": False},
                                                            {"id": "/w/x/.wt/b", "path": "/w/x/.wt/b", "isMainWorktree": False}])
    with pytest.raises(RuntimeError, match="codex runs in /w/x outside Greenlight"):
        ade_basement.remove("/w/x")  # `greenlight.py ade worktree rm` never removes under it
    with pytest.raises(RuntimeError, match="no second agent beside it"):
        ade_basement.launch("/w/x", "claude", "go")  # nor starts a second agent beside it
    assert asked == [(True, ["/w/x", "/w/x/.wt/b"])] * 2  # fresh, against every worktree (nested ones count)


def test_native_listing_runs_the_first_run_import(home, monkeypatch):
    import importers
    calls = []
    monkeypatch.setattr(importers, "first_run", lambda: calls.append(1))
    monkeypatch.setattr(ade_basement, "panes", lambda cmd=False: [])
    monkeypatch.setattr(ade_basement, "saved", list)
    ade_basement.worktrees()
    assert calls == [1]  # every native listing asks; first_run itself makes it once (its own tests)
