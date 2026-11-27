"""Worktrunk CLI contract used by native workspace creation and cleanup."""
import json
import subprocess

import pytest

import worktrunk


def test_create_resolves_worktrunk_path(monkeypatch):
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
