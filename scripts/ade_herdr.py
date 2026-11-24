"""herdr backend (GREENLIGHT_ADE=herdr, a legacy live driver): ade.py's contract answered by herdr.

herdr has workspaces, git worktrees, panes and agent detection of its own; goals live in ~/.greenlight/goals.json
(herdr has no workspace description). GREENLIGHT_HERDR_SESSION targets a named session instead of the default.
"""
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

import ade
import ade_common
from ade_common import draft, git_info, goals, plain, repo_list

BRIEFS = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight") / "briefs"
NAME = "herdr"
STATES = {"working": "working", "blocked": "waiting", "idle": "done", "done": "done"}  # herdr -> Orca agent state


def herdr(*args, js=True):
    s = os.environ.get("GREENLIGHT_HERDR_SESSION")
    p = subprocess.run(["herdr", *(["--session", s] if s else []), *args], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError(f"herdr {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return json.loads(p.stdout or "{}").get("result", {}) if js else p.stdout  # send-text prints nothing


def agents():
    """Live agents by pane id."""
    return {a["pane_id"]: a for a in herdr("agent", "list")["agents"]}


def terminals():
    ags, now = agents(), int(time.time() * 1000)
    # ponytail: herdr keeps no output timestamps, so "working" stands in for "active just now"; a busy plain
    # shell looks idle to cleanup's 30m guard
    return [{"handle": p["pane_id"], "worktreeId": p["workspace_id"], "agentIdentity": ags.get(p["pane_id"], {}).get("agent"),
             "lastOutputAt": now if ags.get(p["pane_id"], {}).get("agent_status") == "working" else None}
            for p in herdr("pane", "list")["panes"]]


def worktrees():
    ags, g, now = list(agents().values()), goals(), int(time.time() * 1000)
    cwd = {p["workspace_id"]: p["cwd"] for p in reversed(herdr("pane", "list")["panes"])}  # first pane wins
    out = []
    for w in herdr("workspace", "list")["workspaces"]:
        path = (w.get("worktree") or {}).get("checkout_path") or cwd.get(w["workspace_id"], "")
        mine = [a for a in ags if a["workspace_id"] == w["workspace_id"]]
        out.append({
            **git_info(path), "id": w["workspace_id"], "worktreeId": w["workspace_id"], "displayName": w["label"],
            "comment": g.get(path, ""), "isPinned": False, "linkedGitLabMR": None,
            "lastOutputAt": now if any(a["agent_status"] == "working" for a in mine) else None,
            # ponytail: herdr gives no last reply or timestamps; dash shows the state without the agent's last words
            "agents": [{"state": STATES.get(a["agent_status"], "unknown"), "agentType": a["agent"], "lastAssistantMessage": "",
                        "updatedAt": 0, "stateStartedAt": None} for a in mine],
        })
    return out


def read(handle, limit):
    lines = herdr("pane", "read", handle, "--source", "visible", "--lines", str(limit), "--format", "ansi", js=False).splitlines()
    return {"terminal": {"handle": handle, "tail": [plain(x) for x in lines], "draft": draft([plain(x, ghost=False) for x in lines])}}


def send(handle, text, enter):
    if enter and handle in agents():  # agent prompt pastes (one message, newlines kept) and submits in one write
        herdr("agent", "prompt", handle, "--", text)
    elif enter:
        herdr("pane", "run", handle, "--", text)
    else:  # a menu pick
        herdr("pane", "send-text", handle, "--", text)


def launch(ws_id, agent, prompt, pane=None):
    if not pane:
        root = next(p["pane_id"] for p in herdr("pane", "list", "--workspace", ws_id)["panes"])
        pane = herdr("pane", "split", root, "--direction", "right", "--no-focus")["pane"]["pane_id"]
    # The brief rides in argv so it survives a startup block (Claude's folder-trust prompt is the user's to answer).
    # macOS caps a typed command line at ~1KB, and a newline would split it, so a long brief goes by file.
    if prompt and ("\n" in prompt or len(shlex.quote(prompt).encode()) > 800):
        BRIEFS.mkdir(mode=0o700, parents=True, exist_ok=True)
        brief = BRIEFS / f"{pane.replace(':', '-')}-{int(time.time())}-{secrets.token_hex(4)}.md"
        fd = os.open(str(brief), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as bf:
            bf.write(prompt)
        prompt = f"Your brief is in {brief}. Read it and follow it."
    for i in range(15):
        try:
            herdr("agent", "start", f"{agent}-{pane.replace(':', '-')}", "--kind", agent, "--pane", pane, "--timeout", "60000",
                  *(["--", prompt] if prompt else []))
        except RuntimeError as e:
            if "agent_pane_busy" in str(e) and i < 14:  # a new pane's shell is still starting
                time.sleep(1)
                continue
            if "agent_not_ready" not in str(e):  # blocked at startup: it shows as waiting until the user answers
                raise
        return {"terminal": {"handle": pane}}


def create(repo, name, base=None, comment="", agent=None, prompt=""):
    src = next((w["path"] for w in worktrees() if w["isMainWorktree"] and repo in (w["repoId"], w["repo"])), repo)
    r = herdr("worktree", "create", "--cwd", src, "--branch", name, "--label", name, "--no-focus",
              *(["--base", base] if base else []))
    ws, path = r["workspace"]["workspace_id"], r["worktree"]["path"]
    if comment:
        ade_common.set_goal(path, comment)
    if agent:
        launch(ws, agent, prompt or "", r["root_pane"]["pane_id"])
    return {"worktree": {"id": ws, "path": path}}


def set_goal(worktree_id, goal):
    ade_common.set_goal(next(w["path"] for w in worktrees() if w["id"] == worktree_id), goal)


def remove(worktree_id, force=False):
    ws = next(w for w in worktrees() if w["id"] == worktree_id)
    if ws["isMainWorktree"]:
        herdr("workspace", "close", worktree_id)
    else:  # never --force: herdr, like git, refuses a dirty tree
        herdr("worktree", "remove", "--workspace", worktree_id)
    ade_common.set_goal(ws["path"], "")


def repos():
    return repo_list(worktrees())


def call(*args):
    return ade.dispatch(sys.modules[__name__], *args)
