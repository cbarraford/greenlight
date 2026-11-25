"""Which workspace backend greenlight drives, and the one contract every backend implements.

    name()          the backend in use: GREENLIGHT_ADE, else config.json's "ade", else DEFAULT
    get(name=None)  that backend's module (ade_<module>.py)
    dispatch(b, *args)  an `orca …`-shaped call (greenlight.py ade, and every core caller today) answered by b's contract

`native` is greenlight's own engine: git worktrees, the app's tmux server (`tmux -L greenlight`), repos.json and
goals.json (ade_basement.py; `basement` is its old name), and the default. `orca` drives the Orca app (opt-in). cmux,
herdr and t3 are the deprecated legacy live drivers: they still answer when set, but importers.py brings their repos
and goals in once, and external.py sees the agents they run, so the native engine needs neither.

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

DEFAULT = "native"  # a new install's engine on macOS and Linux (#195); one that ran on Orca keeps it (settle)
ALIASES = {"basement": "native"}
MODULES = {"native": "ade_basement", "orca": "ade_orca", "cmux": "ade_cmux", "herdr": "ade_herdr", "t3": "ade_t3"}
NAMES = tuple(MODULES)
LEGACY = ("cmux", "herdr", "t3")  # deprecated live drivers: still answer when set, but importers.py + external.py replace them


def _home():
    return Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")


def _config_ade():
    try:
        v = json.loads((_home() / "config.json").read_text()).get("ade")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) else None


def canonical(n):
    """A backend name as written (any case, an alias) → its name in NAMES, else None."""
    n = (n or "").strip().lower()
    n = ALIASES.get(n, n)
    return n if n in MODULES else None


def name():
    """The backend in use. An unknown GREENLIGHT_ADE is an error (it names a backend that isn't there), an unknown
    config value falls through to the default."""
    env = os.environ.get("GREENLIGHT_ADE")
    if env:
        if not (n := canonical(env)):
            raise RuntimeError(f"GREENLIGHT_ADE={env!r}: not one of {', '.join(NAMES)}")
        return n
    return canonical(_config_ade()) or settle()


def settle():
    """The backend for an install that never chose one, decided once and written into config.json ("ade") so it never
    flips later. A new install gets DEFAULT (native). One that already ran greenlight (it has a scan, last.json) ran
    on Orca, the old default, and keeps it: native is then an explicit switch (README: Moving to native)."""
    home = _home()
    n = "orca" if (home / "last.json").exists() else DEFAULT
    f = home / "config.json"
    why = "this install ran on Orca before native became the default" if n == "orca" else "a new install: native by default"
    try:
        home.mkdir(parents=True, exist_ok=True)
        with open(f.with_suffix(".lock"), "w") as lock:  # decide.set_conf's lock: one writer at a time
            fcntl.flock(lock, fcntl.LOCK_EX)
            cfg = json.loads(f.read_text()) if f.exists() else {}
            if isinstance(cfg, dict) and not canonical(cfg.get("ade") if isinstance(cfg.get("ade"), str) else None):
                tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({**cfg, "ade": n, "ade_set_because": why}, indent=1))
                os.replace(tmp, f)
            elif isinstance(cfg, dict):
                n = canonical(cfg["ade"]) or n  # another process settled it meanwhile
    except (OSError, ValueError):
        pass  # can't write it (or not settings we can add to): the same answer next time, from the same facts
    return n


def is_orca():
    return name() == "orca"


def is_native():
    return name() == "native"


def deprecated():
    """The legacy live driver in use (cmux, herdr, t3), else None: diag doctor says so."""
    try:
        n = name()
    except RuntimeError:
        return None
    return n if n in LEGACY else None


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
