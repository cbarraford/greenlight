"""Tests for the non-Orca ADE backends' parsing (remote -> project, orca flags, drafts, T3/basement state)."""
from unittest import mock

import ade_basement
import ade_common
import ade_t3
import dash


def test_project_id_and_flags():
    assert ade_common.project_id("git" + "@gitlab.com:acme/widgets.git") == "git:gitlab.com/acme/widgets"
    assert ade_common.project_id("https://gitlab.com/acme/ops/relay") == "git:gitlab.com/acme/ops/relay"
    assert ade_common.flags(["--terminal", "t1", "--text", "hi", "--enter", "--wait-submit", "10"]) == {
        "terminal": "t1",
        "text": "hi",
        "enter": True,
        "wait-submit": "10",
    }


def test_draft_and_ansi_plain():
    assert ade_common.draft(["───", "❯ ", "───"]) == ""
    assert ade_common.draft(["───", "❯ half typed", "───"]) == "half typed"  # never typed over
    assert ade_common.draft(["Which one?", "❯ 1. Keep it", "  2. Port it"]) == ""  # a menu, not a draft

    # herdr reads ANSI: Claude's dim prompt suggestion isn't a draft, typed text is
    ghost = "\x1b[1m❯\x1b[0m\xa0\x1b[0m\x1b[2mlist both words\x1b[0m\r"
    assert ade_common.plain(ghost) == "❯\xa0list both words"
    assert ade_common.draft([ade_common.plain(ghost, ghost=False)]) == ""
    assert ade_common.draft([ade_common.plain("\x1b[1m❯\x1b[0m half \x1b[32mtyped\x1b[0m", ghost=False)]) == "half typed"


def test_goals_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(ade_common, "GOALS", tmp_path / "goals.json")
    ade_common.set_goal("/w/a", "Merge !1")
    ade_common.set_goal("/w/b", "Review !2")
    ade_common.set_goal("/w/a", "")  # clearing removes it
    assert ade_common.goals() == {"/w/b": "Review !2"}


def test_t3_state_and_timestamp():
    assert ade_t3.state({"status": "running"}) == "working"
    assert ade_t3.state({"status": "running", "pendingRequestCount": 1}) == "waiting"
    assert ade_t3.state({"status": "completed"}) == "done"
    assert ade_t3.ms("2026-10-27T20:55:40.176Z") == 1793134540176


def test_shim_repo_list_for_dash():
    # shims answer `repo list` in Orca's shape, so dash.py's New task can list their repos
    worktrees = [
        {"repoId": "/r/widgets", "projectId": "git:gitlab.com/acme/widgets"},
        {"repoId": "/r/widgets", "projectId": "git:gitlab.com/acme/widgets"},
        {"repoId": "/r/local", "projectId": None},
        {"repoId": ""},
    ]
    with mock.patch.object(dash.greenlight, "orca", return_value=ade_common.repo_list(worktrees)):
        assert dash.repos() == [{"id": "/r/widgets", "name": "widgets", "host": "gitlab", "path": "acme/widgets"}], dash.repos()


def test_basement_state():
    assert ade_basement.state("⠙ Running tests") == "working"
    assert ade_basement.state("bash") is None
    assert ade_basement.state("✳ Claude", ["Pick one", "❯ 1. Yes", "  2. No"]) == "waiting"
    assert ade_basement.state("✳ Claude", ["❯ "]) == "done"
    # Codex and agy panes are detected by tmux prefix glyph or pane_current_command
    assert ade_basement.agent_type("⌬ review") == "codex" and ade_basement.agent_type("◈ fix") == "agy"
    assert ade_basement.agent_type("zsh", "codex") == "codex" and ade_basement.agent_type("zsh", "agy") == "agy"
    assert ade_basement.agent_type("zsh", "zsh") is None
    assert ade_basement.state("⌬ Codex", ["Working (12s · esc to interrupt)"]) == "working"
    assert ade_basement.state("⌬ Codex", ["› 1. Apply", "  2. Cancel"]) == "waiting"
    assert ade_basement.state("⌬ Codex", ["› "]) == "done"
    assert ade_basement.state("◈ Antigravity", ["Thinking (esc to cancel, 3s)"]) == "working"
    assert ade_basement.state("◈ Antigravity", ["▸ 1. Yes", "  2. No"]) == "waiting"
    assert ade_basement.state("◈ Antigravity", ["> "]) == "done"
    assert ade_common.DEAD_RE.search("Run agy login to continue")
    assert ade_common.BUSY_RE.search("Working (esc to cancel, 2s)")
    assert not ade_common.BUSY_RE.search("Esc to cancel")
