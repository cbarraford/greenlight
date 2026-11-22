#!/usr/bin/env python3
"""What I'm blocking on GitLab, by a team's review rules. Off unless config.json has a `blocking` roster:

    "blocking": {"repos": {"acme/widgets": "Release-", "acme/tools": null},   # null: every open MR; a prefix: only MRs in that milestone
                 "people": ["me", "bob"], "teams": {"dev-team": ["me", "bob"]},  # teams: approval-rule name -> members
                 "lead": "me"}                                                   # who gets orphans (default: you)

    blocking.py [--json] [--fresh]   # the open MRs I'm blocking now and why

It walks every open, non-draft MR in REPOS (a repo with a prefix: only MRs in an active <prefix> milestone) and pings
whoever owns the next action. The checks run in order and the first that blocks ends the walk:
  conflict    the owner (first known assignee, else the author) must rebase
  pipeline    the newest pipeline isn't green: a failed job -> the owner; still running -> nobody yet
  discussion  each unresolved thread -> everyone in it plus the owner, minus whoever spoke last
  approval    approvals left -> rule-team members who haven't approved, reviewed, or commented since the
              last push; if that's nobody, the owner ("needs N more approvals")
  orphan      a milestone-repo MR that pinged nobody and isn't waiting on CI -> the lead dev (me)
Orphans on MRs I've already approved are dropped, and so is an
approval ask on an MR GitLab won't let me approve (my commits are in it).

Cached in ~/.greenlight/blocks.json for CACHE seconds: adopt.py scans, dash.py and cleanup.py only read the file.
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import greenlight  # noqa: E402

ROSTER = greenlight.CONFIG["blocking"] or {}
ME = greenlight.ME
LEAD = ROSTER.get("lead") or ME
FILE = greenlight.STATE / "blocks.json"
CACHE = 300  # precheck and apply run back to back each automation tick: one scan serves both
REPOS = ROSTER.get("repos") or {}
PEOPLE = set(ROSTER.get("people") or ())
TEAMS = {k: set(v) for k, v in (ROSTER.get("teams") or {}).items()}
PENDING = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def owner(mr):
    return next((a["username"] for a in mr.get("assignees") or [] if a["username"] in PEOPLE), mr["author"]["username"])


def check(proj, mr):
    """-> [(user, reason, why)] the rules would ping about this MR, orphan included."""
    base = f"projects/{greenlight.pid(proj)}/merge_requests/{mr['iid']}"
    own, milestone = owner(mr), bool(REPOS.get(proj))
    hits, mentioned, pending, note = [], False, False, None

    def orphan():
        if not milestone or mentioned or pending:
            return []
        ap = greenlight.gl(f"{base}/approvals")
        if any(a["user"]["username"] == ME for a in ap.get("approved_by", [])):
            return []
        return [(LEAD, "orphan", f"falling through the cracks ({note or 'nobody to notify'})")]

    # ponytail: a review-bot-outage classifier is skipped: such a thread counts as an ordinary one here, and an
    # unknown-owner MR with one is orphaned where the original rules stay quiet. Add one if that shows up.
    if own not in PEOPLE:
        note = f"unknown author @{mr['author']['username']}"
        return orphan()
    if mr.get("has_conflicts"):
        return [(own, "conflict", f"needs a rebase onto {mr['target_branch']}")]
    pipes = greenlight.gl(f"{base}/pipelines?per_page=1")
    if pipes and pipes[0]["status"] not in ("success", "skipped", "manual"):
        pending = pipes[0]["status"] in PENDING
        try:
            jobs = greenlight.gl_all(f"projects/{greenlight.pid(proj)}/pipelines/{pipes[0]['id']}/jobs?scope[]=failed")
        except Exception:  # noqa: BLE001 - a pipeline we can't read doesn't block, go on to threads
            jobs = None
        if jobs is not None:
            failed = sorted((j for j in jobs if j["status"] == "failed" and not j.get("allow_failure")), key=lambda j: -j["id"])
            if failed:
                return [(own, "pipeline", f"CI failed: {failed[0]['stage']}")]
            note = "pipeline"
            return orphan()
    blocked = False
    for d in greenlight.gl_all(f"{base}/discussions"):
        notes = [n for n in d["notes"] if n.get("resolvable")]
        if not notes or all(n.get("resolved") for n in notes):
            continue
        last = notes[-1]["author"]["username"]
        who = ({n["author"]["username"] for n in notes} | {own}) - {last}
        if not who:
            continue
        blocked, note = True, "discussion"
        hits += [(u, "discussion", f"@{last} is waiting on you in a thread") for u in who if u in PEOPLE]
    mentioned = bool(hits)
    if not blocked:
        ap = greenlight.gl(f"{base}/approvals")
        left = ap.get("approvals_left") or 0
        if not left:
            return []  # ready: the rules say "ship it" to the lead dev, which isn't a block
        approved = {a["user"]["username"] for a in ap.get("approved_by", [])}
        reviewed = set()
        try:  # fail open: an error nags rather than mutes
            reviewed |= {r["user"]["username"] for r in greenlight.gl(f"{base}/reviewers")
                         if r.get("state") in ("reviewed", "requested_changes")}
        except Exception:  # noqa: BLE001
            pass
        try:
            since = ts(greenlight.gl(f"{base}/commits?per_page=1")[0]["committed_date"])
            reviewed |= {n["author"]["username"] for n in greenlight.gl(f"{base}/notes?sort=desc&per_page=100")
                         if not n.get("system") and ts(n["created_at"]) > since}
        except Exception:  # noqa: BLE001
            pass
        names = set().union(*(TEAMS.get(r["name"], set()) for r in ap.get("approval_rules_left") or []))
        names -= {mr["author"]["username"]} | approved | reviewed
        if not ap.get("user_can_approve", True):  # glab is me: my commits are in it, so GitLab won't take my approval
            names.discard(ME)  # can't approve it, so it's no block on me
        hits = [(u, "approval", "awaiting your review") for u in names] if names else \
            [(own, "approval", f"needs {left} more approval{'s' if left > 1 else ''}")]
        mentioned, note = True, "approval"
    return hits + orphan()


def open_mrs(proj, prefix):
    p = greenlight.pid(proj)
    if prefix:
        mrs = {}
        for s in greenlight.gl_all(f"projects/{p}/milestones?state=active"):
            if s["title"].startswith(prefix):
                for m in greenlight.gl_all(f"projects/{p}/milestones/{s['id']}/merge_requests"):
                    mrs.setdefault(m["iid"], m)
        mrs = list(mrs.values())
    else:
        mrs = greenlight.gl_all(f"projects/{p}/merge_requests?state=opened")
    pid_num = greenlight.gl(f"projects/{p}")["id"]
    return [m for m in mrs if m["state"] == "opened" and not m.get("draft") and m["project_id"] == pid_num]


def scan():
    """{"<project>!<iid>": {reason, why, n, url, title}} for every MR blocked on me. Raises when a repo can't be
    listed (no answer is not "nothing blocked")."""
    jobs = [(proj, m) for proj, pre in REPOS.items() for m in open_mrs(proj, pre)]

    def safe(job):
        try:
            return check(*job), True
        except Exception:  # noqa: BLE001 - ponytail: an MR whose check errored isn't orphaned; a GitLab blip
            return [], False  # shouldn't make a hex, so it's skipped until the next scan

    out, errs = {}, 0
    with ThreadPoolExecutor(8) as ex:
        for (proj, m), (hits, ok) in zip(jobs, ex.map(safe, jobs)):
            if not ok:
                errs += 1
                continue
            for u, reason, why in hits:
                if u != ME:
                    continue
                b = out.setdefault(f"{proj}!{m['iid']}", {"reason": reason, "why": why, "n": 0,
                                                         "url": m["web_url"], "title": m["title"]})
                if b["reason"] == reason:  # first reason wins; threads are counted
                    b["n"] += 1
    if jobs and errs == len(jobs):
        raise RuntimeError(f"all {errs} MR blocking checks failed")
    for b in out.values():
        if b["reason"] == "discussion" and b["n"] > 1:
            b["why"] = f"{b['n']} threads are waiting on you"
    return out


def blocks(fresh=False):
    """The cached scan when younger than CACHE, else a new one; None when GitLab couldn't answer; {} when off."""
    if not REPOS:
        return {}
    c = cached(CACHE)
    if c is not None and not fresh:
        return c
    try:
        b = scan()
    except Exception as e:  # noqa: BLE001
        print(f"blocking scan failed: {e}", file=sys.stderr)
        return None
    import os, uuid
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps({"ts": int(time.time()), "blocks": b}, indent=1))
    os.replace(tmp, FILE)
    return b


def cached(max_age=6 * 3600):
    """The last scan's blocks, or None when there's none younger than max_age (a stopped automation
    mustn't pin "needs you" on a hex forever)."""
    if not REPOS:
        return {}
    try:
        c = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return None
    return c.get("blocks") if time.time() - c.get("ts", 0) <= max_age else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="scan now even if the cache is young")
    a = ap.parse_args()
    b = blocks(fresh=a.fresh)
    if b is None:
        sys.exit("blocking: GitLab couldn't answer, so this is no answer (not 'nothing blocked')")
    if a.json:
        print(json.dumps(b, indent=1))
        return
    order = ["approval", "discussion", "orphan", "conflict", "pipeline"]  # what I do first, then what's mine to fix
    for k, v in sorted(b.items(), key=lambda kv: (order.index(kv[1]["reason"]), kv[0])):
        print(f"- [{v['reason']}] {k} {v['title'][:70]} — {v['why']}\n  {v['url']}")
    if not b:
        print("nothing is blocked on me")


if __name__ == "__main__":
    main()
