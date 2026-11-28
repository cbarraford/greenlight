"""Native backend (GREENLIGHT_ADE=native; `basement` is its old name): greenlight's own engine, ade.py's contract.

No ADE app at all: worktrees are managed with Worktrunk, terminals are the Greenlight app's own tmux server (`tmux -L greenlight`), and an
agent is a pane whose title Claude sets (braille spinner = working, ✳ = idle). Repos are the main checkouts of every pane's
folder plus the ones listed in ~/.greenlight/repos.json (each worktree this makes adds its repo there). An agent some
other ADE or terminal runs in a worktree (external.py) is listed too, as working and `external`: never driven, never
deleted under (#195 passive coexistence).
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

import external
import harness
import last_agent
import ade
import ade_common
import worktrunk
from ade_common import MENU_ITEM_RE, draft, git, git_info, goals, plain, repo_list, screen_state

HOME = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
REPOS, BRIEFS = HOME / "repos.json", HOME / "briefs"
SEP = "\t"
NAME = "native"


TMUX_SOCKET = os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"


READS = ("list-panes", "capture-pane", "display-message")


def tmux(*args):
    try:
        p = subprocess.run(["tmux", "-L", TMUX_SOCKET, *args], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:  # no tmux: nothing of ours runs (a listing goes on); starting an agent can't
        if args and args[0] in READS:
            return ""
        raise RuntimeError("tmux isn't installed: the native engine runs agents in `tmux -L greenlight`") from None
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
        if len(parts) == 6 and not parts[1].startswith("gl-warm-"):
            out.append(dict(zip(("handle", "session", "cwd", "title", "at", "cmd"), parts)))
    return out


SPIN = re.compile(r"^[^\w\s❯>] ?\S[^(]*…(?: \(\d+[smh]\b|$)")  # Claude's "✶ Skedaddling… (3s · …)" line: inside tmux it doesn't animate its title


def agent_type(title, cmd=""):
    """Identify a Basement pane's agent (claude, codex, agy, gemini, opencode, pi) from its title glyph or tmux command, else None."""
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
    if low == "gemini" or low.startswith("gemini-"):
        return "gemini"
    if low == "opencode" or low.startswith("opencode-"):
        return "opencode"
    if low == "pi":
        return "pi"
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
    recs, now_ms = _load_recs(), int(time.time() * 1000)
    rec_paths = _path_map(recs) if recs else None
    for p in panes():
        owner = _owner_of(p["cwd"], rec_paths) if rec_paths else None
        kind, s = _pane_agent(p, owner, recs, now_ms)
        if not kind or not s:
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


def _save_repos(repos):
    REPOS.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPOS.with_name(f".{REPOS.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(repos, indent=1))
    os.replace(tmp, REPOS)


def remember(repo):
    with ade_common.locked(REPOS):
        if repo not in (have := saved()):
            _save_repos([*have, repo])


def forget(repo):
    with ade_common.locked(REPOS):
        if repo in (have := saved()):
            _save_repos([r for r in have if r != repo])


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


def _path_map(paths):
    """Precomputed (norm_pairs, real_pairs) ordered longest-first for _owner_of()."""
    norm = sorted(((os.path.normpath(w), w) for w in dict.fromkeys(paths) if w), key=lambda x: -len(x[0]))
    real = sorted(((os.path.realpath(nw), w) for nw, w in norm), key=lambda x: -len(x[0]))
    return norm, real


def _match_prefix(path, pairs):
    for prefix, orig in pairs:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return orig
    return None


def _owner_of(cwd, paths):
    """Return the longest worktree path in paths containing cwd, matching normpath first and realpath for symlinks."""
    if not cwd:
        return None
    norm, real = paths if isinstance(paths, tuple) and len(paths) == 2 and isinstance(paths[0], list) else _path_map(paths)
    ncwd = os.path.normpath(cwd)
    if (hit := _match_prefix(ncwd, norm)) is not None:
        return hit
    return _match_prefix(os.path.realpath(ncwd), real)


def worktrees():
    import importers  # here: importers imports this module
    importers.first_run()  # the first listing on this install brings the other ADEs' repos and goals in (and retries a
    # source that wasn't up, a few times); a no-op after that
    ps, g = panes(), goals()
    repos = dict.fromkeys([*saved(), *(git_info(p["cwd"])["repoId"] for p in {p["cwd"]: p for p in ps}.values())])
    paths = [l[9:] for repo in filter(None, repos)
             for l in git(repo, "worktree", "list", "--porcelain").splitlines()
             if l.startswith("worktree ") and not agent_worktree(l[9:])]
    pm = _path_map(paths)
    owned: dict[str, list] = {}
    for p in ps:
        if (owner := _owner_of(p["cwd"], pm)) is not None:
            owned.setdefault(owner, []).append(p)
    out, ext = [], external.by_worktree_safe(paths)  # agents another ADE or a plain terminal runs in these worktrees
    recs, now_ms = _load_recs(), int(time.time() * 1000)
    for path in dict.fromkeys(paths):
        mine = owned.get(path, [])
        ags = []
        for p in mine:
            kind, s = _pane_agent(p, path, recs, now_ms)
            if kind and s:
                at_ms = int(p["at"] or 0) * 1000
                ags.append({"state": s, "agentType": kind, "updatedAt": at_ms, "stateStartedAt": at_ms,
                            "handle": p["handle"], "session": p.get("session"), "paneKey": f"basement:{p['handle']}"})
        # passive coexistence: busy, not ours to drive. updatedAt 0: an agent of ours here that's active still comes first
        ags += [{"state": "working", "agentType": e["agent"], "external": True, "pid": e["pid"], "updatedAt": 0,
                 "stateStartedAt": e["start"] * 1000 if e.get("start") else None} for e in ext.get(path, [])]
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


def _load_recs():
    try:
        raw = last_agent.load()
    except OSError:
        return {}
    out = {}
    for p, rec in raw.items():
        np = os.path.normpath(p)
        out[np] = rec
        rp = os.path.realpath(np)
        if rp != np:
            out.setdefault(rp, rec)
    return out


def _pane_rec(p, owner=None, recs=None):
    if not owner or not recs:
        return None
    norm = os.path.normpath(owner)
    raw = recs.get(norm) or recs.get(os.path.realpath(norm))
    return raw if isinstance(raw, dict) and raw.get("session") == p.get("session") else None


def _is_fresh_rec(rec, now_ms):
    return bool(rec and now_ms is not None and rec.get("used") and 0 <= now_ms - rec["used"] < 15_000)


def _pane_kind(p, owner=None, recs=None, now_ms=None):
    """Identify a Basement pane's harness without capturing its screen (handles wrapped and freshly launched panes)."""
    kind = agent_type(p["title"], p.get("cmd", ""))
    if kind:
        return kind
    rec = _pane_rec(p, owner, recs)
    if rec is None:
        return None
    if last_agent.cmd(p) in last_agent.CMDS.get(rec["harness"], ()) or _is_fresh_rec(rec, now_ms):
        return rec["harness"]
    return None


def _pane_agent(p, owner=None, recs=None, now_ms=None):
    """Identify (kind, state) for a Basement tmux pane, falling back to last_agent for starting or wrapped harnesses."""
    title, cmd = p["title"], p.get("cmd", "")
    direct = agent_type(title, cmd)
    rec = _pane_rec(p, owner, recs)
    fresh_rec = _is_fresh_rec(rec, now_ms)
    kind = direct or _pane_kind(p, owner, recs, now_ms)
    if not kind:
        return None, None
    if not direct and rec is not None and last_agent.cmd(p) not in last_agent.CMDS.get(rec["harness"], ()):
        return kind, "working"
    needs_tail = kind != "claude" or not ("⠀" <= title[:1] <= "⣿")
    s = state(title, screen(p["handle"]) if needs_tail else None, kind)
    if s == "done" and fresh_rec and rec is not None and last_agent.cmd(p) in last_agent.CMDS.get(rec["harness"], ()):
        s = "working"
    return kind, s


def outside(path, paths=None):
    """The agent another ADE or terminal runs in path (or a worktree nested in it), else None; a failed check says so.
    cleanup.outside_agent's answer, against every worktree (paths, else listed now)."""
    import cleanup  # here: cleanup imports greenlight, which imports ade and so this module
    return cleanup.outside_agent(path, fresh=True, paths=[w["path"] for w in worktrees()] if paths is None else paths)


def launch(path, agent, prompt):
    """A detached tmux session in the worktree running the agent; the page attaches a tab to it (mc-… is the page's own naming).
    Never beside an agent another ADE or terminal runs there (a second agent would fight it)."""
    if out := outside(path):
        raise RuntimeError(f"{out} runs in {path} outside Greenlight: no second agent beside it")
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
    pm = _path_map([w["path"] for w in worktrees()])
    recs, now_ms = _load_recs(), int(time.time() * 1000)
    out = []
    for p in panes():
        owner = _owner_of(p["cwd"], pm)
        kind = _pane_kind(p, owner, recs, now_ms)
        out.append({"handle": p["handle"], "title": p["title"], "lastOutputAt": int(p["at"] or 0) * 1000,
                    "agentIdentity": kind, "worktreeId": owner, "tabId": "basement",
                    "leafId": p["handle"], "session": p.get("session")})
    return out


def set_goal(worktree_id, goal):
    ade_common.set_goal(worktree_id, goal)


def remove(worktree_id, force=False):
    trees = worktrees()
    ws = next((w for w in trees if w["id"] == worktree_id), None)
    if ws is None:
        raise RuntimeError(f"no native workspace {worktree_id}")
    if ws["isMainWorktree"]:
        raise RuntimeError(f"{worktree_id} is a repo's main checkout: it is never removed")
    if out := outside(ws["path"], [w["path"] for w in trees]):
        raise RuntimeError(f"{out} runs in {worktree_id} outside Greenlight: it isn't removed under it")
    worktrunk.remove(ws["repoId"], ws["path"], force=force)
    ade_common.set_goal(ws["path"], "")


def create(repo, name, base=None, comment="", agent=None, prompt=""):
    """A new worktree of repo (its path, or a known repo's folder name) on branch name; its goal; and, when agent is
    named (as Orca's `worktree create --agent`), that agent launched in it on prompt. A launch that fails leaves the
    workspace and its goal (greenlight dispatches to it later) and says why in `error`."""
    if not Path(repo).is_absolute():
        named = [r for r in saved() if Path(r).name == repo]
        if len(named) != 1:  # never a folder relative to wherever this runs
            raise RuntimeError(f"repo {repo!r} names {len(named) or 'no'} registered repo{'s' * (len(named) != 1)}: pass its path"
                               + (" (greenlight.py repos add <path> registers one)" if not named else ""))
        repo = named[0]
    if not (main := main_checkout(repo)):
        raise RuntimeError(f"not a git checkout: {repo}")
    path = worktrunk.create(main, name, base)
    remember(main)
    if comment:
        ade_common.set_goal(path, comment)
    out: dict = {"worktree": {"id": path, "path": path}}
    if agent:
        try:
            out["terminal"] = launch(path, agent, prompt)["terminal"]
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:  # ValueError: a harness name harness.py lacks
            out["error"] = f"the workspace was made, but {agent} didn't start: {str(e)[:300]}"
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
