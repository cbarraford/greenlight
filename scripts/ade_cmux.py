"""Read-only cmux import source for importers.py (no longer a live GREENLIGHT_ADE=cmux backend).

Live workspace operations use Worktrunk and ade_basement.py (`GREENLIGHT_ADE=native`).
cmux has workspaces and terminals but no worktrees, repos or agent records, so those come from git
and from cmux's agent hook stores (~/.cmuxterm/*-hook-sessions.json). The goal lives in the
workspace description, as it lives in the Orca comment. Needs cmux's socket in Automation mode
(automation.socketControlMode in ~/.config/cmux/cmux.json).
"""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import ade
import harness
from ade_common import draft, git_info, worktree_add, worktree_remove, repo_list

HOOKS = Path.home() / ".cmuxterm"  # <agent>-hook-sessions.json, written by `cmux hooks setup <agent>`
NAME = "cmux"
STATES = {"running": "working", "needsInput": "waiting", "idle": "done"}  # cmux lifecycle -> Orca agent state


def cmux(*args, js=True):
    env = os.environ | {"CMUX_QUIET": "1"}
    p = subprocess.run(["cmux", "--id-format", "uuids", *(["--json"] if js else []), *args],
                       capture_output=True, text=True, timeout=60, env=env)
    if p.returncode:
        raise RuntimeError(f"cmux {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return json.loads(p.stdout) if js else p.stdout


def sessions():
    """Live agent sessions by surface id."""
    out = {}
    for f in HOOKS.glob("*-hook-sessions.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        live = {v["sessionId"] for v in d.get("activeSessionsBySurface", {}).values()}
        out |= {s["surfaceId"]: s for s in d.get("sessions", {}).values() if s.get("sessionId") in live}
    return out


def terminals():
    agents = sessions()
    out = []
    for win in cmux("tree", "--all")["windows"]:
        for ws in win["workspaces"]:
            for pane in ws["panes"]:
                for s in pane["surfaces"]:
                    if s["type"] == "terminal":
                        ag = agents.get(s["id"], {})
                        out.append({"handle": s["id"], "worktreeId": ws["id"], "title": s.get("title"),
                                    "agentIdentity": (ag.get("launchCommand") or {}).get("launcher") or ("claude" if ag else None),
                                    # ponytail: no hook, no timestamp, so cleanup's "active in the last 30m" guard
                                    # can't see a busy plain shell; diff read-screen snapshots if that bites
                                    "lastOutputAt": ag.get("updatedAt") and ag["updatedAt"] * 1000})
    return out


def worktrees():
    agents = sessions()
    out = []
    for win in cmux("list-windows"):
        for w in cmux("workspace", "list", "--window", win["id"])["workspaces"]:
            path = w.get("current_directory") or ""
            ags = sorted((a for a in agents.values() if a.get("workspaceId") == w["id"]),
                         key=lambda a: a.get("updatedAt") or 0, reverse=True)
            out.append({
                **git_info(path), "id": w["id"], "worktreeId": w["id"], "displayName": w["title"],
                "comment": w.get("description") or "", "isPinned": w.get("pinned"), "linkedGitLabMR": None,
                "lastOutputAt": (ags[0].get("updatedAt") or 0) * 1000 if ags else None,
                "agents": [{"state": STATES.get(a.get("agentLifecycle"), "unknown"),
                            "agentType": (a.get("launchCommand") or {}).get("launcher") or "claude",
                            **({"sessionId": a["sessionId"]} if isinstance(a.get("sessionId"), str) and a["sessionId"] else {}),
                            "lastAssistantMessage": a.get("lastBody") or "", "updatedAt": (a.get("updatedAt") or 0) * 1000,
                            "stateStartedAt": (a.get("updatedAt") or 0) * 1000} for a in ags],
            })
    return out


def read(handle, limit):
    # ponytail: read-screen is plain text, so Claude's dim prompt suggestion reads as a draft and greenlight won't
    # type over it; herdr reads ANSI and drops it. Fix if cmux grows an ANSI read
    lines = cmux("read-screen", "--surface", handle, "--lines", str(limit))["text"].splitlines()
    return {"terminal": {"handle": handle, "tail": lines, "draft": draft(lines)}}


def send(handle, text, enter):
    if enter:  # terminal.paste keeps a multi-line brief one message and submits it; typed newlines would each submit
        cmux("rpc", "terminal.paste", json.dumps({"surface_id": handle, "text": text}))
    else:  # ponytail: typed, for a menu pick; terminal.paste always submits
        cmux("send", "--surface", handle, "--", text, js=False)


def launch(ws_id, agent, prompt):
    cwd = next((w.get("path") for w in worktrees() if w["id"] == ws_id), None)  # agy and codex bypass trust it for the session
    s = cmux("new-pane", "--type", "terminal", "--workspace", ws_id, "--focus", "false")["surface_id"]
    time.sleep(2)  # ponytail: fixed wait for the shell prompt; poll read-screen if 2s ever proves short
    send(s, shlex.join(harness.interactive(agent, prompt, cwd=cwd)), True)
    return {"terminal": {"handle": s}}


def create(repo, name, base=None, comment="", agent=None, prompt=""):
    repo = next((w["repoId"] for w in worktrees() if repo in (w["repoId"], w["repo"])), repo)
    path = worktree_add(repo, name, base)
    ws = cmux("workspace", "create", "--name", name, "--cwd", path, "--focus", "false",
              *(["--description", comment] if comment else []))
    ws_id = ws.get("workspace_id") or ws.get("id")
    if agent:
        launch(ws_id, agent, prompt)
    return {"worktree": {"id": ws_id, "path": path}}


def set_goal(worktree_id, goal):  # the goal is the workspace description
    cmux("workspace-action", "--workspace", worktree_id, "--action", "set-description" if goal else "clear-description",
         *(["--description", goal] if goal else []), js=False)


def remove(worktree_id, force=False):  # never --force: cmux has no undo for a dirty tree
    ws = next(w for w in worktrees() if w["id"] == worktree_id)
    worktree_remove(ws)
    cmux("workspace", "close", ws["id"], js=False)


def repos():
    return repo_list(worktrees())


def call(*args):
    return ade.dispatch(sys.modules[__name__], *args)
