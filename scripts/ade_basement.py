"""Native backend (GREENLIGHT_ADE=native; `basement` is its old name): greenlight's own engine, ade.py's contract.

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
import sys
import time
from pathlib import Path

import harness
import last_agent
import ade
import ade_common
from ade_common import MENU_ITEM_RE, draft, git, git_info, goals, plain, repo_list, screen_state, worktree_add, worktree_remove

HOME = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
REPOS, BRIEFS = HOME / "repos.json", HOME / "briefs"
SEP = "\t"
NAME = "native"


TMUX_SOCKET = os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"


def tmux(*args):
    p = subprocess.run(["tmux", "-L", TMUX_SOCKET, *args], capture_output=True, text=True, timeout=20)
    if p.returncode and "no server running" not in p.stderr and "error connecting" not in p.stderr:
        raise RuntimeError(f"tmux {args[0]}: {p.stderr.strip()[:300]}")
    return p.stdout


def panes(cmd=False):
    """Every pane: handle (%id), session, folder, title, last activity (s); cmd: also what runs in it (#{pane_current_command})."""
    f = SEP.join(("#{pane_id}", "#{session_name}", "#{pane_current_path}", "#{pane_title}", "#{window_activity}",
                  "#{pane_current_command}"))
    out = []
    for l in tmux("list-panes", "-a", "-F", f).splitlines():
        parts = l.split(SEP)
        if len(parts) == 5 and not cmd:
            parts.append("")
        if len(parts) == 6:
            out.append(dict(zip(("handle", "session", "cwd", "title", "at", "cmd"), parts)))
    return out


SPIN = re.compile(r"^[^\w\s❯>] ?\S[^(]*…(?: \(\d+[smh]\b|$)")  # Claude's "✶ Skedaddling… (3s · …)" line: inside tmux it doesn't animate its title


def agent_type(title, cmd=""):
    """Identify a Basement pane's agent (claude, codex, agy) from its title glyph or tmux command, else None."""
    g = (title or "")[:1]
    if "⠀" <= g <= "⣿" or g == "✳":
        return "claude"
    if g == "⌬":
        return "codex"
    if g == "◈":
        return "agy"
    low = os.path.basename((cmd or "").strip()).lower()
    if low == "claude" or low.startswith("claude-"):
        return "claude"
    if low == "codex" or low.startswith("codex-"):
        return "codex"
    if low in ("agy", "antigravity") or low.startswith("agy-"):
        return "agy"
    return None


def state(title, tail=None, cmd=""):
    """Agent title/command and screen → Orca's agent state; None when the pane isn't an agent."""
    kind = agent_type(title, cmd)
    if not kind:
        return None
    if kind == "claude":
        g, lines = (title or "")[:1], [x.strip() for x in (tail or [])[-15:]]
        if "⠀" <= g <= "⣿" or (g == "✳" and any(SPIN.match(x) for x in lines)):
            return "working"
        if g == "✳":
            return "waiting" if any(MENU_ITEM_RE.search(x) for x in lines) else "done"
    seen = screen_state(tail or [])["state"]
    if seen == "working":
        return "working"
    if seen == "waiting_for_user":
        return "waiting"
    return "done"


def screen(handle):
    return [x for x in tmux("capture-pane", "-p", "-t", handle).splitlines() if x.strip()]


_STATE_PRIO = {"done": 0, "working": 1, "waiting": 2}
_TITLE_PREFIX = {"codex": "⌬ ", "agy": "◈ "}


def pane_states():
    """Map Basement tmux session -> {state, title} for every agent pane on TMUX_SOCKET."""
    out: dict[str, dict[str, str]] = {}
    for p in panes():
        kind = agent_type(p["title"], p.get("cmd", ""))
        if not kind:
            continue
        needs_tail = kind != "claude" or not ("⠀" <= p["title"][:1] <= "⣿")
        s = state(p["title"], screen(p["handle"]) if needs_tail else None, p.get("cmd", ""))
        if not s:
            continue
        pref = _TITLE_PREFIX.get(kind, "")
        title = p["title"] if not pref or p["title"].startswith(pref) else pref + p["title"]
        prev = out.get(p["session"])
        if prev is None or _STATE_PRIO[s] >= _STATE_PRIO[prev["state"]]:
            out[p["session"]] = {"state": s, "title": title}
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


def forget(repo):
    if repo in (have := saved()):
        tmp = REPOS.with_name(f".{REPOS.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps([r for r in have if r != repo], indent=1))
        os.replace(tmp, REPOS)


def main_checkout(path):
    """The main checkout of the git repo path is in (absolute), else None: what repos.json holds."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return None
    info = git_info(str(p.resolve()))
    return info["repoId"] or None


def register(path):
    """Add the repo path is in to repos.json (its main checkout); the path stored. RuntimeError when it isn't a git repo."""
    repo = main_checkout(path)
    if not repo:
        raise RuntimeError(f"not a git checkout: {path}")
    remember(repo)
    return repo


def agent_worktree(path):
    """Claude Code's own subagent checkouts (`<repo>/.claude/worktrees/agent-…`): not a workspace, so hidden."""
    return "/.claude/worktrees/" in path + "/"


def worktrees():
    ps, g = panes(), goals()
    repos = dict.fromkeys([*saved(), *(git_info(p["cwd"])["repoId"] for p in {p["cwd"]: p for p in ps}.values())])
    paths = [l[9:] for repo in filter(None, repos)
             for l in git(repo, "worktree", "list", "--porcelain").splitlines()
             if l.startswith("worktree ") and not agent_worktree(l[9:])]
    by_len = sorted(set(paths), key=len, reverse=True)
    owned: dict[str, list] = {}
    for p in ps:
        owner = next((w for w in by_len if p["cwd"] == w or p["cwd"].startswith(w + "/")), None)
        if owner is not None:
            owned.setdefault(owner, []).append(p)
    out = []
    for path in dict.fromkeys(paths):
        mine = owned.get(path, [])
        ags = []
        for p in mine:
            kind = agent_type(p["title"], p.get("cmd", ""))
            if not kind:
                continue
            needs_tail = kind != "claude" or not ("⠀" <= p["title"][:1] <= "⣿")
            s = state(p["title"], screen(p["handle"]) if needs_tail else None, p.get("cmd", ""))
            if s:
                ags.append({"state": s, "agentType": kind, "updatedAt": int(p["at"] or 0) * 1000,
                            "stateStartedAt": int(p["at"] or 0) * 1000, "handle": p["handle"]})
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
    shell_cmd = "$SHELL -lic " + shlex.quote(shlex.join(harness.interactive(agent, prompt))) + "; exec $SHELL -l"
    tmux_args = ["new-session", "-d", "-s", s, "-c", path, shell_cmd]
    try:
        import pane_log

        pipe_cmd = pane_log.pipe_command(
            str(Path(__file__).resolve().parent.parent), str(HOME), TMUX_SOCKET,
            session=s, cwd=path, agent=agent, python=sys.executable,
        )
        tmux_args.extend([";", "if", "-F", "#{pane_pipe}", "", f"pipe-pane -O -t ={s}: " + pane_log.tmux_quote(pipe_cmd)])
    except Exception:
        pass
    tmux(*tmux_args)
    with contextlib.suppress(OSError):  # the workspace page's Basement button attaches to it (last_agent.py); panes can't tell codex apart
        last_agent.note({path: (agent, s, int(time.time() * 1000))})
    return {"terminal": {"handle": tmux("display-message", "-p", "-t", s, "#{pane_id}").strip(), "session": s}}


def terminals():
    trees = sorted(worktrees(), key=lambda w: -len(w["path"]))
    return [{"handle": p["handle"], "title": p["title"], "lastOutputAt": int(p["at"] or 0) * 1000,
             "agentIdentity": agent_type(p["title"], p.get("cmd", "")),
             "worktreeId": next((w["path"] for w in trees if p["cwd"] == w["path"] or p["cwd"].startswith(w["path"] + "/")), None)}
            for p in panes()]


def set_goal(worktree_id, goal):
    ade_common.set_goal(worktree_id, goal)


def remove(worktree_id, force=False):
    ws = next((w for w in worktrees() if w["id"] == worktree_id), None)
    if ws is None:
        raise RuntimeError(f"no native workspace {worktree_id}")
    worktree_remove(ws, force=force)
    ade_common.set_goal(ws["path"], "")


def create(repo, name, base=None, comment="", agent=None, prompt=""):
    """A new worktree of repo (its path, or a known repo's folder name) on branch name; its goal; and, when agent is
    named (as Orca's `worktree create --agent`), that agent launched in it on prompt."""
    known = {Path(r).name: r for r in saved()}
    repo = repo if Path(repo).is_absolute() else known.get(repo, repo)
    path = worktree_add(repo, name, base)
    remember(repo)
    if comment:
        ade_common.set_goal(path, comment)
    out = {"worktree": {"id": path, "path": path}}
    if agent:
        out["terminal"] = launch(path, agent, prompt)["terminal"]
    return out


def repos():
    return repo_list(worktrees())


def call(*args):
    return ade.dispatch(sys.modules[__name__], *args)


if __name__ == "__main__":  # the parsers: python3 ade_basement.py
    assert state("⠋ Fixing tests") == "working" and state("✳ Done") == "done" and state("zsh") is None
    assert state("✳ Claude", ["  ❯ 1. Yes", "    2. No"]) == "waiting"
    assert state("✳ Smoke", ["✶ Skedaddling… (3s · thinking with high effort)", "❯ "]) == "working"
    assert state("✳ Smoke", ["✶ Skedaddling…", "❯ "]) == "working"  # its first second: no timer yet
    assert state("✳ Smoke", ["✻ Churned for 36s · done 12:52 PM", "❯ "]) == "done"
    print("ok")
