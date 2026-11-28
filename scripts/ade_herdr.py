"""Read-only herdr import source for importers.py (no longer a live GREENLIGHT_ADE=herdr backend).

Live workspace operations use Worktrunk and ade_basement.py (`GREENLIGHT_ADE=native`).
herdr has workspaces, git worktrees, panes and agent detection of its own; goals live in ~/.greenlight/goals.json
(herdr has no workspace description). GREENLIGHT_HERDR_SESSION targets a named session instead of the default.
"""
import json
import os
import subprocess
import time

from ade_common import git_info, goals, repo_list

NAME = "herdr"
STATES = {"working": "working", "blocked": "waiting", "idle": "done", "done": "done"}  # herdr -> Orca agent state


def herdr(*args, js=True):
    s = os.environ.get("GREENLIGHT_HERDR_SESSION")
    p = subprocess.run(["herdr", *(["--session", s] if s else []), *args], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError(f"herdr {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return json.loads(p.stdout or "{}").get("result", {}) if js else p.stdout


def agents():
    """Live agents by pane id."""
    return {a["pane_id"]: a for a in herdr("agent", "list")["agents"]}


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


def repos():
    return repo_list(worktrees())
