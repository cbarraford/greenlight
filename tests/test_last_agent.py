"""Tests for scripts/last_agent.py: the last agent per workspace, and what a workspace page's Basement button resumes (issue 157)."""
import json
import subprocess
from unittest import mock

import pytest

import ade_basement
import dash
import harness
import last_agent


@pytest.fixture()
def home(gl_home):
    return gl_home


def test_harness_names_match_harness_py():
    assert set(last_agent.HARNESSES) == set(harness.H)


def test_harness_of_maps_ade_ids():
    assert last_agent.harness_of("Claude") == "claude"
    assert last_agent.harness_of("antigravity") == "agy"
    assert last_agent.harness_of(" codex ") == "codex"
    for bad in (None, "", "cursor", "claude; rm -rf ~", 7):
        assert last_agent.harness_of(bad) is None


NOW = int(last_agent.time.time() * 1000)
strip = lambda recs: {k: {x: v[x] for x in v if x != "used"} for k, v in recs.items()}


def test_note_records_newest_and_writes_only_on_a_change(home):
    last_agent.note({"/w/a": ("codex", None, NOW), "/w/b": ("claude", "mc-1a2b3c4d", NOW)})
    assert strip(last_agent.load()) == {"/w/a": {"harness": "codex"}, "/w/b": {"harness": "claude", "session": "mc-1a2b3c4d"}}
    with mock.patch.object(last_agent.os, "replace") as rep:
        last_agent.note({"/w/a": ("codex", None, NOW + 5000)})  # the same agent seconds later: nothing written
        last_agent.note({"/w/a": ("claude", None, NOW - 1)})  # an older agent the ADE still lists: kept out
        assert not rep.called
    last_agent.note({"/w/a": ("codex", None, NOW + last_agent.REFRESH_MS)})  # a minute on: refreshed
    assert last_agent.load()["/w/a"]["used"] == NOW + last_agent.REFRESH_MS
    last_agent.note({"/w/b": ("codex", None, NOW + 1)})  # a newer agent replaces the record, session and all
    assert strip(last_agent.load())["/w/b"] == {"harness": "codex"}


def test_note_drops_bad_input(home):
    last_agent.note({"relative": ("claude", None, NOW), "/w/x": ("cursor", None, NOW), "/w/y": ("claude", "bad session; rm", NOW),
                     "/w/v": ("claude", "user-made", NOW), "/w/t": ("claude", None, "soon")})
    assert sorted(last_agent.load()) == ["/w/v", "/w/y"] and not any("session" in r for r in last_agent.load().values())
    last_agent.note({})
    assert sorted(last_agent.load()) == ["/w/v", "/w/y"]


def test_load_tolerates_a_corrupt_file(home):
    last_agent.FILE.write_text("{not json")
    assert last_agent.load() == {}
    last_agent.FILE.write_text(json.dumps([1, 2]))
    assert last_agent.load() == {}
    last_agent.FILE.write_text(json.dumps({"/w/a": {"harness": "nope"}, "/w/b": "x", "/w/c": {"harness": "pi", "used": "17"},
                                           "/w/d": {"harness": "pi", "used": "soon"}, "/w/e": {"harness": "pi", "used": True},
                                           "/w/f": {"harness": "pi", "session": 7}}))
    assert last_agent.load() == {"/w/c": {"harness": "pi", "used": 17}, "/w/f": {"harness": "pi", "used": 0}}
    assert last_agent.resume(last_agent.load()["/w/c"], [], created=NOW) == {}  # no crash on what's left


def test_note_drops_worktrees_gone_a_day(home, tmp_path):
    old = NOW - last_agent.STALE_MS - 10
    last_agent.FILE.write_text(json.dumps({"/w/gone": {"harness": "claude", "used": old}, "/w/recent": {"harness": "codex", "used": NOW - 1000},
                                           str(tmp_path): {"harness": "pi", "used": old}}))
    last_agent.note({"/w/z": ("claude", None, NOW)})
    assert sorted(last_agent.load()) == sorted(["/w/recent", str(tmp_path), "/w/z"])  # a day gone and no folder: dropped


def test_runs_needs_the_harness_command_in_that_session():
    rec = {"harness": "codex", "session": "mc-a", "used": 1}
    assert last_agent.runs(rec, [{"session": "mc-a", "cmd": "node"}])
    assert last_agent.runs(rec, [{"session": "mc-a", "cmd": "zsh"}, {"session": "mc-a", "cmd": "codex"}])
    assert not last_agent.runs(rec, [{"session": "mc-a", "cmd": "-zsh"}])  # quit: its shell is left
    assert not last_agent.runs(rec, [{"session": "mc-a", "cmd": "vim"}])  # something the user ran since
    assert not last_agent.runs(rec, [{"session": "mc-b", "cmd": "codex"}])
    assert not last_agent.runs({"harness": "claude", "used": 1}, [{"session": "mc-a", "cmd": "claude"}])
    assert not last_agent.runs({"harness": "claude", "session": "mc-a", "used": 1}, [{"session": "mc-a", "cmd": "node"}])


def test_observe_goes_by_last_use(home):
    agents = [("/w/a", {"agentType": "codex", "updatedAt": NOW}), ("/w/b", {"agentType": "codex", "updatedAt": 1000}),
              ("/w/c", {}), ("/w/ab", None), ("/w/d", {"agentType": "pi", "state": "working"})]  # d: an ADE with no times (herdr)
    panes = [{"session": "mc-b", "cwd": "/w/b/sub", "title": "✳ Claude", "at": "5", "cmd": "claude"},  # newer than b's codex
             {"session": "mc-b0", "cwd": "/w/b", "title": "✳ Older", "at": "4", "cmd": "claude"},
             {"session": "mc-a", "cwd": "/w/a", "title": "✳ Claude", "at": "5", "cmd": "claude"},  # older than a's codex
             {"session": "mc-q", "cwd": "/w/c", "title": "✳ Quit", "at": "9", "cmd": "zsh"},  # Claude quit: its title stays
             {"session": "mc-out", "cwd": "/elsewhere", "title": "✳ Claude", "at": "9", "cmd": "claude"}]  # in no workspace
    last_agent.observe(agents, panes, lambda t: t.startswith("✳"))
    assert strip(last_agent.load()) == {"/w/a": {"harness": "codex"}, "/w/b": {"harness": "claude", "session": "mc-b"},
                                        "/w/d": {"harness": "pi"}}
    assert last_agent.load()["/w/d"]["used"] >= NOW


def test_owner_is_the_longest_containing_worktree():
    assert last_agent.owner("/w/a-b/x", ["/w/a", "/w/a-b"]) == "/w/a-b"
    assert last_agent.owner("/w/ab", ["/w/a"]) is None
    assert last_agent.owner("/w/a", ["/w/a"]) == "/w/a"


def test_resume():
    running = [{"session": "mc-a", "cmd": "claude"}]
    assert last_agent.resume(None, running) == {}  # never ran an agent: the plain button
    a, b = {"harness": "claude", "session": "mc-a", "used": 100}, {"harness": "codex", "used": 100}
    assert last_agent.resume(a, running) == {"harness": "claude", "session": "mc-a"}  # its agent still runs: attach
    assert last_agent.resume(a, running, busy=True) == {"harness": "claude", "session": "mc-a"}
    assert last_agent.resume(a, [{"session": "mc-a", "cmd": "zsh"}]) == {"harness": "claude"}  # quit: continue its last session
    assert last_agent.resume(b, [], binary="/opt/codex") == {"harness": "codex", "bin": "/opt/codex"}
    assert last_agent.resume(b, [], busy=True) == {}  # working in another ADE: don't start a second copy
    assert last_agent.resume(b, [], created=100) == {"harness": "codex"}
    assert last_agent.resume(b, [], created=101) == {}  # used before the worktree was made: a deleted workspace's at that path
    assert last_agent.resume({"harness": "pi", "used": 0}, [], created=101) == {"harness": "pi"}  # no time known: it stands


def test_basement_launch_records_its_session(home):
    with mock.patch.object(ade_basement, "tmux", side_effect=lambda *a: "%9\n" if a[0] == "display-message" else ""):
        out = ade_basement.launch("/w/feat", "codex", "go")
    s = out["terminal"]["session"]
    rec = last_agent.load()["/w/feat"]
    assert last_agent.resume(rec, [{"session": s, "cmd": "node"}], created=NOW - 1) == {"harness": "codex", "session": s}


def test_basement_launch_survives_an_unwritable_home(home):
    with mock.patch.object(ade_basement, "tmux", side_effect=lambda *a: "%9\n" if a[0] == "display-message" else ""), \
            mock.patch.object(last_agent, "note", side_effect=OSError("read-only")):
        assert ade_basement.launch("/w/feat", "claude", "")["terminal"]["handle"] == "%9"


def test_panes_cmd():
    rows = "\t".join(["%1", "mc-a", "/w/a", "✳ x", "1", "claude"]) + "\n" + "\t".join(["%2", "mc-b", "/w/b", "zsh", "2"]) + "\n"
    with mock.patch.object(ade_basement, "tmux", return_value=rows):
        assert [p["cmd"] for p in ade_basement.panes(cmd=True)] == ["claude"]
    with mock.patch.object(ade_basement, "tmux", return_value=rows.replace("\tclaude", "")):
        assert [p["session"] for p in ade_basement.panes()] == ["mc-a", "mc-b"]


def _tmux_rows(*rows):
    return subprocess.CompletedProcess(["tmux"], 0, stdout="".join("\t".join(r) + "\n" for r in rows), stderr="")


def test_add_resume_on_pucks(home, monkeypatch):
    last_agent.note({"/w/old": ("opencode", "mc-gone", NOW), "/w/exited": ("claude", "mc-sh2", NOW)})
    monkeypatch.setitem(dash.greenlight.CONFIG, "harnesses", {"opencode": {"bin": "/opt/oc/opencode"}, "claude": {"bin": "x; rm"}})
    pucks = [{"path": p, "agent": {"state": st}} for p, st in
             (("/w/live", "done"), ("/w/old", "none"), ("/w/busy", "working"), ("/w/new", "none"), ("/w/exited", "none"))]
    agents = [("/w/live", {"agentType": "claude"}), ("/w/old", {}), ("/w/busy", {"agentType": "codex", "state": "working"}),
              ("/w/new", {}), ("/w/exited", {})]
    tm = _tmux_rows(["%1", "mc-live", "/w/live", "✳ Done", "1", "claude"], ["%2", "mc-sh", "/w/new", "zsh", "1", "zsh"],
                    ["%3", "mc-sh2", "/w/exited", "✳ Done", "1", "-zsh"])  # Claude quit there: its title stays, its shell runs
    with mock.patch.object(ade_basement.subprocess, "run", return_value=tm):
        dash.add_resume(pucks, agents)
    by = {p["path"]: p["resume"] for p in pucks}
    assert by == {"/w/live": {"harness": "claude", "session": "mc-live"}, "/w/old": {"harness": "opencode", "bin": "/opt/oc/opencode"},
                  "/w/busy": {}, "/w/new": {}, "/w/exited": {"harness": "claude"}}


def test_add_resume_on_a_recreated_workspace(home, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x")
    last_agent.FILE.write_text(json.dumps({str(wt): {"harness": "claude", "used": 1000}}))  # the deleted workspace's, long before
    pucks = [{"path": str(wt), "agent": {}}]
    with mock.patch.object(ade_basement.subprocess, "run", return_value=_tmux_rows()):
        dash.add_resume(pucks, [(str(wt), {})])
    assert pucks[0]["resume"] == {}  # its agent isn't this one's
    with mock.patch.object(ade_basement.subprocess, "run", return_value=_tmux_rows()):
        dash.add_resume(pucks, [(str(wt), {"agentType": "claude", "updatedAt": dash.created(str(wt)) + 1000})])  # the new one, same harness
    assert pucks[0]["resume"] == {"harness": "claude"}


def test_add_resume_without_tmux(home):
    last_agent.note({"/w/a": ("pi", "mc-a", NOW)})
    pucks = [{"path": "/w/a", "agent": {}}]
    with mock.patch.object(ade_basement.subprocess, "run", side_effect=FileNotFoundError("tmux")):
        dash.add_resume(pucks, [("/w/a", {})])
    assert pucks[0]["resume"] == {"harness": "pi"}


def test_add_resume_with_an_unwritable_home(home):
    pucks = [{"path": "/w/a", "agent": {}}]
    with mock.patch.object(ade_basement.subprocess, "run", return_value=_tmux_rows()), \
            mock.patch.object(last_agent, "note", side_effect=OSError("full")):
        dash.add_resume(pucks, [("/w/a", {"agentType": "claude"})])
    assert pucks[0]["resume"] == {}
