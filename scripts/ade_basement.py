"""Basement backend: answers the `orca ...` calls greenlight makes, in Orca's JSON shape (GREENLIGHT_ADE=basement).

No ADE app at all: worktrees are git's, terminals are the Greenlight app's own tmux server (`tmux -L greenlight`), and an
agent is a pane whose title Claude sets (braille spinner = working, ✳ = idle). Repos are the main checkouts of every pane's
folder plus the ones listed in ~/.greenlight/repos.json (each worktree this makes adds its repo there).
"""
import contextlib
import json
import re
import os
import secrets
import shlex
import subprocess
import time
from pathlib import Path

import harness
import last_agent
from ade_common import MENU_ITEM_RE, default_agent, flags, git, git_info, goals, plain, draft, repo_list, set_goal, worktree_add, worktree_remove

HOME = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
REPOS, BRIEFS = HOME / "repos.json", HOME / "briefs"
SEP = "\t"


TMUX_SOCKET = os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"


def tmux(*args):
    p = subprocess.run(["tmux", "-L", TMUX_SOCKET, *args], capture_output=True, text=True, timeout=20)
    if p.returncode and "no server running" not in p.stderr and "error connecting" not in p.stderr:
        raise RuntimeError(f"tmux {args[0]}: {p.stderr.strip()[:300]}")
    return p.stdout


def panes(cmd=False):
    """Every pane: handle (%id), session, folder, title, last activity (s); cmd: also what runs in it (#{pane_current_command})."""
    keys = ("handle", "session", "cwd", "title", "at", *(("cmd",) if cmd else ()))
    f = SEP.join(("#{pane_id}", "#{session_name}", "#{pane_current_path}", "#{pane_title}", "#{window_activity}",
                  *(("#{pane_current_command}",) if cmd else ())))
    return [dict(zip(keys, l.split(SEP))) for l in tmux("list-panes", "-a", "-F", f).splitlines() if l.count(SEP) == len(keys) - 1]


SPIN = re.compile(r"^[^\w\s❯>] ?\S[^(]*…(?: \(\d+[smh]\b|$)")  # Claude's "✶ Skedaddling… (3s · …)" line: inside tmux it doesn't animate its title


def state(title, tail=None):
    """Claude's title glyph and screen → Orca's agent state; None when the pane isn't an agent."""
    g, tail = title[:1], [x.strip() for x in (tail or [])[-15:]]
    if "⠀" <= g <= "⣿" or (g == "✳" and any(SPIN.match(x) for x in tail)):
        return "working"
    if g != "✳":  # ponytail: Claude only; codex/opencode panes read as plain shells
        return None
    return "waiting" if any(MENU_ITEM_RE.search(x) for x in tail) else "done"


def screen(handle):
    return [x for x in tmux("capture-pane", "-p", "-t", handle).splitlines() if x.strip()]


_STATE_PRIO = {"done": 0, "working": 1, "waiting": 2}


def pane_states():
    """Map Basement tmux session -> {state, title} for every agent pane on TMUX_SOCKET."""
    out: dict[str, dict[str, str]] = {}
    for p in panes():
        s = state(p["title"], screen(p["handle"]) if p["title"][:1] == "✳" else None)
        if not s:
            continue
        prev = out.get(p["session"])
        if prev is None or _STATE_PRIO[s] >= _STATE_PRIO[prev["state"]]:
            out[p["session"]] = {"state": s, "title": p["title"]}
    return out


def saved():
    try:
        return [r for r in json.loads(REPOS.read_text()) if isinstance(r, str)]
    except (OSError, ValueError):
        return []


def remember(repo):
    if repo not in (have := saved()):
        REPOS.parent.mkdir(parents=True, exist_ok=True)
        tmp = REPOS.with_name(f".{REPOS.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps([*have, repo], indent=1))
        os.replace(tmp, REPOS)


def worktrees():
    ps, g = panes(), goals()
    repos = dict.fromkeys([*saved(), *(git_info(p["cwd"])["repoId"] for p in {p["cwd"]: p for p in ps}.values())])
    paths = [l[9:] for repo in filter(None, repos)
             for l in git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]
    by_len = sorted(set(paths), key=len, reverse=True)
    owned: dict[str, list] = {}
    for p in ps:
        owner = next((w for w in by_len if p["cwd"] == w or p["cwd"].startswith(w + "/")), None)
        if owner is not None:
            owned.setdefault(owner, []).append(p)
    out = []
    for path in dict.fromkeys(paths):
        mine = owned.get(path, [])
        ags = [{"state": s, "agentType": "claude", "updatedAt": int(p["at"] or 0) * 1000, "stateStartedAt": int(p["at"] or 0) * 1000,
                "handle": p["handle"]} for p in mine if (s := state(p["title"], screen(p["handle"]) if p["title"][:1] == "✳" else None))]
        out.append({**git_info(path), "id": path, "worktreeId": path, "displayName": Path(path).name, "comment": g.get(path, ""),
                    "isPinned": False, "linkedGitLabMR": None, "agents": ags,
                    "lastOutputAt": max((int(p["at"] or 0) * 1000 for p in mine), default=None)})
    return out


def read(handle, limit):
    lines = tmux("capture-pane", "-p", "-e", "-t", handle, "-S", f"-{int(limit)}").splitlines()
    return {"terminal": {"handle": handle, "tail": [plain(x) for x in lines], "draft": draft([plain(x, ghost=False) for x in lines])}}


def send(handle, text, enter):
    if enter:  # a bracketed paste keeps a multi-line brief one message; then Enter submits it
        buf = f"gl-{secrets.token_hex(4)}"
        try:
            subprocess.run(["tmux", "-L", TMUX_SOCKET, "load-buffer", "-b", buf, "-"], input=text, text=True, timeout=20, check=True)
            tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", handle)
        finally:
            subprocess.run(["tmux", "-L", TMUX_SOCKET, "delete-buffer", "-b", buf], capture_output=True, timeout=10)
        time.sleep(0.2)
        tmux("send-keys", "-t", handle, "Enter")
    else:  # a menu pick
        tmux("send-keys", "-t", handle, "-l", "--", text)


def launch(path, agent, prompt):
    """A detached tmux session in the worktree running the agent; the page attaches a tab to it (mc-… is the page's own naming)."""
    if prompt and ("\n" in prompt or len(prompt.encode()) > 800):  # as herdr: a long brief goes by file
        BRIEFS.mkdir(mode=0o700, parents=True, exist_ok=True)
        brief = BRIEFS / f"{Path(path).name}-{int(time.time())}-{secrets.token_hex(4)}.md"
        fd = os.open(str(brief), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as bf:
            bf.write(prompt)
        prompt = f"Your brief is in {brief}. Read it and follow it."
    s = f"mc-{secrets.token_hex(4)}"
    # -i: the agent's PATH (~/.local/bin for claude) is set in .zshrc/.bashrc, which a bare `sh -c` from tmux never reads
    tmux("new-session", "-d", "-s", s, "-c", path, "$SHELL -lic " + shlex.quote(shlex.join(harness.interactive(agent, prompt))) + "; exec $SHELL -l")
    with contextlib.suppress(OSError):  # the workspace page's Basement button attaches to it (last_agent.py); panes can't tell codex apart
        last_agent.note({path: (agent, s, int(time.time() * 1000))})
    return {"terminal": {"handle": tmux("display-message", "-p", "-t", s, "#{pane_id}").strip(), "session": s}}


def call(*args):
    noun, verb, f = *args[:2], flags(args[2:])
    wid = str(f.get("worktree", "")).removeprefix("id:")
    if noun == "worktree" and verb in ("ps", "list"):
        return {"worktrees": worktrees()}
    if noun == "terminal" and verb == "list":
        trees = worktrees()
        return {"terminals": [{"handle": p["handle"], "title": p["title"], "lastOutputAt": int(p["at"] or 0) * 1000,
                               "agentIdentity": "claude" if state(p["title"]) else None,
                               "worktreeId": next((w["path"] for w in sorted(trees, key=lambda w: -len(w["path"]))
                                                   if p["cwd"] == w["path"] or p["cwd"].startswith(w["path"] + "/")), None)}
                              for p in panes()]}
    if noun == "terminal" and verb == "read":
        return read(f["terminal"], f.get("limit", 200))
    if noun == "terminal" and verb == "send":
        send(f["terminal"], f.get("text", ""), "enter" in f)
        return {}
    if noun == "agent" and verb == "launch":
        return launch(wid, f.get("agent") or default_agent(), f.get("prompt", ""))
    if noun == "worktree" and verb == "set":
        if "comment" in f:
            set_goal(wid, f["comment"] if isinstance(f["comment"], str) else "")
        return {}
    if noun == "worktree" and verb == "rm":
        worktree_remove(next(w for w in worktrees() if w["id"] == wid), force="force" in f)
        return {"removed": wid}
    if noun == "worktree" and verb == "create":
        repo = f["repo"].removeprefix("id:")
        path = worktree_add(repo, f["name"], f.get("base-branch") or f.get("base"))
        remember(repo)
        if f.get("comment"):
            set_goal(path, f["comment"])
        return {"worktree": {"id": path, "path": path}}
    if noun == "repo" and verb == "list":
        return repo_list(worktrees())
    raise RuntimeError(f"orca {noun} {verb} has no Basement equivalent")


if __name__ == "__main__":  # the parsers: python3 ade_basement.py
    assert state("⠋ Fixing tests") == "working" and state("✳ Done") == "done" and state("zsh") is None
    assert state("✳ Claude", ["  ❯ 1. Yes", "    2. No"]) == "waiting"
    assert state("✳ Smoke", ["✶ Skedaddling… (3s · thinking with high effort)", "❯ "]) == "working"
    assert state("✳ Smoke", ["✶ Skedaddling…", "❯ "]) == "working"  # its first second: no timer yet
    assert state("✳ Smoke", ["✻ Churned for 36s · done 12:52 PM", "❯ "]) == "done"
    print("ok")
