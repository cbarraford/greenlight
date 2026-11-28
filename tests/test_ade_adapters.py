"""Comprehensive unit tests for ade_cmux, ade_herdr, ade_basement, and ade_t3."""
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import ade_basement
import ade_cmux
import ade_herdr
import ade_t3


def _fake_git_info(path: str) -> dict:
    return {
        "path": path,
        "repo": Path(path).name or "widgets",
        "repoId": "/r/widgets",
        "projectId": "git:gitlab.com/acme/widgets",
        "branch": "feat/x",
        "isMainWorktree": path.endswith("/widgets"),
    }


# ---------------------------------------------------------------------------
# ade_cmux
# ---------------------------------------------------------------------------

def test_cmux_subprocess_wrapper_and_error():
    ok = subprocess.CompletedProcess(["cmux"], 0, stdout='{"ok": true}', stderr="")
    with mock.patch.object(ade_cmux.subprocess, "run", return_value=ok) as m:
        assert ade_cmux.cmux("list-windows") == {"ok": True}
        assert "--json" in m.call_args.args[0]
    raw = subprocess.CompletedProcess(["cmux"], 0, stdout="plain", stderr="")
    with mock.patch.object(ade_cmux.subprocess, "run", return_value=raw):
        assert ade_cmux.cmux("send", js=False) == "plain"
    bad = subprocess.CompletedProcess(["cmux"], 1, stdout="", stderr="socket closed")
    with mock.patch.object(ade_cmux.subprocess, "run", return_value=bad), pytest.raises(RuntimeError, match="socket closed"):
        ade_cmux.cmux("list-windows")


def test_cmux_sessions_worktrees_and_repos(tmp_path):
    hooks_dir = tmp_path / "cmuxterm"
    hooks_dir.mkdir()
    (hooks_dir / "broken-hook-sessions.json").write_text("{not json")
    (hooks_dir / "claude-hook-sessions.json").write_text(json.dumps({
        "activeSessionsBySurface": {"surf-1": {"sessionId": "s1"}},
        "sessions": {
            "s1": {
                "sessionId": "s1",
                "surfaceId": "surf-1",
                "workspaceId": "ws-1",
                "agentLifecycle": "needsInput",
                "launchCommand": {"launcher": "codex"},
                "lastBody": "Pick A or B",
                "updatedAt": 1700000,
            },
            "s-dead": {"sessionId": "s-dead", "surfaceId": "surf-2", "workspaceId": "ws-1"},
        },
    }))

    def fake_cmux(*args, js=True):
        if args == ("list-windows",):
            return [{"id": "win-1"}]
        if args[:2] == ("workspace", "list"):
            return {"workspaces": [{"id": "ws-1", "title": "feat-1", "current_directory": "/w/feat-1", "description": "Merge !9", "pinned": True}]}
        return {}

    with (
        mock.patch.object(ade_cmux, "HOOKS", hooks_dir),
        mock.patch.object(ade_cmux, "cmux", side_effect=fake_cmux),
        mock.patch.object(ade_cmux, "git_info", side_effect=_fake_git_info),
    ):
        sess = ade_cmux.sessions()
        assert list(sess.keys()) == ["surf-1"]

        wts = ade_cmux.worktrees()
        assert len(wts) == 1 and wts[0]["comment"] == "Merge !9"
        assert wts[0]["agents"][0]["state"] == "waiting"

        repos = ade_cmux.repos()
        assert repos["repos"][0]["id"] == "/r/widgets"

    for dead_op in ("call", "terminals", "read", "send", "launch", "create", "set_goal", "remove"):
        assert not hasattr(ade_cmux, dead_op)


# ---------------------------------------------------------------------------
# ade_herdr
# ---------------------------------------------------------------------------

def test_herdr_subprocess_wrapper_and_session_flag():
    ok = subprocess.CompletedProcess(["herdr"], 0, stdout='{"result": {"panes": []}}', stderr="")
    with (
        mock.patch.dict(os.environ, {"GREENLIGHT_HERDR_SESSION": "sess-9"}),
        mock.patch.object(ade_herdr.subprocess, "run", return_value=ok) as m,
    ):
        assert ade_herdr.herdr("pane", "list") == {"panes": []}
        assert m.call_args.args[0][:3] == ["herdr", "--session", "sess-9"]
    bad = subprocess.CompletedProcess(["herdr"], 1, stdout="", stderr="boom")
    with mock.patch.object(ade_herdr.subprocess, "run", return_value=bad), pytest.raises(RuntimeError, match="boom"):
        ade_herdr.herdr("pane", "list")


def test_herdr_worktrees_and_repos(tmp_path):
    def fake_herdr(*args, js=True):
        if args[:2] == ("agent", "list"):
            return {"agents": [{"pane_id": "p:1", "workspace_id": "w1", "agent": "claude", "agent_status": "working"}]}
        if args[:2] == ("pane", "list"):
            return {"panes": [{"pane_id": "p:1", "workspace_id": "w1", "cwd": "/w/feat-1"}, {"pane_id": "p:2", "workspace_id": "w2", "cwd": "/w/widgets"}]}
        if args[:2] == ("workspace", "list"):
            return {"workspaces": [
                {"workspace_id": "w1", "label": "feat-1", "worktree": {"checkout_path": "/w/feat-1"}},
                {"workspace_id": "w2", "label": "widgets", "worktree": None},
            ]}
        return {}

    goals_file = tmp_path / "goals.json"
    goals_file.write_text(json.dumps({"/w/feat-1": "Goal 1"}))
    with (
        mock.patch.object(ade_herdr, "herdr", side_effect=fake_herdr),
        mock.patch.object(ade_herdr, "git_info", side_effect=_fake_git_info),
        mock.patch("ade_common.GOALS", goals_file),
    ):
        wts = ade_herdr.worktrees()
        assert wts[0]["comment"] == "Goal 1" and wts[0]["agents"][0]["state"] == "working"
        assert wts[1]["path"] == "/w/widgets"
        assert ade_herdr.repos()["repos"][0]["id"] == "/r/widgets"

    for dead_op in ("call", "terminals", "read", "send", "launch", "create", "set_goal", "remove"):
        assert not hasattr(ade_herdr, dead_op)


# ---------------------------------------------------------------------------
# ade_basement
# ---------------------------------------------------------------------------

def test_basement_full_lifecycle(tmp_path):
    repos_file = tmp_path / "repos.json"
    briefs_dir = tmp_path / "briefs"
    goals_file = tmp_path / "goals.json"

    pane_row = "\t".join(["%1", "mc-a", "/w/feat-1/sub", "✳ Claude", "1700000"])
    pane_row2 = "\t".join(["%2", "mc-b", "/w/widgets", "⠋ Working", "1700001"])

    def fake_tmux(*args):
        if args[:2] == ("list-panes", "-a"):
            return f"{pane_row}\n{pane_row2}\n"
        if args[0] == "capture-pane":
            return "Pick one\n❯ 1. Yes\n  2. No\n"
        if args[0] == "display-message":
            return "%9\n"
        return ""

    tmux_calls = []
    def tracking_tmux(*args):
        tmux_calls.append(args)
        return fake_tmux(*args)

    with (
        mock.patch.object(ade_basement, "REPOS", repos_file),
        mock.patch.object(ade_basement, "BRIEFS", briefs_dir),
        mock.patch("ade_common.GOALS", goals_file),
        mock.patch.object(ade_basement, "tmux", side_effect=tracking_tmux),
        mock.patch.object(ade_basement, "git", return_value="worktree /w/widgets\nworktree /w/feat-1\n"),
        mock.patch.object(ade_basement, "git_info", side_effect=_fake_git_info),
        mock.patch.object(ade_basement.worktrunk, "create", return_value="/w/new-wt") as m_create,
        mock.patch.object(ade_basement, "main_checkout", side_effect=lambda r: r),  # /r/widgets is a fake repo
        mock.patch.object(ade_basement.worktrunk, "remove") as m_rm,
        mock.patch.object(ade_basement.subprocess, "run", return_value=subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")),
        mock.patch.object(ade_basement.time, "sleep"),
    ):
        assert ade_basement.saved() == []
        ade_basement.remember("/r/widgets")
        ade_basement.remember("/r/widgets")
        assert ade_basement.saved() == ["/r/widgets"]

        ade_basement.call("worktree", "set", "--worktree", "/w/feat-1", "--comment", "Ship it")
        wts = ade_basement.call("worktree", "ps")["worktrees"]
        by_path = {w["path"]: w for w in wts}
        assert by_path["/w/feat-1"]["comment"] == "Ship it"
        assert by_path["/w/feat-1"]["agents"][0]["state"] == "waiting"
        assert by_path["/w/widgets"]["agents"][0]["state"] == "working"

        terms = ade_basement.call("terminal", "list")["terminals"]
        assert len(terms) == 2 and terms[0]["worktreeId"] == "/w/feat-1"
        assert terms[0]["tabId"] == "basement" and terms[0]["leafId"] == "%1" and terms[0]["session"] == "mc-a"

        rd = ade_basement.call("terminal", "read", "--terminal", "%1", "--limit", "30")
        assert rd["terminal"]["handle"] == "%1" and rd["terminal"]["draft"] == ""

        ade_basement.call("terminal", "send", "--terminal", "%1", "--text", "hello\nworld", "--enter")
        ade_basement.call("terminal", "send", "--terminal", "%1", "--text", "2")
        assert any(c[0] == "paste-buffer" for c in tmux_calls)
        assert any(c[:2] == ("send-keys", "-t") and "-l" in c for c in tmux_calls)

        launched = ade_basement.call("agent", "launch", "--worktree", "/w/feat-1", "--agent", "claude", "--prompt", "a\nb")
        assert launched["terminal"]["handle"] == "%9" and launched["terminal"]["session"].startswith("mc-")
        assert len(list(briefs_dir.glob("*.md"))) == 1

        created = ade_basement.call("worktree", "create", "--repo", "id:/r/widgets", "--name", "new-wt", "--comment", "Goal")
        assert created == {"worktree": {"id": "/w/new-wt", "path": "/w/new-wt"}}
        m_create.assert_called_once_with("/r/widgets", "new-wt", None)

        removed = ade_basement.call("worktree", "rm", "--worktree", "/w/feat-1", "--force")
        assert removed == {"removed": "/w/feat-1"}
        m_rm.assert_called_once_with("/r/widgets", "/w/feat-1", force=True)

        assert ade_basement.call("repo", "list")["repos"][0]["id"] == "/r/widgets"
        with pytest.raises(RuntimeError, match="no native equivalent"):
            ade_basement.call("bad", "cmd")


def test_basement_detects_codex_and_agy_panes(tmp_path):
    rows = "\n".join([
        "\t".join(["%1", "mc-a", "/w/feat-1", "my-codex-task", "1700001", "codex"]),
        "\t".join(["%2", "mc-b", "/w/widgets", "my-agy-task", "1700002", "agy"]),
        "\t".join(["%3", "mc-c", "/w/widgets", "zsh", "1700003", "zsh"]),
    ])

    def fake_tmux(*args):
        if args[:2] == ("list-panes", "-a"):
            return rows
        if args[0] == "capture-pane":
            return "Working (5s · esc to interrupt)\n" if "%1" in args else "▸ 1. Continue\n  2. Stop\n"
        return ""

    with (
        mock.patch.object(ade_basement, "REPOS", tmp_path / "repos.json"),
        mock.patch("ade_common.GOALS", tmp_path / "goals.json"),
        mock.patch.object(ade_basement, "tmux", side_effect=fake_tmux),
        mock.patch.object(ade_basement, "git", return_value="worktree /w/widgets\nworktree /w/feat-1\n"),
        mock.patch.object(ade_basement, "git_info", side_effect=_fake_git_info),
    ):
        wts = {w["path"]: w for w in ade_basement.call("worktree", "ps")["worktrees"]}
        assert wts["/w/feat-1"]["agents"][0]["agentType"] == "codex" and wts["/w/feat-1"]["agents"][0]["state"] == "working"
        assert wts["/w/widgets"]["agents"][0]["agentType"] == "agy" and wts["/w/widgets"]["agents"][0]["state"] == "waiting"
        terms = {t["handle"]: t for t in ade_basement.call("terminal", "list")["terminals"]}
        assert terms["%1"]["agentIdentity"] == "codex" and terms["%2"]["agentIdentity"] == "agy" and terms["%3"]["agentIdentity"] is None
        ps = ade_basement.pane_states()
        assert ps["mc-a"] == {"state": "working", "title": "⌬ my-codex-task"}
        assert ps["mc-b"] == {"state": "waiting", "title": "◈ my-agy-task"}
        assert "mc-c" not in ps


def test_basement_hides_claude_agent_worktrees(tmp_path):
    """Claude Code's `.claude/worktrees/agent-…` checkouts aren't workspaces (#127)."""
    porcelain = "\n".join([
        "worktree /w/widgets", "HEAD abc", "branch refs/heads/main", "",
        "worktree /w/widgets/.claude/worktrees/agent-a1b2", "HEAD def", "detached", "",
        "worktree /w/feat-1", "HEAD 123", "branch refs/heads/feat-1", "",
        "worktree /w/not.claude/worktrees-x", "HEAD 456", "branch refs/heads/x", "",
    ])
    row = "\t".join(["%1", "mc-a", "/w/widgets/.claude/worktrees/agent-a1b2/src", "⠋ Working", "1700001"])

    def fake_tmux(*args):
        return row if args[:2] == ("list-panes", "-a") else ""

    with (
        mock.patch.object(ade_basement, "REPOS", tmp_path / "repos.json"),
        mock.patch("ade_common.GOALS", tmp_path / "goals.json"),
        mock.patch.object(ade_basement, "tmux", side_effect=fake_tmux),
        mock.patch.object(ade_basement, "git", return_value=porcelain),
        mock.patch.object(ade_basement, "git_info", side_effect=_fake_git_info),
    ):
        wts = {w["path"]: w for w in ade_basement.call("worktree", "ps")["worktrees"]}
        assert sorted(wts) == ["/w/feat-1", "/w/not.claude/worktrees-x", "/w/widgets"]
        # a pane inside a hidden agent checkout counts toward the checkout that holds it
        assert wts["/w/widgets"]["agents"][0]["state"] == "working"
    assert ade_basement.agent_worktree("/r/.claude/worktrees/agent-1")
    assert not ade_basement.agent_worktree("/r/claude/worktrees/agent-1")
    assert not ade_basement.agent_worktree("/r/.claude/worktreesX")


def test_basement_tmux_no_server_vs_error():
    no_srv = subprocess.CompletedProcess(["tmux"], 1, stdout="", stderr="no server running on /tmp/tmux")
    with mock.patch.object(ade_basement.subprocess, "run", return_value=no_srv):
        assert ade_basement.tmux("list-panes") == ""
        with pytest.raises(RuntimeError, match="no server running"):
            ade_basement.tmux("send-keys", "-t", "%1", "hi")
    err = subprocess.CompletedProcess(["tmux"], 1, stdout="", stderr="bad target")
    with mock.patch.object(ade_basement.subprocess, "run", return_value=err), pytest.raises(RuntimeError, match="bad target"):
        ade_basement.tmux("list-panes")


def test_basement_pane_states():
    rows = "\n".join([
        "\t".join(["%1", "mc-work", "/w/a", "⠋ Fixing tests", "1700001"]),
        "\t".join(["%2", "mc-wait", "/w/b", "✳ Ask", "1700002"]),
        "\t".join(["%3", "mc-done", "/w/c", "✳ Shipped", "1700003"]),
        "\t".join(["%4", "mc-shell", "/w/d", "zsh", "1700004"]),
        "\t".join(["%5", "mc-multi", "/w/e", "✳ Multi", "1700005"]),
        "\t".join(["%6", "mc-multi", "/w/e", "⠋ Busy", "1700006"]),
    ])
    screens = {
        "%2": "Which one?\n❯ 1. Yes\n  2. No\n",
        "%3": "✻ Churned for 3s · done\n❯ \n",
        "%5": "❯ \n",
    }

    def fake_tmux(*args):
        if args[:2] == ("list-panes", "-a"):
            return rows + "\n"
        if args[0] == "capture-pane":
            return screens[args[-1]]
        return ""

    with mock.patch.object(ade_basement, "tmux", side_effect=fake_tmux):
        got = ade_basement.pane_states()
    assert got == {
        "mc-work": {"state": "working", "title": "⠋ Fixing tests"},
        "mc-wait": {"state": "waiting", "title": "✳ Ask"},
        "mc-done": {"state": "done", "title": "✳ Shipped"},
        "mc-multi": {"state": "working", "title": "⠋ Busy"},
    }


# ---------------------------------------------------------------------------
# ade_t3
# ---------------------------------------------------------------------------

def test_t3_projects_sqlite_worktrees_and_repos(tmp_path):
    ud = tmp_path / "userdata"
    ud.mkdir()
    db_path = ud / "statev2.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("create table projection_projects (project_id text, title text, workspace_root text, deleted_at text)")
    conn.execute("insert into projection_projects values ('p1', 'feat-1', '/w/feat-1', null)")
    conn.execute("insert into projection_projects values ('p-del', 'old', '/w/old', '2026-01-01')")
    conn.commit()
    conn.close()

    def fake_t3(*args, stdin=None):
        if args[:2] == ("thread", "list"):
            return json.dumps([
                {"threadId": "th-1", "status": "running", "updatedAt": "2026-10-27T20:55:40.176Z", "providerInstanceId": "claudeAgent"},
            ])
        if args[:2] == ("thread", "show"):
            return json.dumps({
                "thread": {"threadId": "th-1", "status": "waiting", "updatedAt": "2026-10-27T20:55:40.176Z", "providerInstanceId": "claudeAgent"},
                "messages": [{"role": "user", "text": "check ci"}, {"role": "assistant", "text": "CI passed"}],
            })
        return ""

    goals_file = tmp_path / "goals.json"
    goals_file.write_text(json.dumps({"/w/feat-1": "Merge !3"}))
    with (
        mock.patch.object(ade_t3, "HOME", tmp_path),
        mock.patch.object(ade_t3, "t3", side_effect=fake_t3),
        mock.patch.object(ade_t3, "git_info", side_effect=_fake_git_info),
        mock.patch("ade_common.GOALS", goals_file),
    ):
        assert ade_t3.projects() == [("p1", "feat-1", "/w/feat-1")]

        wts = ade_t3.worktrees()
        assert len(wts) == 1 and wts[0]["comment"] == "Merge !3"
        assert wts[0]["agents"][0]["state"] == "waiting"
        assert wts[0]["agents"][0]["lastAssistantMessage"] == "CI passed"
        assert wts[0]["agents"][0]["agentType"] == "claude"

        assert ade_t3.repos()["repos"][0]["id"] == "/r/widgets"

    for dead_op in ("call", "terminals", "read", "send", "launch", "create", "set_goal", "remove"):
        assert not hasattr(ade_t3, dead_op)


def test_t3_subprocess_wrapper_error():
    ok = subprocess.CompletedProcess(["t3"], 0, stdout="ok", stderr="")
    with mock.patch.object(ade_t3.subprocess, "run", return_value=ok):
        assert ade_t3.t3("thread", "list") == "ok"
    bad = subprocess.CompletedProcess(["t3"], 1, stdout="", stderr="server down")
    with mock.patch.object(ade_t3.subprocess, "run", return_value=bad), pytest.raises(RuntimeError, match="server down"):
        ade_t3.t3("thread", "list")


def test_basement_worktrees_detects_wrapped_and_fresh_harnesses(gl_home):
    import last_agent

    now_ms = int(ade_basement.time.time() * 1000)
    last_agent.note({"/w/fresh": ("codex", "mc-fresh", now_ms), "/w/node-cx": ("codex", "mc-node", now_ms - 60_000)})
    pane_rows = [
        {"handle": "%1", "session": "mc-1", "cwd": "/w/feat-1/sub", "title": "⠋ Working", "at": "1700001", "cmd": "claude"},
        {"handle": "%2", "session": "mc-fresh", "cwd": "/w/fresh", "title": "zsh", "at": "1700002", "cmd": "zsh"},
        {"handle": "%3", "session": "mc-node", "cwd": "/w/node-cx", "title": "codex", "at": "1700003", "cmd": "node"},
        {"handle": "%4", "session": "mc-gem", "cwd": "/w/gem", "title": "gemini", "at": "1700004", "cmd": "gemini"},
        {"handle": "%5", "session": "mc-oc", "cwd": "/w/oc", "title": "opencode", "at": "1700005", "cmd": "opencode"},
        {"handle": "%6", "session": "mc-pi", "cwd": "/w/pi", "title": "pi", "at": "1700006", "cmd": "pi"},
    ]
    screens = {"%3": ["Working (5s · esc to interrupt)"], "%4": ["▸ 1. Allow", "  2. Deny"]}
    porcelain = "\n".join(f"worktree {p}" for p in ("/w/feat-1", "/w/fresh", "/w/node-cx", "/w/gem", "/w/oc", "/w/pi"))
    with (
        mock.patch.object(ade_basement, "saved", return_value=["/w"]),
        mock.patch.object(ade_basement, "git", return_value=porcelain),
        mock.patch.object(ade_basement, "git_info", side_effect=lambda p: {"repoId": "/w", "repo": "w", "path": p, "branch": "feat", "isMainWorktree": False}),
        mock.patch.object(ade_basement, "panes", return_value=pane_rows),
        mock.patch.object(ade_basement, "screen", side_effect=lambda h: screens.get(h, [])),
        mock.patch.object(ade_basement.external, "by_worktree_safe", return_value={}),
    ):
        by_id = {w["id"]: w for w in ade_basement.worktrees()}
    assert by_id["/w/feat-1"]["agents"][0]["agentType"] == "claude" and by_id["/w/feat-1"]["agents"][0]["state"] == "working"
    assert by_id["/w/feat-1"]["agents"][0]["paneKey"] == "basement:%1"
    assert by_id["/w/fresh"]["agents"][0]["agentType"] == "codex" and by_id["/w/fresh"]["agents"][0]["state"] == "working"
    assert by_id["/w/node-cx"]["agents"][0]["agentType"] == "codex" and by_id["/w/node-cx"]["agents"][0]["state"] == "working"
    assert by_id["/w/gem"]["agents"][0]["agentType"] == "gemini" and by_id["/w/gem"]["agents"][0]["state"] == "waiting"
    assert by_id["/w/oc"]["agents"][0]["agentType"] == "opencode" and by_id["/w/pi"]["agents"][0]["agentType"] == "pi"
    with (
        mock.patch.object(ade_basement, "panes", return_value=pane_rows),
        mock.patch.object(ade_basement, "screen", side_effect=lambda h: screens.get(h, [])),
    ):
        ps = ade_basement.pane_states()
    assert ps["mc-fresh"] == {"state": "working", "title": "⌬ zsh"}
    assert ps["mc-node"] == {"state": "working", "title": "⌬ codex"}
    assert ps["mc-gem"] == {"state": "waiting", "title": "gemini"}


def test_basement_symlinked_cwd_and_failed_create_does_not_mutate_repos(tmp_path, gl_home):
    import last_agent

    real_root = tmp_path / "real"
    real_wt = real_root / "feat-1"
    (real_wt / "sub").mkdir(parents=True)
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root)
    now_ms = int(ade_basement.time.time() * 1000)
    last_agent.note({str(real_wt): ("codex", "mc-sym", now_ms - 60_000)})

    pane = {"handle": "%4", "session": "mc-sym", "cwd": str(link_root / "feat-1" / "sub"),
            "title": "codex", "at": "1700001", "cmd": "node"}
    with (
        mock.patch.object(ade_basement, "saved", return_value=[str(real_root)]),
        mock.patch.object(ade_basement, "git", return_value=f"worktree {real_wt}\n"),
        mock.patch.object(ade_basement, "git_info", side_effect=lambda p: {
            "repoId": str(real_root), "repo": "widgets", "path": p, "branch": "feat-1", "isMainWorktree": False,
        }),
        mock.patch.object(ade_basement, "panes", return_value=[pane]),
        mock.patch.object(ade_basement, "screen", return_value=["Working (3s · esc to interrupt)"]),
        mock.patch.object(ade_basement.external, "by_worktree_safe", return_value={}),
    ):
        wts = ade_basement.worktrees()
        terms = ade_basement.terminals()
    assert wts[0]["agents"][0]["agentType"] == "codex" and wts[0]["agents"][0]["paneKey"] == "basement:%4"
    assert terms == [{
        "handle": "%4", "title": "codex", "lastOutputAt": 1700001000,
        "agentIdentity": "codex", "worktreeId": str(real_wt),
        "tabId": "basement", "leafId": "%4", "session": "mc-sym",
    }]

    # Reverse direction: worktree registered via symlink path while pane cwd is realpath
    assert ade_basement._owner_of(str(real_wt / "sub"), [str(link_root / "feat-1")]) == str(link_root / "feat-1")

    # Failed worktrunk.create must not pollute repos.json
    repos_file = gl_home / "repos.json"
    with (
        mock.patch.object(ade_basement, "REPOS", repos_file),
        mock.patch.object(ade_basement, "main_checkout", return_value="/r/new-repo"),
        mock.patch.object(ade_basement.worktrunk, "create", side_effect=RuntimeError("branch exists")),
        pytest.raises(RuntimeError, match="branch exists"),
    ):
        ade_basement.create("/r/new-repo", "feat-dup")
    assert not repos_file.exists() and ade_basement.saved() == []


def test_greenlight_scan_and_orchestrate_recognize_basement_agent(gl_home):
    import dash
    import decide
    import greenlight
    import last_agent
    import orchestrate
    import ws_state

    now_ms = int(ade_basement.time.time() * 1000)
    last_agent.note({"/w/login-fix": ("codex", "mc-login", now_ms - 60_000)})
    ade_basement.ade_common.set_goal("/w/login-fix", "Fix login")
    ade_basement.ade_common.set_goal("/w/claude-wt", "Fix auth")

    pane_rows = [
        {"handle": "%7", "session": "mc-login", "cwd": "/w/login-fix", "title": "codex", "at": "1700001", "cmd": "node"},
        {"handle": "%8", "session": "mc-claude", "cwd": "/w/claude-wt", "title": "✳ Claude", "at": "1700002", "cmd": "claude"},
    ]
    screens = {
        "%7": "Working (4s · esc to interrupt)\n",
        "%8": "Pick one\n❯ 1. Yes\n  2. No\n",
    }

    def fake_tmux(*args):
        if args[0] == "capture-pane":
            handle = args[args.index("-t") + 1]
            return screens[handle]
        return ""

    calls = []

    def real_basement_orca(*args, **kw):
        calls.append(args)
        if args[:2] == ("agent", "launch"):
            raise AssertionError("must not dispatch a duplicate agent while Basement agent is running")
        return ade_basement.call(*args)

    def fake_git_info(path):
        return {
            "path": path, "repo": "widgets", "repoId": "/w", "projectId": "github:acme/widgets",
            "branch": Path(path).name, "isMainWorktree": False,
        }

    with (
        mock.patch.object(greenlight, "orca", side_effect=real_basement_orca),
        mock.patch.object(greenlight, "gh_open_prs", return_value=[]),
        mock.patch.object(greenlight, "gh", return_value=[]),
        mock.patch.object(greenlight, "upstream_gone", return_value=False),
        mock.patch.object(ade_basement, "saved", return_value=["/w"]),
        mock.patch.object(ade_basement, "git", return_value="worktree /w/login-fix\nworktree /w/claude-wt\n"),
        mock.patch.object(ade_basement, "git_info", side_effect=fake_git_info),
        mock.patch.object(ade_basement, "panes", return_value=pane_rows),
        mock.patch.object(ade_basement, "tmux", side_effect=fake_tmux),
        mock.patch.object(ade_basement.external, "by_worktree_safe", return_value={}),
    ):
        res = greenlight.scan(save=False)
        rows_by_name = {r["name"]: r for r in res["rows"]}
        row = rows_by_name["login-fix"]
        assert row["agent"]["state"] == "working" and row["agent"]["type"] == "codex"
        assert row["agent"]["pane_key"] == "basement:%7"
        assert row["next"]["owner"] == "working"

        # Positive screen-reading path: greenlight.screen matches paneKey via real ade_basement.terminals() and reads terminal
        calls.clear()
        terms = ade_basement.terminals()
        seen = greenlight.screen(row, {row["id"]: terms})
        assert seen is not None and seen["state"] == "working"
        assert any(c[:2] == ("terminal", "read") and "%7" in c for c in calls)

        # dash.waiting_term finds the terminal for both Claude and node-wrapped Codex waiting panes
        wt_claude = dash.waiting_term("claude-wt")
        assert not isinstance(wt_claude, str) and wt_claude[2] == "%8"
        screens["%7"] = "Pick one\n▸ 1. Apply\n  2. Skip\n"
        wt_codex = dash.waiting_term("login-fix")
        assert not isinstance(wt_codex, str) and wt_codex[2] == "%7"

        # Restore working screen for blocking checks
        screens["%7"] = "Working (4s · esc to interrupt)\n"
        card = ws_state.load(row["card"])
        assert orchestrate.plan([row], {row["card"]: card}, judge=False) == []

        # TOCTOU race: stale scan rows still block dispatch via live_worktree
        stale_none = {**row, "next": {"step": "freeform", "owner": "agent", "action": "work toward goal: Fix login"},
                      "agent": {"state": "none", "type": None, "pane_key": None, "last": ""}}
        out = orchestrate.act_one(stale_none, stale_none["name"], {"do": "dispatch", "sig": orchestrate.sig(stale_none)}, {row["card"]: card})
        assert any("agent started or is waiting" in line for line in out), out

        stale_done = {**stale_none, "agent": {"state": "done", "type": "codex", "pane_key": "basement:%7", "last": ""}}
        out2 = orchestrate.act_one(stale_done, stale_done["name"], {"do": "dispatch", "sig": orchestrate.sig(stale_done)}, {row["card"]: card})
        assert any("agent started or is waiting" in line for line in out2), out2
        assert not any(c[:2] == ("agent", "launch") for c in calls)

        # merge_agent_blocks: blocks when working, unblocks when screen shows idle finished prompt
        live = orchestrate.live_worktree(row)
        assert orchestrate.merge_agent_blocks(live, row) is True
        screens["%7"] = "Done.\n› \n"
        with mock.patch.object(decide, "choice", return_value={"choice": "finished", "confidence": 0.92, "probs": {"finished": 0.92}, "provider": "test"}):
            assert orchestrate.merge_agent_blocks(live, row) is False

        # active_agent_blocks(rate_limit_handoff=True): unblocks when screen shows rate limit
        screens["%7"] = "You've hit your usage limit. Try again later.\n"
        assert orchestrate.active_agent_blocks(live, row, rate_limit_handoff=True) is False
