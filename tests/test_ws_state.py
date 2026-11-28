"""Tests for scripts/ws_state.py: local_work, resolve_card_key, observe edge transitions, reset_attempts, current_key, and CLI."""
import json
import subprocess
import sys
from unittest import mock

import pytest

import ws_state


def test_local_work_strips_git_outputs_and_handles_errors(tmp_path):
    # Empty path -> {}
    assert ws_state.local_work("") == {}

    # BUG-CORE-1: outputs with trailing newlines/whitespace must still parse cleanly
    def fake_git(_path, *args):
        if args == ("status", "--porcelain"):
            return " M a.py\n?? b.py\n"
        if args == ("rev-parse", "HEAD"):
            return "sha-head\n"
        if args == ("rev-list", "--count", "sha-mr..HEAD"):
            return "  3 \n"
        return ""

    with mock.patch.object(ws_state, "git", side_effect=fake_git):
        assert ws_state.local_work(str(tmp_path), mr_sha="sha-mr") == {"dirty": 2, "ahead": 3}

    # Same HEAD as mr_sha (with trailing newline) -> clean, not ahead
    def fake_git_clean(_path, *args):
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "sha-mr\n"
        return ""

    with mock.patch.object(ws_state, "git", side_effect=fake_git_clean):
        assert ws_state.local_work(str(tmp_path), mr_sha="sha-mr") == {}

    # mr_sha missing locally -> falls back to @{u}..HEAD (and defaults to 1 if no upstream)
    def fake_git_fallback(_path, *args):
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "sha-local\n"
        if args == ("rev-list", "--count", "sha-mr..HEAD"):
            return None
        if args == ("rev-list", "--count", "@{u}..HEAD"):
            return "2\n"
        return ""

    with mock.patch.object(ws_state, "git", side_effect=fake_git_fallback):
        assert ws_state.local_work(str(tmp_path), mr_sha="sha-mr") == {"dirty": 0, "ahead": 2}

    def fake_git_no_upstream(_path, *args):
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "sha-local\n"
        return None

    with mock.patch.object(ws_state, "git", side_effect=fake_git_no_upstream):
        assert ws_state.local_work(str(tmp_path), mr_sha="sha-mr") == {"dirty": 0, "ahead": 1}

    # No mr_sha -> uses @{u}..HEAD; timeout/None returns {}
    with mock.patch.object(ws_state, "git", side_effect=lambda _p, *a: " 4\n" if "@{u}..HEAD" in a else ""):
        assert ws_state.local_work(str(tmp_path)) == {"dirty": 0, "ahead": 4}
    with mock.patch.object(ws_state, "git", return_value=None):
        assert ws_state.local_work(str(tmp_path)) == {}


def test_resolve_card_key_and_current_key(gl_home):
    rows = [
        {"id": "id-1", "name": "feat-a", "forge": "github", "project": "octo/app", "mr": {"iid": 11}},
        {"id": "id-2", "name": "feat-b", "forge": "gitlab", "project": "acme/widgets", "mr": {"iid": 42}},
        {"id": "id-3", "name": "dup", "forge": "github", "project": "octo/app", "mr": {"iid": 42}},
        {"id": "id-4", "name": "dup", "forge": "github", "project": "octo/other", "mr": {"iid": 42}},
    ]
    (gl_home / "last.json").write_text(json.dumps({"rows": rows}))

    # Resolve by name, #N, !N, proj#N, exact card key
    k1, r1 = ws_state.resolve_card_key("feat-a")
    assert k1 == "feat-a" and r1["id"] == "id-1"
    k2, r2 = ws_state.resolve_card_key("!42")
    assert k2 == "feat-b" and r2["id"] == "id-2"
    k3, r3 = ws_state.resolve_card_key("#11")
    assert k3 == "feat-a" and r3["id"] == "id-1"
    k4, r4 = ws_state.resolve_card_key("octo/other#42")
    assert r4["id"] == "id-4" and k4.startswith("dup")

    # Ambiguous MR ref and ambiguous workspace name (when bare card belongs to a different id) raise ValueError
    with pytest.raises(ValueError, match="ambiguous MR"):
        ws_state.resolve_card_key("#42")
    ws_state.save(ws_state.blank("dup") | {"id": "id-old"})
    with pytest.raises(ValueError, match="ambiguous workspace"):
        ws_state.resolve_card_key("dup")
    with pytest.raises(LookupError, match="no scanned workspace"):
        ws_state.resolve_card_key("missing-ws")

    # BUG-CORE-11: current_key resolves via full rows with migrate=False (no .owner-* files or migration marker written)
    assert ws_state.current_key("feat-a") == "feat-a"
    assert not ws_state.owner_file("id-1").exists() and not (ws_state.DIR / ".owners-migrated").exists()
    with pytest.raises(SystemExit, match="ambiguous"):
        ws_state.current_key("dup")
    assert ws_state.current_key("nonexistent") == "nonexistent"


def test_observe_transitions_and_reset_attempts(gl_home):
    r = {
        "id": "w-obs", "name": "obs-ws", "path": "/nonexistent", "branch": "obs-ws",
        "project": "octo/app", "forge": "github", "goal": "Merge #5: ship",
        "agent": {"state": "done", "type": "claude", "last": "Fixed unit tests"},
        "next": {"step": "ci_green", "owner": "agent", "action": "Fix CI"},
        "life": {"at": "2.1"},
        "mr": {"iid": 5, "ref": "#5", "title": "ship", "state": "opened", "role": "author",
               "sha": "sha1", "target_branch": "main", "pipeline": "failed",
               "failed_jobs": [{"name": "lint", "stage": "test"}]},
    }
    c, moved = ws_state.observe(r, "Merge #5 — done when #5 merges", recs=[])
    assert moved == ("", "2.1 ci_green/agent")
    assert c["open_risks"] == ["CI failed: lint"]

    # Record stage attempts, check stage_attempts(), then reset_attempts() for one step and globally
    ws_state.record_attempt("obs-ws", step="ci_green", sig="ci_green@sha1", agent="claude", sha="sha1", mr_iid=5)
    c = ws_state.load("obs-ws")
    att = ws_state.stage_attempts(c, r)
    assert att and att["count"] == 1 and att["stuck"] is True
    c["dispatches"].append({"ts": 1, "agent": "codex", "step": "threads", "sig": "threads@sha1", "pending": False})
    ws_state.save(c)
    c = ws_state.reset_attempts("obs-ws", step="ci_green")
    assert all(d["step"] != "ci_green" for d in c["dispatches"])
    assert any(d["step"] == "threads" for d in c["dispatches"])
    c = ws_state.reset_attempts("obs-ws")
    assert c["dispatches"] == [] and c["forks"] == {}

    # Stage transition records transition history
    r2 = {**r, "next": {"step": "merge", "owner": "greenlight", "action": "merge"}, "life": {"at": "5.1"}}
    c2, moved2 = ws_state.observe(r2, recs=[])
    assert moved2 == ("2.1 ci_green/agent", "5.1 merge/greenlight")
    assert len(c2["transitions"]) == 2


def test_ws_state_cli_show_and_steward(gl_home, monkeypatch, capsys):
    rows = [{
        "id": "w-cli", "name": "cli-ws", "path": "/nonexistent", "branch": "cli-ws",
        "project": "octo/app", "forge": "github", "goal": "Merge #8: demo",
        "agent": {"state": "none", "type": None, "last": ""},
        "next": {"step": "ci_green", "owner": "agent", "action": "Fix CI"},
        "life": {"at": "2.1"},
        "mr": {"iid": 8, "ref": "#8", "title": "demo", "state": "opened", "role": "author", "sha": "sha8", "target_branch": "main"},
    }]
    (gl_home / "last.json").write_text(json.dumps({"rows": rows}))
    ws_state.observe(rows[0], "Merge #8", recs=[])

    def run_cli(*args):
        monkeypatch.setattr(sys, "argv", ["ws_state.py", *args])
        ws_state.main()
        return capsys.readouterr().out

    shown = json.loads(run_cli("show", "cli-ws", "--json"))
    assert shown["ws"] == "cli-ws" and shown["goal_contract"] == "Merge #8"

    st_on = json.loads(run_cli("steward", "#8", "on", "--json"))
    assert st_on["steward"] is True and st_on["head"] == "sha8" and st_on["target"] == "main"

    st_log = json.loads(run_cli("steward", "cli-ws", "log", "--json"))
    assert st_log["steward"] is True and st_log["activity"]

    st_off = json.loads(run_cli("steward", "cli-ws", "off", "--json"))
    assert st_off["steward"] is False

    # Git helper timeout returns None cleanly
    with mock.patch.object(ws_state.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 5)):
        assert ws_state.git("/tmp", "status") is None
