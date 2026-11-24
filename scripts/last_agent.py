"""The last coding agent each workspace ran, so a workspace page's Basement button can bring it back (issue 157).

    ~/.greenlight/last_agent.json   {worktree path: {"harness": "claude", "session": "mc-1a2b3c4d" | absent, "used": epoch ms}}

`harness` is one of greenlight's harness names (harness.H). `session` is the app's tmux session (`tmux -L greenlight`) the
agent ran in, when it ran in the Basement: a tab the page opened, or one ade_basement.launch started. An agent in another
ADE (Orca, cmux, herdr, T3) has no session here: its terminal isn't ours to attach to, so the button resumes it instead.
`used` is when that agent was last seen at work (the ADE's updatedAt, the pane's last activity, the launch): the newest
sighting wins, so an older agent an ADE still lists never replaces the one used since.

Written by dash.py's snapshot (every agent the ADE lists, and every Claude pane in the app's tmux) and by
ade_basement.launch; read by the snapshot as each puck's `resume`, which the button hands to terminal.html's term.ws:
attach to the session while its agent still runs there, else start the harness's own "continue the last session here"
command. "Still runs there": a pane in that session runs the harness's own command (#{pane_current_command}), not the shell
a Basement agent's session falls back to (`agent; exec $SHELL -l`) nor whatever the user ran in it since.

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
# what tmux names a pane running each (Linux: its process group's argv[0]; macOS: the file it runs, so the native Claude is its
# version, e.g. 2.1.308, and cmd() calls that claude): the npm-installed ones run under node, but not Claude's: a quit Claude's
# ✳ title outlives it, and node is anything (an npm Claude on a Mac is continued, not attached)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CMDS = {"claude": {"claude"}, "codex": {"codex", "node"}, "opencode": {"opencode", "node", "bun"}, "gemini": {"gemini", "node"},
        "pi": {"pi", "node"}, "agy": {"agy", "antigravity"}}
SESSION_RE = re.compile(r"mc-[\w-]{1,64}")  # fullmatch  # the app's own sessions (sid(), launch); App.swift's open and resumeOf check the same
STALE_MS = 86400_000  # a record whose worktree has been gone this long is dropped
REFRESH_MS = 60_000  # the same agent seen again: written at most once a minute


def harness_of(agent_type):
    """An ADE's agentType → greenlight's harness name, or None for one greenlight can't resume."""
    t = str(agent_type or "").strip().lower()
    t = ALIASES.get(t, t)
    return t if t in HARNESSES else None


def _record(v):
    """A stored record as resume() may trust it, or None."""
    if not isinstance(v, dict) or not harness_of(v.get("harness")) or isinstance(v.get("used", 0), bool):
        return None
    try:
        used = int(v.get("used") or 0)
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON's 1e999 is inf
        return None
    s = v.get("session")
    return {"harness": harness_of(v["harness"]), **({"session": s} if isinstance(s, str) and SESSION_RE.fullmatch(s) else {}), "used": used}


def load(strict=False):
    """The records; strict (note, before it rewrites the file): a read error other than a missing file raises, not {}."""
    try:
        d = json.loads(FILE.read_text())
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    except ValueError:  # corrupt: rewritten from what's seen next
        return {}
    return {k: r for k, v in d.items() if isinstance(k, str) and (r := _record(v))} if isinstance(d, dict) else {}


@contextlib.contextmanager
def _lock():
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / ".last_agent.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _newer(old, rec, panes):
    """Whether a sighting replaces what's stored: a newer one, and for the same agent only once it's a minute newer; a live
    session of the stored harness also replaces one whose agent has quit (attaching beats a second copy beside it)."""
    if not old or (rec.get("session") and rec["harness"] == old["harness"] and old.get("session") not in ("", None, rec["session"])
                   and not runs(old, panes)):
        return True
    same = (old["harness"], old.get("session")) == (rec["harness"], rec.get("session"))
    return rec["used"] >= old["used"] + REFRESH_MS if same else rec["used"] > old["used"] or not old["used"]


def note(seen, panes=()):
    """Record {path: (harness, session or None, used ms)}, where newer than what's stored (panes: ade_basement.panes(cmd=True),
    to tell a stored session's agent has quit). Writes only on a change; a write also drops records of worktrees gone for a
    day. A read error raises (OSError) rather than overwrite the file with only these."""
    recs = {p: r for p, (h, s, used) in seen.items() if isinstance(p, str) and p.startswith("/")
            and (r := _record({"harness": h, "session": s, "used": used}))}
    if not recs:
        return
    with _lock():
        cur, now = load(strict=True), int(time.time() * 1000)
        new = {p: r for p, r in recs.items() if _newer(cur.get(p), r, panes)}
        if not new:
            return
        cur = {k: v for k, v in {**cur, **new}.items() if k in new or now - v["used"] < STALE_MS or os.path.isdir(k)}
        tmp = FILE.with_name(f".{FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cur, indent=1, sort_keys=True))
        os.replace(tmp, FILE)


def owner(cwd, paths):
    """The worktree (longest path) a folder is in, or None."""
    return next((w for w in sorted(paths, key=len, reverse=True) if cwd == w or cwd.startswith(w.rstrip("/") + "/")), None)


def cmd(pane):
    """What a pane runs, as CMDS names it (a login shell's leading - and any folder dropped; a bare version is Claude's)."""
    c = (pane.get("cmd") or "").lstrip("-").rsplit("/", 1)[-1]
    return "claude" if VERSION_RE.match(c) else c


def runs(rec, panes):
    """Whether the record's session still runs its harness: a pane of it whose command is that harness's."""
    s = (rec or {}).get("session")
    return bool(s) and any(p.get("session") == s and cmd(p) in CMDS[rec["harness"]] for p in panes)


def _used(ag, now):
    """When the ADE last saw this agent at work (ms); a live one with no time (herdr) counts as now, else unknown (0)."""
    try:
        t = int(ag.get("updatedAt") or ag.get("stateStartedAt") or 0)
    except (TypeError, ValueError, OverflowError):
        t = 0
    return t or (now if ag.get("state") in ("working", "waiting") else 0)


def observe(agents, panes, is_agent):
    """agents: [(worktree path, the ADE's primary agent dict)]; panes: ade_basement.panes(cmd=True) rows; is_agent(title): a
    Claude title (ade_basement.state). A Claude pane counts while claude runs in it (a quit one leaves its title behind); a
    pane and the ADE's own listing go by which was used last, the pane on a tie. The Basement ADE's own agents (they carry a
    `handle`) are these same panes by title alone, a quit one's too, so the panes speak for them."""
    now, seen = int(time.time() * 1000), {}
    for path, ag in agents:
        if (ag or {}).get("handle"):
            continue
        if h := harness_of((ag or {}).get("agentType") or (ag or {}).get("type")):
            seen[path] = (h, None, _used(ag, now))
    paths, live = [p for p, _ in agents], dict[str, tuple]()
    for p in panes:  # the newest pane still running Claude, per worktree
        w = is_agent(p.get("title") or "") and cmd(p) in CMDS["claude"] and owner(p.get("cwd") or "", paths)
        at = int(p["at"]) * 1000 if str(p.get("at") or "").isdigit() else now
        if w and at >= live.get(w, ("", None, -1))[2]:
            live[w] = ("claude", p.get("session"), at)
    for w, pane in live.items():  # it beats an older agent the ADE lists
        if w not in seen or pane[2] >= seen[w][2]:
            seen[w] = pane
    note(seen, panes)


def resume(rec, panes, busy=False, created=None, binary=None):
    """What the Basement button does for a workspace, from its record: {"harness", "session"} attaches to the session (its agent
    still runs there), {"harness"} starts the harness's continue command there ("bin": config.json's harnesses.<name>.bin),
    {} is a plain shell. busy: its agent is working, or waiting on you, somewhere we can't attach to: a second copy would fight it.
    created (ms): the worktree's birth; a record used before it is a deleted workspace's at the same path. ponytail: a used
    of 0 (an ADE that gives no times, herdr) can't be told apart, so it stands."""
    if not rec:
        return {}
    if runs(rec, panes):  # first: a session still running its harness is live whatever .git's mtime says (git worktree repair)
        return {"harness": rec["harness"], "session": rec["session"]}
    if created and rec["used"] and rec["used"] < created:
        return {}
    if busy:
        return {}
    return {"harness": rec["harness"], **({"bin": binary} if binary else {})}
