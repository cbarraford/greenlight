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
    last_agent.note({"relative": ("claude", None), "/w/x": ("cursor", None), "/w/y": ("claude", "bad session; rm")})
    assert list(last_agent.load()) == ["/w/y"] and "session" not in last_agent.load()["/w/y"]
    last_agent.note({})
    assert list(last_agent.load()) == ["/w/y"]


def test_load_tolerates_a_corrupt_file(home):
    last_agent.FILE.write_text("{not json")
    assert last_agent.load() == {}
    last_agent.FILE.write_text(json.dumps([1, 2]))
    assert last_agent.load() == {}
    last_agent.FILE.write_text(json.dumps({"/w/a": {"harness": "nope"}, "/w/b": "x", "/w/c": {"harness": "pi"}}))
    assert list(last_agent.load()) == ["/w/c"]


def test_observe_prefers_an_attachable_basement_pane(home):
    agents = [("/w/a", {"agentType": "codex"}), ("/w/b", {"agentType": "claude"}), ("/w/c", {}), ("/w/ab", None)]
    panes = [{"session": "mc-b", "cwd": "/w/b/sub", "title": "✳ Claude"},
             {"session": "mc-shell", "cwd": "/w/a", "title": "zsh"},  # a plain shell isn't an agent
             {"session": "mc-out", "cwd": "/elsewhere", "title": "✳ Claude"}]  # in no workspace
    last_agent.observe(agents, panes, lambda t: t.startswith("✳"))
    rec = last_agent.load()
    assert rec["/w/a"]["harness"] == "codex" and "session" not in rec["/w/a"]
    assert (rec["/w/b"]["harness"], rec["/w/b"]["session"]) == ("claude", "mc-b")
    assert "/w/c" not in rec and "/w/ab" not in rec


def test_owner_is_the_longest_containing_worktree():
    assert last_agent.owner("/w/a-b/x", ["/w/a", "/w/a-b"]) == "/w/a-b"
    assert last_agent.owner("/w/ab", ["/w/a"]) is None
    assert last_agent.owner("/w/a", ["/w/a"]) == "/w/a"


def test_resume(home):
    assert last_agent.resume("/w/none", set()) == {}  # never ran an agent: the plain button
    last_agent.note({"/w/a": ("claude", "mc-a"), "/w/b": ("codex", None)})
    assert last_agent.resume("/w/a", {"mc-a"}) == {"harness": "claude", "session": "mc-a"}  # alive: attach
    assert last_agent.resume("/w/a", {"mc-a"}, busy=True) == {"harness": "claude", "session": "mc-a"}
    assert last_agent.resume("/w/a", set()) == {"harness": "claude"}  # exited: continue its last session
    assert last_agent.resume("/w/b", set()) == {"harness": "codex"}
    assert last_agent.resume("/w/b", set(), busy=True) == {}  # working in another ADE: don't start a second copy


def test_basement_launch_records_its_session(home):
    with mock.patch.object(ade_basement, "tmux", side_effect=lambda *a: "%9\n" if a[0] == "display-message" else ""):
        out = ade_basement.launch("/w/feat", "codex", "go")
    s = out["terminal"]["session"]
    assert last_agent.resume("/w/feat", {s}) == {"harness": "codex", "session": s}


def test_basement_launch_survives_an_unwritable_home(home):
    with mock.patch.object(ade_basement, "tmux", side_effect=lambda *a: "%9\n" if a[0] == "display-message" else ""), \
            mock.patch.object(last_agent, "note", side_effect=OSError("read-only")):
        assert ade_basement.launch("/w/feat", "claude", "")["terminal"]["handle"] == "%9"


def _tmux_rows(*rows):
    return subprocess.CompletedProcess(["tmux"], 0, stdout="".join("\t".join(r) + "\n" for r in rows), stderr="")


def test_add_resume_on_pucks(home):
    last_agent.note({"/w/old": ("opencode", "mc-gone")})
    pucks = [{"path": "/w/live", "agent": {"state": "done"}}, {"path": "/w/old", "agent": {"state": "none"}},
             {"path": "/w/busy", "agent": {"state": "working"}}, {"path": "/w/new", "agent": {"state": "none"}}]
    agents = [("/w/live", {"agentType": "claude"}), ("/w/old", {}), ("/w/busy", {"agentType": "codex", "state": "working"}), ("/w/new", {})]
    tm = _tmux_rows(["%1", "mc-live", "/w/live", "✳ Done", "1"], ["%2", "mc-sh", "/w/new", "zsh", "1"])
    with mock.patch.object(ade_basement.subprocess, "run", return_value=tm):
        dash.add_resume(pucks, agents)
    by = {p["path"]: p["resume"] for p in pucks}
    assert by == {"/w/live": {"harness": "claude", "session": "mc-live"}, "/w/old": {"harness": "opencode"},
                  "/w/busy": {}, "/w/new": {}}


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
