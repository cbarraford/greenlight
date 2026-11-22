#!/usr/bin/env python3
"""dash: live data for the Greenlight left pane (app/dashboard.html).

    dash.py snapshot          one JSON line: every workspace as a puck
    dash.py stream [secs]     a snapshot every `secs` (default 10); records state changes as history
    dash.py page <ws> [--fresh] [--model M] [--effort E]  one JSON line: facts + an AI-designed layout for that workspace's page
    dash.py suggest <ws> [--model M] [--effort E]  one JSON line: the next prompt to give an agent there (the console drawer, the
                                                    hover card's button); cached per state, and snapshots carry it as `suggestion`
    dash.py run <ws> < prompt                       launch an agent in that workspace with the prompt on stdin
    dash.py transcript <ws> [--before CURSOR]       one JSON line: the agents' conversation there, newest page first (the agent card's All output)
    dash.py trees                                   one JSON line: every worktree of every installed ADE (orca, cmux, herdr, t3), for the terminal's sidebar
    dash.py tree info|rm|goal|cfg|new|diff <path> [--force]  the sidebar's workspace menu: git facts and listening ports; remove
                                                    the worktree (refuses a dirty, unpushed or pinned one without --force; stdin: the
                                                    teardown to run); set the goal (stdin); write the main checkout's .greenlight.json
                                                    (stdin); a new worktree of path's repo (stdin {name, base, goal, agent, setup, answers, force}: as task new);
                                                    the diff (stdin: head | branch)
    dash.py ask <ws> [--model M] [--effort E]       one JSON line: the waiting agent's question and the answers to offer
    dash.py answer <ws> < text                      answer the waiting agent (a menu choice number, or a reply + Enter)
    dash.py park <ws> on|off                        add or drop "(parked)" on that workspace's goal (the left dock)
    dash.py task repos -                            one JSON line: the repos a task can go to (New task)
    dash.py task new <repo> < {text, files}         start a workspace on that repo with an agent working the request; files are data URLs.
                                                    A goal lifestage.preflight finds ambiguous answers {paused, questions} instead;
                                                    send it again with answers {id: text}, or force: true to start anyway

Pucks join ~/.greenlight/last.json (lifecycle, MR, next step from greenlight.py scan, refreshed by every greenlight run; the interval is in Greenlight Settings → Dashboard)
with a live `orca worktree ps` (agent state, seconds old). Never calls GitLab for the overview.
Page layouts come from Claude Haiku picking widgets out of CATALOG; the page renders only known
widgets, so a bad layout degrades to fewer cards, never to executed output.
"""
import base64
import calendar
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ade_common  # noqa: E402
import audit  # noqa: E402
import blocking  # noqa: E402
import diag  # noqa: E402
import greenlight  # noqa: E402
import harness  # noqa: E402
import lifestage  # noqa: E402

DASH = greenlight.STATE / "dash"
NAME_RE = re.compile(r"^[\w.@+/-]{1,200}$")
HIST_RE = re.compile(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) (.*)")

# Puck states, most urgent first. Size in the overview follows this order.
STATES = ["needs", "crashed", "raised", "working", "queued", "quiet", "done"]

CATALOG = {
    # widget: (props, when to use). P = also fits inside a puck.
    "text": ('{"text": str}', "P one short line of what is happening"),
    "stat": ('{"value": str, "label": str}', "P one big number or value"),
    "bar": ('{"done": num, "total": num, "label": str}', "P done out of total"),
    "stack": ('{"parts": [{"label": str, "value": num, "tone": tone}]}', "P parts of a whole"),
    "grid": ('{"cells": [tone], "cols": int}', "P many small pass/fail results"),
    "steps": ('{"steps": [str], "current": int}', "P a sequence with a current step (0-based)"),
    "meter": ('{"value": 0-100, "label": str}', "P a level against a target"),
    "spark": ('{"points": [num], "label": str}', "P a trend over time"),
    "kv": ('{"rows": [[str, str]]}', "labelled facts"),
    "list": ('{"items": [{"text": str, "detail": str, "tone": tone}]}', "a short list of things"),
    "ranked": ('{"items": [{"label": str, "value": 0-100}]}', "options or hypotheses ranked by weight"),
    "timeline": ('{"items": [{"ts": "YYYY-MM-DD HH:MM", "text": str, "tone": tone}]}', "dated events, newest first"),
    "files": ('{"files": [{"path": str, "add": int, "del": int}]}', "files changed"),
    "log": ('{"lines": [str]}', "raw output or transcript lines"),
    "checklist": ('{"items": [{"text": str, "done": bool}]}', "things to do with done state"),
    "links": ('{"links": [{"label": str, "url": str}]}', "where to go: MR, pipeline, docs"),
    "callout": ('{"text": str, "tone": tone}', "the one thing that matters most right now"),
    "heatmap": ('{"rows": [{"label": str, "cells": [tone]}]}', "results per item over runs"),
    "table": ('{"columns": [str], "rows": [[str]]}', "small comparisons"),
    "note": ('{"text": str}', "a paragraph of explanation"),
}
TONES = "ok | warn | bad | info | idle"
# The page's only generated part: up to 3 equal tiles plus one wide panel. Everything else on it is fixed markup.
TILES = ("stat", "meter", "bar", "spark", "grid", "stack", "note")
WIDE = ("files", "list")
PAGE_V = 2  # bump when the layout shape changes so cached pages are redesigned


def block_of(r, blk):
    """The block on me for this row's open MR (blocking.py's last scan), or None."""
    mr = r.get("mr") or {}
    return mr.get("state") == "opened" and (blk or {}).get(f"{r.get('project')}!{mr.get('iid')}") or None


PROMPTS = greenlight.ROOT / "prompts"  # the defaults everyone starts from: <kind>.md
COMMON = ("mr", "url", "title", "branch", "target", "sha")
KINDS = {"review": ("last_review_sha",), "threads": ("threads",), "ci": ("failed_jobs", "pipeline_url"), "conflicts": ()}  # card order is MOVES


def own_prompt(repo, kind):
    return greenlight.STATE / "prompts" / repo / f"{kind}.md"  # yours, private to this host


def prompt_of(repo, kind):
    """(template, yours): ~/.greenlight/prompts/<repo>/<kind>.md, else the bundled prompts/<kind>.md. {default} in yours pulls that in."""
    d, f = (PROMPTS / f"{kind}.md").read_text().strip(), own_prompt(repo, kind)
    return (f.read_text().strip().replace("{default}", d), True) if f.exists() else (d, False)


# Settings › Repositories › Auto-run: kind → (when, [(gate label, what the unattended run may do)]). Merge has no gate: greenlight merges.
AUTO = {
    "ci": ("The pipeline failed on your MR", [("Stop before push", "Stop before pushing and wait for me."),
                                              ("Push the fix", "You have my OK to push once the fix is proven; don't retry jobs.")]),
    "conflicts": ("Your MR has merge conflicts", [("Stop before push", "Stop before pushing and wait for me."),
                                                  ("Force-push", "OK to git push --force-with-lease once resolved and tests pass; if both sides can't hold, stop and ask.")]),
    "threads": ("Your MR has unresolved threads", [("Stop before push", "Stop before pushing and wait for me."),
                                                   ("Push, draft replies", "Push the fixes, but don't post or resolve anything; leave the replies here."),
                                                   ("Push, post & resolve", "Push, post your replies, and resolve only the threads you fixed.")]),
    "review": ("Your review is the next move", [("Draft only", "Don't post anything; leave the findings here."),
                                                ("Post comments", "Post the findings as review comments; never approve.")]),
    "merge": ("greenlight says it can merge now", []),
}
AUTO_HEAD = "This run was started automatically by Greenlight; nobody is watching. "
PUSH_RULE = (f" Before any push, run `python3 {greenlight.HERE / 'greenlight.py'} scan` to refresh this workspace's goal "
             f"and MR, then `python3 {audit.HERE / 'audit.py'} gate push --execute` from its clean worktree. "
             "The gate audits and pushes the exact commit; use `--target <base branch>` for a nondefault target and do not run a separate git push. "
             f"Before creating a draft PR/MR, put its exact title, body, and target branch in a JSON file outside the worktree and run "
             f"`python3 {audit.HERE / 'audit.py'} gate pr <metadata.json> --execute`; the gate audits and creates it. "
             "Stop and report any block.")


def autorun():
    """~/.greenlight/autorun.json: {repo: {kind: gate index}}; a kind that's there is on."""
    f = greenlight.STATE / "autorun.json"
    return json.loads(f.read_text()) if f.exists() else {}


def merge_how(cmd):
    """'Squash', 'Rebase' or 'Merge': how greenlight's merge cmd lands it."""
    return next((f[2:].capitalize() for f in cmd if f in ("--squash", "--rebase")), "Merge")


def moves(r, blk=None):
    """Both hover cards' (and the page's) buttons for this row's open MR, most urgent first: conflicts › ci › threads (the MR's fixes) ›
    review (your review is the next move) › merge (greenlight can merge now; never beside a fix) › ready (take it out of
    draft, when that waits on you). None → the card
    shows greenlight's generated next step instead. Each prompt's {vars} filled in."""
    mr, nxt = r.get("mr") or {}, r.get("next") or {}
    if mr.get("state") == "merged" and nxt.get("owner") == "me" and "signoff" in (nxt.get("cmd") or []):  # your step after the merge: deploy, verify
        if nxt["step"] not in greenlight.post_merge(r["project"]):
            return []  # a row from before the lifecycle dropped the step (greenlight.md's old deploy): the next scan says done
        st = next(s for s in greenlight.lifecycle_doc(r["project"])["stages"] if s["key"] == nxt["step"])
        return [{"kind": "signoff", "step": nxt["step"], "run": st["run"]}]
    if mr.get("state") != "opened":
        return []
    mine = mr.get("role") == "author"
    review = nxt.get("owner") == "me" and (nxt.get("step") == "review" or "signoff" in (nxt.get("cmd") or []) or "review it" in nxt.get("action", ""))
    on = {"conflicts": mr.get("conflicts"), "ci": mr.get("pipeline") == "failed", "threads": mr.get("todo_threads"),  # the fixes: on any MR, yours or a review
          "review": review or (block_of(r, blk) or {}).get("reason") == "approval"}
    out, cmd = [], nxt.get("cmd") or []
    repo = r["project"].rsplit("/", 1)[-1]
    goal = r.get("goal") or ""
    on_auto = {} if greenlight.HOLD_RE.search(goal) or greenlight.PARKED_RE.search(goal) or greenlight.AUTOMATION_RE.search(goal) else autorun().get(repo, {})
    lines = lambda xs: "\n".join(f"- {x}" for x in xs) or None
    vals = dict(zip(COMMON, (mr.get("ref"), mr.get("web_url"), mr.get("title"), mr.get("source_branch") or r.get("branch"), mr.get("target_branch"), mr.get("sha"))),
                last_review_sha=mr.get("reviewed_sha"), threads=lines(mr.get("todo_threads") or []), pipeline_url=mr.get("pipeline_url"),
                failed_jobs=lines(f"{j['name']} ({j['stage']}): {j['url']}" for j in mr.get("failed_jobs") or []))
    for kind in ("conflicts", "ci", "threads", "review"):
        if on[kind]:
            text, own = prompt_of(repo, kind)
            ok = set(COMMON + KINDS[kind])
            text = re.sub(r"\{(\w+)\}", lambda m, ok=ok: str(vals[m[1]] or "none") if m[1] in ok else m[0], text)  # one pass: a title can't fill in
            out.append({"kind": kind, "prompt": text, "own": own, "repo": repo, "n": len(mr.get("todo_threads") or []), "jobs": [j["name"] for j in mr.get("failed_jobs") or []]})
    fix = any(m["kind"] != "review" for m in out)  # conflicts, CI or threads: fix those first
    if not fix and nxt.get("step") == "merge" and (cmd[:1] in (["gh"], ["glab"]) or cmd[2:3] == ["checked-merge"]):
        out.append({"kind": "merge", "how": merge_how(cmd), "target": mr.get("target_branch")})
    if not fix and nxt.get("step") == "ready" and nxt.get("owner") == "me" and cmd[:1] in (["gh"], ["glab"]):
        out.append({"kind": "ready"})
    for m in out:  # auto: what the terminal runs unattended (merge: True, and only on its own: never before a review), once per head commit
        g = on_auto.get(m["kind"]) if m["kind"] != "ready" and not (m["kind"] == "merge" and len(out) > 1) \
            and (mine or m["kind"] not in ("conflicts", "ci", "threads")) else None  # unattended pushes only to your own branch
        if not (isinstance(g, int) and 0 <= g < max(1, len(AUTO[m["kind"]][1]))):
            continue
        if m["kind"] != "merge":  # the run's own gate, then audit.py's: the contracts before it pushes, the outbound scorer before it posts
            m["sha"], m["auto"] = mr.get("sha"), f"{AUTO_HEAD}{AUTO[m['kind']][1][g][1]} {audit.brief(m['kind'], g)}".rstrip() + f"\n\n{m['prompt']}"
            continue
        tag = audit.ident(r, mr)
        v = audit.cached(r.get("path") or "", mr.get("sha") or "", tag)  # an unattended merge waits on the semantic audit of this head
        scoped = r.get("project") and mr.get("iid") and r.get("forge") and mr.get("target_branch")
        valid = audit.fresh(v) and (not scoped or audit.same_target(v, r["project"], mr["iid"], r["forge"], mr["sha"], mr["target_branch"]))
        if valid and v.get("ok"):
            m["sha"], m["auto"] = mr.get("sha"), True
        elif valid:
            m["audit"] = audit.summary(v)  # held: the user was pinged the failed contracts; the card's Merge is still theirs
        elif r.get("path") and mr.get("sha"):
            audit.kick(r["path"], mr["sha"], escalate=True, tag=tag,
                       spec={"target": mr.get("target_branch"), "goal": audit.goal_of(r, mr), "project": r.get("project"),
                             "iid": mr.get("iid"), "forge": r.get("forge"), "name": r.get("name"),
                             "ref": mr.get("ref"), "web_url": mr.get("web_url")})
            m["audit"] = "auditing"
    return out


def tree_merge(path, automatic=False):
    """The card's Merge: greenlight's own merge cmd for this workspace's MR, pinned to the head greenlight saw, so a push since
    can't land unseen. The forge still checks everything else; its refusal comes back verbatim."""
    original = json.loads((greenlight.STATE / "last.json").read_text())
    rows = {r["path"]: r for r in original["rows"]}
    r = rows.get(path) or {}
    m = next((x for x in moves(r) if x["kind"] == "merge"), None) if r else None
    if not m or not r["mr"].get("sha") or automatic and not m.get("auto"):
        return {"error": "greenlight doesn't say this can merge now"}
    mr_identity = (r.get("id"), r.get("forge"), r.get("project"), r["mr"].get("iid"), r["mr"].get("sha"))
    cmd = r["next"]["cmd"]
    # The card is a human Merge click even when an unattended auto action is
    # also available. Keep the source-head pin, but leave semantic target
    # gating on greenlight's separate automatic checked-merge command.
    manual = cmd[9:] if cmd[2:3] == ["checked-merge"] else cmd
    if automatic:
        mr = r["mr"]
        if not all((r.get("project"), r.get("forge"), mr.get("iid"), mr.get("target_branch"))):
            return {"error": "automatic merge needs a scoped PR"}
        v = audit.cached(path, mr["sha"], audit.ident(r, mr))
        if not audit.fresh(v) or not v.get("ok") or not v.get("target_oid") or \
           not audit.same_target(v, r["project"], mr["iid"], r["forge"], mr["sha"], mr["target_branch"]):
            return {"error": "the semantic merge audit is no longer current"}
        guarded = [sys.executable, str(audit.HERE / "audit.py"), "checked-merge", r["forge"], r["project"], str(mr["iid"]),
                   mr["target_branch"], mr["sha"], v["target_oid"], *manual]
    else:
        pin = "--match-head-commit" if manual[0] == "gh" else "--sha"
        guarded = manual if manual[-2:] == [pin, r["mr"]["sha"]] else manual + [pin, r["mr"]["sha"]]
    greenlight.run(guarded, timeout=90)
    # ponytail: mark it merged in the snapshot so the card stops offering it; greenlight's next scan rewrites the row anyway
    post = greenlight.post_merge(r["project"])  # steps after merge: the next scan asks for them; until then it's not done
    r["mr"]["state"], r["next"] = "merged", {"step": post[0], "owner": "me", "action": f"merged from Greenlight; next: {post[0].replace('_', ' ')}"} \
        if post else {"step": "done", "owner": "done", "action": "merged from Greenlight"}
    r["life"] = lifestage.place(r)
    import ws_state
    with ws_state.lock():
        snap = json.loads((greenlight.STATE / "last.json").read_text())
        current_clock = greenlight.scan_clock_id()
        if snap.get("scan_clock_id") != current_clock:
            snap["dashboard_base_started_ns"] = 0
        else:
            snap.setdefault("dashboard_base_started_ns", snap.get("scan_started_ns", 0))
        # The lifecycle and author role are shared by every checkout of this forge request (one global ME).
        patch = {"request": True, "forge": mr_identity[1], "project": mr_identity[2], "iid": mr_identity[3],
                 "sha": mr_identity[4], "row": {"next": r["next"]}}
        snap["dashboard_merges"] = (snap.get("dashboard_merges") or []) + [patch]
        for peer in snap["rows"]:
            pmr = peer.get("mr") or {}
            if (peer.get("forge"), peer.get("project"), pmr.get("iid")) != mr_identity[1:4] or \
                    pmr.get("state") == "merged":
                continue
            peer["mr"] = pmr | {"state": "merged"}
            peer["next"] = r["next"].copy()
            peer["life"] = lifestage.place(peer)
        if snap.get("scan_id"):
            snap["scan_id"] = uuid.uuid4().hex
            snap["scan_started_ns"] = time.monotonic_ns()
            snap["scan_clock_id"] = current_clock
            for row in snap["rows"]:
                row["_scan_id"] = snap["scan_id"]
        tmp = greenlight.STATE / f"last.dashboard.{uuid.uuid4().hex}.json"
        marker = greenlight.STATE / f"scan_id.dashboard.{uuid.uuid4().hex}"
        try:
            tmp.write_text(json.dumps(snap))
            tmp.replace(greenlight.STATE / "last.json")
            if snap.get("scan_id"):
                marker.write_text(f"{snap['scan_id']} {(greenlight.STATE / 'last.json').stat().st_ino}")
                marker.replace(greenlight.STATE / "scan_id")
        finally:
            tmp.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
    return {"ok": True, "ref": r["mr"].get("ref"), "target": r["mr"].get("target_branch"), "how": m["how"]}


def tree_ready(path):
    """The card's Mark ready: greenlight's own ready cmd (gh pr ready / glab mr update --ready) for this workspace's draft."""
    r = {x["path"]: x for x in json.loads((greenlight.STATE / "last.json").read_text())["rows"]}.get(path) or {}
    if not r or not any(x["kind"] == "ready" for x in moves(r)):
        return {"op": "ready", "path": path, "error": "greenlight doesn't say this is waiting to come out of draft"}
    greenlight.run(r["next"]["cmd"], timeout=90)
    return {"op": "ready", "path": path, "ok": True, "ref": r["mr"].get("ref")}  # ponytail: the next scan shows it out of draft


def tree_signoff(path):
    """The card's Deploy (any step after merge): runs the step's `Run:` command from your lifecycle on this host, in the
    worktree, then signs the step off. No Run: just signs it off. A failed command signs nothing off; its output comes back."""
    r = {x["path"]: x for x in json.loads((greenlight.STATE / "last.json").read_text())["rows"]}.get(path) or {}
    m = next((x for x in moves(r) if x["kind"] == "signoff"), None) if r else None
    base = {"op": "signoff", "path": path, "ref": (r.get("mr") or {}).get("ref"), "step": m and m["step"]}
    if not m:
        return {**base, "error": "greenlight doesn't say a step after the merge waits on you"}
    if m["run"]:
        try:
            p = subprocess.run(["bash", "-lc", m["run"]], cwd=path if os.path.isdir(path) else None, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return {**base, "error": f"`{m['run']}` ran past 10 minutes"}
        if p.returncode:
            return {**base, "error": f"`{m['run']}` exited {p.returncode}: " + (p.stderr or p.stdout).strip()[-600:]}
    greenlight.run(r["next"]["cmd"], timeout=30)
    return {**base, "ok": True, "ran": m["run"]}


class BoardAllow(dict):
    """Card identities plus every displayed worktree ID for scoped session isolation."""


def board(only=None, with_ids=False):
    """The chat's view of every workspace: one line each with its lifecycle step, who it waits on, and (when it's on you)
    what to do. Blocks on you first, then your moves, then the rest; finished ones only counted. `only` (else $GREENLIGHT_ONLY,
    JSON the chat sets): the window the user is in, {"paths": its worktrees, "repos": its projects' repos} (a bare list: paths only).
    A workspace in one of its repos counts even when the window listed its worktrees before that one existed.
    Like the dashboard, the scan is joined with a live `worktree ps`: a workspace created since the scan is on the board at once."""
    if only is None and os.environ.get("GREENLIGHT_ONLY"):
        only = json.loads(os.environ["GREENLIGHT_ONLY"])
    rows, blk = json.loads((greenlight.STATE / "last.json").read_text())["rows"], blocking.cached()
    try:
        live = live_agents()
    except Exception:  # noqa: BLE001 - the ADE isn't answering: the scan alone
        live = None
    if live is not None:
        rows = live_rows(rows, live)
        for r in rows:
            if ag := live[r["id"]][1]:  # the live agent, not the scan's: one started since shows as working
                r["agent"] = {**r["agent"], "state": ag.get("state", r["agent"]["state"])}
    if any(not r.get("card") for r in rows):
        import ws_state
        rows = ws_state.keyed(rows, migrate=False)  # a pre-upgrade scan still needs stable question targets
    identity_counts = {}
    for r in rows:
        ident = (r["name"], (r.get("mr") or {}).get("ref"), r["project"])
        identity_counts[ident] = identity_counts.get(ident, 0) + 1
    if only is not None:
        paths, repos = (only, []) if isinstance(only, list) else (only.get("paths") or [], only.get("repos") or [])
        rows = [r for r in rows if r.get("path") in paths or r.get("repo") and r["repo"] in repos]
    lines, done, allowed = [], 0, BoardAllow()
    allowed.members = []
    for r in rows:
        st, why = state_of(r, blk)
        mr = r.get("mr") or {}
        allowed.members.append((r["id"], r.get("branch"), r.get("forge"), r.get("project"), mr.get("ref"),
                                mr.get("target_branch")))
        if st == "done":
            done += 1
            continue
        if r.get("card"):
            import ws_state
            allowed[r["card"]] = (r.get("forge") or "", r.get("project") or "", (r.get("mr") or {}).get("ref") or "",
                                  (r.get("mr") or {}).get("sha") or "", r.get("goal") or "",
                                  (r.get("mr") or {}).get("target_branch") or "", r.get("id") or "", r.get("branch") or "",
                                  ws_state.stage_of(r))
        _, steps, i = progress(r)
        mr, nxt, ad = r.get("mr") or {}, r["next"], advice(r)
        ref = f"[{mr['ref']}]({mr['web_url']})" if mr.get("ref") and mr.get("web_url") else mr.get("ref") or "no MR"
        where = f"step {i + 1}/{len(steps)} {nxt['step']}" if steps and i is not None else nxt["step"]
        ident = (r["name"], mr.get("ref"), r["project"])
        location = f", {r['path']}" if identity_counts[ident] > 1 else ""
        lines.append(((st != "needs", st != "raised"), f"- {r['name']} ({ref}, {r['project']}{location}): {where} · on {nxt['owner']} · {(nxt.get('action') or why)[:110]}"
                      + (f" · do: {ad}" if ad else "") + (" · agent working" if (r.get("agent") or {}).get("state") == "working" else "")))
    lines.sort(key=lambda x: x[0])
    result = "\n".join(l for _, l in lines) + f"\n({done} more finished)" * bool(done)
    return (result, allowed) if with_ids else result


GH_WORK = """query($q: String!) { search(type: ISSUE, first: 50, query: $q) { nodes {
  ... on PullRequest { number title url isDraft updatedAt reviewDecision mergeable repository { nameWithOwner }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }
  ... on Issue { number title url updatedAt repository { nameWithOwner } labels(first: 5) { nodes { name } }
    closedByPullRequestsReferences(first: 1) { totalCount } } } } }"""


def gl_item(m, role):
    """A GitLab MR or issue in forge_line's shape."""
    if "iid" in m and "source_branch" in m:
        st = ["draft"] * bool(m.get("draft")) + ["conflicts"] * bool(m.get("has_conflicts")) + [(m.get("detailed_merge_status") or "").replace("draft_status", "").replace("_", " ")]
    else:
        st = [", ".join(m.get("labels") or []), f"due {m['due_date']}" if m.get("due_date") else "", "has an MR" if m.get("merge_requests_count") else "no MR"]
    return {"role": role, "ref": m["references"]["full"], "title": m["title"], "url": m["web_url"], "updated": m.get("updated_at") or "", "status": st}


def gh_item(n, role):
    """A GitHub PR or issue (GH_WORK's nodes) in forge_line's shape."""
    ref = f"{n['repository']['nameWithOwner']}#{n['number']}"
    if "isDraft" in n:
        ci = ((((n.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}).get("statusCheckRollup") or {}).get("state")
        st = ["draft"] * n["isDraft"] + ["conflicts"] * (n.get("mergeable") == "CONFLICTING") + [f"CI {ci.lower()}" if ci else "", (n.get("reviewDecision") or "").lower().replace("_", " ")]
    else:
        st = [", ".join(l["name"] for l in n["labels"]["nodes"]), "has a PR" if n["closedByPullRequestsReferences"]["totalCount"] else "no PR"]
    return {"role": role, "ref": ref, "title": n["title"], "url": n["url"], "updated": n.get("updatedAt") or "", "status": st}


def forge_line(it, ws, now):
    """One line for the chat: what it is, its status, how stale, and the workspace already on it."""
    days = (now - calendar.timegm(time.strptime(it["updated"][:19], "%Y-%m-%dT%H:%M:%S"))) / 86400 if it["updated"] else None
    age = "" if days is None else "updated today" if days < 1 else f"updated {int(days)}d ago"
    return (f"- [{it['ref']}]({it['url']}) {it['title'][:90]} · " + " · ".join(x for x in it["status"] + [age] if x)
            + (f" · workspace: {ws[it['ref']]}" if it["ref"] in ws else ""))


def forge(repo=None):
    """Open issues and MRs/PRs with their statuses, for the chat to suggest what to pick up: mine (to review, authored,
    assigned issues) on both forges, or with repo every open one in that repo. Workspaces already on one are named."""
    try:
        rows = json.loads((greenlight.STATE / "last.json").read_text())["rows"]
    except (OSError, ValueError, KeyError):
        rows = []
    ws = {f"{r['project']}{'#' if (r.get('mr') or {}).get('forge', r.get('forge')) == 'github' else '!'}{r['mr']['iid']}": r["name"]
          for r in rows if (r.get("mr") or {}).get("iid")}
    items, errs, me = [], [], greenlight.ME
    gh_q = lambda q, role: [gh_item(n, role) for n in greenlight.gh("api", "graphql", "-f", f"q={q} is:open archived:false", "-f", f"query={GH_WORK}")["data"]["search"]["nodes"] if n]
    if repo:
        forge_of = next((r.get("forge") or (r.get("mr") or {}).get("forge") for r in rows if r["project"] == repo), None)
        try:
            if forge_of != "gitlab":
                items = gh_q(f"repo:{repo} is:pr", "open") + gh_q(f"repo:{repo} is:issue", "open")
        except Exception as e:  # noqa: BLE001 - not a GitHub repo (or gh is out): try GitLab
            errs.append(f"github: {str(e)[:120]}")
        if not items and forge_of != "github":
            try:
                pp = f"projects/{greenlight.pid(repo)}"
                items = [gl_item(m, "open") for m in greenlight.gl(f"{pp}/merge_requests?state=opened&per_page=50")] + \
                        [gl_item(m, "open") for m in greenlight.gl(f"{pp}/issues?state=opened&per_page=50")]
                errs = []
            except Exception as e:  # noqa: BLE001
                errs.append(f"gitlab: {str(e)[:120]}")
    else:
        for name, fetch in (("gitlab", lambda: [gl_item(m, "review") for m in greenlight.gl(f"merge_requests?scope=all&state=opened&reviewer_username={me}&per_page=50")]
                                     + [gl_item(m, "mine") for m in greenlight.gl(f"merge_requests?scope=all&state=opened&author_username={me}&per_page=50")]
                                     + [gl_item(m, "issue") for m in greenlight.gl("issues?scope=assigned_to_me&state=opened&per_page=50")]),
                            ("github", lambda: gh_q("is:pr review-requested:@me", "review") + gh_q("is:pr author:@me", "mine") + gh_q("is:issue assignee:@me", "issue"))):
            try:
                items += fetch()
            except Exception as e:  # noqa: BLE001 - that CLI isn't installed or logged in: the other forge still answers
                errs.append(f"{name}: {str(e)[:120]}")
    heads = {"review": "Asked to review", "mine": "Your MRs/PRs", "issue": "Issues assigned to you", "open": f"Open in {repo}"}
    out, now = [], time.time()
    for role, head in heads.items():
        group = sorted((i for i in items if i["role"] == role), key=lambda i: i["updated"], reverse=True)
        if group:
            out += [f"{head} ({len(group)}):"] + [forge_line(i, ws, now) for i in group[:40]]
    return "\n".join(out + [f"(couldn't ask {e})" for e in errs]) or "Nothing open."


def state_of(r, blk=None):
    """(state, why) from greenlight's next step plus the live agent and what I'm blocking."""
    nxt, ag = r["next"], r["agent"]
    if ag["state"] == "waiting":
        return "needs", "agent is waiting on your answer"
    if nxt["step"] == "error" or "logged out" in nxt.get("action", ""):
        return "crashed", nxt.get("action", "")
    if b := block_of(r, blk):
        return "needs", f"You're blocking: {b['why']}"
    if ag["state"] == "working" or nxt["owner"] in ("working", "ci"):
        return "working", "agent working" if ag["state"] == "working" else "CI running"
    if nxt["owner"] == "me":
        return "raised", "your move"
    if nxt["owner"] in ("greenlight", "agent"):
        return "queued", "greenlight dispatches next"
    if nxt["owner"] == "done":
        return "done", "goal reached"
    return "quiet", f"waiting on {nxt['owner']}"


def progress(r):
    """(fraction, steps, index). No MR yet sits on step 0 (`mr_open` is implement, push, open the MR); someone else's MR
    has one step, your review; steps after `merge` are the lifecycle's own post-merge ones."""
    name, steps = greenlight.lifecycle(r["project"]) if r.get("forge") else ("", [])  # no forge: a goal-only tree
    step = r["next"]["step"]
    if (r.get("mr") or {}).get("role") == "review" or step == "review":
        steps = ["review"]
    if r["next"]["owner"] == "done" or step == "done":
        return 1.0, steps, len(steps)
    if step in steps:
        return steps.index(step) / len(steps), steps, steps.index(step)
    if steps and not r.get("mr") and step in ("freeform", "goal", "automation"):
        return 0.0, steps, 0
    return None, steps, None


def _advice_me(r):
    nxt, mr, a = r["next"], r.get("mr") or {}, r["next"].get("action", "")
    step, cmd = nxt["step"], nxt.get("cmd") or []
    if "logged out" in a:
        return "Log the agent back in in its terminal; greenlight re-dispatches once it's back."
    if step == "error":
        return "Usually a forge auth or network blip; the next scan retries. Open the workspace if it sticks."
    if step == "goal":
        return "Give it a goal (the workspace comment), or delete the workspace if it's stale."
    if step == "automation":
        return "Look over what the run left behind, then delete the workspace."
    if step == "review":
        if "re-review" in a:
            return "Read the author's replies, then resolve your threads or follow up."
        return f"CI {mr['pipeline']}, so read the diff, then approve or leave threads for what has to change." \
            if mr.get("pipeline") not in (None, "success") else "Read the diff, then approve or leave threads for what has to change."
    if "signoff" in cmd:
        return f"Once it's true, sign off {step.replace('_', ' ')} in the chat; greenlight records it for this MR."
    if step == "ready":
        return "If the reason to keep it a draft is gone, take it out of draft; otherwise leave it."
    if step == "milestone":
        return "Pick the release milestone on the MR yourself: two share the latest due date."
    if step == "merge":
        if "goal says hold" in a:
            return "Merge from here when the hold is over, or take the hold out of the goal."
        if "nobody else reviews" in a:
            return "Read the diff yourself, then merge from here."
        if "rebase" in a:
            return "There's no real conflict: say yes to a rebase in the chat and greenlight runs it."
        if "branch protection" in a:
            return "Open the PR and find which protection rule isn't met (a required check or review)."
        if a.startswith("mergeable into"):
            return f"Merge it into {mr.get('target_branch') or 'its base'} yourself, or retarget it at the default branch."
        return "Open the MR to see why the forge won't merge it."
    return None


AGENT_ADVICE = {"ci_green": "No agent is on it: run Fix CI.", "no_conflicts": "No agent is on it: run Fix conflicts.",
                "threads": "No agent is on it: run Resolve threads.", "freeform": "No agent is running: start one on the goal."}


def advice(r):
    """One sentence on what to do, when you're the blocker or the step is an agent's and none is on it. None: no rule
    covers it (your own steps, mostly), so `suggest` writes one; "" for pucks that don't get advice at all."""
    nxt, ag = r["next"], r.get("agent") or {}
    if ag.get("state") == "waiting":
        return "Answer it from here, or open the workspace if it needs more than a reply."
    if nxt["owner"] == "me":
        return _advice_me(r)
    if nxt["owner"] == "agent" and ag.get("state") != "working":  # owner is the scan's; the agent state is live
        return AGENT_ADVICE.get(nxt["step"])
    return ""


def live_agents():
    out = {}
    for w in greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]:
        out[w["worktreeId"]] = (w, greenlight.primary_agent(w.get("agents")))
    return out


def live_rows(rows, live):
    """The scan's rows joined with a live `worktree ps`: workspaces deleted since the scan dropped, and ones it hasn't seen added
    as goal-only rows: those on neither GitLab nor GitHub (greenlight.py skips them), and ones created since the scan (a workspace
    `ade worktree create` just started shows up at once, not at the next scan). Each row gets `repo`, the ADE's repo name: what
    a window's projects are made of."""
    rows = [r for r in rows if r["id"] in live]
    known = {r["id"] for r in rows}
    for wid, (w, ag) in live.items():  # with an agent or a goal
        if wid in known or w.get("isMainWorktree") or w.get("isArchived") or not w.get("branch") \
                or not (w.get("agents") or w.get("comment")):
            continue
        rows.append({"id": wid, "name": w["displayName"], "path": w["path"], "project": w.get("repo") or "",
                     "goal": (w.get("comment") or "").strip(), "mr": None,
                     "next": {"step": "freeform", "owner": "parked","action": "no lifecycle: goal only"},
                     "agent": {"state": "none", "type": None, "last": ""}})
    for r in rows:
        r["repo"] = live[r["id"]][0].get("repo") or ""
    return rows


def snapshot(full=False):  # full: the agent's whole last message (pages); the 5s stream keeps the tail
    snap = json.loads((greenlight.STATE / "last.json").read_text())
    live = live_agents()
    now = time.time() * 1000
    rows = live_rows(snap["rows"], live)
    pucks, blk = [], blocking.cached()
    for r in rows:
        w, ag = live[r["id"]]
        if ag:
            r["agent"].update(state=ag.get("state", r["agent"]["state"]), type=ag.get("agentType") or r["agent"]["type"],
                              last=(ag.get("lastAssistantMessage") or r["agent"]["last"])[-(20000 if full else 400):],
                              task=(ag.get("taskTitle") or ag.get("prompt") or "")[:160], tool=ag.get("toolName") or "",
                              input=(ag.get("toolInput") or "")[:160], since=ag.get("stateStartedAt"))
        r["agent"]["idle_h"] = w.get("lastOutputAt") and round((now - w["lastOutputAt"]) / 3.6e6, 1)
        state, why = state_of(r, blk)
        prog, steps, idx = progress(r)
        mr = r.get("mr") or {}
        page = cached_page(r["name"])
        pucks.append({
            "name": r["name"], "path": r["path"], "project": r["project"], "repo": r["repo"], "goal": r["goal"], "goal_live": w.get("comment") or "", "state": state, "why": why,
            "progress": prog, "steps": steps, "step_index": idx, "step": r["next"]["step"],
            # the live goal, not last.json's: a park from the dock shows up before the next scan
            "owner": r["next"]["owner"], "hidden": bool(greenlight.PARKED_RE.search(w.get("comment") or "")
                                                        and (r.get("mr") or {}).get("state") not in ("merged", "closed")),
            "action": r["next"].get("action", ""), "changed": r.get("changed"),
            "agent": r["agent"], "mr": mr and {k: mr.get(k) for k in ("iid", "ref", "forge", "web_url", "title", "role", "draft", "author", "state",
                                                     "pipeline", "conflicts", "threads", "approvals", "approvals_required", "mergeable",
                                                     "source_branch", "target_branch", "labels", "milestone", "approved_by",
                                                     "pipeline_url", "threads_total")},
            "branch": (w.get("branch") or "").removeprefix("refs/heads/"),
            "puck": page.get("layout", {}).get("puck"),
            "blocking": (block_of(r, blk) or {}).get("reason"),  # the reason I'm blocking its MR, if I am
            # lifestage.py: lifecycle stage and sub-stage, who holds it, the suggested move (the hover card's line and button label),
            # with the agent's state as of now rather than the scan's
            "life": lifestage.now(r.get("life") or lifestage.place(r), r["agent"]["state"], "logged out" in r["next"].get("action", ""), block_of(r, blk)),
        })
        pucks[-1]["moves"] = moves(r, blk)  # the hex card's smart button, the same as the Basement card's
        pucks[-1]["sig"] = sig(pucks[-1])
        pucks[-1]["built"] = page.get("sig") == pucks[-1]["sig"] and page.get("v") == PAGE_V  # its page layout is current; else the app queues a build
        # the hover card's button, written ahead (the app runs `suggest` for a puck whose state moved); None until then
        pucks[-1]["advice"] = advice(r)  # None: the suggestion's advice line stands in
        sg = cached_suggest(r["name"])
        pucks[-1]["suggestion"] = {"label": sg["label"], "prompt": sg["prompt"], "advice": sg.get("advice") or ""} \
            if fresh_suggest(sg, pucks[-1]) and sg.get("prompt") else None
    pucks.sort(key=lambda p: (STATES.index(p["state"]), p["name"]))
    # every other worktree (default branches first): the console's picker reaches all of them, the map stays goals-only
    have = {p["path"] for p in pucks}
    trees = sorted(({"name": w["displayName"], "path": w["path"], "project": w.get("repo") or "", "main": bool(w.get("isMainWorktree")),
                     "branch": (w.get("branch") or "").removeprefix("refs/heads/"), "agent": (ag or {}).get("state") or "none"}
                    for w, ag in live.values() if w["path"] not in have and not w.get("isArchived")),
                   key=lambda x: (not x["main"], x["project"], x["name"]))
    return {"ts": int(time.time()), "scanned": snap["ts"], "pucks": pucks, "trees": trees}


ADES = ("orca", "cmux", "herdr", "t3", *(["basement"] if os.environ.get("GREENLIGHT_ADE") == "basement" else []))  # basement: opt-in


def trees():
    """Every installed ADE's worktrees merged, first ADE wins a shared path. An ADE whose CLI is missing or
    app isn't running raises, and is skipped."""
    out, seen = [], set()
    try:  # greenlight's last scan: the MR, CI, threads and "needs you" for the workspaces it tracks (no API calls here)
        scan, blk = {r["path"]: r for r in json.loads((greenlight.STATE / "last.json").read_text())["rows"]}, blocking.cached()
    except (OSError, ValueError, KeyError):
        scan, blk = {}, None
    for ade in ADES:
        try:
            call = greenlight.orca_cli if ade == "orca" else importlib.import_module(f"ade_{ade}").call
            ws = call("worktree", "ps", "--limit", "10000")["worktrees"]
        except Exception:
            continue
        for w in ws:
            if w.get("isArchived") or not w.get("path") or w["path"] in seen:
                continue
            seen.add(w["path"])
            ag = greenlight.primary_agent(w.get("agents"))
            out.append({"ade": ade, "project": w.get("repo") or "", "name": w.get("displayName") or Path(w["path"]).name,
                        "path": w["path"], "branch": (w.get("branch") or "").removeprefix("refs/heads/"),
                        "main": bool(w.get("isMainWorktree")), "agent": ag.get("state") or "none",
                        "goal": w.get("comment") or "", "pinned": bool(w.get("isPinned")), "active": w.get("lastOutputAt"),
                        "since": ag.get("stateStartedAt"), "created": None if w.get("isMainWorktree") else created(w["path"])})
            if out[-1]["main"] and (c := repo_cfg(w["path"])):
                out[-1]["cfg"] = c
            if r := scan.get(w["path"]):
                out[-1]["puck"] = r["name"]  # greenlight's name for it: `suggest` and the dashboard know it by this
                r["agent"] = {**r["agent"], "state": out[-1]["agent"]}  # the live agent, not the scan's
                out[-1]["state"], out[-1]["why"] = state_of(r, blk)
                _, out[-1]["steps"], out[-1]["step_index"] = progress(r)  # the card's lifecycle checklist
                out[-1]["step"], out[-1]["owner"], out[-1]["advice"] = r["next"]["step"], r["next"]["owner"], advice(r)  # and what to do, when it's on you
                if r.get("life"):
                    out[-1]["life"] = r["life"]  # the stage, for the step line
                if mv := moves(r, blk):
                    out[-1]["moves"] = mv
                    if any("auto" in m for m in mv):  # auto-run: has greenlight's scan caught up with what's pushed? (the tab's "needs you")
                        h = subprocess.run(["git", "-C", w["path"], "rev-parse", "@{u}"], capture_output=True, text=True)
                        out[-1]["pushed"] = h.stdout.strip() if h.returncode == 0 else None
                if mr := r.get("mr"):
                    out[-1]["mr"] = {k: mr.get(k) for k in ("ref", "web_url", "title", "state", "draft", "pipeline", "conflicts",
                                                             "rebase", "threads", "mergeable", "by", "assignees",
                                                             "target_branch", "labels", "milestone", "description", "sha")}
    return sorted(out, key=lambda x: (x["project"].lower(), not x["main"], x["name"].lower()))


def created(path):
    """ms: a linked worktree's `.git` is a one-line file `git worktree add` writes and nothing rewrites."""
    try:  # ponytail: `git worktree repair/move` rewrites it too; the branch's first reflog entry if that misleads
        return int((Path(path) / ".git").stat().st_mtime * 1000)
    except OSError:
        return None


def tree(path):
    """(ade, worktree) for a path, re-listed now so a stale id from the page can't name another tree."""
    for ade in ADES:
        try:
            call = greenlight.orca_cli if ade == "orca" else importlib.import_module(f"ade_{ade}").call
            ws = call("worktree", "ps", "--limit", "10000")["worktrees"]
        except Exception:
            continue
        if w := next((w for w in ws if w.get("path") == path and not w.get("isArchived")), None):
            return call, w
    raise LookupError(f"no workspace at {path}")


def tree_info(path):
    """The git facts the sidebar's menu shows and Delete checks: uncommitted files, commits no remote has, last commit."""
    git = lambda *a: subprocess.run(["git", "-C", path, *a], capture_output=True, text=True, timeout=20)
    st = git("status", "--porcelain", "--untracked-files=normal")
    if st.returncode:
        return {"error": (st.stderr.strip() or "not a git checkout")[:200]}
    last = git("log", "-1", "--format=%ct").stdout.strip()
    try:
        lp = ports(path)
    except (OSError, subprocess.SubprocessError, ValueError):  # no tmux server or no lsof: no ports, not an error
        lp = []
    lines = st.stdout.splitlines()
    ahead = git("log", "--format=%h %s", "HEAD", "--not", "--remotes").stdout.splitlines()
    cmd = (repo_cfg(main_of(path)) or {}).get("teardown")
    base = git("merge-base", "HEAD", "origin/HEAD").stdout.strip() or "HEAD"
    # the card's Diff counts, branch plus uncommitted; ponytail: untracked files aren't counted (the Diff dialog has them)
    sh = git("diff", "--shortstat", base).stdout
    n = lambda w: int(m[1]) if (m := re.search(rf"(\d+) {w}", sh)) else 0
    # ports/dirty/unpushed as before; the rest is what Delete lists (first 20 files, 10 commits) and the port rows' process names
    return {"dirty": len(lines), "unpushed": len(ahead), "commit": int(last) * 1000 if last.isdigit() else None, "ports": [p for p, _ in lp],
            "procs": {str(p): c for p, c in lp}, "untracked": sum(l[:2] == "??" for l in lines), "files": [[l[:2].strip(), l[3:]] for l in lines[:20]],
            "commits": ahead[:10], "teardown": cmd if isinstance(cmd, str) else "",
            "added": n("insertion"), "removed": n("deletion")}


def tree_rm(path, force=False, teardown=""):
    """Remove the worktree (never its branch). Refuses the main checkout always, and, unless forced, a pinned or dirty
    tree or one with commits no remote has. The repo's teardown runs in it first when the page named it (as tree_new's setup)."""
    call, w = tree(path)
    if w.get("isMainWorktree"):
        return {"error": "the repo's main checkout is never removed"}
    info = tree_info(path)
    risks = [*(["it's pinned"] if w.get("isPinned") else []), *([info["error"]] if "error" in info else []),
             *([f"{info['dirty']} uncommitted file{'s' * (info['dirty'] != 1)}"] if info.get("dirty") else []),
             *([f"{info['unpushed']} commit{'s' * (info['unpushed'] != 1)} no remote has (the local branch keeps them)"] if info.get("unpushed") else [])]
    if risks and not force:
        return {"risks": risks}
    warn = None
    if teardown:  # a failed teardown doesn't stop the delete; the page toasts why
        if teardown != (repo_cfg(main_of(path)) or {}).get("teardown"):
            warn = "Teardown skipped: the repo's .greenlight.json changed"
        elif err := hook(teardown, path):
            warn = f"Teardown failed ({err}); deleted anyway"
    # ponytail: only Orca and the Basement honour --force; the other git-backed ADEs still refuse a dirty tree, and that error reaches the page
    call("worktree", "rm", "--worktree", f"id:{w.get('worktreeId') or w['id']}", *(["--force"] if force else []))
    return {"ok": True, **({"warn": warn} if warn else {})}


def tree_goal(path, goal):
    call, w = tree(path)
    call("worktree", "set", "--worktree", f"id:{w.get('worktreeId') or w['id']}", "--comment", " ".join(goal.split())[:500])
    return {"ok": True}


CFG = ".greenlight.json"  # in a repo's main checkout: {"name", "look", "host", "hosts"}, its project in the terminal (README)


def repo_cfg(path):
    """The checkout's .greenlight.json; None when it's missing, over 8 KB or not a JSON object."""
    try:
        f = Path(path) / CFG
        if f.stat().st_size <= 8192 and isinstance(o := json.loads(f.read_text()), dict):
            return o
    except (OSError, ValueError):
        pass
    return None


def tree_cfg(path, text):
    """Save to repo: write the main checkout's .greenlight.json (you commit it)."""
    _, w = tree(path)
    if not w.get("isMainWorktree"):
        return {"error": "only a repo's main checkout holds .greenlight.json"}
    try:
        o = json.loads(text)
    except ValueError:
        o = None
    if not isinstance(o, dict):
        return {"error": "not a JSON object"}
    (Path(path) / CFG).write_text(json.dumps(o, indent=2) + "\n")
    return {"ok": True, "cfg": o}


BRANCH_RE = re.compile(r"\w[\w./-]{0,99}")


def main_of(path):
    """The main checkout of path's repo (where .greenlight.json lives)."""
    return ade_common.git_info(path)["repoId"] or path


def hook(cmd, path):
    """.greenlight.json's setup or teardown: one shell command, run in the worktree. None when it passed, else why not."""
    try:
        p = subprocess.run(cmd, shell=True, cwd=path, capture_output=True, text=True, timeout=600)  # noqa: S602
    except subprocess.TimeoutExpired:
        return "it ran over 10 minutes"
    return None if p.returncode == 0 else f"exit {p.returncode}: " + (p.stderr or p.stdout).strip()[-300:]


def tree_new(path, o):
    """The sidebar's New workspace: branch `name` off `base` as a worktree of path's repo, made by the ADE that holds path;
    then the repo's setup command in it, then the default agent on the goal. Setup runs only when the page showed it (o.setup)
    and the file still says the same: a repo can't slip a new command in between."""
    name, base = str(o.get("name") or "").strip(), str(o.get("base") or "").strip()
    if not BRANCH_RE.fullmatch(name) or ".." in name or base and (not BRANCH_RE.fullmatch(base) or ".." in base):
        return {"error": "a branch name is letters, digits, and . _ - /"}
    goal = " ".join(str(o.get("goal") or "").split())[:500]
    gate = preflight(goal, "", o) if o.get("agent") and goal else None
    if gate and gate["pause"]:  # 1.3: ask before anything is made, so a dismissed question leaves nothing behind
        return paused(gate)
    call, w = tree(path)
    res = call("worktree", "create", "--repo", f"id:{w['repoId']}", "--name", name,
               *(["--base-branch", base] if base else []), *(["--comment", goal] if goal else []))
    made = res.get("worktree") or {}
    if not made.get("path"):  # ponytail: Orca's create answer isn't pinned down here; find it by branch instead
        made = next((x for x in call("worktree", "ps", "--limit", "10000")["worktrees"]
                     if (x.get("branch") or "").removeprefix("refs/heads/").endswith(name)), {})
    new, wid = made.get("path"), made.get("worktreeId") or made.get("id")
    if not new:
        return {"error": "the worktree was made but its folder didn't come back; refresh the sidebar"}
    cmd = (repo_cfg(main_of(path)) or {}).get("setup")
    if o.get("setup") and o["setup"] == cmd and (err := hook(cmd, new)):
        return {"ok": True, "path": new, "repo": path, "warn": f"Setup failed ({err}); no agent started"}
    out = {"ok": True, "path": new, "repo": path}  # repo: the checkout it was asked for, so the page files the new row under that repo's project at once
    if o.get("agent") and wid:
        prompt = (goal + (gate or {}).get("brief", "") + PUSH_RULE
                  + f"\n\nIf you implement code, run its tests with `python3 {Path(lifestage.__file__).resolve()} selftest -- <the test command>` "
                    "in this worktree before opening a draft PR, so the self-test is on record.")
        r = call("agent", "launch", "--agent", greenlight.CONFIG["agents"][0], "--worktree", f"id:{wid}",
                 *(["--prompt", prompt] if goal else []))
        if gate:
            lifestage.save("preflight", new, gate["record"])
        if s := (r.get("terminal") or {}).get("session"):
            out["session"] = s  # a Basement-made agent: the page opens a tab on its tmux session
    return out


DIFF_CAP = 300_000  # chars of patch: the answer is one JSON line back over ssh


def tree_diff(path, mode):
    """The Diff dialog: uncommitted changes (mode head), or everything the branch changes from where it left the default branch."""
    git = lambda *a: subprocess.run(["git", "-C", path, *a], capture_output=True, text=True, timeout=30)
    base, label = "HEAD", "HEAD"
    if mode == "branch":  # ponytail: origin's default branch, else the usual names; a stacked branch diffs against the trunk
        for r in ("origin/HEAD", "origin/main", "origin/master", "origin/develop"):
            if b := git("merge-base", "HEAD", r).stdout.strip():
                base, label = b, git("rev-parse", "--abbrev-ref", r).stdout.strip().removeprefix("origin/") or r  # origin/HEAD names main
                break
        else:
            return {"error": "no origin/HEAD, main, master or develop to compare with"}
    num = git("diff", "--numstat", base)
    if num.returncode:
        return {"error": (num.stderr.strip() or "not a git checkout")[:200]}
    patch = git("diff", "--no-color", "--no-ext-diff", base).stdout
    files = [[f, a, d] for a, d, f in (l.split("\t", 2) for l in num.stdout.splitlines())]  # path, +, − ("-" = binary)
    kind = {x[-1]: x[0][0] for x in (l.split("\t") for l in git("diff", "--name-status", base).stdout.splitlines()) if len(x) > 1}  # M A D R…
    # an agent's new files are untracked, which `git diff` never shows; ponytail: first 100, one git call each
    for f in [x for x in git("ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0") if x][:100]:
        a, d = (git("diff", "--no-index", "--numstat", "/dev/null", f).stdout or "0\t0\t").split("\t")[:2]  # an empty file prints nothing
        files.append([f, a, d]); kind[f] = "U"
        if len(patch) <= DIFF_CAP:  # past the cap only the counts: a big un-ignored file isn't read whole
            patch += git("diff", "--no-index", "--no-color", "--no-ext-diff", "/dev/null", f).stdout
    return {"mode": mode, "base": label, "files": files, "status": [kind.get(f, "M") for f, _, _ in files], "patch": patch[:DIFF_CAP], "cut": len(patch) > DIFF_CAP}


def listeners(lsof):
    """`lsof -Fpcn` output → {pid: (command, {port})}."""
    out, pid, cmd = {}, None, ""
    for l in lsof.splitlines():
        if l[:1] == "p":
            pid, cmd = int(l[1:]), ""
        elif l[:1] == "c":
            cmd = l[1:]
        elif l[:1] == "n" and pid and (port := l.rpartition(":")[2]).isdigit():
            out.setdefault(pid, (cmd, set()))[1].add(int(port))
    return out


def under(pairs, roots):
    """pids in the process trees under roots, from (pid, ppid) pairs."""
    kids, seen, todo = {}, set(), list(roots)
    for p, pp in pairs:
        kids.setdefault(pp, []).append(p)
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.add(p)
            todo += kids.get(p, [])
    return seen


def ports(path):
    """[(port, command)] listening from processes started in this worktree's Basement terminals (tmux panes whose folder is in it)."""
    run = lambda *a: subprocess.run(a, capture_output=True, text=True, timeout=10).stdout
    sock = os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"
    roots = [int(pid) for pid, _, cwd in (l.partition(" ") for l in run("tmux", "-L", sock, "list-panes", "-a", "-F",
             "#{pane_pid} #{pane_current_path}").splitlines()) if pid.isdigit() and (cwd == path or cwd.startswith(path + "/"))]
    if not roots:
        return []
    pids = under([tuple(map(int, l.split())) for l in run("ps", "-A", "-o", "pid=,ppid=").splitlines() if len(l.split()) == 2], roots)
    # ponytail: lsof on both (Linux shows your own processes without root); `ss -ltnp` if a host lacks lsof
    return sorted({(p, c[:40]) for pid, (c, ps) in listeners(run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn")).items() if pid in pids for p in ps})


def safe(name):
    return re.sub(r"[^\w.@+-]", "_", name)


def record(pucks):
    """Append a history line per puck whose (state, step, owner) moved since the last tick."""
    DASH.mkdir(parents=True, exist_ok=True)
    f = DASH / "state.json"
    prev = json.loads(f.read_text()) if f.exists() else {}
    cur = {}
    for p in pucks:
        sig = [p["state"], p["step"], p["owner"]]
        cur[p["name"]] = sig
        if prev.get(p["name"]) not in (None, sig):
            with open(DASH / f"{safe(p['name'])}.jsonl", "a") as h:
                h.write(json.dumps({"ts": int(time.time()), "from": prev[p["name"]], "to": sig, "action": p["action"][:200]}) + "\n")
    f.write_text(json.dumps(cur))


def history(name):
    hf = greenlight.history_file(name)
    notes = [{"ts": m[1], "text": m[2]} for line in (hf.read_text().splitlines() if hf.exists() else [])
             if (m := HIST_RE.match(line))]
    moves = []
    jf = DASH / f"{safe(name)}.jsonl"
    for line in jf.read_text().splitlines() if jf.exists() else []:
        e = json.loads(line)
        moves.append({"ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"])),
                      "text": f"{e['from'][0]} → {e['to'][0]} ({e['to'][1]}/{e['to'][2]}): {e['action']}", "state": e["to"][0]})
    return sorted(notes + moves, key=lambda e: e["ts"], reverse=True)[:60]


def git_facts(path):
    try:
        out = subprocess.run(["git", "-C", path, "log", "-15", "--format=@%h\t%cI\t%s", "--numstat"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    commits, files = [], {}
    for line in out.splitlines():
        if line.startswith("@"):
            h, ts, subj = line[1:].split("\t", 2)
            commits.append({"sha": h, "ts": ts[:16].replace("T", " "), "subject": subj})
        elif line.count("\t") == 2:
            a, d, p = line.split("\t")
            f = files.setdefault(p, {"path": p, "add": 0, "del": 0})
            f["add"] += int(a) if a.isdigit() else 0
            f["del"] += int(d) if d.isdigit() else 0
    return {"commits": commits, "files": sorted(files.values(), key=lambda f: -(f["add"] + f["del"]))[:12]}


def mr_facts(project, iid, forge="gitlab"):
    try:
        mr = greenlight.mr_details(project, iid, forge)
        jobs = [{k: c[k] for k in ("name", "stage", "status")} for c in mr.get("_checks", [])]  # GitHub: the head commit's checks
        if (pl := mr.get("head_pipeline")) and pl.get("id"):
            jobs = [{"name": j["name"], "stage": j["stage"], "status": j["status"]}
                    for j in greenlight.gl_all(f"projects/{greenlight.pid(project)}/pipelines/{pl['id']}/jobs?scope[]=failed&scope[]=success&scope[]=running&scope[]=pending")]
        return {"pipeline": (mr.get("head_pipeline") or {}).get("status"), "pipeline_url": (mr.get("head_pipeline") or {}).get("web_url"),
                "jobs": jobs[:60], "unresolved": len(greenlight.unresolved(mr)), "approved_by": mr.get("_approved_by", []),
                "conflicts": mr.get("has_conflicts"), "milestone": (mr.get("milestone") or {}).get("title"),
                "updated": mr.get("updated_at"), "labels": mr.get("labels", [])}
    except Exception as e:
        return {"error": str(e)[:200]}


def facts(name):
    snap = snapshot(full=True)
    p = next((p for p in snap["pucks"] if p["name"] == name), None)
    if not p:
        raise SystemExit(json.dumps({"error": f"no workspace {name}"}))
    f = {"puck": p, "history": history(name), "git": git_facts(p["path"])}
    if p["mr"] and p["mr"].get("iid") and p["mr"].get("state") == "opened":
        f["mr_live"] = mr_facts(p["project"], p["mr"]["iid"], p["mr"].get("forge") or "gitlab")
    return f


def design(f, model="claude-haiku-5-5", effort="low"):
    pick = lambda ks: "\n".join(f"- {k} {CATALOG[k][0]}: {CATALOG[k][1].removeprefix('P ')}" for k in ks)
    prompt = (
        "You pick the Insights for one workspace's page in a developer's Mac app. The page already shows, as fixed parts: "
        "the MR title and goal, the current step and next action, all lifecycle steps, the agent and its last message, "
        "pipeline status, conflicts, unresolved threads, approvals, milestone, labels, links, history and commits. "
        "NEVER repeat any of those. Insights add what they don't say: e.g. CI jobs as a grid, where the diff is as a stack, "
        f"commit cadence as a spark, one non-obvious finding as a note. Tone is one of {TONES}.\n"
        f"Tile widgets:\n{pick(TILES)}\nWide panel widgets:\n{pick(WIDE)}\n"
        f"Puck widgets (fit inside a small hex on the overview):\n{pick(k for k, v in CATALOG.items() if v[1].startswith('P '))}\n\n"
        "Return ONLY JSON, no prose: {\"puck\": {\"widget\": w, \"props\": {...}}, "
        "\"tiles\": [{\"widget\": w, \"title\": 2-4 words, \"props\": {...}}], \"wide\": {\"widget\": w, \"title\": 2-4 words, \"props\": {...}} or null}.\n"
        "0 to 3 tiles, most useful first. Fewer is better than filler. Build every value from the facts; never invent data.\n\nFacts:\n"
        + json.dumps(f)[:30000])
    m = re.search(r"\{.*\}", ask(prompt, model, effort), re.S)
    return validate(json.loads(m[0]) if m else {})


def ask(prompt, model, effort):
    """One headless answer: a gemini-* model runs on agy, anything else on config llm_harness
    (a Claude model name means nothing to another harness, so that one uses its default)."""
    h, model = harness.route(model, greenlight.CONFIG["llm_harness"])
    cmd, text = harness.argv(h, prompt, model=model, effort=effort)
    output = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=180).stdout
    if h == "agy":
        for line in reversed(output.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "result":
                result = event.get("result") or {}
                return result.get("response", "") if result.get("status") == "SUCCESS" else ""
        return ""
    return output


def suggest(name, model="claude-haiku-5-5", effort="low"):
    """The console drawer's ghost prompt: greenlight's next step, written as a task for an agent in that workspace."""
    p = next((p for p in snapshot()["pucks"] if p["name"] == name), None)
    if not p:
        return {"name": name, "error": f"no workspace {name}"}
    cache, s = DASH / "suggest" / f"{safe(name)}.json", p["sig"]
    cached = cached_suggest(name)
    if fresh_suggest(cached, p):
        return cached
    want = p.get("advice") is None  # no rule covers this step: the reply carries the advice line too
    prompt = (
        f"You are {greenlight.CONFIG['name']}, a developer's agent manager. Write the ONE prompt the developer should send to a fresh coding agent "
        "working in this workspace so it does the next thing toward the workspace goal. Ground it on greenlight's next step "
        "(puck.step, puck.owner, puck.action) and on the move the lifecycle calls for: puck.life says where the change is and who holds it, "
        "and puck.life.do says in plain words what has to happen now, so the prompt must carry out or prepare exactly that move and "
        "nothing else; be concrete (MR number, failing job, file, thread) using only the facts. "
        "If the next move is the developer's own (a review, an approval, an answer), the prompt prepares it for them "
        "(e.g. summarize the diff and open threads, draft a reply) and must not approve, comment or push. "
        "Rules: don't suggest merging (greenlight merges). " + "".join(f"{r.rstrip('.')}. " for r in greenlight.CONFIG["suggest_rules"]) +
        "Reply with a 1 to 4 word button label for the step on the first line (imperative, e.g. Fix failing lint job), " +
        ("then a line starting `Advice:` with one sentence telling the developer what to do next themselves (don't restate "
         "puck.action, they see it), " if want else "") +
        "then the prompt on the next line: 1 to 3 imperative sentences, no quotes, no preamble.\n\nFacts:\n" + json.dumps(facts(name))[:30000])
    label, text = split_label(ask(prompt, model, effort))
    tip, text = split_advice(text)
    out = {"name": name, "sig": s, "prompt": text[:1000], "label": label, "advice": tip[:300]}
    if text:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    return out


def cached_suggest(name):
    try:
        return json.loads((DASH / "suggest" / f"{safe(name)}.json").read_text())
    except (OSError, ValueError):
        return {}


def fresh_suggest(sg, p):
    """The cached suggestion is for this state, and has an advice line if this puck needs one (older ones have neither)."""
    return sg.get("sig") == p["sig"] and "label" in sg and (p.get("advice") is not None or "advice" in sg)  # "" is the model's answer: don't ask again


def split_advice(text):
    """An optional `Advice: ...` line ahead of the prompt -> (advice, prompt)."""
    head, _, rest = text.strip().partition("\n")
    if re.match(r"\W*advice:", head, re.I) and rest.strip():
        return re.sub(r"^\W*advice:\W*", "", head, flags=re.I).strip().rstrip("*").strip(), rest.strip()
    return "", text


def split_label(text):
    """suggest's reply: a 1-4 word label line, then the prompt. A one-line reply is all prompt, no label."""
    head, _, rest = text.strip().partition("\n")
    if not rest.strip():
        return "", head.strip().strip('"')
    return " ".join(head.strip().strip('"*').split()[:4]).rstrip("."), rest.strip().strip('"')


def park(name, on):
    w = next((w for w, _ in live_agents().values() if w["displayName"] == name), None)
    if not w:
        return {"error": f"no workspace {name}"}
    goal = " ".join(greenlight.PARKED_RE.sub("", w.get("comment") or "").split())
    goal = f"{goal} (parked)".strip() if on else goal
    greenlight.orca("worktree", "set", "--worktree", f"id:{w['worktreeId']}", "--comment", goal)
    hf = greenlight.history_file(name)
    hf.parent.mkdir(parents=True, exist_ok=True)
    with hf.open("a") as h:
        h.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {'parked' if on else 'unparked'} from the dashboard dock\n")
    return {"ok": True, "goal": goal}


def run(name, prompt, worktree_id=None):
    """Launch an agent in the workspace with `prompt`. Refuses while its agent is working or waiting on an answer."""
    w, ag = next(((w, ag) for w, ag in live_agents().values() if w["worktreeId"] == worktree_id), (None, None)) if worktree_id else \
        next(((w, ag) for w, ag in live_agents().values() if w["displayName"] == name), (None, None))
    if not w:
        return {"error": f"no workspace {name}"}
    if ag.get("state") == "working":
        return {"error": "its agent is still working; wait for it to finish"}
    if ag.get("state") == "waiting":
        return {"error": "its agent is waiting on an answer; answer it in its workspace or the chat"}
    rows = json.loads((greenlight.STATE / "last.json").read_text())["rows"]
    row = next((r for r in rows if r["id"] == w["worktreeId"]), {})
    agent = (row.get("next") or {}).get("agent") or greenlight.CONFIG["agents"][0]
    # ponytail: always a new agent tab; reusing the idle one needs greenlight's empty-input-box check first
    order = list(dict.fromkeys([agent, *greenlight.CONFIG["agents"]]))
    for a in order:
        try:
            greenlight.orca("agent", "launch", "--agent", a, "--worktree", f"id:{w['worktreeId']}", "--prompt", prompt + PUSH_RULE)
            break
        except RuntimeError as e:
            if a == order[-1] or not greenlight.LIMIT_RE.search(str(e)):
                return {"error": str(e)[:300]}
    mr = f"{row['mr'].get('ref') or '!' + str(row['mr']['iid'])} " if row.get("mr") else ""
    hf = greenlight.history_file(name)
    hf.parent.mkdir(parents=True, exist_ok=True)
    with hf.open("a") as h:
        h.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {mr}console → launched {a}: {' '.join(prompt.split())[:300]}\n")
    return {"ok": True, "agent": a}


MENU_RE = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(\S.*)$")


def waiting_term(name):
    """(worktree, agent, terminal handle, terminal read) for the workspace's waiting agent, or an error string."""
    w, ag = next(((w, ag) for w, ag in live_agents().values() if w["displayName"] == name), (None, None))
    if not w:
        return f"no workspace {name}"
    if ag.get("state") != "waiting":
        return "its agent isn't waiting on you any more"
    terms = [t for t in greenlight.orca("terminal", "list", "--limit", "10000")["terminals"]
             if t["worktreeId"] == w["worktreeId"] and t.get("agentIdentity")]
    if pane := ag.get("paneKey"):
        terms = [t for t in terms if f'{t.get("tabId")}:{t.get("leafId")}' == pane]
    if not terms:
        return "no terminal found for its waiting agent"
    h = max(terms, key=lambda t: t.get("lastOutputAt") or 0)["handle"]
    return w, ag, h, greenlight.orca("terminal", "read", "--terminal", h, "--limit", "60")["terminal"]


def menu(tail):
    """A Claude question menu on screen: [(digit, label)], or [] when the agent waits at its normal prompt."""
    lines = [str(x).rstrip() for x in tail][-30:]
    if any(x.strip() == "❯" for x in lines):  # the empty input box: a plain end-of-turn question
        return []
    opts = [(m[1], m[2].strip()) for x in lines if (m := MENU_RE.match(x))]
    return opts if len(opts) >= 2 else []


def picker(tail):
    """The on-screen question above a menu and each option's description: (question, {digit: detail})."""
    lines = [str(x).rstrip() for x in tail][-30:]
    q, details, cur = [], {}, None
    for x in lines:
        if x.lstrip().startswith("☐"):
            q, details, cur = [], {}, None
        elif (m := MENU_RE.match(x)):
            cur = m[1]
        elif x.startswith("│") and cur is None:
            q.append(x[1:].strip())
        elif cur and x[:1].isspace() and x.strip():
            details[cur] = (details.get(cur, "") + " " + x.strip()).strip()
        elif not x.strip() or x.lstrip().startswith(("─", "Enter to select")):
            cur = None if x.lstrip().startswith("─") else cur
    return " ".join(q), details


def ask_user(name, model="claude-haiku-5-5", effort="low"):
    t = waiting_term(name)
    if isinstance(t, str):
        return {"name": name, "error": t}
    w, ag, h, rd = t
    q = (ag.get("lastAssistantMessage") or "").strip()
    opts = menu(rd.get("tail") or [])
    if opts:  # "Type something." needs a second, typed step in the picker; that one is answered in the workspace
        pq, det = picker(rd.get("tail") or [])
        return {"name": name, "question": (pq or q)[-4000:], "kind": "menu", "questions": pending_questions(w.get("path") or ""),
                "options": [{"key": k, "label": l[:200], "detail": det.get(k, "")[:400]} for k, l in opts if l != "Type something."]}
    cache = DASH / "ask" / f"{safe(name)}.json"
    key = hashlib.sha1(q.encode()).hexdigest()[:12]
    try:
        c = json.loads(cache.read_text())
        if c.get("key") == key:
            return c["out"]
    except (OSError, ValueError):
        pass
    out = {"name": name, "question": q[-4000:], "kind": "prompt", "options": []}
    try:
        raw = ask("A coding agent ended its turn with this message to the developer. If it asks the developer to choose or confirm "
                  "something, list up to 3 short replies (each under 12 words, in the developer's voice) that answer it. "
                  "If it asks nothing answerable that way, return []. Return ONLY a JSON array of strings.\n\n" + q[-6000:], model, effort)
        m = re.search(r"\[.*\]", raw, re.S)
        out["options"] = [{"key": "", "label": str(o)[:200]} for o in (json.loads(m[0]) if m else []) if str(o).strip()][:3]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "out": out}))
    except Exception:  # no suggestions is fine: the free-text reply still works
        pass
    return out


def pending_questions(path):
    """The questions of an AskUserQuestion call still waiting on its result in the newest Claude transcript, else None."""
    fs = [f for f, kind in transcript_files(path) if kind == "claude"] if path else []
    if not fs:
        return None
    pend = None
    for line in fs[0].open(errors="replace"):  # ponytail: rescans the whole transcript per ask; tail-read if screenshot-heavy ones get slow
        try:
            content = json.loads(line).get("message", {}).get("content")
        except ValueError:
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                pend = b
            elif b.get("type") == "tool_result" and pend and b.get("tool_use_id") == pend.get("id"):
                pend = None
    qs = ((pend or {}).get("input") or {}).get("questions")
    return qs if isinstance(qs, list) and qs else None


def answer(name, text):
    """Answer the waiting agent. Never types over text the user left unsent in that terminal."""
    t = waiting_term(name)
    if isinstance(t, str):
        return {"error": t}
    w, ag, h, rd = t
    if (rd.get("draft") or "").strip():
        return {"error": "there's unsent text in that agent's input box; answer it in its workspace"}
    opts = menu(rd.get("tail") or [])
    text = " ".join(text.split())  # one line: a newline would submit half the answer
    if opts and text in {k for k, _ in opts}:
        greenlight.orca("terminal", "send", "--terminal", h, "--text", text)
    elif opts:  # Esc closes the picker, then the answer goes in as a plain message
        greenlight.orca("terminal", "send", "--terminal", h, "--text", "\x1b")
        for _ in range(20):  # ponytail: polls ~8s for the picker to close
            time.sleep(0.4)
            if not menu(greenlight.orca("terminal", "read", "--terminal", h, "--limit", "60")["terminal"].get("tail") or []):
                break
        else:
            return {"error": "the agent's menu didn't close; answer it in its workspace"}
        greenlight.orca("terminal", "send", "--terminal", h, "--text", text, "--enter")
    else:
        greenlight.orca("terminal", "send", "--terminal", h, "--text", text, "--enter")
    hf = greenlight.history_file(name)
    hf.parent.mkdir(parents=True, exist_ok=True)
    with hf.open("a") as f:
        f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} you answered the agent: {' '.join(text.split())[:300]}\n")
    return {"ok": True}


def validate(layout):
    """Keep only allowed widgets with dict props, at most 3 tiles and 1 wide panel. The page never runs model output."""
    def ok(w, allowed):
        return isinstance(w, dict) and w.get("widget") in allowed and isinstance(w.get("props"), dict)
    def card(w):
        return {"widget": w["widget"], "title": str(w.get("title") or "")[:40], "props": w["props"]}
    tiles = [card(t) for t in layout.get("tiles") or [] if ok(t, TILES)][:3]
    wide = layout.get("wide")
    puck = layout.get("puck")
    puck = puck if ok(puck, CATALOG) and CATALOG[puck["widget"]][1].startswith("P ") else None
    return {"puck": puck and {"widget": puck["widget"], "props": puck["props"]}, "tiles": tiles,
            "wide": card(wide) if ok(wide, WIDE) else None}


def sig(p):
    return hashlib.sha1(json.dumps([p["state"], p["step"], p["owner"], p["agent"]["state"], (p["mr"] or {}).get("state")]).encode()).hexdigest()[:12]


def cached_page(name):
    try:
        return json.loads((DASH / "pages" / f"{safe(name)}.json").read_text())
    except (OSError, ValueError):
        return {}


def page(name, fresh=False, **designer):
    f = facts(name)
    cache = DASH / "pages" / f"{safe(name)}.json"
    cached = json.loads(cache.read_text()) if cache.exists() else {}
    s = sig(f["puck"])
    if fresh or cached.get("sig") != s or cached.get("v") != PAGE_V:
        try:
            cached = {"v": PAGE_V, "sig": s, "designed": int(time.time()), "layout": design(f, **designer)}
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(cached))
        except Exception as e:  # designer down / out of tokens: keep the old layout, say why
            cached = cached | {"error": f"designer failed: {str(e)[:200]}"}
    return {"name": name, "facts": f, **cached}


def _claude_entries(f):
    out = []
    for line in f.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") or {}
        if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        ct = m.get("content")
        for b in [{"type": "text", "text": ct}] if isinstance(ct, str) else ct or []:
            if b.get("type") == "text":
                t = b.get("text") or ""
                cmd = re.search(r"<command-name>(.*?)</command-name>", t)
                if cmd:
                    t = cmd[1] + " " + (re.search(r"<command-args>(.*?)</command-args>", t, re.S) or [""," "])[1]
                elif t.lstrip().startswith("<"):  # caveats, reminders, local command output
                    continue
                out.append(("user" if d["type"] == "user" else "assistant", t.strip()))
            elif b.get("type") == "tool_use":
                i = b.get("input") or {}
                arg = next((i[k] for k in ("command", "file_path", "pattern", "description", "url", "prompt") if i.get(k)), "")
                out.append(("tool", f"{b.get('name')} {' '.join(str(arg).split())}"))
    return out


def _codex_entries(f):
    out = []
    for line in f.open(errors="replace"):
        try:
            p = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            for b in p.get("content") or []:
                t = b.get("text") or ""
                if t.strip() and not t.lstrip().startswith("<"):
                    out.append((p["role"], t.strip()))
        elif p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            out.append(("tool", f"{p.get('name') or 'shell'} {' '.join(str(p.get('arguments') or p.get('input') or '').split())}"))
    return out


def transcript_files(path):
    """Every agent session that ran in `path`, newest first: Claude Code transcripts and Codex rollouts."""
    fs = [(f, "claude") for f in (Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", path)).glob("*.jsonl")]
    cutoff = time.time() - 30 * 86400
    for f in (Path.home() / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"):  # ponytail: reads each recent rollout's first line; index by cwd if this gets slow
        try:
            if f.stat().st_mtime > cutoff and json.loads(f.open().readline())["payload"].get("cwd") == path:
                fs.append((f, "codex"))
        except (OSError, ValueError, KeyError):
            pass
    return sorted(fs, key=lambda x: x[0].stat().st_mtime, reverse=True)[:40]


RECENT = 300  # seconds


def transcript(name, before=None, limit=60):
    """A page of the conversation in the workspace, oldest first, ending just before `before` ("<file>:<index>").
    Older sessions continue where the newest one starts; `more` is the cursor for the page before this one."""
    w = next((w for w, _ in live_agents().values() if w["displayName"] == name), None)
    if not w:
        return {"error": f"no workspace {name}", "before": before}  # before: the page can tell a failed "Load earlier" from a failed refresh
    files = transcript_files(w["path"])
    f, i = 0, None
    if before:
        stem, _, idx = before.rpartition(":")
        f = next((n for n, (x, _) in enumerate(files) if x.stem == stem), len(files))
        i = int(idx)
    page = []
    while f < len(files) and len(page) < limit:
        path, kind = files[f]
        es = (_claude_entries if kind == "claude" else _codex_entries)(path)
        i = len(es) if i is None else min(i, len(es))
        take = max(0, i - (limit - len(page)))
        page[:0] = [{"id": f"{path.stem}:{n}", "role": r, "text": t[:8000], "kind": kind} for n, (r, t) in enumerate(es[take:i], take)]
        if take == 0:
            page.insert(0, {"id": f"{path.stem}:start", "role": "sep",
                            "text": f"{kind} session · {time.strftime('%b %-d %H:%M', time.localtime(path.stat().st_ctime))}"})
            f, i = f + 1, None
        else:
            i = take
            break
    more = f"{files[f][0].stem}:{999999999 if i is None else i}" if f < len(files) else None  # 999999999: from that session's end
    # the newest page: also the tails of other sessions written to in the last RECENT seconds (a Codex review the main agent
    # started, say), so a busy newest session never starves them out of All output's refresh
    recent = [] if before else [{"id": f"{path.stem}:{n}", "role": r, "text": t[:8000], "kind": kind}
                                for path, kind in files[1:] if time.time() - path.stat().st_mtime < RECENT
                                for es in [(_claude_entries if kind == "claude" else _codex_entries)(path)]
                                for n, (r, t) in enumerate(es[-20:], max(0, len(es) - 20))]
    return {"entries": page, "more": more, "before": before, "recent": recent}


# ---------- New task: a request (+ files) becomes a workspace whose agent starts on it at once ----------
TASKS = greenlight.STATE / "tasks"
FILE_RE = re.compile(r"^data:[\w.+/-]*(?:;[\w=.-]+)*;base64,([A-Za-z0-9+/=\s]*)$")
FILES_CAP = 15 << 20  # raw bytes, all files together; stdin is capped at 20 MB of base64


def repos():
    """Orca repos with a GitHub/GitLab remote: where a task's MR can live."""
    out = []
    for r in greenlight.orca("repo", "list")["repos"]:
        key = (r.get("gitRemoteIdentity") or {}).get("canonicalKey") or ""
        host, _, path = key.partition("/")
        if host in ("gitlab.com", "github.com") and path:
            out.append({"id": r["id"], "name": r["displayName"], "host": host.split(".")[0], "path": path})
    return out


def task_title(text, cap=120):
    """A task's title from what was typed: its first non-empty line, cut to its first sentence, or else at a word with …, to fit cap."""
    line = next((" ".join(l.split()) for l in str(text or "").splitlines() if l.strip()), "")
    if len(line) <= cap:
        return line
    m = re.search(r"[.!?](?=\s|$)", line[:cap + 1])
    if m and m.end() <= cap:
        return line[:m.end()]
    cut = line[:cap].rfind(" ")  # at most cap - 1 chars before it: room for the …
    return (line[:cut].rstrip() if cut > 0 else line[:cap - 1]) + "…"


REQUEST_CAP = 8000  # of the request quoted in the brief: Orca passes it as --prompt argv


def _note(name, text):
    hf = greenlight.history_file(name)
    hf.parent.mkdir(parents=True, exist_ok=True)
    with hf.open("a") as h:
        h.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {text}\n")


def _slug(title, n=40):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:n].rstrip("-") or "untitled"


def _free(base, taken):
    return next(n for n in (base if i == 1 else f"{base}-{i}" for i in range(1, 100)) if n not in taken)


def _files(files):
    """[(safe basename, bytes)] from [{name, data: data URL}], or an error string. Names are the user's; the bytes stay opaque."""
    out, seen, total = [], set(), 0
    for f in files or []:
        m = FILE_RE.match(str((f or {}).get("data") or ""))
        if not m:
            return f"{(f or {}).get('name') or 'a file'} didn't come through"
        raw = base64.b64decode(m[1])
        total += len(raw)
        if total > FILES_CAP:
            return "attachments are capped at 15 MB together"
        name = re.sub(r"[^\w.+-]+", "-", Path(str(f.get("name") or "")).name).lstrip(".-")[:80] or "file"
        name = name if name not in seen else f"{len(out) + 1}-{name}"
        seen.add(name)
        out.append((name, raw))
    return out


def preflight(goal, context, o):
    """lifestage.preflight on a New task / New workspace payload: its `answers` ({question id: text}) and `force` (start anyway)."""
    answers = o.get("answers") if isinstance(o.get("answers"), dict) else None
    return lifestage.preflight(goal, context, answers, o.get("force") is True)


def paused(gate):
    """The reply for a goal preflight wants to ask about first: the questions with their options, and the verdict behind them."""
    return {"paused": True, **{k: gate.get(k) for k in ("questions", "p_ambiguous", "clarity", "provider", "why")}}


def task_new(repo_name, payload):
    """Start a task: a workspace whose goal is the title (greenlight's freeform step) and whose agent gets the whole request and the files."""
    repo = next((r for r in repos() if r["name"] == repo_name), None)
    if not repo:
        return {"error": f"no repo {repo_name} with a GitHub/GitLab remote"}
    request = str(payload.get("text") or "").strip()
    title = task_title(request)
    if not title:
        return {"error": "describe the task first"}
    files = _files(payload.get("files"))
    if isinstance(files, str):
        return {"error": files}
    gate = preflight(title, request, payload)
    if gate["pause"]:  # 1.3: ask before the workspace and its agent exist
        return paused(gate)
    name = _free(_slug(title), {w["displayName"] for w, _ in live_agents().values()} | {d.name for d in TASKS.glob("*")})
    d = TASKS / name
    (d / "files").mkdir(parents=True)
    (d / "request.md").write_text(request + "\n")
    for fn, raw in files:
        (d / "files" / fn).write_bytes(raw)
    quoted = request if len(request) <= REQUEST_CAP else request[:REQUEST_CAP] + f"\n[… cut here: the rest is in {d}/request.md]"
    brief = (f"Your task in {repo['path']}: {title}. My whole request is below (verbatim in {d}/request.md)."
             + (f" I attached {len(files)} file{'s' * (len(files) > 1)}, in {d}/files: {', '.join(fn for fn, _ in files)}. Read them first."
                if files else "")
             + " Follow my request and clarifying answers. If they ask for a plan or investigation first, stop at that deliverable. "
               "Otherwise implement it in this workspace with tests, then commit, push and open a draft MR (a draft PR on GitHub). If something is unclear, ask me with the "
               f"AskUserQuestion tool (2-4 options, your recommendation first) rather than guess. Before you open the MR, run its tests with "
               f"`python3 {Path(lifestage.__file__).resolve()} selftest -- <the test command>` in the worktree, so the self-test is on record."
              + f"\n\nMy request:\n\n{quoted}{gate['brief']}" + PUSH_RULE)
    for a in greenlight.CONFIG["agents"]:
        try:
            res = greenlight.orca("worktree", "create", "--repo", f"id:{repo['id']}", "--name", name, "--setup", "skip",
                             "--comment", title, "--no-parent", "--agent", a, "--prompt", brief)
            break
        except RuntimeError as e:
            if a == greenlight.CONFIG["agents"][-1] or not greenlight.LIMIT_RE.search(str(e)):
                return {"error": f"couldn't start the workspace: {str(e)[:300]}"}
    wid = (res.get("worktree") or {}).get("id")
    if wid:  # keep Orca from prefixing the name; cosmetic, so a failure here doesn't fail the task
        try:
            greenlight.orca("worktree", "set", "--worktree", f"id:{wid}", "--display-name", name)
        except RuntimeError:
            pass
    lifestage.save("preflight", name, gate["record"])
    if path := (res.get("worktree") or {}).get("path"):
        lifestage.save("preflight", path, gate["record"])
    _note(name, f"task started ({a}, {len(files)} file{'s' * (len(files) != 1)}): {title} [preflight: {gate['why']}]")
    return {"ok": True, "name": name, "repo": repo["name"]}


def task(op, arg, stdin):
    if op == "repos":
        return {"repos": [{k: r[k] for k in ("name", "host", "path")} for r in repos()]}
    if op == "new":
        return task_new(arg, json.loads(stdin or "{}"))
    return settings(op, arg, stdin)


MEMORY = greenlight.ROOT / "memory"


def settings(op, arg, stdin):
    """The app's Settings: a repo's lifecycle, greenlight's standing instructions, and greenlight's memory notes."""
    if op.startswith("prompt-"):  # the hover card's prompts: arg <repo>/<kind>; yours in ~/.greenlight/prompts/<repo>/<kind>.md
        repo, _, kind = arg.partition("/")
        if kind not in KINDS or not re.fullmatch(r"[\w.-]{1,100}", repo) or repo.startswith("."):
            return {"error": "bad repo or kind"}
        f = own_prompt(repo, kind)
        if op == "prompt-set" and stdin.strip():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(stdin.strip() + "\n")
        elif op == "prompt-set":
            f.unlink(missing_ok=True)
        elif op != "prompt-get":
            return {"error": f"no op {op}"}
        return {"kind": kind, "text": f.read_text() if f.exists() else "", "default": (PROMPTS / f"{kind}.md").read_text().strip(),
                "own": f.exists(), "vars": COMMON + KINDS[kind]}
    if op in ("autorun-get", "autorun-set"):  # arg <repo>; set's stdin {kind: gate index}, a kind left out is off
        if not re.fullmatch(r"[\w.-]{1,100}", arg) or arg.startswith("."):
            return {"error": "bad repo"}
        a = autorun()
        if op == "autorun-set":
            on = json.loads(stdin or "{}")
            a[arg] = {k: g for k, g in on.items() if k in AUTO and isinstance(g, int) and 0 <= g < max(1, len(AUTO[k][1]))}
            (greenlight.STATE / "autorun.json").write_text(json.dumps(a, indent=1))
        return {"on": a.get(arg, {}), "kinds": [{"kind": k, "when": w, "gates": [g for g, _ in gs]} for k, (w, gs) in AUTO.items()]}
    if op.startswith("lifecycle-") and (not re.fullmatch(r"[\w.-]{1,100}", arg) or arg.startswith(".")):
        return {"error": "bad repo"}
    if op.startswith("lifecycle-"):
        f = greenlight.STATE / "lifecycles" / f"{arg}.md"
        if op == "lifecycle-set":
            md = greenlight.lifecycle_md(arg, json.loads(stdin))
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(md)
        elif op == "lifecycle-reset":
            f.unlink(missing_ok=True)
        elif op != "lifecycle-get":
            return {"error": f"no op {op}"}
        return {**greenlight.lifecycle_doc(arg), "builtins": greenlight.BUILTIN}
    if op in ("instructions-get", "instructions-set"):
        f = greenlight.STATE / "instructions.md"
        if op == "instructions-set":
            f.write_text(stdin.strip() + "\n") if stdin.strip() else f.unlink(missing_ok=True)
        return {"text": f.read_text() if f.exists() else ""}
    if op.startswith("memory-"):
        index = MEMORY / "MEMORY.md"
        lines = index.read_text().splitlines() if index.exists() else []
        if op != "memory-list":
            if not re.fullmatch(r"[\w-]{1,100}", arg) or arg == "MEMORY":
                return {"error": "bad note name"}
            f, link = MEMORY / f"{arg}.md", f"]({arg}.md)"
            keep = [ln for ln in lines if link not in ln]
            if op == "memory-set":
                MEMORY.mkdir(parents=True, exist_ok=True)
                f.write_text(stdin.rstrip() + "\n")
                desc = re.search(r"^description:\s*(.+)$", stdin, re.M)
                keep.append(f"- [{arg}]({arg}.md) — {desc[1].strip() if desc else arg}")
            elif op == "memory-delete":
                f.unlink(missing_ok=True)
            else:
                return {"error": f"no op {op}"}
            index.write_text("\n".join(keep) + "\n")
        return {"notes": [{"name": f.stem, "text": f.read_text()} for f in sorted(MEMORY.glob("*.md")) if f.name != "MEMORY.md"]}
    return {"error": f"no task op {op}"}



def oops(a, e):
    """A dash op's caught failure, for diag: the op, the exception type and class, never the message."""
    diag.event("dash_error", op=a[0], sub=a[1] if a[0] in ("task", "tree") else None, **diag.failure(e))

def main():
    a = sys.argv[1:]
    if a[:1] == ["snapshot"]:
        print(json.dumps(snapshot()))
    elif a[:1] == ["stream"]:
        every = float(a[1]) if len(a) > 1 else 10
        while True:
            try:
                s = snapshot()
                record(s["pucks"])
                print(json.dumps(s), flush=True)
            except Exception as e:
                oops(a, e)
                print(json.dumps({"error": str(e)[:300]}), flush=True)
            time.sleep(every)
    elif a[:1] == ["page"] and len(a) > 1 and NAME_RE.match(a[1]):
        opt = {k: a[a.index("--" + k) + 1] for k in ("model", "effort") if "--" + k in a[:-1]}
        if not all(re.fullmatch(r"[\w.-]{1,40}", v) for v in opt.values()):
            sys.exit("bad --model/--effort")
        print(json.dumps(page(a[1], "--fresh" in a, **opt)))
    elif a[:1] == ["suggest"] and len(a) > 1 and NAME_RE.match(a[1]):
        opt = {k: a[a.index("--" + k) + 1] for k in ("model", "effort") if "--" + k in a[:-1]}
        if not all(re.fullmatch(r"[\w.-]{1,40}", v) for v in opt.values()):
            sys.exit("bad --model/--effort")
        try:
            print(json.dumps(suggest(a[1], **opt)))
        except Exception as e:
            oops(a, e)
            print(json.dumps({"name": a[1], "error": str(e)[:200]}))
    elif a[:1] == ["run"] and len(a) > 1 and NAME_RE.match(a[1]):
        prompt = sys.stdin.read().strip()[:8000]
        print(json.dumps({"name": a[1], **(run(a[1], prompt) if prompt else {"error": "empty prompt"})}))
    elif a[:1] in (["ask"], ["answer"]) and len(a) > 1 and NAME_RE.match(a[1]):
        opt = {k: a[a.index("--" + k) + 1] for k in ("model", "effort") if "--" + k in a[:-1]}
        if not all(re.fullmatch(r"[\w.-]{1,40}", v) for v in opt.values()):
            sys.exit("bad --model/--effort")
        try:
            if a[0] == "ask":
                print(json.dumps(ask_user(a[1], **opt)))
            else:
                text = sys.stdin.read().strip()[:4000]
                print(json.dumps({"name": a[1], **(answer(a[1], text) if text else {"error": "empty answer"})}))
        except Exception as e:
            oops(a, e)
            print(json.dumps({"name": a[1], "error": str(e)[:200]}))
    elif a[:1] == ["park"] and len(a) == 3 and NAME_RE.match(a[1]) and a[2] in ("on", "off"):
        try:
            print(json.dumps({"name": a[1], **park(a[1], a[2] == "on")}))
        except Exception as e:
            oops(a, e)
            print(json.dumps({"name": a[1], "error": str(e)[:200]}))
    elif a[:1] == ["task"] and len(a) == 3 and NAME_RE.match(a[2]):
        try:
            print(json.dumps({"op": a[1], "arg": a[2], **task(a[1], a[2], sys.stdin.read().strip()[:20 << 20])}))
        except Exception as e:
            oops(a, e)
            print(json.dumps({"op": a[1], "arg": a[2], "error": str(e)[:300]}))
    elif a[:1] == ["transcript"] and len(a) > 1 and NAME_RE.match(a[1]):
        before = a[a.index("--before") + 1] if "--before" in a[:-1] else None
        if before and not re.fullmatch(r"[\w.-]{1,200}:\d{1,9}", before):
            sys.exit("bad --before")
        print(json.dumps({"name": a[1], **transcript(a[1], before)}))
    elif a[:1] == ["board"]:
        print(board())
    elif a[:1] == ["forge"] and (len(a) == 1 or len(a) == 2 and re.fullmatch(r"[\w.-]+(/[\w.-]+)+", a[1])):
        print(forge(a[1] if len(a) == 2 else None))
    elif a[:1] == ["trees"]:
        print(json.dumps({"trees": trees()}))
    elif a[:1] == ["tree"] and len(a) >= 3 and a[1] in ("info", "rm", "goal", "cfg", "merge", "auto-merge", "ready", "signoff", "new", "diff") and re.fullmatch(r"/[\w./@+-]{1,300}", a[2]) and ".." not in a[2]:
        try:
            op, arg = a[1], a[2]
            out = tree_info(arg) if op == "info" else tree_merge(arg, automatic=op == "auto-merge") if op in ("merge", "auto-merge") else tree_ready(arg) if op == "ready" else tree_signoff(arg) if op == "signoff" else tree_rm(arg, "--force" in a[3:], sys.stdin.read()[:2000]) if op == "rm" \
                else tree_goal(arg, sys.stdin.read()[:2000]) if op == "goal" else tree_diff(arg, sys.stdin.read(20).strip()) if op == "diff" \
                else tree_new(arg, json.loads(sys.stdin.read(8193) or "{}")) if op == "new" else tree_cfg(arg, sys.stdin.read(8193))
        except Exception as e:
            oops(a, e)
            out = {"error": str(e)[:300]}
        print(json.dumps({"op": a[1], "path": a[2], **out}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    diag.install("dash")
    main()
