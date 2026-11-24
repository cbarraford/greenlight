"""The last coding agent each workspace ran, so a workspace page's Basement button can bring it back (issue 157).

    ~/.greenlight/last_agent.json   {worktree path: {"harness": "claude", "session": "mc-1a2b3c4d" | absent, "at": epoch s}}

`harness` is one of greenlight's harness names (harness.H). `session` is the app's tmux session (`tmux -L greenlight`) the
agent ran in, when it ran in the Basement: a tab the page opened, or one ade_basement.launch started. An agent in another
ADE (Orca, cmux, herdr, T3) has no session here: its terminal isn't ours to attach to, so the button resumes it instead.

Written by dash.py's snapshot (every agent the ADE lists, and every Claude pane in the app's tmux) and by
ade_basement.launch; read by the snapshot as each puck's `resume`, which the button hands to terminal.html's term.ws:
attach to the session while it's alive, else start the harness's own "continue the last session here" command.

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
SESSION_RE = re.compile(r"^[\w.-]{1,64}$")  # tmux session names greenlight makes (mc-…, the page's own); App.swift checks the same


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


def note(seen):
    """Record {path: (harness, session or None)}: each sighting replaces that workspace's record. Writes only on a change."""
    seen = {p: (h, s if isinstance(s, str) and SESSION_RE.match(s) else None)
            for p, (h, s) in seen.items() if isinstance(p, str) and p.startswith("/") and harness_of(h)}
    if not seen:
        return
    with _lock():
        cur = load()
        changed = False
        for path, (h, s) in seen.items():
            rec = {"harness": harness_of(h), **({"session": s} if s else {})}
            old = cur.get(path) or {}
            if {k: old.get(k) for k in ("harness", "session") if old.get(k)} != rec:
                cur[path] = {**rec, "at": int(time.time())}
                changed = True
        if changed:
            tmp = FILE.with_name(f".{FILE.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cur, indent=1, sort_keys=True))
            os.replace(tmp, FILE)


def owner(cwd, paths):
    """The worktree (longest path) a folder is in, or None."""
    return next((w for w in sorted(paths, key=len, reverse=True) if cwd == w or cwd.startswith(w.rstrip("/") + "/")), None)


def observe(agents, panes, is_agent):
    """agents: [(worktree path, the ADE's primary agent dict)]; panes: ade_basement.panes() rows; is_agent(title): whether a pane
    runs Claude (ade_basement.state). A Basement pane wins over the ADE's own listing: it's the one that can be attached."""
    seen = {}
    for path, ag in agents:
        if h := harness_of((ag or {}).get("agentType") or (ag or {}).get("type")):
            seen[path] = (h, None)
    paths = [p for p, _ in agents]
    for p in panes:
        if is_agent(p.get("title") or "") and (w := owner(p.get("cwd") or "", paths)):
            seen[w] = ("claude", p.get("session"))
    note(seen)


def resume(path, alive, busy=False):
    """What the Basement button does for a workspace: {"harness", "session"} attaches to the session (still alive in tmux),
    {"harness"} starts the harness's continue command there, {} is a plain shell. busy: its agent is working somewhere we
    can't attach to, and a second copy on the same session would fight it."""
    rec = load().get(path)
    if not rec:
        return {}
    s = rec.get("session")
    if isinstance(s, str) and s in alive:
        return {"harness": rec["harness"], "session": s}
    return {} if busy else {"harness": rec["harness"]}
