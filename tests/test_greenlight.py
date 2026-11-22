"""python3 test_greenlight.py: lifecycle decisions on canned MRs (no network)."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import shutil, sys
import greenlight
import audit
assert greenlight.primary_agent([]) == {}
assert greenlight.primary_agent([{"state": "working", "updatedAt": 1}, {"state": "done", "updatedAt": 2}])["state"] == "working"
assert greenlight.primary_agent([{"state": "waiting", "updatedAt": 1}, {"state": "working", "updatedAt": 2}])["state"] == "working"
real_merge_verdict = audit.merge_verdict
audit.merge_verdict = lambda ws, mr: {"ok": True, "failed": [], "target_oid": "base1"}

greenlight.BOT = "review-bot"  # the release-train example has a review bot step
(greenlight.STATE / "lifecycles").mkdir()
shutil.copy(greenlight.ROOT / "examples/lifecycles/release-train.md", greenlight.STATE / "lifecycles/widgets.md")

U = lambda n: {"username": n}
def mr(**kw):
    base = {"iid": 1, "state": "opened", "author": U(greenlight.ME), "draft": False, "has_conflicts": False,
            "assignees": [U(greenlight.BOT)], "reviewers": [U(greenlight.BOT)], "milestone": {"title": "R"},
            "head_pipeline": {"status": "success", "web_url": "u"}, "blocking_discussions_resolved": True,
            "detailed_merge_status": "mergeable", "_discussions": [], "_approved_by": ["alice"],
            "sha": "abc", "target_branch": "main", "_default_branch": "main"}
    return base | kw
ws = lambda goal="": {"project": "acme/widgets", "goal": goal, "branch": "b"}
nxt = lambda m, w=None: (lambda r: (r["step"], r["owner"]))(greenlight.evaluate(w or ws(), m, {"title": "Release-9.9.9"}))
thread = lambda who, last: {"notes": [{"resolvable": True, "resolved": False, "author": U(who)}, {"author": U(last)}]}

assert nxt(mr()) == ("merge", "greenlight")
assert nxt(mr(), ws("hold until release")) == ("merge", "me")
# a hold is something I wrote in the goal, never words in the MR title that "Merge !N: <title>" copies in
held = lambda title, goal, **kw: nxt(mr(title=title, **kw), ws(goal))[1] == "me"
t290 = "code_audit: dedupe against closed security-hold issues"
assert not held(t290, f"Merge !290: {t290}")  # was once held for its title
assert nxt(mr(title="Document the (parked) label"), ws("Merge !1: Document the (parked) label")) != ("parked", "parked")
assert nxt(mr(title="Document the (parked) label"), ws("Merge !1: Document the (parked) label.")) != ("parked", "parked")
assert nxt(mr(title="Document the (parked) label"), ws("Merge !1: Document the (parked) label. (parked)")) == ("parked", "parked")
t187 = "Batch: security-hold fixes for #11, #12, #13 (priority::0)"
assert not held(t187, f"Merge !187: {t187}")  # a batch MR
for t in ["feat: hold inbounds on solvency-gated chains until a solvent report covers them",
          "feat(acme): keep gas in vaults for the tokens they hold (VaultTokenGasReserve)",
          "ci: don't merge-train forks", "fix: keep draft_status in sync"]:
    assert not held(t, f"Merge !5: {t}") and not held(t, f"Merge !5: {t.upper()}"), t
assert not held("x", "Merge !5: x (security_hold, on-hold-list)")  # joined by - or _: a name, not an instruction
assert greenlight.intents({"comment": "Hold"}, {"title": "Hold"})["is_held"]  # ambiguous standalone comment stays an explicit safety hold
for extra in [" (hold)", " hold", " (hold until 3.22)", " — don't merge", " (do not merge)", " (don’t merge)", " (HOLD)"]:
    assert held(t290, f"Merge !290: {t290}{extra}"), extra
assert held("renamed since", f"Merge !290: {t290} (hold)")  # title edited after the goal was written: still held
assert held("Hold", "Merge !290: Hold\nHold until #67 merges")  # a second occurrence is an instruction
assert held("Hold", "Merge !12: Hold until QA signs off")  # a title prefix must not erase an explicit hold
assert held("Fix parser (hold until QA)", "Merge !66: Fix parser (hold until QA)")  # edited title must not absorb a goal's hold
assert not greenlight.intents({"goal": "Merge !12: Document the (parked) label (keep draft)", "comment": ""},
                              {"title": "Document the (parked) label"})["is_parked"]
assert not greenlight.intents({"goal": "Merge !12: Document the (parked) label (hold until QA)", "comment": ""},
                              {"title": "Document the (parked) label"})["is_parked"]
assert not greenlight.intents({"goal": "Merge !1: Hold inbound transactions.\nRun the smoke tests", "comment": ""},
                              {"title": "Hold inbound transactions"})["is_held"]
assert not greenlight.intents({"goal": "Merge #12: Document the (parked) label (run smoke tests first)", "comment": ""},
                              {"title": "Document the (parked) label"})["is_parked"]
assert not greenlight.intents({"goal": "Merge !12: Document the (parked) label — run smoke tests", "comment": ""},
                              {"title": "Document the (parked) label"})["is_parked"]
assert not greenlight.intents({"goal": "Merge !12: Hold inbound transactions — run smoke tests", "comment": ""},
                              {"title": "Hold inbound transactions"})["is_held"]
assert not greenlight.said({"goal": "Merge #1: Hold inbound transactions", "comment": "", "_goal_backfill": True},
                           {"title": "Throttle inbound transactions"}).strip()  # renamed generated title is not the user's hold
assert nxt(mr(title=t290), ws() | {"comment": f"Merge !290: {t290} (hold) — dispatched 2026-10-17"}) == ("merge", "me")  # old status line
assert nxt(mr(title="keep draft PRs as drafts", draft=True), ws("Merge !9: keep draft PRs as drafts")) == ("ready", "greenlight")
assert nxt(mr(title="x", draft=True), ws("Merge !9: x (stay draft)")) == ("ready", "me")
# every goal that says hold still holds, whatever its title
for goal in ["Merge !101: fix(app): SEC-001 - widget count overflows (hold)",
             "Merge !102: fix(core): SEC-002 - typo in the greeting (hold)",
             "Merge !103: fix(ui): SEC-003 - button is the wrong blue (hold)",
             "Merge !104: fix(docs): SEC-004 - broken link in the README (hold)",
             "Merge !105: fix(tools): SEC-005 - script prints twice (hold)"]:
    assert held(goal.split(": ", 1)[1].removesuffix(" (hold)"), goal), goal
for goal in ["Merge !106: feat(cart): add a coupon field (stay draft)",
             "Merge !107: chore: bump the base image (CVE-2000-0001) (stay draft)",
             "Merge !108: fix: upgrade the web server (CVE-2000-0002 and 5 more) (stay draft)"]:
    assert nxt(mr(title=goal.split(": ", 1)[1].removesuffix(" (stay draft)"), draft=True), ws(goal)) == ("ready", "me"), goal
assert nxt(mr(head_pipeline={"status": "failed", "web_url": "u"})) == ("ci_green", "agent")
assert nxt(mr(head_pipeline={"status": "running", "web_url": "u"})) == ("ci_green", "ci")
assert nxt(mr(has_conflicts=True)) == ("no_conflicts", "agent")
assert nxt(mr(reviewers=[])) == ("review_bot", "greenlight")
assert nxt(mr(milestone=None)) == ("milestone", "greenlight")
assert nxt(mr(draft=True)) == ("ready", "greenlight")
assert nxt(mr(draft=True), ws("Merge !1 (stay draft)")) == ("ready", "me")
remembered = {"wid": {"comment": "", "goal": "Merge !1: x (stay draft)"}}
assert greenlight.goal_with_backfill("", "wid", remembered) == ("Merge !1: x (stay draft)", False)
assert greenlight.goal_with_backfill("Review !1 — dispatched 2026-11-21", "wid", remembered) == ("Merge !1: x (stay draft)", False)
assert greenlight.goal_with_backfill("new user goal", "wid", remembered) == ("new user goal", True)
remembered = {"wid": {"comment": "", "goal": "Merge !12: old", "branch": "feature", "linked_mr": 12}}
assert greenlight.goal_with_backfill("", "wid", remembered, "feature", 34) == ("", True)
assert greenlight.goal_with_backfill("", "wid", remembered, "other-branch", 12) == ("", True)
scoped_backfill = {"wid": remembered["wid"] | {"forge": "github", "project": "octo/a"}}
assert greenlight.goal_with_backfill("", "wid", scoped_backfill, "feature", 12, "github", "octo/a")[1] is False
assert greenlight.goal_with_backfill("", "wid", scoped_backfill, "feature", 12, "github", "octo/b")[1] is True
assert greenlight.goal_with_backfill("", "wid", remembered, "feature", 12, "github", "octo/a")[1] is True
greenlight.backfills_update(lambda d: d.update({"old": {"goal": "old"}}))
snapshot = greenlight.backfills_load()["old"]
greenlight.backfills_update(lambda d: d.update({"new": {"goal": "new"}}))
greenlight.backfills_update(lambda d: d.pop("old", None) if d.get("old") == snapshot else None)
assert "new" in greenlight.backfills_load()  # pruning an old snapshot must not lose a concurrent new goal
assert nxt(mr(blocking_discussions_resolved=False)) == ("threads", "agent")
assert nxt(mr(detailed_merge_status="not_approved")) == ("approved", "reviewers")
assert nxt(mr(detailed_merge_status="merge_request_blocked", _approvals_left=2)) == ("approved", "reviewers")  # !5217
assert nxt(mr(detailed_merge_status="merge_request_blocked", _blocked_by=[5187])) == ("merge", "dependency")
assert nxt(mr(state="merged")) == ("done", "done")
other = dict(author=U("someone"))
assert nxt(mr(**other)) == ("review", "me")
assert nxt(mr(**other, _can_approve=False)) == ("review", "reviewers")  # my commits in it
assert nxt(mr(**other, _approved_by=[greenlight.ME])) == ("done", "done")
assert nxt(mr(**other, _discussions=[thread(greenlight.ME, greenlight.ME)])) == ("review", "author")
assert nxt(mr(**other, _discussions=[thread(greenlight.ME, "someone")])) == ("review", "me")
assert nxt(None) == ("goal", "me") and nxt(None, ws("build x")) == ("freeform", "agent")
assert nxt(mr(draft=True), ws() | {"comment": "Rebase !5008 (stay draft) — dispatched 2026-10-17"}) == ("ready", "me")
assert nxt(mr(_approved_by=[greenlight.BOT])) == ("merge", "reviewers")
assert nxt(mr(draft=True), ws("Merge !5008 (stay draft, revisit 2999-01-01)")) == ("parked", "parked")
assert nxt(mr(draft=True), ws("Merge !5008 (stay draft, revisit 2000-01-01)")) == ("ready", "me")
# threads: reviewer spoke last -> agent; we (or the bot) replied last -> reviewer re-review
assert nxt(mr(_discussions=[thread("bob", "bob")])) == ("threads", "agent")
assert nxt(mr(_discussions=[thread("bob", greenlight.BOT)])) == ("threads", "reviewers")
assert nxt(None, ws("nightly loop (automation)")) == ("freeform", "working")
assert nxt(mr(), ws("Merge !5131 (parked)")) == ("parked", "parked")
assert nxt(mr(state="merged"), ws("Merge !5131 (parked)")) != ("parked", "parked")  # a finished MR still gets cleaned up
print("ok")
audit.merge_verdict = real_merge_verdict

# stale questions: a pinged workspace closes once it's gone or no longer waiting on me
import json
greenlight.ping("ws-a: merge?", "ws-a"); greenlight.ping("ws-b: goal?", "ws-b"); greenlight.ping("ws-c: delete?", "ws-c"); greenlight.ping("fyi"); greenlight.ping("blocking: review?", "@review-x!147")
row = lambda name, owner: {"id": name + "-id", "name": name, "next": {"owner": owner, "action": "x"}}
greenlight.close_stale([row("ws-a", "ci"), row("ws-b", "me")])
out = [json.loads(l) for l in (greenlight.STATE / "outbox.jsonl").read_text().splitlines()]
assert [o.get("key") for o in out[:5]] == ["ws-a", "ws-b", "ws-c", None, "@review-x!147"]
assert {o["resolve"]: o["why"] for o in out[5:]} == {"ws-a": "now ci: x", "ws-c": "workspace removed"}
assert set(greenlight.pinged()) == {"ws-b", "@review-x!147"}
greenlight.resolve("@review-x!147", "no longer blocking")
greenlight.ping("blocking: review again?", "@review-x!147")  # back within a day: a flap, not news
greenlight.ping("ws-b: changed again", "ws-b")  # workspace keys always ping: the app replaces the open one
out = [json.loads(l) for l in (greenlight.STATE / "outbox.jsonl").read_text().splitlines()]
assert [o.get("text") for o in out[-2:]] == [None, "ws-b: changed again"]
greenlight.ping("ws-dup: merge?", "ws-dup")
before = len((greenlight.STATE / "outbox.jsonl").read_text().splitlines())
greenlight.pinged({k: v for k, v in greenlight.pinged().items() if k != "ws-dup"})  # crash after outbox, before key write
greenlight.ping("ws-dup: merge?", "ws-dup")
assert len((greenlight.STATE / "outbox.jsonl").read_text().splitlines()) == before and "ws-dup" in greenlight.pinged()
greenlight.resolve("ws-dup", "question resolved")
greenlight.ping("ws-dup: merge?", "ws-dup")
assert len((greenlight.STATE / "outbox.jsonl").read_text().splitlines()) == before + 2  # resolve plus a new ask
head_row = row("head-dup", "me") | {"project": "octo/app", "mr": {"ref": "#42", "sha": "head-a"}}
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [head_row]}))
greenlight.ping("head-dup: merge #42?", "head-dup")
head_row["mr"]["sha"] = "head-b"
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [head_row]}))
greenlight.pinged({k: v for k, v in greenlight.pinged().items() if k != "head-dup"})
greenlight.ping("head-dup: merge #42?", "head-dup")
head_events = [e for e in greenlight.outbox_events() if e.get("key") == "head-dup" and e.get("text")]
assert len(head_events) == 2 and head_events[0]["record"] != head_events[1]["record"]

# ping --ws: the workspaces a ping is about (the app lifts their hexes); names or ids, checked against the last scan
from unittest import mock
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [row(n, "reviewers") for n in ("feat-025", "fix-001", "fix-018")]}))
def cli(*args):
    n = len((greenlight.STATE / "outbox.jsonl").read_text().splitlines())
    with mock.patch.object(greenlight.sys, "argv", ["greenlight.py", "ping", *args]):
        try:
            greenlight.main(); code = 0
        except SystemExit as e:
            code = e.code
    new = (greenlight.STATE / "outbox.jsonl").read_text().splitlines()[n:]
    return code, [json.loads(l) for l in new]
O = "\nOptions: a | b"
code, out = cli("--key", "@t", "--ws", "feat-025", "--ws", "fix-001-id", "x" + O)  # an id comes out as its name
assert code == 0 and len(out) == 1 and out[0]["ws"] == ["feat-025", "fix-001"] and out[0]["key"] == "@t" and out[0]["text"] == "x" + O
code, out = cli("--key", "@t2", "--ws", "feat-025", "--ws", "nope", "x" + O)
assert code and "nope" in str(code) and out == [] and "@t2" not in greenlight.pinged()  # unknown: nothing sent, nothing recorded
code, out = cli("--key", "fix-018", "y" + O)
assert code == 0 and out[0]["ws"] == ["fix-018"] and out[0]["key"] == "fix-018"
code, out = cli("--key", "fix-018-id", "--ws", "fix-018", "y2" + O)  # an id key stays the key; its ws entry is the name, once
assert out[0]["ws"] == ["fix-018"] and out[0]["key"] == "fix-018-id"
code, out = cli("--ws", "fix-001", "--key", "@t3", "two words" + O)  # flags in any order before the text
assert out[0]["ws"] == ["fix-001"] and out[0]["key"] == "@t3" and out[0]["text"] == "two words" + O
code, out = cli("just", "text" + O)
assert code == 0 and "ws" not in out[0] and "key" not in out[0] and out[0]["text"] == "just text" + O
code, out = cli("--key", "@grouped", "z" + O)
assert "ws" not in out[0]  # an @ key isn't a workspace
code, out = cli("--key", "@t4", "a question with no buttons")  # the app would show only "Reply"
assert code and "Options" in str(code) and out == []

# glab retries: timeouts and transient 5xx/reset retry (3 tries); real errors fail fast
import subprocess
ok = subprocess.CompletedProcess([], 0, "[1]", "")
bad = lambda err: subprocess.CompletedProcess([], 1, "", err)
slow = subprocess.TimeoutExpired("glab", 45)
def gl_with(*results):
    with mock.patch.object(greenlight.subprocess, "run", side_effect=list(results)) as r, mock.patch.object(greenlight.time, "sleep"):
        try:
            return greenlight.gl("x"), r.call_count
        except Exception as e:
            return type(e).__name__, r.call_count
assert gl_with(slow, ok) == ([1], 2)
assert gl_with(bad("HTTP 502 Bad Gateway"), bad("read: connection reset by peer"), ok) == ([1], 3)
assert gl_with(slow, slow, slow) == ("TimeoutExpired", 3)
assert gl_with(bad("HTTP 503"), bad("HTTP 504"), bad("HTTP 502")) == ("RuntimeError", 3)
assert gl_with(bad("HTTP 404 Not Found")) == ("RuntimeError", 1)
assert gl_with(bad("HTTP 401 Unauthorized")) == ("RuntimeError", 1)

# precheck keeps last.json only when no run follows (an actionable run's report diffs against the old one)
import adopt
def precheck(owner, adoptions=0):
    (greenlight.STATE / "last.json").write_text("old")
    res = {"rows": [row("ws-a", owner)]}
    with mock.patch.object(greenlight, "scan", return_value=res), mock.patch.object(greenlight.sys, "argv", ["greenlight.py", "precheck"]), \
            mock.patch.object(adopt, "plan", return_value={"create": [{}] * adoptions}):
        try:
            greenlight.main()
        except SystemExit as e:
            return e.code, (greenlight.STATE / "last.json").read_text()
code, snapshot = precheck("ci")
saved = json.loads(snapshot)
assert code == 1 and saved["rows"][0] == row("ws-a", "ci") | {"_scan_id": saved["scan_id"]}
assert precheck("agent") == (0, "old")
assert precheck("ci", adoptions=2) == (0, "old")  # MRs with no workspace wake the automation too
newer = {"scan_id": "newer", "scan_started_ns": 20, "rows": [row("snapshot", "ci")]}
assert greenlight.keep(newer) is True
old_revision = greenlight.backfill_revision()
greenlight.backfills_update(lambda data: data.__setitem__("snapshot-id", {"goal": "generated"}))
try:
    greenlight.keep({"scan_id": "stale-overlay", "scan_started_ns": 21, "rows": [row("stale-overlay", "ci")]},
                    expected_backfill_revision=old_revision)
    assert False, "a scan with an old goal overlay must be recomputed"
except greenlight.BackfillChanged:
    pass
assert json.loads((greenlight.STATE / "last.json").read_text())["scan_id"] == "newer"
assert greenlight.keep({"scan_id": "older", "scan_started_ns": 10, "rows": [row("stale", "ci")]}) is False
assert json.loads((greenlight.STATE / "last.json").read_text())["scan_id"] == "newer"
assert greenlight.keep({"scan_id": "clock-a", "scan_started_ns": 100, "scan_clock_id": "boot-a", "rows": [row("clock-a", "ci")]})
assert not greenlight.keep({"scan_id": "clock-old", "scan_started_ns": 99, "scan_clock_id": "boot-a", "rows": [row("clock-old", "ci")]})
assert greenlight.keep({"scan_id": "clock-b", "scan_started_ns": 1, "scan_clock_id": "boot-b", "rows": [row("clock-b", "ci")]})
assert json.loads((greenlight.STATE / "last.json").read_text())["scan_id"] == "clock-b"
merged_row = row("merged", "done") | {"id": "original", "forge": "github", "project": "octo/app", "path": "/merged",
                                      "mr": {"iid": 1, "sha": "head", "state": "merged"}}
open_row = merged_row | {"mr": {"iid": 1, "sha": "head", "state": "opened"}, "next": {"owner": "ci", "action": "wait"}}
(greenlight.STATE / "last.json").write_text(json.dumps({"scan_id": "dashboard", "scan_started_ns": 30, "rows": [merged_row],
    "dashboard_base_started_ns": 10, "dashboard_merges": [{"request": True, "path": "/merged", "id": "original", "forge": "github",
        "project": "octo/app", "iid": 1, "sha": "head", "row": merged_row}]}))
assert not greenlight.keep({"scan_id": "too-old", "scan_started_ns": 5, "rows": [open_row]})
peer = open_row | {"id": "peer", "path": "/peer", "mr": open_row["mr"] | {"sha": "stale-head"}}
assert greenlight.keep({"scan_id": "older-but-useful", "scan_started_ns": 20, "rows": [open_row, peer, row("unrelated", "ci")]})
reconciled = json.loads((greenlight.STATE / "last.json").read_text())
assert len(reconciled["rows"]) == 3 and all(x["mr"]["state"] == "merged" for x in reconciled["rows"][:2])
assert reconciled["dashboard_base_started_ns"] == 10 and reconciled["accepted_scan_started_ns"] == 20
other_fork = open_row | {"id": "replacement", "project": "other/app"}
assert greenlight.keep({"scan_id": "other-fork", "scan_started_ns": 21, "rows": [other_fork]})
assert json.loads((greenlight.STATE / "last.json").read_text())["rows"][0]["mr"]["state"] == "opened"
fresher = open_row | {"agent": {"state": "working"}, "mr": open_row["mr"] | {"pipeline": "success"}}
assert greenlight.keep({"scan_id": "fresher", "scan_started_ns": 25, "rows": [fresher]})
reconciled = json.loads((greenlight.STATE / "last.json").read_text())
assert reconciled["accepted_scan_started_ns"] == 25 and reconciled["rows"][0]["mr"] == fresher["mr"] | {"state": "merged"}
assert reconciled["rows"][0]["agent"] == {"state": "working"}
assert not greenlight.keep({"scan_id": "stale-after-merge", "scan_started_ns": 22, "rows": [open_row]})
post_merge = merged_row | {"next": {"owner": "me", "step": "deploy", "action": "deploy now"}, "life": {"at": "9.1"}}
assert greenlight.keep({"scan_id": "post-merge", "scan_started_ns": 27, "rows": [post_merge]})
assert json.loads((greenlight.STATE / "last.json").read_text())["rows"][0]["next"] == post_merge["next"]
with mock.patch.object(greenlight.decisions, "asked", return_value="record-id") as record, mock.patch.object(greenlight, "outbox", side_effect=OSError("outbox stopped")):
    try:
        greenlight.ping("Fix it?\nOptions: retry | leave it", "@stuck:test", kind="agent-stuck")
        assert False
    except OSError:
        assert record.called  # persist the keyed question before publishing its answerable notification
with mock.patch.object(greenlight.decisions, "asked", return_value="record-id") as record, mock.patch.object(greenlight, "outbox"):
    greenlight.ping("Coordinate these workspaces?", "workspace-a", ws=["workspace-b"], ask=True)
    assert record.call_args.args[3] == ["workspace-a", "workspace-b"]
greenlight.ping("Agent stuck?\nOptions: retry | leave it", "@stuck:snapshot:one", ws=["snapshot-id"], kind="agent-stuck")
greenlight.close_stale([row("renamed", "me") | {"id": "snapshot-id"}])
assert "@stuck:snapshot:one" in greenlight.pinged()
greenlight.close_stale([])
assert "@stuck:snapshot:one" not in greenlight.pinged()
with (greenlight.STATE / "outbox.jsonl").open("ab") as interrupted:
    interrupted.write(b'{"text":"incomplete"')
greenlight.outbox({"text": "after partial append"})
assert greenlight.outbox_events()[-1]["text"] == "after partial append"

# --- GitHub repos: projectId forms, the PR → MR-shape adapter, gh commands, stacked PRs, lookup, cleanup ---
assert greenlight.forge_project("github:octo/greenlight") == ("github", "octo/greenlight")
assert greenlight.forge_project("git:github.com/octo/greenlight") == ("github", "octo/greenlight")
assert greenlight.forge_project("git:gitlab.com/acme/ops/relay") == ("gitlab", "acme/ops/relay")
assert greenlight.forge_project("repo:e749bc1b") is None and greenlight.forge_project(None) is None

def gh_raw(**kw):
    """A canned `gh api graphql` answer for one PR (viewer = octo)."""
    pr = {"number": 23, "url": "https://github.com/octo/greenlight/pull/23", "title": "Console button", "state": "OPEN",
          "isDraft": False, "updatedAt": "2026-11-04T21:09:04Z", "author": {"login": "octo"},
          "headRefName": "octo/plan-x", "headRefOid": "abc", "baseRefName": "main", "mergeable": "MERGEABLE",
          "mergeStateStatus": "CLEAN", "reviewDecision": None, "labels": {"nodes": [{"name": n} for n in ("ui", "reviewed", "tried")]}, "milestone": None,
          "assignees": {"nodes": []}, "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "bob"}}]},
          "latestOpinionatedReviews": {"nodes": []}, "reviewThreads": {"nodes": []},
          "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}} | kw
    return json.dumps({"data": {"viewer": {"login": "octo"},
                                "repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": pr}}})

def gh_pr(**kw):
    with mock.patch.object(greenlight, "run", return_value=gh_raw(**kw)):
        return greenlight.gh_pr("octo/greenlight", 23)

p = gh_pr()
assert p["author"] == {"username": greenlight.ME, "login": "octo", "avatar_url": None} and p["state"] == "opened" and p["iid"] == 23
assert p["head_pipeline"] is None and p["detailed_merge_status"] == "mergeable" and not p["_can_approve"]
assert p["target_branch"] == "main" and p["_default_branch"] == "main" and p["labels"] == ["ui", "reviewed", "tried"]
assert p["reviewers"] == [{"username": "bob"}]
assert gh_pr(isDraft=True)["detailed_merge_status"] == "draft_status"
assert gh_pr(state="MERGED")["state"] == "merged" and gh_pr(state="CLOSED")["state"] == "closed"
assert gh_pr(mergeable="CONFLICTING", mergeStateStatus="DIRTY")["has_conflicts"]
assert gh_pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")["detailed_merge_status"] == "checking"
assert gh_pr(reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED")["_approvals_left"] == 1
roll = lambda state, *ctx: {"nodes": [{"commit": {"statusCheckRollup": {"state": state, "contexts": {"nodes": list(ctx)}}}}]}
run_ = lambda name, status, concl=None: {"__typename": "CheckRun", "name": name, "status": status, "conclusion": concl,
                                         "checkSuite": {"workflowRun": {"workflow": {"name": "test"}}}}
p = gh_pr(commits=roll("FAILURE", run_("unit", "COMPLETED", "FAILURE"), run_("lint", "COMPLETED", "SUCCESS")))
assert p["head_pipeline"] == {"status": "failed", "web_url": "https://github.com/octo/greenlight/pull/23/checks"}
assert [(c["name"], c["status"], c["stage"]) for c in p["_checks"]] == [("unit", "failed", "test"), ("lint", "success", "test")]
assert gh_pr(commits=roll("PENDING", run_("unit", "IN_PROGRESS")))["head_pipeline"]["status"] == "running"
# review threads: first and last commenter, with the gh viewer as ME
th = lambda first, last, done=False: {"isResolved": done, "first": {"nodes": [{"author": {"login": first}}]},
                                      "last": {"nodes": [{"author": {"login": last}}]}}
p = gh_pr(reviewThreads={"nodes": [th("alice", "alice"), th("alice", "octo"), th("alice", "alice", True)]})
assert len(greenlight.unresolved(p)) == 2 and not p["blocking_discussions_resolved"]
assert [d["notes"][-1]["author"]["username"] for d in greenlight.unresolved(p)] == ["alice", greenlight.ME]
# {threads} and {failed_jobs}: who, where, the first line; GitHub's failed checks with their links
p = gh_pr(reviewThreads={"nodes": [{"isResolved": False, "first": {"nodes": [{"author": {"login": "alice"}, "body": "\n Why this?\nmore", "path": "a.py", "line": 3}]},
                                    "last": {"nodes": [{"author": {"login": "alice"}}]}}]})
assert greenlight.thread_line(greenlight.unresolved(p)[0]) == 'alice, a.py:3: "Why this?"'
assert greenlight.thread_line({"notes": [{"author": {"username": "bob"}, "body": "", "position": None}]}) == "bob"
p = gh_pr(commits=roll("FAILURE", {**run_("unit", "COMPLETED", "FAILURE"), "detailsUrl": "https://x/1"}, run_("lint", "COMPLETED", "SUCCESS")))
assert [(j["name"], j["url"]) for j in p["_failed_jobs"]] == [("unit", "https://x/1")]
p = gh_pr(latestOpinionatedReviews={"nodes": [{"state": "APPROVED", "author": {"login": "alice"}},
                                              {"state": "CHANGES_REQUESTED", "author": {"login": "bob"}}]})
assert p["_approved_by"] == ["alice"] and {"username": "alice"} in p["reviewers"]

# lifecycle on GitHub: gh commands, solo repo asks me to merge, stacked PRs never auto-merge into their base
gws = lambda goal="Merge MR for #20", project="octo/greenlight": {"project": project, "forge": "github", "goal": goal, "branch": "b"}
ev = lambda m, w=None: greenlight.evaluate(w or gws(), m, None)
e = ev(gh_pr(isDraft=True))
assert (e["step"], e["owner"], e["cmd"]) == ("ready", "greenlight", ["gh", "pr", "ready", "23", "-R", "octo/greenlight"])
assert e["lifecycle"] == "greenlight.md"
assert (ev(gh_pr(isDraft=True), gws("Merge MR for #20 (stay draft)"))["owner"]) == "me"
import audit
with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}) as mv:
    e = ev(gh_pr())
assert (e["step"], e["owner"], e["cmd"][2:9], e["cmd"][9:]) == ("merge", "greenlight", ["checked-merge", "github", "octo/greenlight", "23", "main", "abc", "base1"],
                                                                          ["gh", "pr", "merge", "23", "-R", "octo/greenlight", "--merge"])  # live target checked, head pinned by wrapper
assert mv.call_count == 1 and mv.call_args.args[1]["sha"] == "abc"  # after the semantic audit of the head passed
approved = gh_pr(latestOpinionatedReviews={"nodes": [{"state": "APPROVED", "author": {"login": "alice"}}]})
with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}) as mv:
    assert ev(approved)["cmd"][2] == "checked-merge" and mv.call_count == 1  # approval does not bypass semantic audit
with mock.patch.object(audit, "merge_verdict", return_value={"ok": False, "failed": [], "pending": True}):
    assert ev(approved)["owner"] == "ci"
old_config = greenlight.CONFIG
greenlight.CONFIG = {**old_config, "require_human_approval": False}
with mock.patch.object(audit, "merge_verdict", return_value={"ok": False, "failed": [], "pending": True}):
    assert ev(gh_pr())["owner"] == "ci"  # disabling approval still keeps the audit gate
greenlight.CONFIG = old_config
with mock.patch.object(audit, "merge_verdict", return_value={"ok": False, "failed": [], "pending": True}):
    e = ev(gh_pr())  # the audit runs in the background: the scan doesn't wait on it, and nothing merges meanwhile
assert (e["owner"], e["cmd"][:3]) == ("ci", ["gh", "pr", "merge"]) and e["action"] == "automerge waits on the semantic audit of abc", e
_blk = {"ok": False, "failed": [{"check": "tests_not_weakened", "contract": "GenuineFix", "p_broken": 0.93}]}
with mock.patch.object(audit, "merge_verdict", return_value=_blk):
    e = ev(gh_pr())  # a broken contract holds the automerge for the user, with the merge cmd attached and the contract named
assert (e["owner"], e["cmd"][:3], e["cmd"][-2:]) == ("me", ["gh", "pr", "merge"], ["--match-head-commit", "abc"]) \
    and "GenuineFix (tests_not_weakened, p=0.93)" in e["action"], e
with mock.patch.object(audit, "merge_verdict", return_value={"ok": False, "failed": [], "error": "RuntimeError: no diff"}):
    assert ev(gh_pr())["owner"] == "me"  # an audit that couldn't run holds it too
e = ev(gh_pr(labels={"nodes": [{"name": "tried"}]}))
assert (e["step"], e["owner"]) == ("reviewed", "agent") and "`reviewed`" in e["action"]  # no merge before the three reviews
assert ev(gh_pr(labels={"nodes": [{"name": "reviewed"}]}))["step"] == "tried"
with mock.patch.object(greenlight, "lifecycle_opts", return_value={"solo"}):
    assert ev(gh_pr())["owner"] == "me"  # solo without automerge: the user reviews, then merges
with mock.patch.object(greenlight, "lifecycle_opts", return_value={"automerge"}):
    assert ev(gh_pr(), gws(project="someone/tool"))["owner"] == "reviewers"  # automerge with no checking steps (default.md) merges nothing
assert ev(gh_pr(), gws("Merge MR for #20 (hold)"))["action"].endswith("merge now?")
with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}):
    e = ev(gh_pr(latestOpinionatedReviews={"nodes": [{"state": "APPROVED", "author": {"login": "alice"}}]}), gws(project="someone/tool"))
assert (e["owner"], e["cmd"][2], e["lifecycle"]) == ("greenlight", "checked-merge", "default.md")  # approved default merge is audited too
with mock.patch.object(audit, "merge_verdict", return_value={"ok": False, "failed": [], "pending": True}):
    assert ev(gh_pr(latestOpinionatedReviews={"nodes": [{"state": "APPROVED", "author": {"login": "alice"}}]}), gws(project="someone/tool"))["owner"] == "ci"
with mock.patch.object(audit, "merge_verdict", return_value={"ok": False, "failed": [], "error": "model unavailable"}):
    assert ev(gh_pr(latestOpinionatedReviews={"nodes": [{"state": "APPROVED", "author": {"login": "alice"}}]}), gws(project="someone/tool"))["owner"] == "me"
assert ev(gh_pr(), gws(project="someone/tool"))["owner"] == "reviewers"  # no human approval, not solo
stacked = gh_pr(baseRefName="plan-view") | {"_base_mr": 13}
assert (ev(stacked)["owner"], ev(stacked)["action"]) == ("dependency", "targets plan-view: waiting on #13 to merge first")
assert ev(gh_pr(baseRefName="plan-view"))["owner"] == "me" and not ev(gh_pr(baseRefName="plan-view")).get("cmd")
with mock.patch.object(greenlight, "lifecycle_opts", return_value={"merge_any_base"}):
    with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}):
        assert ev(gh_pr(baseRefName="plan-view", latestOpinionatedReviews={"nodes": [{"state": "APPROVED", "author": {"login": "a"}}]}))["owner"] == "greenlight"
assert (ev(gh_pr(commits=roll("FAILURE")))["step"], ev(gh_pr(commits=roll("FAILURE")))["owner"]) == ("ci_green", "agent")
assert ev(gh_pr(mergeable="CONFLICTING", mergeStateStatus="DIRTY"))["step"] == "no_conflicts"
assert ev(gh_pr(reviewThreads={"nodes": [th("alice", "alice")]}))["step"] == "threads"
assert greenlight.post_merge("octo/greenlight") == []  # greenlight.md has no deploy: nothing comes after the merge
e = ev(gh_pr(state="MERGED"))
assert (e["step"], e["owner"], e.get("cmd")) == ("done", "done", None), e  # merged is done, and no sign-off is asked
assert ev(gh_pr(state="CLOSED"))["owner"] == "done"
assert ev(gh_pr(author={"login": "alice"}))["action"] == "review it"  # someone else's PR is a review
assert ev(gh_pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"))["owner"] == "ci"
assert ev(gh_pr(mergeStateStatus="BLOCKED"))["owner"] == "me"
# GitLab rows keep glab commands
assert greenlight.evaluate(ws(), mr(draft=True), None)["cmd"][:3] == ["glab", "mr", "update"]

# scan: GitHub workspaces are tracked; the PR comes from Orca's linked PR, else the head branch,
# else the open PR closing the goal's issue, else the newest PR of any state on the branch
def wt(name, goal, branch=None, linked=None, issue=None):
    return {"worktreeId": name, "id": name, "displayName": name, "path": "/nonexistent/" + name, "repoId": "gh",
            "branch": "refs/heads/" + (branch or name), "comment": goal, "linkedPR": linked, "linkedIssue": issue, "agents": []}
WTS = [wt("ws-linked", "Merge MR for #14", linked=17),
       wt("ws-linked-obj", "Merge MR for #39", linked={"number": 39, "url": "https://github.com/octo/greenlight/pull/39"}), wt("ws-branch", "Merge MR for #21"),
       wt("ws-issue", "Merge MR for #20", branch="renamed"), wt("ws-old", "Merge MR for #9"),
       wt("ws-none", "Merge MR for #30"),
       {"worktreeId": "gl", "displayName": "gl", "path": "/x", "repoId": "other", "branch": "refs/heads/x", "comment": "y", "agents": []}]
FULL = [{"id": "gh", "repoId": "gh", "projectId": "github:octo/greenlight"},
        {"id": "other", "repoId": "other", "projectId": "repo:e749bc1b"}]
def fake_orca(*a):
    if a[:2] == ("worktree", "ps"):
        return {"worktrees": WTS}
    if a[:2] == ("worktree", "list"):
        return {"worktrees": FULL}
    return {"terminals": []}
OPEN = [{"number": 22, "headRefName": "ws-branch", "closingIssuesReferences": []},
        {"number": 23, "headRefName": "elsewhere", "closingIssuesReferences": [{"number": 20}]},
        {"number": 13, "headRefName": "plan-view", "closingIssuesReferences": []}]
def fake_pr(project, n, forge="gitlab"):
    assert forge == "github" and project == "octo/greenlight" and isinstance(n, int), n  # gh rejects `$n: Int!` = a dict
    return gh_pr(number=n, state="MERGED" if n == 9 else "OPEN", baseRefName="plan-view" if n == 22 else "main")
def fake_gh(*a):
    return [{"number": 9}] if "ws-old" in a else []
(greenlight.STATE / "last.json").unlink(missing_ok=True)  # the precheck test above leaves a non-JSON snapshot
with mock.patch.object(greenlight, "orca", side_effect=fake_orca), mock.patch.object(greenlight, "gh_open_prs", return_value=OPEN), \
        mock.patch.object(greenlight, "mr_details", side_effect=fake_pr), mock.patch.object(greenlight, "gh", side_effect=fake_gh), \
        mock.patch.object(greenlight, "gl", side_effect=AssertionError("GitHub rows must not call GitLab")):
    res = greenlight.scan(save=False)
    WTS[2]["comment"] = ""
    greenlight.backfills_update(lambda d: d.__setitem__("ws-branch", {"comment": "", "branch": "ws-branch", "linked_mr": None,
                                                            "resolved_mr": 21, "goal": "Merge #21: old PR", "forge": "github", "project": "octo/greenlight"}))
    replaced = greenlight.scan(save=False)
    assert next(r for r in replaced["rows"] if r["name"] == "ws-branch")["goal"] == ""
    (greenlight.STATE / "last.json").write_text(json.dumps({"scan_started_ns": 9999999999999999999, "rows": []}))
    with mock.patch.object(greenlight, "keep", return_value=False):
        greenlight.scan(save=True)
    assert greenlight.backfills_load()["ws-branch"]["resolved_mr"] == 21  # rejected stale scan cannot prune it
    WTS[2]["comment"] = "Merge MR for #21"
    greenlight.backfills_update(lambda d: d.pop("ws-branch", None))
by = {r["name"]: r for r in res["rows"]}
assert res["skipped"] == 1 and set(by) == {"ws-linked", "ws-linked-obj", "ws-branch", "ws-issue", "ws-old", "ws-none"}
assert {n: r["mr"] and r["mr"]["ref"] for n, r in by.items()} == {"ws-linked": "#17", "ws-linked-obj": "#39", "ws-branch": "#22", "ws-issue": "#23",
                                                                   "ws-old": "#9", "ws-none": None}
assert by["ws-branch"]["next"]["owner"] == "dependency" and "#13" in by["ws-branch"]["next"]["action"]  # stacked on plan-view
assert by["ws-old"]["next"]["step"] == "done" and by["ws-none"]["next"]["step"] == "freeform"
assert by["ws-issue"]["mr"]["forge"] == "github" and by["ws-issue"]["mr"]["author"] == "octo"
assert by["ws-issue"]["project"] == "octo/greenlight" and by["ws-issue"]["forge"] == "github"
page = greenlight.report(res)
assert ">#23</a>" in page and "Waiting on another MR/PR" in page and "!23" not in page

# cleanup: a merged or closed GitHub PR finishes the workspace like a merged/closed MR; an open one keeps it
import cleanup
repo = cleanup.Repo.__new__(cleanup.Repo)
repo.path, repo.error, repo.host, repo.project, repo.base, repo.default = "/r", None, "github.com", "octo/greenlight", "origin/main", "main"
def gh_cleanup(state, linked=None):
    pr = {"number": 23, "state": state, "headRefOid": "tip", "author": {"login": "octo"}, "latestReviews": []}
    def fake_run(cmd, cwd=None, timeout=60, check=True):
        if cmd[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(cmd, 0, '{"login": "octo"}', "")
        if cmd[0] == "gh":  # the linked PR is viewed; the branch is always looked up too (a newer PR may exist)
            assert cmd[2] == "list" or linked, cmd
            return subprocess.CompletedProcess(cmd, 0, json.dumps(pr if cmd[2] == "view" else [pr]), "")
        out = {"config": "refs/heads/b", "rev-parse": "tip", "status": ""}.get(cmd[3], "")
        return subprocess.CompletedProcess(cmd, 0, out, "")
    with mock.patch.object(cleanup, "run", side_effect=fake_run), mock.patch.object(cleanup.os.path, "isdir", return_value=True):
        return cleanup.evaluate({"id": "w", "path": "/w", "branch": "refs/heads/b", "linkedPR": linked}, repo, set())
with mock.patch.object(greenlight, "post_merge", return_value=[]):  # a lifecycle with nothing after merge
    assert gh_cleanup("MERGED") == ("candidate", "MR #23 merged")
    assert gh_cleanup("MERGED", linked=23) == ("candidate", "MR #23 merged")
assert gh_cleanup("CLOSED") == ("candidate", "MR #23 closed")
assert cleanup.mr_numbers({"linkedPR": {"number": 39}}, "b", repo) == [39]  # newer Orca links a PR as an object
assert gh_cleanup("OPEN")[0] == "keep"
# a merged PR whose lifecycle has steps after merge stays until greenlight's scan says they're done
with mock.patch.object(greenlight, "post_merge", return_value=["deploy"]):
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": [{"path": "/w", "next": {"step": "deploy", "owner": "me"}}]}))
    assert gh_cleanup("MERGED") == ("keep", "MR #23 merged, still to do: deploy")
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": []}))  # not scanned since the merge: keep
    assert gh_cleanup("MERGED") == ("keep", "MR #23 merged, still to do: deploy")
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": [{"path": "/w", "next": {"step": "done", "owner": "done"}}]}))
    assert gh_cleanup("MERGED") == ("candidate", "MR #23 merged")
print("github ok")

# --- config.json knobs
(greenlight.STATE / "config.json").write_text('{"merge_method": "squash"}')
assert greenlight.config()["merge_method"] == "squash" and greenlight.config()["agents"] == ["claude", "codex"]
cfg, greenlight.CONFIG = greenlight.CONFIG, greenlight.config()
with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}):
    assert greenlight.evaluate(ws(), mr(), None)["cmd"][-1] == "--squash"
greenlight.BOT = None  # no review bot: the step passes
with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}):
    assert nxt(mr(assignees=[], reviewers=[])) == ("merge", "greenlight")
greenlight.CONFIG = {**greenlight.CONFIG, "require_human_approval": False}
with mock.patch.object(audit, "merge_verdict", return_value={"ok": True, "failed": [], "target_oid": "base1"}):
    assert nxt(mr(_approved_by=[])) == ("merge", "greenlight")
greenlight.CONFIG, greenlight.BOT = cfg, "review-bot"
(greenlight.STATE / "lifecycles/opts.md").write_text("Options: `solo` `milestone=v`\n")
assert greenlight.lifecycle_opts("x/opts") == {"solo": "", "milestone": "v"}
# GitHub release-train repo: the review bot and milestone steps edit the PR with gh
gw = {"project": "octo/widgets", "forge": "github", "goal": "", "branch": "b"}
shutil.copy(greenlight.ROOT / "examples/lifecycles/release-train.md", greenlight.STATE / "lifecycles/widgets.md")
e = greenlight.evaluate(gw, mr(assignees=[]), {"title": "Release-9.9.9"})
assert e["cmd"] == ["gh", "pr", "edit", "1", "-R", "octo/widgets", "--add-assignee", "review-bot", "--add-reviewer", "review-bot"], e
e = greenlight.evaluate(gw, mr(milestone=None), {"title": "Release-9.9.9"})
assert e["cmd"] == ["gh", "pr", "edit", "1", "-R", "octo/widgets", "--milestone", "Release-9.9.9"], e
with mock.patch.object(greenlight, "gh", return_value=[{"title": "Release-1.2.3", "due_on": "2033-01-02T00:00:00Z"},
                                                  {"title": "Backlog", "due_on": None}]):
    assert greenlight.release_milestone("octo/widgets", "github")["title"] == "Release-1.2.3"
print("config ok")
# --- custom stages: a label, or the user's sign-off (remembered per MR); the doc round-trips through the writer
doc = greenlight.lifecycle_doc("acme/release-train")
doc = {**doc, "stages": [{"key": "ci_green", "req": "green"}, {"key": "qa", "req": "QA\npassed", "done": "label=qa-ok"},
                         {"key": "security", "req": "Security looked.", "done": "signoff"}, {"key": "merge", "req": ""}],
       "options": {"solo": "", "milestone": "v"}, "rules": "1. `nope` never a stage.\nNo force-push."}
(greenlight.STATE / "lifecycles/custom.md").write_text(greenlight.lifecycle_md("custom", doc))
back = greenlight.lifecycle_doc("acme/custom")
assert [(s["key"], s["done"]) for s in back["stages"]] == [("ci_green", ""), ("qa", "label=qa-ok"), ("security", "signoff"), ("merge", "")], back
assert back["stages"][1]["req"] == "QA passed" and back["options"] == doc["options"] and back["rules"] == doc["rules"] and back["source"] == "yours"
cw = {"project": "acme/custom", "goal": "", "branch": "b"}
assert greenlight.evaluate(cw, mr(labels=[]), None)["step"] == "qa"
e = greenlight.evaluate(cw, mr(labels=["qa-ok"]), None)
assert (e["step"], e["owner"], e["cmd"][-3:]) == ("security", "me", ["acme/custom", "1", "security"]), e
with mock.patch.object(sys, "argv", ["greenlight.py"] + e["cmd"][2:]):
    greenlight.main()
assert greenlight.evaluate(cw, mr(labels=["qa-ok"]), None)["step"] == "merge"
for bad in [{"stages": [{"key": "a b"}]}, {"stages": [{"key": "x"}, {"key": "x"}]}, {"stages": [{"key": "x", "done": "label=`"}]}]:
    try:
        greenlight.lifecycle_md("r", bad); raise AssertionError(bad)
    except ValueError:
        pass
# steps after `merge` run once the MR has merged (your own ones only); a built-in there is refused by the writer
(greenlight.STATE / "lifecycles/post.md").write_text(greenlight.lifecycle_md("post", {**doc, "stages": doc["stages"] + [
    {"key": "deploy", "req": "Deployed.", "done": "signoff", "run": "make  deploy"}]}))
assert greenlight.lifecycle_doc("acme/post")["stages"][-1] == {"key": "deploy", "req": "Deployed.", "done": "signoff", "run": "make deploy"}
pw = {"project": "acme/post", "goal": "", "branch": "b"}
assert greenlight.split_merge(["a", "merge", "deploy"]) == (["a", "merge"], ["deploy"]) and greenlight.post_merge("acme/post") == ["deploy"]
assert greenlight.evaluate(pw, mr(labels=["qa-ok"]), None)["step"] == "security"  # open: deploy isn't checked yet
e = greenlight.evaluate(pw, mr(state="merged"), None)
assert (e["step"], e["owner"], e["cmd"][-1]) == ("deploy", "me", "deploy"), e
with mock.patch.object(sys, "argv", ["greenlight.py"] + e["cmd"][2:]):
    greenlight.main()
assert greenlight.evaluate(pw, mr(state="merged"), None)["step"] == "done"
assert greenlight.evaluate(pw, mr(state="closed"), None)["step"] == "done"  # closed unmerged: nothing to deploy
try:
    greenlight.lifecycle_md("r", {"stages": [{"key": "merge"}, {"key": "ci_green"}]}); raise AssertionError("built-in after merge")
except ValueError:
    pass
print("custom stages ok")


# Read-only requests get answered, not a workspace (the gpt-live-24h-cost bug; tests/test_chat.py runs it end to end)
asks = ["How much has GPT-Live cost us in the last 24 hours?", "what's !101's status", "Is !42 merged?",
        "Check the 24h GPT-Live spend", "why is CI red on #12?", "what's running", "look up our OpenAI bill",
        "Does !42 have conflicts?", "What's the status of fix-001?", "Can you check the 24h spend?",
        "Could you tell me why CI is red?", "Please check the 24h GPT-Live spend", "What did gpt-live-24h-cost find?",
        "Can you check if the token expired?", "Why is it so slow?", "Is it also failing on main?", "Is CI ok?",
        "Could you check the 24h spend, please?", "What are the cost and latency of GPT-Live?", "Compare GPT-Live and Realtime costs", "List my open MRs", "How much did GPT-Live cost?" + greenlight.ASKED + "Last 24h:\n- **Realtime sessions**: $9\n- **Agent runs**: $3\n- **Test calls**: $0.40\n\nWhich account?" + greenlight.ANSWER + "prod", "How much did GPT-Live cost?" + greenlight.ASKED + "Here's the split:\n- Agent runs: $3\n- Deploy: 4 min\nAnything else?" + greenlight.ANSWER + "no",
        "How much did GPT-Live cost?" + greenlight.ASKED + "Breakdown:\n- Test runs cost $3\n- Review bot cost $2\nWhich account?" + greenlight.ANSWER + "last week", "How much did GPT-Live cost?" + greenlight.ASKED + "Breakdown:\n- Agent runs $3\n- Workspace hosting $2\nWant the daily split?" + greenlight.ANSWER + "last week",
        "How much did GPT-Live cost?" + greenlight.ASKED + "Costs by item:\n- Build minutes $4\n- Release jobs $1\nWhich week?" + greenlight.ANSWER + "last week", "How much did GPT-Live cost?" + greenlight.ASKED + "Would you like me to check staging too?" + greenlight.ANSWER + "yes", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I look at fix-001 or fix-002?" + greenlight.ANSWER + "the first", "How much did GPT-Live cost?" + greenlight.ASKED + "Last 24h by line item:\n- Realtime sessions: $9\n- Agent runs: $3\n- Test calls: $0.40\n\nWhich account do you mean?" + greenlight.ANSWER + "prod",
        "How much did GPT-Live cost?" + greenlight.ASKED + "It cost $12. The spike started after Tuesday's deploy.\n\nWhich account?\n`Options: prod | staging`" + greenlight.ANSWER + "prod",
        "How much did GPT-Live cost?" + greenlight.ASKED + "I can see $12 in prod since the deploy.\n\nDo you also want staging?" + greenlight.ANSWER + "prod",
        "Check the 24h spend and tell me why it spiked", "What did fix-001 and fix-002 find?", "What is !42 and !43 status?", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I look at prod or staging?" + greenlight.ANSWER + "prod", "What docs cover the API?",
        "How much has GPT-Live cost us in the last 24 hours?" + greenlight.ASKED + "Do you mean prod or staging?" + greenlight.ANSWER + "prod",
        "How much did GPT-Live cost?" + greenlight.ASKED + "Which account should I use?\n`Options: prod | staging`" + greenlight.ANSWER + "prod",
        "How much did GPT-Live cost?" + greenlight.ASKED + "Prod or staging?" + greenlight.ANSWER + "prod" + greenlight.ASKED + "Which window?" + greenlight.ANSWER + "the last 24h"]
# wrongly refusing a change is the worse mistake: requests, answers and anything unclear may create
changes = ["Add a 24h cost meter", "fix the failing CI on #12", "Can you rebase !42?", "Merge !42", "do it", "go ahead",
           "check the spend and then update the dashboard", "/greenlight (automation run: nobody is watching, don't ask questions)",
           '(Replying to your question: "Workspace for !55 is gone; recreate?")\nyes', "yes", "",
           '(Replying to your question: "Two options:\n- recreate it ("fresh")\n- or keep it?")\nyes',
           '(Replying to your notification: "Automation x failed")\nlook into it and fix it', "Create a workspace to check the bill",
           "Can you take over !88?", "Could you get !42 green?", "Can we get this merged?", "Can you finish !42?",
           "Can you address the review comments on !42?", "Could you respond to the review?", "Can you port auto.py to Swift?",
           "Should we wire the cost API into dash.py?", "Can you file an issue for it?", "Do the same for !43",
           "Show the 24h cost on the dashboard", "Show me the 24h cost on the dashboard", "Why is CI red on #12? Get an agent on it.", "Can you review !42?",
           "Look into the keychain prompts and sort it out", "yes please, how long will it take?",
           "Check for flaky tests and get them fixed", "Check the security tracker and triage new issues",
           "Can you grant a teammate access to the dashboard?", "Why did CI fail on !42? Re-run it.", "Is !42 merged? If so auto-merge !43", "What is the 24h cost? e-mail it to me", "Have codex look at !42", "Check off the completed lifecycle step for !42", "Please list our app on Product Hunt", "Who's on call? Page them.", "Is the token expired? Roll it.",
           "Check whether the cert expired and renew it", "Has anyone started on the cost meter? Spin one up otherwise",
           "What about adding a cost meter to the dashboard?", "Is !42 on main? cherry-pick it onto release-1.2",
           "How much did GPT-Live cost?" + greenlight.ANSWER + "sure", "How much did GPT-Live cost?" + greenlight.ASKED + "Want me to add a cost meter to the dashboard?" + greenlight.ANSWER + "the dashboard",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Anything else?" + greenlight.ANSWER + "put a cost meter on the dashboard",
           "Check in the hotfix to main", "Check the invoice and refund the customer", "Check the invoice and refund Alice", "Check the invoice and refund $10", "Can you check !42 and sign off?", "How much did GPT-Live cost?" + greenlight.ASKED + "Which?\n1. Add a meter $2/mo\n2. Skip" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Which?\n1. Add it: $2/mo\n2. Skip" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Which?\n1. **Fix it**: recommended\n2. Skip" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Which?\n1. **Add a meter**: $2/mo\n2. Skip" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I:\n1. Add a cost meter: $2/mo\n2. File an issue: free\nWhich?" + greenlight.ANSWER + "1",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Should I:\n- add a meter: cheap\n- skip it\nWhich one?" + greenlight.ANSWER + "the first",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Options:\n1. Add a cost meter (recommended): cheap\n2. Skip\nWhich?" + greenlight.ANSWER + "1",
           "How much did GPT-Live cost?" + greenlight.ASKED + "It was $12.\n\n1. **Add a cost meter** to the dashboard\n2. **File an issue**\n\nWhich would you like?" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Which would you like?\n1. Add a cost meter: dashboard\n2. Skip" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I edit greenlight.py?" + greenlight.ANSWER + "the config", "How much did GPT-Live cost?" + greenlight.ASKED + "Next steps:\n- add a meter\n- file an issue\nWhich?" + greenlight.ANSWER + "the first", "How much did GPT-Live cost?" + greenlight.ASKED + "Add a cost meter to the dashboard?" + greenlight.ANSWER + "yes", "How much did GPT-Live cost?" + greenlight.ASKED + "Fix it now?" + greenlight.ANSWER + "ok",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Should a cost alert be set up?" + greenlight.ANSWER + "sure", "How much did GPT-Live cost?" + greenlight.ASKED + "Would you like a cost meter on the dashboard?" + greenlight.ANSWER + "yes",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Next steps?\n- add a meter\n- file an issue" + greenlight.ANSWER + "the first", "How much did GPT-Live cost?" + greenlight.ASKED + "Which would you like?\n1. Add a cost meter\n2. File an issue" + greenlight.ANSWER + "1",
           "How much did GPT-Live cost?" + greenlight.ASKED + "I can:\n\n1. add a meter\n2. file an issue" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Options\n1. add a meter\n2. skip" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I merge !42?" + greenlight.ANSWER + "!42", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I deploy v1.2 or v1.3?" + greenlight.ANSWER + "v1.3",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Should I set up a $12.50 alert or a $20 one?" + greenlight.ANSWER + "the second", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I:\n- add a meter\n- skip it\nWhich one?" + greenlight.ANSWER + "the first",
           "How much did GPT-Live cost?" + greenlight.ASKED + "I can:\n1. add a meter\n2. file an issue\n\nWhich?" + greenlight.ANSWER + "1", "How much did GPT-Live cost?" + greenlight.ASKED + "Want that on the dashboard?" + greenlight.ANSWER + "the dashboard",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Should I set an alert at $50?" + greenlight.ANSWER + "go for it", "Summarize the changes in the MR description",
           "Summarize it in the changelog", "Summarize the incident in a report", "Summarize it in a Google Doc", "How much did GPT-Live cost?" + greenlight.ASKED + "Can I add a cost meter?" + greenlight.ANSWER + "the dashboard", "Check CI and open !42", "Check the logs and open PRs for the fixes", "Explain the architecture in our README",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Add a cost meter?\nOptions: Dashboard | Weekly report | No" + greenlight.ANSWER + "Dashboard",
           "How much did GPT-Live cost?" + greenlight.ASKED + "`Options: add a meter | skip`" + greenlight.ANSWER + "first one",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Should I file an issue for it?" + greenlight.ANSWER + "the backlog",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Want a cost meter on the dashboard?" + greenlight.ANSWER + "the dashboard", "Summarize the incident in the docs",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Which?\nOptions: Add a cost meter | Skip" + greenlight.ANSWER + "the first",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Should I set up an alert?" + greenlight.ANSWER + "prod",
           "How much did GPT-Live cost?" + greenlight.ASKED + "I can add a cost meter.\n\nWhere should it go?" + greenlight.ANSWER + "dashboard", "How much did GPT-Live cost?" + greenlight.ASKED + "Should I add a meter for it?" + greenlight.ANSWER + "the dashboard", "What is the 24h cost, the dashboard needs a meter for it", "Why is CI red on #12, I want it green before lunch",
           "How much did GPT-Live cost?" + greenlight.ASKED + "Want me to add a cost meter to the dashboard?\nOptions: Dashboard | Weekly report | No thanks" + greenlight.ANSWER + "Dashboard", "How much did GPT-Live cost?" + greenlight.ASKED + "I can add a cost meter.\nWhere should it go?" + greenlight.ANSWER + "dashboard", "Check out a new branch for the cost meter", "Is it possible to put a meter on the dashboard?",
           "Summarize the incident in a doc", "Explain the architecture in the README", "How much did GPT-Live cost?" + greenlight.ANSWER + "Have codex look into it", "Investigate the flake and put up an MR", "Who should review !42? Spin up a workspace for it"]
for t in asks:
    assert greenlight.read_only(t), t
for t in changes:
    assert not greenlight.read_only(t), t
os.environ["GREENLIGHT_REQUEST"] = "How much did GPT-Live cost in the last 24h?"
assert greenlight.create_refused(["worktree", "create", "--name", "gpt-live-24h-cost"])
assert not greenlight.create_refused(["worktree", "create", "--name", "x", "--change", "adds the cost meter"])  # the model's override
assert not greenlight.create_refused(["worktree", "create", "--change=adds it"]) and greenlight.without_change(["--change=adds it", "--name", "x"]) == ["--name", "x"]
assert greenlight.without_change(["worktree", "create", "--change", "why", "--name", "x"]) == ["worktree", "create", "--name", "x"]
assert not greenlight.create_refused(["worktree", "ps"])  # reading state is the answer
assert not greenlight.create_refused(["agent", "launch", "--worktree", "id:w1"])
os.environ["GREENLIGHT_REQUEST"] = "Add a 24h cost meter"
assert not greenlight.create_refused(["worktree", "create", "--name", "cost-meter"])
del os.environ["GREENLIGHT_REQUEST"]  # no request (a person at a terminal, adopt.py): never refused
assert not greenlight.create_refused(["worktree", "create", "--name", "x"])
print("read-only ok")
