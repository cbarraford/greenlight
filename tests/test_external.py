"""external.py: agents outside greenlight's tmux, by worktree, and what the scan, cleanup and native rows do with them (#195)."""
import os
from pathlib import Path
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
    assert external.agent_of(["env", "FOO=1", "claude"]) == "claude"  # and its VAR=value
    assert external.agent_of(["bun", "run", "/srv/n/@anthropic-ai/claude-code/cli.js"]) == "claude"
    assert external.agent_of(["deno", "run", "-A", "/srv/n/@google/gemini-cli/dist/index.js"]) == "gemini"
    assert external.agent_of(["node", "--require", "/srv/hook.js", "/srv/n/@anthropic-ai/claude-code/cli.js"]) == "claude"
    assert external.agent_of(["node", "--a", "--b", "/srv/n/@openai/codex/bin/codex.js"]) == "codex"
    assert external.agent_of(["/Applications/Foo.app/Contents/Resources/bin/claude", "-p", "x"]) == "claude"  # shipped in an app
    assert external.agent_of(["node", "/srv/server.js", "claude"]) is None  # a script's argument naming an agent isn't one
    assert external.agent_of(["claude", "-p", "fix the crash in Greenlight.app/Contents/MacOS"]) == "claude"  # a prompt isn't the program
    assert external.agent_of(["claude", "mcp", "serve"]) is None  # a server that never edits a folder
    assert external.agent_of(["opencode", "serve"]) == "opencode"  # an ADE's per-worktree opencode server runs its sessions
    assert external.agent_of(["codex", "app-server"]) == "codex"  # T3's and VS Code's per-session codex: an agent
    assert external.agent_of(["/srv/.codex/bin/codex", "app-server", "daemon", "pid-update-loop"]) is None  # the machine-wide daemon
    assert external.agent_of(["codex", "app-server", "--listen", "unix://x"]) is None
    assert external.agent_of(["codex", "app-server", "--listen", "stdio://"]) == "codex"  # a session's, spelled out
    assert external.agent_of(["codex", "app-server", "--listen=stdio://"]) == "codex"
    assert external.agent_of(["codex", "app-server", "-c", "k=v", "--listen", "stdio://"]) == "codex"  # past the first words
    assert external.agent_of(["node", "/srv/n/@anthropic-ai/claude-agent-sdk/cli.js", "--output-format", "stream-json"]) == "claude"  # T3's Claude
    assert external.agent_of(["/Applications/T3.app/Contents/MacOS/T3", "/srv/n/@anthropic-ai/claude-agent-sdk/cli.js"]) == "claude"  # as node
    assert external.agent_of(["node", "/srv/n/@anthropic-ai/claude-code/cli.js", "mcp", "serve"]) is None
    assert external.agent_of(["codex", "mcp-server"]) is None and external.agent_of(["codex", "exec", "fix it"]) == "codex"


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
    # one of our pane's agents running another in a different worktree (`cd ../B && codex exec`): that one is outside
    ours = {11: {"ppid": 1, "argv": ["zsh"]}, 12: {"ppid": 11, "argv": ["claude"]}, 13: {"ppid": 12, "argv": ["bash"]},
            14: {"ppid": 13, "argv": ["codex", "exec"]}}
    got = external.scan(procs=ours, roots={11: "/w/mine"}, cwd_of={12: "/w/mine/src", 14: "/w/other"}.get)
    assert [(a["pid"], a.get("pane")) for a in got] == [(12, "/w/mine"), (14, "/w/mine")]
    owned = external.by_worktree(["/w/mine", "/w/other"], got)
    assert {k: [a["pid"] for a in v] for k, v in owned.items()} == {"/w/other": [14]}  # ours stays ours; the other counts there
    nested = external.scan(procs=ours, roots={11: "/w/mine"}, cwd_of={12: "/w/mine", 14: "/w/mine/.wt/b"}.get)
    owned = external.by_worktree(["/w/mine", "/w/mine/.wt/b"], nested)  # a worktree nested in the pane's folder
    assert {k: [a["pid"] for a in v] for k, v in owned.items()} == {"/w/mine/.wt/b": [14]}
    # the reverse: our pane is in the nested worktree, its agent works in the parent. Asked about the parent alone, the
    # pane looks like the parent's (wrongly ours); only the whole list shows the agent is outside there
    rev = external.scan(procs=ours, roots={11: "/w/mine/.wt/b"}, cwd_of={12: "/w/mine/.wt/b", 14: "/w/mine"}.get)
    assert external.by_worktree(["/w/mine"], rev) == {}
    assert [a["pid"] for a in external.by_worktree(["/w/mine", "/w/mine/.wt/b"], rev)["/w/mine"]] == [14]
    launched = {11: {"ppid": 1, "argv": ["zsh"]}, 15: {"ppid": 11, "argv": ["opencode"]}}  # Greenlight launches opencode too
    assert external.by_worktree(["/w/b"], external.scan(procs=launched, roots={11: "/w/b"}, cwd_of={15: "/w/b"}.get)) == {}
    kid = {11: {"ppid": 1, "argv": ["zsh"]}, 12: {"ppid": 11, "argv": ["claude"]}, 13: {"ppid": 12, "argv": ["bash"]},
           16: {"ppid": 13, "argv": ["gemini", "-p", "x"]}}  # our claude's gemini in another worktree: outside there
    got = external.by_worktree(["/w/a", "/w/b"], external.scan(procs=kid, roots={11: "/w/a"}, cwd_of={12: "/w/a", 16: "/w/b"}.get))
    assert {k: [a["pid"] for a in v] for k, v in got.items()} == {"/w/b": [16]}
    assert external.scan(procs={5: {"ppid": 6, "argv": ["claude"]}, 6: {"ppid": 5, "argv": ["sh"]}}, roots=set(),
                         cwd_of=lambda p: "/x") == [{"pid": 5, "agent": "claude", "cwd": "/x", "start": None}]  # a ppid loop ends


def test_by_worktree_deepest_wins():
    found = external.scan(procs=PROCS, roots={11}, cwd_of=CWD.get)
    got = external.by_worktree(["/srv/w/app-feat/", "/srv/w/app-feat/sub", "/srv/w/app"], found)
    assert {k: [a["pid"] for a in v] for k, v in got.items()} == {"/srv/w/app-feat": [21], "/srv/w/app-feat/sub": [30]}
    assert "/srv/w/app" not in external.by_worktree(["/srv/w/app"], found)  # /srv/w/app-feat isn't inside /srv/w/app


def test_agents_cache_and_failure(monkeypatch):
    monkeypatch.setattr(external, "_CACHE", {"at": 0.0, "val": None, "failed": 0.0})
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
    monkeypatch.setattr(external, "scan", lambda: calls.append(1) or [{"pid": 2}])
    n = len(calls)
    assert external.agents() == [] and len(calls) == n  # right after a failure: no pile-up of scans
    assert external.agents(fresh=True) == [] and len(calls) == n  # even a fresh ask waits out the failure backoff
    monkeypatch.setitem(external._CACHE, "failed", 0.0)
    assert external.agents(fresh=True) == [{"pid": 2}] and len(calls) == n + 1


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="Linux /proc")
def test_linux_proc_table_reads_this_process():
    procs = external._proc_linux()
    assert os.getpid() in procs and procs[os.getpid()]["ppid"] == os.getppid()
    assert external._cwd_linux(os.getpid()) == os.getcwd()


def test_mac_parsers(monkeypatch):
    monkeypatch.setattr(external.time, "time", lambda: 1000000)
    sup = "/opt/Application Support/x/claude"  # spaces in the path (as under ~/Library): whitespace-split args lose it
    out = {"pid=,args=": f" 101 {sup} --resume\n 102 node x\n",
           "pid=,ppid=,etime=,uid=,comm=": f" 101  1   01:02:03 {os.getuid()} {sup}\n 102 101 2-00:00:05 0 node\nbad line\n",
           "lsof": "p101\nfcwd\nn/srv/w/app\np102\nn/srv/w/b\n"}
    monkeypatch.setattr(external.subprocess, "run",
                        lambda cmd, **k: mock.Mock(stdout=out["lsof" if "lsof" in cmd[0] else cmd[-1]], returncode=0))
    assert external._proc_mac() == {101: {"ppid": 1, "argv": [sup, "--resume"], "start": 1000000 - 3723, "other": False},
                                    102: {"ppid": 101, "argv": ["node", "x"], "start": 1000000 - 172805, "other": True}}
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
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 21, "agent": "claude", "cwd": "/srv/w/app-feat/src", "start": 1700}])
    rows = {w["path"]: w for w in ade_basement.worktrees()}
    assert rows["/srv/w/app"]["agents"] == []
    (ag,) = rows["/srv/w/app-feat"]["agents"]
    assert ag["state"] == "working" and ag["external"] and ag["agentType"] == "claude" and ag["pid"] == 21 and ag["stateStartedAt"] == 1700000
    assert greenlight.primary_agent(rows["/srv/w/app-feat"]["agents"]) is ag
    mine = {"state": "waiting", "agentType": "claude", "updatedAt": 5}  # an agent of ours waiting here still shows first
    assert greenlight.primary_agent([ag, mine]) is mine


def test_scan_keeps_off_an_external_agent():
    ws = {"next": {"owner": "agent", "step": "mr_open", "action": "x"}, "agent": {"state": "working", "type": "claude", "external": True, "ext_type": "claude"}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "working" and "outside Greenlight" in ws["next"]["action"]
    ws = {"next": {"owner": "greenlight", "step": "merge", "action": "x"}, "agent": {"state": "working", "type": "codex", "external": True, "ext_type": "codex"}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "working"  # no merge under it either
    ws = {"next": {"owner": "me", "step": "merge", "action": "sign off"}, "agent": {"state": "working", "external": True}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "me" and ws["next"]["action"] == "sign off"  # the user's move stays theirs
    old = greenlight.NOW_MS - (greenlight.EXTERNAL_STALE_H + 1) * 3.6e6
    ws = {"next": {"owner": "greenlight", "step": "merge", "action": "x"}, "agent": {"state": "working", "type": "claude", "external": True, "since": old, "ext_type": "codex"}}
    greenlight.agent_overlay(ws, {})
    assert ws["next"]["owner"] == "me" and ws["next"]["action"].startswith("codex has been running here outside Greenlight for over 6h")  # yours to close
    # (no hour count: the action is the question's marker, so it must not change every hour and re-ask)
    ws = {"next": {"owner": "agent", "step": "ci_green", "action": "x"}, "agent": {"state": "working", "type": "claude", "external": True, "ext_type": None}}
    greenlight.agent_overlay(ws, {})  # a failed process check (by_worktree_safe's placeholder): held, and says so
    assert ws["next"]["owner"] == "working" and "process check failed" in ws["next"]["action"]


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
        assert strict, "cleanup must ask strictly: a lenient scan answers [] on failure"
        raise external.Unknown("TimeoutExpired")
    monkeypatch.setattr(external, "agents", unknown)  # the process check failed: never a candidate
    assert cleanup.evaluate(w, repo, set())[0] == "skip"
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 8, "agent": "claude", "cwd": str(wt / ".wt" / "b")}])
    assert cleanup.outside_agent(str(wt), paths=[str(wt), str(wt / ".wt" / "b")]) == "claude"  # nested: removing wt removes it too
    rev = [{"pid": 14, "agent": "codex", "cwd": str(wt), "pane": str(wt / ".wt" / "b")}]  # our nested pane's codex in wt
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: rev)
    monkeypatch.setattr(cleanup, "ALL_PATHS", [str(wt), str(wt / ".wt" / "b")])  # scan() fills it with every worktree
    assert cleanup.outside_agent(str(wt)) == "codex"
    monkeypatch.setattr(cleanup, "ALL_PATHS", [])
    assert cleanup.outside_agent(str(wt)) is None  # (what asking about wt alone would have said)
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")  # Orca lists its own agents' terminals: the busy check covers them
    assert cleanup.outside_agent(str(wt)) is None
    assert cleanup.evaluate(w, repo, set()) == ("candidate", "MR !4 merged")


def test_orchestrate_rechecks_afresh():
    src = (Path(__file__).resolve().parent.parent / "scripts" / "orchestrate.py").read_text()
    main = src[src.index('diag.install("orchestrate")'):]
    assert "external.TTL = 0" in main[:300]  # its re-check before a dispatch never reuses the tick's scan


def test_basement_delete_warns(monkeypatch):
    import dash
    monkeypatch.setattr(dash, "outside_agent", lambda path: "codex")
    monkeypatch.setattr(dash, "tree", lambda p: (None, {"isPinned": False}))
    monkeypatch.setattr(dash, "tree_info", lambda p: {"dirty": 0, "unpushed": 0, "outside": dash.outside_agent(p)})
    assert dash.tree_rm("/srv/w/x") == {"risks": ["codex runs in it outside Greenlight"]}  # asks first (the page lists it, tree_info's "outside")
    monkeypatch.undo()
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    ab = dash.importlib.import_module("ade_basement")
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 14, "agent": "codex", "cwd": "/srv/w/x", "pane": "/srv/w/x/.wt/b"}])
    monkeypatch.setattr(ab, "worktrees", lambda: [{"path": "/srv/w/x"}, {"path": "/srv/w/x/.wt/b"}])
    assert dash.outside_agent("/srv/w/x") == "codex"  # the Delete check passes the whole worktree list
    monkeypatch.setattr(ab, "worktrees", lambda: 1 / 0)
    assert "can't be ruled out" in dash.outside_agent("/srv/w/x")  # no worktree list: Delete asks rather than guess
    listed = []
    monkeypatch.setattr(ab, "worktrees", lambda: listed.append(1) or [])
    assert dash.outside_agent("/srv/w/quiet") is None and listed == []  # nothing near it: no costly listing


def test_dash_marks_rows_any_ade_listed(monkeypatch):
    import dash
    rows = [{"path": "/srv/w/orca-made", "ade": "orca", "agent": "done"}, {"path": "/srv/w/quiet", "ade": "orca", "agent": "done"}]
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 3, "agent": "claude", "cwd": "/srv/w/orca-made/src"}])
    failed = [{"path": "/srv/w/a", "ade": "orca", "agent": "done"}]
    monkeypatch.setattr(external, "by_worktree_safe", lambda paths: {"/srv/w/a": [{"agent": None}]})
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    dash.mark_external(failed)
    assert failed[0]["agent"] == "working"  # a failed scan: every row busy, so the page starts nothing beside an agent
    monkeypatch.undo()
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 3, "agent": "claude", "cwd": "/srv/w/orca-made/src"}])
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    dash.mark_external(rows)
    assert not any("external" in r for r in rows)  # Orca in use: Orca's own rows say what runs
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    dash.mark_external(rows)
    assert rows[0]["external"] and rows[0]["agent"] == "working" and "external" not in rows[1] and rows[1]["agent"] == "done"
    mine = [{"path": "/srv/w/orca-made", "ade": "native", "agent": "waiting"}]  # a native row already says (primary_agent)
    dash.mark_external(mine)
    assert "external" not in mine[0]
    assert dash.resume_agent({"state": "working", "agentType": "codex", "external": True}) == {"state": "working"}
    assert dash.resume_agent(None) is None and dash.resume_agent({"state": "done", "agentType": "claude"})["agentType"] == "claude"
    # (the page's auto-run, auto-merge and Delete guards key on a working agent)


def test_symlinked_listings(monkeypatch, tmp_path):
    import dash
    tree = tmp_path / "tree"
    (tree / ".wt" / "child").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tree)
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    monkeypatch.setattr(external, "agents", lambda fresh=False, strict=False: [{"pid": 9, "agent": "claude", "cwd": str(tree / ".wt" / "child")}])
    link, child = str(tmp_path / "link"), str(tree / ".wt" / "child")
    assert cleanup.outside_agent(link, paths=[link, child]) == "claude"  # the parent listed through a symlink, the child not
    monkeypatch.setattr(dash.importlib.import_module("ade_basement"), "worktrees", lambda: [{"path": link}, {"path": child}])
    assert dash.outside_agent(link) == "claude"  # the Delete check's "anything near it?" resolves too


def test_tmux_roots_and_symlinks(monkeypatch, tmp_path):
    run = lambda rc, out="", err="": monkeypatch.setattr(external.subprocess, "run", lambda *a, **k: mock.Mock(returncode=rc, stdout=out, stderr=err))
    run(0, "11\t/w/a\n12\t/w/b c\n")
    assert external._tmux_roots() == {11: "/w/a", 12: "/w/b c"}
    run(1, err="no server running on /tmp/tmux-1/greenlight")
    assert external._tmux_roots() == {}  # no server: nothing of ours runs
    run(1, err="server exited unexpectedly")
    with pytest.raises(OSError):
        external._tmux_roots()  # can't tell ours from theirs: never "all of them are outside"
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    got = external.by_worktree([str(tmp_path / "link")], [{"pid": 1, "agent": "claude", "cwd": str(real / "src")}])
    assert list(got) == [str(tmp_path / "link")]  # keyed by the path as given


def test_linux_cwd_unreadable(monkeypatch):
    def denied(path):
        raise PermissionError(path)
    monkeypatch.setattr(external.os, "readlink", denied)
    monkeypatch.setattr(external.os, "stat", lambda p: mock.Mock(st_uid=os.getuid()))
    with pytest.raises(PermissionError):
        external._cwd_linux(1)  # ours and unreadable: the scan fails (closed), never "no agent here"
    monkeypatch.setattr(external.os, "stat", lambda p: mock.Mock(st_uid=os.getuid() + 1))
    assert external._cwd_linux(1) is None  # another user's: skipped (a known limit)
    monkeypatch.setattr(external.os, "readlink", lambda p: (_ for _ in ()).throw(FileNotFoundError(p)))
    assert external._cwd_linux(1) is None  # exited


def test_failures_fail_closed(monkeypatch):
    def unknown(fresh=False, strict=False):
        raise external.Unknown("OSError")
    monkeypatch.setattr(external, "agents", unknown)
    got = external.by_worktree_safe(["/srv/w/a/", "/srv/w/b"])  # dispatch waits a tick instead of landing in a live session
    assert set(got) == {"/srv/w/a", "/srv/w/b"} and got["/srv/w/a"][0]["agent"] is None
    monkeypatch.setattr(external.platform, "system", lambda: "Darwin")  # lsof gave no cwd for a live agent: not "none here"
    monkeypatch.setattr(external, "_alive", lambda pid: True)
    with pytest.raises(OSError, match="no cwd"):
        external.scan(procs={7: {"ppid": 1, "argv": ["claude"]}}, roots=set(), cwd_of=lambda p: None)
    deep = {11: {"ppid": 1, "argv": ["zsh"]}, 12: {"ppid": 11, "argv": ["claude"]}, 13: {"ppid": 12, "argv": ["bash"]},
            14: {"ppid": 13, "argv": ["codex", "exec"]}}
    with pytest.raises(OSError, match="no cwd"):  # our claude's codex, maybe in another worktree, with no cwd: fails closed
        external.scan(procs=deep, roots={11: "/w/a"}, cwd_of={12: "/w/a"}.get)
    assert external.scan(procs=deep, roots={11: "/w/a"}, cwd_of={14: "/w/a"}.get)[0]["pid"] == 14  # the pane's own claude missed: fine
    # another user's agent (root's, a second account's): skipped, not a failure that holds every worktree
    assert external.scan(procs={8: {"ppid": 1, "argv": ["claude"], "other": True}}, roots=set(), cwd_of=lambda p: None) == []
    assert external.scan(procs={9: {"ppid": 1, "argv": ["claude"]}}, roots=set(), cwd_of=lambda p: "") == []  # lsof listed it, no path (its folder was deleted): seen
    monkeypatch.setattr(external, "_alive", lambda pid: False)  # it exited mid-scan: nothing to find
    assert external.scan(procs={7: {"ppid": 1, "argv": ["claude"]}}, roots=set(), cwd_of=lambda p: None) == []
