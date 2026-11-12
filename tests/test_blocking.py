"""python3 test_blocking.py: the blocking rules as blocking.py ports them, on canned GitLab data (no network)."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import pathlib
import tempfile
from unittest import mock

import blocking
import greenlight

ME, TN, PRIV = greenlight.ME, "acme/widgets", "acme/widgets-private"
tmp = pathlib.Path(tempfile.mkdtemp())
blocking.FILE = tmp / "blocks.json"
blocking.REPOS = {TN: "Release-", PRIV: None}
blocking.PEOPLE = blocking.DEVS = {ME, "bob", "carol", "dave", "erin", "frank"}
blocking.TEAMS = {"dev-team": blocking.DEVS}


def mr(author="bob", assignees=(), conflicts=False):
    return {"iid": 9, "author": {"username": author}, "assignees": [{"username": a} for a in assignees],
            "has_conflicts": conflicts, "target_branch": "develop"}


def thread(*speakers, resolved=False):
    return {"notes": [{"author": {"username": s}, "resolvable": True, "resolved": resolved} for s in speakers]}


def check(m, proj=PRIV, pipe="success", jobs=(), threads=(), left=0, rules=("dev-team",), approved=(), reviewers=(),
          notes=(), can_approve=True):
    """the blocking hits for one MR. GitLab answers by path suffix; anything it isn't asked for stays unasked."""
    api = {"/pipelines?per_page=1": [{"id": 1, "status": pipe}] if pipe else [],
           "/jobs?per_page=100": [{"id": i, "status": s, "stage": st} for i, (s, st) in enumerate(jobs)],
           "/approvals": {"approvals_left": left, "approval_rules_left": [{"name": r} for r in rules],
                          "approved_by": [{"user": {"username": u}} for u in approved], "user_can_approve": can_approve},
           "/reviewers": [{"user": {"username": u}, "state": "reviewed"} for u in reviewers],
           "/commits?per_page=1": [{"committed_date": "2026-11-01T00:00:00Z"}],
           "/notes?sort=desc&per_page=100": [{"author": {"username": u}, "system": False,
                                              "created_at": "2026-11-02T00:00:00Z"} for u in notes]}
    gl = lambda path: next(v for k, v in api.items() if path.endswith(k))
    with mock.patch.object(greenlight, "gl", side_effect=gl), mock.patch.object(greenlight, "gl_all", return_value=list(threads)):
        return blocking.check(proj, m)


# owner: the first assignee on the roster, else the author
assert blocking.owner(mr(assignees=["review-bot", "carol"])) == "carol"
assert blocking.owner(mr(assignees=["review-bot"])) == "bob"

assert check(mr(ME, conflicts=True)) == [(ME, "conflict", "needs a rebase onto develop")]
assert check(mr(ME), pipe="failed", jobs=[("failed", "build"), ("failed", "test")]) == [(ME, "pipeline", "CI failed: test")]
assert check(mr(ME), proj=TN, pipe="running", jobs=[("running", "test")]) == []  # CI still going: nobody yet, no orphan
assert check(mr(ME), proj=TN, pipe="canceled") == [(ME, "orphan", "falling through the cracks (pipeline)")]

# threads: everyone in it plus the owner, minus whoever spoke last; resolved ones don't count; approvals unasked
hits = check(mr(), threads=[thread("bob", ME, "bob"), thread("carol"), thread(ME, resolved=True)], left=2)
assert sorted(hits) == [("bob", "discussion", "@carol is waiting on you in a thread"),
                        (ME, "discussion", "@bob is waiting on you in a thread")], hits
assert check(mr(), threads=[thread("bob", ME)]) == [("bob", "discussion", f"@{ME} is waiting on you in a thread")]

# approvals: rule-team members minus the author, who approved, and who reviewed or commented since the last push
hits = check(mr(), left=1, approved=["carol"], reviewers=["dave"], notes=["erin"])
assert sorted(u for u, *_ in hits) == ["frank", ME] and all(r == "approval" for _, r, _ in hits)
assert check(mr(ME), left=2, approved=["bob", "carol", "dave", "erin", "frank"]) == \
    [(ME, "approval", "needs 2 more approvals")]  # nobody left to ask: the owner
hits = check(mr(), left=1, approved=["carol"], reviewers=["dave"], notes=["erin"], can_approve=False)
assert [u for u, *_ in hits] == ["frank"]  # my commits are in it: GitLab won't take my approval, so no ask
assert check(mr(ME), proj=TN) == []  # ready to ship: not a block, not an orphan

# orphan: a milestone-repo MR nobody owns goes to me, unless I've approved it
assert check(mr("rando"), proj=TN) == [(ME, "orphan", "falling through the cracks (unknown author @rando)")]
assert check(mr("rando"), proj=TN, approved=[ME]) == []
assert check(mr("rando")) == []  # not a milestone repo

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
print("ok")
