#!/usr/bin/env python3
"""Where each workspace's agent runs, and which ones aren't on the host greenlight runs on.

    placement.py [list] [--all] [--json] [--expect HOST]   workspaces with an agent (--all: every goal one), the
                                                           machine each is on, and `misplaced` where that isn't HOST
                                                           (default: this machine, or ORCA_ENVIRONMENT's runtime). Exit 1 when any is misplaced.
    placement.py recreate <name> [--execute]               a misplaced workspace again on this machine: the same
                                                           goal, branch and first agent. Prints the plan; --execute runs it

Chief creates workspaces through `greenlight.py ade worktree create`, which pins them here (greenlight.place_here).
An Orca workspace's agent runs in its Orca runtime's terminals, not in `tmux -L greenlight` (that's the Basement's):
on an Orca host, `orca worktree ps` (or this script) is the check, not `tmux ls`. A recreated workspace leaves the
old one where it is: delete it once the new one has picked up the work.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diag  # noqa: E402
import greenlight  # noqa: E402

_MACHINES: dict = {"at": 0.0, "map": {}}
MACHINES_TTL = 300  # dash.py's stream asks every few seconds; hosts change when you pair one


def machines(fresh=False):
    """{hostId: machine name} for the ADE in use. Only Orca has hosts; every other ADE runs where greenlight does.
    An Orca that can't say keeps `local` as this machine, or as ORCA_ENVIRONMENT's name when that picks another runtime."""
    if os.environ.get("GREENLIGHT_ADE", "orca") != "orca":
        return {"local": greenlight.here()}
    if not fresh and _MACHINES["map"] and time.time() - _MACHINES["at"] < MACHINES_TTL:
        return _MACHINES["map"]
    try:
        hosts = greenlight.orca("host", "list", timeout=20).get("hosts") or []
    except Exception:  # noqa: BLE001 - an older Orca with no `host list`, or one that isn't up
        hosts = []
    out = {h["id"]: h.get("machineName") or h.get("name") or h["id"] for h in hosts if isinstance(h, dict) and h.get("id")}
    out["local"] = out.get("local") or os.environ.get("ORCA_ENVIRONMENT") or greenlight.here()
    _MACHINES.update(at=time.time(), map=out)
    return out


def expected(ms):
    """The machine agents belong on: this one, or the runtime ORCA_ENVIRONMENT names (the user picked it)."""
    return ms["local"] if os.environ.get("ORCA_ENVIRONMENT") else greenlight.here()


def host_of(w, ms):
    """The machine a worktree (an `orca worktree ps` row) lives on, or its host id when Orca didn't name it."""
    hid = w.get("hostId") or "local"
    return ms.get(hid) or hid


def is_misplaced(w, ms, expect, repo_target="server"):
    """True when `w` is not on the machine its repo expects (`mac` repos belong off a Linux server; `server` repos belong on `expect`)."""
    on_expect = greenlight.same_host(host_of(w, ms), expect)
    if repo_target == "mac" and sys.platform != "darwin":
        return on_expect
    return not on_expect


def rows(worktrees, ms, expect, every=False):
    """One row per workspace with an agent (every: or a goal), main checkouts and archived ones left out."""
    main_paths = {w.get("repo"): w.get("path") for w in worktrees if w.get("isMainWorktree") and w.get("repo") and w.get("path")}
    out = []
    for w in worktrees:
        if w.get("isMainWorktree") or w.get("isArchived") or not (w.get("agents") or every and w.get("comment")):
            continue
        host = host_of(w, ms)
        ag = greenlight.primary_agent(w.get("agents"))
        repo = w.get("repo") or ""
        target = greenlight.resolve_repo_host(repo, path=main_paths.get(repo))["target"] if repo else "server"
        out.append({"name": w.get("displayName") or w.get("path"), "id": w.get("worktreeId"), "host": host,
                    "agent": (ag or {}).get("state") or "none", "branch": (w.get("branch") or "").removeprefix("refs/heads/"),
                    "misplaced": is_misplaced(w, ms, expect, repo_target=target)})
    return sorted(out, key=lambda r: (not r["misplaced"], r["name"] or ""))


def setups():
    """Orca's project setups by repoId: which project each checkout is, so a recreate picks the same repo."""
    return {x.get("repoId"): x for x in greenlight.project_setups() or []}


def local_repo(worktrees, ms, w, expect, sts):
    """The main checkout of w's repo on the expected machine: the same Orca project when Orca says which, else the
    same repo name."""
    proj = (sts.get(w.get("repoId")) or {}).get("projectId")
    def same_repo(m):
        return (sts.get(m.get("repoId")) or {}).get("projectId") == proj if proj else m.get("repo") == w.get("repo")
    return next((m for m in worktrees if m.get("isMainWorktree") and same_repo(m) and greenlight.same_host(host_of(m, ms), expect)), None)


def on_origin(path, branch):
    """True when origin, as the checkout at path sees it, has exactly refs/heads/<branch> (a pattern would also match
    someone/<branch>). False when it hasn't, or didn't answer."""
    try:
        greenlight.run(["git", "-C", path, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"], timeout=60)
        return True
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def fetch_cmd(path, branch):
    """Fetch branch into origin/<branch> even where the checkout's refspec leaves it out (a --single-branch clone)."""
    return ["git", "-C", path, "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]


def plan(worktrees, ms, name, expect, sts=None):
    """{cmd, fetch, after} to make workspace `name` again on `expect`, or {error}. Its branch has to be on origin:
    the new checkout starts from it, so pushed work carries over and nothing is guessed."""
    w = next((x for x in worktrees if name in (x.get("displayName"), x.get("path"), x.get("worktreeId"))
              and not x.get("isMainWorktree") and not x.get("isArchived")), None)
    if not w:
        return {"error": f"no workspace named {name}"}
    src = host_of(w, ms)
    if greenlight.same_host(src, expect):
        return {"error": f"{name} is already on {expect}"}
    main = local_repo(worktrees, ms, w, expect, sts if sts is not None else setups())
    if not main:
        return {"error": f"{w.get('repo') or 'its repo'} has no checkout on {expect}: add it to the ADE there first"}
    branch, goal = (w.get("branch") or "").removeprefix("refs/heads/"), (w.get("comment") or "").strip()
    if not branch or not on_origin(main["path"], branch):
        return {"error": f"{name}'s branch {branch or '(none)'} isn't on origin (or origin didn't answer): push it from {src} "
                         "first, so its work comes along"}
    agent = (greenlight.CONFIG.get("agents") or ["claude"])[0]
    brief = (f"This workspace replaces {w.get('displayName')} on {src}, which ran on the wrong machine. It starts from "
             f"origin/{branch}: switch to {branch} tracking it and push only there. Commits or changes the old workspace "
             f"never pushed aren't here; ask me before redoing any. "
             + (f"Carry on with its goal: {goal}" if goal else "Ask me what it was for before changing anything."))
    cmd = ["worktree", "create", "--repo", f"id:{main['repoId']}", "--name", w.get("displayName") or branch,
           "--base-branch", f"origin/{branch}", "--setup", "skip", "--no-parent",
           *(["--comment", goal] if goal else []), "--agent", agent, "--prompt", brief]
    return {"from": src, "to": expect, "fetch": fetch_cmd(main["path"], branch), "cmd": cmd,
            "after": f"delete {w.get('displayName')} on {src} once the new one has the work"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("cmd", nargs="?", default="list", choices=("list", "recreate"))
    p.add_argument("name", nargs="?")
    p.add_argument("--expect", default=None, help="the machine agents should run on (default: this one)")
    p.add_argument("--all", action="store_true", help="also workspaces with a goal and no agent")
    p.add_argument("--json", action="store_true")
    p.add_argument("--execute", action="store_true", help="recreate: run the plan, not just print it")
    a = p.parse_args(argv)
    if a.cmd == "recreate" and not a.name:  # before any Orca call
        p.error("recreate needs a workspace name")
    if a.cmd == "recreate" and (a.expect or os.environ.get("ORCA_ENVIRONMENT")):  # its git checks and fetch run here
        print(f"recreate makes the workspace on this machine ({greenlight.here()}): run it on the one it belongs on, without "
              "--expect or ORCA_ENVIRONMENT", file=sys.stderr)
        return 2
    with diag.span("placement", op=a.cmd):
        ms = machines(fresh=True)
        expect = a.expect or expected(ms)
        wts = greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
    if a.cmd == "list":
        rs = rows(wts, ms, expect, every=a.all)
        if a.json:
            print(json.dumps({"expect": expect, "rows": rs}, indent=1))
        else:
            for r in rs:
                print(f"{'MISPLACED' if r['misplaced'] else 'ok':9} {r['host'][:20]:20} {r['agent']:8} {r['name']}")
            print(f"{sum(r['misplaced'] for r in rs)} of {len(rs)} not on {expect}")
        return 1 if any(r["misplaced"] for r in rs) else 0
    pl = plan(wts, ms, a.name, expect)
    if "error" in pl:
        print(pl["error"], file=sys.stderr)
        return 2
    if not a.execute:
        print(json.dumps(pl, indent=1))
        return 0
    try:  # origin/<branch> as the base needs it here
        greenlight.run(pl["fetch"], timeout=300)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"couldn't fetch the branch, so nothing was created: {e}", file=sys.stderr)
        return 2
    print(json.dumps(greenlight.orca(*pl["cmd"]), indent=1))
    print(pl["after"])
    return 0


if __name__ == "__main__":
    diag.install("placement")
    sys.exit(main())
