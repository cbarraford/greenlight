"""bins: finding CLIs when the backend's PATH lacks ~/.local/bin (ssh, launchd, systemd)."""
import json
import os

import pytest

import bins


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


def test_find_prefers_path_then_overrides(bare, tmp_path, monkeypatch):
    _exe(bare / ".local" / "bin" / "wt")
    on_path = _exe(tmp_path / "pathbin" / "wt")
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    assert bins.find("wt") == on_path

    (tmp_path / "gl").mkdir()
    (tmp_path / "gl" / "config.json").write_text(json.dumps({"bins": {"wt": "~/tools/wt"}}))
    assert bins.find("wt") == str(bare / "tools" / "wt")  # an override is used even before it exists: its error names it
    monkeypatch.setenv("WORKTRUNK_BIN", "/custom/wt")
    assert bins.find("wt", env="WORKTRUNK_BIN") == "/custom/wt"
    assert bins.find("wt") == str(bare / "tools" / "wt")  # env only when asked for

    (tmp_path / "gl" / "config.json").write_text(json.dumps({"bins": {"gh": "gh-beta"}}))
    beta = _exe(bare / ".local" / "bin" / "gh-beta")
    assert bins.find("gh") == beta  # a bare name is looked up the same way


@pytest.mark.parametrize("cfg", ['{"bins": "wt"}', '{"bins": {"wt": 7}}', '{"bins": {"wt": " "}}', "[]", "not json"])
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
    assert bins.ensure_path({}).startswith("/usr/bin:/bin:")  # no PATH at all: the system default first


def test_ensure_path_defaults_to_os_environ(bare, monkeypatch):
    (bare / ".cargo" / "bin").mkdir(parents=True)
    bins.ensure_path()
    assert os.environ["PATH"].split(os.pathsep)[-1] == str(bare / ".cargo" / "bin")
