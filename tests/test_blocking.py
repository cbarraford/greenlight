"""Tests for scripts/blocking.py: blocking rules on canned GitLab data (no network)."""
from unittest import mock

import pytest

import blocking
import greenlight

TN = "acme/widgets"
PRIV = "acme/widgets-private"


@pytest.fixture()
def blocking_env(gl_home, monkeypatch):
    me = greenlight.ME
    devs = {me, "bob", "carol", "dave", "erin", "frank"}
    monkeypatch.setattr(blocking, "FILE", gl_home / "blocks.json")
    monkeypatch.setattr(blocking, "REPOS", {TN: "Release-", PRIV: None})
    monkeypatch.setattr(blocking, "PEOPLE", devs)
    monkeypatch.setattr(blocking, "TEAMS", {"dev-team": devs})
    return me


def _mr(author="bob", assignees=(), conflicts=False):
    return {
        "iid": 9,
        "author": {"username": author},
        "assignees": [{"username": a} for a in assignees],
        "has_conflicts": conflicts,
        "target_branch": "develop",
    }


def _thread(*speakers, resolved=False):
    return {"notes": [{"author": {"username": s}, "resolvable": True, "resolved": resolved} for s in speakers]}


def _check(
    m,
    proj=PRIV,
    pipe="success",
    jobs=(),
    threads=(),
    left=0,
    rules=("dev-team",),
    approved=(),
    reviewers=(),
    notes=(),
    can_approve=True,
):
    """The blocking hits for one MR. GitLab answers by path suffix; anything unasked stays unasked."""
    api = {
        "/pipelines?per_page=1": [{"id": 1, "status": pipe}] if pipe else [],
        "/jobs?scope[]=failed": [{"id": i, "status": s, "stage": st} for i, (s, st) in enumerate(jobs)],
        "/approvals": {
            "approvals_left": left,
            "approval_rules_left": [{"name": r} for r in rules],
            "approved_by": [{"user": {"username": u}} for u in approved],
            "user_can_approve": can_approve,
        },
        "/reviewers": [{"user": {"username": u}, "state": "reviewed"} for u in reviewers],
        "/commits?per_page=1": [{"committed_date": "2026-11-01T00:00:00Z"}],
        "/notes?sort=desc&per_page=100": [
            {"author": {"username": u}, "system": False, "created_at": "2026-11-02T00:00:00Z"} for u in notes
        ],
    }

    def fake_gl(path):
        return next(v for k, v in api.items() if path.endswith(k))

    def fake_gl_all(path):
        return api["/jobs?scope[]=failed"] if path.endswith("/jobs?scope[]=failed") else list(threads)

    with mock.patch.object(greenlight, "gl", side_effect=fake_gl), mock.patch.object(greenlight, "gl_all", side_effect=fake_gl_all):
        return blocking.check(proj, m)


def test_owner_selection(blocking_env):
    # owner: the first assignee on the roster, else the author
    assert blocking.owner(_mr(assignees=["review-bot", "carol"])) == "carol"
    assert blocking.owner(_mr(assignees=["review-bot"])) == "bob"


def test_conflicts_pipeline_and_canceled_orphan(blocking_env):
    me = blocking_env
    assert _check(_mr(me, conflicts=True)) == [(me, "conflict", "needs a rebase onto develop")]
    assert _check(_mr(me), pipe="failed", jobs=[("failed", "build"), ("failed", "test")]) == [(me, "pipeline", "CI failed: test")]
    assert _check(_mr(me), proj=TN, pipe="running", jobs=[("running", "test")]) == []  # CI still going: nobody yet, no orphan
    assert _check(_mr(me), proj=TN, pipe="canceled") == [(me, "orphan", "falling through the cracks (pipeline)")]


def test_discussion_threads(blocking_env):
    me = blocking_env
    # threads: everyone in it plus the owner, minus whoever spoke last; resolved ones don't count; approvals unasked
    hits = _check(_mr(), threads=[_thread("bob", me, "bob"), _thread("carol"), _thread(me, resolved=True)], left=2)
    assert sorted(hits) == [
        ("bob", "discussion", "@carol is waiting on you in a thread"),
        (me, "discussion", "@bob is waiting on you in a thread"),
    ], hits
    assert _check(_mr(), threads=[_thread("bob", me)]) == [("bob", "discussion", f"@{me} is waiting on you in a thread")]


def test_approvals_and_ready_to_ship(blocking_env):
    me = blocking_env
    # approvals: rule-team members minus the author, who approved, and who reviewed or commented since the last push
    hits = _check(_mr(), left=1, approved=["carol"], reviewers=["dave"], notes=["erin"])
    assert sorted(u for u, *_ in hits) == ["frank", me]
    assert all(r == "approval" for _, r, _ in hits)
    assert _check(_mr(me), left=2, approved=["bob", "carol", "dave", "erin", "frank"]) == [
        (me, "approval", "needs 2 more approvals")
    ]  # nobody left to ask: the owner
    hits = _check(_mr(), left=1, approved=["carol"], reviewers=["dave"], notes=["erin"], can_approve=False)
    assert [u for u, *_ in hits] == ["frank"]  # my commits are in it: GitLab won't take my approval, so no ask
    assert _check(_mr(me), proj=TN) == []  # ready to ship: not a block, not an orphan


def test_orphan_unknown_author(blocking_env):
    me = blocking_env
    # orphan: a milestone-repo MR nobody owns goes to me, unless I've approved it
    assert _check(_mr("rando"), proj=TN) == [(me, "orphan", "falling through the cracks (unknown author @rando)")]
    assert _check(_mr("rando"), proj=TN, approved=[me]) == []
    assert _check(_mr("rando")) == []  # not a milestone repo


def test_blocks_caching_and_expiry(blocking_env):
    # blocks(): scans once, then serves the cache until it's CACHE old; cached() ages out
    with mock.patch.object(blocking, "scan", return_value={f"{TN}!9": {"reason": "approval"}}) as scan:
        assert blocking.blocks() == blocking.blocks() == blocking.cached() == {f"{TN}!9": {"reason": "approval"}}
        assert scan.call_count == 1
        blocking.blocks(fresh=True)
        assert scan.call_count == 2
    with mock.patch.object(blocking.time, "time", return_value=blocking.time.time() + 7 * 3600):
        assert blocking.cached() is None
    with mock.patch.object(blocking, "scan", side_effect=RuntimeError("502")):
        assert blocking.blocks(fresh=True) is None  # no answer, not "nothing blocked"


def test_scan_aggregation_and_all_failed_error(blocking_env):
    me = blocking_env
    # scan(): aggregates hits, skips transient single-MR errors, raises if ALL MR checks fail
    m1 = {"iid": 10, "web_url": "https://example.invalid/10", "title": "First"}
    m2 = {"iid": 11, "web_url": "https://example.invalid/11", "title": "Second"}

    def fake_check(_proj, m):
        if m["iid"] == 10:
            return [(me, "discussion", "@bob"), (me, "discussion", "@carol")]
        raise RuntimeError("blip")

    with (
        mock.patch.object(blocking, "open_mrs", side_effect=lambda proj, pre: [m1, m2] if proj == TN else []),
        mock.patch.object(blocking, "check", side_effect=fake_check),
    ):
        res = blocking.scan()
        assert res == {
            f"{TN}!10": {
                "reason": "discussion",
                "why": "2 threads are waiting on you",
                "n": 2,
                "url": m1["web_url"],
                "title": m1["title"],
            }
        }, res

    with (
        mock.patch.object(blocking, "open_mrs", side_effect=lambda proj, pre: [m1] if proj == TN else []),
        mock.patch.object(blocking, "check", side_effect=RuntimeError("502")),
        pytest.raises(RuntimeError, match="all 1 MR blocking checks failed"),
    ):
        blocking.scan()
