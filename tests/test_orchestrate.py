import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
if __name__ != "__main__":
    import pytest
    pytest.skip("standalone test script; CI runs it in its own process", allow_module_level=True)
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo", GREENLIGHT_MEMORY=tempfile.mkdtemp())  # never the real ~/.greenlight
import contextlib, io, json, pathlib, subprocess, time, types
from unittest import mock
import greenlight, ws_state, orchestrate

HOME, MEM = pathlib.Path(os.environ["GREENLIGHT_HOME"]), pathlib.Path(os.environ["GREENLIGHT_MEMORY"])
greenlight.CONFIG["agents"] = ["claude", "codex"]
real_greenlight_ping = greenlight.ping
live_current_mr_state = orchestrate.current_mr_state
orchestrate.current_mr_state = orchestrate.scanned_mr_state
live_branch_has_open_mr = orchestrate.branch_has_open_mr
orchestrate.branch_has_open_mr = lambda r: False


def row(name, step="ci_green", owner="agent", action="Fix CI (pipeline failed)", mr=True, **kw):
    r = {"id": f"id-{name}", "name": name, "path": kw.pop("path", "/nonexistent"), "branch": name, "project": "octo/app", "forge": "github",
         "goal": kw.pop("goal", f"Merge #1: {name}"), "comment": kw.pop("comment", ""), "linked_mr": 1,
         "agent": kw.pop("agent", {"state": "none", "type": None, "last": ""}),
         "next": {"step": step, "owner": owner, "action": action, **kw.pop("nxt", {})}, "life": {"at": "2.1"}}
    if mr:
        r["mr"] = {"iid": 1, "ref": "#1", "web_url": "https://example.test/pr/1", "title": name, "state": "opened", "role": "author",
                   "sha": kw.pop("sha", "abc"), "pipeline": "failed", "failed_jobs": [{"name": "unit", "stage": "test", "url": "u"}],
                   "conflicts": False, "threads": 0, "todo_threads": [], "source_branch": name, "target_branch": "main"} | kw.pop("mrx", {})
    return r | kw


milestone_row = row("milestone", step="milestone", owner="greenlight", nxt={"cmd": ["gh", "pr", "edit", "1", "--milestone", "Release-1.2.3"]})
with mock.patch.object(greenlight, "mr_details", return_value={"sha": "abc", "target_branch": "main"}), \
        mock.patch.object(greenlight, "lifecycle", return_value=("default", ["milestone"])), \
        mock.patch.object(greenlight, "release_milestone", return_value={"title": "Release-1.2.3"}) as release, \
        mock.patch.object(greenlight, "evaluate", side_effect=lambda r, m, candidate: {"step": "milestone", "owner": "greenlight", "action": r["next"]["action"], "cmd": ["gh", "pr", "edit", "1", "--milestone", candidate["title"]]}):
    assert live_current_mr_state(milestone_row) == orchestrate.scanned_mr_state(milestone_row)
    release.assert_called_once_with("octo/app", "github")
    changed_action = milestone_row | {"next": milestone_row["next"] | {"action": "set a different milestone"}}
    assert live_current_mr_state(changed_action) != orchestrate.scanned_mr_state(milestone_row)
thread_checked = row("live-threads", step="threads", mrx={"threads_fingerprint": "old-thread"})
with mock.patch.object(greenlight, "mr_details", return_value={"sha": "abc", "target_branch": "main"}), \
        mock.patch.object(greenlight, "lifecycle", return_value=("default", [])), \
        mock.patch.object(greenlight, "evaluate", return_value=thread_checked["next"]), \
        mock.patch.object(greenlight, "unresolved", return_value=[{"id": "new-thread"}]), \
        mock.patch.object(greenlight, "thread_line", return_value="new-thread"):
    assert live_current_mr_state(thread_checked) != orchestrate.scanned_mr_state(thread_checked)
with mock.patch.object(greenlight, "gh", return_value=[{"number": 2}]):
    assert live_branch_has_open_mr(row("new-pr", mr=False, path="/missing"))
overlaid = row("overlaid", action="Fix CI [claude out of tokens: dispatch with codex]", nxt={"agent": "codex"})
overlaid["_base_next"] = overlaid["next"] | {"action": "Fix CI"}
with mock.patch.object(greenlight, "mr_details", return_value={"sha": "abc", "target_branch": "main"}), \
        mock.patch.object(greenlight, "lifecycle", return_value=("default", [])), \
        mock.patch.object(greenlight, "evaluate", return_value={"step": "ci_green", "owner": "agent", "action": "Fix CI"}):
    assert live_current_mr_state(overlaid) == orchestrate.scanned_mr_state(overlaid)


# ---- cards: observe at transitions, agent notes, decisions, the brief

preview_state = pathlib.Path(tempfile.mkdtemp())
with mock.patch.object(ws_state, "DIR", preview_state):
    legacy = row("preview-legacy", mr=False)
    ws_state.path("preview-legacy").write_text(json.dumps({"id": legacy["id"], "ws": "preview-legacy"}))
    assert orchestrate.plan([legacy], {}, judge=False)
    assert legacy["card"] == "preview-legacy"
    assert not list(preview_state.glob(".owner-*"))
    assert not (preview_state / ".owners-migrated").exists()
    ws_state.keyed([row("preview-legacy", mr=False)])
    assert ws_state.owner_file(legacy["id"]).exists()

git = lambda p, *a: subprocess.run(["git", "-C", str(p), *a], check=True, capture_output=True)
repo = pathlib.Path(tempfile.mkdtemp())
git(repo, "init", "-q", "-b", "main"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
(repo / "a.py").write_text("1"); git(repo, "add", "."); git(repo, "commit", "-qm", "a")
git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
git(repo, "checkout", "-qb", "feat"); (repo / "b.py").write_text("2"); git(repo, "add", "."); git(repo, "commit", "-qm", "b")
(repo / "c.py").write_text("wip")

(HOME / "decisions.jsonl").write_text(json.dumps({"workspace": "feat", "worktree_id": "id-feat", "mr": "#1", "target_branch": "main", "question": "Keep it a draft?", "answer": "no, ship it", "answered": 5}) + "\n"
                                       + json.dumps({"workspace": "other", "question": "x?", "answer": "y", "answered": 6}) + "\n")
r = row("feat", path=str(repo), agent={"state": "done", "type": "claude", "last": "I think the flake is the cache key."})
c, moved = ws_state.observe(r, "Merge #1 — done when #1 merges")
assert moved == ("", "2.1 ci_green/agent"), moved
assert c["touched_files"] == ["b.py", "c.py"], c["touched_files"]  # the branch's commits against origin/main, and uncommitted files
assert c["current_hypothesis"] == "I think the flake is the cache key." and c["open_risks"] == ["CI failed: unit"], c
assert c["decisions_made"] == ["Keep it a draft? → no, ship it"], c["decisions_made"]  # only this workspace's answers
assert not ws_state.answered([{"worktree_id": "id-feat", "mr": "#1", "target_branch": "main", "question": "Merge?", "answer": "yes", "answered": 5}],
                             row("feat", mrx={"target_branch": "release"}), "feat")  # same head, different merge target
assert c["goal_contract"] == "Merge #1 — done when #1 merges"
assert "done when #1 merges" in ws_state.contract(row("manual"))
title_hold = row("title-hold", goal="Merge #1: Hold inbound transactions", mrx={"title": "Hold inbound transactions"})
assert "goal says hold: no merge" not in ws_state.risks(title_hold)
assert "goal says hold: no merge" in ws_state.risks(row("suffix-hold", goal="Merge !12: Hold until QA signs off", mrx={"title": "Hold"}))
assert "goal says hold: no merge" in ws_state.risks(row("edited-title-hold", goal="Merge !66: Fix parser (hold until QA)", mrx={"title": "Fix parser (hold until QA)"}))
assert ws_state.tokens("#7") == ["7"]
ws_state.observe(row("ref-only", goal="Fix startup timeout", mrx={"iid": 7, "ref": "#7"}))
assert any(h["name"] == "ref-only" for h in ws_state.top("#7", 5, kinds=("card",)))
c, moved = ws_state.observe(r)
assert moved is None and len(c["transitions"]) == 1  # no transition, no new entry
ws_state.update("feat", hypothesis="It's the lockfile, not the cache.", failed=["bumping the cache key"], risk="release freeze Friday")
c, _ = ws_state.observe(r)
assert c["current_hypothesis"] == "It's the lockfile, not the cache.", c  # the agent's own note holds while the stage does
assert "release freeze Friday" in c["open_risks"] and "CI failed: unit" in c["open_risks"]
c, moved = ws_state.observe(row("feat", step="threads", action="Resolve MR Threads (2 unresolved)", path=str(repo),
                                agent={"state": "done", "type": "claude", "last": "Threads next."}, mrx={"pipeline": "success", "threads": 2}))
assert moved == ("2.1 ci_green/agent", "2.1 threads/agent") and c["current_hypothesis"] == "Threads next.", c  # a new stage: the last message again
assert c["open_risks"] == ["2 unresolved thread(s)", "release freeze Friday"], c["open_risks"]
b = ws_state.brief(c)
assert "Failed approaches (don't repeat them):\n  - bumping the cache key" in b and "Touched files: b.py, c.py" in b and "ws_state.py set feat" in b, b
c, _ = ws_state.observe(row("bare", step="automation", owner="me", action="automation run with no MR: delete?", mr=False, path=str(repo)))
assert c["touched_files"] == ["b.py", "c.py"] and c["last_stage"] == "2.1 automation/me", c  # regression: a row with no MR (KeyError target_branch)
ws_state.update("feat", decision="Keep the old API until migration")
assert "Keep the old API until migration" in ws_state.observe(r, recs=[])[0]["decisions_made"]
(repo / "space name.py").write_text("new")
assert "space name.py" in ws_state.observe(r, recs=[])[0]["touched_files"]  # same stage and SHA, new dirty file
git(repo, "add", "space name.py"); git(repo, "commit", "-qm", "spaced path")
assert "space name.py" in ws_state.touched(r)  # committed paths use NUL separators too
with mock.patch.object(ws_state, "git", return_value=""):
    assert ws_state.touched(r, ["b.py"]) == ["b.py"]  # missing base ref cannot erase prior committed paths
local_repo = pathlib.Path(tempfile.mkdtemp())
git(local_repo, "init", "-q", "-b", "topic")
(local_repo / "untracked.txt").write_text("draft")
assert "untracked.txt" in ws_state.touched(row("local-only", mr=False, path=str(local_repo)))
local_row = row("local-only", mr=False, path=str(local_repo))
ws_state.observe(local_row)
(local_repo / "untracked.txt").unlink()
ws_state.observe(local_row)
assert "untracked.txt" not in ws_state.load("local-only")["touched_files"]
(local_repo / "new").mkdir()
(local_repo / "new" / "a.py").write_text("a")
(local_repo / "new" / "b.py").write_text("b")
assert {"new/a.py", "new/b.py"}.issubset(set(ws_state.touched(local_row)))
def overlapping_git(p, *args):
    if args[0] == "rev-parse":
        return "base" if overlap_base[0] else ""
    if args[0] == "diff":
        return "a.py\0"
    return " M a.py\0" if overlap_dirty[0] else ""
overlap_base, overlap_dirty = [True], [True]
with mock.patch.object(ws_state, "git", side_effect=overlapping_git):
    both = ws_state.touched(row("overlap", path=str(local_repo)), with_dirty=True)
    overlap_base[0], overlap_dirty[0] = False, False
    assert ws_state.touched(row("overlap", path=str(local_repo)), both[0], both[2], both[1]) == ["a.py"]
ci_state = row("ci-state")
ws_state.observe(ci_state)
ws_state.update("ci-state", hypothesis="old CI diagnosis")
ci_card = ws_state.load("ci-state")
ci_card["dispatches"] = [{"sig": "ci_green@abc", "ts": 1, "agent": "claude", "judged": True}]
ci_card["forks"] = {"ci_green@abc": {"choice": 2, "done": True}}
ws_state.save(ci_card)
missing_jobs = row("ci-state", mrx={"failed_jobs": []})
ws_state.observe(missing_jobs)
assert len(ws_state.load("ci-state")["dispatches"]) == 1 and ws_state.load("ci-state")["forks"]
new_failure = row("ci-state", mrx={"failed_jobs": [{"name": "integration", "stage": "test"}]})
ws_state.observe(new_failure)
assert len(ws_state.load("ci-state")["dispatches"]) == 1 and not ws_state.load("ci-state")["forks"]
ws_state.save(ws_state.load("ci-state") | {"forks": {"ci_green@abc": {"choice": "ask the user", "done": True}}})
ws_state.update("ci-state", failed="old PR fix", risk="old PR risk", decision="old PR decision")
ws_state.observe(row("ci-state", mrx={"iid": 2}), recs=[])
assert not ws_state.load("ci-state")["forks"] and not ws_state.load("ci-state")["dispatches"]  # a different PR starts fresh
assert "old PR fix" not in ws_state.load("ci-state")["failed_approaches"] and "old PR risk" not in ws_state.load("ci-state")["open_risks"]
assert "old PR fix" in ws_state.load("ci-state")["mr_archive"]["github:octo/app:1:main"]["failed_approaches"]
before_missing = ws_state.load("ci-state")
ws_state.observe(row("ci-state", mr=False, step="error", owner="me"), recs=[])
assert ws_state.load("ci-state")["last_stage"] == before_missing["last_stage"]  # missing MR scan does not erase the previous request
missing_row = row("ci-state", mr=False, step="error", owner="me")
ws_state.observe(missing_row, recs=[])
assert orchestrate.plan([missing_row], {"ci-state": ws_state.load("ci-state")})[0]["do"] == "skip"
ws_state.observe(row("ci-state", mr=False, step="freeform"), recs=[])
assert ws_state.load("ci-state")["last_stage"].endswith("freeform/agent")  # confirmed absence may advance
legacy = {"workspace": "ci-state", "repo": "octo/app", "mr": "#1", "question": "Keep draft?", "answer": "yes"}
assert not ws_state.answered([legacy], ci_state, "ci-state")  # name-only history cannot prove worktree ownership
assert not ws_state.answered([legacy], row("ci-state", mrx={"ref": "#2"}), "ci-state")
legacy_multi = {"workspaces": [legacy], "question": "Keep draft?", "answer": "yes"}
assert not ws_state.answered([legacy_multi], ci_state, "ci-state")
identified_old_mr = {"worktree_id": ci_state["id"], "mr": "#1", "question": "Ship #1?", "answer": "yes"}
assert not ws_state.answered([identified_old_mr], row("ci-state", mrx={"ref": "#2"}), "ci-state")
assert not ws_state.answered([{"worktree_id": ci_state["id"], "mr": None, "answer": "yes"}], ci_state, "ci-state")
freeform_legacy = {"workspace": "free", "repo": "octo/app", "mr": None, "step": "freeform", "question": "Proceed?", "answer": "yes"}
assert not ws_state.answered([freeform_legacy], row("free", mr=False, step="freeform"), "free")
old_sha = row("sha-state")
ws_state.observe(old_sha)
sha_card = ws_state.load("sha-state"); sha_card["forks"] = {"ci_green@abc": {"choice": "ask the user", "done": True}}; ws_state.save(sha_card)
new_sha = row("sha-state", sha="new-sha")
ws_state.observe(new_sha)
assert new_sha["_state_reset"] and not ws_state.load("sha-state")["forks"]
ws_state.observe(row("ci-state", step="threads", mrx={"pipeline": "success", "threads": 1}), recs=[])
assert not ws_state.load("ci-state")["current_hypothesis"]
rename_old = row("before-rename", mr=False); rename_old["id"] = "rename-id"
ws_state.observe(rename_old); ws_state.update("before-rename", failed="keep this history")
rename_new = row("after-rename", mr=False, goal=rename_old["goal"], branch=rename_old["branch"]); rename_new["id"] = "rename-id"
renamed, _ = ws_state.observe(rename_new)
assert rename_new["card"] == "before-rename" and "keep this history" in renamed["failed_approaches"]
assert "after-rename" in ws_state.text_of(renamed)
large_card = ws_state.load("before-rename")
large_card["touched_committed"] = [f"committed-{i}" for i in range(90)]
large_card["touched_dirty"] = [f"dirty-{i}" for i in range(90)]
ws_state.save(large_card)
assert len(ws_state.load("before-rename")["touched_committed"]) == 40 and len(ws_state.load("before-rename")["touched_dirty"]) == 40
owner_pointer = ws_state.owner_file("rename-id")
before_pointer = owner_pointer.stat().st_mtime_ns
ws_state.observe(rename_new)
assert owner_pointer.stat().st_mtime_ns == before_pointer  # a routine refresh leaves the owner pointer alone
stale_agent = {"state": "done", "type": "codex", "last": "CI cache is broken"}
ws_state.observe(row("old-message", agent=stale_agent))
ws_state.observe(row("old-message", step="threads", agent=stale_agent, mrx={"pipeline": "success", "threads": 1}))
assert not ws_state.load("old-message")["current_hypothesis"]
first_pr = row("first-pr", mr=False, step="freeform")
ws_state.observe(first_pr); ws_state.update("first-pr", decision="keep API", failed="old approach", risk="release freeze")
ws_state.observe(row("first-pr"), recs=[])
first_card = ws_state.load("first-pr")
assert "keep API" in first_card["decisions_made"] and "old approach" in first_card["failed_approaches"] and "release freeze" in first_card["open_risks"]
ws_state.observe(first_pr, recs=[])
assert "old approach" in ws_state.load("first-pr")["failed_approaches"]
project_a = row("project-swap", mrx={"iid": 12, "ref": "#12"})
ws_state.observe(project_a); ws_state.update("project-swap", decision="org a only", failed="old repo fix")
project_b = row("project-swap", project="org/b", mrx={"iid": 12, "ref": "#12"})
ws_state.observe(project_b, recs=[{"worktree_id": project_a["id"], "repo": "octo/app", "mr": "#12", "question": "old repo?", "answer": "yes"}])
assert "org a only" not in ws_state.load("project-swap")["decisions_made"]
assert "old repo? → yes" not in ws_state.load("project-swap")["decisions_made"]
assert "old repo fix" not in ws_state.load("project-swap")["failed_approaches"]
goal_one = row("pr-goal", goal="Mark ready")
ws_state.observe(goal_one)
ws_state.update("pr-goal", decision="yes to ready")
goal_two = row("pr-goal", goal="Keep draft")
ws_state.observe(goal_two, recs=[{"worktree_id": goal_one["id"], "repo": goal_one["project"], "mr": "#1", "goal": "Mark ready", "question": "Ready?", "answer": "yes"}])
assert "yes to ready" not in ws_state.load("pr-goal")["decisions_made"]
assert "Ready? → yes" not in ws_state.load("pr-goal")["decisions_made"]
ws_state.observe(goal_one, recs=[])
assert "yes to ready" in ws_state.load("pr-goal")["decisions_made"]
ws_state.observe(row("pr-goal", goal="other request", mrx={"iid": 2, "ref": "#2"}), recs=[])
ws_state.observe(goal_two, recs=[])
assert "yes to ready" not in ws_state.load("pr-goal")["decisions_made"]
free_a = row("goal-swap", mr=False, step="freeform", goal="Fix CI")
ws_state.observe(free_a); ws_state.update("goal-swap", decision="CI only", failed="old fix", risk="CI risk")
free_b = row("goal-swap", mr=False, step="freeform", goal="Write docs")
ws_state.observe(free_b)
assert "CI only" not in ws_state.load("goal-swap")["decisions_made"] and "old fix" not in ws_state.load("goal-swap")["failed_approaches"]
ws_state.observe(free_a)
assert "CI only" in ws_state.load("goal-swap")["decisions_made"] and "old fix" in ws_state.load("goal-swap")["failed_approaches"]
other_branch = free_a | {"branch": "feature-b"}
ws_state.observe(other_branch, recs=[])
assert "CI only" not in ws_state.load("goal-swap")["decisions_made"]
ws_state.observe(free_a, recs=[])
assert "CI only" in ws_state.load("goal-swap")["decisions_made"]
branch_answer = {"worktree_id": free_a["id"], "repo": free_a["project"], "mr": None, "branch": free_a["branch"],
                 "goal": free_a["goal"], "question": "Use staging?", "answer": "yes"}
assert ws_state.answered([branch_answer], free_a, "goal-swap")
assert not ws_state.answered([branch_answer], other_branch, "goal-swap")
ws_state.observe(row("goal-swap", goal="Merge #3: new task", mrx={"iid": 3, "ref": "#3"}), recs=[])
assert "CI only" not in ws_state.load("goal-swap")["decisions_made"]
ws_state.observe(free_a, recs=[])
assert "CI only" in ws_state.load("goal-swap")["decisions_made"]
fresh_scan = row("scan-generation", mr=False, step="freeform", goal="current") | {"_scan_id": "current-scan"}
(HOME / "last.json").write_text(json.dumps({"scan_id": "current-scan", "rows": [fresh_scan]}))
(HOME / "scan_id").write_text(f"current-scan {(HOME / 'last.json').stat().st_ino}")
ws_state.observe(fresh_scan)
(HOME / "last.json").write_text(json.dumps({"scan_id": "newer-scan", "rows": []}))
(HOME / "scan_id").write_text(f"newer-scan {(HOME / 'last.json').stat().st_ino}")
try:
    ws_state.observe(fresh_scan)
    assert False, "an older scan must not overwrite a newer card"
except RuntimeError as e:
    assert "superseded" in str(e)
(HOME / "last.json").write_text(json.dumps({"scan_id": "current-scan", "rows": [fresh_scan]}))
ws_state.observe(fresh_scan)  # interrupted publication of the newer marker leaves the complete snapshot usable
docs_answer = {"worktree_id": free_a["id"], "mr": None, "goal": "Write docs", "question": "Docs first?", "answer": "yes", "answered": int(time.time())}
assert not ws_state.answered([docs_answer], free_a, "goal-swap", allow_untagged=False)
archive_a = row("archive")
ws_state.observe(archive_a); ws_state.update("archive", decision="PR1 only", failed="PR1 failed fix")
ws_state.observe(row("archive", mrx={"iid": 2, "ref": "#2"}), recs=[])
assert "PR1 only" not in ws_state.load("archive")["decisions_made"]
ws_state.observe(archive_a, recs=[])
assert "PR1 only" in ws_state.load("archive")["decisions_made"] and "PR1 failed fix" in ws_state.load("archive")["failed_approaches"]
assert not ws_state.load("archive")["forks"] and not ws_state.load("archive")["dispatches"]
old_token = ws_state.situation_token(ws_state.load("archive"))
ws_state.observe(row("archive", mrx={"iid": 2, "ref": "#2"}), recs=[])
try:
    ws_state.update("archive", if_situation=old_token, risk="late PR1 note")
    raise AssertionError("late agent wrote into a relinked card")
except SystemExit:
    pass
assert "late PR1 note" not in ws_state.load("archive")["noted_risks"]
pending_relink = row("pending-relink")
ws_state.observe(pending_relink)
pending_card = ws_state.load("pending-relink"); pending_card["dispatches"] = [{"sig": "ci_green@abc", "ts": 1, "pending": True,
    "assignment": ws_state.situation_token(pending_card)}]; ws_state.save(pending_card)
relinked_card = ws_state.observe(row("pending-relink", mrx={"iid": 2, "ref": "#2"}), recs=[])[0]
assert relinked_card["dispatches"][0]["pending"]
owner_row = row("owner-transition")
ws_state.observe(owner_row)
assignment_token = ws_state.situation_token(ws_state.load("owner-transition"))
ws_state.observe(row("owner-transition", owner="working"))
ws_state.update("owner-transition", if_situation=assignment_token, failed="agent's in-progress note")
assert "agent's in-progress note" in ws_state.load("owner-transition")["failed_approaches"]
ping_row = row("manual-ping")
ws_state.observe(ping_row)
ping_card = ws_state.load("manual-ping"); ping_card["forks"] = {"ci_green@abc": {"choice": "ask the user", "ping_key": "@stuck:manual-ping:old"}}; ws_state.save(ping_card)
next_ping = row("manual-ping", step="threads")
ws_state.observe(next_ping)
with mock.patch.object(greenlight, "pinged", return_value={"@stuck:manual-ping:old": 1}), mock.patch.object(greenlight, "resolve") as resolved:
    orchestrate.phase_cards([next_ping])
    resolved.assert_called_once()
assert not ws_state.load("manual-ping").get("stale_pings")
token_row = row("token-stable")
ws_state.observe(token_row)
assignment_token = ws_state.situation_token(ws_state.load("token-stable"))
ws_state.observe(row("token-stable", sha="pushed-sha"))
assert ws_state.situation_token(ws_state.load("token-stable")) == assignment_token  # push stays in the assignment
ws_state.observe(row("token-stable", branch="other-branch"))
assert ws_state.situation_token(ws_state.load("token-stable")) != assignment_token
backfill_card = row("backfill-card", goal="")
ws_state.observe(backfill_card)
backfill_token = ws_state.situation_token(ws_state.load("backfill-card"))
saved = ws_state.load("backfill-card"); saved["forks"] = {"ci_green@abc": {"choice": "ask the user", "done": True}}; ws_state.save(saved)
backfill_card["goal"], backfill_card["_goal_backfill"] = "Merge #1: generated", True
ws_state.observe(backfill_card)
assert ws_state.situation_token(ws_state.load("backfill-card")) == backfill_token
assert ws_state.load("backfill-card")["forks"]  # generated goal after a crash cannot erase leave-it
(HOME / "last.json").write_text(json.dumps({"rows": [row("backfill-card", goal="")]}))
refresh_calls = []
ws_state.observe_all(scan=lambda save: (refresh_calls.append(save), {"rows": [backfill_card]})[1])
assert ws_state.load("backfill-card")["forks"]  # manual refresh uses live overlay, not stale last.json
assert refresh_calls == [True]  # publish a generation so an older manual refresh cannot overwrite a newer tick
dup_a, dup_b = row("shared", mr=False, goal="A"), row("shared", mr=False, goal="B")
dup_a.update(id="a-id", project="octo/a")
dup_b.update(id="b-id", project="octo/a")  # even the same project must get separate cards
ws_state.observe(dup_a)
ws_state.update("shared", failed="A-only failure")
dups, _ = orchestrate.phase_cards([dup_a, dup_b])
assert len(dups) == 2 and "A-only failure" in dups[dup_a["card"]]["failed_approaches"]
assert "A-only failure" not in dups[dup_b["card"]]["failed_approaches"]
assert ws_state.path("shared").exists() and ws_state.load("shared")["id"] == "a-id"  # the running A agent keeps its bare key
race_b = row("card-race", mr=False); race_b["id"] = "race-b"
ws_state.keyed([race_b])
assert race_b["card"] == "card-race"
ws_state.save(ws_state.blank("card-race") | {"id": "race-a"})  # older observer claimed the key after B's scan
race_b["_scan_id"] = "race-scan"
(HOME / "last.json").write_text(json.dumps({"scan_id": "race-scan", "rows": [race_b]}))
(HOME / "scan_id").write_text(f"race-scan {(HOME / 'last.json').stat().st_ino}")
ws_state.observe(race_b, recs=[])
assert race_b["card"] != "card-race"
assert json.loads((HOME / "last.json").read_text())["rows"][0]["card"] == race_b["card"]
assert (HOME / "scan_id").read_text().split() == ["race-scan", str((HOME / "last.json").stat().st_ino)]
assert orchestrate.keyed([dup_a])[0]["card"] == dup_a["card"]  # removing B must not switch A back to the old key
ws_state.update("shared", decision="A agent wrote after B appeared")
assert "A agent wrote after B appeared" in orchestrate.phase_cards([dup_a, dup_b])[0][dup_a["card"]]["decisions_made"]
answer_b = {"workspace": "shared", "worktree_id": "b-id", "branch": dup_b["branch"], "question": "Ship B?", "answer": "yes", "answered": 99}
assert "Ship B? → yes" not in ws_state.observe(dup_a, recs=[answer_b])[0]["decisions_made"]
assert "Ship B? → yes" in ws_state.observe(dup_b, recs=[answer_b])[0]["decisions_made"]
collision_owner = row("collision", mr=False); collision_owner["id"] = "old-collision"
ws_state.observe(collision_owner)
replacement = row("collision", mr=False); replacement["id"] = "new-collision"
literal = row(f"collision@id-{ws_state.digest(replacement['id'])}", mr=False)
ws_state.keyed([replacement, literal])
assert replacement["card"] != literal["card"]
assert "set 'Release Candidate'" in ws_state.brief(ws_state.blank("Release Candidate"))
assert "set 'Release Candidate' --exact" in ws_state.brief(ws_state.blank("Release Candidate"))
with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sys, "argv", ["ws_state.py", "show", "shared", "--exact"]):
    ws_state.main()
with (HOME / "decisions.jsonl").open("a") as f:
    f.write("{malformed final line\n")
assert len(ws_state.decision_records()) == 2  # a partial record must not hide earlier answers
assert ws_state.path("team/api") != ws_state.path("team__api")
old = row("reused", mr=False); old["id"] = "old-id"
ws_state.observe(old); ws_state.update("reused", failed="old worktree only")
new = row("reused", mr=False); new["id"] = "new-id"
fresh, _ = orchestrate.phase_cards([new])
assert new["card"] != "reused" and "old worktree only" not in fresh[new["card"]]["failed_approaches"]
assert not ws_state.answered([{"workspace": "reused", "question": "Old tree?", "answer": "yes"}], new, new["card"])
assert ws_state.load("reused")["id"] == "old-id"
(HOME / "last.json").write_text(json.dumps({"rows": [new]}))
assert ws_state.current_key("reused") == new["card"]  # manual brief follows the current worktree
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sys, "argv", ["ws_state.py", "set", "reused", "--risk", "current worktree risk"]):
    ws_state.main()
assert "current worktree risk" in ws_state.load(new["card"])["open_risks"] and "current worktree risk" not in ws_state.load("reused")["open_risks"]
with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sys, "argv", ["ws_state.py", "set", "reused", "--exact", "--risk", "old card risk"]):
    ws_state.main()
assert "old card risk" in ws_state.load("reused")["open_risks"] and "old card risk" not in ws_state.load(new["card"])["open_risks"]
ws_state.observe(row("cleared-goal", mr=False, goal="original"))
assert ws_state.observe(row("cleared-goal", mr=False, goal=""))[0]["goal_contract"] == ""
bad_git = row("unreadable-git", mr=False, path=tempfile.mkdtemp())
ws_state.observe(bad_git)
ws_state.update("unreadable-git", risk="keep existing files")
old_card = ws_state.load("unreadable-git"); old_card["touched_files"] = ["previous.py"]; ws_state.save(old_card)
assert ws_state.observe(bad_git)[0]["touched_files"] == ["previous.py"]
old_foo = row("foo", mr=False); old_foo["id"] = "old-foo"
ws_state.observe(old_foo)
victim = row("foo", mr=False); victim["id"] = "victim"
literal_name = f"foo@id-{ws_state.digest('victim')}"
literal = row(literal_name, mr=False); literal["id"] = "literal"
ws_state.observe(literal)
assert ws_state.observe(victim)[0]["ws"] != literal_name and ws_state.load(literal_name)["id"] == "literal"
single_a = row("sequential", mr=False); single_a["id"] = "sequential-a"
single_b = row("sequential", mr=False); single_b["id"] = "sequential-b"
ws_state.observe(single_a)
assert ws_state.observe(single_b)[0]["ws"] != "sequential"  # standalone observe also checks identity
huge = ws_state.blank("huge") | {"touched_files": [f"f{i}.py" for i in range(40)], "failed_approaches": ["x" * 500] * 6, "current_hypothesis": "y" * 600}
assert len(ws_state.brief(huge)) <= ws_state.BRIEF_CAP  # a dispatch prompt never balloons
huge["open_risks"], huge["touched_files"], huge["goal_contract"] = ["critical release hold"], ["important.py"], "g" * 2000
assert "critical release hold" in ws_state.brief(huge) and "important.py" in ws_state.brief(huge)
relink_old = row("touch-relink")
with mock.patch.object(ws_state, "touched", return_value=(["old.py"], ["old.py"], [])):
    ws_state.observe(relink_old)
relink_new = relink_old | {"mr": relink_old["mr"] | {"iid": 2, "ref": "#2"}}
with mock.patch.object(ws_state, "touched", return_value=(["new.py"], ["new.py"], [])):
    assert ws_state.observe(relink_new)[0]["touched_files"] == ["new.py"]
missing_target = relink_new | {"mr": relink_new["mr"] | {"iid": 3, "ref": "#3", "target_branch": "missing"}, "path": str(repo)}
assert "old.py" not in ws_state.observe(missing_target)[0]["touched_files"]
scope_old = row("touch-scope")
ws_state.observe(scope_old)
scope_card = ws_state.load("touch-scope"); scope_card["touched_files"] = ["old.py"]; scope_card["touched_committed"] = ["old.py"]; ws_state.save(scope_card)
target_changed = scope_old | {"mr": scope_old["mr"] | {"target_branch": "other"}}
with mock.patch.object(ws_state, "touched", return_value=None):
    assert not ws_state.observe(target_changed)[0]["touched_files"]
scope_card = ws_state.load("touch-scope"); scope_card["touched_files"] = ["old-project.py"]; scope_card["touched_committed"] = ["old-project.py"]; ws_state.save(scope_card)
other_project = target_changed | {"project": "other/app", "path": str(repo)}
assert "old-project.py" not in ws_state.observe(other_project)[0]["touched_files"]
print("cards ok")
verdict_row = row("stale-verdict", goal="first goal")
ws_state.observe(verdict_row)
old_token = ws_state.situation_token(ws_state.load("stale-verdict"))
ws_state.observe(verdict_row | {"goal": "new goal"})
assert not orchestrate.set_fork("stale-verdict", orchestrate.sig(verdict_row), {"choice": "dispatch again"}, old_token)
assert not ws_state.load("stale-verdict")["forks"]
assert orchestrate.branch_changed(verdict_row, {"branch": ""})  # detached HEAD cannot receive a launch
thread_row = row("thread-identity", step="threads", mrx={"threads": 1, "threads_fingerprint": "first"})
ws_state.observe(thread_row)
tc = ws_state.load("thread-identity"); tc["forks"][orchestrate.sig(thread_row)] = {"choice": "ask the user"}; ws_state.save(tc)
ws_state.observe(thread_row | {"mr": thread_row["mr"] | {"threads_fingerprint": "replacement"}})
assert not ws_state.load("thread-identity")["forks"]
assert not orchestrate.set_fork("thread-identity", orchestrate.sig(thread_row), {"choice": "dispatch again"}, ws_state.fork_token(tc))
retarget_row = row("target-identity", mrx={"target_branch": "main"})
target_approval = {"worktree_id": retarget_row["id"], "mr": retarget_row["mr"]["ref"], "target_branch": "main",
                   "question": "Merge this head into main?", "answer": "yes", "answered": 15}
assert any("Merge this head" in d for d in ws_state.observe(retarget_row, recs=[target_approval])[0]["decisions_made"])
retarget_card = ws_state.load("target-identity"); retarget_card["forks"][orchestrate.sig(retarget_row)] = {"choice": "ask the user", "done": True}; ws_state.save(retarget_card)
retargeted = retarget_row | {"mr": retarget_row["mr"] | {"target_branch": "release"}}
retarget_result = ws_state.observe(retargeted, recs=[target_approval])[0]
assert not retarget_result["forks"] and not any("Merge this head" in d for d in retarget_result["decisions_made"])
freeform_a = row("project-swap", mr=False, goal="Investigate launch")
ws_state.observe(freeform_a)
ws_state.update("project-swap", hypothesis="Old project assumption", failed="Old project experiment")
old_assignment = ws_state.situation_token(ws_state.load("project-swap"))
freeform_b = freeform_a | {"project": "other/app", "forge": "gitlab"}
swapped = ws_state.observe(freeform_b)[0]
assert ws_state.situation_token(swapped) != old_assignment
assert not swapped["current_hypothesis"] and not swapped["failed_approaches"]
signoff_a = row("signed-head", step="verify", owner="me", sha="head-a")
approval = {"worktree_id": signoff_a["id"], "mr": signoff_a["mr"]["ref"], "repo": signoff_a["project"], "goal": signoff_a["goal"],
            "sha": "head-a", "target_branch": signoff_a["mr"].get("target_branch"), "question": "Approve this head?", "answer": "approve", "answered": 10}
assert any("Approve this head" in d for d in ws_state.observe(signoff_a, recs=[approval])[0]["decisions_made"])
signoff_b = signoff_a | {"mr": signoff_a["mr"] | {"sha": "head-b"}}
assert not any("Approve this head" in d for d in ws_state.observe(signoff_b, recs=[approval])[0]["decisions_made"])
goal_a = row("approval-archive", goal="A", sha="head-a")
ws_state.observe(goal_a, recs=[approval | {"worktree_id": "id-approval-archive", "goal": "A"}])
ws_state.observe(goal_a | {"goal": "B"}, recs=[])
restored = ws_state.observe(goal_a | {"mr": goal_a["mr"] | {"sha": "head-b"}}, recs=[])[0]
assert not any("Approve this head" in d for d in restored["decisions_made"])
parked_pending = row("park-pending", step="ci_green", owner="parked")
ws_state.observe(parked_pending)
pc = ws_state.load("park-pending"); pc["dispatches"] = [{"ts": 1, "sig": "ci_green@abc", "step": "ci_green", "pending": True}]; ws_state.save(pc)
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": parked_pending["id"], "agents": []}):
    orchestrate.phase_cards([parked_pending])
assert ws_state.load("park-pending")["dispatches"][0]["pending"]  # unknown launch survives owner changes
assignment_row = row("assignment-change", goal="Fix CI")
ws_state.observe(assignment_row); assignment_row["card"] = "assignment-change"
assignment = orchestrate.record_dispatch(assignment_row, orchestrate.sig(assignment_row))
changed_assignment = assignment_row | {"goal": "Write docs"}
ws_state.observe(changed_assignment)
assert orchestrate.record_dispatch(assignment_row, orchestrate.sig(assignment_row), expected=assignment) is False
assert orchestrate.record_dispatch(assignment_row, orchestrate.sig(assignment_row), "codex", expected=assignment) is False
ac = ws_state.load("assignment-change"); ac["dispatches"][0]["ts"] = 1; ws_state.save(ac)
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": assignment_row["id"], "agents": []}):
    orchestrate.phase_cards([changed_assignment])
assert ws_state.load("assignment-change")["dispatches"][0]["pending"]  # old goal launch still needs a verdict
assert orchestrate.plan([changed_assignment], {"assignment-change": ws_state.load("assignment-change")})[0]["do"] == "fork"

# ---- retrieval: top-K over the index, never a full load; the index is a B-tree keyed by term

(MEM / "MEMORY.md").write_text("- index")
(MEM / "lockfile-policy.md").write_text("---\nname: lockfile-policy\ndescription: never regenerate the lockfile in a fix PR\n---\nRegenerating it hides the real change.")
for i in range(300):
    ws_state.update(f"ws-{i}", hypothesis=f"routine work item {i} on the dashboard")
hits = ws_state.top("why is feat stuck on the lockfile?", 3)
assert [h["id"] for h in hits[:2]] == ["card:feat", "note:lockfile-policy.md"] or {h["id"] for h in hits[:2]} == {"card:feat", "note:lockfile-policy.md"}, hits
assert hits[1]["text"].startswith("never regenerate the lockfile in a fix PR —") or hits[0]["text"].startswith("never"), hits
assert ws_state.top("lockfile", 5, allow={"ws-1"}) == [h for h in ws_state.top("lockfile", 5) if h["kind"] == "note"]  # a window's cards only
assert [h["name"] for h in ws_state.top("item 17", 1)] == ["ws-17"]
assert ws_state.top("the of and", 5) == []  # stopwords only: nothing, not every card
index_file = ws_state.DIR / "index.sqlite"
index_file.unlink()
(ws_state.DIR / ".index-dirty").unlink(missing_ok=True)
with mock.patch.object(ws_state, "index", side_effect=OSError("interrupted before rebuild")):
    try:
        ws_state.top("lockfile", 5)
        assert False, "the interrupted index open should fail"
    except OSError:
        pass
assert (ws_state.DIR / ".index-dirty").exists()  # next query must rebuild all cards, even if one was saved
ws_state.top("lockfile", 5)
with ws_state.index() as rebuilt:
    assert rebuilt.execute("SELECT count(*) FROM docs WHERE kind='card'").fetchone()[0] >= 300
with ws_state.index() as db:
    n, total = ws_state.stats(db)
    assert (n, total) == db.execute("SELECT count(*), sum(len) FROM docs").fetchone(), (n, total)  # the counters track every put/drop
    plan = " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN SELECT p.doc, p.tf, d.len, d.kind FROM postings p JOIN docs d ON d.id = p.doc WHERE p.term = ?", ("x",)))
    assert "SEARCH p USING PRIMARY KEY (term=?)" in plan and "SCAN" not in plan, plan  # O(log P + p_t), no table scan
    assert "SCAN" not in " ".join(r[-1] for r in db.execute("EXPLAIN QUERY PLAN SELECT count(*) FROM postings WHERE term = ?", ("x",)))
    # "dashboard" is in 300 of 302 cards: dropped when the query has a rarer word, so the rarer word decides
    assert [d for _, d in ws_state.search(db, "dashboard feat", 1)] == ["card:feat"]
(MEM / "lockfile-policy.md").unlink()
assert all(h["kind"] == "card" for h in ws_state.top("lockfile", 5))  # a deleted note leaves the index
(ws_state.DIR / "index.sqlite").unlink()
assert ws_state.top("item 17", 1)[0]["name"] == "ws-17"  # a lost index is rebuilt from the cards
(ws_state.DIR / "index.sqlite").write_bytes(b"not a sqlite database")
assert ws_state.top("item 17", 1)[0]["name"] == "ws-17"  # a corrupt index is rebuilt from JSON cards
(ws_state.DIR / "index.sqlite").write_bytes(b"still corrupt")
assert ws_state.reindex() >= 300  # manual repair uses a fresh database too
put = ws_state.put
ws_state.put = lambda *a: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("locked"))
ws_state.save(ws_state.blank("missed-index") | {"current_hypothesis": "rareuniqueword"})
ws_state.put = put
assert ws_state.top("rareuniqueword", 5)[0]["name"] == "missed-index"  # retry the missed index write
(ws_state.DIR / "index.sqlite").unlink()
ws_state.update("ws-0", hypothesis="one card rewrote a lost index")
assert ws_state.top("item 17", 1)[0]["name"] == "ws-17"  # partial rebuilt index must be completed
marker = ws_state.DIR / ".index-dirty"
marker.write_text("before")
reindex = ws_state.reindex
ws_state.reindex = lambda: (marker.write_text("after"), 0)[1]
ws_state.top("item 17", 1)
assert marker.read_text() == "after"  # a concurrent failed save keeps the dirty signal
ws_state.reindex = reindex
marker.unlink()
original_dir = ws_state.DIR
ws_state.DIR = pathlib.Path(tempfile.mkdtemp())
with ws_state.index() as db:
    for name, body in (("inside", "lockfile"), ("outside", "quasar"), ("other1", "lockfile"), ("other2", "lockfile")):
        ws_state.put(db, "card:" + name, "card", body)
    assert ws_state.search(db, "lockfile quasar", 5, allow={"inside"})[0][1] == "card:inside"
ws_state.DIR = original_dir
scoped = {}
for i in range(26):
    name = f"candidate-{i}"
    c = ws_state.blank(name) | {"id": f"id-{i}", "forge": "github", "project": "octo/app", "branch": "feat",
                                "mr_ref": "#1", "sha": "head", "goal_source": "Merge #1", "target_branch": "main",
                                "last_stage": "ci_green/agent", "current_hypothesis": "scopeuniqueterm " * (20 if i < 25 else 1)}
    ws_state.save(c)
    scoped[name] = ("github", "octo/app", "#1", "stale-head" if i < 25 else "head", "Merge #1", "main",
                    f"id-{i}", "feat", "ci_green/agent")
assert [x["name"] for x in ws_state.top("scopeuniqueterm", 5, kinds=("card",), allow=scoped)] == ["candidate-25"]
print("retrieval ok")

# ---- plan: the deterministic state machine

orchestrate.OPTS["max_dispatch"] = 2
rows = [
    row("ready", step="ready", owner="greenlight", action="mark ready for review", nxt={"cmd": ["gh", "pr", "ready", "1"]}),
    row("held", step="merge", owner="greenlight", action="merge", goal="Merge #2: x (hold for the release)", nxt={"cmd": ["gh", "pr", "merge", "2", "--merge"]}),
    row("odd", step="ready", owner="greenlight", action="?", nxt={"cmd": ["rm", "-rf", "/"]}),
    row("nogoal", step="review", owner="reviewers", action="waiting", goal="", comment="Old note: keep as draft please"),
    row("ci1"), row("ci2"), row("ci3"),
    row("asks", owner="me", action="agent is waiting on you"),
]
acts = orchestrate.plan(rows, {}, judge=False)
got = [(a["ws"], a["do"]) for a in acts]
assert got == [("ready", "cmd"), ("held", "skip"), ("odd", "skip"), ("nogoal", "goal"), ("ci1", "dispatch"), ("ci2", "dispatch"), ("ci3", "skip")], got
assert acts[3]["goal"] == "Merge #1: nogoal\n\nPrevious workspace note: Old note: keep as draft please", acts[3]
assert "cap" in acts[-1]["why"]
card_error = row("card-error")
with mock.patch.object(ws_state, "observe", side_effect=OSError("card unreadable")):
    orchestrate.phase_cards([card_error])
assert orchestrate.plan([card_error], {})[0]["do"] == "skip"
exhausted = row("exhausted", action="claude out of tokens: dispatch with codex", nxt={"agent": "codex"})
exhausted_card = ws_state.blank("exhausted")
exhausted_card["dispatches"] = [{"sig": "ci_green@abc", "agent": "claude", "step": "ci_green", "ts": 1, "judged": True}]
exhausted_card["forks"] = {"ci_green@abc": {"choice": "try next agent", "done": True}}
assert orchestrate.plan([exhausted], {"exhausted": exhausted_card})[0]["do"] == "dispatch"
exhausted_card["forks"]["ci_green@abc"] = {"choice": "ask the user", "done": True, "provider": "user"}
assert orchestrate.plan([exhausted], {"exhausted": exhausted_card})[0]["do"] != "dispatch"
assert orchestrate.failed_dispatches(exhausted, exhausted_card | {"dispatches": [{"sig": "ci_green@abc", "agent": "launch pending", "step": "ci_green", "ts": 1, "pending": True}]}) is None
print("plan ok")

# ---- act: templated dispatch with the card, failover on a usage limit, forks to decide.py

launched, pings, cmds = [], [], []
def fake_orca(*a, **kw):
    if a[:2] == ("worktree", "ps"):
        return {"worktrees": [{"worktreeId": f"id-{n}", "comment": "Old note: keep as draft please" if n == "nogoal" else "", "linkedPR": 1, "agents": []} for n in
                              ("ci1", "ci2", "ci3", "free", "held", "ready", "nogoal", "odd")]}
    if a[:2] == ("agent", "launch"):
        agent, prompt = a[a.index("--agent") + 1], a[a.index("--prompt") + 1]
        launched.append((agent, prompt))
        if agent == "claude" and limit[0]:
            raise RuntimeError("orca agent launch: You've hit your usage limit")
    return {}
limit = [True]
greenlight.orca = fake_orca
greenlight.ping = lambda text, key=None, ws=(), kind=None: (pings.append((key, kind, text)), {"asked": "test"})[1]
real_run_cmd = orchestrate.run_cmd
orchestrate.run_cmd = lambda cmd: (cmds.append(cmd), (True, ""))[1]

ci = row("ci1")
orchestrate.current_mr_state = lambda r: orchestrate.scanned_mr_state(r) | {"sha": "new-head"}
assert "MR head or lifecycle step changed" in orchestrate.act([ci], {}, orchestrate.plan([ci], {}))[0]
assert not launched
orchestrate.current_mr_state = orchestrate.scanned_mr_state
ws_state.observe(ci)
acts = orchestrate.plan([ci], {})
limit[0] = True
greenlight.CONFIG["agents"] = ["claude", "codex"]
done = orchestrate.act([ci], {}, acts)
assert [a for a, _ in launched] == ["claude", "codex"], (launched, done, limit[0], greenlight.CONFIG["agents"])  # claude out of tokens: codex takes it
prompt = launched[1][1]
assert "Fix CI" in prompt or "pipeline" in prompt.lower(), prompt[:300]  # the repo's ci prompt (prompts/ci.md), as the dashboard's button
assert "You take over from claude, which hit its usage limit" in prompt and "WORKSPACE STATE" in prompt and "CI failed: unit" in prompt, prompt[-1500:]
card = ws_state.load("ci1")
assert card["dispatches"][0]["agent"] == "codex" and card["dispatches"][0]["sig"] == "ci_green@abc"
assert "greenlight: claude hit its usage limit; handed to codex" in card["decisions_made"], card["decisions_made"]
assert done == ["dispatched codex to ci1: Fix CI (pipeline failed)"], done
hist = greenlight.history_file("ci1").read_text()
assert "dispatched codex with its state card" in hist and "dispatched codex" in (HOME / "log.jsonl").read_text()

# the same step at the same commit, after the agent stopped: a failed approach, then a fork. decide.py's local
# fallback (not configured here) is low confidence, so greenlight asks the user once instead of looping
card["dispatches"][0]["ts"] -= 600
ws_state.save(card)
ci["agent"] = {"state": "done", "type": "codex", "last": "Pushed nothing: can't reproduce."}
cards, _ = orchestrate.phase_cards([ci])
assert cards["ci1"]["failed_approaches"][0].startswith("codex dispatch for ci_green"), cards["ci1"]["failed_approaches"]
launched.clear()
acts = orchestrate.plan([ci], cards)
assert acts[0]["do"] == "fork" and acts[0]["verdict"]["choice"] == "ask the user" and acts[0]["verdict"]["provider"] == "local", acts
orchestrate.act([ci], cards, acts)
assert not launched and pings and pings[0][0].startswith("@stuck:ci1:") and pings[0][1] == "agent-stuck" and pings[0][2].rstrip().endswith("Options: dispatch again | leave it"), pings
cards, _ = orchestrate.phase_cards([ci])
acts = orchestrate.plan([ci], cards)
orchestrate.act([ci], cards, acts)
assert len(pings) == 1 and not launched, (pings, acts, ws_state.load("ci1")["forks"])  # judged once per situation
obsolete = row("obsolete-ci")
ws_state.observe(obsolete); obsolete["card"] = "obsolete-ci"
before_pings = len(pings)
ask_verdict = {"choice": "ask the user", "provider": "local", "confidence": None}
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": obsolete["id"], "branch": obsolete["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(orchestrate, "current_mr_state", return_value=orchestrate.scanned_mr_state(obsolete) | {"step": "ready"}):
    assert "lifecycle step changed" in orchestrate.act_one(obsolete, obsolete["name"], {"do": "fork", "sig": orchestrate.sig(obsolete), "verdict": ask_verdict, "why": "stuck"}, {}, peers=[obsolete])[0]
assert len(pings) == before_pings

# a configured decide.py that's sure: hand it to the next agent, with the card and a handoff note
calls = []
sys.modules["decide"] = types.SimpleNamespace(choice=lambda state, q, options, context="": (calls.append((q, options, context)), {
    "choice": options[1], "index": 1, "probs": {}, "confidence": 0.9, "provider": "haiku"})[1])
limit[0] = False
ci2 = row("ci2", sha="def")
ws_state.observe(ci2)
c2 = ws_state.load("ci2")
c2["dispatches"] = [{"ts": 1, "agent": "claude", "step": "ci_green", "sig": "ci_green@def", "judged": True}]
ws_state.save(c2)
acts = orchestrate.plan([ci2], {})
assert acts[0]["verdict"]["choice"] == "hand it to the next agent" and calls and "WORKSPACE STATE" in calls[0][2], acts
orchestrate.act([ci2], {}, acts)
assert [a for a, _ in launched] == ["codex"] and "You take over from claude, whose attempt didn't move the step" in launched[0][1], launched
assert any(d.startswith("greenlight: judged (haiku, 0.9) → hand it to the next agent") for d in ws_state.load("ci2")["decisions_made"])
# a sure verdict below the confidence floor is still the user's call
sys.modules["decide"] = types.SimpleNamespace(choice=lambda *a, **k: {"choice": "dispatch again", "index": 0, "probs": {}, "confidence": 0.5, "provider": "jev"})
ff = row("free", step="freeform", action="work toward goal: tidy docs", mr=False, goal="tidy docs", agent={"state": "done", "type": "claude", "last": "Done."})
ws_state.observe(ff)
assert orchestrate.plan([ff], {})[0]["do"] == "dispatch"  # an old agent type is not evidence it attempted this goal
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": ff["id"], "branch": ff["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(orchestrate, "branch_has_open_mr", return_value=True):
    fresh_result = orchestrate.act_one(ff, ff["name"], {"do": "dispatch", "sig": orchestrate.sig(ff)}, {})
    assert "new MR opened since scan" in fresh_result[0], fresh_result
sys.modules["decide"] = types.SimpleNamespace(choice=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")))
assert orchestrate.decide_choice({}, "q", ["a", "b"])["provider"].startswith("decide error")  # a backend error is a non-answer
del sys.modules["decide"]

# a launch that fails after a verdict leaves the verdict to act on next tick, without asking decide.py again
sys.modules["decide"] = types.SimpleNamespace(choice=lambda *a, **k: (calls.append(1), {"choice": "dispatch again", "index": 0, "probs": {}, "confidence": 0.95, "provider": "haiku"})[1])
ci3 = row("ci3", sha="ghi")
ws_state.observe(ci3)
c3 = ws_state.load("ci3")
c3["dispatches"] = [{"ts": 1, "agent": "claude", "step": "ci_green", "sig": "ci_green@ghi", "judged": True}]
ws_state.save(c3)
real_launch, launched_before, calls[:] = orchestrate.launch, len(launched), []
orchestrate.launch = lambda *a: (_ for _ in ()).throw(RuntimeError("orca: worktree not found"))
orchestrate.act([ci3], {}, orchestrate.plan([ci3], {}))
assert ws_state.load("ci3")["forks"]["ci_green@ghi"].get("done") is None and len(calls) == 1
orchestrate.launch = real_launch
orchestrate.act([ci3], {}, orchestrate.plan([ci3], {}))
assert len(calls) == 1 and len(launched) == launched_before + 1 and ws_state.load("ci3")["forks"]["ci_green@ghi"]["done"], launched  # acted on, not re-judged
del sys.modules["decide"]
st = row("stall", sha="third")
sc = ws_state.blank("stall")
sc["dispatches"] = [{"ts": 1, "agent": "codex", "step": "ci_green", "sig": "ci_green@third", "judged": True}]
sc["forks"]["ci_green@third"] = {"choice": "dispatch again", "done": True, "ts": 2}
v = orchestrate.plan([st], {"stall": sc})[0]["verdict"]
assert v["choice"] == "ask the user" and not v.get("done"), v  # judged retry that also failed must escalate
sc["forks"]["ci_green@third"] = v | {"done": True, "ping_key": "@stuck:stall:answer-test"}
with (HOME / "decisions.jsonl").open("a") as f:
    f.write(json.dumps({"key": "@stuck:stall:answer-test", "answered": int(time.time()), "choice": 0}) + "\n")
assert orchestrate.plan([st], {"stall": sc})[0]["verdict"]["choice"] == "dispatch again"  # consumes user's answer
assert orchestrate.stuck_key(st, "ci_green@third") != orchestrate.stuck_key(st, "ci_green@third")
recent = ws_state.blank("recent") | {"dispatches": [{"ts": int(time.time()), "agent": "codex", "step": "ci_green", "sig": "ci_green@abc"}]}
assert orchestrate.plan([row("recent")], {"recent": recent})[0]["do"] == "skip"  # don't judge a launch still settling
pending_old = row("pending-step", step="ci_green")
ws_state.observe(pending_old)
pc = ws_state.load("pending-step"); pc["dispatches"] = [{"ts": 1, "agent": "launch pending", "step": "ci_green", "sig": "ci_green@abc", "pending": True}]; ws_state.save(pc)
next_step = row("pending-step", step="threads", action="resolve threads")
assert ws_state.observe(next_step)[0]["dispatches"][0]["pending"]  # a step change does not prove launch rejection
held_question = row("held-question")
ws_state.observe(held_question); held_question["card"] = "held-question"
orchestrate.record_dispatch(held_question, orchestrate.sig(held_question))
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": held_question["id"], "branch": held_question["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "ping", return_value=None):
    result = orchestrate.act_one(held_question, held_question["name"], {"do": "fork", "sig": orchestrate.sig(held_question),
        "verdict": {"choice": "ask the user", "provider": "rule", "ts": 1}, "why": "uncertain launch"}, {}, peers=[held_question])
    assert "not published" in result[0] and ws_state.load("held-question")["dispatches"][0]["pending"]
saved_question = ws_state.load("held-question")["forks"][orchestrate.sig(held_question)]["ping_key"]
stale_pending = ws_state.load("held-question"); stale_pending["dispatches"][0]["ts"] = 1; ws_state.save(stale_pending)
retry_question = orchestrate.plan([held_question], {"held-question": ws_state.load("held-question")})[0]
assert retry_question["verdict"]["ping_key"] == saved_question
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": held_question["id"], "branch": held_question["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "ping", return_value=None), mock.patch.object(greenlight, "pinged", return_value={saved_question: 1}):
    assert "asked about" in orchestrate.act_one(held_question, held_question["name"], retry_question, {}, peers=[held_question])[0]
assert not ws_state.load("held-question")["dispatches"][0]["pending"]
parked_pending = row("parked-pending", step="signoff", owner="me", action="await approval")
ws_state.observe(parked_pending); parked_pending["card"] = "parked-pending"
parked_card = ws_state.load("parked-pending")
parked_card["dispatches"] = [{"ts": 1, "agent": "launch pending", "step": "ci_green", "sig": "ci_green@abc", "pending": True}]
ws_state.save(parked_card)
parked_action = orchestrate.plan([parked_pending], {"parked-pending": ws_state.load("parked-pending")})[0]
assert parked_action["do"] == "fork" and parked_action["pending_sig"] == "ci_green@abc"
nonagent = row("pending-merge", step="merge", owner="greenlight", action="merge", sha="s1",
               nxt={"cmd": ["gh", "pr", "merge", "1", "--merge"]})
ws_state.observe(nonagent); nonagent["card"] = "pending-merge"
nonagent_card = ws_state.load("pending-merge")
nonagent_card["forks"][orchestrate.sig(nonagent)] = {"awaiting_nonagent": True, "ping_key": "@stuck:pending-merge",
                                                   "ts": 1, "choice": "ask the user", "done": True}
ws_state.save(nonagent_card)
with mock.patch.object(ws_state, "decision_records", return_value=[]):
    assert orchestrate.plan([nonagent], {"pending-merge": nonagent_card})[-1]["do"] == "skip"
with mock.patch.object(ws_state, "decision_records", return_value=[{"key": "@stuck:pending-merge", "answered": 2, "choice": 1}]):
    assert orchestrate.plan([nonagent], {"pending-merge": nonagent_card})[-1]["do"] == "skip"
with mock.patch.object(ws_state, "decision_records", return_value=[{"key": "@stuck:pending-merge", "answered": 2, "choice": 0}]):
    assert orchestrate.plan([nonagent], {"pending-merge": nonagent_card})[-1]["do"] == "cmd"
with mock.patch.object(orchestrate.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as invoked:
    orchestrate.TICK_LOCK_FD = 77
    try:
        assert real_run_cmd(["gh", "pr", "edit", "1"])[0]
        assert invoked.call_args.kwargs["pass_fds"] == (77,)
    finally:
        orchestrate.TICK_LOCK_FD = None

# a merge re-reads the goal and is pinned to the scanned head commit
ps = {"worktrees": [{"worktreeId": "id-m", "comment": "", "linkedPR": 1}]}
greenlight.orca = lambda *a, **kw: ps if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
m = row("m", step="merge", owner="greenlight", action="merge", sha="s1", nxt={"cmd": ["gh", "pr", "merge", "1", "--merge"]})
checked_cmd = [sys.executable, str(pathlib.Path(greenlight.__file__).with_name("audit.py")), "checked-merge", "github", m["project"], "1", "main", "s1", "base1", "gh", "pr", "merge", "1", "-R", m["project"], "--merge"]
checked_row = m | {"next": m["next"] | {"cmd": checked_cmd}}
assert orchestrate.plan([checked_row], {}, judge=False)[0]["do"] == "cmd"
assert orchestrate.guard_merge(checked_row, checked_cmd)[0] == checked_cmd
orchestrate.act([m], {}, orchestrate.plan([m], {}))
assert cmds[-1] == ["gh", "pr", "merge", "1", "--merge", "--match-head-commit", "s1"], cmds
peer = row("peer", step="merge", owner="me", action="hold merge", comment="hold until #67 merges", sha="s1")
ps["worktrees"].append({"worktreeId": peer["id"], "comment": peer["comment"], "linkedPR": 1})
before_merge = len(cmds)
assert any("linked workspace says hold" in d for d in orchestrate.act([m, peer], {}, orchestrate.plan([m, peer], {})))
assert len(cmds) == before_merge
ps["worktrees"].pop()
ps["worktrees"].append({"worktreeId": "newly-linked", "comment": "hold until #67 merges", "linkedPR": 1})
assert "newly linked workspace says hold" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"], peers=[m])[1]
other_clone = {"worktreeId": "other-clone", "repoId": "another", "linkedPR": 1, "comment": "hold until #67 merges", "agents": []}
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": m["id"], "branch": m["branch"], "linkedPR": 1, "comment": "", "repoId": "original", "agents": []}), \
        mock.patch.object(greenlight, "orca", side_effect=lambda *a: {"worktrees": [
            {"worktreeId": m["id"], "branch": m["branch"], "linkedPR": 1, "comment": "", "repoId": "original", "agents": []}, other_clone]}
            if a[:2] == ("worktree", "ps") else {"worktrees": [{"repoId": "another", "projectId": "git:github.com/octo/app"}]}):
    assert "newly linked workspace says hold" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"], peers=[m])[1]
ps["worktrees"][-1]["linkedPR"] = {"number": 1}
assert "newly linked workspace says hold" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"], peers=[m])[1]
ps["worktrees"].pop()
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": m["id"], "branch": m["branch"], "linkedPR": 1, "comment": "", "agents": []}):
    ps["worktrees"][0]["comment"] = "hold added during final read"
    assert "goal changed during final check" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"])[1]
    ps["worktrees"][0]["comment"] = ""
    ps["worktrees"][0]["linkedPR"] = 2
    assert "linked workspace goal changed during final check" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"])[1]
    ps["worktrees"][0]["linkedPR"] = 1
ps["worktrees"][0]["agents"] = [{"state": "working"}]
assert "agent is still working" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"])[1]
ps["worktrees"][0]["agents"] = []
finished_merge = m | {"agent": {"state": "working", "type": "claude", "pane_key": "finished-pane"}}
with mock.patch.object(greenlight, "orca", return_value={"terminals": []}), mock.patch.object(greenlight, "screen", return_value={"state": "finished", "prob": 0.99, "idle_prompt": True}):
    assert not orchestrate.merge_agent_blocks({"agents": [{"state": "working", "paneKey": "finished-pane"}]}, finished_merge)
    assert orchestrate.merge_agent_blocks({"agents": [{"state": "waiting", "paneKey": "finished-pane"}]}, finished_merge)
    assert orchestrate.merge_agent_blocks({"agents": [{"state": "working", "paneKey": "other-pane"}]}, finished_merge)
def add_hold_during_forge(r):
    ps["worktrees"][0]["comment"] = "hold until #67 merges"
    return orchestrate.scanned_mr_state(r)
orchestrate.current_mr_state = add_hold_during_forge
assert "goal changed since the scan" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"])[1]
orchestrate.current_mr_state = orchestrate.scanned_mr_state
ps["worktrees"][0]["comment"] = ""
orchestrate.current_mr_state = lambda r: orchestrate.scanned_mr_state(r) | {"target_branch": "release"}
assert "lifecycle step changed" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"])[1]
orchestrate.current_mr_state = orchestrate.scanned_mr_state
late_peer = row("late-peer", step="merge", owner="me", action="merge", sha="s1")
ps["worktrees"].append({"worktreeId": late_peer["id"], "branch": late_peer["branch"], "linkedPR": 1, "comment": "", "agents": []})
def late_hold_after_forge(r):
    ps["worktrees"][-1]["comment"] = "hold until reviewed"
    return orchestrate.scanned_mr_state(r)
orchestrate.current_mr_state = late_hold_after_forge
assert "linked workspace goal changed during final check" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"], peers=[m, late_peer])[1]
orchestrate.current_mr_state = orchestrate.scanned_mr_state
ps["worktrees"].pop()
greenlight.orca = lambda *a, **kw: {"worktree": None} if a[:2] == ("worktree", "show") else ps
assert "workspace gone" in orchestrate.guard_merge(m, ["gh", "pr", "merge", "1"])[1]
greenlight.orca = lambda *a, **kw: ps if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
ps["worktrees"][0]["comment"] = "Merge #1: m (hold: wait for QA)"
n_cmds = len(cmds)
assert orchestrate.act([m], {}, orchestrate.plan([m], {})) == ["left m: the goal says hold now"] and len(cmds) == n_cmds  # a hold added since the scan
ps["worktrees"][0]["comment"] = "keep draft"
ready_m = row("m", step="ready", owner="greenlight", action="mark ready", nxt={"cmd": ["gh", "pr", "ready", "1"]})
assert orchestrate.act([ready_m], {}, orchestrate.plan([ready_m], {})) == ["left m: the goal changed since the scan"]
assert len(cmds) == n_cmds
gl = row("g", step="merge", owner="greenlight", action="merge", sha="s2", nxt={"cmd": ["glab", "mr", "merge", "1", "--yes"]}) | {"id": "id-m", "forge": "gitlab"}
ps["worktrees"][0]["comment"] = ""
ps["worktrees"][0]["linkedGitLabMR"] = 1
orchestrate.act([gl], {}, orchestrate.plan([gl], {}))
assert cmds[-1][-2:] == ["--sha", "s2"], cmds
greenlight.orca = fake_orca
cmds.clear()
live = {"worktrees": [{"worktreeId": "id-nogoal", "comment": "new user goal (hold for QA)", "linkedPR": 1, "agents": []}]}
greenlight.orca = lambda *a, **kw: live if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
assert orchestrate.act([rows[3]], {}, orchestrate.plan([rows[3]], {}, judge=False))[0].startswith("left nogoal: goal changed")
live["worktrees"] = [{"worktreeId": "id-ci1", "comment": "", "linkedPR": 1, "agents": [{"state": "working"}]}]
assert "an agent started" in orchestrate.act([ci], {}, [{"do": "dispatch", "id": ci["id"], "ws": ci["name"], "sig": "ci_green@abc"}])[0]
live["worktrees"][0]["agents"] = []
live["worktrees"][0]["comment"] = "park until next month"
assert "goal changed since scan" in orchestrate.act([ci], {}, [{"do": "dispatch", "id": ci["id"], "ws": ci["name"], "sig": "ci_green@abc"}])[0]
live["worktrees"][0].update(comment="", linkedPR=2)
assert "linked request changed" in orchestrate.act([ci], {}, [{"do": "dispatch", "id": ci["id"], "ws": ci["name"], "sig": "ci_green@abc"}])[0]
assert "linked request changed" in orchestrate.guard_merge(ci, ["gh", "pr", "merge", "1"])[1]
greenlight.orca = fake_orca

# greenlight's own steps run exactly; goal backfill goes through the ADE
ready_result = orchestrate.act(rows[:4], {}, orchestrate.plan(rows[:4], {}, judge=False))
assert not cmds and any("linked workspace says keep draft" in d for d in ready_result), (cmds, ready_result)
assert "Previous workspace note: Old note: keep as draft please" in greenlight.history_file("nogoal").read_text()
assert "Previous workspace note: Old note: keep as draft please" in ws_state.load("nogoal")["goal_contract"]
draft_ready = row("draft-ready", step="ready", owner="greenlight", action="mark ready", goal="", comment="keep as draft",
                  nxt={"cmd": ["gh", "pr", "ready", "1"]})
live_draft = {"worktreeId": draft_ready["id"], "comment": draft_ready["comment"], "linkedPR": 1, "agents": []}
def draft_orca(*args, **kw):
    if args[:2] == ("worktree", "ps"):
        return {"worktrees": [live_draft]}
    if args[:2] == ("worktree", "set"):
        live_draft["comment"] = args[args.index("--comment") + 1]
    return {}
greenlight.orca = draft_orca
assert any("says keep draft" in d for d in orchestrate.act([draft_ready], {}, orchestrate.plan([draft_ready], {}, judge=False)))
assert live_draft["comment"] == "keep as draft" and json.loads((HOME / "goal_backfill.json").read_text())[draft_ready["id"]]["goal"].startswith("Merge #1")
stale = row("stale-backfill", goal="", comment="", linked_mr=None) | {"forge": "gitlab"}
stale["card"] = "stale-backfill"
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": stale["id"], "branch": stale["branch"], "comment": ""}), \
        mock.patch.object(greenlight, "gl", return_value=[{"iid": 2}]), \
        mock.patch.object(greenlight, "orca") as set_worktree:
    assert "changed since scan" in orchestrate.act_one(stale, stale["name"], {"do": "goal", "goal": "Merge !1: stale"}, {})[0]
    set_worktree.assert_not_called()
live_link = {"worktreeId": stale["id"], "branch": stale["branch"], "comment": "", "linkedGitLabMR": None}
def link_during_lookup(*a, **kw):
    if a[:2] == ("worktree", "ps"):
        return {"worktrees": [live_link]}
    if a[:2] == ("worktree", "show"):
        return {"worktree": live_link}
    raise AssertionError("operator's new link must not be overwritten")
def lookup_then_link(*a):
    live_link["linkedGitLabMR"] = 2
    return [{"iid": 1}]
with mock.patch.object(greenlight, "orca", side_effect=link_during_lookup), mock.patch.object(greenlight, "gl", side_effect=lookup_then_link):
    assert "link or goal changed during backfill" in orchestrate.act_one(stale, stale["name"], {"do": "goal", "goal": "Merge !1: stale"}, {})[0]
greenlight.orca = fake_orca
print("act ok")

# ---- tick: phases in order, a failed scan dispatches nothing; one tick at a time
real_script = orchestrate.script
seq = []
orchestrate.script = lambda name, *a, timeout=0: (seq.append(name), (1, "boom") if name == "greenlight.py" else (0, ""))[1]
out = orchestrate.tick()
assert seq == ["adopt.py", "greenlight.py"] and out["error"].startswith("scan failed"), (seq, out)
seq.clear()
(HOME / "last.json").write_text(json.dumps({"rows": [row("ready", step="ready", owner="greenlight", action="mark ready", nxt={"cmd": ["gh", "pr", "ready", "1"]})]}))
orchestrate.script = lambda name, *a, timeout=0: (seq.append(name), (0, "- ✓ gone [b] — merged" if name == "cleanup.py" else ""))[1]
out = orchestrate.tick()
assert seq == ["adopt.py", "greenlight.py", "cleanup.py"] and out["owners"] == {"greenlight": 1}, (seq, out)
orchestrate.script = lambda name, *a, timeout=0: (1, "cleanup broke") if name == "cleanup.py" else (0, "")
assert orchestrate.tick()["error"] == "cleanup failed"  # the scheduler must report a failed run
orchestrate.script = lambda name, *a, timeout=0: (1, "adopt broke") if name == "adopt.py" else (0, "")
assert orchestrate.tick()["error"] == "adopt failed"
real_phase_cards = orchestrate.phase_cards
def broken_cards(rows):
    rows[0]["_card_error"] = "disk full"
    return {}, ["ready: card failed: disk full"]
orchestrate.phase_cards = broken_cards
orchestrate.script = lambda *a, **kw: (0, "")
assert "card refresh failed" in orchestrate.tick()["error"]
orchestrate.phase_cards = real_phase_cards
real_act = orchestrate.act
orchestrate.act = lambda *a: ["dispatch failed on ready: launch failed"]
orchestrate.script = lambda *a, **kw: (0, "")
assert "dispatch failed" in orchestrate.tick()["error"]
orchestrate.act = real_act
orchestrate.OPTS["max_dispatch"] = 1
first, second = row("cap-first"), row("cap-second", mrx={"iid": 2, "ref": "#2"}, linked_mr=2)
greenlight.orca = lambda *a, **kw: {"worktrees": [{"worktreeId": "id-cap-first", "comment": "", "linkedPR": 1, "agents": []},
                                                    {"worktreeId": "id-cap-second", "comment": "", "linkedPR": 2, "agents": []}]} if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
real_dispatch = orchestrate.dispatch
orchestrate.dispatch = lambda r, *a: (None, "launch failed") if r["name"] == "cap-first" else ("codex", "")
cap_done = orchestrate.act([first, second], {}, orchestrate.plan([first, second], {}, judge=False))
assert any("dispatch failed on cap-first" in d for d in cap_done) and any("dispatched codex to cap-second" in d for d in cap_done), cap_done
orchestrate.OPTS["max_forge_actions"] = 1
forge_acts = [{"do": "cmd", "id": first["id"], "ws": first["name"], "cmd": ["gh", "pr", "ready", "1"], "action": "ready"},
              {"do": "cmd", "id": second["id"], "ws": second["name"], "cmd": ["gh", "pr", "ready", "2"], "action": "ready"}]
with mock.patch.object(orchestrate, "act_one", return_value=["ran first"] ) as run_forge:
    forge_done = orchestrate.act([first, second], {}, forge_acts)
    assert run_forge.call_count == 1 and "forge action cap" in forge_done[-1]
with mock.patch.object(orchestrate, "act_one", side_effect=[["left cap-first: goal says hold"], ["ran cap-second"]]) as run_forge:
    forge_done = orchestrate.act([first, second], {}, forge_acts)
    assert run_forge.call_count == 2 and "ran cap-second" in forge_done
orchestrate.OPTS["max_forge_actions"] = 5
same_a, same_b = row("same-pr-a"), row("same-pr-b")
orchestrate.OPTS["max_dispatch"] = 2
with mock.patch.object(orchestrate, "dispatch", return_value=("codex", "")) as one_dispatch, \
        mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": []}):
    pair = orchestrate.act([same_a, same_b], {}, orchestrate.plan([same_a, same_b], {}, judge=False))
    assert one_dispatch.call_count == 1 and any("another workspace is acting" in d for d in pair), pair
retry_a, retry_b = row("retry-pr-a"), row("retry-pr-b")
with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": []}), \
        mock.patch.object(orchestrate, "dispatch", side_effect=lambda r, *a: (None, "rejected") if r["id"] == retry_a["id"] else ("codex", "")):
    retry_done = orchestrate.act([retry_a, retry_b], {}, orchestrate.plan([retry_a, retry_b], {}, judge=False))
    assert any("dispatch failed on retry-pr-a" in d for d in retry_done) and any("dispatched codex to retry-pr-b" in d for d in retry_done), retry_done
with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": [{"state": "working"}] if r["id"] == same_a["id"] else []}):
    assert "another workspace has an active agent" in orchestrate.act_one(same_b, same_b["name"], {"do": "dispatch", "sig": orchestrate.sig(same_b)}, {}, peers=[same_a, same_b])[0]
leave_card = ws_state.load(same_a["card"]); leave_card["forks"][orchestrate.sig(same_b)] = {"choice": "ask the user", "done": True}; ws_state.save(leave_card)
with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": []}):
    assert "user decision about this MR" in orchestrate.act_one(same_b, same_b["name"], {"do": "dispatch", "sig": orchestrate.sig(same_b)}, {}, peers=[same_a, same_b])[0]
with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": []}), \
        mock.patch.object(orchestrate, "dispatch", return_value=("codex", "")):
    approved_retry = orchestrate.act_one(same_b, same_b["name"], {"do": "fork", "sig": orchestrate.sig(same_b),
        "verdict": {"choice": "dispatch again", "provider": "user"}, "why": "user answered"}, {}, peers=[same_a, same_b])
    assert any("dispatched codex" in d for d in approved_retry), approved_retry
assert not ws_state.load(same_a["card"])["forks"].get(orchestrate.sig(same_b))
failed_retry_a, failed_retry_b = row("failed-retry-a"), row("failed-retry-b")
ws_state.observe(failed_retry_a); ws_state.observe(failed_retry_b)
failed_retry_a["card"], failed_retry_b["card"] = "failed-retry-a", "failed-retry-b"
peer_decision = ws_state.load(failed_retry_a["card"]); peer_decision["forks"][orchestrate.sig(failed_retry_b)] = {"choice": "ask the user", "done": True}; ws_state.save(peer_decision)
with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": []}), mock.patch.object(orchestrate, "dispatch", return_value=(None, "rejected")):
    rejected_retry = orchestrate.act_one(failed_retry_b, failed_retry_b["name"], {"do": "fork", "sig": orchestrate.sig(failed_retry_b),
        "verdict": {"choice": "dispatch again", "provider": "user"}, "why": "user answered"}, {}, peers=[failed_retry_a, failed_retry_b])
    assert "dispatch failed" in rejected_retry[0] and ws_state.load(failed_retry_a["card"])["forks"].get(orchestrate.sig(failed_retry_b))
done_fork, eligible = row("done-fork"), row("eligible-after-fork")
completed = [{"do": "fork", "id": done_fork["id"], "ws": done_fork["name"], "sig": orchestrate.sig(done_fork), "verdict": {"choice": "dispatch again", "done": True}},
             {"do": "dispatch", "id": eligible["id"], "ws": eligible["name"], "sig": orchestrate.sig(eligible)}]
with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None: {"worktreeId": r["id"], "branch": r["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": []}), mock.patch.object(orchestrate, "dispatch", return_value=("codex", "")):
    assert any("dispatched codex to eligible-after-fork" in d for d in orchestrate.act([done_fork, eligible], {}, completed))
new_peer = {"worktreeId": "late-peer", "linkedPR": {"number": 1}, "agents": [{"state": "working"}]}
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": same_b["id"], "branch": same_b["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": [new_peer]}):
    assert "newly linked workspace has an active agent" in orchestrate.act_one(same_b, same_b["name"], {"do": "dispatch", "sig": orchestrate.sig(same_b)}, {}, peers=[same_b])[0]
cross_clone_peer = new_peer | {"repoId": "other-clone", "projectId": "git:github.com/octo/app"}
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": same_b["id"], "branch": same_b["branch"], "linkedPR": 1, "repoId": "original", "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", side_effect=lambda *a: {"worktrees": [cross_clone_peer]} if a[:2] == ("worktree", "ps") else {"worktrees": [{"repoId": "other-clone", "projectId": "git:github.com/octo/app"}]}):
    assert "newly linked workspace has an active agent" in orchestrate.act_one(same_b, same_b["name"], {"do": "dispatch", "sig": orchestrate.sig(same_b)}, {}, peers=[same_b])[0]
fallback_row = row("fallback-live", action="claude out of tokens: dispatch with codex", nxt={"agent": "codex"}, agent={"state": "waiting", "type": "claude", "last": "limit", "pane_key": "exhausted-pane"})
ws_state.observe(fallback_row)
fallback_row["card"] = "fallback-live"
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": fallback_row["id"], "branch": fallback_row["branch"], "linkedPR": 1, "comment": "", "agents": [{"state": "waiting", "agentType": "claude", "paneKey": "exhausted-pane"}]}), \
        mock.patch.object(greenlight, "orca", side_effect=lambda *a: {"terminals": []} if a[:2] == ("terminal", "list") else {"worktrees": []}), \
        mock.patch.object(greenlight, "screen", return_value={"state": "rate_limited", "prob": 0.99}), \
        mock.patch.object(orchestrate, "dispatch", return_value=("codex", "")) as launch_fallback:
    assert orchestrate.act_one(fallback_row, fallback_row["name"], {"do": "dispatch", "sig": orchestrate.sig(fallback_row)}, {}, peers=[fallback_row])[0].startswith("dispatched codex")
    launch_fallback.assert_called_once()
assert orchestrate.active_agent_blocks({"agents": [{"state": "working", "agentType": "codex"}]}, fallback_row, True)
assert orchestrate.active_agent_blocks({"agents": [{"state": "working", "agentType": "claude", "paneKey": "new-pane"}]}, fallback_row, True)
eof_row = row("eof-launch")
ws_state.observe(eof_row); eof_row["card"] = "eof-launch"
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": eof_row["id"], "branch": eof_row["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": []}), mock.patch.object(orchestrate, "dispatch", return_value=(None, "EOF")):
    assert "dispatch failed" in orchestrate.act_one(eof_row, eof_row["name"], {"do": "dispatch", "sig": orchestrate.sig(eof_row)}, {}, peers=[eof_row])[0]
assert any(d.get("pending") for d in ws_state.load("eof-launch")["dispatches"])
import dash
with mock.patch.object(dash, "moves", return_value=[{"kind": "ci", "prompt": "ordinary fix", "auto": "Stop before push; get review"}]):
    assert "Stop before push; get review" in orchestrate.brief(row("gated", step="ci_green"), ws_state.blank("gated"))
hold_title = row("title-hold", goal="Merge !1: Hold\nHold until #67 merges", mrx={"title": "Hold"})
assert "goal says hold: no merge" in ws_state.risks(hold_title)
with mock.patch.object(greenlight, "orca", return_value={"terminals": []}), mock.patch.object(greenlight, "screen", return_value={"state": "working", "prob": 0.99}):
    assert orchestrate.active_agent_blocks({"agents": [{"state": "working", "paneKey": "exhausted-pane"}]}, fallback_row, True)
orchestrate.OPTS["max_dispatch"] = 1
orchestrate.dispatch = lambda r, *a: ("codex", "")
real_record = orchestrate.record_dispatch
record_calls = [0]
def fail_after_launch(*a, **kw):
    record_calls[0] += 1
    if record_calls[0] == 2:
        raise OSError("disk full")
    return real_record(*a, **kw)
orchestrate.record_dispatch = fail_after_launch
record_a, record_b = row("cap-record-a"), row("cap-record-b", mrx={"iid": 2, "ref": "#2"}, linked_mr=2)
greenlight.orca = lambda *a, **kw: {"worktrees": [{"worktreeId": "id-cap-record-a", "comment": "", "linkedPR": 1, "agents": []},
                                                    {"worktreeId": "id-cap-record-b", "comment": "", "linkedPR": 2, "agents": []}]} if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
cap_done = orchestrate.act([record_a, record_b], {}, orchestrate.plan([record_a, record_b], {}, judge=False))
assert any("dispatch recording failed on cap-record-a" in d for d in cap_done) and not any("dispatched codex to cap-record-b" in d for d in cap_done), cap_done
orchestrate.record_dispatch = real_record
orchestrate.dispatch = real_dispatch
timed = row("timed")
greenlight.orca = lambda *a, **kw: {"worktrees": [{"worktreeId": timed["id"], "comment": "", "linkedPR": 1, "agents": []}]} if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
orchestrate.dispatch = lambda *a: (None, "TimeoutExpired: timed out")
assert "dispatch failed on timed" in orchestrate.act([timed], {}, orchestrate.plan([timed], {}, judge=False))[0]
assert ws_state.load("timed")["dispatches"][0]["pending"]  # uncertain launch must not be retried blindly
quota_row = row("quota-rejected")
greenlight.orca = lambda *a, **kw: {"worktrees": [{"worktreeId": quota_row["id"], "comment": "", "linkedPR": 1, "agents": []}]} if a[:2] == ("worktree", "ps") else fake_orca(*a, **kw)
orchestrate.dispatch = lambda *a: (None, "usage limit reached")
assert "dispatch failed on quota-rejected" in orchestrate.act([quota_row], {}, orchestrate.plan([quota_row], {}, judge=False))[0]
assert not any(d.get("pending") for d in ws_state.load("quota-rejected")["dispatches"])
stale_failure = row("stale-failure")
ws_state.observe(stale_failure); stale_failure["card"] = "stale-failure"
def relink_during_failed_launch(*args):
    ws_state.observe(row("stale-failure", mrx={"iid": 2, "ref": "#2"}), recs=[])
    return None, "rejected"
greenlight.orca = lambda *a, **kw: {"worktrees": []}
with mock.patch.object(orchestrate, "live_worktree", return_value={"worktreeId": stale_failure["id"], "branch": stale_failure["branch"], "linkedPR": 1, "comment": "", "agents": []}), \
        mock.patch.object(orchestrate, "dispatch", side_effect=relink_during_failed_launch):
    assert "dispatch failed" in orchestrate.act_one(stale_failure, stale_failure["name"], {"do": "dispatch", "sig": orchestrate.sig(stale_failure)}, {}, peers=[stale_failure])[0]
assert not any("launch failed" in f for f in ws_state.load("stale-failure")["failed_approaches"])
orchestrate.dispatch = real_dispatch
quota_handoff = row("quota-handoff", nxt={"agent": "codex"}, agent={"state": "done", "type": "claude", "last": "usage limit"})
ws_state.observe(quota_handoff); quota_handoff["card"] = "quota-handoff"
quota_calls = []
with mock.patch.object(orchestrate, "launch", side_effect=lambda agent, *a: (quota_calls.append(agent), (_ for _ in ()).throw(RuntimeError("usage limit reached")))[1]):
    assert orchestrate.dispatch(quota_handoff, ws_state.load("quota-handoff"), assignment=ws_state.situation_token(ws_state.load("quota-handoff")),
                                 exhausted={"claude"})[0] is None
assert quota_calls == ["codex"]  # never retry the provider already known to be exhausted
quota_calls.clear()
with mock.patch.object(orchestrate, "launch", side_effect=lambda agent, *a: (quota_calls.append(agent), (_ for _ in ()).throw(RuntimeError("usage limit reached")))[1]), \
        mock.patch.object(orchestrate, "live_worktree", return_value={"comment": "new user goal", "branch": quota_handoff["branch"], "linkedPR": 1, "agents": []}):
    assert "workspace or request changed" in orchestrate.dispatch(quota_handoff, ws_state.load("quota-handoff"), first="claude",
        assignment=ws_state.situation_token(ws_state.load("quota-handoff")))[1]
assert quota_calls == ["claude"]  # recheck before the fallback launch
quota_peer = row("quota-peer")
ws_state.observe(quota_peer); quota_peer["card"] = "quota-peer"
quota_calls.clear()
def peer_started(peer, snapshot=None):
    return {"comment": "", "branch": peer["branch"], "linkedPR": 1,
            "agents": [{"state": "working"}] if peer["id"] == quota_peer["id"] else []}
with mock.patch.object(orchestrate, "launch", side_effect=lambda agent, *a: (quota_calls.append(agent), (_ for _ in ()).throw(RuntimeError("usage limit reached")))[1]), \
        mock.patch.object(orchestrate, "live_worktree", side_effect=peer_started):
    assert "linked workspace active" in orchestrate.dispatch(quota_handoff, ws_state.load("quota-handoff"), first="claude",
        assignment=ws_state.situation_token(ws_state.load("quota-handoff")), peers=[quota_peer])[1]
assert quota_calls == ["claude"]  # peer started while the first launch failed; no fallback launch
free_quota = row("free-quota", step="freeform", mr=False, goal="Improve docs")
ws_state.observe(free_quota); free_quota["card"] = "free-quota"
quota_calls.clear()
with mock.patch.object(orchestrate, "launch", side_effect=lambda agent, *a: (quota_calls.append(agent), (_ for _ in ()).throw(RuntimeError("usage limit reached")))[1]), \
        mock.patch.object(orchestrate, "live_worktree", return_value={"comment": "", "branch": free_quota["branch"], "linkedPR": 1, "agents": []}), \
        mock.patch.object(orchestrate, "branch_has_open_mr", return_value=True):
    assert "new MR opened" in orchestrate.dispatch(free_quota, ws_state.load("free-quota"), first="claude")[1]
assert quota_calls == ["claude"]
quota_calls.clear()
with mock.patch.object(orchestrate, "launch", side_effect=lambda agent, *a: (quota_calls.append(agent), (_ for _ in ()).throw(RuntimeError("usage limit reached")))[1]), \
        mock.patch.object(orchestrate, "live_worktree", side_effect=peer_started), \
        mock.patch.object(greenlight, "orca", return_value={"worktrees": [{"worktreeId": quota_handoff["id"], "linkedPR": 1},
                                                               {"worktreeId": "new-unlinked", "branch": quota_handoff["branch"], "agents": [{"state": "working"}]}]}):
    assert "newly linked workspace active" in orchestrate.dispatch(quota_handoff, ws_state.load("quota-handoff"), first="claude")[1]
assert quota_calls == ["claude"]
greenlight.orca = fake_orca
orchestrate.OPTS["max_dispatch"] = 2
real_phase_me = orchestrate.phase_me
def phase_me_with_current_workspace(rows, cards):
    with mock.patch.object(orchestrate, "live_worktree", side_effect=lambda r, snapshot=None:
                           {"comment": r.get("comment"), "branch": r.get("branch"),
                            "linkedPR": r.get("linked_mr"), "linkedGitLabMR": r.get("linked_mr")}):
        return real_phase_me(rows, cards)
orchestrate.phase_me = phase_me_with_current_workspace
needs_user = row("me-ping", step="signoff", owner="me", action="Approve release")
needs_user["card"] = "me-ping"
sent_pings = []
with mock.patch.object(greenlight, "ping", side_effect=lambda *a: (sent_pings.append(a), {"asked": "test"})[1]), \
        mock.patch.object(greenlight, "pinged", return_value={}):
    assert orchestrate.phase_me([needs_user], {"me-ping": {"handoff_epoch": "one"}}) == ["me-ping"]
    assert orchestrate.phase_me([needs_user], {"me-ping": {"handoff_epoch": "one"}}) == []
    assert orchestrate.phase_me([needs_user], {"me-ping": {"handoff_epoch": "two"}}) == ["me-ping"]
    new_commit_user_step = row("me-ping", step="signoff", owner="me", action="Approve release", sha="next-head") | {"card": "me-ping"}
    assert orchestrate.phase_me([new_commit_user_step], {"me-ping": {"handoff_epoch": "two"}}) == ["me-ping"]
assert len(sent_pings) == 3 and sent_pings[0][0].endswith("Options: approve | leave it")
with mock.patch.object(greenlight, "ping") as ping_user, mock.patch.object(greenlight, "resolve") as resolve_user:
    assert orchestrate.phase_me([needs_user | {"_mr_inconclusive": True}], {}) == []
    ping_user.assert_not_called(); resolve_user.assert_not_called()
manual = row("manual-release", step="milestone", owner="me", action="Choose release") | {"card": "manual-release"}
question = {"key": manual["id"], "worktree_id": manual["id"], "mr": manual["mr"]["ref"], "step": "milestone", "goal": manual["goal"], "sha": manual["mr"]["sha"],
            "target_branch": manual["mr"]["target_branch"],
            "question": f"{manual['name']}: {manual['next']['action']} {manual['mr']['web_url']}", "answer": None, "closed": None}
with mock.patch.object(greenlight, "pinged", return_value={manual["id"]: 1}), \
        mock.patch.object(greenlight.decisions, "load", return_value=[question]), \
        mock.patch.object(greenlight, "ping") as ping_user, mock.patch.object(greenlight, "resolve") as resolve_user:
    assert orchestrate.phase_me([manual], {}) == []
    ping_user.assert_not_called(); resolve_user.assert_not_called()
(greenlight.STATE / "me-pings" / (ws_state.safe(manual["id"]) + ".generated")).write_text(question["question"])
manual_detail = question | {"question": "Please choose the exact release milestone for this request"}
with mock.patch.object(greenlight, "pinged", return_value={manual["id"]: 1}), \
        mock.patch.object(greenlight.decisions, "load", return_value=[manual_detail]), \
        mock.patch.object(greenlight, "ping") as ping_user, mock.patch.object(greenlight, "resolve") as resolve_user:
    assert orchestrate.phase_me([manual | {"next": manual["next"] | {"action": "Choose another release"}}], {}) == []
    ping_user.assert_not_called(); resolve_user.assert_not_called()
(greenlight.STATE / "me-pings" / ws_state.safe(manual["id"])).unlink()
with mock.patch.object(greenlight, "pinged", return_value={manual["id"]: 1}), \
        mock.patch.object(greenlight.decisions, "load", return_value=[question]), \
        mock.patch.object(greenlight, "ping", return_value={"asked": "changed"}) as ping_user, \
        mock.patch.object(greenlight, "resolve") as resolve_user:
    changed_manual = manual | {"next": manual["next"] | {"action": "Choose another release"}}
    assert orchestrate.phase_me([changed_manual], {}) == ["manual-release"]
    ping_user.assert_called_once(); resolve_user.assert_called_once()
prior_signoff = question | {"key": "new-signoff-id", "worktree_id": "new-signoff-id", "step": "signoff", "sha": "head-a"}
new_signoff = row("new-signoff", step="signoff", owner="me", sha="head-b") | {"card": "new-signoff"}
with mock.patch.object(greenlight, "pinged", return_value={new_signoff["id"]: 1}), \
        mock.patch.object(greenlight.decisions, "load", return_value=[prior_signoff]), \
        mock.patch.object(greenlight, "ping", return_value={"asked": "new"}) as ping_user, mock.patch.object(greenlight, "resolve") as resolve_user:
    assert orchestrate.phase_me([new_signoff], {}) == ["new-signoff"]
    ping_user.assert_called_once(); resolve_user.assert_called_once()
custom_signoff = row("custom-signoff", step="verify", owner="me", sha="head-b") | {"card": "custom-signoff"}
old_custom = prior_signoff | {"key": custom_signoff["id"], "worktree_id": custom_signoff["id"], "step": "verify"}
with mock.patch.object(greenlight, "pinged", return_value={custom_signoff["id"]: 1}), \
        mock.patch.object(greenlight.decisions, "load", return_value=[old_custom]), \
        mock.patch.object(greenlight, "ping", return_value={"asked": "new"}) as ping_user:
    assert orchestrate.phase_me([custom_signoff], {}) == ["custom-signoff"]
    ping_user.assert_called_once()
with mock.patch.object(greenlight, "ping", side_effect=lambda *a: (sent_pings.append(a), {"asked": "test"})[1]), \
        mock.patch.object(greenlight, "pinged", return_value={}):
    orchestrate.phase_me([needs_user | {"next": needs_user["next"] | {"owner": "working"}}], {})
    assert orchestrate.phase_me([new_commit_user_step], {"me-ping": {"handoff_epoch": "two"}}) == ["me-ping"]
cycle_a = row("cycle-me", step="review", owner="me", action="review it") | {"card": "cycle-me"}
cycle_b = cycle_a | {"next": cycle_a["next"] | {"action": "re-review it"}}
with mock.patch.object(greenlight, "ping", return_value={"asked": "test"}) as ping_user, mock.patch.object(greenlight, "pinged", return_value={}):
    assert orchestrate.phase_me([cycle_a], {}) == ["cycle-me"]
    assert orchestrate.phase_me([cycle_b], {}) == ["cycle-me"]
    assert orchestrate.phase_me([cycle_a], {}) == ["cycle-me"]
    assert ping_user.call_count == 3
with mock.patch.object(greenlight, "ping", side_effect=lambda *a: (sent_pings.append(a), {"asked": "test"})[1]), \
        mock.patch.object(greenlight, "pinged", return_value={needs_user["id"]: 1}), \
        mock.patch.object(greenlight, "resolve") as resolve_old:
    changed_user_step = row("me-ping", step="merge", owner="me", action="Merge now?") | {"card": "me-ping"}
    assert orchestrate.phase_me([changed_user_step], {"me-ping": {"handoff_epoch": "three"}}) == ["me-ping"]
    resolve_old.assert_called_once()
same_name_a = row("shared-question", step="signoff", owner="me", mr=False, goal="") | {"id": "question-a", "card": "question-a"}
same_name_b = row("shared-question", step="signoff", owner="me", mr=False, goal="") | {"id": "question-b", "card": "question-b"}
with mock.patch.object(greenlight, "pinged", return_value={}), mock.patch.object(greenlight, "ping", return_value={"asked": "test"}) as ping_user:
    assert len(orchestrate.phase_me([same_name_a, same_name_b], {})) == 2
    assert [call.args[2] for call in ping_user.call_args_list] == [["question-a"], ["question-b"]]
orchestrate.phase_me = real_phase_me
with mock.patch.object(orchestrate, "live_worktree", return_value={"branch": needs_user["branch"], "comment": "a new goal", "linkedPR": needs_user.get("linked_mr")}), \
        mock.patch.object(greenlight, "ping") as stale_ping, mock.patch.object(greenlight, "resolve") as stale_resolve:
    assert orchestrate.phase_me([needs_user], {}) == []
    stale_ping.assert_not_called(); stale_resolve.assert_not_called()
with mock.patch.object(orchestrate, "live_worktree", return_value={"branch": needs_user["branch"], "comment": needs_user.get("comment"), "linkedPR": needs_user.get("linked_mr")}), \
        mock.patch.object(orchestrate, "current_mr_state", return_value=orchestrate.scanned_mr_state(needs_user) | {"target_branch": "release"}), \
        mock.patch.object(greenlight, "ping") as stale_ping, mock.patch.object(greenlight, "resolve") as stale_resolve:
    assert orchestrate.phase_me([needs_user], {}) == []
    stale_ping.assert_not_called(); stale_resolve.assert_not_called()
(HOME / "last.json").write_text(json.dumps({"rows": [same_name_a, same_name_b]}))
with mock.patch.object(greenlight.decisions, "asked", return_value="record-id") as record_question, \
        mock.patch.object(greenlight.decisions, "load", return_value=[]):
    real_greenlight_ping("shared question\nOptions: yes | no", "question-a", ["question-a"], "signoff")
    assert record_question.call_args.args[3] == ["question-a"]
print("tick ok")
timed_phase = mock.Mock(pid=999)
timed_phase.communicate.side_effect = [subprocess.TimeoutExpired("adopt", 1), ("", None)]
with mock.patch.object(orchestrate.subprocess, "Popen", return_value=timed_phase), mock.patch.object(orchestrate.os, "killpg") as kill_group, \
        mock.patch.object(orchestrate.os, "write"), mock.patch.object(orchestrate, "process_start", return_value="phase-birth"):
    assert real_script("adopt.py", "apply", timeout=1)[0] == 124
    kill_group.assert_called_once_with(999, orchestrate.signal.SIGKILL)

# ---- auto.py: an --orchestrator automation runs the tick, not an agent
import auto
ps_listing = "10 1 10\n11 10 11\n12 11 11\n"
with mock.patch.object(auto.subprocess, "run", return_value=types.SimpleNamespace(stdout=ps_listing)), \
        mock.patch.object(auto.os, "killpg") as kill_group:
    auto.kill_process_tree(10)
    assert [(call.args[0], call.args[1]) for call in kill_group.call_args_list] == [
        (10, auto.signal.SIGSTOP), (11, auto.signal.SIGKILL), (10, auto.signal.SIGKILL)]
(HOME / "orchestrator-phase-10.pid").write_text("42")
with mock.patch.object(auto.subprocess, "run", side_effect=OSError("ps unavailable")), \
        mock.patch.object(auto.os, "killpg") as kill_group:
    auto.kill_process_tree(10)
    assert (42, auto.signal.SIGKILL) not in [(call.args[0], call.args[1]) for call in kill_group.call_args_list]
(HOME / "orchestrator-phase-10.pid").write_text(json.dumps({"phase": 42, "nonce": "current-tick", "root_start": "root-birth", "phase_start": "phase-birth"}))
with mock.patch.object(auto.subprocess, "run", side_effect=OSError("ps unavailable")), \
        mock.patch.object(auto, "process_start", side_effect=lambda pid: "root-birth" if pid == 10 else "phase-birth"), \
        mock.patch.object(auto.os, "getpgid", return_value=42), mock.patch.object(auto.os, "killpg") as kill_group:
    auto.kill_process_tree(10, nonce="current-tick")
    assert (42, auto.signal.SIGKILL) in [(call.args[0], call.args[1]) for call in kill_group.call_args_list]
(HOME / "orchestrator-phase-10.pid").write_text(json.dumps({"phase": 42, "nonce": "current-tick", "root_start": "root-birth", "phase_start": "phase-birth"}))
with mock.patch.object(auto.subprocess, "run", side_effect=OSError("ps unavailable")), \
        mock.patch.object(auto, "process_start", side_effect=lambda pid: "root-birth" if pid == 10 else "reused-birth"), \
        mock.patch.object(auto.os, "getpgid", return_value=42), mock.patch.object(auto.os, "killpg") as kill_group:
    auto.kill_process_tree(10, nonce="current-tick")
    assert (42, auto.signal.SIGKILL) not in [(call.args[0], call.args[1]) for call in kill_group.call_args_list]
auto.STATE = pathlib.Path(tempfile.mkdtemp()); auto.FILE = auto.STATE / "automations.json"; auto.DIR = auto.STATE / "auto"
auto.save({"g": {"schedule": "*/30 * * * *", "kind": "orchestrator"}})
auto.agent = lambda *a: (_ for _ in ()).throw(AssertionError("no agent for an orchestrator run"))
auto.orchestrator = lambda a, log: ("dispatched claude to x", 0)
auto.execute("g")
rec = json.loads((auto.DIR / "runs.jsonl").read_text().splitlines()[-1])
assert rec["status"] == "ok" and rec["engine"] == "orchestrator", rec
print("auto ok")

# ---- chat: a long board is focused on the message; cards and notes go once per session
import chat
chat.DIR = tempfile.mkdtemp()
board = "\n".join(f"- w{i} (#{i}, octo/app): step 2/6 ci_green · on agent · Fix CI" for i in range(40)) + "\n(3 more finished)"
f = chat.focus(board, "what about w30 and #33?", {"w21"})
assert [chat.board_name(l) for l in f.splitlines()[:-2]] == ["w0", "w1", "w2", "w3", "w4", "w5", "w21", "w30", "w33"], f
assert f.splitlines()[-2].startswith("(31 more unfinished") and f.endswith("(3 more finished)"), f
assert chat.focus("- a (#1, x): step", "hi") == "- a (#1, x): step"  # a short board goes whole
spaced = "\n".join([f"- w{i} (#{i}, octo/app): step" for i in range(20)] + ["- Release Candidate ([#99](u), octo/app): step", "- w3x (no MR, octo/app): step"])
f = chat.focus(spaced, "where is release candidate? and #1?")
assert "- Release Candidate ([#99]" in f and "- w1 (#1," in f and "- w19 (" not in f and "- w3x" not in f, f  # names with spaces; #1 isn't #19
assert chat.board_name("- w3x (no MR, octo/app): step") == "w3x"
assert chat.board_name("- Release (#42) follow-up (#1, octo/app): step") == "Release (#42) follow-up"
many_refs = "\n".join(f"- w{i} (#{1}, repo/{i}): step 1/2 ready" for i in range(150))
assert sum(bool(chat.board_name(l)) for l in chat.focus(many_refs, "what about #1?").splitlines()) <= chat.BOARD_FOCUS_MAX
assert "- special (#1," in chat.focus(many_refs + "\n- special (#1, repo/s): step", "special and #1")
(HOME / "last.json").write_text(json.dumps({"rows": ws_state.keyed([row("feat", path=str(repo))])}))
ws_state.update("deleted-worktree", hypothesis="deleted_workspace_secret")
scope_of = lambda r: (r.get("forge") or "", r.get("project") or "", (r.get("mr") or {}).get("ref") or "",
                      (r.get("mr") or {}).get("sha") or "", r.get("goal") or "",
                      (r.get("mr") or {}).get("target_branch") or "", r.get("id") or "", r.get("branch") or "",
                      ws_state.stage_of(r))
assert chat.window_allow("- feat (#1, octo/app): step") == {"feat": scope_of(row("feat"))}
fresh_head = row("feat", path=str(repo), sha="new-head")
(HOME / "last.json").write_text(json.dumps({"rows": ws_state.keyed([fresh_head])}))
assert not ws_state.top("feat", 5, kinds=("card",), allow=chat.window_allow("- feat (#1, octo/app): step"))
(HOME / "last.json").write_text(json.dumps({"rows": ws_state.keyed([row("feat", path=str(repo))])}))
replacement = row("feat", path=str(repo), mrx={"iid": 2, "ref": "#2"})
(HOME / "last.json").write_text(json.dumps({"rows": ws_state.keyed([replacement])}))
assert chat.window_allow("- feat (#2, octo/app): step") == {"feat": scope_of(replacement)}
assert not ws_state.top("feat", 5, kinds=("card",), allow=chat.window_allow("- feat (#2, octo/app): step"))  # old PR card rejected after search
(HOME / "last.json").write_text(json.dumps({"rows": ws_state.keyed([row("feat", path=str(repo)) | {"project": "other/app"}])}))
assert not ws_state.top("feat", 5, kinds=("card",), allow=chat.window_allow("- feat (#1, other/app): step"))
(HOME / "last.json").write_text(json.dumps({"rows": ws_state.keyed([row("feat", path=str(repo))])}))
assert chat.window_allow("") == {}
assert not ws_state.top("deleted_workspace_secret", 5, kinds=("card",), allow=chat.window_allow("- feat (#1, octo/app): step"))
note = chat.context_note("why is feat stuck on the lockfile?", "- feat (#1, octo/app): step")
assert note.startswith("CONTEXT (") and "- workspace feat:" in note, note
assert chat.context_note("why is feat stuck on the lockfile?", "- feat (#1, octo/app): step") == ""  # the session has it
ws_state.update("feat", risk="new risk on the lockfile")
assert "new risk" in chat.context_note("why is feat stuck on the lockfile?", "- feat (#1, octo/app): step")  # a changed card goes again
os.environ["GREENLIGHT_ONLY"] = "[]"
assert "workspace feat" not in chat.context_note("feat lockfile risk freeze", board)  # not in the window: not sent
del os.environ["GREENLIGHT_ONLY"]
same_a, same_b = row("window-shared", mr=False, path="/window/a"), row("window-shared", mr=False, path="/window/b")
same_a["id"], same_b["id"] = "window-a", "window-b"
ws_state.keyed([same_a, same_b])
ws_state.observe(same_a); ws_state.observe(same_b)
ws_state.update(same_b["card"], hypothesis="beta_window_secret")
(HOME / "last.json").write_text(json.dumps({"rows": [same_a, same_b]}))
assert chat.window_allow("- window-shared (no MR, octo/app): step") == {}  # stale pathless board cannot admit both
with mock.patch.object(dash, "live_agents", return_value={r["id"]: ({"repo": "app", "agents": []}, {}) for r in (same_a, same_b)}):
    scoped_board = dash.board(only={"paths": ["/window/a"], "repos": []})
assert "/window/a" in scoped_board and "/window/b" not in scoped_board
assert chat.window_allow(scoped_board) == {same_a["card"]: scope_of(same_a)}
os.environ["GREENLIGHT_ONLY"] = json.dumps({"paths": ["/window/a"]})
allowed = chat.window_allow("- window-shared (no MR, octo/app, /window/a): step")
assert allowed == {same_a["card"]: scope_of(same_a)} and not ws_state.top("beta_window_secret", 5, kinds=("card",), allow=allowed)
os.environ["GREENLIGHT_ONLY"] = json.dumps({"paths": [], "repos": ["app"]})
assert chat.window_allow("- window-shared (no MR, octo/app, /window/a): step") == {same_a["card"]: scope_of(same_a)}
del os.environ["GREENLIGHT_ONLY"]
assert chat.window_allow("- window-shared (no MR, octo/app, /window/b): step") == {same_b["card"]: scope_of(same_b)}
punctuated = row("Release (v2): follow-up", mr=False)
ws_state.keyed([punctuated])
(HOME / "last.json").write_text(json.dumps({"rows": [punctuated]}))
assert chat.window_allow("- Release (v2): follow-up (no MR, octo/app): step") == {punctuated["card"]: scope_of(punctuated)}
import dash
old_line = "- feat (#1, octo/app): step 2/6 ci_green · on agent · Fix CI"
new_line = "- feat (#1, octo/app): step 3/6 ready · on greenlight · Mark ready"
fake_board = lambda text: lambda **kw: (text, {}) if kw.get("with_ids") else text
dash.board = fake_board(new_line)
seen = {"board": "old", "lines": {old_line: ws_state.digest(old_line)}, "context": {}}
changed_note = chat.board_note("feat", "claude:updated", seen)
assert new_line in changed_note and "no longer on this board" not in changed_note, changed_note
long_board = "\n".join(f"- w{i} (#{i}, octo/app): step" for i in range(30)) + "\n" + new_line
dash.board = fake_board(long_board)
seen = {"board": "old", "lines": {old_line: ws_state.digest(old_line)}, "context": {}}
assert new_line in chat.board_note("what about w1?", "claude:updated-hidden", seen)
old_bare = "- bare (#4, octo/app): ci_green · on agent · Fix CI"
new_bare = "- bare (#4, octo/app): ready · on greenlight · Mark ready"
dash.board = fake_board(long_board + "\n" + new_bare)
seen = {"board": "old", "lines": {old_bare: ws_state.digest(old_bare)}, "context": {}}
assert new_bare in chat.board_note("what about w1?", "claude:bare-update", seen)
old_named = "- Release (v2): docs (#1, octo/app): step"
new_named = "- Release (v2): API (#2, octo/app): step"
dash.board = fake_board(long_board + "\n" + new_named)
seen = {"board": "old", "lines": {old_named: ws_state.digest(old_named)}, "context": {}}
collision_note = chat.board_note("what about w1?", "claude:collision", seen)
assert old_named in collision_note and "no longer on this board" in collision_note and new_named not in collision_note, collision_note
dash.board = fake_board("- feat (#1, octo/app): step\n(4 more finished)")
seen = {"board": "old", "lines": {"- feat (#1, octo/app): step": "digest"}, "context": {}, "finished": 3}
assert "4 more finished" in chat.board_note("feat", "claude:finished", seen)
dash.board = fake_board("")
old_lines = {f"- gone{i} (#{i}, octo/app): step": "digest" for i in range(25)}
seen = {"board": "old", "lines": old_lines.copy(), "context": {}}
reset_note = chat.board_note("status", "claude:removed", seen)
assert "BOARD reset" in reset_note and not seen["lines"]
excluded_line = "- secret (#9, octo/app): private workspace detail"
seen = {"board": "broad", "scope": "all", "lines": {excluded_line: ws_state.digest(excluded_line)}, "context": {}}
chat.session("codex", "broad-session")
os.environ["GREENLIGHT_ONLY"] = json.dumps({"paths": ["/window/a"]})
assert chat.session("codex") is None  # a restricted window never resumes the broad model context
chat.session("codex", "narrow-session")
dash.board = fake_board("- visible (#1, octo/app): current")
assert excluded_line not in chat.board_note("status", "claude:narrow", seen) and excluded_line not in seen["lines"]
del os.environ["GREENLIGHT_ONLY"]
assert chat.session("codex") == "broad-session"
dash.board = fake_board("- feat (#1, octo/app): step 2/6 ci_green · on agent · Fix CI")
chat.harness.route = lambda model: ("claude", model)
chat.harness.argv = lambda *a, **kw: (["no-harness"], kw.get("sid"))
chat.session = lambda *a: None
chat.run = lambda *a, **kw: (None, "Stopped")
chat.decisions.quiet = lambda *a, **kw: None
chat.turn("what next?", "claude", None)
assert not (pathlib.Path(chat.DIR) / chat.SEEN).exists()  # failed first turn must not mark a new session as informed
pending = chat.seen_load("claude:known")
chat.board_note("what next?", "claude:known", pending)
assert not (pathlib.Path(chat.DIR) / chat.SEEN).exists()  # hard kill before harness delivery cannot persist seen state
chat.seen_save(pending | {"board": "old", "lines": {"- old (#1)": "digest"}})
os.environ["GREENLIGHT_ONLY"] = "[]"
dash.board = fake_board("")
assert "no workspaces in this window" in chat.board_note("what next?", "claude:known")
del os.environ["GREENLIGHT_ONLY"]
print("chat ok")
print("test_orchestrate ok")
