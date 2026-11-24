"""The last coding agent each workspace ran, so a workspace page's Basement button can bring it back (issue 157).

    ~/.greenlight/last_agent.json   {worktree path: {"harness": "claude", "session": "mc-1a2b3c4d" | absent, "at": epoch s}}

`harness` is one of greenlight's harness names (harness.H). `session` is the app's tmux session (`tmux -L greenlight`) the
agent ran in, when it ran in the Basement: a tab the page opened, or one ade_basement.launch started. An agent in another
ADE (Orca, cmux, herdr, T3) has no session here: its terminal isn't ours to attach to, so the button resumes it instead.

Written by dash.py's snapshot (every agent the ADE lists, and every Claude pane in the app's tmux) and by
ade_basement.launch; read by the snapshot as each puck's `resume`, which the button hands to terminal.html's term.ws:
attach to the session while its agent still runs there, else start the harness's own "continue the last session here"
command. A session counts as live only while one of its panes runs something other than a shell: a Basement agent's
session outlives it (`agent; exec $SHELL -l`), and a tmux session the page reopens under a dead name is a plain shell.

Stdlib only; never imports greenlight.py.
"""
import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path

HOME = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
FILE = HOME / "last_agent.json"
HARNESSES = ("claude", "codex", "opencode", "gemini", "pi", "agy")  # harness.H's names (tests/test_last_agent.py checks)
ALIASES = {"antigravity": "agy", "claude-code": "claude", "claude code": "claude", "gemini-cli": "gemini"}  # the ADEs' ids
SESSION_RE = re.compile(r"^mc-[\w-]{1,64}$")  # the app's own sessions (sid(), launch); App.swift's open and resumeOf check the same
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "login"}
STALE_S = 86400  # a record whose worktree has been gone this long is dropped


def harness_of(agent_type):
    """An ADE's agentType → greenlight's harness name, or None for one greenlight can't resume."""
    t = str(agent_type or "").strip().lower()
    t = ALIASES.get(t, t)
    return t if t in HARNESSES else None


def load():
    try:
        d = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, dict) and harness_of(v.get("harness"))} \
        if isinstance(d, dict) else {}


@contextlib.contextmanager
def _lock():
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / ".last_agent.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _gone(path, rec, now):
    return now - (rec.get("at") or 0) > STALE_S and not os.path.isdir(path)


def note(seen):
    """Record {path: (harness, session or None)}: each sighting replaces that workspace's record. Writes only on a change;
    a write also drops records of worktrees gone for a day."""
    seen = {p: (h, s if isinstance(s, str) and SESSION_RE.match(s) else None)
            for p, (h, s) in seen.items() if isinstance(p, str) and p.startswith("/") and harness_of(h)}
    if not seen:
        return
    with _lock():
        cur, now = load(), int(time.time())
        changed = False
        for path, (h, s) in seen.items():
            rec = {"harness": harness_of(h), **({"session": s} if s else {})}
            old = cur.get(path) or {}
            if {k: old.get(k) for k in ("harness", "session") if old.get(k)} != rec:
                cur[path] = {**rec, "at": now}
                changed = True
        if changed:
            cur = {k: v for k, v in cur.items() if k in seen or not _gone(k, v, now)}
            tmp = FILE.with_name(f".{FILE.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cur, indent=1, sort_keys=True))
            os.replace(tmp, FILE)


def owner(cwd, paths):
    """The worktree (longest path) a folder is in, or None."""
    return next((w for w in sorted(paths, key=len, reverse=True) if cwd == w or cwd.startswith(w.rstrip("/") + "/")), None)


def live(panes):
    """The sessions an agent (anything but a login shell) still runs in: panes need `cmd` (#{pane_current_command})."""
    return {p.get("session") for p in panes if (p.get("cmd") or "").lstrip("-").rsplit("/", 1)[-1] not in SHELLS | {""}}


def observe(agents, panes, is_agent, alive=frozenset()):
    """agents: [(worktree path, the ADE's primary agent dict)]; panes: ade_basement.panes() rows; is_agent(title): whether a pane
    runs Claude (ade_basement.state); alive: live(panes). A Basement pane wins over the ADE's own listing (it can be attached),
    the most recently active one among several; and a record whose session's agent still runs (a codex launch, which no
    title gives away) stays until that agent is gone."""
    seen = {}
    for path, ag in agents:
        if h := harness_of((ag or {}).get("agentType") or (ag or {}).get("type")):
            seen[path] = (h, None)
    paths, at = [p for p, _ in agents], dict[str, int]()
    for p in panes:
        w = is_agent(p.get("title") or "") and owner(p.get("cwd") or "", paths)
        if w and int(p.get("at") or 0) >= at.get(w, -1):
            seen[w], at[w] = ("claude", p.get("session")), int(p.get("at") or 0)
    cur = load()
    note({w: v for w, v in seen.items() if (s := (cur.get(w) or {}).get("session")) not in alive or v[1] == s})


def resume(rec, alive, busy=False, created=None, binary=None):
    """What the Basement button does for a workspace, from its record: {"harness", "session"} attaches to the session (its agent
    still runs there), {"harness"} starts the harness's continue command there ("bin": config.json's harnesses.<name>.bin),
    {} is a plain shell. busy: its agent is working somewhere we can't attach to, and a second copy would fight it.
    created (ms): the worktree's birth; a record older than it is a deleted workspace's, at the same path."""
    if not rec or (created and (rec.get("at") or 0) * 1000 < created):
        return {}
    s = rec.get("session")
    if isinstance(s, str) and s in alive:
        return {"harness": rec["harness"], "session": s}
    if busy:
        return {}
    return {"harness": rec["harness"], **({"bin": binary} if binary else {})}
