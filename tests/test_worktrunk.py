"""Worktrunk CLI contract used by native workspace creation and cleanup."""
import json
import shutil
import subprocess

import pytest

import worktrunk

LIST_FLAGS = [
    "--config-set", "list.json-schema=2",
    "--config-set", "list.full=false",
    "--config-set", "list.branches=false",
    "--config-set", "list.remotes=false",
    "list", "--format=json",
]


@pytest.fixture(autouse=True)
def no_host_wt(request, tmp_path, monkeypatch):
    """These tests pin the argv to a bare "wt": hide the host's own Worktrunk (~/.local/bin/wt) from bins.find."""
    if request.node.name == "test_real_worktrunk_end_to_end":
        return
    real = shutil.which
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "gl"))
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    monkeypatch.setattr(worktrunk.bins, "DIRS", ())
    monkeypatch.setattr(worktrunk.bins.shutil, "which", lambda n, *a, **k: None if n == "wt" else real(n, *a, **k))

def test_create_resolves_worktrunk_path_from_json_stdout(monkeypatch):
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"branch": "fix/ui", "path": "/custom/ui"}), "")

    monkeypatch.setattr(worktrunk.subprocess, "run", fake)
    assert worktrunk.create("/repo", "fix/ui", "release") == "/custom/ui"
    assert calls == [
        ["wt", "-C", "/repo", "switch", "--create", "--no-cd", "--no-hooks", "--format=json", "fix/ui", "--base", "release"],
    ]


def test_create_falls_back_to_items_when_stdout_lacks_path(monkeypatch):
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    for switch_stdout in ('{"branch":"fix/ui"}', ""):
        calls = []

        def fake(argv, stdout=switch_stdout, **kwargs):
            calls.append(argv)
            if "list" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"schema": 2, "items": [
                    {"branch": "fix/ui", "worktree": {"path": "/custom/ui"}},
                    {"branch": "other"},
                ]}), "")
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        monkeypatch.setattr(worktrunk.subprocess, "run", fake)
        assert worktrunk.create("/repo", "fix/ui", "release") == "/custom/ui"
        assert calls == [
            ["wt", "-C", "/repo", "switch", "--create", "--no-cd", "--no-hooks", "--format=json", "fix/ui", "--base", "release"],
            ["wt", "-C", "/repo", *LIST_FLAGS],
        ]


def test_attach_uses_switch_without_create(monkeypatch):
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"branch": "fix/ui", "path": "/custom/ui"}), "")

    monkeypatch.setattr(worktrunk.subprocess, "run", fake)
    assert worktrunk.attach("/repo", "fix/ui") == "/custom/ui"
    assert calls == [
        ["wt", "-C", "/repo", "switch", "--no-cd", "--no-hooks", "--format=json", "fix/ui"],
    ]
    with pytest.raises(RuntimeError, match="invalid branch or base ref"):
        worktrunk.attach("/repo", "pr:12")


def test_remove_preserves_branch_and_waits(monkeypatch):
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    calls = []
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    worktrunk.remove("/repo", "/worktrees/feature", force=True)
    assert calls == [["wt", "-C", "/repo", "remove", "--foreground", "--no-hooks", "--no-delete-branch", "--force", "--", "/worktrees/feature"]]


def test_missing_binary_and_bad_schema(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("wt")

    monkeypatch.setattr(worktrunk.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="not found on PATH or in.*bins\\.wt in config\\.json"):
        worktrunk.items("/repo")
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""))
    with pytest.raises(RuntimeError, match="schema 2"):
        worktrunk.items("/repo")


def test_worktrunk_env_override_and_error_paths(monkeypatch):
    calls = []
    monkeypatch.setenv("WORKTRUNK_BIN", "/custom/bin/wt")
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    worktrunk.remove("/repo", "/worktrees/feature")
    assert calls == [["/custom/bin/wt", "-C", "/repo", "remove", "--foreground", "--no-hooks", "--no-delete-branch", "--", "/worktrees/feature"]]

    for err, says in ((FileNotFoundError, "Worktrunk not found at /custom/bin/wt"),
                      (PermissionError, r"Worktrunk at /custom/bin/wt can't run \(PermissionError\)")):
        monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, err=err, **kw: (_ for _ in ()).throw(err(argv[0])))
        with pytest.raises(RuntimeError, match=says):
            worktrunk.run("/repo", "list")

    monkeypatch.setenv("WORKTRUNK_BIN", "wt-nightly")
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError(argv[0])))
    with pytest.raises(RuntimeError, match="Worktrunk not found at wt-nightly"):
        worktrunk.run("/repo", "list")
    monkeypatch.setenv("WORKTRUNK_BIN", "/custom/bin/wt")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)))
    with pytest.raises(RuntimeError, match="wt list timed out"):
        worktrunk.run("/repo", "list")
    with pytest.raises(RuntimeError, match="wt timed out"):
        worktrunk.run("/repo")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    with pytest.raises(RuntimeError, match="wt list: boom"):
        worktrunk.run("/repo", "list")
    with pytest.raises(RuntimeError, match="wt list: boom"):
        worktrunk.items("/repo")
    with pytest.raises(RuntimeError, match="wt: boom"):
        worktrunk.run("/repo")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not-json", ""))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        worktrunk.items("/repo")

    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps({"schema": 2, "items": []}), ""))
    with pytest.raises(RuntimeError, match="did not identify one worktree"):
        worktrunk.create("/repo", "feat")

    dup = json.dumps({"schema": 2, "items": [
        {"branch": "feat", "worktree": {"path": "/a"}},
        {"branch": "feat", "worktree": {"path": "/b"}},
    ]})
    monkeypatch.setattr(worktrunk.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, dup, ""))
    with pytest.raises(RuntimeError, match="did not identify one worktree"):
        worktrunk.create("/repo", "feat")


@pytest.mark.parametrize("bad_ref", [
    "",
    True,
    123,
    "-bad",
    "--base-flag",
    "@",
    "^",
    "-",
    "pr:12",
    "mr:5",
    "https://github.com/a/b/pull/1",
    "a..b",
    "bad branch",
    "bad\nbranch",
    "bad\x00branch",
])
def test_create_rejects_invalid_branch_and_base(bad_ref):
    with pytest.raises(RuntimeError, match="invalid branch or base ref"):
        worktrunk.create("/repo", bad_ref)
    with pytest.raises(RuntimeError, match="invalid branch or base ref"):
        worktrunk.create("/repo", "feat", bad_ref)


@pytest.mark.parametrize("bad_repo", ["", None, True, "-repo", "--config"])
def test_run_rejects_invalid_repo(bad_repo):
    with pytest.raises(RuntimeError, match="invalid repo path"):
        worktrunk.run(bad_repo, "list")


@pytest.mark.parametrize("bad_path", ["", None, True, "relative/path", "-dash", "--delete-branch"])
def test_remove_rejects_invalid_path(bad_path):
    with pytest.raises(RuntimeError, match="invalid worktree path"):
        worktrunk.remove("/repo", bad_path)


def test_real_worktrunk_end_to_end(tmp_path, monkeypatch):
    if not shutil.which("wt"):
        pytest.skip("wt binary not installed")
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    (xdg / "worktrunk").mkdir(parents=True)
    # Prove items()/create() override user [list] full = true config cleanly without forge/LLM calls.
    (xdg / "worktrunk" / "config.toml").write_text("[list]\nfull = true\nbranches = true\nremotes = true\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)

    wt_path = worktrunk.create(str(repo), "feat-e2e")
    assert wt_path and (tmp_path / wt_path).is_dir()
    listed = {item["worktree"]["path"]: item.get("branch") for item in worktrunk.items(str(repo))}
    assert listed[wt_path] == "feat-e2e"

    (tmp_path / wt_path / "dirty.txt").write_text("uncommitted")
    with pytest.raises(RuntimeError, match="wt remove:"):
        worktrunk.remove(str(repo), wt_path, force=False)

    worktrunk.remove(str(repo), wt_path, force=True)
    assert not (tmp_path / wt_path).exists()
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "feat-e2e"], capture_output=True, text=True, check=True).stdout
    assert "feat-e2e" in branches

    reattached = worktrunk.attach(str(repo), "feat-e2e")
    assert reattached and (tmp_path / reattached).is_dir()
    worktrunk.remove(str(repo), reattached)
    assert not (tmp_path / reattached).exists()
