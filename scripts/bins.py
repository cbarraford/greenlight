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


def is_executable(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


_executable = is_executable


def _clean_override(val):
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s or any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        return None
    s = os.path.expanduser(s)
    if os.sep in s and not os.path.isabs(s):
        return None
    return s


def configured(name):
    """config.json's bins.<name>, "~" expanded; None when unset, invalid, or relative."""
    # diag.home()'s lookup, repeated: diag imports bins, so bins can't import diag
    home = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
    try:
        conf = json.loads((home / "config.json").read_text()).get("bins")
    except (OSError, ValueError, AttributeError):
        return None
    return _clean_override(conf.get(name)) if isinstance(conf, dict) else None


def find(name, env=None, override=None):
    """An absolute path to the CLI name, or None: override, $env (when named and set), config.json's bins.<name>,
    the PATH, then DIRS. An override that isn't executable is still returned, so the error names it instead of a PATH lookalike."""
    ov = _clean_override(override) or _clean_override(os.environ.get(env) if env else None) or configured(name)
    if ov:  # a bare name ("wt-nightly") is looked up; an absolute path is taken as is
        return ov if os.sep in ov else _lookup(ov) or ov
    return _lookup(name)


def _lookup(name):
    return shutil.which(name) or next((p for d in _dirs() if is_executable(p := os.path.join(d, name))), None)


def ensure_path(environ=None):
    """Append DIRS that exist and are missing to PATH (in os.environ, or the dict given); returns the new PATH."""
    environ = os.environ if environ is None else environ
    cur = [p for p in (environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep) if p]
    seen = {p.rstrip(os.sep) or os.sep for p in cur}
    extra = [d for d in dict.fromkeys(_dirs()) if (d.rstrip(os.sep) or os.sep) not in seen and os.path.isdir(d)]
    environ["PATH"] = os.pathsep.join(cur + extra)
    return environ["PATH"]


def argv(cmd):
    """cmd with its program swapped for config.json's bins.<program>, when one is set; else unchanged (PATH finds it)."""
    if cmd and isinstance(cmd[0], str) and os.sep not in cmd[0] and (alt := configured(cmd[0])):
        return [alt if os.sep in alt else _lookup(alt) or alt, *cmd[1:]]
    return list(cmd)


def searched():
    """Where find() looks after the PATH, for an error message (no user paths: "~" stays unexpanded)."""
    return ", ".join(DIRS)
