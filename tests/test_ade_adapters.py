"""Comprehensive unit tests for ade_cmux, ade_herdr, ade_basement, and ade_t3."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_BOOT_HOME = tempfile.mkdtemp(prefix="gl-ade-tests-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT_HOME)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ade_basement  # noqa: E402
import ade_cmux  # noqa: E402
import ade_herdr  # noqa: E402
import ade_t3  # noqa: E402


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


def test_cmux_sessions_terminals_worktrees_read_send_launch_create_and_call(tmp_path):
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
        if args[:2] == ("tree", "--all"):
            return {"windows": [{"workspaces": [{"id": "ws-1", "panes": [{"surfaces": [
                {"id": "surf-1", "type": "terminal", "title": "codex"},
                {"id": "surf-browser", "type": "browser", "title": "docs"},
            ]}]}]}]}
        if args == ("list-windows",):
            return [{"id": "win-1"}]
        if args[:2] == ("workspace", "list"):
            return {"workspaces": [{"id": "ws-1", "title": "feat-1", "current_directory": "/w/feat-1", "description": "Merge !9", "pinned": True}]}
        if args[0] == "read-screen":
            return {"text": "───\n❯ half draft\n───"}
        if args[:2] == ("new-pane", "--type"):
            return {"surface_id": "surf-new"}
        if args[:2] == ("workspace", "create"):
            return {"workspace_id": "ws-new"}
        return {}

    sent_calls = []
    def tracking_cmux(*args, js=True):
        sent_calls.append((args, js))
        return fake_cmux(*args, js=js)

    with (
        mock.patch.object(ade_cmux, "HOOKS", hooks_dir),
        mock.patch.object(ade_cmux, "cmux", side_effect=tracking_cmux),
        mock.patch.object(ade_cmux, "git_info", side_effect=_fake_git_info),
        mock.patch.object(ade_cmux.time, "sleep"),
        mock.patch.object(ade_cmux, "worktree_add", return_value="/w/new-ws") as m_add,
        mock.patch.object(ade_cmux, "worktree_remove") as m_rm,
    ):
        sess = ade_cmux.sessions()
        assert list(sess.keys()) == ["surf-1"]

        terms = ade_cmux.call("terminal", "list")["terminals"]
        assert len(terms) == 1 and terms[0]["handle"] == "surf-1" and terms[0]["agentIdentity"] == "codex"
        assert terms[0]["lastOutputAt"] == 1700000000

        wts = ade_cmux.call("worktree", "ps")["worktrees"]
        assert len(wts) == 1 and wts[0]["comment"] == "Merge !9"
        assert wts[0]["agents"][0]["state"] == "waiting"

        rd = ade_cmux.call("terminal", "read", "--terminal", "surf-1", "--limit", "50")
        assert rd["terminal"]["draft"] == "half draft"

        ade_cmux.call("terminal", "send", "--terminal", "surf-1", "--text", "line1\nline2", "--enter")
        ade_cmux.call("terminal", "send", "--terminal", "surf-1", "--text", "1")
        assert any(c[0][:2] == ("rpc", "terminal.paste") for c in sent_calls)
        assert any(c[0][0] == "send" and c[1] is False for c in sent_calls)

        launched = ade_cmux.call("agent", "launch", "--worktree", "id:ws-1", "--agent", "claude", "--prompt", "fix")
        assert launched["terminal"]["handle"] == "surf-new"

        ade_cmux.call("worktree", "set", "--worktree", "id:ws-1", "--comment", "New goal")
        ade_cmux.call("worktree", "set", "--worktree", "id:ws-1", "--comment", "")
        assert any("--action" in c[0] and "set-description" in c[0] for c in sent_calls)
        assert any("--action" in c[0] and "clear-description" in c[0] for c in sent_calls)

        created = ade_cmux.call("worktree", "create", "--repo", "id:/r/widgets", "--name", "new-ws", "--comment", "Ship", "--agent", "claude", "--prompt", "go")
        assert created == {"worktree": {"id": "ws-new", "path": "/w/new-ws"}}
        m_add.assert_called_once()

        removed = ade_cmux.call("worktree", "rm", "--worktree", "id:ws-1")
        assert removed == {"removed": "ws-1"}
        m_rm.assert_called_once()

        repos = ade_cmux.call("repo", "list")
        assert repos["repos"][0]["id"] == "/r/widgets"

        with pytest.raises(RuntimeError, match="no cmux equivalent"):
            ade_cmux.call("bogus", "op")


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


def test_herdr_full_dispatch_launch_brief_retry_and_rm(tmp_path):
    calls = []
    start_attempts = {"n": 0}

    def fake_herdr(*args, js=True):
        calls.append((args, js))
        if args[:2] == ("agent", "list"):
            return {"agents": [{"pane_id": "p:1", "workspace_id": "w1", "agent": "claude", "agent_status": "working"}]}
        if args[:2] == ("pane", "list"):
            return {"panes": [{"pane_id": "p:1", "workspace_id": "w1", "cwd": "/w/feat-1"}, {"pane_id": "p:2", "workspace_id": "w2", "cwd": "/w/widgets"}]}
        if args[:2] == ("workspace", "list"):
            return {"workspaces": [
                {"workspace_id": "w1", "label": "feat-1", "worktree": {"checkout_path": "/w/feat-1"}},
                {"workspace_id": "w2", "label": "widgets", "worktree": None},
            ]}
        if args[:2] == ("pane", "read"):
            return "\x1b[1m❯\x1b[0m\x1b[2mghost\x1b[0m\n\x1b[1m❯\x1b[0m real draft"
        if args[:2] == ("pane", "split"):
            return {"pane": {"pane_id": "p:9"}}
        if args[:2] == ("agent", "start"):
            start_attempts["n"] += 1
            if start_attempts["n"] == 1:
                raise RuntimeError("herdr agent start: agent_pane_busy")
            if start_attempts["n"] == 3:
                raise RuntimeError("herdr agent start: agent_not_ready")
            return {}
        if args[:2] == ("worktree", "create"):
            return {"workspace": {"workspace_id": "w3"}, "worktree": {"path": "/w/w3"}, "root_pane": {"pane_id": "p:3"}}
        return {}

    goals_file = tmp_path / "goals.json"
    briefs_dir = tmp_path / "briefs"
    with (
        mock.patch.object(ade_herdr, "herdr", side_effect=fake_herdr),
        mock.patch.object(ade_herdr, "git_info", side_effect=_fake_git_info),
        mock.patch.object(ade_herdr, "BRIEFS", briefs_dir),
        mock.patch("ade_common.GOALS", goals_file),
        mock.patch.object(ade_herdr.time, "sleep"),
    ):
        ade_herdr.call("worktree", "set", "--worktree", "id:w1", "--comment", "Goal 1")
        wts = ade_herdr.call("worktree", "list")["worktrees"]
        assert wts[0]["comment"] == "Goal 1" and wts[0]["agents"][0]["state"] == "working"

        terms = ade_herdr.call("terminal", "list")["terminals"]
        assert terms[0]["handle"] == "p:1" and terms[0]["lastOutputAt"] is not None
        assert terms[1]["lastOutputAt"] is None

        rd = ade_herdr.call("terminal", "read", "--terminal", "p:1", "--limit", "40")
        assert rd["terminal"]["draft"] == "real draft"

        ade_herdr.call("terminal", "send", "--terminal", "p:1", "--text", "hi", "--enter")
        ade_herdr.call("terminal", "send", "--terminal", "p:2", "--text", "ls", "--enter")
        ade_herdr.call("terminal", "send", "--terminal", "p:1", "--text", "1")
        assert any(c[0][:2] == ("agent", "prompt") for c in calls)
        assert any(c[0][:2] == ("pane", "run") for c in calls)
        assert any(c[0][:2] == ("pane", "send-text") for c in calls)

        # Long multi-line brief creates 0600 brief file and retries past agent_pane_busy
        launched = ade_herdr.call("agent", "launch", "--worktree", "id:w1", "--agent", "claude", "--prompt", "line1\nline2")
        assert launched["terminal"]["handle"] == "p:9"
        briefs = list(briefs_dir.glob("*.md"))
        assert len(briefs) == 1 and (briefs[0].stat().st_mode & 0o777) == 0o600

        # agent_not_ready does not raise
        created = ade_herdr.call("worktree", "create", "--repo", "id:/r/widgets", "--name", "w3", "--base-branch", "main", "--comment", "C", "--agent", "claude", "--prompt", "short")
        assert created == {"worktree": {"id": "w3", "path": "/w/w3"}}

        # rm main worktree uses workspace close; non-main uses worktree remove
        assert ade_herdr.call("worktree", "rm", "--worktree", "id:w1") == {"removed": "w1"}
        assert ade_herdr.call("worktree", "rm", "--worktree", "id:w2") == {"removed": "w2"}
        assert any(c[0][:2] == ("worktree", "remove") for c in calls)
        assert any(c[0][:2] == ("workspace", "close") for c in calls)

        assert ade_herdr.call("repo", "list")["repos"][0]["id"] == "/r/widgets"
        with pytest.raises(RuntimeError, match="no herdr equivalent"):
            ade_herdr.call("unknown", "verb")


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
        mock.patch.object(ade_basement, "worktree_add", return_value="/w/new-wt"),
        mock.patch.object(ade_basement, "worktree_remove") as m_rm,
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

        removed = ade_basement.call("worktree", "rm", "--worktree", "/w/feat-1", "--force")
        assert removed == {"removed": "/w/feat-1"}
        m_rm.assert_called_once()

        assert ade_basement.call("repo", "list")["repos"][0]["id"] == "/r/widgets"
        with pytest.raises(RuntimeError, match="no Basement equivalent"):
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


def test_basement_tmux_no_server_vs_error():
    no_srv = subprocess.CompletedProcess(["tmux"], 1, stdout="", stderr="no server running on /tmp/tmux")
    with mock.patch.object(ade_basement.subprocess, "run", return_value=no_srv):
        assert ade_basement.tmux("list-panes") == ""
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

def test_t3_projects_sqlite_and_full_dispatch(tmp_path):
    ud = tmp_path / "userdata"
    ud.mkdir()
    db_path = ud / "statev2.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("create table projection_projects (project_id text, title text, workspace_root text, deleted_at text)")
    conn.execute("insert into projection_projects values ('p1', 'feat-1', '/w/feat-1', null)")
    conn.execute("insert into projection_projects values ('p-del', 'old', '/w/old', '2026-01-01')")
    conn.commit()
    conn.close()
    (ud / "model-manifest.json").write_text(json.dumps({"manifest": {"currentModels": {"claudeAgent": ["claude-sonnet"]}}}))

    t3_calls = []
    def fake_t3(*args, stdin=None):
        t3_calls.append((args, stdin))
        if args[:2] == ("thread", "list"):
            return json.dumps([
                {"threadId": "th-1", "status": "running", "updatedAt": "2026-10-27T20:55:40.176Z", "providerInstanceId": "claudeAgent"},
            ])
        if args[:2] == ("thread", "show"):
            return json.dumps({
                "thread": {"threadId": "th-1", "status": "waiting", "updatedAt": "2026-10-27T20:55:40.176Z", "providerInstanceId": "claudeAgent"},
                "messages": [{"role": "user", "text": "check ci"}, {"role": "assistant", "text": "CI passed"}],
            })
        if args[:2] == ("thread", "start"):
            return json.dumps({"threadId": "th-new"})
        if args[:2] == ("project", "add"):
            return "Added project p2 (new-wt) at /w/new-wt."
        return ""

    goals_file = tmp_path / "goals.json"
    with (
        mock.patch.object(ade_t3, "HOME", tmp_path),
        mock.patch.object(ade_t3, "t3", side_effect=fake_t3),
        mock.patch.object(ade_t3, "git_info", side_effect=_fake_git_info),
        mock.patch("ade_common.GOALS", goals_file),
        mock.patch.object(ade_t3, "worktree_add", return_value="/w/new-wt"),
        mock.patch.object(ade_t3, "worktree_remove") as m_rm,
    ):
        assert ade_t3.projects() == [("p1", "feat-1", "/w/feat-1")]

        ade_t3.call("worktree", "set", "--worktree", "id:p1", "--comment", "Merge !3")
        wts = ade_t3.call("worktree", "ps")["worktrees"]
        assert len(wts) == 1 and wts[0]["comment"] == "Merge !3"
        assert wts[0]["agents"][0]["state"] == "waiting"
        assert wts[0]["agents"][0]["lastAssistantMessage"] == "CI passed"
        assert wts[0]["agents"][0]["agentType"] == "claude"

        terms = ade_t3.call("terminal", "list")["terminals"]
        assert len(terms) == 1 and terms[0]["handle"] == "th-1" and terms[0]["agentIdentity"] == "claude"

        rd = ade_t3.call("terminal", "read", "--terminal", "th-1", "--limit", "10")
        assert rd["terminal"]["tail"] == ["❯ check ci", "⏺ CI passed"]

        ade_t3.call("terminal", "send", "--terminal", "th-1", "--text", "continue")
        assert any(c[0][:2] == ("thread", "send") and c[1] == "continue" for c in t3_calls)

        launched = ade_t3.call("agent", "launch", "--worktree", "id:p1", "--agent", "claude", "--prompt", "ship")
        assert launched == {"terminal": {"handle": "th-new"}}

        created = ade_t3.call("worktree", "create", "--repo", "id:/r/widgets", "--name", "new-wt", "--comment", "Goal", "--agent", "claude", "--prompt", "hi")
        assert created == {"worktree": {"id": "p2", "path": "/w/new-wt"}}

        removed = ade_t3.call("worktree", "rm", "--worktree", "id:p1")
        assert removed == {"removed": "p1"}
        m_rm.assert_called_once()
        assert any(c[0][:3] == ("project", "remove", "--force") for c in t3_calls)

        assert ade_t3.call("repo", "list")["repos"][0]["id"] == "/r/widgets"
        with pytest.raises(RuntimeError, match="no t3 equivalent"):
            ade_t3.call("bad", "verb")


def test_t3_subprocess_wrapper_error():
    ok = subprocess.CompletedProcess(["t3"], 0, stdout="ok", stderr="")
    with mock.patch.object(ade_t3.subprocess, "run", return_value=ok):
        assert ade_t3.t3("thread", "list") == "ok"
    bad = subprocess.CompletedProcess(["t3"], 1, stdout="", stderr="server down")
    with mock.patch.object(ade_t3.subprocess, "run", return_value=bad), pytest.raises(RuntimeError, match="server down"):
        ade_t3.t3("thread", "list")
