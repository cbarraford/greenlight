"""T3 Code backend: answers the `orca ...` calls greenlight makes, in Orca's JSON shape (GREENLIGHT_ADE=t3).

A workspace is a T3 project (one git worktree each); its threads are both its agents and its "terminals", so a
terminal handle is a thread id. Goals live in ~/.greenlight/goals.json (T3 has no project description). Needs the
T3 Code server running (the desktop app, or `t3 serve`).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ade_common import default_agent, flags, git_info, goals, set_goal, worktree_add, worktree_remove, repo_list

HOME = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3")
T3 = shutil.which("t3") or str(HOME / "bin" / "t3")
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


def create(f):
    repo = next((w["repoId"] for w in worktrees() if f["repo"].removeprefix("id:") in (w["repoId"], w["repo"])), f["repo"].removeprefix("id:"))
    path = worktree_add(repo, f["name"], f.get("base-branch") or f.get("base"))
    pid = t3("project", "add", "--title", f["name"], path).split()[2]  # "Added project <id> (<title>) at <path>."
    if f.get("comment"):
        set_goal(path, f["comment"])
    if f.get("agent"):
        launch(pid, f["agent"], f.get("prompt") or "")
    return {"worktree": {"id": pid, "path": path}}


def call(*args):
    noun, verb, f = *args[:2], flags(args[2:])
    wid = str(f.get("worktree", "")).removeprefix("id:")
    if noun == "worktree" and verb in ("ps", "list"):
        return {"worktrees": worktrees()}
    if noun == "terminal" and verb == "list":
        return {"terminals": terminals()}
    if noun == "terminal" and verb == "read":
        return read(f["terminal"], f.get("limit", 200))
    if noun == "terminal" and verb == "send":  # a thread takes whole messages; a busy one queues it as its next turn
        t3("thread", "send", f["terminal"], "-", stdin=f.get("text", ""))
        return {}
    if noun == "agent" and verb == "launch":
        return launch(wid, f.get("agent") or default_agent(), f.get("prompt", ""))
    if noun == "worktree" and verb == "set":  # board status and MR links are Orca-only
        if "comment" in f:
            root = next(r for p, _, r in projects() if p == wid)
            set_goal(root, f["comment"] if isinstance(f["comment"], str) else "")
        return {}
    if noun == "worktree" and verb == "rm":
        ws = next(w for w in worktrees() if w["id"] == wid)
        worktree_remove(ws)  # first: git refuses a dirty tree, and then the project stays
        t3("project", "remove", "--force", wid)  # --force: drop its threads too, as Orca drops terminals
        set_goal(ws["path"], "")
        return {"removed": wid}
    if noun == "worktree" and verb == "create":
        return create(f)
    if noun == "repo" and verb == "list":
        return repo_list(worktrees())
    raise RuntimeError(f"orca {noun} {verb} has no t3 equivalent")
