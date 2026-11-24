"""T3 Code backend (GREENLIGHT_ADE=t3, a legacy live driver): ade.py's contract answered by T3 Code.

A workspace is a T3 project (one git worktree each); its threads are both its agents and its "terminals", so a
terminal handle is a thread id. Goals live in ~/.greenlight/goals.json (T3 has no project description). Needs the
T3 Code server running (the desktop app, or `t3 serve`).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import ade
import ade_common
from ade_common import git_info, goals, worktree_add, worktree_remove, repo_list

HOME = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
T3 = shutil.which("t3") or str(HOME / "bin" / "t3")
NAME = "t3"
PROVIDERS = {"claude": "claudeAgent"}  # greenlight's agent name -> T3 provider id, where they differ
AGENTS = {v: k for k, v in PROVIDERS.items()}
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


def terminals():
    now = int(time.time() * 1000)
    return [{"handle": t["threadId"], "worktreeId": pid, "agentIdentity": AGENTS.get(t.get("providerInstanceId"), t.get("providerInstanceId")),
             "lastOutputAt": now if t["status"] in WORKING else ms(t["updatedAt"])}
            for pid, _, _ in projects() for t in threads(pid)]


def read(handle, limit):
    shown = json.loads(t3("thread", "show", handle, "--json", "--messages", "20"))
    lines = [ln for m in shown["messages"] for ln in f"{'❯' if m['role'] == 'user' else '⏺'} {m['text']}".splitlines()]
    # ponytail: T3's composer isn't readable from the CLI, so an unsent draft there is invisible to greenlight
    return {"terminal": {"handle": handle, "tail": lines[-int(limit):], "draft": ""}}


def launch(pid, agent, prompt):
    provider = PROVIDERS.get(agent, agent)
    manifest = json.loads((HOME / "userdata" / "model-manifest.json").read_text())["manifest"]
    model = manifest["currentModels"][provider][0]  # ponytail: T3's first current model; add a knob if it's the wrong pick
    r = json.loads(t3("thread", "start", pid, "-", "--provider", provider, "--model", model, "--json",
                      stdin=prompt or "Stand by for instructions."))
    return {"terminal": {"handle": r["threadId"]}}


def create(repo, name, base=None, comment="", agent=None, prompt=""):
    repo = next((w["repoId"] for w in worktrees() if repo in (w["repoId"], w["repo"])), repo)
    path = worktree_add(repo, name, base)
    pid = t3("project", "add", "--title", name, path).split()[2]  # "Added project <id> (<title>) at <path>."
    if comment:
        ade_common.set_goal(path, comment)
    if agent:
        launch(pid, agent, prompt or "")
    return {"worktree": {"id": pid, "path": path}}


def send(handle, text, enter):  # a thread takes whole messages; a busy one queues it as its next turn
    t3("thread", "send", handle, "-", stdin=text)


def set_goal(worktree_id, goal):  # T3 has no project description: goals.json, by the project's root
    ade_common.set_goal(next(r for p, _, r in projects() if p == worktree_id), goal)


def remove(worktree_id, force=False):
    ws = next(w for w in worktrees() if w["id"] == worktree_id)
    worktree_remove(ws)  # first: git refuses a dirty tree, and then the project stays
    t3("project", "remove", "--force", worktree_id)  # --force: drop its threads too, as Orca drops terminals
    ade_common.set_goal(ws["path"], "")


def repos():
    return repo_list(worktrees())


def call(*args):
    return ade.dispatch(sys.modules[__name__], *args)
