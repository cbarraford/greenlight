"""Finding the CLIs greenlight runs (wt, tmux, gh, glab, agents) when its PATH is a bare one.

The app starts the backend over ssh or launchd, and the automations under systemd: none of those read the shell
profile that puts ~/.local/bin (pipx, uv tool, the Worktrunk installer), Homebrew or ~/.cargo/bin on PATH.
ensure_path() appends those folders to os.environ's PATH, so every subprocess finds them; find() resolves one tool to
an absolute path, config.json's "bins" ({"wt": "/path/to/wt"}) first.
"""
import json
import os
import shutil
from pathlib import Path

# Per-user and package-manager bin folders a login shell usually adds. Appended after the PATH's own entries.
DIRS = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.cargo/bin",
    "/opt/local/bin",
    "~/.nix-profile/bin",
    "/nix/var/nix/profiles/default/bin",
    "~/bin",
)


def _dirs():
    return [os.path.expanduser(d) for d in DIRS]


def _executable(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def configured(name):
    """config.json's bins.<name>, "~" expanded; None when unset or not a string."""
    home = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
    try:
        conf = json.loads((home / "config.json").read_text()).get("bins")
    except (OSError, ValueError, AttributeError):
        return None
    val = conf.get(name) if isinstance(conf, dict) else None
    return os.path.expanduser(val) if isinstance(val, str) and val.strip() else None


def find(name, env=None):
    """An absolute path to the CLI name, or None: $env (when named and set), config.json's bins.<name>, the PATH,
    then DIRS. An override that isn't executable is still returned, so the error names it instead of a PATH lookalike."""
    override = (os.environ.get(env) if env else None) or configured(name)
    if override:  # a bare name ("wt-nightly") is looked up; a path is taken as is
        return override if os.sep in override else _lookup(override) or override
    return _lookup(name)


def _lookup(name):
    return shutil.which(name) or next((p for d in _dirs() if _executable(p := os.path.join(d, name))), None)


def ensure_path(environ=None):
    """Append DIRS that exist and are missing to PATH (in os.environ, or the dict given); returns the new PATH."""
    environ = os.environ if environ is None else environ
    cur = [p for p in (environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep) if p]
    extra = [d for d in _dirs() if d not in cur and os.path.isdir(d)]
    environ["PATH"] = os.pathsep.join(cur + list(dict.fromkeys(extra)))
    return environ["PATH"]


def searched():
    """Where find() looks after the PATH, for an error message (no user paths: "~" stays unexpanded)."""
    return ", ".join(DIRS)
