"""placement.py and greenlight.place_here: workspaces chief makes stay on its machine, and strays show up (#160)."""
import json

import pytest

import dash
import greenlight
import placement

HOSTS = {"hosts": [{"id": "local", "kind": "local", "machineName": "devbox", "name": "this machine"},
                   {"id": "ssh:t1", "kind": "ssh", "name": "laptop"}]}


def wt(name, host=None, agents=True, **k):
    return {"worktreeId": f"r::{name}", "displayName": name, "path": f"/w/{name}", "repo": "widgets", "repoId": "r1",
            "branch": f"refs/heads/{name}", "comment": f"Merge {name}", **({"hostId": host} if host else {}),
            "agents": [{"state": "working", "updatedAt": 1}] if agents else [], **k}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("GREENLIGHT_ADE", raising=False)
    monkeypatch.delenv("ORCA_ENVIRONMENT", raising=False)
    monkeypatch.setattr(greenlight, "here", lambda: "devbox")
    placement._MACHINES.update(at=0.0, map={})


def orca_with(monkeypatch, worktrees, hosts=HOSTS, calls=None):
    def fake(*a, **kw):
        if calls is not None:
            calls.append(list(a))
        if a[:2] == ("host", "list"):
            if isinstance(hosts, Exception):
                raise hosts
            return hosts
        if a[:2] == ("worktree", "ps"):
            return {"worktrees": worktrees}
        return {"worktree": {"id": "new"}}
    monkeypatch.setattr(greenlight, "orca", fake)


def test_same_host_ignores_case_and_domain():
    assert greenlight.same_host("DevBox.lan", "devbox")
    assert not greenlight.same_host("devbox", "laptop")
    assert not greenlight.same_host("", "") and not greenlight.same_host(None, "devbox")


def test_machines_names_each_host(monkeypatch):
    orca_with(monkeypatch, [])
    assert placement.machines() == {"local": "devbox", "ssh:t1": "laptop"}


def test_machines_cached_until_fresh(monkeypatch):
    calls = []
    orca_with(monkeypatch, [], calls=calls)
    placement.machines()
    placement.machines()
    assert calls.count(["host", "list"]) == 1  # the 5s stream doesn't ask Orca each time
    placement.machines(fresh=True)
    assert calls.count(["host", "list"]) == 2


def test_machines_without_host_list(monkeypatch):
    orca_with(monkeypatch, [], hosts=RuntimeError("unknown command"))
    assert placement.machines() == {"local": "devbox"}  # an older Orca: its workspaces are here
    monkeypatch.setenv("ORCA_ENVIRONMENT", "studio")
    assert placement.machines(fresh=True) == {"local": "studio"}  # the runtime the user picked, by its name
    assert placement.expected({"local": "studio"}) == "studio"


def test_other_ades_run_here(monkeypatch):
    monkeypatch.setenv("GREENLIGHT_ADE", "basement")
    assert placement.machines() == {"local": "devbox"}


def test_expected_is_this_machine_unless_an_environment_was_picked(monkeypatch):
    ms = {"local": "studio"}
    assert placement.expected(ms) == "devbox"
    monkeypatch.setenv("ORCA_ENVIRONMENT", "studio")
    assert placement.expected(ms) == "studio"


def test_rows_flag_workspaces_elsewhere():
    ms = {"local": "devbox", "ssh:t1": "laptop"}
    wts = [wt("a"), wt("b", host="ssh:t1"), wt("c", host="ssh:gone"), wt("idle", agents=False),
           wt("main", isMainWorktree=True), wt("old", isArchived=True)]
    rs = placement.rows(wts, ms, "devbox")
    assert [(r["name"], r["host"], r["misplaced"]) for r in rs] == [
        ("b", "laptop", True), ("c", "ssh:gone", True), ("a", "devbox", False)]  # misplaced first; an unnamed host by its id
    assert rs[0]["branch"] == "b" and rs[0]["agent"] == "working"
    assert "idle" in [r["name"] for r in placement.rows(wts, ms, "devbox", every=True)]  # --all: goal-only ones too


@pytest.fixture
def pushed(monkeypatch):
    """Every branch is on origin unless a test says otherwise; records the ls-remote calls."""
    seen = []
    monkeypatch.setattr(placement, "on_origin", lambda path, br: seen.append((path, br)) or br != "unpushed")
    return seen


def test_plan_recreates_on_this_machine(monkeypatch, pushed):
    monkeypatch.setitem(greenlight.CONFIG, "agents", ["codex", "claude"])
    ms = {"local": "devbox", "ssh:t1": "laptop"}
    wts = [wt("fix-1", host="ssh:t1"), wt("widgets", isMainWorktree=True, repoId="r-here", path="/src/widgets"),
           wt("widgets-there", host="ssh:t1", isMainWorktree=True, repoId="r-there")]
    pl = placement.plan(wts, ms, "fix-1", "devbox", sts={})
    cmd = pl["cmd"]
    assert pl["from"] == "laptop" and pl["to"] == "devbox"
    assert cmd[:6] == ["worktree", "create", "--repo", "id:r-here", "--name", "fix-1"]  # this machine's checkout, not laptop's
    flags = {cmd[i]: cmd[i + 1] for i in range(6, len(cmd) - 1) if cmd[i].startswith("--")}
    assert flags["--base-branch"] == "origin/fix-1"  # starts from the pushed work, not the default branch
    assert flags["--agent"] == "codex" and flags["--comment"] == "Merge fix-1"
    assert "fix-1" in flags["--prompt"] and "Merge fix-1" in flags["--prompt"] and "never pushed" in flags["--prompt"]
    assert pl["fetch"] == ["git", "-C", "/src/widgets", "fetch", "origin", "+refs/heads/fix-1:refs/remotes/origin/fix-1"]
    assert pushed == [("/src/widgets", "fix-1")]
    assert "--no-parent" in cmd and "fix-1 on laptop" in pl["after"]


def test_plan_matches_the_same_project_not_just_the_name(pushed):
    ms = {"local": "devbox", "ssh:t1": "laptop"}
    wts = [wt("fix-1", host="ssh:t1", repoId="r-there"),
           wt("widgets", isMainWorktree=True, repoId="r-fork"),  # another project that happens to be called widgets
           wt("widgets-acme", isMainWorktree=True, repoId="r-here", repo="acme-widgets")]
    sts = {"r-there": {"projectId": "github:acme/widgets"}, "r-fork": {"projectId": "github:fork/widgets"},
           "r-here": {"projectId": "github:acme/widgets"}}
    assert placement.plan(wts, ms, "fix-1", "devbox", sts=sts)["cmd"][3] == "id:r-here"


def test_plan_refusals(pushed):
    ms = {"local": "devbox", "ssh:t1": "laptop"}
    wts = [wt("here-1"), wt("away-1", host="ssh:t1"), wt("unpushed", host="ssh:t1"),
           wt("widgets", isMainWorktree=True, host="ssh:t1"), wt("old", host="ssh:t1", isArchived=True)]
    assert "no workspace" in placement.plan(wts, ms, "nope", "devbox", sts={})["error"]
    assert "already on devbox" in placement.plan(wts, ms, "here-1", "devbox", sts={})["error"]
    assert "no checkout on devbox" in placement.plan(wts, ms, "away-1", "devbox", sts={})["error"]
    for skipped in ("widgets", "old"):  # a main checkout or an archived workspace is never recreated
        assert "no workspace" in placement.plan(wts, ms, skipped, "devbox", sts={})["error"]
    wts.append(wt("widgets-here", isMainWorktree=True, repoId="r-here"))
    err = placement.plan(wts, ms, "unpushed", "devbox", sts={})["error"]
    assert "isn't on origin" in err and "push it from laptop" in err  # its commits would be left behind


def test_setups_by_repo(monkeypatch):
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: {"setups": [{"repoId": "r1", "projectId": "p1"}, "junk"]})
    assert placement.setups() == {"r1": {"repoId": "r1", "projectId": "p1"}}
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("old orca")))
    assert placement.setups() == {}


def test_on_origin_wants_the_exact_branch(monkeypatch):
    got = []
    def run(c, **k):
        got.append(c)
        raise RuntimeError("exit 2")
    monkeypatch.setattr(greenlight, "run", run)
    assert placement.on_origin("/src/w", "fix-1") is False
    assert got == [["git", "-C", "/src/w", "ls-remote", "--exit-code", "origin", "refs/heads/fix-1"]]  # not someone/fix-1
    monkeypatch.setattr(greenlight, "run", lambda c, **k: "abc\trefs/heads/fix-1\n")
    assert placement.on_origin("/src/w", "fix-1") is True
    monkeypatch.setattr(greenlight, "run", lambda c, **k: (_ for _ in ()).throw(placement.subprocess.TimeoutExpired(c, 60)))
    assert placement.on_origin("/src/w", "fix-1") is False  # origin didn't answer: no plan, not a traceback


def test_cli_list_exit_code(monkeypatch, capsys):
    orca_with(monkeypatch, [wt("a"), wt("b", host="ssh:t1")])
    assert placement.main(["--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["expect"] == "devbox" and [r["name"] for r in out["rows"] if r["misplaced"]] == ["b"]
    orca_with(monkeypatch, [wt("a")])
    assert placement.main([]) == 0
    assert "0 of 1 not on devbox" in capsys.readouterr().out


def test_cli_recreate_dry_run_then_execute(monkeypatch, capsys, pushed):
    calls, ran = [], []
    orca_with(monkeypatch, [wt("b", host="ssh:t1"), wt("widgets", isMainWorktree=True)], calls=calls)
    monkeypatch.setattr(placement, "setups", dict)
    monkeypatch.setattr(greenlight, "run", lambda c, **k: ran.append(c) or "")
    assert placement.main(["recreate", "b"]) == 0
    assert json.loads(capsys.readouterr().out)["to"] == "devbox"
    assert not any(c[:2] == ["worktree", "create"] for c in calls) and ran == []  # a plan changes nothing
    assert placement.main(["recreate", "b", "--execute"]) == 0
    assert ran and ran[0][-3:] == ["fetch", "origin", "+refs/heads/b:refs/remotes/origin/b"]  # before Orca branches off origin/b
    assert any(c[:2] == ["worktree", "create"] for c in calls)
    assert placement.main(["recreate", "missing"]) == 2


def test_cli_recreate_fetch_failure_creates_nothing(monkeypatch, capsys, pushed):
    calls = []
    orca_with(monkeypatch, [wt("b", host="ssh:t1"), wt("widgets", isMainWorktree=True)], calls=calls)
    monkeypatch.setattr(placement, "setups", dict)
    monkeypatch.setattr(greenlight, "run", lambda c, **k: (_ for _ in ()).throw(RuntimeError("git fetch: no such ref")))
    assert placement.main(["recreate", "b", "--execute"]) == 2
    assert "nothing was created" in capsys.readouterr().err
    assert not any(c[:2] == ["worktree", "create"] for c in calls)


def test_cli_recreate_only_onto_this_machine(monkeypatch, capsys):
    orca_with(monkeypatch, [wt("b", host="ssh:t1")])
    assert placement.main(["recreate", "b", "--expect", "laptop"]) == 2  # its git would run here on laptop's path
    monkeypatch.setenv("ORCA_ENVIRONMENT", "studio")
    assert placement.main(["recreate", "b"]) == 2
    assert "run it on the one it belongs on" in capsys.readouterr().err


def test_cli_recreate_needs_a_name(monkeypatch, capsys):
    calls = []
    orca_with(monkeypatch, [], calls=calls)
    with pytest.raises(SystemExit):
        placement.main(["recreate"])
    assert placement.main(["recreate", "b", "--expect", "laptop"]) == 2
    assert calls == []  # a bad command line never reaches Orca


# greenlight.place_here: what `ade worktree create` (and every greenlight.orca create) is sent


SETUPS = [{"id": "s-here", "repoId": "r-here", "displayName": "widgets", "path": "/src/widgets", "hostId": "local", "projectId": "p1"},
          {"id": "s-there", "repoId": "r-there", "displayName": "widgets", "path": "/srv/widgets", "hostId": "ssh:t1", "projectId": "p1"},
          {"id": "s-only", "repoId": "r-only", "displayName": "gadgets", "path": "/srv/gadgets", "hostId": "ssh:t1", "projectId": "p2"}]


def cli_with(monkeypatch, local="devbox", fail=False, paired=False, setups=None, created=None):
    """A fake Orca CLI: host list (paired: plus laptop), project setups (SETUPS, or setups), and `worktree create`
    answering created (else {"ok": 1}). Returns the calls it got."""
    calls = []

    def fake(*a, **kw):
        calls.append(list(a))
        if fail:
            raise RuntimeError("unknown command host")
        if a[:2] == ("host", "list"):
            return {"hosts": [{"id": "local", "machineName": local}] + ([{"id": "ssh:t1", "name": "laptop"}] if paired else [])}
        if a[:2] == ("project", "setups"):
            return {"setups": SETUPS if setups is None else setups}
        return created if created and a[:2] == ("worktree", "create") else {"ok": 1}
    monkeypatch.setattr(greenlight, "orca_cli", fake)
    return calls


def test_place_here_leaves_other_calls_alone(monkeypatch):
    calls = cli_with(monkeypatch)
    assert greenlight.place_here(["worktree", "ps"]) == (["worktree", "ps"], None)
    assert calls == []  # no extra Orca call for anything but a create


def test_place_here_pins_a_project_create(monkeypatch):
    cli_with(monkeypatch)
    args = ["worktree", "create", "--project", "widgets", "--name", "x"]
    assert greenlight.place_here(args) == (args + ["--host", "local"], None)
    args = ["worktree", "create", "--project=widgets", "--name", "x"]
    assert greenlight.place_here(args) == (args + ["--host", "local"], None)  # the = spelling too
    args = ["worktree", "create", "--repo", "id:r1", "--name", "x"]
    assert greenlight.place_here(args) == (args, None)  # no other host paired: nothing to look up


def test_place_here_refuses_a_checkout_on_another_host(monkeypatch):
    calls = cli_with(monkeypatch, paired=True)
    args, why = greenlight.place_here(["worktree", "create", "--repo", "id:r-there", "--name", "x"])
    assert args is None and "on laptop" in why and "--repo id:r-here" in why  # and names this machine's checkout
    args, why = greenlight.place_here(["worktree", "create", "--repo=path:/srv/gadgets"])
    assert args is None and "laptop" in why and "--repo id:" not in why  # no checkout here to offer
    args, why = greenlight.place_here(["worktree", "create", "--project-host-setup", "s-only"])
    assert args is None and "project setup s-only" in why
    for ok in ("id:r-here", "id:unknown"):  # here, or Orca's to refuse: unchanged
        assert greenlight.place_here(["worktree", "create", "--repo", ok]) == (["worktree", "create", "--repo", ok], None), ok
    assert ["project", "setups"] in calls
    cli_with(monkeypatch, paired=True, setups=SETUPS + ["junk"])  # an odd entry in Orca's answer is skipped, not a crash
    assert "laptop" in greenlight.place_here(["worktree", "create", "--repo", "id:r-only"])[1]


def test_place_here_pins_a_project_host_setup_with_a_twin_here(monkeypatch):
    twins = SETUPS + [{"id": "s-here", "repoId": "r-x", "path": "/srv/x", "hostId": "ssh:t1", "projectId": "p1"}]
    cli_with(monkeypatch, paired=True, setups=twins)  # setup ids repeat across hosts: Orca would call it ambiguous
    assert greenlight.place_here(["worktree", "create", "--project-host-setup", "s-here"]) == (
        ["worktree", "create", "--project-host-setup", "s-here", "--host", "local"], None)


def test_place_here_lets_the_user_pick_a_checkout(monkeypatch):
    calls = cli_with(monkeypatch, paired=True)
    assert greenlight.place_here(["worktree", "create", "--repo", "id:r-only"], picked=True)[1] is None
    assert ["project", "setups"] not in calls
    cli_with(monkeypatch, local="laptop")  # a relayed runtime is refused even then
    assert greenlight.place_here(["worktree", "create", "--repo", "id:r-only"], picked=True)[0] is None


def test_local_first_puts_this_machines_checkouts_first(monkeypatch):
    cli_with(monkeypatch, paired=True)
    repos = [{"id": "r-there", "path": "/srv/widgets"}, {"id": "r-here", "path": "/src/widgets"}, {"id": "r-new", "path": "/n"}]
    assert [r["id"] for r in greenlight.local_first(repos)] == ["r-here", "r-there", "r-new"]
    cli_with(monkeypatch, fail=True)
    assert greenlight.local_first(repos) == repos  # Orca can't say: as listed


def test_new_task_and_adopt_pick_this_machines_checkout(monkeypatch, tmp_path):
    import adopt
    there, here_ = tmp_path / "there", tmp_path / "here"
    there.mkdir(), here_.mkdir()
    rows = [{"id": "r-there", "path": str(there), "displayName": "widgets", "gitRemoteIdentity": {"canonicalKey": "github.com/acme/widgets"}},
            {"id": "r-here", "path": str(here_), "displayName": "widgets", "gitRemoteIdentity": {"canonicalKey": "github.com/acme/widgets"}}]
    sts = [{"repoId": "r-there", "path": str(there), "hostId": "ssh:t1"}, {"repoId": "r-here", "path": str(here_), "hostId": "local"}]
    monkeypatch.setattr(greenlight, "orca", lambda *a, **k: {"repos": rows})
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: {"setups": sts})
    assert dash.repos()[0]["id"] == "r-here"
    assert adopt.repo_map()["acme/widgets"]["id"] == "r-here"
    monkeypatch.setenv("GREENLIGHT_ADE", "cmux")  # no hosts there: no Orca call, nothing reordered
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: pytest.fail("asked Orca under cmux"))
    assert [r["id"] for r in greenlight.local_first(rows)] == ["r-there", "r-here"]


def test_project_setups_retries_a_blip(monkeypatch):
    n = []
    def flaky(*a, **k):
        n.append(a)
        if len(n) == 1:
            raise RuntimeError("timeout")
        return {"setups": SETUPS}
    monkeypatch.setattr(greenlight, "orca_cli", flaky)
    assert greenlight.project_setups() == SETUPS and len(n) == 2
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert greenlight.project_setups() is None  # unanswered, not "no repos"


def test_place_here_fails_closed_when_paired_and_unverified(monkeypatch):
    calls = cli_with(monkeypatch, paired=True)
    real = greenlight.orca_cli
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")) if a[0] == "project" else real(*a, **k))
    args, why = greenlight.place_here(["worktree", "create", "--repo", "id:r-there"])
    assert args is None and "Try again" in why  # a blip with another host paired is how a stray would slip through
    assert calls == [["host", "list"]]
    cli_with(monkeypatch)  # nothing paired: no lookup, nothing to refuse
    assert greenlight.place_here(["worktree", "create", "--repo", "id:r-there"])[1] is None


def test_place_here_refuses_a_runtime_on_another_machine(monkeypatch):
    cli_with(monkeypatch, local="laptop")
    args, why = greenlight.place_here(["worktree", "create", "--repo", "id:r1", "--name", "x"])
    assert args is None and "laptop" in why and "devbox" in why and why.startswith("refused")


def test_place_here_respects_an_explicit_choice(monkeypatch):
    calls = cli_with(monkeypatch, local="laptop")
    for args in (["worktree", "create", "--project", "p", "--host", "ssh:t1"], ["worktree", "create", "--host=laptop"]):
        assert greenlight.place_here(args) == (args, None)
    monkeypatch.setenv("ORCA_ENVIRONMENT", "studio")
    assert greenlight.place_here(["worktree", "create", "--name", "x"])[1] is None
    assert calls == []


def test_place_here_old_orca_and_other_ades(monkeypatch):
    calls = cli_with(monkeypatch, fail=True)
    assert greenlight.place_here(["worktree", "create", "--name", "x"]) == (["worktree", "create", "--name", "x"], None)
    monkeypatch.setenv("GREENLIGHT_ADE", "basement")
    monkeypatch.setattr(greenlight.importlib, "import_module", lambda n: type("M", (), {"call": staticmethod(lambda *a: {"via": n})}))
    calls.clear()
    assert greenlight.orca("worktree", "create", "--project", "p") == {"via": "ade_basement"}
    assert calls == []  # another ADE runs where greenlight does: no Orca call to place it


def test_orca_refuses_a_misplaced_create(monkeypatch):
    calls = cli_with(monkeypatch, local="laptop")
    with pytest.raises(RuntimeError, match="refused"):
        greenlight.orca("worktree", "create", "--repo", "id:r1", "--name", "x")
    assert not any(c[:2] == ["worktree", "create"] for c in calls)  # adopt.py / dash.py creates are placed too


def test_ade_cli_exits_3_when_refused(monkeypatch, capsys):
    cli_with(monkeypatch, local="laptop")
    monkeypatch.setattr("sys.argv", ["greenlight.py", "ade", "worktree", "create", "--repo", "id:r1", "--name", "x", "--change", "fix"])
    monkeypatch.setenv("GREENLIGHT_REQUEST", "Fix the flaky test")
    with pytest.raises(SystemExit) as e:
        greenlight.main()
    assert e.value.code == 3 and "laptop" in capsys.readouterr().err


def test_ade_cli_creates_here(monkeypatch, capsys):
    calls = cli_with(monkeypatch)
    monkeypatch.setattr("sys.argv", ["greenlight.py", "ade", "worktree", "create", "--project", "p", "--name", "x", "--json"])
    monkeypatch.setenv("GREENLIGHT_REQUEST", "Fix the flaky test")
    greenlight.main()
    assert calls[-1] == ["worktree", "create", "--project", "p", "--name", "x", "--host", "local"]
    assert calls.count(["host", "list"]) == 1  # placed once
    assert json.loads(capsys.readouterr().out) == {"ok": 1}


# dash.py: the board says where each workspace's agent runs


def test_snapshot_carries_host_and_away(tmp_path, monkeypatch):
    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    (tmp_path / "last.json").write_text(json.dumps({"ts": 1, "rows": []}))
    orca_with(monkeypatch, [wt("here-1"), wt("away-1", host="ssh:t1"), wt("plain", agents=False, comment="")])
    snap = dash.snapshot()
    by = {p["name"]: p for p in snap["pucks"]}
    assert (by["here-1"]["host"], by["here-1"]["away"]) == ("devbox", False)
    assert (by["away-1"]["host"], by["away-1"]["away"]) == ("laptop", True)
    assert [t["host"] for t in snap["trees"]] == ["devbox"]


def test_tree_new_places_an_orca_create(monkeypatch, tmp_path):
    # the sidebar's New workspace calls Orca's CLI directly (dash.tree): it gets the relayed-runtime check, and the row
    # the user clicked is their own pick of a checkout
    monkeypatch.setattr(dash, "repo_cfg", lambda p: None)
    monkeypatch.setattr(dash, "main_of", lambda p: None)
    calls = cli_with(monkeypatch, local="laptop")
    monkeypatch.setattr(dash, "tree", lambda path: (greenlight.orca_cli, {"repoId": "r1", "path": path}))
    out = dash.tree_new("/src/widgets", {"name": "fix-2"})
    assert "refused" in out["error"] and not any(c[:2] == ["worktree", "create"] for c in calls)
    calls = cli_with(monkeypatch, paired=True, created={"worktree": {"path": "/w/fix-3"}})
    monkeypatch.setattr(dash, "tree", lambda path: (greenlight.orca_cli, {"repoId": "r-only", "path": path}))
    assert dash.tree_new("/srv/gadgets", {"name": "fix-3"})["path"] == "/w/fix-3"  # the only checkout at that path
    assert ["worktree", "create", "--repo", "id:r-only", "--name", "fix-3"] in calls
    real = greenlight.orca_cli  # another host has a checkout at the same path: the click didn't say which, so it's checked
    twins = {"worktrees": [{"path": "/srv/gadgets", "repoId": "r-only"}, {"path": "/srv/gadgets", "repoId": "r-here"}]}
    monkeypatch.setattr(greenlight, "orca_cli", lambda *a, **k: twins if a[:2] == ("worktree", "ps") else real(*a, **k))
    monkeypatch.setattr(dash, "tree", lambda path: (greenlight.orca_cli, {"repoId": "r-only", "path": path}))
    assert "on laptop" in dash.tree_new("/srv/gadgets", {"name": "fix-4"})["error"]
    other = []
    monkeypatch.setattr(dash, "tree", lambda path: (lambda *a: other.append(a) or {"worktree": {"path": "/w/fix-2"}}, {"repoId": "r1"}))
    assert dash.tree_new("/src/widgets", {"name": "fix-2"})["path"] == "/w/fix-2" and other  # another ADE's call: as before
