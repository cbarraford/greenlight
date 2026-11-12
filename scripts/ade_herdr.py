"""herdr backend: answers the `orca ...` calls greenlight makes, in Orca's JSON shape (GREENLIGHT_ADE=herdr).

herdr has workspaces, git worktrees, panes and agent detection of its own; goals live in ~/.greenlight/goals.json
(herdr has no workspace description). GREENLIGHT_HERDR_SESSION targets a named session instead of the default.
"""
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from ade_common import default_agent, draft, flags, git_info, goals, plain, set_goal, repo_list

BRIEFS = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight") / "briefs"
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
        herdr("agent", "prompt", handle, text)
    elif enter:
        herdr("pane", "run", handle, text)
    else:  # a menu pick
        herdr("pane", "send-text", handle, text)


def launch(ws_id, agent, prompt, pane=None):
    if not pane:
        root = next(p["pane_id"] for p in herdr("pane", "list", "--workspace", ws_id)["panes"])
        pane = herdr("pane", "split", root, "--direction", "right", "--no-focus")["pane"]["pane_id"]
    # The brief rides in argv so it survives a startup block (Claude's folder-trust prompt is the user's to answer).
    # macOS caps a typed command line at ~1KB, and a newline would split it, so a long brief goes by file.
    if prompt and ("\n" in prompt or len(shlex.quote(prompt).encode()) > 800):
        brief = BRIEFS / f"{pane.replace(':', '-')}-{int(time.time())}.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(prompt)
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


def create(f):
    src = next((w["path"] for w in worktrees() if w["isMainWorktree"] and f["repo"].removeprefix("id:") in (w["repoId"], w["repo"])), f["repo"].removeprefix("id:"))
    base = f.get("base-branch") or f.get("base")  # Orca's flag is --base-branch
    r = herdr("worktree", "create", "--cwd", src, "--branch", f["name"], "--label", f["name"], "--no-focus",
              *(["--base", base] if base else []))
    ws, path = r["workspace"]["workspace_id"], r["worktree"]["path"]
    if f.get("comment"):
        set_goal(path, f["comment"])
    if f.get("agent"):
        launch(ws, f["agent"], f.get("prompt") or "", r["root_pane"]["pane_id"])
    return {"worktree": {"id": ws, "path": path}}


def call(*args):
    noun, verb, f = *args[:2], flags(args[2:])
    wid = str(f.get("worktree", "")).removeprefix("id:")
    if noun == "worktree" and verb in ("ps", "list"):
        return {"worktrees": worktrees()}
    if noun == "terminal" and verb == "list":
        return {"terminals": terminals()}
    if noun == "terminal" and verb == "read":
        return read(f["terminal"], f.get("limit", 200))
    if noun == "terminal" and verb == "send":
        send(f["terminal"], f.get("text", ""), "enter" in f)
        return {}
    if noun == "agent" and verb == "launch":
        return launch(wid, f.get("agent") or default_agent(), f.get("prompt", ""))
    if noun == "worktree" and verb == "set":  # board status and MR links are Orca-only
        if "comment" in f:
            set_goal(next(w["path"] for w in worktrees() if w["id"] == wid), f["comment"] if isinstance(f["comment"], str) else "")
        return {}
    if noun == "worktree" and verb == "rm":
        ws = next(w for w in worktrees() if w["id"] == wid)
        if ws["isMainWorktree"]:
            herdr("workspace", "close", wid)
        else:  # never --force: herdr, like git, refuses a dirty tree
            herdr("worktree", "remove", "--workspace", wid)
        set_goal(ws["path"], "")
        return {"removed": wid}
    if noun == "worktree" and verb == "create":
        return create(f)
    if noun == "repo" and verb == "list":
        return repo_list(worktrees())
    raise RuntimeError(f"orca {noun} {verb} has no herdr equivalent")
