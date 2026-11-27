"""Worktrunk CLI contract used by native workspace creation and cleanup."""
import json
import subprocess

import pytest

import worktrunk


def test_create_resolves_worktrunk_path(monkeypatch):
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"schema": 2, "items": [
                {"branch": "fix/ui", "worktree": {"path": "/custom/ui"}},
                {"branch": "other"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, '{"branch":"fix/ui"}', "")

    monkeypatch.setattr(worktrunk.subprocess, "run", fake)
    assert worktrunk.create("/repo", "fix/ui", "release") == "/custom/ui"
    assert calls == [
        ["wt", "-C", "/repo", "-y", "switch", "--create", "--no-cd", "--no-hooks", "--format=json", "fix/ui", "--base", "release"],
        ["wt", "-C", "/repo", "-y", "--config-set", "list.json-schema=2", "list", "--format=json"],
    ]


def test_remove_preserves_branch_and_waits(monkeypatch):
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    calls = []
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    worktrunk.remove("/repo", "/worktrees/feature", force=True)
    assert calls == [["wt", "-C", "/repo", "-y", "remove", "--foreground", "--no-hooks", "--no-delete-branch", "--force", "/worktrees/feature"]]


def test_missing_binary_and_bad_schema(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("wt")

    monkeypatch.setattr(worktrunk.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="Worktrunk"):
        worktrunk.items("/repo")
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""))
    with pytest.raises(RuntimeError, match="schema 2"):
        worktrunk.items("/repo")


def test_worktrunk_env_override_and_error_paths(monkeypatch):
    calls = []
    monkeypatch.setenv("WORKTRUNK_BIN", "/custom/bin/wt")
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    worktrunk.remove("/repo", "/worktrees/feature")
    assert calls == [["/custom/bin/wt", "-C", "/repo", "-y", "remove", "--foreground", "--no-hooks", "--no-delete-branch", "/worktrees/feature"]]

    with pytest.raises(RuntimeError, match="invalid branch or base ref"):
        worktrunk.create("/repo", "-bad")
    with pytest.raises(RuntimeError, match="invalid branch or base ref"):
        worktrunk.create("/repo", "feat", "--base-flag")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)))
    with pytest.raises(RuntimeError, match="timed out"):
        worktrunk.run("/repo", "list")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    with pytest.raises(RuntimeError, match="wt list: boom"):
        worktrunk.run("/repo", "list")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not-json", ""))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        worktrunk.items("/repo")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps({"schema": 2, "items": []}), ""))
    with pytest.raises(RuntimeError, match="did not identify one worktree"):
        worktrunk.create("/repo", "feat")
