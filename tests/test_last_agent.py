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


def test_note_records_and_only_writes_on_change(home):
    last_agent.note({"/w/a": ("codex", None), "/w/b": ("claude", "mc-1a2b3c4d")})
    assert {k: {x: v[x] for x in v if x != "at"} for k, v in last_agent.load().items()} == {
        "/w/a": {"harness": "codex"}, "/w/b": {"harness": "claude", "session": "mc-1a2b3c4d"}}
    before = last_agent.FILE.stat().st_mtime_ns
    with mock.patch.object(last_agent.os, "replace") as rep:
        last_agent.note({"/w/a": ("codex", None)})  # the same sighting again: nothing written
        assert not rep.called
    assert last_agent.FILE.stat().st_mtime_ns == before
    last_agent.note({"/w/b": ("codex", None)})  # a newer agent replaces the record, session and all
    assert last_agent.load()["/w/b"]["harness"] == "codex" and "session" not in last_agent.load()["/w/b"]


def test_note_drops_bad_input(home):
    last_agent.note({"relative": ("claude", None), "/w/x": ("cursor", None), "/w/y": ("claude", "bad session; rm"), "/w/v": ("claude", "user-made")})
    assert sorted(last_agent.load()) == ["/w/v", "/w/y"] and not any("session" in r for r in last_agent.load().values())
    last_agent.note({})
    assert sorted(last_agent.load()) == ["/w/v", "/w/y"]


def test_load_tolerates_a_corrupt_file(home):
    last_agent.FILE.write_text("{not json")
    assert last_agent.load() == {}
    last_agent.FILE.write_text(json.dumps([1, 2]))
    assert last_agent.load() == {}
    last_agent.FILE.write_text(json.dumps({"/w/a": {"harness": "nope"}, "/w/b": "x", "/w/c": {"harness": "pi"}}))
    assert list(last_agent.load()) == ["/w/c"]


def test_note_drops_worktrees_gone_a_day(home, tmp_path):
    here = str(tmp_path)
    old = int(last_agent.time.time()) - last_agent.STALE_S - 10
    last_agent.FILE.write_text(json.dumps({"/w/gone": {"harness": "claude", "at": old}, "/w/new-gone": {"harness": "codex", "at": old + 20},
                                           here: {"harness": "pi", "at": old}}))
    last_agent.note({"/w/z": ("claude", None)})
    assert sorted(last_agent.load()) == sorted(["/w/new-gone", here, "/w/z"])  # a day gone and no folder: dropped


def test_live_sessions_are_the_ones_an_agent_still_runs_in():
    panes = [{"session": "mc-a", "cmd": "claude"}, {"session": "mc-b", "cmd": "zsh"}, {"session": "mc-c", "cmd": "-bash"},
             {"session": "mc-d", "cmd": "codex"}, {"session": "mc-d", "cmd": "zsh"}, {"session": "mc-e", "cmd": ""}, {"session": "mc-f"}]
    assert last_agent.live(panes) == {"mc-a", "mc-d"}


def test_observe_prefers_an_attachable_basement_pane(home):
    agents = [("/w/a", {"agentType": "codex"}), ("/w/b", {"agentType": "claude"}), ("/w/c", {}), ("/w/ab", None)]
    panes = [{"session": "mc-b", "cwd": "/w/b/sub", "title": "✳ Claude", "at": "5"},
             {"session": "mc-b0", "cwd": "/w/b", "title": "✳ Older", "at": "4"},  # the most recently active one wins
             {"session": "mc-shell", "cwd": "/w/a", "title": "zsh", "at": "9"},  # a plain shell isn't an agent
             {"session": "mc-out", "cwd": "/elsewhere", "title": "✳ Claude", "at": "9"}]  # in no workspace
    last_agent.observe(agents, panes, lambda t: t.startswith("✳"))
    rec = last_agent.load()
    assert rec["/w/a"]["harness"] == "codex" and "session" not in rec["/w/a"]
    assert (rec["/w/b"]["harness"], rec["/w/b"]["session"]) == ("claude", "mc-b")
    assert "/w/c" not in rec and "/w/ab" not in rec


def test_observe_keeps_a_record_whose_agent_still_runs(home):
    last_agent.note({"/w/a": ("codex", "mc-cx")})  # ade_basement.launch: no title gives codex away
    panes = [{"session": "mc-cl", "cwd": "/w/a", "title": "✳ Claude", "at": "1"}]
    last_agent.observe([("/w/a", {"agentType": "claude"})], panes, lambda t: t.startswith("✳"), alive={"mc-cx", "mc-cl"})
    assert last_agent.load()["/w/a"]["session"] == "mc-cx"
    last_agent.observe([("/w/a", {"agentType": "claude"})], panes, lambda t: t.startswith("✳"), alive={"mc-cl"})  # codex quit
    assert last_agent.load()["/w/a"]["session"] == "mc-cl"


def test_owner_is_the_longest_containing_worktree():
    assert last_agent.owner("/w/a-b/x", ["/w/a", "/w/a-b"]) == "/w/a-b"
    assert last_agent.owner("/w/ab", ["/w/a"]) is None
    assert last_agent.owner("/w/a", ["/w/a"]) == "/w/a"


def test_resume():
    assert last_agent.resume(None, set()) == {}  # never ran an agent: the plain button
    a, b = {"harness": "claude", "session": "mc-a", "at": 100}, {"harness": "codex", "at": 100}
    assert last_agent.resume(a, {"mc-a"}) == {"harness": "claude", "session": "mc-a"}  # its agent still runs: attach
    assert last_agent.resume(a, {"mc-a"}, busy=True) == {"harness": "claude", "session": "mc-a"}
    assert last_agent.resume(a, set()) == {"harness": "claude"}  # gone (or only a shell left): continue its last session
    assert last_agent.resume(b, set(), binary="/opt/codex") == {"harness": "codex", "bin": "/opt/codex"}
    assert last_agent.resume(b, set(), busy=True) == {}  # working in another ADE: don't start a second copy
    assert last_agent.resume(b, set(), created=100_000) == {"harness": "codex"}
    assert last_agent.resume(b, set(), created=100_001) == {}  # older than the worktree: a deleted workspace's at that path


def test_basement_launch_records_its_session(home):
    with mock.patch.object(ade_basement, "tmux", side_effect=lambda *a: "%9\n" if a[0] == "display-message" else ""):
        out = ade_basement.launch("/w/feat", "codex", "go")
    s = out["terminal"]["session"]
    assert last_agent.resume(last_agent.load()["/w/feat"], {s}) == {"harness": "codex", "session": s}


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
    last_agent.note({"/w/old": ("opencode", "mc-gone"), "/w/exited": ("claude", "mc-sh2")})
    monkeypatch.setitem(dash.greenlight.CONFIG, "harnesses", {"opencode": {"bin": "/opt/oc/opencode"}, "claude": {"bin": "x; rm"}})
    pucks = [{"path": p, "agent": {"state": st}} for p, st in
             (("/w/live", "done"), ("/w/old", "none"), ("/w/busy", "working"), ("/w/new", "none"), ("/w/exited", "none"))]
    agents = [("/w/live", {"agentType": "claude"}), ("/w/old", {}), ("/w/busy", {"agentType": "codex", "state": "working"}),
              ("/w/new", {}), ("/w/exited", {})]
    tm = _tmux_rows(["%1", "mc-live", "/w/live", "✳ Done", "1", "claude"], ["%2", "mc-sh", "/w/new", "zsh", "1", "zsh"],
                    ["%3", "mc-sh2", "/w/exited", "zsh", "1", "-zsh"])  # Claude quit: its session is a shell now
    with mock.patch.object(ade_basement.subprocess, "run", return_value=tm):
        dash.add_resume(pucks, agents)
    by = {p["path"]: p["resume"] for p in pucks}
    assert by == {"/w/live": {"harness": "claude", "session": "mc-live"}, "/w/old": {"harness": "opencode", "bin": "/opt/oc/opencode"},
                  "/w/busy": {}, "/w/new": {}, "/w/exited": {"harness": "claude"}}


def test_add_resume_ignores_a_deleted_workspaces_record(home, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x")
    last_agent.FILE.write_text(json.dumps({str(wt): {"harness": "claude", "at": 1}}))  # from long before this worktree
    pucks = [{"path": str(wt), "agent": {}}]
    with mock.patch.object(ade_basement.subprocess, "run", return_value=_tmux_rows()):
        dash.add_resume(pucks, [(str(wt), {})])
    assert pucks[0]["resume"] == {}


def test_add_resume_without_tmux(home):
    last_agent.note({"/w/a": ("pi", "mc-a")})
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
