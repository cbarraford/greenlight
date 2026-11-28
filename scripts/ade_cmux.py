"""Read-only cmux import source for importers.py (no longer a live GREENLIGHT_ADE=cmux backend).

Live workspace operations use Worktrunk and ade_basement.py (`GREENLIGHT_ADE=native`).
cmux has workspaces and terminals but no worktrees, repos or agent records, so those come from git
and from cmux's agent hook stores (~/.cmuxterm/*-hook-sessions.json). The goal lives in the
workspace description, as it lives in the Orca comment. Needs cmux's socket in Automation mode
(automation.socketControlMode in ~/.config/cmux/cmux.json).
"""
import json
import os
import subprocess
from pathlib import Path

from ade_common import git_info, repo_list

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


def repos():
    return repo_list(worktrees())
