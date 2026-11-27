"""Which workspace backend greenlight drives, and the one contract every backend implements.

    name()          the backend in use: GREENLIGHT_ADE, else config.json's "ade", else DEFAULT
    get(name=None)  that backend's module (ade_<module>.py)
    dispatch(b, *args)  an `orca …`-shaped call (greenlight.py ade, and every core caller today) answered by b's contract

`native` is Greenlight's engine: Worktrunk worktrees, the app's tmux server (`tmux -L greenlight`), repos.json and
goals.json (ade_basement.py; `basement` is its old name). Orca, cmux, herdr and T3 adapters remain only as read-only
sources for importers.py. external.py sees agents another app runs, so Greenlight avoids their active worktrees.

The contract (what core code may ask of a backend; rows are in Orca's worktree/terminal shape, the one the scan reads):

    worktrees()                         [worktree row]: path, branch, repoId, comment (the goal), agents, …
    terminals()                         [{handle, worktreeId, agentIdentity, lastOutputAt, …}]
    read(handle, limit)                 {"terminal": {handle, tail, draft}}
    send(handle, text, enter)           type text into a terminal; enter submits it as one message
    launch(worktree_id, agent, prompt)  {"terminal": {handle, …}}: a new agent session in that worktree
    set_goal(worktree_id, goal)         the workspace's goal ("" clears it)
    remove(worktree_id, force=False)    delete the workspace's worktree (never its branch)
    create(repo, name, base=None, comment="", agent=None, prompt="")  {"worktree": {id, path}}
    repos()                             {"repos": [repo row]}

Stdlib only; no greenlight import at module level (greenlight imports this; ade_orca imports greenlight lazily).
"""
import fcntl
import importlib
import json
import os
from pathlib import Path

import ade_common

DEFAULT = "native"
ALIASES = {"basement": "native"}
MODULES = {"native": "ade_basement", "orca": "ade_orca", "cmux": "ade_cmux", "herdr": "ade_herdr", "t3": "ade_t3"}
NAMES = tuple(MODULES)
LEGACY = ("orca", "cmux", "herdr", "t3")  # import sources, never live workspace backends


def _home():
    return Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")


def _config_raw_ade():
    try:
        cfg = json.loads((_home() / "config.json").read_text())
    except (OSError, ValueError):
        return None
    return cfg.get("ade") if isinstance(cfg, dict) else None


def canonical(n):
    """A backend name as written (any case, an alias) → its name in NAMES, else None."""
    n = (n or "").strip().lower() if isinstance(n, str) else ""
    n = ALIASES.get(n, n)
    return n if n in MODULES else None


def name():
    """The native backend. Older ADE selections are migrated; importers still read those sources."""
    env = os.environ.get("GREENLIGHT_ADE")
    if env:
        if not (n := canonical(env)):
            raise RuntimeError(f"GREENLIGHT_ADE={env!r}: use native")
        if n != DEFAULT:
            raise RuntimeError(f"GREENLIGHT_ADE={env!r}: live ADE backends are retired; use greenlight.py import")
        return DEFAULT
    raw = _config_raw_ade()
    if canonical(raw if isinstance(raw, str) else None) in LEGACY:
        _migrate_config()
    elif raw in (None, ""):
        settle()
    return DEFAULT


def _migrate_config():
    """Persist the native selection without changing any other user settings."""
    f = _home() / "config.json"
    try:
        with open(f.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cfg = json.loads(f.read_text())
            if isinstance(cfg, dict) and canonical(cfg.get("ade")) in LEGACY:
                cfg["ade"] = DEFAULT
                cfg["ade_set_because"] = "live ADE backends retired; import remains available"
                tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(cfg, indent=1))
                os.replace(tmp, f)
    except (OSError, ValueError):
        pass


def ran_before():
    """This install already ran greenlight (on Orca, the old default): what only a past scan or run leaves. Not me.json
    (importing greenlight writes it), state/ (update.py), auto/ (the timer's first tick) or diag/."""
    return any((_home() / x).exists() for x in ("last.json", "log.jsonl", "decisions.jsonl", "history"))


def config_ok():
    """False when config.json sets an "ade" that names no backend (doctor says so)."""
    raw = _config_raw_ade()
    return raw in (None, "") or bool(canonical(raw if isinstance(raw, str) else None))


def settle():
    """Record the native backend for an install that never chose one."""
    home = _home()
    n = DEFAULT
    f = home / "config.json"
    why = "Worktrunk workspaces with Greenlight agent sessions"
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(f.with_suffix(".lock"), "w") as lock:  # decide.set_conf's lock: one writer at a time
            fcntl.flock(lock, fcntl.LOCK_EX)
            cfg = json.loads(f.read_text()) if f.exists() else {}
            if isinstance(cfg, dict) and not cfg.get("ade"):  # absent only: a typo stays the user's to fix
                tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({**cfg, "ade": n, "ade_set_because": why}, indent=1))
                os.replace(tmp, f)
            elif isinstance(cfg, dict) and isinstance(cfg["ade"], str):
                n = DEFAULT
    except (OSError, ValueError):
        pass  # can't write it (or not settings we can add to): the same answer next time, from the same facts
    return n


def is_orca():
    return name() == "orca"


def is_native():
    return name() == "native"


def deprecated():
    return None


def get(n=None):
    """The backend module for n (default: the one in use); KeyError for a name that isn't one."""
    c = canonical(n) if n else name()
    if not c:
        raise KeyError(f"no ADE backend {n!r}")
    return importlib.import_module(MODULES[c])


def dispatch(b, *args):
    """An `orca <noun> <verb> --flag v …` call answered through backend b's contract, in Orca's JSON shape."""
    noun, verb, f = *args[:2], ade_common.flags(args[2:])
    wid = str(f.get("worktree", "")).removeprefix("id:")
    if noun == "worktree" and verb in ("ps", "list"):
        return {"worktrees": b.worktrees()}
    if noun == "terminal" and verb == "list":
        return {"terminals": b.terminals()}
    if noun == "terminal" and verb == "read":
        return b.read(f["terminal"], f.get("limit", 200))
    if noun == "terminal" and verb == "send":
        b.send(f["terminal"], f.get("text", ""), "enter" in f)
        return {}
    if noun == "agent" and verb == "launch":
        return b.launch(wid, f.get("agent") or ade_common.default_agent(), f.get("prompt", ""))
    if noun == "worktree" and verb == "set":  # board status and MR links are Orca-only
        if "comment" in f:
            b.set_goal(wid, f["comment"] if isinstance(f["comment"], str) else "")
        return {}
    if noun == "worktree" and verb == "rm":
        b.remove(wid, force="force" in f)
        return {"removed": wid}
    if noun == "worktree" and verb == "create":
        comment = f.get("comment")
        return b.create(str(f["repo"]).removeprefix("id:"), f["name"], f.get("base-branch") or f.get("base"),
                        comment if isinstance(comment, str) else "", f.get("agent") or None, f.get("prompt") or "")
    if noun == "repo" and verb == "list":
        return b.repos()
    raise RuntimeError(f"orca {noun} {verb} has no {getattr(b, 'NAME', 'backend')} equivalent")


def call(*args, n=None):
    """dispatch() on the backend in use (or n). Orca takes its own CLI's arguments whole."""
    return get(n).call(*args)
