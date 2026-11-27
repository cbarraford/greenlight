"""Shared pytest bootstrap and isolated fixtures for Greenlight Python tests.

Ensures GREENLIGHT_HOME and GREENLIGHT_DEV_HOME are isolated in temporary directories
BEFORE any Greenlight script module is imported, so tests never touch real `~/.greenlight`.
"""
import atexit
import copy
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Preserve the caller's original GREENLIGHT_HOME for read-only leak.re checks (test_release.py).
ORIG_GREENLIGHT_HOME = os.environ.get("GREENLIGHT_HOME")

_BOOT_HOME = Path(tempfile.mkdtemp(prefix="gl-pytest-home-"))
_BOOT_DEV_HOME = Path(tempfile.mkdtemp(prefix="gl-pytest-dev-"))
atexit.register(shutil.rmtree, _BOOT_HOME, True)
atexit.register(shutil.rmtree, _BOOT_DEV_HOME, True)

os.environ["GREENLIGHT_HOME"] = str(_BOOT_HOME)
os.environ["GREENLIGHT_DEV_HOME"] = str(_BOOT_DEV_HOME)
os.environ.setdefault("GREENLIGHT_ME", "octo")
os.environ.setdefault("GREENLIGHT_ADE", "native")
(_BOOT_HOME / "imported.json").write_text("{}")  # never the first-run import of this machine's real ADEs (importers.first_run)
os.environ["GREENLIGHT_TMUX_SOCKET"] = f"gl-pytest-{os.getpid()}"  # dash.snapshot lists the app's tmux panes: never the real Basement's
os.environ.pop("GREENLIGHT_RUN", None)
os.environ["GREENLIGHT_USAGE"] = "off"  # usage.py: never the agents' usage APIs with your real sign-ins


def _patch_loaded_modules(monkeypatch, home: Path) -> None:
    """Re-bind module-level state paths for any already-imported Greenlight modules."""
    gl = sys.modules.get("greenlight")
    cfg = None
    if gl is not None:
        cfg = copy.deepcopy(gl.DEFAULTS)
        monkeypatch.setattr(gl, "STATE", home)
        monkeypatch.setattr(gl, "CONFIG", cfg)
        monkeypatch.setattr(gl, "BOT", cfg["review_bot"])
        monkeypatch.setattr(gl, "ME", "octo")

    bindings: dict[str, dict[str, object]] = {
        "ade_common": {"GOALS": home / "goals.json"},
        "ade_basement": {"HOME": home, "REPOS": home / "repos.json", "BRIEFS": home / "briefs"},
        "ade_herdr": {"BRIEFS": home / "briefs"},
        "adopt": {"DISMISSED": home / "dismissed.json", "STATE_FILE": home / "adopt.json", "LOG": home / "log.jsonl"},
        "auto": {"STATE": home, "FILE": home / "automations.json", "DIR": home / "auto"},
        "autonomy": {"STATE": home, "MEMORY": home / "MEMORY.md"},
        "blocking": {"FILE": home / "blocks.json"},
        "chat": {"DIR": str(home / "chat"), "PIDFILE": str(home / "chat" / "pid"), "CODEX_SESSIONS": str(home / "codex")},
        "cleanup": {"LOG": home / "log.jsonl"},
        "dash": {"DASH": home / "dash", "TASKS": home / "tasks", "MEMORY": home / "memory"},
        "decide": {"HOME": home},
        "decisions": {"STATE": home},
        "diag": {"_leak": False, "MAX": 5 << 20},
        "dwim": {"STATE": home, "DWIM": home / "dwim"},
        "last_agent": {"HOME": home, "FILE": home / "last_agent.json"},
        "lifestage": {"STATE": home, "CHECKS": home / "checks"},
        "usage": {"STATE": home},
        "ws_state": {"STATE": home, "DIR": home / "state", "MEMORY": home / "memory"},
    }
    if cfg is not None:
        bindings["auto"]["CONFIG"] = cfg
        bindings["harness"] = {"CONFIG": cfg}
        bindings["usage"]["CONFIG"] = cfg

    for mod_name, attrs in bindings.items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr, val in attrs.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val)


@pytest.fixture()
def gl_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated per-test GREENLIGHT_HOME directory with all loaded module paths rebound."""
    home = tmp_path / "gl-home"
    home.mkdir()
    monkeypatch.setenv("GREENLIGHT_HOME", str(home))
    monkeypatch.setenv("GREENLIGHT_ME", "octo")
    monkeypatch.delenv("GREENLIGHT_RUN", raising=False)
    _patch_loaded_modules(monkeypatch, home)
    return home


@pytest.fixture()
def gl_dev_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolated per-test environment for scripts/dev.py (never touches ~/Applications or ~/.greenlight-dev)."""
    home = tmp_path / "dev-home"
    apps = tmp_path / "apps"
    support = tmp_path / "support"
    agents = tmp_path / "agents"
    for d in (home, apps, support, agents):
        d.mkdir()
    monkeypatch.setenv("GREENLIGHT_DEV_HOME", str(home))
    dev_mod = sys.modules.get("dev")
    if dev_mod is not None:
        monkeypatch.setattr(dev_mod, "HOME", home)
        monkeypatch.setattr(dev_mod, "ME", home / "dev.py")
        monkeypatch.setattr(dev_mod, "APPS", apps)
        monkeypatch.setattr(dev_mod, "SUPPORT", support)
        monkeypatch.setattr(dev_mod, "AGENTS", agents)
    return {"home": home, "apps": apps, "support": support, "agents": agents}


@pytest.fixture()
def git_repo(tmp_path: Path):
    """Factory that creates isolated git repositories under tmp_path."""

    def _make(name: str = "repo", *, branch: str = "main", bare: bool = False, commit: bool = True) -> Path:
        repo = (tmp_path / name).resolve()
        if bare:
            subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
            return repo
        subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        if commit:
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        return repo

    return _make
