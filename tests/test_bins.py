"""bins: finding CLIs when the backend's PATH lacks ~/.local/bin (ssh, launchd, systemd)."""
import errno
import json
import os
import subprocess

import pytest

import ade_basement
import ade_common
import audit
import bins
import cleanup
import external
import greenlight
import orchestrate
import pane_log


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A HOME with nothing installed, a PATH without ~/.local/bin, and no config.json."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path / "gl"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(bins, "DIRS", ("~/.local/bin", "~/.cargo/bin"))
    monkeypatch.delenv("WORKTRUNK_BIN", raising=False)
    return home


def test_find_falls_back_to_well_known_dirs(bare):
    assert bins.find("wt") is None
    cargo = _exe(bare / ".cargo" / "bin" / "wt")
    assert bins.find("wt") == cargo
    local = _exe(bare / ".local" / "bin" / "wt")
    assert bins.find("wt") == local  # DIRS order: ~/.local/bin first
    (bare / ".local" / "bin" / "tmux").write_text("not executable")
    assert bins.find("tmux") is None
    (bare / ".local" / "bin" / "wt-dir").mkdir()
    assert not bins.is_executable(str(bare / ".local" / "bin" / "wt-dir"))
    assert bins.find("wt-dir") is None
    assert "~/" in bins.searched()


def test_find_prefers_path_then_overrides(bare, tmp_path, monkeypatch):
    _exe(bare / ".local" / "bin" / "wt")
    on_path = _exe(tmp_path / "pathbin" / "wt")
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    assert bins.find("wt") == on_path

    (tmp_path / "gl").mkdir()
    (tmp_path / "gl" / "config.json").write_text(json.dumps({"bins": {"wt": " ~/tools/wt "}}))
    assert bins.find("wt") == str(bare / "tools" / "wt")  # trimmed + ~-expanded; returned even before it exists
    monkeypatch.setenv("WORKTRUNK_BIN", "  ")  # whitespace-only env does not shadow config
    assert bins.find("wt", env="WORKTRUNK_BIN") == str(bare / "tools" / "wt")
    monkeypatch.setenv("WORKTRUNK_BIN", "~/env/wt")
    assert bins.find("wt", env="WORKTRUNK_BIN") == str(bare / "env" / "wt")
    monkeypatch.setenv("WORKTRUNK_BIN", "/custom/wt")
    assert bins.find("wt", env="WORKTRUNK_BIN") == "/custom/wt"
    assert bins.find("wt") == str(bare / "tools" / "wt")  # env only when asked for

    (tmp_path / "gl" / "config.json").write_text(json.dumps({"bins": {"gh": "gh-beta"}}))
    assert bins.find("gh") == "gh-beta"  # missing bare-name override is still returned so errors name it
    beta = _exe(bare / ".local" / "bin" / "gh-beta")
    assert bins.find("gh") == beta  # a bare name is looked up the same way


@pytest.mark.parametrize("cfg", [
    '{"bins": "wt"}',
    '{"bins": {"wt": 7}}',
    '{"bins": {"wt": " "}}',
    '{"bins": {"wt": "./wt"}}',
    '{"bins": {"wt": "bin/wt"}}',
    '{"bins": {"wt": "/opt/wt\\u0000"}}',
    "[]",
    "not json",
])
def test_bad_config_is_ignored(bare, tmp_path, cfg):
    (tmp_path / "gl").mkdir()
    (tmp_path / "gl" / "config.json").write_text(cfg)
    assert bins.configured("wt") is None
    assert bins.find("wt") is None


def test_ensure_path_appends_existing_dirs_once(bare):
    env = {"PATH": "/usr/bin:/bin"}
    assert bins.ensure_path(env) == "/usr/bin:/bin"  # neither folder exists yet
    (bare / ".local" / "bin").mkdir(parents=True)
    assert bins.ensure_path(env) == f"/usr/bin:/bin:{bare}/.local/bin"
    assert bins.ensure_path(env) == f"/usr/bin:/bin:{bare}/.local/bin"  # idempotent
    # trailing slash in PATH still deduplicates
    env_slash = {"PATH": f"/usr/bin:{bare}/.local/bin/"}
    assert bins.ensure_path(env_slash) == f"/usr/bin:{bare}/.local/bin/"
    assert bins.ensure_path({}).startswith("/usr/bin:/bin:")  # no PATH at all: the system default first


def test_ensure_path_defaults_to_os_environ(bare):
    (bare / ".cargo" / "bin").mkdir(parents=True)
    bins.ensure_path()
    assert os.environ["PATH"].split(os.pathsep)[-1] == str(bare / ".cargo" / "bin")


def test_argv_swaps_in_a_configured_program(bare, tmp_path):
    assert bins.argv(["glab", "mr", "list"]) == ["glab", "mr", "list"]  # nothing set: PATH's
    (tmp_path / "gl").mkdir()
    (tmp_path / "gl" / "config.json").write_text(json.dumps({"bins": {"glab": "~/opt/glab", "gh": "gh-beta"}}))
    assert bins.argv(["glab", "mr", "list"]) == [str(bare / "opt" / "glab"), "mr", "list"]
    beta = _exe(bare / ".local" / "bin" / "gh-beta")
    assert bins.argv(["gh", "pr", "list"]) == [beta, "pr", "list"]  # bare-name override resolved via DIRS
    assert bins.argv(["/usr/bin/glab", "mr"]) == ["/usr/bin/glab", "mr"]  # an explicit path is left alone
    assert bins.argv([]) == []


def test_subprocesses_honour_bins_overrides(gl_home, bare, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(gl_home))
    monkeypatch.setattr(greenlight.diag, "_ver", "test")
    (gl_home / "config.json").write_text(json.dumps({"bins": {
        "glab": "/opt/x/glab",
        "gh": "/opt/x/gh",
        "tmux": "/opt/x/tmux",
        "git": "/opt/x/git",
    }}))
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv[0])
        is_text = kw.get("text") or kw.get("encoding")
        return subprocess.CompletedProcess(argv, 0, "ok" if is_text else b"ok", "" if is_text else b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert greenlight.run(["glab", "mr", "list"]) == "ok"
    ade_basement.tmux("list-sessions")
    ade_basement.send("%1", "hello", True)
    external._tmux_roots()
    assert pane_log._tmux_bin() == "/opt/x/tmux"
    assert orchestrate.run_cmd(["gh", "pr", "merge", "1"]) == (True, "ok")
    cleanup.run(["glab", "api", "user"])
    audit._run(["gh", "pr", "view", "1"])
    ade_common.git("/tmp", "status")
    assert seen[0] == "/opt/x/glab"
    assert "/opt/x/tmux" in seen and "/opt/x/gh" in seen and "/opt/x/git" in seen
    assert not ({"glab", "gh", "tmux", "git"} & set(seen))


def test_greenlight_and_tmux_override_error_paths(gl_home, bare, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(gl_home))
    monkeypatch.setattr(greenlight.diag, "_ver", "test")
    (gl_home / "config.json").write_text(json.dumps({"bins": {"glab": "/opt/bad/glab", "tmux": "/opt/bad/tmux"}}))

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "Permission denied")))
    with pytest.raises(RuntimeError, match="glab: Permission denied") as exc_info:
        greenlight.run(["glab", "mr", "list"])
    assert getattr(exc_info.value, "_gl_tool", None) == "glab"
    assert getattr(exc_info.value, "_gl_class", None) == "permission"

    with pytest.raises(RuntimeError, match=r"tmux at /opt/bad/tmux can't run \(PermissionError\)"):
        ade_basement.tmux("list-panes")
    with pytest.raises(RuntimeError, match=r"tmux at /opt/bad/tmux can't run \(PermissionError\)"):
        ade_basement.send("%1", "hi", True)

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("/opt/bad/tmux")))
    with pytest.raises(RuntimeError, match="tmux not found at /opt/bad/tmux"):
        ade_basement.tmux("list-panes")

    monkeypatch.setattr(subprocess, "run", lambda argv, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 5)))
    with pytest.raises(subprocess.TimeoutExpired) as to_info:
        greenlight.run(["glab", "mr", "list"], tries=1)
    assert greenlight.scan_failed(to_info.value)["tool"] == "glab"


def test_on_base_normalizes_refs_and_uses_origin_head(tmp_path):
    # mixed refs/heads/ vs short names + missing repoId guard
    wts = [
        {"repoId": "/r/a", "isMainWorktree": True, "branch": "refs/heads/main", "path": "/nonexistent/a"},
        {"repoId": "", "isMainWorktree": True, "branch": "refs/heads/main", "path": "/nonexistent/empty"},
        {"isMainWorktree": True, "branch": "refs/heads/main", "path": "/nonexistent/none"},
    ]
    bases = greenlight.base_branches(wts)
    assert bases == {"/r/a": "main"}
    assert greenlight.on_base({"repoId": "/r/a", "branch": "main"}, bases)
    assert greenlight.on_base({"repoId": "/r/a", "branch": "refs/heads/main"}, bases)
    assert not greenlight.on_base({"repoId": "", "branch": "main"}, bases)
    assert not greenlight.on_base({"branch": "main"}, bases)

    # real git repo whose main worktree is on a feature branch while origin/HEAD points at main
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    main_wt = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(main_wt)], check=True)
    subprocess.run(["git", "-C", str(main_wt), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(main_wt), "push", "-q", "-u", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(main_wt), "remote", "set-head", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(main_wt), "checkout", "-q", "-b", "feat-in-main"], check=True)
    real_bases = greenlight.base_branches([{"repoId": str(main_wt), "isMainWorktree": True, "branch": "refs/heads/feat-in-main", "path": str(main_wt)}])
    assert real_bases == {str(main_wt): "main"}
    assert greenlight.on_base({"repoId": str(main_wt), "branch": "refs/heads/main"}, real_bases)
    assert not greenlight.on_base({"repoId": str(main_wt), "branch": "refs/heads/feat-in-main"}, real_bases)
