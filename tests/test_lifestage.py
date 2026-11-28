"""Tests for scripts/lifestage.py: lifecycle placement, holders, moves, preflight, selftest, and CLI piles."""
import json
import shutil
import subprocess
import sys
import time
from unittest import mock

import pytest

import audit
import dash
import greenlight
import lifestage


def _u(name):
    return {"username": name}


def _thread(who, last):
    return {"notes": [{"resolvable": True, "resolved": False, "author": _u(who)}, {"author": _u(last)}]}


def _mr(**kw):
    base = {
        "iid": 7,
        "state": "opened",
        "author": _u(greenlight.ME),
        "draft": False,
        "has_conflicts": False,
        "assignees": [_u(greenlight.BOT)],
        "reviewers": [_u(greenlight.BOT)],
        "milestone": {"title": "R"},
        "head_pipeline": {"status": "success", "web_url": "u"},
        "blocking_discussions_resolved": True,
        "detailed_merge_status": "mergeable",
        "_discussions": [],
        "_approved_by": ["alice"],
        "sha": "abc",
        "target_branch": "main",
        "_default_branch": "main",
    }
    return base | kw


def _row(m, goal="", agent="idle", project="acme/widgets", branch="b", **ws):
    """A scan row the way scan() builds it: the MR's summary, greenlight's next step, then its life."""
    r = {"id": "w1", "name": "w1", "project": project, "goal": goal, "branch": branch, "agent": {"state": agent}} | ws
    r["mr"] = m and {
        "iid": m["iid"],
        "ref": f"!{m['iid']}",
        "state": m["state"],
        "role": "author" if m["author"]["username"] == greenlight.ME else "review",
        "threads": len(greenlight.unresolved(m)),
        "approvals": len(m.get("_approved_by", [])),
        "todo_threads": [1 for d in greenlight.unresolved(m) if greenlight.awaiting_me(d)],
        "threads_total": len(m.get("_discussions", [])),
        "rebase": m.get("detailed_merge_status") == "need_rebase",
    }
    r["next"] = greenlight.evaluate(r, m, {"title": "Release-9.9.9"})
    r["life"] = lifestage.now(lifestage.place(r), agent)
    return r


def _life(m, **kw):
    lf = _row(m, **kw)["life"]
    return lf["at"], lf["pile"], lf["holder"], (lf["suggestion"] or {}).get("label")


def _do(m, **kw):
    return _row(m, **kw)["life"]["do"]


class FakeDecide:
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


@pytest.fixture()
def lifestage_env(gl_home, monkeypatch):
    monkeypatch.setattr(audit, "merge_verdict", lambda ws, mr: {"ok": True, "failed": [], "target_oid": "base1"})
    monkeypatch.setattr(greenlight, "BOT", "review-bot")
    (greenlight.STATE / "lifecycles").mkdir()
    shutil.copy(greenlight.ROOT / "examples/lifecycles/release-train.md", greenlight.STATE / "lifecycles/widgets.md")
    (greenlight.STATE / "lifecycles/gadgets.md").write_text(
        "# gadgets\n\nOptions: `solo`\n\n1. `mr_open` — x\n2. `ci_green` — x\n3. `qa` — QA passed. Done: `label=qa-ok`\n"
        "4. `legal` — Legal looked.\n5. `merge` — x\n"
    )
    (greenlight.STATE / "lifecycles/gizmos.md").write_text(
        "# gizmos\n\n1. `mr_open` — x\n2. `merge` — x\n3. `deploy` — Prod runs it. Done: `signoff`\n4. `smoke` — Smoke passed. Done: `label=smoke-ok`\n"
    )
    return gl_home


def test_define_build_verify_review_land_and_done(lifestage_env):
    # Define and Build: no MR yet
    assert _life(None) == ("1.1", "yours", "you", "Set a goal")
    assert _life(None, branch="auto-nightly") == ("1.1", "yours", "you", "Delete it or set a goal")
    assert _life(None, goal="add retries") == ("2.2", "yours", "your agent", "Work toward the goal")
    assert _life(None, goal="add retries", agent="working") == ("2.2", "working", "your agent", "Work toward the goal")
    assert _life(None, goal="nightly sweep (automation)") == ("2.2", "working", "an automation", None)

    # Verify
    assert _life(_mr(head_pipeline={"status": "running", "web_url": "u"})) == ("3.1", "waiting", "CI", None)
    assert _life(_mr(head_pipeline={"status": "failed", "web_url": "u"})) == ("3.2", "yours", "your agent", "Fix CI")
    assert _life(_mr(head_pipeline={"status": "failed", "web_url": "u"}), agent="working")[:2] == ("3.2", "working")
    assert _life(_mr(has_conflicts=True)) == ("3.3", "yours", "your agent", "Fix conflicts")
    assert _life(_mr(detailed_merge_status="need_rebase")) == ("3.3", "yours", "you", "Rebase !7?")
    assert _life(_mr(head_pipeline={"status": "failed", "web_url": "u"}, _discussions=[_thread("alice", "alice")]))[0] == "3.2"

    # Review: the chores open it, then the loop
    assert _life(_mr(assignees=[])) == ("4.1", "working", "Greenlight", "Assign the review bot")
    assert _life(_mr(milestone=None)) == ("4.1", "working", "Greenlight", "Set the milestone")
    r = _row(_mr(milestone=None))
    r["next"] = greenlight.evaluate(r, _mr(milestone=None), None)
    assert lifestage.place(r)["suggestion"] == {"label": "Pick a milestone", "kind": "you"} and lifestage.place(r)["pile"] == "yours"
    assert _life(_mr(draft=True)) == ("4.1", "working", "Greenlight", "Mark it ready")
    assert _life(_mr(draft=True), goal="x (keep draft)") == ("4.1", "yours", "you", "Take it out of draft?")
    assert _life(_mr(_discussions=[_thread("alice", "alice"), _thread("bob", "bob")])) == ("4.3", "yours", "your agent", "Resolve 2 threads")
    assert _life(_mr(_discussions=[_thread("alice", "alice"), _thread("bob", "octo")]))[3] == "Resolve 1 thread"
    assert _life(_mr(_discussions=[_thread("alice", "octo")])) == ("4.4", "waiting", "reviewers", None)
    assert _life(_mr(_approvals_left=2, _approved_by=[])) == ("4.2", "waiting", "reviewers", None)
    assert _life(_mr(_approvals_left=1)) == ("4.5", "waiting", "reviewers", None)
    assert _life(_mr(_approved_by=[], _approvals_required=1)) == ("4.5", "waiting", "reviewers", None)  # required, met by nobody human
    # a repo that requires no approvals: nobody to wait on, and nothing says "approval"
    nb = _row(_mr(_approved_by=[]))["life"]
    assert (nb["at"], nb["pile"], nb["holder"], nb["suggestion"]["label"]) == ("5.1", "yours", "you", "Merge !7?")
    assert "approv" not in nb["do"] and "review" not in nb["do"], nb

    # Land
    assert _life(_mr()) == ("5.1", "working", "Greenlight", "Merge !7")
    assert _life(_mr(), goal="x (hold)") == ("5.1", "yours", "you", "Merge now or keep holding?")
    assert _life(_mr(detailed_merge_status="ci_still_running")) == ("5.1", "waiting", "CI", None)
    assert _life(_mr(detailed_merge_status="merge_request_blocked", _blocked_by=[3])) == ("5.1", "waiting", "another MR", None)
    assert _life(_mr(detailed_merge_status="blocked_status")) == ("5.1", "yours", "you", "See why it can't merge")
    assert _life(_mr(_approved_by=[]), project="acme/gadgets")[3] == "Meet qa"

    # the repo's own steps: a label the agent earns, a sign-off that's mine
    assert _life(_mr(), project="acme/gadgets") == ("3.4", "yours", "your agent", "Meet qa")
    assert _life(_mr(labels=["qa-ok"]), project="acme/gadgets") == ("3.4", "yours", "you", "Sign off legal")
    (greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/gadgets#7": ["legal"]}))
    assert _life(_mr(labels=["qa-ok"], _approved_by=[]), project="acme/gadgets") == ("5.1", "yours", "you", "Review it, then merge")

    # done: merged, closed, approved by me, or the branch is gone from the remote
    assert _life(_mr(state="merged")) == ("5.3", "done", None, None)
    assert _life(_mr(state="closed")) == (None, "done", None, None)
    assert _life(None, goal="add retries", branch_gone=True) == (None, "done", None, None)
    assert _life(_mr(author=_u("alice"), _approved_by=["octo"]))[1] == "done"

    # after the merge: the lifecycle's own steps keep it at Land › Merged until they're met
    assert _life(_mr(state="merged"), project="acme/gizmos") == ("5.3", "yours", "you", "Sign off deploy")
    assert _do(_mr(state="merged"), project="acme/gizmos") == "!7 is merged, and deploy comes after the merge. Deploy: Prod runs it. Confirm it is met to sign it off."
    (greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/gizmos#7": ["deploy"]}))
    assert _life(_mr(state="merged"), project="acme/gizmos") == ("5.3", "yours", "your agent", "Meet smoke")
    assert _life(_mr(state="merged"), project="acme/gizmos", agent="working")[:2] == ("5.3", "working")
    assert _life(_mr(state="merged", labels=["smoke-ok"]), project="acme/gizmos") == ("5.3", "done", None, None)

    r = _row(_mr(state="merged"), project="acme/gizmos")
    r["next"] = {"step": "deploy", "owner": "me", "action": "merged from Greenlight; next: deploy"}
    assert (lifestage.place(r)["at"], lifestage.place(r)["suggestion"]["label"], lifestage.place(r)["do"]) == (
        "5.3",
        "Sign off deploy",
        "!7 is merged, and deploy comes after the merge. Do it, then sign it off.",
    )

    # someone else's MR: I only hold it while the review is mine
    theirs = lambda **kw: _mr(author=_u("alice"), _approved_by=[], **kw)
    assert _life(theirs()) == ("4.2", "yours", "you", "Review !7")
    assert _life(theirs(_discussions=[_thread("octo", "alice")])) == ("4.4", "yours", "you", "Re-review !7")
    assert _life(theirs(_discussions=[_thread("octo", "octo")])) == ("4.3", "waiting", "the author", None)
    assert _life(theirs(_can_approve=False)) == ("4.5", "waiting", "reviewers", None)


def test_builtin_verify_step_places_in_verify_author_check(lifestage_env):
    (greenlight.STATE / "lifecycles/checked.md").write_text("# checked\n\n1. `mr_open` — x\n2. `verified` — x\n3. `merge` — x\n")
    life = lambda m: _life(m, project="acme/checked")
    assert life(_mr()) == ("3.4", "yours", "your agent", "Verify !7")  # no receipt: a fresh verifier
    greenlight.verify_record("acme/checked", 7, "fail", sha="abc", summary="cancel kept state")
    assert life(_mr()) == ("3.4", "yours", "your agent", "Fix the defects")  # the builder's turn
    assert life(_mr(sha="def")) == ("3.4", "yours", "your agent", "Verify !7")  # the fix landed: the verifier's again
    greenlight.verify_record("acme/checked", 7, "uncertain", sha="def", summary="needs a prod key")
    r = _row(_mr(sha="def"), project="acme/checked")
    assert (r["life"]["at"], r["life"]["suggestion"]["label"]) == ("3.4", "Look at the verification") and "needs a prod key" in r["life"]["do"]
    greenlight.verify_record("acme/checked", 7, "pass", sha="def")
    assert life(_mr(sha="def"))[0] == "5.1"  # passed: on to the merge gate


def test_do_descriptions_and_parked_paths(lifestage_env):
    theirs = lambda **kw: _mr(author=_u("alice"), _approved_by=[], **kw)
    assert _do(_mr(), goal="x (hold)") == "!7 is ready to merge, but your goal says hold. Merge it now, or leave the hold in place."
    assert _do(theirs(_discussions=[_thread("octo", "alice")])) == "The author replied on your threads on !7. Read the replies, then resolve each thread or answer it."
    assert _do(_mr(_discussions=[_thread("alice", "alice")])) == "1 thread on !7 needs a fix or a reply. Start an agent to work through it."
    assert _do(_mr(_discussions=[_thread("alice", "alice")]), agent="working") == "1 thread on !7 needs a fix or a reply. Your agent is working on it now."
    assert _do(_mr(_discussions=[_thread("alice", "alice")]), agent="waiting") == "Your agent asked you a question and has stopped until you answer it."
    assert _do(_mr(_approvals_left=1)) == "Waiting for approvals on !7."
    assert _do(_mr(state="merged")) == "!7 is merged. The workspace can be deleted."
    assert _do(_mr(detailed_merge_status="blocked_status")) == "The forge won't merge !7 (GitHub blocks the merge (branch protection): check the PR). Open it and clear what blocks it."
    assert _do(_mr(detailed_merge_status="merge_request_blocked", _blocked_by=[3])) == "!7 is waiting on !3 to merge first."
    assert _do(None) == "There is no MR and no goal. Say what this workspace is for, or delete it."
    assert _do(_mr(iid=8, labels=["qa-ok"]), project="acme/gadgets") == "Legal: Legal looked. Confirm it is met to sign it off."
    assert all(_row(m)["life"]["do"] for m in (_mr(), None, theirs(), _mr(draft=True), _mr(milestone=None), _mr(has_conflicts=True)))

    # off the path
    assert _life(_mr(), goal="x (parked)")[:2] == (None, "parked")
    assert _life(_mr(), goal="x (revisit 2999-01-01)")[:2] == (None, "parked")
    assert _life(None, goal="Plan: the retry work")[:2] == ("1.4", "parked")


def test_agent_now_overlay_blocks_and_stage_model(lifestage_env):
    base = lifestage.place(_row(_mr(_discussions=[_thread("alice", "octo")])))
    asked = lifestage.now(base, "waiting")
    assert (asked["pile"], asked["holder"], asked["suggestion"]["label"], asked["at"]) == ("yours", "you", "Answer your agent", "4.4")
    assert lifestage.now(asked, "waiting") == asked
    back = lifestage.now(asked, "idle")
    assert (back["pile"], back["holder"], back["suggestion"]) == ("waiting", "reviewers", None) and "_was" not in back

    ci = lifestage.place(_row(_mr(head_pipeline={"status": "failed", "web_url": "u"})))
    assert lifestage.now(ci, "idle", logged_out=True)["suggestion"]["label"] == "Log your agent back in"
    assert lifestage.now(lifestage.now(ci, "working"), "idle")["pile"] == "yours"

    # roster says a thread is waiting on me
    answered = lifestage.place(_row(_mr(_discussions=[_thread("alice", "octo")])))
    mine = lifestage.now(answered, "idle", block={"reason": "discussion", "why": "2 threads are waiting on you"})
    assert (mine["at"], mine["pile"], mine["holder"], mine["blocker"], mine["suggestion"]["label"]) == ("4.3", "yours", "you", "author", "Reply in the thread")
    assert mine["why"] == "you're blocking: 2 threads are waiting on you" and lifestage.now(mine, "idle") == answered
    assert mine["do"] == "Someone is waiting on you here: 2 threads are waiting on you. Reply in the thread."
    assert lifestage.now(answered, "waiting", block={"reason": "discussion"})["suggestion"]["label"] == "Answer your agent"
    assert lifestage.now(lifestage.place(_row(_mr(state="merged"))), "idle", block={"reason": "discussion"})["pile"] == "done"
    fix = lifestage.place(_row(_mr(head_pipeline={"status": "failed", "web_url": "u"})))
    assert lifestage.now(fix, "idle", block={"reason": "pipeline"}) == fix

    (greenlight.STATE / "blocks.json").write_text(json.dumps({"ts": time.time(), "blocks": {"acme/widgets!7": {"reason": "approval", "why": "x"}}}))
    assert lifestage.block_of(_row(_mr()), lifestage.blocks())["reason"] == "approval"
    assert not lifestage.block_of(_row(_mr(state="merged")), lifestage.blocks())
    (greenlight.STATE / "blocks.json").write_text(json.dumps({"ts": 1, "blocks": {"acme/widgets!7": {"reason": "approval"}}}))
    assert lifestage.blocks() == {}

    old = _row(_mr(has_conflicts=True))
    old["next"]["owner"] = "working"
    del old["life"]
    assert lifestage.of(old | {"agent": {"state": "working"}})["pile"] == "working" and lifestage.of(old)["at"] == "3.3"

    keys = {(s, k) for s, _, subs in lifestage.STAGES for k, _ in subs}
    assert lifestage.at("review", "respond") == {"stage": "review", "stage_name": "Review", "sub": "respond", "sub_name": "Respond", "at": "4.3"}
    assert lifestage.at(None, None)["at"] is None and len(lifestage.STAGES) == 5 and ("land", "merged") in keys


def test_upstream_gone_on_real_git_repo(lifestage_env, tmp_path):
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "-u", "origin", "main")
    git("switch", "-q", "-c", "feature")
    git("push", "-q", "-u", "origin", "feature")
    git("branch", "-q", "local-only")

    gone = lambda b: greenlight.upstream_gone({"path": str(repo), "branch": b})
    assert not gone("feature") and not gone("local-only") and not gone("main")
    git("push", "-q", "origin", "--delete", "feature")
    assert gone("feature") and not gone("local-only") and not gone("main")
    assert not greenlight.upstream_gone({"path": "/nonexistent", "branch": "feature"})


def test_preflight_done_condition_and_checks(lifestage_env):
    pf = lifestage.preflight
    d = FakeDecide(0.1)
    clear = pf("Add exponential retry backoff to the outbound queue; done when test_queue passes", decide=d)
    assert not clear["pause"] and clear["questions"] == [] and clear["record"]["ok"] is True and clear["brief"] == ""
    assert isinstance(d.calls[0][1], str) and "Goal: Add exponential" in d.calls[0][1] and [c[0] for c in d.calls] == ["bool", "score"]
    assert not pf("x", decide=FakeDecide(0.70))["pause"]

    amb = pf("make it better", decide=FakeDecide(0.9, clarity=0.2))
    assert amb["pause"] and amb["record"] is None and amb["p_ambiguous"] == 0.9 and amb["clarity"] == 0.2
    assert [q["id"] for q in amb["questions"]] == ["done", "scope"] and all(2 <= len(q["options"]) <= 4 for q in amb["questions"])
    assert amb["questions"][0]["q"] == "What does done look like?"
    assert [q["id"] for q in pf("Rework how the scheduler decides which of the queued jobs runs first", decide=FakeDecide(0.8, clarity=0.3))["questions"]] == ["done"]
    assert [q["id"] for q in pf("Rework how the scheduler decides which of the queued jobs runs first", decide=FakeDecide(0.8, clarity=0.9))["questions"]] == ["scope"]
    assert not pf("make it better", decide=FakeDecide(0.9, value=False))["pause"]

    d = FakeDecide(0.95, clarity=0.1)
    ans = pf("make it better", answers={"done": "A draft PR with tests", "scope": "  ", "evil": "x"}, decide=d)
    assert not ans["pause"] and ans["brief"] == "\n\nBefore you started I answered these:\n- What does done look like? A draft PR with tests"
    assert ans["record"]["answers"] == {"What does done look like?": "A draft PR with tests"} and ans["record"]["ok"] is True
    assert "Answers to clarifying questions:\nWhat does done look like? A draft PR with tests" in d.calls[0][1]

    d = FakeDecide(0.95)
    forced = pf("make it better", force=True, decide=d)
    assert not forced["pause"] and forced["record"]["forced"] and forced["record"]["ok"] is False and not d.calls

    down = pf("make it better", decide=FakeDecide(0.9, fail=TimeoutError("jev timed out")))
    assert not down["pause"] and down["record"]["ok"] is None and "TimeoutError: jev timed out" in down["why"]
    local = pf("make it better", decide=FakeDecide(0.5, clarity=0.5, provider="local"))
    assert not local["pause"] and local["record"]["ok"] is None and "no System One provider" in local["why"]
    if not (greenlight.ROOT / "scripts/decide.py").exists():
        assert "ModuleNotFoundError" in pf("make it better")["why"]

    lifestage.save("preflight", "w1", clear["record"] | {"goal": "add retries"})
    assert lifestage.place(_row(None, goal="add retries"))["checks"]["done_condition"]["ok"] is True
    assert lifestage.place(_row(None, goal="add retries, and a dashboard"))["checks"]["done_condition"] is None


def test_selftest_cli_freshness_and_undrafting(lifestage_env, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True).stdout.strip()
    g("config", "user.name", "t")
    g("config", "user.email", "t@t")
    (wt / "a.py").write_text("x = 1\n")
    g("add", "-A")
    g("commit", "-qm", "one")

    st = lambda *cmd: subprocess.run([sys.executable, lifestage.__file__, "selftest", "--", *cmd], cwd=wt, capture_output=True, text=True)
    ws = lambda **kw: _row(None, goal="add retries", path=str(wt), **kw)
    assert _life(None, goal="add retries", path=str(wt))[0] == "2.2" and ws()["life"]["checks"]["self_test"] is None

    out = st(sys.executable, "-c", "print('3 passed')")
    assert out.returncode == 0 and "3 passed" in out.stdout and "exit 0 recorded" in out.stderr, out
    w = ws()["life"]
    assert (w["at"], w["sub_name"], w["suggestion"]["label"]) == ("2.4", "Propose", "Open the draft PR") and w["checks"]["self_test"]["fresh"]
    assert w["do"] == f"Its self-test passed on {g('rev-parse', 'HEAD')[:7]} (`{sys.executable} -c print('3 passed')`). Start an agent to push it and open the draft PR."
    assert ws(agent="working")["life"]["do"].endswith("(`" + sys.executable + " -c print('3 passed')`). Your agent is working on it now.")

    (wt / "a.py").write_text("x = 2\n")
    assert _life(None, goal="add retries", path=str(wt))[0] == "2.4"
    g("commit", "-qam", "two")
    assert _life(None, goal="add retries", path=str(wt))[::3] == ("2.3", "Run the self-test again")
    assert st(sys.executable, "-c", "raise SystemExit(3)").returncode == 3
    assert _life(None, goal="add retries", path=str(wt))[::3] == ("2.3", "Fix the self-test") and "exit 3" in ws()["life"]["do"]

    (wt / "b.py").write_text("y = 1\n")
    st("true")
    assert _life(None, goal="add retries", path=str(wt))[0] == "2.3"
    g("add", "-A")
    g("commit", "-qm", "three")
    assert _life(None, goal="add retries", path=str(wt))[0] == "2.4"
    assert "a.py" in g("status", "--porcelain", "--untracked-files=all") or not g("status", "--porcelain")

    dr = lambda **kw: _row(_mr(draft=True), path=str(wt), **kw)["life"]["do"]
    assert dr() == "!7 is still a draft. Greenlight marks it ready on its next run."
    (wt / "a.py").write_text("x = 3\n")
    g("commit", "-qam", "four")
    assert dr().endswith("Its last self-test is older than its head commit (`true`).")
    assert _row(_mr(draft=True), path="/nonexistent")["life"]["do"] == "!7 is still a draft. Greenlight marks it ready on its next run."
    assert "selftest" in subprocess.run([sys.executable, lifestage.__file__, "selftest"], capture_output=True, text=True).stderr

    empty = tmp_path / "empty-wt"
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    o = subprocess.run([sys.executable, lifestage.__file__, "selftest", "true"], cwd=empty, capture_output=True, text=True)
    assert o.returncode == 0 and "counts for nothing" in o.stderr and lifestage.self_test({"path": str(empty)})["fresh"] is False


def test_dash_new_task_and_tree_new_preflight_gates(lifestage_env):
    repo = [{"id": "r1", "name": "widgets", "host": "github", "path": "acme/widgets"}]

    def task(payload, decide_obj):
        calls = []

        def orca(*a):
            calls.append(a)
            return {"worktree": {"id": "w9", "path": "/w/" + a[a.index("--name") + 1]}} if a[:2] == ("worktree", "create") else {}

        with (
            mock.patch.object(dash, "repos", return_value=repo),
            mock.patch.object(dash, "live_agents", return_value={}),
            mock.patch.object(dash.greenlight, "orca", side_effect=orca),
            mock.patch.dict(sys.modules, {"decide": decide_obj}),
        ):
            return dash.task("new", "widgets", json.dumps(payload)), calls

    out, calls = task({"text": "make it better"}, FakeDecide(0.9, clarity=0.2))
    assert out["paused"] and [q["id"] for q in out["questions"]] == ["done", "scope"] and out["p_ambiguous"] == 0.9 and not calls, out
    assert not (dash.TASKS / "make-it-better").exists()

    out, calls = task(
        {"text": "make it better", "answers": {"done": "A working prototype I can try", "scope": "The smallest change that works"}},
        FakeDecide(0.9, clarity=0.2),
    )
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

    out, calls = task({"text": "make a plan", "answers": {"done": "A written plan first, no code"}}, FakeDecide(0.9, clarity=0.2))
    brief = next(a for a in calls if a[:2] == ("worktree", "create"))
    brief = brief[brief.index("--prompt") + 1]
    assert "If they ask for a plan or investigation first, stop at that deliverable" in brief and "- What does done look like? A written plan first, no code" in brief
    assert "gate push --execute" in brief and "gate pr <metadata.json> --execute" in brief

    out, calls = task({"text": "Add retry backoff to the outbound queue, done when test_queue passes"}, FakeDecide(0.05))
    assert out["ok"] and any(a[:2] == ("worktree", "create") for a in calls)
    out, calls = task({"text": "make it worse", "force": True}, FakeDecide(0.99))
    assert out["ok"] and lifestage.record("preflight", {"name": "make-it-worse"})["forced"]

    # New workspace: asked only when an agent would start on a goal
    import ade_basement

    tcalls, launches = [], []

    def tree_call(*a):
        tcalls.append(a)
        return {"worktree": {"path": "/w/new", "worktreeId": "t1"}} if a[:2] == ("worktree", "create") else {}

    def fake_launch(path, agent, prompt):
        launches.append((path, agent, prompt))
        return {"terminal": {"handle": "%9", "session": "mc-pf"}}

    with (
        mock.patch.object(dash, "tree", return_value=(tree_call, {"repoId": "r"}, [{"repoId": "r"}])),
        mock.patch.object(dash, "repo_cfg", return_value={}),
        mock.patch.object(ade_basement, "launch", side_effect=fake_launch),
        mock.patch.dict(sys.modules, {"decide": FakeDecide(0.9, clarity=0.2)}),
    ):
        p = dash.tree_new("/w/repo", {"name": "better", "goal": "make it better", "agent": True})
        assert p["paused"] and len(p["questions"]) == 2 and not tcalls and not launches
        assert dash.tree_new("/w/repo", {"name": "better", "goal": "make it better", "agent": False})["ok"] and tcalls and not launches
        tcalls.clear()
        out = dash.tree_new("/w/repo", {"name": "better", "goal": "make it better", "agent": True, "answers": {"scope": "Investigate and report back first"}})
        assert out["ok"] and out["session"] == "mc-pf"
        assert not any(a[:2] == ("agent", "launch") for a in tcalls)
        assert len(launches) == 1 and launches[0][0] == "/w/new"
        assert launches[0][2].startswith("make it better\n\nBefore you started I answered these:\n- How far should it go? Investigate and report back first")
        assert "lifestage.py selftest -- <the test command>" in launches[0][2]
        assert lifestage.record("preflight", {"path": "/w/new"})["answers"]


def test_tree_new_starts_default_harness_in_basement_preserving_goal_newlines(monkeypatch):
    import ade_basement

    create_calls, launches = [], []

    def fake_create(*args, **kw):
        create_calls.append(args)
        if args[:2] == ("worktree", "create"):
            return {"worktree": {"path": "/w/new"}}
        return {}

    def fake_launch(path, agent, prompt):
        launches.append((path, agent, prompt))
        return {"terminal": {"handle": "%4", "session": "mc-new"}}

    monkeypatch.setattr(dash, "tree", lambda path, every=False: (fake_create, {"repoId": "/w/repo", "hostId": "local"}, [{"repoId": "/w/repo", "hostId": "local"}]))
    monkeypatch.setattr(dash, "repo_cfg", lambda path: {})
    monkeypatch.setattr(dash, "main_of", lambda path: None)
    monkeypatch.setattr(dash, "preflight", lambda *args: None)
    monkeypatch.setattr(ade_basement, "launch", fake_launch)
    monkeypatch.setitem(greenlight.CONFIG, "agents", ["codex", "claude"])

    goal = "Fix login\nKeep the existing API"
    out = dash.tree_new("/w/repo", {"name": "login", "goal": goal, "agent": True})
    assert out["ok"] and out["path"] == "/w/new" and out["session"] == "mc-new"
    assert not any(args[:2] == ("agent", "launch") for args in create_calls)
    assert len(launches) == 1
    assert launches[0][0] == "/w/new" and launches[0][1] == "codex"
    assert launches[0][2].startswith(goal) and "gate push --execute" in launches[0][2]


def test_tree_new_warns_on_basement_launch_failure_and_remote_missing_id(monkeypatch):
    import ade_basement

    def local_call(*args):
        if args[:2] == ("worktree", "create"):
            return {"worktree": {"path": "/w/new"}}
        return {"worktrees": []}

    monkeypatch.setattr(dash, "tree", lambda path, every=False: (local_call, {"repoId": "r"}, [{"repoId": "r"}]))
    monkeypatch.setattr(dash, "repo_cfg", lambda path: {})
    monkeypatch.setattr(dash, "main_of", lambda path: None)
    monkeypatch.setattr(dash, "preflight", lambda *args: None)
    monkeypatch.setattr(ade_basement, "launch", lambda *args: (_ for _ in ()).throw(RuntimeError("tmux isn't installed")))
    out = dash.tree_new("/w/repo", {"name": "login", "goal": "Fix login", "agent": True})
    assert out["ok"] and "didn't start" in out["warn"] and "session" not in out

    # Remote Orca worktree falls back to ADE launch and warns when worktree ID is missing
    monkeypatch.setattr(dash, "tree", lambda path, every=False: (local_call, {"repoId": "r", "hostId": "ssh:t1"}, [{"repoId": "r", "hostId": "ssh:t1"}]))
    out = dash.tree_new("/w/repo", {"name": "login", "goal": "Fix login", "agent": True})
    assert out["ok"] and "no agent started" in out["warn"]


def test_lifestage_cli_piles_and_custom_phases(lifestage_env, tmp_path):
    snap = [
        _row(_mr(_discussions=[_thread("alice", "alice")])) | {"id": "a", "name": "alpha"},
        _row(_mr(_approvals_left=1)) | {"id": "b", "name": "beta"},
        _row(_mr(state="merged")) | {"id": "c", "name": "gamma"},
    ]
    del snap[1]["life"]
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": snap}))
    run = lambda *a: subprocess.run([sys.executable, lifestage.__file__, *a], capture_output=True, text=True, check=True).stdout
    out = run()
    assert out.index("YOURS (1)") < out.index("WAITING FOR OTHERS (1)") < out.index("DONE (1)"), out
    assert "4.3 Review › Respond" in out and "Resolve 1 thread" in out and "4.5 Review › Approved" in out and "5.3 Land › Merged" in out, out
    one = json.loads(run("beta", "--json"))
    assert len(one) == 1 and (one[0]["workspace"], one[0]["pile"], one[0]["blocker"], one[0]["mr"]) == ("beta", "waiting", "reviewers", "!7")
    assert "no scanned workspace named nope" in run("nope")

    # Stale selftest + preflight summary in CLI output
    wt = tmp_path / "delta-wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True)
    g("config", "user.name", "t")
    g("config", "user.email", "t@t")
    (wt / "a.py").write_text("x = 1\n")
    g("add", "-A")
    g("commit", "-qm", "one")
    subprocess.run([sys.executable, lifestage.__file__, "selftest", "--", "true"], cwd=wt, capture_output=True, text=True, check=True)
    (wt / "a.py").write_text("x = 2\n")
    g("commit", "-qam", "two")

    snap = [_row(None, goal="add retries", path=str(wt)) | {"id": "d", "name": "delta"}]
    del snap[0]["life"]
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": snap}))
    lifestage.save("preflight", "delta", {"goal": "add retries", "ok": True, "p_ambiguous": 0.1, "clarity": 0.9, "provider": "fake", "answers": {}})
    out = run("delta")
    assert "1.3 done condition: P(ambiguous) 0.1, clarity 0.9 (fake)" in out and "2.3 self-test: `true` exit 0" in out and "stale" in out, out

    # custom stages respect explicit Owner: and Phase: instead of hardcoding 3.4 Verify / agent
    (greenlight.STATE / "lifecycles/custom-phases.md").write_text(
        "# custom\n\n1. `rfc` — RFC approved. Done: `signoff` Phase: `define`\n2. `mr_open` — x\n"
        "3. `qa` — QA passed. Done: `label=qa-ok` Owner: `reviewers` Phase: `review`\n"
        "4. `e2e` — E2E green. Done: `check=e2e` Phase: `verify`\n5. `merge` — x\n"
        "6. `release_qa` — Prod QA. Done: `label=prod-ok` Owner: `reviewers`\n"
    )
    assert _life(None, goal="new feature", project="acme/custom-phases") == ("1.2", "yours", "you", "Sign off rfc")
    (greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/custom-phases@b": ["rfc"]}))
    assert _life(_mr(labels=[]), project="acme/custom-phases") == ("4.5", "waiting", "reviewers", None)
    assert _life(_mr(labels=["qa-ok"], _checks=[{"name": "e2e", "status": "running"}]), project="acme/custom-phases") == ("3.4", "waiting", "CI", None)
    assert _life(_mr(state="merged", labels=[]), project="acme/custom-phases") == ("5.3", "waiting", "reviewers", None)

    (greenlight.STATE / "lifecycles/auto-reb.md").write_text(
        "# auto-reb\n\nOptions: `auto_rebase`\n\n1. `mr_open` — x\n2. `me_lbl` — Add it. Done: `label=ok` Owner: `me`\n"
        "3. `rev_so` — Reviewer signoff. Done: `signoff` Owner: `reviewers`\n4. `merge` — x\n"
    )
    assert _life(_mr(labels=[]), project="acme/auto-reb") == ("3.4", "yours", "you", "Complete me lbl")
    assert _life(_mr(labels=["ok"]), project="acme/auto-reb") == ("3.4", "waiting", "reviewers", None)
    (greenlight.STATE / "signoffs.json").write_text(json.dumps({"acme/auto-reb#7": ["rev_so"]}))
    assert _life(_mr(labels=["ok"], detailed_merge_status="need_rebase"), project="acme/auto-reb") == ("3.3", "yours", "your agent", "Rebase !7")
