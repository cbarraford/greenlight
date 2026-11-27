#!/usr/bin/env python3
"""The deterministic orchestrator: one greenlight run as a Python state machine instead of an LLM session.

    orchestrate.py tick [--json]    adopt → scan/report → state cards → next.cmd → goal backfill → dispatch → cleanup
    orchestrate.py plan [--json]    the same decisions from the last scan, nothing run (no adopt, scan, cleanup or decide calls)

Each phase is the same step SKILL.md describes, done the same way:
  adopt      `adopt.py apply` (workspaces for open MRs that have none)
  scan       `greenlight.py report` (last.json + report.html; it closes stale pings)
  cards      ws_state.observe() on every row: the handoff card, updated at each transition
  next.cmd   a `greenlight` row's `next.cmd`, run exactly (glab/gh only; a merge is skipped when the goal says hold)
  backfill   an MR row with no goal gets "Merge !N: <title>" / "Review !N: <title>" (+ --gitlab-mr when Orca has no link),
             carrying "keep draft" / "hold" from the old comment
  dispatch   an `agent` row with no agent on it gets one, launched with a templated brief: the repo's own prompt for the
             step (prompts/ci|conflicts|threads.md, as the dashboard's buttons), else a /goal brief (goal, lifecycle file,
             next action, MR, the lifecycle's Rules verbatim), and always the workspace's state card. The agent is
             next.agent or config agents[0]; on a usage limit the next one takes it with the card and a handoff note.
  cleanup    `cleanup.py apply`
The only judgment calls go to decide.py (System One), and only at ambiguous forks: a step that an earlier dispatch
didn't move (same step at the same commit, or the step's third dispatch), or a free-form goal an agent has already
worked on. decide.choice picks redispatch / next agent / ask the user; below config orchestrator.min_confidence
(default 0.75), or with decide.py's local fallback, it asks the user (one ping, `@stuck:<ws>`). Each fork is judged
once per situation (cached in the card). `me`, parked, working, ci, reviewers, author and dependency rows are left alone,
as in SKILL.md. Every mutation is logged to log.jsonl and the workspace's history.

config.json "orchestrator": {"max_dispatch": 5, "min_confidence": 0.75}
"""
import fcntl
import contextlib
import hashlib
import uuid
import urllib.parse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diag  # noqa: E402
import greenlight  # noqa: E402
import ws_state  # noqa: E402

HERE = Path(__file__).resolve().parent
OPTS = {"max_dispatch": 5, "max_forge_actions": 5, "min_confidence": 0.75} | (greenlight.CONFIG.get("orchestrator") or {})
LOG = greenlight.STATE / "log.jsonl"
TICK_LOCK_FD = None  # inherited by each mutating child so a killed parent cannot release the tick lock early
FIX_KIND = {"ci_green": "ci", "no_conflicts": "conflicts", "threads": "threads", "verified": "verify"}  # step → prompts/<kind>.md
MAX_SAME_STEP = 3  # the third dispatch for one step is a fork, whatever the commit
FORK = ["dispatch again", "hand it to the next agent", "ask the user"]
BRIEF = """/goal {goal}

Workspace {name} ({project}). Follow the lifecycle in {lifecycle} end to end: carry the goal through every agent step in it{custom_hint}.
Next step: {step}: {action}
MR: {url}{extra}

Rules for this task:
{rules}{repo_rules}"""


def _brief_rules(doc):
    """Build default brief rules from the repo's actual lifecycle stages instead of assuming every repo has the same cycle."""
    keys = {s["key"] for s in doc.get("stages") or []}
    lines = []
    if not keys or "threads" in keys:
        lines += ["- verify each review claim with a refuting subagent before acting on it;",
                  "- fix it with a test, or reply inline with evidence, then resolve the thread (always leave a comment);"]
    if not keys or "ci_green" in keys:
        lines.append("- get CI green;")
    if not keys or "ready" in keys:
        lines.append("- keep drafts as drafts.")
    return "\n".join(lines) or "- follow the lifecycle stages and rules below."


def _custom_hint(doc):
    custom = [s["key"] for s in doc.get("stages") or []
              if s["key"] not in greenlight.BUILTIN and s.get("done") != "signoff" and (s.get("owner") or "agent") == "agent"]
    if not custom:
        return ""
    return ", including " + ", ".join(f"`{k}`" for k in custom)


def decide_choice(state, question, options, context=""):
    """decide.choice (scripts/decide.py, System One) when it's there, else its contract's local low-confidence answer."""
    try:
        import decide
    except ImportError:
        decide = None
    if decide is not None:
        try:
            return decide.choice(state, question, options, context=context)
        except Exception as e:  # noqa: BLE001 - a backend error is a non-answer, never a crash
            err = f"decide error: {str(e)[:120]}"
        else:
            err = None
    else:
        err = None
    p = round(1 / len(options), 3)
    return {"choice": options[-1], "index": len(options) - 1, "probs": {o: p for o in options}, "confidence": p,
            "provider": err or "local"}


def log(entry):
    with LOG.open("a") as fh:
        fh.write(json.dumps({"ts": int(time.time())} | entry) + "\n")
    diag.event("orch", did=str(entry.get("did") or "").split(" ")[0], ws_h=diag.h(entry.get("ws")), mr_h=diag.h(entry.get("mr")),
               ok=entry.get("ok"), step=entry.get("step"))


def ref_of(r):
    mr = r.get("mr") or {}
    return mr.get("ref") or (f"!{mr['iid']}" if mr.get("iid") else "")


def contract(r):
    return ws_state.contract(r)


def head(r):
    p = r.get("path") or ""
    if not os.path.isdir(p):
        return ""
    out = subprocess.run(["git", "-C", p, "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip()


def current_mr_state(r):
    mr = r.get("mr") or {}
    if not mr.get("iid"):
        return None
    live = greenlight.mr_details(r["project"], mr["iid"], r["forge"])
    candidate = greenlight.release_milestone(r["project"], r["forge"]) if not live.get("milestone") and "milestone" in greenlight.lifecycle(r["project"])[1] else None
    nxt = greenlight.evaluate(r, live, candidate)
    threads = (hashlib.sha1(json.dumps(sorted(greenlight.thread_line(d) for d in greenlight.unresolved(live))).encode()).hexdigest()[:12]
               if (r.get("mr") or {}).get("threads_fingerprint") is not None else None)
    return {"sha": live.get("sha"), "target_branch": live.get("target_branch"), "step": nxt["step"], "owner": nxt["owner"],
            "action": nxt["action"], "threads_fingerprint": threads,
            "milestone_cmd": nxt.get("cmd") if nxt["step"] == "milestone" else None}


def scanned_mr_state(r):
    nxt = r.get("_base_next") or r["next"]
    return {"sha": (r.get("mr") or {}).get("sha"), "target_branch": (r.get("mr") or {}).get("target_branch"),
            "step": nxt["step"], "owner": nxt["owner"], "action": nxt["action"],
            "threads_fingerprint": (r.get("mr") or {}).get("threads_fingerprint"),
            "milestone_cmd": nxt.get("cmd") if nxt["step"] == "milestone" else None}


def branch_has_open_mr(r):
    """A free-form dispatch needs a fresh scan when a request opened after its scan."""
    branches = {r["branch"]}
    if os.path.isdir(r.get("path") or ""):
        upstream = subprocess.run(["git", "-C", r["path"], "config", f"branch.{r['branch']}.merge"],
                                  capture_output=True, text=True).stdout.strip().removeprefix("refs/heads/")
        if upstream:
            branches.add(upstream)
    for branch in branches:
        if r["forge"] == "github":
            hits = greenlight.gh("pr", "list", "-R", r["project"], "--head", branch, "--state", "open", "--json", "number")
        else:
            query = urllib.parse.urlencode({"source_branch": branch, "state": "opened", "per_page": 1})
            hits = greenlight.gl(f"projects/{greenlight.pid(r['project'])}/merge_requests?{query}")
        if hits:
            return True
    return False


def active_agent_blocks(live, r, rate_limit_handoff=False):
    active = [a for a in live.get("agents") or [] if a.get("state") in ("working", "waiting")]
    scanned = r.get("agent") or {}
    if not rate_limit_handoff and scanned.get("state") != "done":
        return bool(active)
    if not active:
        return False
    try:
        terminals = greenlight.orca("terminal", "list", "--limit", "10000")["terminals"]
    except Exception:
        return True
    for a in active:
        pane = a.get("paneKey")
        if not pane:
            return True
        probe = r | {"agent": {"state": a["state"], "type": a.get("agentType") or a.get("type"), "pane_key": pane}}
        try:
            seen = greenlight.screen(probe, {r["id"]: terminals})
        except Exception:
            return True
        if not seen or seen.get("prob", 0) < greenlight.SURE:
            return True
        expected = "rate_limited" if rate_limit_handoff and pane == scanned.get("pane_key") else "finished"
        if seen.get("state") == expected and (expected == "rate_limited" or seen.get("idle_prompt")):
            continue
        if pane != scanned.get("pane_key") and seen.get("state") == "rate_limited":
            continue  # an older exhausted pane is not another live worker
        return True
    return False


def merge_agent_blocks(live, r):
    active = [a for a in live.get("agents") or [] if a.get("state") in ("working", "waiting")]
    if not active:
        return False
    if any(a.get("state") == "waiting" for a in active):
        return True  # a stale user prompt is never sufficient evidence to merge
    pane = (r.get("agent") or {}).get("pane_key")
    if not pane or any(a.get("paneKey") != pane for a in active):
        return True
    try:
        terminals = greenlight.orca("terminal", "list", "--limit", "10000")["terminals"]
        seen = greenlight.screen(r, {r["id"]: terminals})
    except Exception:
        return True
    return not (seen and seen.get("prob", 0) >= greenlight.SURE and seen.get("state") == "finished" and seen.get("idle_prompt"))


def sig(r):
    """The situation a dispatch is for: the step at a commit (a free-form goal: the goal itself)."""
    nxt = r["next"]
    if nxt["step"] == "freeform":
        return f"freeform:{ws_state.digest(r.get('goal') or '')}"
    return f"{nxt['step']}@{(r.get('mr') or {}).get('sha') or head(r)}"


def keyed(rows, migrate=True):
    return ws_state.keyed(rows, migrate=migrate)


def fork(r, card):
    """Why this dispatch needs judgment, or None when it's mechanical."""
    s, step = sig(r), r["next"]["step"]
    same = [d for d in card["dispatches"] if d.get("sig") == s]
    nxt_agent = r["next"].get("agent")
    if same and "out of tokens" in r["next"]["action"] and nxt_agent and nxt_agent not in {d["agent"] for d in same}:
        return None  # the agent we sent hit its limit mid-task: the next one takes it (SKILL.md), no judgment needed
    if same:
        return f"a {same[0]['agent']} dispatch for this same situation ({s}) didn't move it"
    if sum(d.get("step") == step for d in card["dispatches"]) >= MAX_SAME_STEP - 1:
        return f"{MAX_SAME_STEP - 1} dispatches for step {step} haven't passed it"
    return None


def brief(r, card, handoff=""):
    """The templated dispatch prompt."""
    nxt, mr = r["next"], r.get("mr") or {}
    body = None
    if (kind := FIX_KIND.get(nxt["step"])) and mr:
        import dash  # noqa: PLC0415 - the dashboard's own buttons' prompts, the user's per-repo ones included
        body = next((m.get("auto") or m["prompt"] for m in dash.moves(r) if m["kind"] == kind), None)
    if body is None:
        try:
            doc = greenlight.lifecycle_doc(r["project"]) if r.get("project") else {"file": "(no MR: free-form goal)", "rules": "", "stages": []}
        except Exception:  # noqa: BLE001
            doc = {"file": "(unknown)", "rules": "", "stages": []}
        extra = "".join(f"\nFailed job: {j.get('name')} ({j.get('stage')}): {j.get('url')}" for j in (mr.get("failed_jobs") or [])[:10])
        extra += "".join(f"\nThread: {t}" for t in (mr.get("todo_threads") or [])[:10])
        if (v := mr.get("verify") or {}).get("phase") == "fix":  # the verified step's defects, back to the builder: fix and push, don't record a verdict
            extra += "".join(f"\nVerification defect: {d}" for d in (v.get("defects") or [])[:10])
            extra += "".join(f"\nCould not prove: {u}" for u in (v.get("unproven") or [])[:10])
            extra += f"\nVerification evidence: {v['evidence']}" if v.get("evidence") else ""
            extra += "\nPush a fix as a new commit; a fresh verifier checks it. Don't record a verdict yourself."
        body = BRIEF.format(goal=r.get("goal") or mr.get("title") or nxt["action"], name=r["name"], project=r.get("project"),
                            lifecycle=doc.get("file") or "(no MR: free-form goal)", custom_hint=_custom_hint(doc),
                            step=nxt["step"], action=nxt["action"], url=mr.get("web_url") or "(none yet)",
                            extra=extra, rules=_brief_rules(doc), repo_rules=f"\n\nThe repo's standing rules:\n{doc['rules']}" if doc["rules"] else "")
    return body + (f"\n\n{handoff}" if handoff else "") + "\n\n" + ws_state.brief(card)


def launch(agent, wid, prompt):
    greenlight.orca("agent", "launch", "--agent", agent, "--worktree", f"id:{wid}", "--prompt", prompt)


def dispatch(r, card, first=None, why_handoff="", assignment=None, exhausted=(), peers=()):
    """Launch an agent; on a usage limit the next of config agents takes the task, with the card and a handoff note.
    Returns (agent or None, error)."""
    agents = greenlight.CONFIG.get("agents") or []
    lead = first or r["next"].get("agent") or (agents[0] if agents else None)
    order = [a for a in dict.fromkeys([*([lead] if lead else []), *agents]) if a not in exhausted]
    if not order:
        return None, "rejected: all configured agents are exhausted"
    handoff, err = why_handoff, ""
    for i, a in enumerate(order):
        if i:
            try:
                live = live_worktree(r)
                if live is None or branch_changed(r, live) or link_changed(r, live) or \
                        (live.get("comment") or "").strip() != (r.get("comment") or "").strip() or \
                        active_agent_blocks(live, r, bool(exhausted)) or \
                        (r.get("mr") or {}).get("iid") and current_mr_state(r) != scanned_mr_state(r) or \
                        assignment is not None and ws_state.situation_token(ws_state.load(r["card"])) != assignment:
                    return None, "rejected: workspace or request changed before quota fallback"
                if not (r.get("mr") or {}).get("iid") and branch_has_open_mr(r):
                    return None, "rejected: a new MR opened before quota fallback"
                if (r.get("mr") or {}).get("iid"):
                    known = {r["id"]}
                    for peer in peers:
                        if peer["id"] == r["id"]:
                            continue
                        if (peer.get("forge"), peer.get("project"), (peer.get("mr") or {}).get("iid")) != \
                                (r.get("forge"), r.get("project"), r["mr"]["iid"]):
                            continue
                        known.add(peer["id"])
                        other = live_worktree(peer)
                        if other is None or any(x.get("state") in ("working", "waiting") for x in other.get("agents") or []) or \
                                any(d.get("pending") for d in ws_state.load(peer["card"])["dispatches"]):
                            return None, "rejected: linked workspace active or pending before quota fallback"
                    link_field = "linkedPR" if r.get("forge") == "github" else "linkedGitLabMR"
                    current = greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
                    projects_by_repo = None
                    for w in current:
                        same_link = str(greenlight.linked_number(w.get(link_field)) or "") == str(r["mr"]["iid"])
                        same_branch = (w.get("branch") or "").removeprefix("refs/heads/") == (r["mr"].get("source_branch") or r.get("branch"))
                        if w.get("worktreeId") in known or not (same_link or same_branch):
                            continue
                        if live.get("repoId") and w.get("repoId") and live["repoId"] != w["repoId"]:
                            if projects_by_repo is None:
                                full = greenlight.orca("worktree", "list", "--limit", "10000")["worktrees"]
                                projects_by_repo = {x["repoId"]: greenlight.forge_project(x.get("projectId")) for x in full if x.get("projectId")}
                            project = greenlight.forge_project(w.get("projectId")) or projects_by_repo.get(w["repoId"])
                            if project != (r.get("forge"), r.get("project")):
                                if project is None:
                                    return None, "rejected: linked repository unknown before quota fallback"
                                continue
                        if any(x.get("state") in ("working", "waiting") for x in w.get("agents") or []):
                            return None, "rejected: newly linked workspace active before quota fallback"
            except Exception as e:
                return None, f"rejected: could not recheck workspace before quota fallback: {str(e)[:100]}"
        try:
            launch(a, r["id"], brief(r, card, handoff))
        except Exception as e:  # noqa: BLE001 - a timeout or an adapter's error is a failed launch too
            err = str(e)[:300] or type(e).__name__
            quota = greenlight.LIMIT_RE.search(err) or greenlight.LIMIT_RE.search(err.removeprefix("orca agent launch: "))
            if i == len(order) - 1 or not quota:
                return None, err
            try:
                card = ws_state.update(r["card"], if_situation=assignment,
                                       decision=f"greenlight: {a} hit its usage limit; handed to {order[i + 1]}")
            except SystemExit:
                return None, "card assignment changed during quota failover"
            handoff = (f"You take over from {a}, which hit its usage limit before starting. The card below is what's known.")
            continue
        return a, ""
    return None, err


def held(r, goal=None):
    """The goal says hold. The MR title it copies in doesn't count ("Hold inbound txs" is a name), as in greenlight.said."""
    ws = r if goal is None else {**r, "goal": goal, "comment": goal}
    if goal is not None:
        ws.pop("_intents", None)
    return bool(greenlight.intents(ws, r.get("mr") or {})["is_held"])


def stay_draft(r, goal=None):
    ws = r if goal is None else {**r, "goal": goal, "comment": goal}
    if goal is not None:
        ws.pop("_intents", None)
    return bool(greenlight.intents(ws, r.get("mr") or {})["stay_draft"])


def backfill_goal(r):
    """The goal an MR row with none gets, or None."""
    mr = r.get("mr") or {}
    if r.get("goal") or not mr.get("iid") or mr.get("state") != "opened":
        return None
    goal = f"{'Merge' if mr.get('role') == 'author' else 'Review'} {ref_of(r)}: {mr.get('title') or ''}".strip()
    old = r.get("comment") or ""
    return goal + (f"\n\nPrevious workspace note: {old}" if old else "")


def store_backfill(r, goal):
    """Remember a generated goal without racing a user's edit to the Orca comment."""
    record = {"goal": goal, "comment": (r.get("comment") or "").strip(), "branch": r.get("branch"),
              "linked_mr": r.get("linked_mr"), "resolved_mr": (r.get("mr") or {}).get("iid"),
              "forge": r.get("forge"), "project": r.get("project")}
    greenlight.backfills_update(lambda data: data.__setitem__(r["id"], record))


def _drift_confirmed_in_inbox(drift, records):
    pk = (drift or {}).get("ping_key")
    if not pk:
        return False
    ans = next((d for d in reversed(records) if d.get("key") == pk and (d.get("answered") or 0) >= drift.get("ts", 0)), None)
    return bool(ans and ans.get("choice") == 0)


def _steward_drift_action(card, sha, target, drift):
    if ws_state.is_stale_confirmed(card, sha, target):
        return {"do": "skip", "why": "waiting for scan to catch up after drift confirmation"}
    if card.get("steward"):
        if card.get("steward_target") and target and target != card["steward_target"]:
            if drift and drift.get("kind") == "target_changed" and drift.get("target") == target:
                return {"do": "skip", "why": "paused on unconfirmed target_changed"}
            return {"do": "drift", "kind": "target_changed", "old_value": card["steward_target"],
                    "new_value": target, "sha": sha, "target": target}
        if card.get("steward_head") and sha and sha != card["steward_head"] and not card.get("steward_expecting_from_sha"):
            if drift and drift.get("kind") == "head_changed" and drift.get("sha") == sha:
                return {"do": "skip", "why": "paused on unconfirmed head_changed"}
            return {"do": "drift", "kind": "head_changed", "old_value": card["steward_head"],
                    "new_value": sha, "sha": sha, "target": target}
    return {"do": "skip", "why": f"paused on unconfirmed {drift.get('kind', 'drift')}"} if drift else None


def _interrupted_rereview_guard(r, card, sha, target, records):
    """Hold re-review while an interrupted-launch @stuck fork awaits the user's answer."""
    if not any(d.get("sig") == f"rereview@{sha}" and d.get("agent") == "launch interrupted" for d in card.get("dispatches") or []):
        return None
    s = sig(r)
    fork_v = (card.get("forks") or {}).get(s) or {}
    if not (fork_v.get("done") and fork_v.get("choice") == FORK[-1]):
        return None
    recs = ws_state.decision_records() if records is None else records
    ans = next((d for d in reversed(recs) if d.get("key") == fork_v.get("ping_key")
                and (d.get("answered") or 0) >= fork_v.get("ts", 0)), None)
    if not ans:
        return {"do": "skip", "why": "interrupted re-review launch needs the user's answer"}
    if ans.get("choice") != 0:
        return {"do": "skip", "why": "the user left the interrupted re-review launch parked"}
    return {"do": "rereview", "sha": sha, "target": target, "clear_interrupted": s}


def _steward_plan_guard(r, card, records=None):
    """Check steward-mode head/target drift and post-agent diff re-review before normal orchestrate actions."""
    mr = r.get("mr") or {}
    if not mr.get("iid") or mr.get("state") != "opened":
        return None
    sha, target = mr.get("sha") or "", mr.get("target_branch") or ""
    drift = card.get("steward_drift")
    if drift and _drift_confirmed_in_inbox(drift, ws_state.decision_records() if records is None else records):
        return {"do": "confirm_drift"}
    if card.get("steward") or drift:
        if act := _steward_drift_action(card, sha, target, drift):
            return act
    if not card.get("steward") or ((r.get("agent") or {}).get("state") or "none") in ("working", "waiting"):
        return None
    if any(d.get("pending") for d in card.get("dispatches") or []):
        return None  # let plan()'s pending/interrupted-launch handler settle uncertain launches first
    pending_sha = card.get("steward_pending_rereview_sha") or ""
    if sha and pending_sha == sha and sha != card.get("steward_reviewed_sha"):
        return _interrupted_rereview_guard(r, card, sha, target, records) or {"do": "rereview", "sha": sha, "target": target}
    return None


def _act_confirm_drift(r, name):
    """Apply a user's inbox confirmation of a pending head/target drift pause."""
    c, pk = ws_state.confirm_drift(r["card"])
    if pk and pk in greenlight.pinged():
        with contextlib.suppress(Exception):
            greenlight.resolve(pk, "confirmed via inbox")
    log({"ws": name, "mr": ref_of(r), "did": "confirmed drift", "ok": True})
    return [f"confirmed drift on {name}"]


def _act_drift(r, name, a):
    """Pause the workspace card and ask the user (merge-held, human-only) when head SHA or target branch drifts."""
    key, ref = r["card"], ref_of(r)
    kind, old_val, new_val = a["kind"], a["old_value"], a["new_value"]
    sha, target = a.get("sha") or "", a.get("target") or ""
    summary = (f"target branch changed ({old_val} → {new_val})" if kind == "target_changed"
               else f"head changed unexpectedly ({old_val[:8]} → {new_val[:8]})")
    ping_key = f"@drift:{key}:{ws_state.digest(f'{kind}:{sha}:{target}')}"
    cur = (ws_state.load(key).get("steward_drift") or {})
    old_ping = cur.get("ping_key")
    open_pings = greenlight.pinged()
    ping_ts = int(time.time())
    if old_ping and old_ping != ping_key and old_ping in open_pings:
        with contextlib.suppress(Exception):
            greenlight.resolve(old_ping, f"superseded by {summary}")
    _resolve_rereview_ping(key, sha, f"superseded by {summary}")
    if ping_key not in open_pings:
        opt = f"Confirm target {new_val}" if kind == "target_changed" else f"Confirm head {new_val[:8]}"
        url = (r.get("mr") or {}).get("web_url") or ""
        greenlight.ping(f"{name}: paused — {ref} {summary}. {url}\nOptions: {opt} | Keep waiting",
                        key=ping_key, ws=[r["id"]], kind="merge-held", ask=True)
    with ws_state.lock():
        c = ws_state.load(key)
        cur = c.get("steward_drift") or {}
        if cur.get("kind") == kind and cur.get("sha") == sha and cur.get("target") == target:
            return [f"left {name}: paused on unconfirmed {kind}"]
        c["steward_drift"] = {"kind": kind, "sha": sha, "target": target, "old_value": old_val,
                              "ping_key": ping_key, "ts": ping_ts}
        ws_state.append_activity(c, f"Paused: {summary}", kind="drift", step=r["next"].get("step", ""),
                                 sha=sha, target=target, ok=False)
        ws_state.save(c)
    log({"ws": name, "mr": ref, "did": f"paused ({kind})", "ok": False})
    greenlight.note(name, f"{ref} {summary} → paused and asked the user (orchestrator)")
    return [f"paused {name}: {summary}"]


def _resolve_rereview_ping(key, sha, why):
    prefix = f"@rereview:{key}:"
    for pk in [k for k in greenlight.pinged() if k.startswith(prefix)]:
        with contextlib.suppress(Exception):
            greenlight.resolve(pk, why)


def _rereview_live_ready(r, name, peers):
    """Run fresh live worktree/agent/MR/peer pre-launch guards before dispatching a re-review fix."""
    try:
        live = live_worktree(r)
    except Exception as e:  # noqa: BLE001
        return f"couldn't re-read workspace before re-review fix: {str(e)[:100]}"
    if live is None or branch_changed(r, live) or link_changed(r, live) or \
            (live.get("comment") or "").strip() != (r.get("comment") or "").strip():
        return "workspace branch, link, or goal changed during diff re-review"
    if active_agent_blocks(live, r, False):
        return "an agent started or is waiting during diff re-review"
    try:
        if (r.get("mr") or {}).get("iid") and current_mr_state(r) != scanned_mr_state(r):
            return "MR head or lifecycle step changed during diff re-review"
    except Exception as e:  # noqa: BLE001
        return f"couldn't re-read MR state during diff re-review: {str(e)[:100]}"
    for peer in peers:
        if peer["id"] == r["id"] or (peer.get("forge"), peer.get("project"), (peer.get("mr") or {}).get("iid")) != \
                (r.get("forge"), r.get("project"), (r.get("mr") or {}).get("iid")):
            continue
        try:
            other = live_worktree(peer)
        except Exception as e:  # noqa: BLE001 - fail closed on peer lookup failure
            return f"couldn't recheck linked workspace before re-review fix: {str(e)[:100]}"
        if other is None or any(x.get("state") in ("working", "waiting") for x in other.get("agents") or []):
            return "another workspace has an active agent for this MR"
        if any(d.get("pending") for d in ws_state.load(peer["card"]).get("dispatches") or []):
            return "another workspace has a pending launch for this MR"
    return None


def _rereview_fail_closed(r, name, sha, target, summary, reason):
    """Record a fail-closed diff re-review error/stall and ping the user without advancing steward_reviewed_sha."""
    key, ref, mr = r["card"], ref_of(r), r.get("mr") or {}
    ping_key = f"@rereview:{key}:{sha[:12]}"
    with ws_state.lock():
        c = ws_state.load(key)
        c.update(steward_head=sha, steward_pending_rereview_sha=sha)
        ws_state.append_activity(c, f"{reason} at {sha[:8]} (fail-closed)", kind="rereview",
                                 step=r["next"].get("step", ""), sha=sha, target=target, detail=summary, ok=False)
        ws_state.save(c)
    if ping_key not in greenlight.pinged():
        url = mr.get("web_url") or ""
        greenlight.ping(f"{name}: {reason.lower()} at {sha[:8]} ({summary[:160]}). {url}\nOptions: retry on next tick | leave it",
                        key=ping_key, ws=[r["id"]], kind="agent-stuck", ask=True)
    log({"ws": name, "mr": ref, "did": "rereview held", "ok": False})
    return [f"left {name}: {reason.lower()} (fail-closed): {summary[:120]}"]


def _rereview_dispatch_fix(r, name, sha, target, summary, peers):
    """Dispatch one fix agent for contract failures on sha; fail closed without advancing steward_reviewed_sha."""
    key, ref = r["card"], ref_of(r)
    fix_sig = f"rereview@{sha}"
    card = ws_state.load(key)
    if any(d.get("sig") == fix_sig for d in card.get("dispatches") or []):
        return _rereview_fail_closed(r, name, sha, target, summary, "Diff re-review still failing after fix")
    if why := _rereview_live_ready(r, name, peers):
        return [f"left {name}: {why}"]
    assignment = record_dispatch(r, fix_sig, expected=ws_state.situation_token(card))
    if assignment is False:
        return [f"left {name}: card assignment changed before re-review fix launch"]
    handoff = f"Diff re-review at {sha[:8]} found contract issue(s): {summary}. Fix these and push."
    agent, err = dispatch(r, ws_state.load(key), why_handoff=handoff, assignment=assignment, peers=peers)
    if agent:
        if record_dispatch(r, fix_sig, agent, expected=assignment) is False:
            return [f"dispatch recording failed on {name}: accepted launch belongs to an older card assignment"]
        with ws_state.lock():
            c = ws_state.load(key)
            c.update(steward_head=sha, steward_pending_rereview_sha=sha,
                     steward_expecting_from_sha=sha, steward_last_agent_state="working")
            ws_state.append_activity(c, f"Re-review failed at {sha[:8]}; dispatched {agent}", kind="rereview",
                                     step=r["next"].get("step", ""), sha=sha, target=target, detail=summary, ok=False)
            ws_state.save(c)
        _resolve_rereview_ping(key, sha, f"dispatched {agent} to fix contracts")
        log({"ws": name, "mr": ref, "did": f"dispatched {agent} (rereview)", "ok": True})
        return [f"dispatched {agent} to {name}: re-review fix ({summary[:80]})"]
    if greenlight.LIMIT_RE.search(err) or re.search(r"rejected|invalid agent|unknown agent|not configured|unsupported agent|no such agent|worktree not found|permission denied", err, re.I):
        clear_pending(r, fix_sig, assignment)
        return _rereview_fail_closed(r, name, sha, target, f"{summary}; dispatch failed: {err[:120]}", "Re-review fix dispatch failed")
    with ws_state.lock():
        c = ws_state.load(key)
        c.update(steward_head=sha, steward_pending_rereview_sha=sha)
        ws_state.append_activity(c, f"Re-review fix launch uncertain at {sha[:8]}", kind="rereview",
                                 step=r["next"].get("step", ""), sha=sha, target=target, detail=err[:160], ok=False)
        ws_state.save(c)
    log({"ws": name, "mr": ref, "did": "rereview dispatch uncertain", "ok": False})
    return [f"dispatch failed on {name}: {err[:150]}"]


def _act_rereview(r, name, a, peers=()):
    """Re-review the diff at the head SHA via audit.merge_audit after a code agent finishes; fail closed on error."""
    import audit
    key, ref, mr = r["card"], ref_of(r), r.get("mr") or {}
    sha, target = a.get("sha") or "", a.get("target") or ""
    if clear_sig := a.get("clear_interrupted"):
        with ws_state.lock():
            c = ws_state.load(key)
            c["dispatches"] = [d for d in c.get("dispatches") or []
                               if not (d.get("sig") == f"rereview@{sha}" and d.get("agent") == "launch interrupted")]
            c.get("forks", {}).pop(clear_sig, None)
            ws_state.save(c)
    if not r.get("path"):
        verdict = {"ok": False, "failed": [], "error": "no local worktree path to run diff re-review"}
    else:
        goal = audit.goal_of(r, mr) or r.get("goal") or mr.get("title") or ""
        try:
            verdict = audit.merge_audit(r["path"], sha=sha, target=target, goal=goal,
                                        project=r["project"], iid=mr["iid"], forge=r["forge"], tag=audit.ident(r, mr))
        except Exception as e:  # noqa: BLE001 - fail closed on any audit error
            verdict = {"ok": False, "failed": [], "error": f"{type(e).__name__}: {e}"}
    if verdict.get("ok") and not verdict.get("unverified"):
        detail = audit.summary(verdict)
        with ws_state.lock():
            c = ws_state.load(key)
            c.update(steward_reviewed_sha=sha, steward_head=sha, steward_pending_rereview_sha="")
            ws_state.append_activity(c, f"Re-reviewed diff at {sha[:8]}", kind="rereview",
                                     step=r["next"].get("step", ""), sha=sha, target=target, detail=detail, ok=True)
            ws_state.save(c)
        _resolve_rereview_ping(key, sha, f"re-review passed at {sha[:8]}")
        log({"ws": name, "mr": ref, "did": "re-reviewed diff", "ok": True})
        return [f"re-reviewed {name} at {sha[:8]}: ok"]
    summary = audit.summary(verdict) if not verdict.get("unverified") else "audit unverified (local fallback)"
    if verdict.get("failed"):
        return _rereview_dispatch_fix(r, name, sha, target, summary, peers)
    return _rereview_fail_closed(r, name, sha, target, summary, "Diff re-review error")


def plan(rows, cards, judge=True):
    """[{ws, do, ...}]: what this tick does, in order. judge=False: forks are reported, not sent to decide.py."""
    acts, n, records = [], 0, None
    for r in keyed(rows, migrate=False):
        nxt, card = r["next"], cards.get(r["card"]) or ws_state.load(r["card"])
        act = lambda r=r, **a: acts.append({"ws": r["name"], "id": r["id"]} | a)
        if r.get("_mr_inconclusive") or r.get("_card_error"):
            act(do="skip", why="MR lookup or card refresh is inconclusive; preserving the previous handoff")
            continue
        if card.get("steward_drift") and records is None:
            records = ws_state.decision_records()
        if guard := _steward_plan_guard(r, card, records):
            act(**guard)
            continue
        if goal := backfill_goal(r):
            act(do="goal", goal=goal)
        pending = next((d for d in card["dispatches"] if d.get("pending")), None)
        if pending and nxt["owner"] != "agent":
            if time.time() - pending.get("ts", 0) < 120:
                act(do="skip", why="launch may still be starting; waiting for the next scan")
            else:
                why = "a previous launch was interrupted before its result was recorded"
                s = sig(r)
                prior_question = card.get("forks", {}).get(s) or {}
                act(do="fork", sig=s, why=why, pending_sig=pending["sig"], verdict={"choice": FORK[-1], "provider": "rule", "confidence": None,
                    "ts": prior_question.get("ts") or int(time.time()), "why": why,
                    **({"ping_key": prior_question["ping_key"]} if prior_question.get("ping_key") else {})})
            continue
        if nxt["owner"] != "agent":
            prior_question = card.get("forks", {}).get(sig(r)) or {}
            if prior_question.get("awaiting_nonagent"):
                if records is None:
                    records = ws_state.decision_records()
                answered = next((d for d in records if d.get("key") == prior_question.get("ping_key")
                                 and d.get("answered", 0) and d["answered"] >= prior_question.get("ts", 0)), None)
                if not answered:
                    act(do="skip", why="interrupted launch needs the user's answer")
                    continue
                if answered.get("choice") != 0:
                    act(do="skip", why="the user left the interrupted launch parked")
                    continue
        if nxt["owner"] == "greenlight" and (cmd := nxt.get("cmd")):
            checked_merge = isinstance(cmd, list) and len(cmd) > 2 and cmd[:3] == [sys.executable, str(Path(greenlight.__file__).resolve().with_name("audit.py")), "checked-merge"]
            if not isinstance(cmd, list) or cmd[:1] not in (["glab"], ["gh"]) and not checked_merge:
                act(do="skip", why=f"next.cmd isn't a glab/gh call: {cmd!r:.80}")
            elif "merge" in cmd and held(r):
                act(do="skip", why="the goal says hold: not merging")
            else:
                act(do="cmd", cmd=cmd, action=nxt["action"])
        elif nxt["owner"] == "agent":
            s, why = sig(r), fork(r, card)
            pending = next((d for d in card["dispatches"] if d.get("pending")), None)
            if pending:
                if time.time() - pending.get("ts", 0) < 120:
                    act(do="skip", why="launch may still be starting; waiting for the next scan")
                    continue
                why = "a previous launch was interrupted before its result was recorded"
                prior_question = card.get("forks", {}).get(s) or {}
                act(do="fork", sig=s, why=why, pending_sig=pending["sig"], verdict={"choice": FORK[-1], "provider": "rule", "confidence": None,
                    "ts": prior_question.get("ts") or int(time.time()), "why": why,
                    **({"ping_key": prior_question["ping_key"]} if prior_question.get("ping_key") else {})})
                continue
            latest = next((d for d in card["dispatches"] if d.get("sig") == s), None)
            if latest and time.time() - latest.get("ts", 0) < 120 and not latest.get("judged") and not (
                    "out of tokens" in nxt["action"] and nxt.get("agent") and nxt["agent"] != latest.get("agent")):
                act(do="skip", why="recent dispatch still settling")
                continue
            if latest and "out of tokens" in nxt["action"] and nxt.get("agent") and nxt["agent"] not in {d.get("agent") for d in card["dispatches"] if d.get("sig") == s} and not (card["forks"].get(s) and card["forks"][s]["choice"] == FORK[-1]):
                if n >= OPTS["max_dispatch"]:
                    act(do="skip", why=f"dispatch cap ({OPTS['max_dispatch']}) reached this tick")
                else:
                    act(do="dispatch", sig=s)
                    n += 1
                continue
            if not why and card["forks"].get(s):
                why = "an earlier verdict for this situation is still in effect"
            if not why:
                if n >= OPTS["max_dispatch"]:
                    act(do="skip", why=f"dispatch cap ({OPTS['max_dispatch']}) reached this tick")
                else:
                    act(do="dispatch", sig=s)
                    n += 1
                continue
            cached = card["forks"].get(s)
            if cached and cached.get("ping_key") and records is None:
                records = ws_state.decision_records()
            answered = next((d for d in records or [] if cached and d.get("key") == cached.get("ping_key")
                             and d.get("answered", 0) and d["answered"] >= cached.get("ts", 0)), None)
            if cached and cached["choice"] == FORK[-1] and answered and answered.get("choice") == 0:
                cached = {**cached, "choice": FORK[0], "provider": "user", "done": False, "why": "the user chose dispatch again"}
            latest = next((d for d in card["dispatches"] if d.get("sig") == s), None)
            if cached and cached.get("done") and cached["choice"] != FORK[-1] and latest and latest.get("judged"):
                why = f"greenlight's judged '{cached['choice']}' didn't move it either"  # never a silent stall
                cached = {"choice": FORK[-1], "picked": None, "confidence": None, "provider": "rule", "why": why, "ts": int(time.time())}
            if not judge and not cached:
                act(do="fork", sig=s, why=why)
                continue
            if not cached:
                state = {"workspace": r["name"], "step": nxt["step"], "owner": nxt["owner"], "action": nxt["action"],
                         "agent": r.get("agent"), "card": {k: card[k] for k in ws_state.FIELDS}}
                ans = decide_choice(state, f"Greenlight's next step for workspace {r['name']} is: {nxt['action']}. "
                                           f"It can't be done mechanically because {why}. What should greenlight do?", FORK,
                                    context=ws_state.brief(card))
                low = ans.get("confidence", 0) < OPTS["min_confidence"] or ans.get("provider") == "local"
                cached = {"choice": FORK[-1] if low else ans["choice"], "picked": ans["choice"], "confidence": ans.get("confidence"),
                          "provider": ans.get("provider"), "why": why, "ts": int(time.time())}
                if not set_fork(r["card"], s, cached, ws_state.fork_token(card)):
                    act(do="skip", why="card changed while judging; retry from the current situation")
                    continue
            act(do="fork", sig=s, why=why, verdict=cached)
            if cached["choice"] != FORK[-1] and not cached.get("done"):
                n += 1
    return acts


def failed_dispatches(r, card):
    """The last dispatch ended (no agent working now) and the step is the same: a failed approach, recorded once."""
    d = card["dispatches"][0] if card["dispatches"] else None
    if not d or d.get("pending") or d.get("judged") or (r.get("agent") or {}).get("state") == "working" or r["next"]["owner"] != "agent":
        return None
    if d.get("step") != r["next"]["step"] or time.time() - d.get("ts", 0) < 120:
        return None
    return d


def record_dispatch(r, s, agent=None, verdict=None, expected=None):
    with ws_state.lock():
        c = ws_state.load(r["card"])
        assignment = ws_state.situation_token(c)
        if expected is not None and assignment != expected:
            return False
        pending = next((d for d in c["dispatches"] if d.get("sig") == s and d.get("pending") and d.get("assignment") == (expected or assignment)), None)
        if agent is None:
            c["dispatches"] = [{"ts": int(time.time()), "agent": "launch pending", "step": r["next"]["step"], "sig": s, "assignment": assignment, "pending": True}] + c["dispatches"]
        elif pending:
            if expected and assignment != expected:
                return False
            pending.update(agent=agent, pending=False)
            if c.get("steward"):
                c["steward_expecting_from_sha"] = (r.get("mr") or {}).get("sha") or c.get("sha") or ""
                c["steward_last_agent_state"] = "working"
            if verdict:
                c["forks"][s] = verdict | {"done": True}
        else:
            return False  # assignment changed while Orca was accepting the launch
        ws_state.save(c)
        return assignment


def clear_pending(r, s, assignment=None):
    with ws_state.lock():
        c = ws_state.load(r["card"])
        c["dispatches"] = [d for d in c["dispatches"] if not (d.get("sig") == s and d.get("pending") and
                           (assignment is None or d.get("assignment") == assignment))]
        ws_state.save(c)


def mark_interrupted(r, s, assignment=None):
    with ws_state.lock():
        c = ws_state.load(r["card"])
        for d in c["dispatches"]:
            if d.get("sig") == s and d.get("pending") and (assignment is None or d.get("assignment") == assignment):
                d.update(agent="launch interrupted", pending=False, judged=True)
                break
        ws_state.save(c)


def set_fork(name, s, v, expected=None, decision=None):
    with ws_state.lock():
        c = ws_state.load(name)
        if expected is not None and ws_state.fork_token(c) != expected:
            return False
        c["forks"][s] = v
        if decision:
            c["decisions_made"] = ws_state.push(c["decisions_made"], decision)
            c["noted_decisions"] = ws_state.push(c.get("noted_decisions", []), decision)
        ws_state.save(c)
        return True


MERGE = (["pr", "merge"], ["mr", "merge"])


def live_worktree(r, snapshot=None):
    """Use one tick-wide agent snapshot and a targeted fresh metadata read for every action."""
    ps = snapshot if snapshot is not None else greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
    row = ps.get(r["id"]) if isinstance(ps, dict) else next((w for w in ps if w.get("worktreeId") == r["id"]), None)
    if row is None:
        return None
    try:
        shown = greenlight.orca("worktree", "show", "--worktree", f"id:{r['id']}")
        if "worktree" in shown and not shown["worktree"]:
            return None  # the target disappeared after the tick snapshot
        detail = shown.get("worktree") or {}
    except Exception:
        fresh = greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
        row = next((w for w in fresh if w.get("worktreeId") == r["id"]), None)
        if row is None:
            return None
        detail = {}  # the native backend supplies the live row through ps
    return {**row, **detail, "worktreeId": r["id"], "agents": detail.get("agents", row.get("agents", []))}


def branch_changed(r, live):
    return "branch" in live and (live.get("branch") or "").removeprefix("refs/heads/") != r.get("branch")


def link_changed(r, live):
    field = "linkedPR" if r.get("forge") == "github" else "linkedGitLabMR"
    return greenlight.linked_number(live.get(field)) != r.get("linked_mr")


def guard_merge(r, cmd, snapshot=None, peers=()):
    """Re-read the goal before any forge action; pin merges to the scanned head commit."""
    checked_merge = len(cmd) > 2 and cmd[:3] == [sys.executable, str(Path(greenlight.__file__).resolve().with_name("audit.py")), "checked-merge"]
    merging = checked_merge or cmd[1:3] in MERGE
    readying = "ready" in cmd or "--ready" in cmd
    sha = (r.get("mr") or {}).get("sha")
    if merging and not sha:
        return None, "no scanned head commit to pin the merge to"
    if merging and r.get("card"):
        card = ws_state.load(r["card"])
        target = (r.get("mr") or {}).get("target_branch") or ""
        sha_s = str(sha or "")
        if card.get("steward_drift"):
            return None, f"merge paused on unconfirmed {card['steward_drift'].get('kind', 'drift')}"
        if card.get("steward"):
            if ws_state.is_stale_confirmed(card, sha_s, target):
                return None, "scan is stale after drift confirmation; rescan before merging"
            if card.get("steward_target") and target and card["steward_target"] != target:
                _act_drift(r, r["name"], {"kind": "target_changed", "old_value": card["steward_target"],
                                          "new_value": target, "sha": sha_s, "target": target})
                return None, f"target branch changed ({card['steward_target']} → {target}); paused and asked user"
            if card.get("steward_head") and sha_s != card["steward_head"] and not card.get("steward_expecting_from_sha"):
                _act_drift(r, r["name"], {"kind": "head_changed", "old_value": card["steward_head"],
                                          "new_value": sha_s, "sha": sha_s, "target": target})
                return None, f"head changed unexpectedly ({card['steward_head'][:8]} → {sha_s[:8]}); paused and asked user"
    try:
        live = live_worktree(r, snapshot)
    except Exception as e:  # noqa: BLE001
        return None, f"couldn't re-read the goal before merging: {str(e)[:100]}"
    if live is None:
        return None, "workspace gone since the scan"
    if branch_changed(r, live) or link_changed(r, live):
        return None, "workspace branch or linked request changed since the scan"
    if merging and held(r, live.get("comment") or ""):
        return None, "the goal says hold now"
    if (live.get("comment") or "").strip() != (r.get("comment") or "").strip():
        return None, "the goal changed since the scan"
    if merging or readying:
        matching = [r] + [p for p in peers if p["id"] != r["id"] and
                    (p.get("forge"), p.get("project"), (p.get("mr") or {}).get("iid")) ==
                    (r.get("forge"), r.get("project"), (r.get("mr") or {}).get("iid"))]
        for peer in matching:
            try:
                fresh = live_worktree(peer, snapshot)
            except Exception as e:
                return None, f"couldn't re-read merge goal: {str(e)[:100]}"
            if fresh is None or branch_changed(peer, fresh) or link_changed(peer, fresh):
                return None, "a linked workspace changed since the scan"
            if merge_agent_blocks(fresh, peer):
                return None, "an agent is still working on a linked workspace"
            if (fresh.get("comment") or "").strip() != (peer.get("comment") or "").strip():
                return None, "a linked workspace goal changed since the scan"
            if merging and (held(peer) or held(peer, fresh.get("comment") or "")):
                return None, "a linked workspace says hold now"
            if readying and (stay_draft(peer) or stay_draft(peer, fresh.get("comment") or "")):
                return None, "a linked workspace says keep draft"
        try:
            fresh_rows = greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
        except Exception as e:
            return None, f"couldn't recheck linked workspaces: {str(e)[:100]}"
        known = {p["id"] for p in matching}
        repo_id = live.get("repoId")
        link_field = "linkedPR" if r.get("forge") == "github" else "linkedGitLabMR"
        by_id = {w.get("worktreeId"): w for w in fresh_rows}
        projects_by_repo = None
        for peer in matching:
            latest = by_id.get(peer["id"])
            if latest is None or branch_changed(peer, latest) or link_changed(peer, latest) or \
                    (latest.get("comment") or "").strip() != (peer.get("comment") or "").strip():
                return None, "a linked workspace goal changed during final check"
            if merge_agent_blocks(latest, peer):
                return None, "an agent started on a linked workspace"
        for w in fresh_rows:
            same_link = str(greenlight.linked_number(w.get(link_field)) or "") == str((r.get("mr") or {}).get("iid"))
            same_branch = (w.get("branch") or "").removeprefix("refs/heads/") == (r.get("mr") or {}).get("source_branch")
            if w.get("worktreeId") in known or not (same_link or same_branch):
                continue
            if repo_id and w.get("repoId") and w["repoId"] != repo_id:
                if projects_by_repo is None:
                    try:
                        full = greenlight.orca("worktree", "list", "--limit", "10000")["worktrees"]
                        projects_by_repo = {x["repoId"]: greenlight.forge_project(x.get("projectId")) for x in full if x.get("projectId")}
                    except Exception as e:
                        return None, f"couldn't identify a newly linked repository: {str(e)[:100]}"
                project = greenlight.forge_project(w.get("projectId")) or projects_by_repo.get(w["repoId"])
                if project != (r.get("forge"), r.get("project")):
                    if project is None:
                        return None, "couldn't identify a newly linked repository"
                    continue
            if any(a.get("state") in ("working", "waiting") for a in w.get("agents") or []):
                return None, "an agent is still working on a newly linked workspace"
            goal = w.get("comment") or ""
            if merging and held(r, goal):
                return None, "a newly linked workspace says hold"
            if readying and stay_draft(r, goal):
                return None, "a newly linked workspace says keep draft"
    if readying and stay_draft(r):
        return None, "the goal says keep draft"
    if (r.get("mr") or {}).get("iid"):
        try:
            current = current_mr_state(r)
        except Exception as e:
            return None, f"couldn't re-read MR lifecycle: {str(e)[:100]}"
        if current != scanned_mr_state(r):
            return None, "MR head or lifecycle step changed since scan"
    try:
        latest_goal = live_worktree(r)
    except Exception as e:
        return None, f"couldn't re-read goal before forge action: {str(e)[:100]}"
    if latest_goal is None or branch_changed(r, latest_goal) or link_changed(r, latest_goal) or \
            (latest_goal.get("comment") or "").strip() != (r.get("comment") or "").strip():
        return None, "goal changed since the scan"
    if merging or readying:
        try:
            final_rows = greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
        except Exception as e:
            return None, f"couldn't recheck linked workspaces after forge read: {str(e)[:100]}"
        final_by_id = {w.get("worktreeId"): w for w in final_rows}
        for peer in matching:
            fresh = final_by_id.get(peer["id"])
            if fresh is None or branch_changed(peer, fresh) or link_changed(peer, fresh) or \
                    (fresh.get("comment") or "").strip() != (peer.get("comment") or "").strip():
                return None, "a linked workspace goal changed during final check"
            if merge_agent_blocks(fresh, peer):
                return None, "an agent started on a linked workspace"
            if merging and held(peer, fresh.get("comment") or ""):
                return None, "a linked workspace says hold now"
            if readying and stay_draft(peer, fresh.get("comment") or ""):
                return None, "a linked workspace says keep draft"
        for fresh in final_rows:
            if fresh.get("worktreeId") in final_by_id and fresh.get("worktreeId") in {p["id"] for p in matching}:
                continue
            same_link = str(greenlight.linked_number(fresh.get(link_field)) or "") == str((r.get("mr") or {}).get("iid"))
            same_branch = (fresh.get("branch") or "").removeprefix("refs/heads/") == (r.get("mr") or {}).get("source_branch")
            if not (same_link or same_branch):
                continue
            if repo_id and fresh.get("repoId") and fresh["repoId"] != repo_id:
                if projects_by_repo is None:
                    return None, "couldn't identify a newly linked repository after forge read"
                project = greenlight.forge_project(fresh.get("projectId")) or projects_by_repo.get(fresh["repoId"])
                if project != (r.get("forge"), r.get("project")):
                    if project is None:
                        return None, "couldn't identify a newly linked repository after forge read"
                    continue
            if any(a.get("state") in ("working", "waiting") for a in fresh.get("agents") or []):
                return None, "an agent started on a newly linked workspace"
            if merging and held(r, fresh.get("comment") or ""):
                return None, "a newly linked workspace says hold"
            if readying and stay_draft(r, fresh.get("comment") or ""):
                return None, "a newly linked workspace says keep draft"
    return cmd + ((["--match-head-commit", sha] if cmd[0] == "gh" else ["--sha", sha]) if merging and not checked_merge else []), None


def run_cmd(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           pass_fds=(TICK_LOCK_FD,) if TICK_LOCK_FD is not None else ())
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}: {e}"[-300:]
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


def process_start(pid):
    """Use the scheduler's birth identity format when writing its phase marker."""
    from auto import process_start as scheduler_process_start
    return scheduler_process_start(pid)


def script(name, *args, timeout=900):
    t = time.monotonic()
    rc, out = _script(name, *args, timeout=timeout)
    diag.event("script", name=name.removesuffix(".py"), sub=args[0] if args else None, rc=rc, ok=rc == 0,
               secs=time.monotonic() - t, msg_class=diag.classify(out[-500:]) if rc else None)
    return rc, out


def _script(name, *args, timeout=900):
    marker = greenlight.STATE / f"orchestrator-phase-{os.getpid()}.pid"
    try:
        # The child waits for release until its process group is recorded for auto.py's timeout cleanup.
        read_fd, write_fd = os.pipe()
        try:
            p = subprocess.Popen([sys.executable, "-c", "import os,sys; sys.exit(1) if os.read(int(sys.argv[1]),1) != b'1' else None; os.execv(sys.executable,[sys.executable,*sys.argv[2:]])",
                                  str(read_fd), str(HERE / name), *args],
                                 pass_fds=(read_fd,) + ((TICK_LOCK_FD,) if TICK_LOCK_FD is not None else ()), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
            os.close(read_fd)
            marker.write_text(json.dumps({"phase": p.pid, "phase_start": process_start(p.pid),
                                          "root_start": process_start(os.getpid()),
                                          "nonce": os.environ.get("GREENLIGHT_TICK_NONCE")}))
            os.write(write_fd, b"1")
        finally:
            with contextlib.suppress(OSError):
                os.close(read_fd)
            os.close(write_fd)
        try:
            out, _ = p.communicate(timeout=timeout)
            return p.returncode, out.strip()
        except subprocess.TimeoutExpired as e:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            return 124, f"TimeoutExpired: {e}"
    except OSError as e:
        return 124, f"{type(e).__name__}: {e}"
    finally:
        marker.unlink(missing_ok=True)


def phase_cards(rows):
    recs = ws_state.decision_records()
    by_id, by_name = {}, {}
    for rec in recs:
        targets = [rec] + (rec.get("workspaces") or [])
        ids = {t.get("worktree_id") for t in targets if t.get("worktree_id")}
        for ident in ids:
            by_id.setdefault(ident, []).append(rec)
        if not ids:
            for name in {t.get("workspace") for t in targets if t.get("workspace")}:
                by_name.setdefault(name, []).append(rec)
    cards, moved = {}, []
    for r in keyed(rows):
        try:
            candidates = by_id.get(r["id"], []) + by_name.get(r["name"], [])
            c, t = ws_state.observe(r, contract(r), candidates)
        except Exception as e:  # noqa: BLE001 - a card is a help, never a blocker
            moved.append(f"{r['name']}: card failed: {e}")
            r["_card_error"] = str(e)[:120]
            continue
        if (d := failed_dispatches(r, c)):
            with ws_state.lock():
                c = ws_state.load(r["card"])
                latest = failed_dispatches(r, c)
                if latest and (latest.get("sig"), latest.get("ts")) == (d.get("sig"), d.get("ts")):
                    c["failed_approaches"] = ws_state.push(c["failed_approaches"], f"{d['agent']} dispatch for {d['step']} "
                                                           f"({time.strftime('%m-%d %H:%M', time.localtime(d['ts']))}) ended with the step unchanged: {r['next']['action'][:160]}")
                    latest["judged"] = True
                    ws_state.save(c)
        if t:
            moved.append(f"{r['name']}: {t[0] or '(new)'} → {t[1]}")
        if r.get("_state_reset") or c.get("stale_pings"):
            open_pings = greenlight.pinged()
            mr = r.get("mr") or {}
            cur_sha, cur_target = mr.get("sha") or "", mr.get("target_branch") or ""
            target_drifted = bool(c.get("steward_target") and cur_target and cur_target != c["steward_target"])
            active_rereview = (
                f"@rereview:{r['card']}:{cur_sha[:12]}"
                if c.get("steward") and not c.get("steward_drift") and not target_drifted and not r.get("_goal_changed")
                and cur_sha and c.get("steward_pending_rereview_sha") == cur_sha
                else ""
            )
            keys = set(c.get("stale_pings") or []) | (
                {k for k in open_pings if k.startswith(stuck_key(r, "")) or
                 (k.startswith(f"@rereview:{r['card']}:") and k != active_rereview)}
                if r.get("_state_reset") else set()
            )
            retry = []
            for k in keys:
                if k in open_pings:
                    try:
                        greenlight.resolve(k, f"situation changed: {r['next']['step']}")
                    except Exception:
                        retry.append(k)
            with ws_state.lock():
                fresh = ws_state.load(r["card"])
                remaining = [k for k in fresh.get("stale_pings") or [] if k not in keys or k in retry]
                remaining = ws_state.merge(remaining, retry)
                if fresh.get("stale_pings") != remaining:
                    fresh["stale_pings"] = remaining
                    ws_state.save(fresh)
                c = fresh
        cards[r["card"]] = c
    return cards, moved


def stuck_key(r, s):
    """Each new stuck question gets a unique key; a resolved one cannot suppress a later question."""
    return f"@stuck:{r['card']}:" + (uuid.uuid4().hex[:12] if s else "")


def phase_me(rows, cards):
    """Notify once for each new user-owned situation during an unattended tick."""
    sent = []
    open_keys = greenlight.pinged()
    try:
        live_snapshot = {w["worktreeId"]: w for w in greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]}
    except Exception:
        return sent  # no live evidence: do not resolve or publish a question from an old scan
    all_decisions = None
    for r in rows:
        if r["next"]["owner"] != "me":
            (greenlight.STATE / "me-pings" / ws_state.safe(r["id"])).unlink(missing_ok=True)
            (greenlight.STATE / "me-pings" / (ws_state.safe(r["id"]) + ".generated")).unlink(missing_ok=True)
            continue
        if r.get("_card_error") or r.get("_mr_inconclusive"):
            continue
        card = cards.get(r.get("card")) or {}
        situation = f"{r['id']}:{r['project']}:{(r.get('mr') or {}).get('iid')}:{(r.get('mr') or {}).get('sha')}:{(r.get('mr') or {}).get('target_branch')}:{r['next']['step']}:{r['next']['action']}:{card.get('handoff_epoch', '')}"
        marker = f"me-ping:{ws_state.digest(situation)}"
        marker_file = greenlight.STATE / "me-pings" / ws_state.safe(r["id"])
        generated_file = marker_file.with_name(marker_file.name + ".generated")
        already_sent = marker_file.exists() and marker_file.read_text().splitlines()[-1:] == [marker]
        if already_sent:
            continue
        try:
            live = live_worktree(r, live_snapshot)
            if live is None or branch_changed(r, live) or link_changed(r, live) or \
                    (live.get("comment") or "").strip() != (r.get("comment") or "").strip() or \
                    (r.get("mr") or {}).get("iid") and current_mr_state(r) != scanned_mr_state(r):
                continue
        except Exception:
            continue  # a transient forge/ADE error cannot authorize an old approval question
        question_text = f"{r['name']}: {r['next']['action']} {(r.get('mr') or {}).get('web_url') or ''}"
        if r["id"] in open_keys:
            if all_decisions is None:
                all_decisions = greenlight.decisions.load()
            current_question = next((q for q in reversed(all_decisions) if q.get("key") == r["id"] and
                                     greenlight.decisions.is_open(q)), None)
            if current_question and current_question.get("worktree_id") == r["id"] and \
                    current_question.get("mr") == (r.get("mr") or {}).get("ref") and \
                    current_question.get("step") == r["next"]["step"] and current_question.get("goal") == r.get("goal") and \
                    current_question.get("sha") == (r.get("mr") or {}).get("sha") and \
                    current_question.get("target_branch") == (r.get("mr") or {}).get("target_branch"):
                old_generated = generated_file.read_text() if generated_file.exists() else None
                if current_question.get("question") == question_text.strip() or \
                        current_question.get("question") != old_generated:
                    marker_file.parent.mkdir(parents=True, exist_ok=True)
                    with marker_file.open("a") as fh:
                        fh.write(marker + "\n")
                    continue  # a more specific manual question already asks about this situation
            greenlight.resolve(r["id"], f"superseded by {r['next']['step']}/me")
            all_decisions = None
        step = r["next"]["step"]
        kind = "merge-held" if step == "merge" else "keep-draft" if step == "ready" else step if step in ("goal", "milestone", "signoff") else "review-verdict"
        options = {"merge": "merge now | keep waiting", "ready": "mark ready | keep draft",
                   "goal": "set a goal | leave it", "milestone": "use latest release | choose another",
                   "signoff": "approve | leave it"}.get(step, "proceed | leave it")
        text = question_text + f"\nOptions: {options}"
        sent_ping = greenlight.ping(text, r["id"], [r["id"]], kind)
        if sent_ping is None:
            continue
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        with marker_file.open("a") as fh:
            fh.write(marker + "\n")
        generated_file.write_text(question_text.strip())
        greenlight.note(r["name"], f"{marker} {step}/me pinged by orchestrator")
        sent.append(r["name"])
    return sent


def act(rows, cards, acts):
    by = {r["id"]: r for r in keyed(rows)}
    done, launches, forge_actions, claimed = [], 0, 0, set()
    try:
        live_snapshot = {w["worktreeId"]: w for w in greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]}
    except Exception:
        live_snapshot = None  # each action will retry its own live read and report failures
    for a in acts:
        r, name = by[a["id"]], a["ws"]
        if a["do"] == "cmd":
            if forge_actions >= OPTS["max_forge_actions"]:
                done.append(f"left {name}: forge action cap ({OPTS['max_forge_actions']}) reached this tick")
                continue
        if a["do"] == "skip" and str(a.get("why", "")).startswith("dispatch cap") and launches < OPTS["max_dispatch"]:
            a = plan([r], cards)[-1]  # an earlier launch failed; give this row the unused slot
        launch_action = a["do"] in ("dispatch", "rereview") or a["do"] == "fork" and not (a.get("verdict") or {}).get("done") and \
            (a.get("verdict") or {}).get("choice") != FORK[-1]
        request = (r.get("forge"), r.get("project"), (r.get("mr") or {}).get("iid")) if (r.get("mr") or {}).get("iid") else None
        if launch_action and request in claimed:
            done.append(f"left {name}: another workspace is acting on this MR this tick")
            continue
        if launch_action and launches >= OPTS["max_dispatch"]:
            done.append(f"left {name}: dispatch cap ({OPTS['max_dispatch']}) reached this tick")
            continue
        if launch_action and request:
            claimed.add(request)
        try:
            result = act_one(r, name, a, cards, live_snapshot, rows)
            done += result
            if a["do"] == "cmd" and any(x.startswith(("ran ", "FAILED ")) for x in result):
                forge_actions += 1
            if a["do"] == "goal" and any(x.startswith("backfilled ") for x in result):
                cards[r["card"]] = ws_state.load(r["card"])
            launches += sum(x.startswith("dispatched ") for x in result)
            if launch_action and request and not any(x.startswith("dispatched ") or "dispatch recording failed" in x for x in result):
                try:
                    pending = any(d.get("pending") for d in ws_state.load(r["card"])["dispatches"])
                except Exception:
                    pending = True
                if not pending:
                    claimed.discard(request)
        except Exception as e:  # noqa: BLE001 - one workspace's failure never stops the tick
            if a["do"] == "cmd":
                forge_actions += 1  # outcome unknown: do not exceed the action cap
            done.append(f"{a['do']} failed on {name}: {type(e).__name__}: {str(e)[:150]}")
    return done


def act_one(r, name, a, cards, snapshot=None, peers=()):
    done, key = [], r["card"]
    ref = ref_of(r)
    if a["do"] == "confirm_drift":
        return _act_confirm_drift(r, name)
    if a["do"] == "drift":
        return _act_drift(r, name, a)
    if a["do"] == "rereview":
        return _act_rereview(r, name, a, peers)
    if a["do"] == "goal":
        try:
            live = live_worktree(r, snapshot)
        except Exception as e:
            return [f"goal backfill failed on {name}: couldn't re-read comment: {str(e)[:100]}"]
        if live is None or branch_changed(r, live) or link_changed(r, live) or (live.get("comment") or "").strip() != (r.get("comment") or "").strip():
            return [f"left {name}: goal changed since scan; backfill skipped"]
        try:
            old_comment = r.get("comment") or ""
            store_backfill(r, a["goal"])
            greenlight.decisions.backfilled_goal(r["id"], (r.get("mr") or {}).get("ref"), a["goal"], r.get("project"), r.get("forge"))
            r["goal"] = a["goal"]  # a later action sees this generated goal; the user's comment remains untouched
            r["_goal_backfill"] = True
            try:
                ws_state.observe(r, contract(r))  # its first dispatch gets the generated goal without erasing a verdict
            finally:
                r.pop("_goal_backfill", None)
            log({"ws": name, "mr": ref, "did": "backfilled goal", "old_comment": old_comment})
            greenlight.note(name, f"{ref} goal backfilled in Greenlight → {a['goal']} (orchestrator)")
            done.append(f"backfilled {name}: {a['goal'][:80]}")
        except Exception as e:  # noqa: BLE001
            done.append(f"goal backfill failed on {name}: {str(e)[:150]}")
    elif a["do"] == "cmd":
        cmd, why = guard_merge(r, a["cmd"], snapshot, peers)
        if why:
            greenlight.note(name, f"{ref} {a['action']} → not run: {why} (orchestrator)")
            done.append(f"left {name}: {why}")
            return done
        ok, out = run_cmd(cmd)
        log({"ws": name, "mr": ref, "did": f"ran {' '.join(a['cmd'][:4])}", "ok": ok})
        ws_state.record_activity(key, f"{'Ran' if ok else 'Failed'} {a['action']}", kind="cmd",
                                 step=r["next"].get("step", ""), sha=(r.get("mr") or {}).get("sha") or "",
                                 target=(r.get("mr") or {}).get("target_branch") or "",
                                 detail="" if ok else out[-160:], ok=ok)
        greenlight.note(name, f"{ref} {a['action']} → ran `{' '.join(a['cmd'])}` ({'ok' if ok else 'failed: ' + out[-120:]}) (orchestrator)")
        done.append(f"{'ran' if ok else 'FAILED'} {name}: {a['action']}" + ("" if ok else f" ({out[-120:]})"))
    elif a["do"] == "dispatch" or a["do"] == "fork" and a.get("verdict"):
        v = a.get("verdict")
        rate_limit_handoff = bool(r["next"].get("agent") and "out of tokens" in r["next"]["action"])
        if v and v.get("done"):
            return done  # judged on an earlier tick and acted on then
        try:
            live = live_worktree(r, snapshot) if v and v.get("choice") == FORK[-1] else live_worktree(r)
        except Exception as e:
            return [f"dispatch failed on {name}: couldn't re-read active agents: {str(e)[:100]}"]
        if live is None:
            return [f"dispatch failed on {name}: workspace gone since scan"]
        if branch_changed(r, live) or link_changed(r, live):
            return [f"left {name}: workspace branch or linked request changed since scan"]
        if (live.get("comment") or "").strip() != (r.get("comment") or "").strip():
            return [f"left {name}: goal changed since scan"]
        if active_agent_blocks(live, r, rate_limit_handoff):
            return [f"left {name}: an agent started or is waiting since scan"]
        current = None
        if (r.get("mr") or {}).get("iid"):
            try:
                current = current_mr_state(r)
            except Exception as e:
                return [f"left {name}: couldn't re-read MR lifecycle: {str(e)[:100]}"]
            if current != scanned_mr_state(r):
                return [f"left {name}: MR head or lifecycle step changed since scan"]
        if v:
            expected = ws_state.fork_token(cards[key]) if key in cards else None
            if not set_fork(key, a["sig"], v, expected,
                            f"greenlight: judged ({v['provider']}, {v.get('confidence')}) → {v['choice']}: {a['why']}"):
                return [f"left {name}: card changed while judging; retry from current situation"]
            if v["choice"] == FORK[-1]:
                url = (r.get("mr") or {}).get("web_url") or ""
                card = ws_state.load(key)
                ping_key = v.get("ping_key") or stuck_key(r, a["sig"])
                if not set_fork(key, a["sig"], v | {"ping_key": ping_key}, expected):
                    return [f"left {name}: card changed before asking; retry from current situation"]
                nonagent = r["next"]["owner"] != "agent"
                sent_ping = greenlight.ping(f"{name}: greenlight won't continue on its own: {a['why']}. Next step: {r['next']['action'][:200]} {url}"
                                            + ("\nOptions: continue | leave it" if nonagent else "\nOptions: dispatch again | leave it"),
                                            key=ping_key, ws=[r["id"]], kind="agent-stuck")
                if sent_ping is None and ping_key not in greenlight.pinged():
                    return [f"left {name}: stuck question was not published; pending launch remains unresolved"]
                if any(d.get("sig") == (a.get("pending_sig") or a["sig"]) and d.get("pending") for d in card["dispatches"]):
                    mark_interrupted(r, a.get("pending_sig") or a["sig"])
                greenlight.note(name, f"{ref} stuck: {a['why']} → asked the user (orchestrator)")
                done.append(f"asked about {name}: {a['why']}")
                set_fork(key, a["sig"], v | {"done": True, "ping_key": ping_key, "awaiting_nonagent": nonagent}, expected)
                return done
        card = ws_state.load(key)  # the judge or another agent may have written a decision since phase_cards
        first = None
        handoff = ""
        if v and v["choice"] == FORK[1]:
            order = greenlight.CONFIG.get("agents") or []
            prev = (card["dispatches"][0]["agent"] if card["dispatches"] else (r.get("agent") or {}).get("type")) or (order[0] if order else None)
            first = (order[(order.index(prev) + 1) % len(order)] if prev in order else order[0]) if order else None
            handoff = f"You take over from {prev}, whose attempt didn't move the step. Its card below says what was tried."
        elif v:
            handoff = f"An earlier attempt didn't move the step ({a['why']}): read the failed approaches below and try something else."
        elif "out of tokens" in r["next"]["action"]:  # agent_overlay saw the last agent hit its limit mid-task
            prev = (r.get("agent") or {}).get("type") or "the last agent"
            handoff = f"You take over from {prev}, which ran out of tokens mid-task. The card below has its last message as the hypothesis."
        if (r.get("mr") or {}).get("sha"):
            if current is None:
                try:
                    current = current_mr_state(r)
                except Exception as e:
                    return [f"dispatch failed on {name}: couldn't re-read MR state: {str(e)[:100]}"]
            if current != scanned_mr_state(r):
                return [f"left {name}: MR head or lifecycle step changed since scan; dispatch needs a fresh plan"]
        elif not (r.get("mr") or {}).get("iid"):
            try:
                if branch_has_open_mr(r):
                    return [f"left {name}: a new MR opened since scan; dispatch needs a fresh plan"]
            except Exception as e:
                return [f"dispatch failed on {name}: couldn't recheck branch MR: {str(e)[:100]}"]
        if (r.get("mr") or {}).get("iid"):
            supersede_peers = []
            for peer in peers:
                if peer["id"] == r["id"] or (peer.get("forge"), peer.get("project"), (peer.get("mr") or {}).get("iid")) != \
                        (r.get("forge"), r.get("project"), r["mr"]["iid"]):
                    continue
                try:
                    other = live_worktree(peer)
                except Exception as e:
                    return [f"dispatch failed on {name}: couldn't recheck another workspace: {str(e)[:100]}"]
                if other is None or any(x.get("state") in ("working", "waiting") for x in other.get("agents") or []):
                    return [f"left {name}: another workspace has an active agent for this MR"]
                peer_card = ws_state.load(peer["card"])
                if any(d.get("pending") for d in peer_card["dispatches"]):
                    return [f"left {name}: another workspace has a pending launch for this MR"]
                verdict = peer_card.get("forks", {}).get(a["sig"]) or {}
                if verdict.get("choice") == FORK[-1] and verdict.get("done"):
                    if v and v.get("provider") == "user" and v.get("choice") != FORK[-1]:
                        supersede_peers.append(peer["card"])
                        continue
                    return [f"left {name}: another workspace has a user decision about this MR"]
            try:
                current_worktrees = greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]
            except Exception as e:
                return [f"dispatch failed on {name}: couldn't recheck linked workspaces: {str(e)[:100]}"]
            link_field = "linkedPR" if r.get("forge") == "github" else "linkedGitLabMR"
            projects_by_repo = None
            for w in current_worktrees:
                same_link = str(greenlight.linked_number(w.get(link_field)) or "") == str(r["mr"]["iid"])
                same_branch = (w.get("branch") or "").removeprefix("refs/heads/") == r["mr"].get("source_branch")
                if w.get("worktreeId") == r["id"] or not (same_link or same_branch):
                    continue
                if live.get("repoId") and w.get("repoId") and live["repoId"] != w["repoId"]:
                    if projects_by_repo is None:
                        try:
                            full = greenlight.orca("worktree", "list", "--limit", "10000")["worktrees"]
                            projects_by_repo = {x["repoId"]: greenlight.forge_project(x.get("projectId")) for x in full if x.get("projectId")}
                        except Exception as e:
                            return [f"dispatch failed on {name}: couldn't identify linked repository: {str(e)[:100]}"]
                    project = greenlight.forge_project(w.get("projectId")) or projects_by_repo.get(w["repoId"])
                    if project != (r.get("forge"), r.get("project")):
                        if project is None:
                            return [f"left {name}: couldn't identify linked repository"]
                        continue
                if any(x.get("state") in ("working", "waiting") for x in w.get("agents") or []):
                    return [f"left {name}: a newly linked workspace has an active agent for this MR"]
        try:
            latest = live_worktree(r)
        except Exception as e:
            return [f"dispatch failed on {name}: couldn't re-read goal before launch: {str(e)[:100]}"]
        if latest is None or branch_changed(r, latest) or link_changed(r, latest) or \
                (latest.get("comment") or "").strip() != (r.get("comment") or "").strip():
            return [f"left {name}: workspace goal or link changed before launch"]
        if active_agent_blocks(latest, r, rate_limit_handoff):
            return [f"left {name}: an agent started before launch"]
        assignment = record_dispatch(r, a["sig"], expected=ws_state.situation_token(card))  # durable intent before launch
        if assignment is False:
            return [f"left {name}: card assignment changed before launch"]
        exhausted = {(r.get("agent") or {}).get("type")} if rate_limit_handoff else set()
        agent, err = dispatch(r, card, first, handoff, assignment, exhausted, peers)
        if agent:
            done.append(f"dispatched {agent} to {name}: {r['next']['action'][:80]}")
            try:
                if record_dispatch(r, a["sig"], agent, v, expected=assignment) is False:
                    done.append(f"dispatch recording failed on {name}: accepted launch belongs to an older card assignment")
                    return done
                for peer_key in supersede_peers if (r.get("mr") or {}).get("iid") else []:
                    with ws_state.lock():
                        current_peer = ws_state.load(peer_key)
                        latest_verdict = current_peer.get("forks", {}).get(a["sig"]) or {}
                        if latest_verdict.get("choice") == FORK[-1] and latest_verdict.get("done"):
                            current_peer["forks"].pop(a["sig"], None)
                            ws_state.save(current_peer)
                            if latest_verdict.get("ping_key"):
                                greenlight.resolve(latest_verdict["ping_key"], "superseded by retry answer on linked workspace")
                log({"ws": name, "mr": ref, "did": f"dispatched {agent}", "step": r["next"]["step"]})
                ws_state.record_activity(key, f"Dispatched {agent} for {r['next']['step']}", kind="dispatch",
                                         step=r["next"]["step"], sha=(r.get("mr") or {}).get("sha") or "",
                                         target=(r.get("mr") or {}).get("target_branch") or "",
                                         detail=r["next"]["action"][:160], ok=True)
                greenlight.note(name, f"{ref} {r['next']['step']}: {r['next']['action'][:160]} → dispatched {agent} with its state card (orchestrator)")
            except Exception as e:  # launch already happened; it still consumes this tick's dispatch slot
                done.append(f"dispatch recording failed on {name}: {type(e).__name__}: {str(e)[:100]}")
        else:
            if greenlight.LIMIT_RE.search(err) or re.search(r"rejected|invalid agent|unknown agent|not configured|unsupported agent|no such agent|worktree not found|permission denied", err, re.I):
                clear_pending(r, a["sig"], assignment)  # a definite rejection can be tried again; an uncertain launch needs judgment
            try:
                ws_state.update(key, if_situation=assignment, failed=f"launch failed: {err[:200]}")
            except SystemExit:
                pass  # the failure belongs to a superseded card assignment
            done.append(f"dispatch failed on {name}: {err[:150]}")
    elif a["do"] in ("skip", "fork"):
        done.append(f"left {name}: {a.get('why')}")
    return done


def tick():
    out = {"phases": {}}
    code, text = script("adopt.py", "apply")
    adopt_failed = bool(code)
    out["phases"]["adopt"] = {"ok": code == 0, "out": text[-1500:]}
    code, text = script("greenlight.py", "report")
    out["phases"]["scan"] = {"ok": code == 0, "out": text[-300:]}
    if code:
        out["error"] = "scan failed: nothing dispatched"
        return out
    res = json.loads((greenlight.STATE / "last.json").read_text())
    rows = res["rows"]
    cards, moved = phase_cards(rows)
    card_failures = [r for r in rows if r.get("_card_error")]
    out["phases"]["cards"] = {"ok": not card_failures, "moved": moved}
    try:
        out["phases"]["me"] = {"ok": True, "pinged": phase_me(rows, cards)}
    except Exception as e:
        out["phases"]["me"] = {"ok": False, "error": str(e)[:200]}
    acts = plan(rows, cards)
    done = act(rows, cards, acts)
    failures = [d for d in done if d.startswith("FAILED ") or " failed on " in d]
    out["phases"]["act"] = {"ok": not failures, "done": done}
    code, text = script("cleanup.py", "apply")
    out["phases"]["cleanup"] = {"ok": code == 0, "out": text[-1500:]}
    if adopt_failed or card_failures or failures or code or not out["phases"]["me"]["ok"]:
        out["error"] = "; ".join((["adopt failed"] if adopt_failed else []) +
                                  ([f"card refresh failed for {len(card_failures)} workspace(s)"] if card_failures else []) +
                                  failures[:3] + (["cleanup failed"] if code else []) +
                                  (["me notification failed"] if not out["phases"]["me"]["ok"] else []))
    owners = {}
    for r in rows:
        owners[r["next"]["owner"]] = owners.get(r["next"]["owner"], 0) + 1
    out["owners"] = owners
    for k, ph in out["phases"].items():
        diag.event("phase", phase=k, ok=ph["ok"], n=len(ph.get("done") or ()) or None)
    return out


def show(out):
    lines = [f"report: file://{greenlight.STATE / 'report.html'}",
             "owners: " + ", ".join(f"{k} {v}" for k, v in sorted((out.get("owners") or {}).items(), key=lambda kv: greenlight.OWNERS.index(kv[0]) if kv[0] in greenlight.OWNERS else 99))]
    if out.get("error"):
        lines.append(f"ERROR: {out['error']}")
    ph = out.get("phases", {})
    for k in ("adopt", "scan", "cleanup"):
        if k in ph and not ph[k]["ok"]:
            lines.append(f"{k} failed: {ph[k]['out'][-200:]}")
    created = [l for l in (ph.get("adopt", {}).get("out") or "").splitlines() if l.startswith("- ✓")]
    lines += [f"adopted {l[4:]}" for l in created]
    lines += ph.get("act", {}).get("done", [])
    lines += [f"deleted {l[4:]}" for l in (ph.get("cleanup", {}).get("out") or "").splitlines() if l.startswith("- ✓")]
    moved = ph.get("cards", {}).get("moved", [])
    new = [m for m in moved if ": (new) → " in m]
    lines += [f"moved {m}" for m in moved if m not in new][:20] + [f"{len(new)} new state card(s)"] * bool(new)
    print("\n".join(lines))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "plan":
        rows = json.loads((greenlight.STATE / "last.json").read_text())["rows"]
        acts = plan(rows, {}, judge=False)
        print(json.dumps(acts, indent=1) if "--json" in sys.argv else "\n".join(
            f"{a['do']:8} {a['ws']}: " + (a.get("goal") or " ".join(a.get("cmd") or []) or a.get("why") or a.get("sig") or "") for a in acts))
    elif cmd == "tick":
        global TICK_LOCK_FD
        greenlight.STATE.mkdir(parents=True, exist_ok=True)
        with open(greenlight.STATE / "orchestrate.lock", "w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return print("another tick is running: skipped")
            TICK_LOCK_FD = lk.fileno()
            try:
                out = tick()
            finally:
                TICK_LOCK_FD = None
        print(json.dumps(out, indent=1) if "--json" in sys.argv else "", end="")
        if "--json" not in sys.argv:
            show(out)
        sys.exit(1 if out.get("error") else 0)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    diag.install("orchestrate")
    import external
    external.TTL = 0  # every live re-check before a dispatch or merge reads the process table afresh
    main()
