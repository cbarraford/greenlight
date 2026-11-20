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
chores apply. Sub-stages a scan can't tell apart yet aren't reported: 1.2-1.4, 2.1, 2.3, 2.4, 5.2.

Stdlib only, and it never imports greenlight.py.
"""
import json
import os
import sys
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


def place(r, nxt=None):
    """A scan row's place in the lifecycle. nxt: the row's next step as evaluate() gave it, before the agent overlay
    changed its owner (the row's own `next` otherwise). The agent's state is applied on top by now().
    `do` is the whole move in a sentence or two: what is true, then what to do about it (or who is doing it)."""
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
        return spot("review", "open-for-review", "author", "greenlight", "Mark it ready", f"{Ref} is still a draft. Greenlight marks it ready on its next run.", why)
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
    if step and step not in BUILTIN and mr.get("state") == "merged":  # a lifecycle step after `merge` (deploy, verify): it's in, not over
        name = step.replace("_", " ")
        rest = sentence(why.replace("Sign it off?", "Confirm it is met to sign it off.")) if ":" in why and "merged from" not in why else ""
        if owner == "me":
            return spot("land", "merged", "author", "you", f"Sign off {name}",
                        f"{Ref} is merged, and {name} comes after the merge. " + (rest or "Do it, then sign it off."), why)
        return spot("land", "merged", "author", "agent", f"Meet {name}", f"{Ref} is merged, and {name} comes after the merge. {rest} Start an agent on it.".replace("  ", " "), why)
    if step and step not in BUILTIN:  # the repo's own step: a label the agent earns, or my sign-off
        name = step.replace("_", " ")
        if "Sign it off" in why:
            return spot("verify", "author-check", "author", "you", f"Sign off {name}", sentence(why.replace("Sign it off?", "Confirm it is met to sign it off.")), why)
        return spot("verify", "author-check", "author", "agent", f"Meet {name}", f"{sentence(why)} Start an agent on it.", why)
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


if __name__ == "__main__":
    main()
