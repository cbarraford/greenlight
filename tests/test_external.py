"""external.py: agents outside greenlight's tmux, by worktree, and what the scan, cleanup and native rows do with them (#195)."""
import os
from unittest import mock

import pytest

import ade_basement
import cleanup
import external
import greenlight


def test_agent_of():
    assert external.agent_of(["claude", "--resume"]) == "claude"
    assert external.agent_of(["/srv/bin/codex"]) == "codex"
    assert external.agent_of(["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"]) == "codex"
    assert external.agent_of(["node", "/srv/x/node_modules/@anthropic-ai/claude-code/cli.js"]) == "claude"
    assert external.agent_of(["node", "/srv/x/node_modules/@google/gemini-cli/dist/index.js"]) == "gemini"
    assert external.agent_of(["agy"]) == "agy" and external.agent_of(["opencode", "run"]) == "opencode"
    assert external.agent_of(["/opt/antigravity/antigravity", "."]) is None  # the editor, open for days, isn't an agent
    assert external.agent_of(["/Applications/Claude.app/Contents/MacOS/Claude"]) is None  # a desktop app: its CLI children count
    assert external.agent_of(["/opt/x/codex", "--type=renderer"]) is None
    assert external.agent_of(["vim", "codex.md"]) is None  # an argument naming an agent isn't one
    assert external.agent_of(["zsh"]) is None and external.agent_of([]) is None
    assert external.agent_of(["env", "-i", "claude"]) == "claude"  # a launcher's flags are skipped


PROCS = {
    1: {"ppid": 0, "argv": ["init"]},
    10: {"ppid": 1, "argv": ["tmux", "-L", "greenlight"]},
    11: {"ppid": 10, "argv": ["zsh"]},                    # a greenlight pane
    12: {"ppid": 11, "argv": ["claude"]},                 # ours: under that pane
    20: {"ppid": 1, "argv": ["orca-terminal"]},
    21: {"ppid": 20, "argv": ["claude"]},                 # Orca's: external
    22: {"ppid": 21, "argv": ["node", "/srv/mcp/server.js"]},
    23: {"ppid": 21, "argv": ["claude", "mcp"]},          # a claude's own child claude: not another agent
    30: {"ppid": 1, "argv": ["node", "/srv/n/@openai/codex/bin/codex.js"]},  # a plain terminal's codex
    31: {"ppid": 30, "argv": ["codex"]},                  # its native binary: the same agent
    40: {"ppid": 39, "argv": ["gemini"], "start": 1700000000},  # parent gone
    50: {"ppid": 21, "argv": ["codex", "exec"]},          # the Orca claude runs codex in another worktree: its own agent
}
CWD = {21: "/srv/w/app-feat", 22: "/srv/w/app-feat", 23: "/srv/w/app-feat", 30: "/srv/w/app-feat/sub",
       31: "/srv/w/app-feat/sub", 40: "/srv/w/elsewhere", 12: "/srv/w/app-mine", 50: "/srv/w/app-other"}


def test_scan_finds_only_outside_agents():
    got = external.scan(procs=PROCS, roots={11}, cwd_of=CWD.get)
    assert got == [{"pid": 21, "agent": "claude", "cwd": "/srv/w/app-feat", "start": None},
                   {"pid": 30, "agent": "codex", "cwd": "/srv/w/app-feat/sub", "start": None},
                   {"pid": 40, "agent": "gemini", "cwd": "/srv/w/elsewhere", "start": 1700000000},
                   {"pid": 50, "agent": "codex", "cwd": "/srv/w/app-other", "start": None}]
    assert external.scan(procs=PROCS, roots=set(), cwd_of=CWD.get)[0]["pid"] == 12  # no greenlight tmux: every agent is external
    assert external.scan(procs={5: {"ppid": 6, "argv": ["claude"]}, 6: {"ppid": 5, "argv": ["sh"]}}, roots=set(),
                         cwd_of=lambda p: "/x") == [{"pid": 5, "agent": "claude", "cwd": "/x", "start": None}]  # a ppid loop ends


def test_by_worktree_deepest_wins():
    found = external.scan(procs=PROCS, roots={11}, cwd_of=CWD.get)
    got = external.by_worktree(["/srv/w/app-feat/", "/srv/w/app-feat/sub", "/srv/w/app"], found)
    assert {k: [a["pid"] for a in v] for k, v in got.items()} == {"/srv/w/app-feat": [21], "/srv/w/app-feat/sub": [30]}
    assert "/srv/w/app" not in external.by_worktree(["/srv/w/app"], found)  # /srv/w/app-feat isn't inside /srv/w/app


def test_agents_cache_and_failure(monkeypatch):
    monkeypatch.setattr(external, "_CACHE", {"at": 0.0, "val": None})
    calls = []
    monkeypatch.setattr(external, "scan", lambda: calls.append(1) or [{"pid": 1}])
    assert external.agents() == [{"pid": 1}] and external.agents() == [{"pid": 1}] and len(calls) == 1
    external.agents(fresh=True)
    assert len(calls) == 2

    def boom():
        raise external.subprocess.TimeoutExpired("lsof", 10)
    monkeypatch.setattr(external, "scan", boom)
    assert external.agents(fresh=True) == []  # listing and dispatch carry on
    with pytest.raises(external.Unknown):
        external.agents(fresh=True, strict=True)  # cleanup must not
    with pytest.raises(external.Unknown):
        external.agents(strict=True)  # a failed scan is never cached as "no agents"


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="Linux /proc")
def test_linux_proc_table_reads_this_process():
    procs = external._proc_linux()
    assert os.getpid() in procs and procs[os.getpid()]["ppid"] == os.getppid()
    assert external._cwd_linux(os.getpid()) == os.getcwd()


def test_mac_parsers(monkeypatch):
    monkeypatch.setattr(external.time, "time", lambda: 1000000)
    sup = "/opt/Application Support/x/claude"  # spaces in the path (as under ~/Library): whitespace-split args lose it
    out = {"pid=,args=": f" 101 {sup} --resume\n 102 node x\n",
           "pid=,ppid=,etime=,comm=": f" 101  1   01:02:03 {sup}\n 102 101 2-00:00:05 node\nbad line\n",
           "lsof": "p101\nfcwd\nn/srv/w/app\np102\nn/srv/w/b\n"}
    monkeypatch.setattr(external.subprocess, "run",
                        lambda cmd, **k: mock.Mock(stdout=out["lsof" if "lsof" in cmd[0] else cmd[-1]], returncode=0))
    assert external._proc_mac() == {101: {"ppid": 1, "argv": [sup, "--resume"], "start": 1000000 - 3723},
                                    102: {"ppid": 101, "argv": ["node", "x"], "start": 1000000 - 172805}}
    assert external.agent_of(external._proc_mac()[101]["argv"]) == "claude"
    assert external._cwds_mac([101, 102]) == {101: "/srv/w/app", 102: "/srv/w/b"} and external._cwds_mac([]) == {}


def test_etime():
    assert external.etime_secs("05") == 5 and external.etime_secs("01:05") == 65 and external.etime_secs("3-01:00:00") == 262800
    assert external.etime_secs("x") is None


def test_native_rows_carry_external_agents(monkeypatch):
    monkeypatch.setattr(ade_basement, "panes", lambda cmd=False: [])
    monkeypatch.setattr(ade_basement, "saved", lambda: ["/srv/w/app"])
    monkeypatch.setattr(ade_basement, "git", lambda repo, *a: "worktree /srv/w/app\nworktree /srv/w/app-feat\n")
    monkeypatch.setattr(ade_basement, "git_info", lambda p: {"path": p, "branch": "refs/heads/x", "isMainWorktree": p == "/srv/w/app",
                                                             "repoId": "/srv/w/app", "repo": "app", "projectId": None})
    monkeypatch.setattr(external, "agents", lambda fresh=False: [{"pid": 21, "agent": "claude", "cwd": "/srv/w/app-feat/src", "start": 1700}])
    rows = {w["path"]: w for w in ade_basement.worktrees()}
    assert rows["/srv/w/app"]["agents"] == []
    (ag,) = rows["/srv/w/app-feat"]["agents"]
    assert ag["state"] == "working" and ag["external"] and ag["agentType"] == "claude" and ag["pid"] == 21 and ag["stateStartedAt"] == 1700000
    assert greenlight.primary_agent(rows["/srv/w/app-feat"]["agents"]) is ag
    mine = {"state": "waiting", "agentType": "claude", "updatedAt": 5}  # an agent of ours waiting here still shows first
    assert greenlight.primary_agent([ag, mine]) is mine


def test_scan_keeps_off_an_external_agent():
    ws = {"next": {"owner": "agent", "step": "mr_open", "action": "x"}, "agent": {"state": "working", "type": "claude", "external": True}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "working" and "outside Greenlight" in ws["next"]["action"]
    ws = {"next": {"owner": "greenlight", "step": "merge", "action": "x"}, "agent": {"state": "working", "type": "codex", "external": True}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "working"  # no merge under it either
    ws = {"next": {"owner": "me", "step": "merge", "action": "sign off"}, "agent": {"state": "working", "external": True}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "me" and ws["next"]["action"] == "sign off"  # the user's move stays theirs
    old = greenlight.NOW_MS - (greenlight.EXTERNAL_STALE_H + 1) * 3.6e6
    ws = {"next": {"owner": "greenlight", "step": "merge", "action": "x"}, "agent": {"state": "working", "type": "claude", "external": True, "since": old}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "me" and "open here outside Greenlight for 7h" in ws["next"]["action"]  # idle for hours: yours to close


def test_cleanup_never_deletes_under_an_external_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 7, "agent": "codex", "cwd": "/srv/w/app-feat"}])
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    assert cleanup.outside_agent("/srv/w/app-feat") == "codex" and cleanup.outside_agent("/srv/w/app-other") is None
    # evaluate(): a merged MR's worktree, clean and idle, is still kept while the agent runs in it
    wt = tmp_path / "w"
    wt.mkdir()
    repo = mock.Mock(error=None, default="main", base="origin/main", project="g/p")
    repo.mrs.return_value = [{"state": "merged", "label": "!4", "sha": "abc", "iid": 4}]
    monkeypatch.setattr(cleanup, "git", lambda path, *a, check=True: "abc" if a[:1] == ("rev-parse",) else "")
    monkeypatch.setattr(cleanup, "post_merge_left", lambda *a: None)
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 7, "agent": "codex", "cwd": str(wt)}])
    w = {"path": str(wt), "branch": "refs/heads/feat", "id": "w", "comment": ""}
    assert cleanup.evaluate(w, repo, set()) == ("skip", "MR !4 merged, but codex runs in it outside Greenlight")

    def unknown(fresh=False, strict=False):
        raise external.Unknown("TimeoutExpired")
    monkeypatch.setattr(external, "agents", unknown)  # the process check failed: never a candidate
    assert cleanup.evaluate(w, repo, set())[0] == "skip"
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")  # Orca lists its own agents' terminals: the busy check covers them
    assert cleanup.outside_agent(str(wt)) is None
    assert cleanup.evaluate(w, repo, set()) == ("candidate", "MR !4 merged")


def test_dash_marks_rows_any_ade_listed(monkeypatch):
    import dash
    rows = [{"path": "/srv/w/orca-made", "ade": "orca"}, {"path": "/srv/w/quiet", "ade": "orca"}]
    monkeypatch.setattr(external, "agents", lambda fresh=False: [{"pid": 3, "agent": "claude", "cwd": "/srv/w/orca-made/src"}])
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    dash.mark_external(rows)
    assert not any("external" in r for r in rows)  # Orca in use: Orca's own rows say what runs
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    dash.mark_external(rows)
    assert rows[0]["external"] and "external" not in rows[1]
