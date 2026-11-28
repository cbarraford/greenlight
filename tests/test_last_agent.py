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
    last_agent.FILE.write_text('{"/w/a": {"harness": "pi", "used": 1e999}, "/w/b": {"harness": "pi", "used": ' + str(NOW) + ', "session": "mc-a\\n"}}')
    assert last_agent.load() == {"/w/b": {"harness": "pi", "used": NOW}}  # inf, and a session with a trailing newline
    last_agent.note({"/w/c": ("pi", None, NOW)})  # and it heals: the next write keeps only good records
    assert sorted(last_agent.load()) == ["/w/b", "/w/c"]


def test_observe_survives_an_infinite_ade_time(home):
    last_agent.observe([("/w/a", {"agentType": "codex", "updatedAt": float("inf")})], [], lambda t: False)
    assert last_agent.load()["/w/a"]["used"] == 0


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
    claude = {"harness": "claude", "session": "mc-a", "used": 1}
    assert last_agent.runs(claude, [{"session": "mc-a", "cmd": "2.1.308"}])  # macOS: the native Claude is its version file
    assert not last_agent.runs(claude, [{"session": "mc-a", "cmd": "node"}])  # anything under node: a dev server in a quit Claude's tab
    assert not last_agent.runs(claude, [{"session": "mc-a", "cmd": "1.2"}, {"session": "mc-a", "cmd": "zsh"}])


def test_observe_goes_by_last_use(home):
    agents = [("/w/a", {"agentType": "codex", "updatedAt": NOW}), ("/w/b", {"agentType": "codex", "updatedAt": 1000}),
              ("/w/c", {}), ("/w/ab", None), ("/w/d", {"agentType": "pi", "state": "working"}), ("/w/e", {})]  # d: an ADE with no times (herdr)
    panes = [{"session": "mc-b", "cwd": "/w/b/sub", "title": "✳ Claude", "at": "5", "cmd": "claude"},  # newer than b's codex
             {"session": "mc-b0", "cwd": "/w/b", "title": "✳ Older", "at": "4", "cmd": "claude"},
             {"session": "mc-a", "cwd": "/w/a", "title": "✳ Claude", "at": "5", "cmd": "2.1.308"},  # older than a's codex
             {"session": "mc-e", "cwd": "/w/e", "title": "✳ Mac", "at": "5", "cmd": "2.1.308"},  # a Mac's native Claude
             {"session": "mc-q", "cwd": "/w/c", "title": "✳ Quit", "at": "9", "cmd": "zsh"},  # Claude quit: its title stays
             {"session": "mc-out", "cwd": "/elsewhere", "title": "✳ Claude", "at": "9", "cmd": "claude"}]  # in no workspace
    last_agent.observe(agents, panes, lambda t: t.startswith("✳"))
    assert strip(last_agent.load()) == {"/w/a": {"harness": "codex"}, "/w/b": {"harness": "claude", "session": "mc-b"},
                                        "/w/d": {"harness": "pi"}, "/w/e": {"harness": "claude", "session": "mc-e"}}
    assert last_agent.load()["/w/d"]["used"] >= NOW


def test_basement_ade_quit_titles_dont_count(home):
    # the Basement ADE lists every ✳ pane as Claude (it carries a handle): a quit one, used last, comes back as the primary agent
    quit_t = NOW + 600_000
    agents = [("/w/a", {"agentType": "claude", "updatedAt": quit_t, "handle": "%2"})]
    panes = [{"session": "mc-live", "cwd": "/w/a", "title": "✳ Claude", "at": str(NOW // 1000), "cmd": "claude"},
             {"session": "mc-quit", "cwd": "/w/a", "title": "✳ Claude", "at": str(quit_t // 1000), "cmd": "zsh"}]
    last_agent.observe(agents, panes, lambda t: t.startswith("✳"))
    assert last_agent.load()["/w/a"]["session"] == "mc-live"
    assert last_agent.resume(last_agent.load()["/w/a"], panes) == {"harness": "claude", "session": "mc-live"}
    last_agent.note({"/w/b": ("codex", "mc-cx", NOW)})  # a launched codex, still running: a quit ✳ there doesn't replace it
    running = [{"session": "mc-q", "cwd": "/w/b", "title": "✳ x", "at": str(quit_t // 1000), "cmd": "zsh"},
               {"session": "mc-cx", "cwd": "/w/b", "title": "⌬ x", "at": str(NOW // 1000), "cmd": "codex"}]
    last_agent.observe([("/w/b", {"agentType": "claude", "updatedAt": quit_t, "handle": "%3"})], running, lambda t: t.startswith("✳"))
    assert last_agent.resume(last_agent.load()["/w/b"], running) == {"harness": "codex", "session": "mc-cx"}


def test_a_newer_agent_elsewhere_beats_an_old_live_basement_pane(home):
    old_pane = [{"session": "mc-old", "cwd": "/w/a", "title": "✳ Claude", "at": str((NOW - 86_400_000) // 1000), "cmd": "claude"}]
    last_agent.observe([("/w/a", {"agentType": "claude", "updatedAt": NOW})], old_pane, lambda t: t.startswith("✳"))  # Orca's, today
    assert "session" not in last_agent.load()["/w/a"]  # continue today's, don't attach yesterday's
    last_agent.observe([("/w/a", {})], old_pane, lambda t: t.startswith("✳"))  # Orca no longer lists it: still today's
    assert "session" not in last_agent.load()["/w/a"]


def test_a_live_session_replaces_a_quit_one(home):
    last_agent.note({"/w/a": ("claude", "mc-quit", NOW + 600_000)})  # recorded while it ran; it has quit since
    panes = [{"session": "mc-live", "cwd": "/w/a", "title": "✳ Claude", "at": str(NOW // 1000), "cmd": "claude"},
             {"session": "mc-quit", "cwd": "/w/a", "title": "✳ Claude", "at": str((NOW + 600_000) // 1000), "cmd": "-zsh"}]
    last_agent.observe([("/w/a", {})], panes, lambda t: t.startswith("✳"))
    assert last_agent.load()["/w/a"]["session"] == "mc-live"


def test_note_keeps_the_file_on_a_read_error(home):
    last_agent.note({"/w/a": ("claude", None, NOW)})
    before = last_agent.FILE.read_text()
    with mock.patch.object(last_agent.Path, "read_text", side_effect=PermissionError("denied")), pytest.raises(OSError):
        last_agent.note({"/w/b": ("codex", None, NOW)})
    assert last_agent.FILE.read_text() == before
    with mock.patch.object(last_agent.Path, "read_text", side_effect=PermissionError("denied")):
        assert last_agent.load() == {}  # a reader just sees nothing


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
    assert last_agent.resume(a, running, created=101) == {"harness": "claude", "session": "mc-a"}  # live: .git's mtime can't hide it


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
    last_agent.note({"/w/old": ("opencode", "mc-gone", NOW), "/w/exited": ("claude", "mc-sh2", NOW), "/w/closed": ("codex", None, NOW)})
    monkeypatch.setitem(dash.greenlight.CONFIG, "harnesses", {"opencode": {"bin": "/opt/oc/opencode"}, "claude": {"bin": "x; rm"}, "pi": "/opt/pi"})
    pucks = [{"path": p, "agent": {"state": st}} for p, st in
             (("/w/live", "done"), ("/w/old", "none"), ("/w/busy", "working"), ("/w/new", "none"), ("/w/exited", "none"),
              ("/w/ask", "waiting"), ("/w/pi", "none"), ("/w/closed", "working"))]  # closed: the scan's state, its agent gone since
    agents = [("/w/live", {"agentType": "claude"}), ("/w/old", {}), ("/w/busy", {"agentType": "codex", "state": "working"}),
              ("/w/new", {}), ("/w/exited", {}), ("/w/ask", {"agentType": "claude", "state": "waiting"}), ("/w/pi", {"agentType": "pi", "updatedAt": NOW}), ("/w/closed", {})]
    tm = _tmux_rows(["%1", "mc-live", "/w/live", "✳ Done", "1", "claude"], ["%2", "mc-sh", "/w/new", "zsh", "1", "zsh"],
                    ["%3", "mc-sh2", "/w/exited", "✳ Done", "1", "-zsh"])  # Claude quit there: its title stays, its shell runs
    with mock.patch.object(ade_basement.subprocess, "run", return_value=tm):
        dash.add_resume(pucks, agents)
    by = {p["path"]: p["resume"] for p in pucks}
    assert by == {"/w/live": {"harness": "claude", "session": "mc-live"}, "/w/old": {"harness": "opencode", "bin": "/opt/oc/opencode"},
                  "/w/busy": {}, "/w/new": {}, "/w/exited": {"harness": "claude"},
                  "/w/ask": {}, "/w/pi": {"harness": "pi"}, "/w/closed": {"harness": "codex"}}  # waiting on you in another ADE: no second copy; a bad config entry: no bin


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


def test_add_resume_says_whether_tmux_answered(home):
    """The console keeps an agent's tab only while trees list its session; when tmux didn't answer, none is listed, and
    the page must know that means unknown, not quit."""
    pucks = [{"path": "/w/a", "agent": {}}]
    with mock.patch.object(ade_basement.subprocess, "run", return_value=_tmux_rows()):
        assert dash.add_resume(pucks, [("/w/a", {})]) is True
    with mock.patch.object(ade_basement.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 20)):
        assert dash.add_resume(pucks, [("/w/a", {})]) is False
    assert "session" not in pucks[0]["resume"]


def test_agent_sessions_go_by_what_runs_not_a_lingering_title():
    tm = [{"session": "mc-a", "cwd": "/w/a/src", "title": "✳ Done", "cmd": "claude"},  # running
          {"session": "mc-b", "cwd": "/w/a", "title": "✳ Done", "cmd": "-zsh"},  # Claude quit there: its title stays
          {"session": "mc-c", "cwd": "/w/a", "title": "x", "cmd": "codex"},
          {"session": "gl-warm-1", "cwd": "/w/a", "title": "", "cmd": "claude"},  # not one of ours
          {"session": "mc-d", "cwd": "/elsewhere", "title": "", "cmd": "claude"}]
    assert dash.agent_sessions(tm, ["/w/a", "/w/b"]) == {"/w/a": ["mc-a", "mc-c"]}
