"""python3 test_lifestage.py: where a change is in its lifecycle, who holds it and the suggested move, from greenlight's own
next step on canned MRs (no network)."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import json, shutil, subprocess
import greenlight, lifestage
import audit
audit.merge_verdict = lambda ws, mr: {"ok": True, "failed": [], "target_oid": "base1"}  # lifecycle placement assumes a completed merge audit

greenlight.BOT = "review-bot"
(greenlight.STATE / "lifecycles").mkdir()
shutil.copy(greenlight.ROOT / "examples/lifecycles/release-train.md", greenlight.STATE / "lifecycles/widgets.md")
(greenlight.STATE / "lifecycles/gadgets.md").write_text(
    "# gadgets\n\nOptions: `solo`\n\n1. `mr_open` — x\n2. `ci_green` — x\n3. `qa` — QA passed. Done: `label=qa-ok`\n"
    "4. `legal` — Legal looked.\n5. `merge` — x\n")
(greenlight.STATE / "lifecycles/gizmos.md").write_text(  # steps after merge (PR #18): the MR is in, the change isn't over
    "# gizmos\n\n1. `mr_open` — x\n2. `merge` — x\n3. `deploy` — Prod runs it. Done: `signoff`\n4. `smoke` — Smoke passed. Done: `label=smoke-ok`\n")

U = lambda n: {"username": n}
thread = lambda who, last: {"notes": [{"resolvable": True, "resolved": False, "author": U(who)}, {"author": U(last)}]}


def mr(**kw):
    base = {"iid": 7, "state": "opened", "author": U(greenlight.ME), "draft": False, "has_conflicts": False,
            "assignees": [U(greenlight.BOT)], "reviewers": [U(greenlight.BOT)], "milestone": {"title": "R"},
            "head_pipeline": {"status": "success", "web_url": "u"}, "blocking_discussions_resolved": True,
            "detailed_merge_status": "mergeable", "_discussions": [], "_approved_by": ["alice"],
            "sha": "abc", "target_branch": "main", "_default_branch": "main"}
    return base | kw


def row(m, goal="", agent="idle", project="acme/widgets", branch="b", **ws):
    """A scan row the way scan() builds it: the MR's summary, greenlight's next step, then its life."""
    r = {"id": "w1", "name": "w1", "project": project, "goal": goal, "branch": branch, "agent": {"state": agent}} | ws
    r["mr"] = m and {"iid": m["iid"], "ref": f"!{m['iid']}", "state": m["state"], "role": "author" if m["author"]["username"] == greenlight.ME else "review",
                     "threads": len(greenlight.unresolved(m)), "approvals": len(m.get("_approved_by", [])),
                     "todo_threads": [1 for d in greenlight.unresolved(m) if greenlight.awaiting_me(d)],
                     "threads_total": len(m.get("_discussions", [])), "rebase": m.get("detailed_merge_status") == "need_rebase"}
    r["next"] = greenlight.evaluate(r, m, {"title": "Release-9.9.9"})
    r["life"] = lifestage.now(lifestage.place(r), agent)
    return r


def life(m, **kw):
    l = row(m, **kw)["life"]
    return l["at"], l["pile"], l["holder"], (l["suggestion"] or {}).get("label")


do = lambda m, **kw: row(m, **kw)["life"]["do"]

# Define and Build: no MR yet
assert life(None) == ("1.1", "yours", "you", "Set a goal")
assert life(None, branch="auto-nightly") == ("1.1", "yours", "you", "Delete it or set a goal")
assert life(None, goal="add retries") == ("2.2", "yours", "your agent", "Work toward the goal")
assert life(None, goal="add retries", agent="working") == ("2.2", "working", "your agent", "Work toward the goal")
assert life(None, goal="nightly sweep (automation)") == ("2.2", "working", "an automation", None)

# Verify
assert life(mr(head_pipeline={"status": "running", "web_url": "u"})) == ("3.1", "waiting", "CI", None)
assert life(mr(head_pipeline={"status": "failed", "web_url": "u"})) == ("3.2", "yours", "your agent", "Fix CI")
assert life(mr(head_pipeline={"status": "failed", "web_url": "u"}), agent="working")[:2] == ("3.2", "working")
assert life(mr(has_conflicts=True)) == ("3.3", "yours", "your agent", "Fix conflicts")
assert life(mr(detailed_merge_status="need_rebase")) == ("3.3", "yours", "you", "Rebase !7?")
# a push during Review that breaks CI is back in Verify, whatever the review said
assert life(mr(head_pipeline={"status": "failed", "web_url": "u"}, _discussions=[thread("alice", "alice")]))[0] == "3.2"

# Review: the chores open it, then the loop
assert life(mr(assignees=[])) == ("4.1", "working", "Greenlight", "Assign the review bot")
assert life(mr(milestone=None)) == ("4.1", "working", "Greenlight", "Set the milestone")
r = row(mr(milestone=None)); r["next"] = greenlight.evaluate(r, mr(milestone=None), None)  # no release milestone stands out
assert lifestage.place(r)["suggestion"] == {"label": "Pick a milestone", "kind": "you"} and lifestage.place(r)["pile"] == "yours"
assert life(mr(draft=True)) == ("4.1", "working", "Greenlight", "Mark it ready")
assert life(mr(draft=True), goal="x (keep draft)") == ("4.1", "yours", "you", "Take it out of draft?")
assert life(mr(_discussions=[thread("alice", "alice"), thread("bob", "bob")])) == ("4.3", "yours", "your agent", "Resolve 2 threads")
assert life(mr(_discussions=[thread("alice", "alice"), thread("bob", "octo")]))[3] == "Resolve 1 thread"
assert life(mr(_discussions=[thread("alice", "octo")])) == ("4.4", "waiting", "reviewers", None)
assert life(mr(_approvals_left=2, _approved_by=[])) == ("4.2", "waiting", "reviewers", None)  # nobody has looked
assert life(mr(_approvals_left=1)) == ("4.5", "waiting", "reviewers", None)  # one in, one to go
assert life(mr(_approved_by=[])) == ("4.5", "waiting", "reviewers", None)  # mergeable, but no human approved

# Land
assert life(mr()) == ("5.1", "working", "Greenlight", "Merge !7")
assert life(mr(), goal="x (hold)") == ("5.1", "yours", "you", "Merge now or keep holding?")
assert life(mr(detailed_merge_status="ci_still_running")) == ("5.1", "waiting", "CI", None)
assert life(mr(detailed_merge_status="merge_request_blocked", _blocked_by=[3])) == ("5.1", "waiting", "another MR", None)
assert life(mr(detailed_merge_status="blocked_status")) == ("5.1", "yours", "you", "See why it can't merge")
assert life(mr(_approved_by=[]), project="acme/gadgets")[3] == "Meet qa"  # the repo's own steps come first there

# the repo's own steps: a label the agent earns, a sign-off that's mine
assert life(mr(), project="acme/gadgets") == ("3.4", "yours", "your agent", "Meet qa")
assert life(mr(labels=["qa-ok"]), project="acme/gadgets") == ("3.4", "yours", "you", "Sign off legal")
(greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/gadgets#7": ["legal"]}))
assert life(mr(labels=["qa-ok"], _approved_by=[]), project="acme/gadgets") == ("5.1", "yours", "you", "Review it, then merge")  # solo

# done: merged, closed, approved by me, or the branch is gone from the remote
assert life(mr(state="merged")) == ("5.3", "done", None, None)
assert life(mr(state="closed")) == (None, "done", None, None)
assert life(None, goal="add retries", branch_gone=True) == (None, "done", None, None)
assert life(mr(author=U("alice"), _approved_by=["octo"]))[1] == "done"

# after the merge: the lifecycle's own steps keep it at Land › Merged, with that step as the move, until they're met
assert life(mr(state="merged"), project="acme/gizmos") == ("5.3", "yours", "you", "Sign off deploy")
assert do(mr(state="merged"), project="acme/gizmos") == "!7 is merged, and deploy comes after the merge. Deploy: Prod runs it. Confirm it is met to sign it off."
(greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/gizmos#7": ["deploy"]}))
assert life(mr(state="merged"), project="acme/gizmos") == ("5.3", "yours", "your agent", "Meet smoke")
assert life(mr(state="merged"), project="acme/gizmos", agent="working")[:2] == ("5.3", "working")
assert life(mr(state="merged", labels=["smoke-ok"]), project="acme/gizmos") == ("5.3", "done", None, None)
# merged from the card: dash.py points the row at the first step after merge until the next scan
r = row(mr(state="merged"), project="acme/gizmos"); r["next"] = {"step": "deploy", "owner": "me", "action": "merged from Greenlight; next: deploy"}
assert (lifestage.place(r)["at"], lifestage.place(r)["suggestion"]["label"], lifestage.place(r)["do"]) == \
    ("5.3", "Sign off deploy", "!7 is merged, and deploy comes after the merge. Do it, then sign it off.")

# someone else's MR: I only hold it while the review is mine
theirs = lambda **kw: mr(author=U("alice"), _approved_by=[], **kw)
assert life(theirs()) == ("4.2", "yours", "you", "Review !7")
assert life(theirs(_discussions=[thread("octo", "alice")])) == ("4.4", "yours", "you", "Re-review !7")
assert life(theirs(_discussions=[thread("octo", "octo")])) == ("4.3", "waiting", "the author", None)
assert life(theirs(_can_approve=False)) == ("4.5", "waiting", "reviewers", None)

# `do` says the whole move: what is true, then what to do about it (or who is doing it)
assert do(mr(), goal="x (hold)") == "!7 is ready to merge, but your goal says hold. Merge it now, or leave the hold in place."
assert do(theirs(_discussions=[thread("octo", "alice")])) == "The author replied on your threads on !7. Read the replies, then resolve each thread or answer it."
assert do(mr(_discussions=[thread("alice", "alice")])) == "1 thread on !7 needs a fix or a reply. Start an agent to work through it."
assert do(mr(_discussions=[thread("alice", "alice")]), agent="working") == "1 thread on !7 needs a fix or a reply. Your agent is working on it now."
assert do(mr(_discussions=[thread("alice", "alice")]), agent="waiting") == "Your agent asked you a question and has stopped until you answer it."
assert do(mr(_approvals_left=1)) == "Waiting for approvals on !7." and do(mr(state="merged")) == "!7 is merged. The workspace can be deleted."
assert do(mr(detailed_merge_status="blocked_status")) == "The forge won't merge !7 (GitHub blocks the merge (branch protection): check the PR). Open it and clear what blocks it."
assert do(mr(detailed_merge_status="merge_request_blocked", _blocked_by=[3])) == "!7 is waiting on !3 to merge first."
assert do(None) == "There is no MR and no goal. Say what this workspace is for, or delete it."
assert do(mr(iid=8, labels=["qa-ok"]), project="acme/gadgets") == "Legal: Legal looked. Confirm it is met to sign it off."  # !7 is signed off above
assert all(row(m)["life"]["do"] for m in (mr(), None, theirs(), mr(draft=True), mr(milestone=None), mr(has_conflicts=True)))

# off the path
assert life(mr(), goal="x (parked)")[:2] == (None, "parked")
assert life(mr(), goal="x (revisit 2999-01-01)")[:2] == (None, "parked")
assert life(None, goal="Plan: the retry work")[:2] == ("1.4", "parked")

# the agent's state right now, on top: a waiting agent is always mine; applying it twice changes nothing
base = lifestage.place(row(mr(_discussions=[thread("alice", "octo")])))
asked = lifestage.now(base, "waiting")
assert (asked["pile"], asked["holder"], asked["suggestion"]["label"], asked["at"]) == ("yours", "you", "Answer your agent", "4.4")
assert lifestage.now(asked, "waiting") == asked
back = lifestage.now(asked, "idle")
assert (back["pile"], back["holder"], back["suggestion"]) == ("waiting", "reviewers", None) and "_was" not in back
ci = lifestage.place(row(mr(head_pipeline={"status": "failed", "web_url": "u"})))
assert lifestage.now(ci, "idle", logged_out=True)["suggestion"]["label"] == "Log your agent back in"
assert lifestage.now(lifestage.now(ci, "working"), "idle")["pile"] == "yours"
# the roster says a thread is waiting on me, though I wrote last on it: mine, and back at Respond. Not once it's merged.
answered = lifestage.place(row(mr(_discussions=[thread("alice", "octo")])))
mine = lifestage.now(answered, "idle", block={"reason": "discussion", "why": "2 threads are waiting on you"})
assert (mine["at"], mine["pile"], mine["holder"], mine["blocker"], mine["suggestion"]["label"]) == ("4.3", "yours", "you", "author", "Reply in the thread")
assert mine["why"] == "you're blocking: 2 threads are waiting on you" and lifestage.now(mine, "idle") == answered
assert mine["do"] == "Someone is waiting on you here: 2 threads are waiting on you. Reply in the thread."
assert lifestage.now(answered, "waiting", block={"reason": "discussion"})["suggestion"]["label"] == "Answer your agent"  # its question first
assert lifestage.now(lifestage.place(row(mr(state="merged"))), "idle", block={"reason": "discussion"})["pile"] == "done"
fix = lifestage.place(row(mr(head_pipeline={"status": "failed", "web_url": "u"})))
assert lifestage.now(fix, "idle", block={"reason": "pipeline"}) == fix  # already mine: the lifecycle's own move stands
(greenlight.STATE / "blocks.json").write_text(json.dumps({"ts": __import__("time").time(), "blocks": {"acme/widgets!7": {"reason": "approval", "why": "x"}}}))
assert lifestage.block_of(row(mr()), lifestage.blocks())["reason"] == "approval" and not lifestage.block_of(row(mr(state="merged")), lifestage.blocks())
(greenlight.STATE / "blocks.json").write_text(json.dumps({"ts": 1, "blocks": {"acme/widgets!7": {"reason": "approval"}}}))
assert lifestage.blocks() == {}  # a stale roster pins nothing
# an old snapshot row, saved after the overlay turned its owner into "working" or "me", still lands in the right place
old = row(mr(has_conflicts=True)); old["next"]["owner"] = "working"; del old["life"]
assert lifestage.of(old | {"agent": {"state": "working"}})["pile"] == "working" and lifestage.of(old)["at"] == "3.3"

# every sub-stage reported is one the model has, and the numbers follow the model
keys = {(s, k) for s, _, subs in lifestage.STAGES for k, _ in subs}
assert lifestage.at("review", "respond") == {"stage": "review", "stage_name": "Review", "sub": "respond", "sub_name": "Respond", "at": "4.3"}
assert lifestage.at(None, None)["at"] is None and len(lifestage.STAGES) == 5 and ("land", "merged") in keys

# scan(): a branch whose remote branch was deleted is done; one never pushed, or still there, is not
repo = tempfile.mkdtemp()
git = lambda *a, cwd=repo: subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, text=True)
origin = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
git("config", "user.name", "t"); git("config", "user.email", "t@example.invalid")
git("commit", "-q", "--allow-empty", "-m", "one"); git("remote", "add", "origin", origin); git("push", "-q", "-u", "origin", "main")
git("switch", "-q", "-c", "feature"); git("push", "-q", "-u", "origin", "feature"); git("branch", "-q", "local-only")
gone = lambda b: greenlight.upstream_gone({"path": repo, "branch": b})
assert not gone("feature") and not gone("local-only") and not gone("main")
git("push", "-q", "origin", "--delete", "feature")  # the push prunes the remote-tracking ref, as a fetch --prune does
assert gone("feature") and not gone("local-only") and not gone("main")
assert not greenlight.upstream_gone({"path": "/nonexistent", "branch": "feature"})

# 1.3 Done condition: preflight asks decide.py before a new workspace's agent starts (a fake decide here: never a network call)
class Decide:
    def __init__(self, p, clarity=0.8, value=None, fail=None, provider="fake"):
        self.p, self.clarity, self.value, self.fail, self.provider, self.calls = p, clarity, value, fail, provider, []
    def bool(self, state, question, *, context=""):
        self.calls.append(("bool", state, context))
        if self.fail:
            raise self.fail
        return {"value": self.p > 0.5 if self.value is None else self.value, "prob": self.p, "provider": self.provider}
    def score(self, state, rubric, *, context=""):
        self.calls.append(("score", state, context))
        return {"score": self.clarity, "provider": self.provider}

pf = lifestage.preflight
clear = pf("Add exponential retry backoff to the outbound queue; done when test_queue passes", decide=(d := Decide(0.1)))
assert not clear["pause"] and clear["questions"] == [] and clear["record"]["ok"] is True and clear["brief"] == ""
assert isinstance(d.calls[0][1], str) and "Goal: Add exponential" in d.calls[0][1] and [c[0] for c in d.calls] == ["bool", "score"]
assert not pf("x", decide=Decide(0.70))["pause"]  # the line itself is clear: only over 0.70 pauses
amb = pf("make it better", decide=Decide(0.9, clarity=0.2))
assert amb["pause"] and amb["record"] is None and amb["p_ambiguous"] == 0.9 and amb["clarity"] == 0.2
assert [q["id"] for q in amb["questions"]] == ["done", "scope"] and all(2 <= len(q["options"]) <= 4 for q in amb["questions"])
assert amb["questions"][0]["q"] == "What does done look like?"
assert [q["id"] for q in pf("Rework how the scheduler decides which of the queued jobs runs first", decide=Decide(0.8, clarity=0.3))["questions"]] == ["done"]
assert [q["id"] for q in pf("Rework how the scheduler decides which of the queued jobs runs first", decide=Decide(0.8, clarity=0.9))["questions"]] == ["scope"]
# prob as confidence in `value`: "not ambiguous, 90% sure" is P(ambiguous) 0.1
assert not pf("make it better", decide=Decide(0.9, value=False))["pause"]
# answered: never asked again, the answers reach the agent and the record; unknown ids and blanks are dropped
ans = pf("make it better", answers={"done": "A draft PR with tests", "scope": "  ", "evil": "x"}, decide=(d := Decide(0.95, clarity=0.1)))
assert not ans["pause"] and ans["brief"] == "\n\nBefore you started I answered these:\n- What does done look like? A draft PR with tests"
assert ans["record"]["answers"] == {"What does done look like?": "A draft PR with tests"} and ans["record"]["ok"] is True
assert "Answers to clarifying questions:\nWhat does done look like? A draft PR with tests" in d.calls[0][1]
forced = pf("make it better", force=True, decide=(d := Decide(0.95)))
assert not forced["pause"] and forced["record"]["forced"] and forced["record"]["ok"] is False and not d.calls
# decide.py missing (until system-one-decide lands) or failing never blocks a start, and says why
down = pf("make it better", decide=Decide(0.9, fail=TimeoutError("jev timed out")))
assert not down["pause"] and down["record"]["ok"] is None and "TimeoutError: jev timed out" in down["why"]
local = pf("make it better", decide=Decide(0.5, clarity=0.5, provider="local"))
assert not local["pause"] and local["record"]["ok"] is None and "no System One provider" in local["why"]
if not (greenlight.ROOT / "scripts/decide.py").exists():
    assert "ModuleNotFoundError" in pf("make it better")["why"]

# checks.done_condition: the verdict saved at launch, while the goal is the one judged
lifestage.save("preflight", "w1", clear["record"] | {"goal": "add retries"})
assert lifestage.place(row(None, goal="add retries"))["checks"]["done_condition"]["ok"] is True
assert lifestage.place(row(None, goal="add retries, and a dashboard"))["checks"]["done_condition"] is None  # goal changed since

# 2.3 Self-test: `lifestage.py selftest -- cmd` in the worktree; no MR + passed on what HEAD holds = 2.4 Propose
wt = tempfile.mkdtemp()
g = lambda *a: subprocess.run(["git", "-C", wt, *a], check=True, capture_output=True, text=True).stdout.strip()
subprocess.run(["git", "init", "-q", "-b", "main", wt], check=True)
g("config", "user.name", "t"); g("config", "user.email", "t@example.invalid")
Path = __import__("pathlib").Path
(Path(wt) / "a.py").write_text("x = 1\n"); g("add", "-A"); g("commit", "-qm", "one")
st = lambda *cmd: subprocess.run([sys.executable, lifestage.__file__, "selftest", "--", *cmd], cwd=wt, capture_output=True, text=True)
ws = lambda **kw: row(None, goal="add retries", path=wt, **kw)
assert life(None, goal="add retries", path=wt)[0] == "2.2" and ws()["life"]["checks"]["self_test"] is None  # nothing on record yet
out = st(sys.executable, "-c", "print('3 passed')")
assert out.returncode == 0 and "3 passed" in out.stdout and "exit 0 recorded" in out.stderr, out
w = ws()["life"]
assert (w["at"], w["sub_name"], w["suggestion"]["label"]) == ("2.4", "Propose", "Open the draft PR") and w["checks"]["self_test"]["fresh"]
assert w["do"] == f"Its self-test passed on {g('rev-parse', 'HEAD')[:7]} (`{sys.executable} -c print('3 passed')`). Start an agent to push it and open the draft PR."
assert ws(agent="working")["life"]["do"].endswith("(`" + sys.executable + " -c print('3 passed')`). Your agent is working on it now.")
(Path(wt) / "a.py").write_text("x = 2\n")  # an edit not yet committed: HEAD, what a PR would carry, is still the tested tree
assert life(None, goal="add retries", path=wt)[0] == "2.4"
g("commit", "-qam", "two")  # committed: HEAD changed since, back to Self-test
assert life(None, goal="add retries", path=wt)[::3] == ("2.3", "Run the self-test again")
assert st(sys.executable, "-c", "raise SystemExit(3)").returncode == 3  # its exit code is the command's
assert life(None, goal="add retries", path=wt)[::3] == ("2.3", "Fix the self-test") and "exit 3" in ws()["life"]["do"]
# tested before committing: the tree it tested is the tree HEAD holds after the commit, so it counts
(Path(wt) / "b.py").write_text("y = 1\n"); st("true")
assert life(None, goal="add retries", path=wt)[0] == "2.3"  # untracked b.py isn't in HEAD yet
g("add", "-A"); g("commit", "-qm", "three")
assert life(None, goal="add retries", path=wt)[0] == "2.4"
assert "a.py" in g("status", "--porcelain", "--untracked-files=all") or not g("status", "--porcelain")  # the real index is untouched
# undrafting: greenlight's Mark it ready says when the self-test on record is failed or stale; no record, no note
dr = lambda **kw: row(mr(draft=True), path=wt, **kw)["life"]["do"]
assert dr() == "!7 is still a draft. Greenlight marks it ready on its next run."
(Path(wt) / "a.py").write_text("x = 3\n"); g("commit", "-qam", "four")
assert dr().endswith("Its last self-test is older than its head commit (`true`).")
assert row(mr(draft=True), path="/nonexistent")["life"]["do"] == "!7 is still a draft. Greenlight marks it ready on its next run."
assert "selftest" in subprocess.run([sys.executable, lifestage.__file__, "selftest"], capture_output=True, text=True).stderr
# no commit yet: recorded, but it counts for nothing
empty = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", empty], check=True)
o = subprocess.run([sys.executable, lifestage.__file__, "selftest", "true"], cwd=empty, capture_output=True, text=True)
assert o.returncode == 0 and "counts for nothing" in o.stderr and lifestage.self_test({"path": empty})["fresh"] is False

# dash.py: New task and New workspace pause on an ambiguous goal before anything is made, and launch on a clear one
import dash
from unittest import mock
REPO = [{"id": "r1", "name": "widgets", "host": "github", "path": "acme/widgets"}]
def task(payload, decide):
    calls = []
    def orca(*a):
        calls.append(a)
        return {"worktree": {"id": "w9", "path": "/w/" + a[a.index("--name") + 1]}} if a[:2] == ("worktree", "create") else {}
    with mock.patch.object(dash, "repos", return_value=REPO), mock.patch.object(dash, "live_agents", return_value={}), \
            mock.patch.object(dash.greenlight, "orca", side_effect=orca), mock.patch.dict(sys.modules, {"decide": decide}):
        return dash.task("new", "widgets", json.dumps(payload)), calls
out, calls = task({"text": "make it better"}, Decide(0.9, clarity=0.2))
assert out["paused"] and [q["id"] for q in out["questions"]] == ["done", "scope"] and out["p_ambiguous"] == 0.9 and not calls, out
assert not (dash.TASKS / "make-it-better").exists()  # a dismissed question leaves nothing behind
out, calls = task({"text": "make it better", "answers": {"done": "A working prototype I can try", "scope": "The smallest change that works"}},
                  Decide(0.9, clarity=0.2))
assert out["ok"] and out["name"] == "make-it-better", out
brief = next(a for a in calls if a[:2] == ("worktree", "create"))
brief = brief[brief.index("--prompt") + 1]
assert "- What does done look like? A working prototype I can try\n- How far should it go? The smallest change that works" in brief
assert "gate push --execute" in brief and "gate pr <metadata.json> --execute" in brief
assert "lifestage.py selftest -- <the test command>" in brief
assert "Follow my request and clarifying answers" in brief
rec = lifestage.record("preflight", {"name": "make-it-better"})
assert rec["ok"] and rec["goal"] == "make it better" and len(rec["answers"]) == 2
assert lifestage.checks({"name": "renamed", "path": "/w/make-it-better", "goal": "make it better"})["done_condition"]["ok"]
assert "[preflight: answered]" in greenlight.history_file("make-it-better").read_text()
out, calls = task({"text": "make a plan", "answers": {"done": "A written plan first, no code"}}, Decide(0.9, clarity=0.2))
brief = next(a for a in calls if a[:2] == ("worktree", "create"))
brief = brief[brief.index("--prompt") + 1]
assert "If they ask for a plan or investigation first, stop at that deliverable" in brief and "- What does done look like? A written plan first, no code" in brief
assert "gate push --execute" in brief and "gate pr <metadata.json> --execute" in brief
out, calls = task({"text": "Add retry backoff to the outbound queue, done when test_queue passes"}, Decide(0.05))
assert out["ok"] and any(a[:2] == ("worktree", "create") for a in calls)  # clear: launched at once
out, calls = task({"text": "make it worse", "force": True}, Decide(0.99))
assert out["ok"] and lifestage.record("preflight", {"name": "make-it-worse"})["forced"]
# New workspace: asked only when an agent would start on a goal
tcalls = []
def tree_call(*a):
    tcalls.append(a)
    return {"worktree": {"path": "/w/new", "worktreeId": "t1"}} if a[:2] == ("worktree", "create") else {}
with mock.patch.object(dash, "tree", return_value=(tree_call, {"repoId": "r"})), mock.patch.object(dash, "repo_cfg", return_value={}), \
        mock.patch.dict(sys.modules, {"decide": Decide(0.9, clarity=0.2)}):
    p = dash.tree_new("/w/repo", {"name": "better", "goal": "make it better", "agent": True})
    assert p["paused"] and len(p["questions"]) == 2 and not tcalls
    assert dash.tree_new("/w/repo", {"name": "better", "goal": "make it better", "agent": False})["ok"] and tcalls  # no agent: nothing to gate
    tcalls.clear()
    assert dash.tree_new("/w/repo", {"name": "better", "goal": "make it better", "agent": True, "answers": {"scope": "Investigate and report back first"}})["ok"]
    launch = next(a for a in tcalls if a[:2] == ("agent", "launch"))
    assert launch[launch.index("--prompt") + 1].startswith("make it better\n\nBefore you started I answered these:\n- How far should it go? Investigate and report back first")
    assert "lifestage.py selftest -- <the test command>" in launch[launch.index("--prompt") + 1]
    assert lifestage.record("preflight", {"path": "/w/new"})["answers"]

# lifestage.py: the piles, from the snapshot; rows from before this file get placed on the way out
snap = [row(mr(_discussions=[thread("alice", "alice")])) | {"id": "a", "name": "alpha"},
        row(mr(_approvals_left=1)) | {"id": "b", "name": "beta"}, row(mr(state="merged")) | {"id": "c", "name": "gamma"}]
del snap[1]["life"]
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": snap}))
run = lambda *a: subprocess.run([sys.executable, lifestage.__file__, *a], capture_output=True, text=True, check=True).stdout
out = run()
assert out.index("YOURS (1)") < out.index("WAITING FOR OTHERS (1)") < out.index("DONE (1)"), out
assert "4.3 Review › Respond" in out and "Resolve 1 thread" in out and "4.5 Review › Approved" in out and "5.3 Land › Merged" in out, out
one = json.loads(run("beta", "--json"))
assert len(one) == 1 and (one[0]["workspace"], one[0]["pile"], one[0]["blocker"], one[0]["mr"]) == ("beta", "waiting", "reviewers", "!7")
assert "no scanned workspace named nope" in run("nope")
snap = [row(None, goal="add retries", path=wt) | {"id": "d", "name": "delta"}]; del snap[0]["life"]
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": snap}))
lifestage.save("preflight", "delta", {"goal": "add retries", "ok": True, "p_ambiguous": 0.1, "clarity": 0.9, "provider": "fake", "answers": {}})
out = run("delta")
assert "1.3 done condition: P(ambiguous) 0.1, clarity 0.9 (fake)" in out and "2.3 self-test: `true` exit 0" in out and "stale" in out, out

# custom stages respect explicit Owner: and Phase: instead of hardcoding 3.4 Verify / agent
(greenlight.STATE / "lifecycles/custom-phases.md").write_text(
    "# custom\n\n1. `rfc` — RFC approved. Done: `signoff` Phase: `define`\n2. `mr_open` — x\n"
    "3. `qa` — QA passed. Done: `label=qa-ok` Owner: `reviewers` Phase: `review`\n"
    "4. `e2e` — E2E green. Done: `check=e2e` Phase: `verify`\n5. `merge` — x\n"
    "6. `release_qa` — Prod QA. Done: `label=prod-ok` Owner: `reviewers`\n")
assert life(None, goal="new feature", project="acme/custom-phases") == ("1.2", "yours", "you", "Sign off rfc")
(greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/custom-phases@b": ["rfc"]}))
assert life(mr(labels=[]), project="acme/custom-phases") == ("4.5", "waiting", "reviewers", None)
assert life(mr(labels=["qa-ok"], _checks=[{"name": "e2e", "status": "running"}]), project="acme/custom-phases") == ("3.4", "waiting", "CI", None)
assert life(mr(state="merged", labels=[]), project="acme/custom-phases") == ("5.3", "waiting", "reviewers", None)
# auto_rebase places as agent-owned mergeable (3.3), and Owner: me on label/check uses "Complete", while Owner: reviewers on signoff waits on reviewers
(greenlight.STATE / "lifecycles/auto-reb.md").write_text(
    "# auto-reb\n\nOptions: `auto_rebase`\n\n1. `mr_open` — x\n2. `me_lbl` — Add it. Done: `label=ok` Owner: `me`\n"
    "3. `rev_so` — Reviewer signoff. Done: `signoff` Owner: `reviewers`\n4. `merge` — x\n")
assert life(mr(labels=[]), project="acme/auto-reb") == ("3.4", "yours", "you", "Complete me lbl")
assert life(mr(labels=["ok"]), project="acme/auto-reb") == ("3.4", "waiting", "reviewers", None)
(greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/auto-reb#7": ["rev_so"]}))
assert life(mr(labels=["ok"], detailed_merge_status="need_rebase"), project="acme/auto-reb") == ("3.3", "yours", "your agent", "Rebase !7")
print("test_lifestage ok")
