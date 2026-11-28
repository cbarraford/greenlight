"""Read-only T3 Code import source for importers.py (no longer a live GREENLIGHT_ADE=t3 backend).

Live workspace operations use Worktrunk and ade_basement.py (`GREENLIGHT_ADE=native`).
A workspace is a T3 project (one git worktree each); its threads are its agents. Goals live in
~/.greenlight/goals.json (T3 has no project description). Needs the T3 Code server running
(the desktop app, or `t3 serve`).
"""
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from ade_common import git_info, goals, repo_list

HOME = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
T3 = shutil.which("t3") or str(HOME / "bin" / "t3")
NAME = "t3"
AGENTS = {"claudeAgent": "claude"}  # T3 provider id -> greenlight's agent name, where they differ
WORKING = {"running", "starting"}


def t3(*args, stdin=None):
    p = subprocess.run([T3, *args], capture_output=True, text=True, timeout=120, input=stdin)
    if p.returncode:
        raise RuntimeError(f"t3 {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000) if iso else 0


def projects():
    # ponytail: the CLI has no `project list`, so read T3's own state DB (read-only); swap in the CLI if it grows one
    db = sqlite3.connect((HOME / "userdata" / "statev2.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        return db.execute("select project_id, title, workspace_root from projection_projects where deleted_at is null").fetchall()
    finally:
        db.close()


def threads(pid):
    return json.loads(t3("thread", "list", "--project", pid, "--json"))


def state(t):
    if t.get("pendingRequestCount") or t["status"] == "waiting":
        return "waiting"
    return "working" if t["status"] in WORKING else "done"


def worktrees():
    g, out = goals(), []
    for pid, title, root in projects():
        ts = sorted(threads(pid), key=lambda t: t["updatedAt"], reverse=True)
        if ts:  # the newest thread is the one greenlight acts on: fetch its pending requests and last reply
            shown = json.loads(t3("thread", "show", ts[0]["threadId"], "--json", "--messages", "4"))
            ts[0] = shown["thread"] | {"last": next((m["text"] for m in reversed(shown["messages"]) if m["role"] == "assistant"), "")}
        out.append({
            **git_info(root), "id": pid, "worktreeId": pid, "displayName": title, "comment": g.get(root, ""),
            "isPinned": False, "linkedGitLabMR": None, "lastOutputAt": ms(ts[0]["updatedAt"]) if ts else None,
            "agents": [{"state": state(t), "agentType": AGENTS.get(t.get("providerInstanceId"), t.get("providerInstanceId")), "lastAssistantMessage": t.get("last", ""),
                        "updatedAt": ms(t["updatedAt"]), "stateStartedAt": ms(t["updatedAt"])} for t in ts],
        })
    return out


def repos():
    return repo_list(worktrees())
