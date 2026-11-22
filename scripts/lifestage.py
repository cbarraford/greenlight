#!/usr/bin/env python3
"""Where a code change is in its life, who is holding it up, and what to do next.

A change goes through five stages. Each has sub-stages, and each sub-stage ends on something that can be observed:

    1 Define   1.1 Capture  1.2 Scope  1.3 Done condition  1.4 Plan
    2 Build    2.1 Set up  2.2 Implement  2.3 Self-test  2.4 Propose
    3 Verify   3.1 Pipeline running  3.2 Pipeline green  3.3 Mergeable  3.4 Author check
    4 Review   4.1 Open for review  4.2 First review  4.3 Respond  4.4 Re-review  4.5 Approved
    5 Land     5.1 Merge gate  5.2 Queued  5.3 Merged

Merged or closed (or its branch deleted on the remote) is done: the workspace can go. A lifecycle's steps after `merge`
(deploy, verify) keep a merged change at 5.3 Land › Merged, with the step as its move, until they're met. A push during Review sends a change
back to Verify, so "the first thing that isn't met" is always where it is.

    lifestage.py [<workspace>] [--json]    every scanned workspace by pile: where it is, who holds it, the suggested move

`greenlight.py scan` puts place() on every row as `life`:

    {"pile": "yours" | "working" | "waiting" | "done" | "parked",
     "stage", "stage_name", "sub", "sub_name", "at": "4.3",          # None when it isn't on the path (parked, an error)
     "blocker": "author" | "reviewers" | "ci" | "dependency" | None,  # the side holding it up
     "by": "you" | "agent" | "greenlight" | "automation" | "others" | None,  # who on that side moves next
     "holder": "you",                                                 # the same, in words
     "do": "!7 is ready to merge, but your goal says hold. Merge it now, or leave the hold in place.",  # the whole move, said plainly
     "why": "...",                                                    # greenlight's own terse reason (next.action)
     "suggestion": {"label": "Resolve 2 threads", "kind": "agent" | "you" | "greenlight" | "answer"} | None}

Piles: yours (your side holds it and something should start: the suggestion says what; this includes an MR that
blocking.py's roster says someone is waiting on you for), working (your side holds it and
is already on it), waiting (someone else holds it), done, parked.

It reads greenlight's own next step (`next`) and the MR's facts off the row, so a repo's lifecycle file still decides which
chores apply. Sub-stages a scan can't tell apart yet aren't reported: 1.2, 1.4, 2.1, 5.2.

Two sub-stages end on evidence the forge doesn't hold, so greenlight keeps it (~/.greenlight/checks/) and every row carries it
as `life["checks"]`:

    1.3 Done condition  preflight(): before a new workspace's agent starts (dash.py task_new / tree_new), decide.py says how
                        likely the goal is ambiguous and how clear its done condition is. Over AMBIGUOUS it pauses and asks 1-2
                        questions with options; the answers go to the agent. checks.done_condition is that verdict, while the
                        goal is still the one it judged.
    2.3 Self-test       `lifestage.py selftest -- <test command>`, run in the worktree, records the exit code against the git tree
                        that was tested. A workspace with no MR whose HEAD is that tree is at 2.4 Propose when it passed, and 2.3
                        Self-test when it failed or the code changed since. checks.self_test says which.

    lifestage.py selftest [--] <cmd...>     run the tests in this worktree and put the result on record (exits with theirs)

Stdlib only, and it never imports greenlight.py.
"""
import collections
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
STAGES = (
    ("define", "Define", (("capture", "Capture"), ("scope", "Scope"), ("done-condition", "Done condition"), ("plan", "Plan"))),
    ("build", "Build", (("set-up", "Set up"), ("implement", "Implement"), ("self-test", "Self-test"), ("propose", "Propose"))),
    ("verify", "Verify", (("pipeline-running", "Pipeline running"), ("pipeline-green", "Pipeline green"), ("mergeable", "Mergeable"),
                          ("author-check", "Author check"))),
    ("review", "Review", (("open-for-review", "Open for review"), ("first-review", "First review"), ("respond", "Respond"),
                          ("re-review", "Re-review"), ("approved", "Approved"))),
    ("land", "Land", (("merge-gate", "Merge gate"), ("queued", "Queued"), ("merged", "Merged"))),
)
PILES = ("yours", "working", "waiting", "done", "parked")
PILE_NAMES = {"yours": "Yours", "working": "Working", "waiting": "Waiting for others", "done": "Done", "parked": "Parked"}
HOLDER = {"you": "you", "agent": "your agent", "greenlight": "Greenlight", "automation": "an automation"}
OTHERS = {"author": "the author", "reviewers": "reviewers", "ci": "CI", "dependency": "another MR"}
BUILTIN = ("mr_open", "ci_green", "no_conflicts", "review_bot", "milestone", "ready", "threads", "approved", "merge")


def at(stage, sub):
    """{"stage", "stage_name", "sub", "sub_name", "at"} for a stage and sub-stage key; all None off the path."""
    for i, (key, name, subs) in enumerate(STAGES, 1):
        if key == stage:
            j, sub_name = next(((j, n) for j, (k, n) in enumerate(subs, 1) if k == sub), (None, None))
            return {"stage": key, "stage_name": name, "sub": sub if j else None, "sub_name": sub_name, "at": f"{i}.{j}" if j else str(i)}
    return {"stage": None, "stage_name": None, "sub": None, "sub_name": None, "at": None}


def spot(stage, sub, blocker, by, label=None, do="", why="", pile=None):
    mine = by in HOLDER
    return at(stage, sub) | {
        "pile": pile or ("working" if by in ("greenlight", "automation") else "yours" if mine else "waiting"),
        "blocker": blocker, "by": by, "holder": HOLDER.get(by) or OTHERS.get(blocker), "do": do or sentence(why), "why": why,
        "suggestion": {"label": label, "kind": by} if label and mine else None}


def sentence(text):
    text = " ".join((text or "").split())
    return text[:1].upper() + text[1:] + ("" if not text or text[-1] in ".?!" else ".")


CHECKS = STATE / "checks"
AMBIGUOUS = 0.70  # P(ambiguous) over this pauses a new workspace before its agent starts
AMBIGUITY_Q = ("Is this coding task too ambiguous to start without asking first: would two capable engineers likely build "
               "noticeably different things from it, or not know when they are done?")
CLARITY_RUBRIC = ("How clear is this coding task's done condition? 1.0: it says what to build and how anyone can check it is done "
                  "(a test, a command's output, a behaviour to observe). 0.5: what to build is clear, how to tell it's done is not. "
                  "0.0: it's unclear what to build.")
QUESTIONS = {  # id: (question, options); the answer can also be the user's own words
    "done": ("What does done look like?", ["A draft PR with tests", "A working prototype I can try", "A written plan first, no code"]),
    "scope": ("How far should it go?", ["The smallest change that works", "The whole feature, edge cases included",
                                        "Investigate and report back first"]),
}


def _file(kind, where):
    """A check's record for a workspace: by its worktree path, or by name (a task's workspace before its path is known)."""
    return CHECKS / f"{kind}-{hashlib.sha1(str(where).encode()).hexdigest()[:16]}.json"


def save(kind, where, data):
    CHECKS.mkdir(parents=True, exist_ok=True)
    f = _file(kind, where)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data | {"ts": int(time.time())}))
    tmp.replace(f)


def record(kind, r):
    p = r.get("path")
    candidates = [p]
    if p:
        rp = os.path.realpath(p)
        if rp != p:
            candidates.append(rp)
    candidates.append(r.get("name"))
    for where in candidates:
        try:
            if where and isinstance(o := json.loads(_file(kind, where).read_text()), dict):
                return o
        except (OSError, ValueError):
            pass
    return None


def _p_true(v):
    """decide.bool's answer as P(True), whether its prob is P(True) or its confidence in `value`."""
    p = min(1.0, max(0.0, float(v.get("prob", 0.5))))
    return 1 - p if not v.get("value") and p > 0.5 else p


def preflight(goal, context="", answers=None, force=False, decide=None):
    """1.3 Done condition, before a new workspace's agent starts. `answers` ({question id: answer}) are the user's replies to an
    earlier pause; `force` starts it unanswered. Returns {"pause", "questions": [{"id", "q", "options"}], "brief": text for the
    agent, "record": what checks.done_condition shows, ...}. decide.py missing or failing never blocks a start: this gate is
    about clarity, not safety, so it opens and says why."""
    goal = " ".join(str(goal or "").split())
    answers = {k: " ".join(str(v).split())[:300] for k, v in (answers or {}).items() if k in QUESTIONS and str(v).strip()}
    said = [(QUESTIONS[k][0], v) for k, v in answers.items()]
    brief = "\n\nBefore you started I answered these:\n" + "\n".join(f"- {q} {a}" for q, a in said) if said else ""
    base = {"goal": goal, "answers": dict(said), "forced": bool(force and not said)}
    if force and not said:
        return {"pause": False, "questions": [], "brief": "", "why": "started without answering", "record": base | {"ok": False}}
    ctx = "\n\n".join(x for x in (f"Goal: {goal}", str(context or "")[:4000],
                                     "Answers to clarifying questions:\n" + "\n".join(f"{q} {a}" for q, a in said) if said else "") if x)
    try:
        decide = decide or importlib.import_module("decide")
        # decide.py's state is the text being judged, and must be JSON serializable for its cache key.
        amb = decide.bool(ctx, AMBIGUITY_Q)
        clear = decide.score(ctx, CLARITY_RUBRIC)
        if amb.get("provider") == "local" or clear.get("provider") == "local":
            why = "no preflight: " + str(amb.get("error") or clear.get("error") or "no System One provider configured")[:160]
            return {"pause": False, "questions": [], "brief": brief, "why": why,
                    "record": base | {"ok": None, "why": why}}
        p, clarity = _p_true(amb), min(1.0, max(0.0, float(clear.get("score", 0.5))))
    except Exception as e:  # ImportError until decide.py lands, a backend down, a malformed answer
        why = f"no preflight: {type(e).__name__}: {str(e)[:160]}"
        return {"pause": False, "questions": [], "brief": brief, "why": why, "record": base | {"ok": None, "why": why}}
    verdict = {"p_ambiguous": round(p, 3), "clarity": round(clarity, 3), "provider": amb.get("provider") or clear.get("provider")}
    if p <= AMBIGUOUS or said:  # answered once: never asked again, whatever the second look says
        return verdict | {"pause": False, "questions": [], "brief": brief, "why": "clear" if not said else "answered",
                          "record": base | verdict | {"ok": True}}
    ids = (["done"] if clarity < 0.6 else []) + (["scope"] if len(goal.split()) < 8 or clarity >= 0.6 else [])
    qs = [{"id": k, "q": QUESTIONS[k][0], "options": QUESTIONS[k][1]} for k in ids[:2]]
    return verdict | {"pause": True, "questions": qs, "brief": "", "why": f"P(ambiguous) {p:.2f} > {AMBIGUOUS}", "record": None}


def _git(path, *a, env=None):
    return subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, timeout=60, env=env, check=True).stdout.strip()


def worktree_tree(path):
    """The git tree of the worktree as it is on disk: HEAD plus every change, untracked files too (not ignored ones)."""
    with tempfile.TemporaryDirectory() as d:
        env = os.environ | {"GIT_INDEX_FILE": os.path.join(d, "index")}
        _git(path, "read-tree", "HEAD", env=env)
        _git(path, "add", "-A", env=env)
        return _git(path, "write-tree", env=env)


def self_test(r):
    """checks.self_test: the worktree's last recorded self-test, and whether it tested what HEAD holds now."""
    rec = record("selftest", {"path": r.get("path")}) if r.get("path") else None
    if not rec:
        return None
    try:
        fresh = bool(rec.get("tree")) and _git(r["path"], "rev-parse", "HEAD^{tree}") == rec["tree"]
    except (OSError, subprocess.SubprocessError):
        fresh = False
    return {k: rec.get(k) for k in ("sha", "cmd", "exit", "ts")} | {"ok": rec.get("exit") == 0, "fresh": fresh}


def checks(r):
    """Evidence for the sub-stages the forge can't show: 1.3's preflight verdict (while the goal is the one judged), 2.3's self-test."""
    pf = record("preflight", r)
    same = pf and pf.get("goal") == " ".join(str(r.get("goal") or "").split())
    return {"done_condition": pf if same else None, "self_test": self_test(r)}


def selftest(cmd, cwd="."):
    """lifestage.py selftest: run cmd in this worktree, echo its output, and record its exit code against the tested tree."""
    root = _git(cwd, "rev-parse", "--show-toplevel")
    try:
        tree, sha = worktree_tree(root), _git(root, "rev-parse", "HEAD")
    except subprocess.SubprocessError:  # no commit yet: nothing a PR could hold
        tree, sha = None, None
    tail = collections.deque(maxlen=15)
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    for line in p.stdout:
        sys.stdout.write(line)
        tail.append(line.rstrip()[:200])
    code = p.wait()
    save("selftest", root, {"tree": tree, "sha": sha, "cmd": " ".join(cmd)[:300], "exit": code, "tail": list(tail)})
    print(f"\nselftest: exit {code} recorded for {(sha or 'no commit')[:7]}" + ("" if tree else " (no commit yet, so it counts for nothing)"),
          file=sys.stderr)
    return code


def place(r, nxt=None):
    """A scan row's place in the lifecycle. nxt: the row's next step as evaluate() gave it, before the agent overlay
    changed its owner (the row's own `next` otherwise). The agent's state is applied on top by now().
    `do` is the whole move in a sentence or two: what is true, then what to do about it (or who is doing it)."""
    ck = checks(r)
    return _place(r, nxt, ck) | {"checks": ck}


PHASE_AT = {"define": ("define", "scope"), "build": ("build", "implement"), "verify": ("verify", "author-check"),
            "review": ("review", "approved"), "land": ("land", "merge-gate")}


def _place_custom(step, owner, why, ref, Ref, merged, phase):
    """Place a repo's own custom stage, respecting declared `phase` and `owner`."""
    name = step.replace("_", " ")
    st, sub = PHASE_AT.get(phase, ("land", "merged") if merged else ("verify", "author-check"))
    if merged:
        rest = sentence(why.replace("Sign it off?", "Confirm it is met to sign it off.")) if ":" in why and "merged from" not in why else ""
        if owner == "me":
            return spot(st, sub, "author", "you", f"Sign off {name}",
                        f"{Ref} is merged, and {name} comes after the merge. " + (rest or "Do it, then sign it off."), why)
        if owner in ("reviewers", "ci"):
            return spot(st, sub, owner, "others", do=f"{Ref} is merged, waiting on {OTHERS[owner]} for {name}.", why=why)
        return spot(st, sub, "author", "agent", f"Meet {name}", f"{Ref} is merged, and {name} comes after the merge. {rest} Start an agent on it.".replace("  ", " "), why)
    if owner == "me":
        lbl = f"Sign off {name}" if "Sign it off" in why else f"Complete {name}"
        return spot(st, sub, "author", "you", lbl, sentence(why.replace("Sign it off?", "Confirm it is met to sign it off.")), why)
    if owner == "reviewers":
        return spot(st, sub, "reviewers", "others", do=f"Waiting on reviewers for {name} on {ref}.", why=why)
    if owner == "ci":
        return spot(st, sub, "ci", "others", do=f"Waiting on CI check for {name} on {ref}.", why=why)
    return spot(st, sub, "author", "agent", f"Meet {name}", f"{sentence(why)} Start an agent on it.", why)


def _place(r, nxt, ck):
    nxt, mr = nxt or r.get("next") or {}, r.get("mr") or {}
    step, owner, why = nxt.get("step"), nxt.get("owner"), nxt.get("action") or ""
    ref, into = mr.get("ref") or "it", mr.get("target_branch") or "its target"
    Ref = mr.get("ref") or "It"
    if step == "done" or owner == "done":
        merged, closed = mr.get("state") == "merged", mr.get("state") == "closed"
        do = f"{Ref} is merged. The workspace can be deleted." if merged else f"{Ref} is closed. The workspace can be deleted." if closed \
            else f"You approved {ref}. Your part is done, so the workspace can be deleted." if mr else "Its branch was deleted on the remote. The workspace can be deleted."
        return spot("land" if merged else None, "merged" if merged else None, None, None, do=do, why=why, pile="done")
    if step == "plan":
        return spot("define", "plan", None, None, do="Being planned in the Plan view.", why=why, pile="parked")
    if step == "parked" or owner == "parked":
        return spot(None, None, None, None, why=why, pile="parked")
    if step == "automation":
        return spot("define", "capture", "author", "you", "Delete it or set a goal",
                    "An automation run left this workspace with no MR and no goal. Delete it, or give it a goal.", why)
    if step == "goal":
        return spot("define", "capture", "author", "you", "Set a goal", "There is no MR and no goal. Say what this workspace is for, or delete it.", why)
    if step == "freeform":
        if owner == "working":  # an Orca automation drives it
            return spot("build", "implement", "author", "automation", do="An Orca automation is running this. Nothing for you to do.", why=why)
        if st := ck["self_test"]:
            sha, cmd = (st["sha"] or "")[:7], st["cmd"]
            if st["fresh"] and st["ok"]:
                return spot("build", "propose", "author", "agent", "Open the draft PR",
                            f"Its self-test passed on {sha} (`{cmd}`). Start an agent to push it and open the draft PR.", why)
            if st["fresh"]:
                return spot("build", "self-test", "author", "agent", "Fix the self-test",
                            f"Its self-test failed on {sha} (`{cmd}`, exit {st['exit']}). Start an agent to fix it.", why)
            return spot("build", "self-test", "author", "agent", "Run the self-test again",
                        f"The code changed since its last self-test ({'passed' if st['ok'] else 'failed'} on {sha}). "
                        "Start an agent to finish it and run the tests again.", why)
        return spot("build", "implement", "author", "agent", "Work toward the goal", "There is no MR yet. Start an agent on the goal.", why)
    if step == "review":  # someone else's MR: my part is the review
        if owner == "me" and "re-review" in why:
            return spot("review", "re-review", "reviewers", "you", f"Re-review {ref}",
                        f"The author replied on your threads on {ref}. Read the replies, then resolve each thread or answer it.", why)
        if owner == "me":
            return spot("review", "first-review", "reviewers", "you", f"Review {ref}",
                        f"{Ref} is waiting for your review. Read it, then leave threads or approve.", why)
        if owner == "author":
            return spot("review", "respond", "author", "others", do=f"Waiting for the author to answer your open threads on {ref}.", why=why)
        return spot("review", "approved", "reviewers", "others", do=f"Your commits are in {ref}, so you can't approve it. Waiting for the other reviewers.", why=why)
    if step == "ci_green":
        if owner == "ci":
            return spot("verify", "pipeline-running", "ci", "others", do=f"Waiting for the pipeline on {ref} to finish.", why=why)
        jobs = ", ".join(j["name"] for j in (mr.get("failed_jobs") or [])[:3])
        return spot("verify", "pipeline-green", "author", "agent", "Fix CI",
                    f"The pipeline failed on {ref}{f' ({jobs})' if jobs else ''}. Start an agent to find the first real error and fix it.", why)
    if step == "no_conflicts":
        return spot("verify", "mergeable", "author", "agent", "Fix conflicts", f"{Ref} conflicts with {into}. Start an agent to rebase it.", why)
    if step == "review_bot":
        return spot("review", "open-for-review", "author", "greenlight", "Assign the review bot",
                    f"{Ref} has no review bot yet. Greenlight adds it on its next run.", why)
    if step == "milestone":
        if owner == "me":
            return spot("review", "open-for-review", "author", "you", "Pick a milestone",
                        f"{Ref} has no milestone, and no single release stands out. Pick the milestone it ships in.", why)
        return spot("review", "open-for-review", "author", "greenlight", "Set the milestone", f"{Ref} has no milestone. Greenlight sets it on its next run.", why)
    if step == "ready":
        if owner == "me":
            return spot("review", "open-for-review", "author", "you", "Take it out of draft?",
                        f"Everything else on {ref} is green, but your goal says to keep it a draft. Mark it ready for review, or leave it.", why)
        st = ck["self_test"]
        note = "" if not st or st["fresh"] and st["ok"] else \
            f" Its last self-test {'failed' if st['fresh'] else 'is older than its head commit'} (`{st['cmd']}`)."
        return spot("review", "open-for-review", "author", "greenlight", "Mark it ready", f"{Ref} is still a draft. Greenlight marks it ready on its next run.{note}", why)
    if step == "threads":
        if owner == "reviewers":
            return spot("review", "re-review", "reviewers", "others",
                        do=f"You answered every open thread on {ref}. Waiting for the reviewers to look again.", why=why)
        n = len(mr.get("todo_threads") or []) or mr.get("threads") or 0
        ts = f"{n} thread{'s' * (n != 1)}" if n else "threads"
        return spot("review", "respond", "author", "agent", f"Resolve {ts}",
                    f"{ts.capitalize()} on {ref} {'needs' if n == 1 else 'need'} a fix or a reply. Start an agent to work through {'it' if n == 1 else 'them'}.", why)
    if step == "approved":
        fresh = not mr.get("approvals") and not mr.get("threads_total")  # nobody has looked yet
        got, need = mr.get("approvals") or 0, mr.get("approvals_required") or 0
        return spot("review", "first-review" if fresh else "approved", "reviewers", "others", why=why,
                    do=f"Nobody has reviewed {ref} yet. Waiting for a first review." if fresh
                    else f"Waiting for approvals on {ref}{f' ({got} of {need} so far)' if need > got else ''}.")
    if step == "merge":
        if owner == "greenlight":
            return spot("land", "merge-gate", "author", "greenlight", f"Merge {ref}",
                        f"{Ref} is ready to merge. Greenlight merges it on its next run, or merge it now.", why)
        if owner == "reviewers":  # mergeable, but no human has approved
            return spot("review", "approved", "reviewers", "others", do=f"{Ref} could merge, but no person has approved it. Waiting for a reviewer.", why=why)
        if owner == "ci":
            return spot("land", "merge-gate", "ci", "others", do=f"Waiting for the pipeline before {ref} can merge.", why=why)
        if owner == "dependency":
            return spot("land", "merge-gate", "dependency", "others", do=f"{Ref} is {why}.", why=why)
        if owner == "agent":
            return spot("verify", "mergeable", "author", "agent", f"Rebase {ref}",
                        f"{Ref} must be rebased onto {into} before it can merge. Start an agent to rebase it.", why)
        if mr.get("rebase") or "rebase" in why:
            return spot("verify", "mergeable", "author", "you", f"Rebase {ref}?",
                        f"{Ref} must be rebased onto {into} before it can merge. There is no conflict. Say whether to rebase it.", why)
        if "hold" in why:
            return spot("land", "merge-gate", "author", "you", "Merge now or keep holding?",
                        f"{Ref} is ready to merge, but your goal says hold. Merge it now, or leave the hold in place.", why)
        if "nobody else reviews" in why:
            return spot("land", "merge-gate", "author", "you", "Review it, then merge",
                        f"{Ref} is ready to merge and nobody else reviews this repo. Read the diff yourself, then merge it.", why)
        if "by hand" in why:
            return spot("land", "merge-gate", "author", "you", "Merge it by hand?",
                        f"{Ref} is ready to merge into {into}, which is not the default branch. Merge it by hand if that is what you want.", why)
        return spot("land", "merge-gate", "author", "you", "See why it can't merge",
                    f"The forge won't merge {ref} ({why.removeprefix('not mergeable: ')}). Open it and clear what blocks it.", why)
    if step == "error":
        return spot(None, None, "author", "you", "Look at the error", f"Greenlight couldn't work out where this stands. {sentence(why)}", why)
    if step and step not in BUILTIN:  # the repo's own step: label, check, or sign-off (pre-PR, pre-merge, or post-merge)
        return _place_custom(step, owner, why, ref, Ref, mr.get("state") == "merged", nxt.get("phase") or "")
    return spot(None, None, None, None, why=why, pile="waiting")


BLOCK_MOVES = {"approval": "Review it", "discussion": "Reply in the thread", "conflict": "Fix conflicts", "pipeline": "Fix CI"}


def blocks(max_age=6 * 3600):
    """blocking.py's last roster scan, {"<project>!<iid>": {reason, why}}: the open MRs someone is waiting on me for.
    Empty when there's none, or none younger than max_age."""
    try:
        c = json.loads((STATE / "blocks.json").read_text())
    except (OSError, ValueError):
        return {}
    return c.get("blocks") or {} if time.time() - c.get("ts", 0) <= max_age else {}


def block_of(r, roster):
    """The block on me for this row's open MR, or None."""
    mr = r.get("mr") or {}
    return mr.get("state") == "opened" and (roster or {}).get(f"{r.get('project')}!{mr.get('iid')}") or None


WAS = ("pile", "by", "holder", "do", "why", "suggestion", "blocker", "stage", "stage_name", "sub", "sub_name", "at")


def now(life, agent_state, logged_out=False, block=None):
    """`life` with what's true right now applied: a waiting agent makes it yours, a working one means it's in hand, and
    an MR the roster says I'm blocking (block: blocking.py's entry for it) is mine whatever the threads' last authors say."""
    life = {k: v for k, v in life.items() if k != "_was"} | life.get("_was", {})  # undo an earlier now()
    was = {k: life.get(k) for k in WAS}
    if agent_state == "waiting":
        life = life | {"pile": "yours", "by": "you", "holder": "you", "why": "your agent is waiting on your answer",
                       "do": "Your agent asked you a question and has stopped until you answer it.",
                       "suggestion": {"label": "Answer your agent", "kind": "answer"}}
    elif block and life["pile"] != "done" and life["by"] not in HOLDER:  # my side doesn't know it holds this one
        if block.get("reason") == "discussion" and life["sub"] == "re-review":
            life = life | at("review", "respond")  # a thread is waiting on me after all
        what = block.get("why") or block.get("reason")
        life = life | {"pile": "yours", "blocker": "reviewers" if block.get("reason") == "approval" else "author",
                       "by": "you", "holder": "you", "why": f"you're blocking: {what}",
                       "do": f"Someone is waiting on you here: {what}. " + ("Review it, then approve or leave threads." if block.get("reason") == "approval"
                                                                           else "Reply in the thread." if block.get("reason") == "discussion" else "Clear it."),
                       "suggestion": {"label": BLOCK_MOVES.get(block.get("reason"), "See what's waiting on you"), "kind": "you"}}
    elif life["by"] == "agent" and logged_out:
        life = life | {"pile": "yours", "by": "you", "holder": "you", "why": "your agent is logged out",
                       "do": "Your agent is logged out, so the work has stopped. Log it back in and Greenlight starts it again.",
                       "suggestion": {"label": "Log your agent back in", "kind": "you"}}
    elif life["by"] == "agent" and agent_state == "working":
        life = life | {"pile": "working", "do": life.get("do", "").split(" Start an agent")[0] + " Your agent is working on it now."}
    else:
        return life
    return life | {"_was": was}


def rows():
    f = STATE / "last.json"
    return json.loads(f.read_text()).get("rows", []) if f.exists() else []


def of(r):
    """A row's life: what the scan stored, else worked out from the row (a snapshot from before lifestage.py)."""
    life = r.get("life") or place(r)
    return now(life, (r.get("agent") or {}).get("state"), "logged out" in (r.get("next") or {}).get("action", ""), block_of(r, blocks()))


def main():
    if sys.argv[1:2] == ["selftest"]:
        cmd = sys.argv[2:][sys.argv[2:3] == ["--"]:]
        sys.exit(selftest(cmd) if cmd else "usage: lifestage.py selftest [--] <test command...>")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    found = [(r, of(r)) for r in rows() if not args or r["name"] in args or r["id"] in args]
    if "--json" in sys.argv:
        print(json.dumps([{"workspace": r["name"], "repo": r.get("project"), "mr": (r.get("mr") or {}).get("ref")}
                          | {k: v for k, v in life.items() if k != "_was"} for r, life in found], indent=1))
        return
    if not found:
        print("no scanned workspace" + (f" named {' or '.join(args)}" if args else "") + ": run greenlight.py scan first")
        return
    for pile in PILES:
        mine = [(r, life) for r, life in found if life["pile"] == pile]
        if not mine:
            continue
        print(f"\n{PILE_NAMES[pile].upper()} ({len(mine)})")
        for r, life in sorted(mine, key=lambda x: (x[1]["at"] or "9", x[0]["name"])):
            where = f"{life['at']} {life['stage_name']}" + (f" › {life['sub_name']}" if life["sub_name"] else "") if life["at"] else "-"
            move = (life["suggestion"] or {}).get("label") or " ".join(life["why"].split())[:60]
            ref = (r.get("mr") or {}).get("ref") or ""
            print(f"  {r['name'][:26]:26} {ref:6} {where:30} {(life['holder'] or '-'):14} {move}")
            if args and life.get("do"):
                print(f"      {life['do']}")
            for line in (evidence(life.get("checks") or {}) if args else []):
                print(f"      {line}")


def evidence(ck):
    """checks as lines for `lifestage.py <workspace>`."""
    out, pf, st = [], ck.get("done_condition"), ck.get("self_test")
    if pf:
        how = "started without answering" if pf.get("forced") else pf.get("why") if pf.get("ok") is None else \
            f"P(ambiguous) {pf.get('p_ambiguous')}, clarity {pf.get('clarity')} ({pf.get('provider')})"
        out.append(f"1.3 done condition: {how}" + "".join(f"; {q} {a}" for q, a in (pf.get("answers") or {}).items()))
    if st:
        out.append(f"2.3 self-test: `{st['cmd']}` exit {st['exit']} on {(st['sha'] or '-')[:7]}" + ("" if st["fresh"] else ", stale: the code changed since"))
    return out


if __name__ == "__main__":
    main()
