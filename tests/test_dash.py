"""uv run python test_dash.py: the state table and layout validation (the page trusts both)."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import dash


def row(owner="reviewers", step="approved", agent="done", action=""):
    return {"next": {"owner": owner, "step": step, "action": action}, "agent": {"state": agent}}


assert dash.state_of(row(agent="waiting"))[0] == "needs"
assert dash.state_of(row(owner="me", step="error"))[0] == "crashed"
assert dash.state_of(row(owner="me", action="agent logged out: /login"))[0] == "crashed"
assert dash.state_of(row(agent="working"))[0] == "working"
assert dash.state_of(row(owner="ci", step="ci_green"))[0] == "working"
assert dash.state_of(row(owner="me", step="ready"))[0] == "raised"
assert dash.state_of(row(owner="greenlight", step="review_bot"))[0] == "queued"
assert dash.state_of(row(owner="done", step="done"))[0] == "done"
assert dash.state_of(row())[0] == "quiet"
# an open MR I'm blocking (blocking.py's scan) needs me, after a waiting agent and a crash; a merged one doesn't
BLK = {"acme/widgets!7": {"reason": "discussion", "why": "2 threads are waiting on you"}}
blk_row = lambda state="opened", **k: row(**k) | {"project": "acme/widgets", "mr": {"iid": 7, "state": state}}
assert dash.state_of(blk_row(owner="ci"), BLK) == ("needs", "You're blocking: 2 threads are waiting on you")
assert dash.state_of(blk_row(state="merged"), BLK)[0] == "quiet" and dash.state_of(blk_row(owner="ci"))[0] == "working"
assert dash.state_of(blk_row(step="error"), BLK)[0] == "crashed"
assert dash.state_of(blk_row(agent="waiting"), BLK) == ("needs", "agent is waiting on your answer")  # its answer box stays

v = dash.validate({"puck": {"widget": "files", "props": {}},  # files can't sit in a puck
                   "tiles": [{"widget": "stat", "props": {"value": "1"}}, {"widget": "script", "props": {}},
                             {"widget": "note", "props": "x"}, {"widget": "kv", "props": {"rows": []}},  # kv repeats the fixed facts
                             {"widget": "spark", "props": {}}, {"widget": "grid", "props": {}}, {"widget": "meter", "props": {}}],
                   "wide": {"widget": "log", "props": {}}})
assert v["puck"] is None and v["wide"] is None
assert [c["widget"] for c in v["tiles"]] == ["stat", "spark", "grid"]  # at most 3
assert dash.validate({"wide": {"widget": "files", "props": {}}})["wide"]["widget"] == "files"
assert dash.validate({"puck": {"widget": "meter", "props": {"value": 5}}})["puck"]["widget"] == "meter"
# answering: a menu only when no empty input box is on screen
assert dash.menu(["Which one?", "❯ 1. Wait for !5187", "  2. Drop the dependency", "  3. Type something."]) == [("1", "Wait for !5187"), ("2", "Drop the dependency"), ("3", "Type something.")]
assert dash.picker([" ☐ Plan", "│ Which way", "│ now?", "❯ 1. Keep it (Recommended)", "     Leave it as is.", "  2. Port it", "     New MR,", "     more review.",
                    "────", "  3. Chat about this", "Enter to select"]) == ("Which way now?", {"1": "Leave it as is.", "2": "New MR, more review."})
assert dash.menu(["  - 1. a list in a message", "  2. more", "────", "❯", "────"]) == []
import json
import tempfile
from pathlib import Path
from unittest import mock
with mock.patch.object(dash, "waiting_term", return_value=({}, {}, "t1", {"draft": "half typed", "tail": ["❯"]})), \
        mock.patch.object(dash.greenlight, "orca") as o:
    assert "unsent" in dash.answer("ws", "yes")["error"] and not o.called
# over a menu: a key is pressed as is; anything else is Esc, wait for the picker to close, then one line + Enter
MENU = {"draft": "", "tail": ["❯ 1. FIFO", "  2. Drag"]}
with mock.patch.object(dash, "waiting_term", return_value=({}, {}, "t1", MENU)), mock.patch.object(dash.greenlight, "history_file", return_value=Path(tempfile.mkdtemp()) / "h.md"), \
        mock.patch.object(dash.time, "sleep"), mock.patch.object(dash.greenlight, "orca", side_effect=[None, {"terminal": {"tail": MENU["tail"]}}, {"terminal": {"tail": ["❯"]}}, None]) as o:
    assert dash.answer("ws", "Queue order: FIFO.\nCap: 3.")["ok"]
    sent = [c.args for c in o.call_args_list]
    assert sent[0][-1] == "\x1b" and sent[1][1] == "read" and sent[3][-3:] == ("--text", "Queue order: FIFO. Cap: 3.", "--enter")
with mock.patch.object(dash, "waiting_term", return_value=({}, {}, "t1", MENU)), mock.patch.object(dash.greenlight, "history_file", return_value=Path(tempfile.mkdtemp()) / "h.md"), \
        mock.patch.object(dash.greenlight, "orca") as o:
    assert dash.answer("ws", "2")["ok"] and o.call_args.args[-2:] == ("--text", "2")
# the pending AskUserQuestion comes from the newest transcript, and goes once it has a result
d = tempfile.mkdtemp()
Q = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": [{"question": "Which?", "options": []}]}}]}}
Path(d, "s.jsonl").write_text(json.dumps(Q) + "\n")
with mock.patch.object(dash, "transcript_files", return_value=[(Path(d, "s.jsonl"), "claude")]):
    assert dash.pending_questions("/w")[0]["question"] == "Which?"
    Path(d, "s.jsonl").write_text(json.dumps(Q) + "\n" + json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "q1"}]}}) + "\n")
    assert dash.pending_questions("/w") is None
# console run: never a second agent on a busy tree, never over an unanswered question
from unittest import mock
for st, word in (("working", "working"), ("waiting", "waiting")):
    with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws", "worktreeId": "w1"}, {"state": st})}), \
            mock.patch.object(dash.greenlight, "orca") as o:
        assert word in dash.run("ws", "do it")["error"] and not o.called
with mock.patch.object(dash, "live_agents", return_value={}):
    assert "no workspace" in dash.run("ws", "do it")["error"]
# console transcript: prompts, replies, tool calls; drops reminders, meta and subagent lines
import json
import tempfile
from pathlib import Path
with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as t:
    for d in ({"type": "user", "message": {"content": "<command-name>/some-skill</command-name><command-args></command-args>"}},
              {"type": "user", "message": {"content": "<system-reminder>x</system-reminder>"}},
              {"type": "user", "isMeta": True, "message": {"content": "meta"}},
              {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "sub"}]}},
              {"type": "assistant", "message": {"content": [{"type": "thinking"}, {"type": "text", "text": "hi"},
                                                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls\n -la"}}]}}):
        t.write(json.dumps(d) + "\n")
assert dash._claude_entries(Path(t.name)) == [("user", "/some-skill"), ("assistant", "hi"), ("tool", "Bash ls -la")]
# park adds or drops "(parked)" on the live goal, and only once
import tempfile
dash.greenlight.STATE = Path(tempfile.mkdtemp())
dash.DASH = dash.greenlight.STATE / "dash"  # cached pages and suggestions too, never the real ~/.greenlight
def parked(comment, on):
    with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws", "worktreeId": "w1", "comment": comment}, {})}), \
            mock.patch.object(dash.greenlight, "orca") as o:
        dash.park("ws", on)
        return o.call_args.args[-1]
assert parked("Merge !5131", True) == "Merge !5131 (parked)"
assert parked("Merge !5131 (parked)", True) == "Merge !5131 (parked)"
assert parked("Merge !5131 (Parked) for now", False) == "Merge !5131 for now"
assert parked("", True) == "(parked)"
import base64
# New task: the title is the first line, cut to a sentence or at a word
T = dash.task_title
assert T("Retry stuck txs") == "Retry stuck txs"
assert T("\n  \nRetry  stuck txs\nafter a churn\n") == "Retry stuck txs"
long = "Retry stuck outbound txs after a churn. " + "They sit in the outbound queue forever and nobody notices until a user complains " * 2
assert T(long) == "Retry stuck outbound txs after a churn."
words = "word " * 40
t = T(words + "end")
assert len(t) <= 120 and t.endswith("…") and t[:-1].split() == ["word"] * len(t[:-1].split()) and not t[:-1].endswith(" ")
assert len(T("x" * 300)) == 120 and T("x" * 300).endswith("…")
assert T("Is it slow? " + "a" * 200) == "Is it slow?"
# a task becomes a workspace at once: goal = title (greenlight's freeform step), the agent gets the whole request and the files
dash.TASKS = dash.greenlight.STATE / "tasks"
REPO = [{"id": "r1", "name": "widgets", "host": "gitlab", "path": "acme/widgets"}]
REQ = "Retry stuck outbound txs after a churn.\n\nThey sit in the queue " + "forever " * 6 + "\n- see the log"
B64 = "data:text/plain;base64," + base64.b64encode(b"panic: boom\n").decode()
def task(payload, fail=None, ws=None):
    calls = []
    def orca(*a):
        calls.append(a)
        for want, err in (fail or {}).items():
            if want(a):
                raise RuntimeError(err)
        return {"worktree": {"id": "w9"}} if a[:2] == ("worktree", "create") else {}
    with mock.patch.object(dash, "repos", return_value=REPO), mock.patch.object(dash, "live_agents", return_value=ws or {}), \
            mock.patch.object(dash.greenlight, "orca", side_effect=orca):
        return dash.task("new", "widgets", json.dumps(payload)), calls
def opt(a, flag):
    return a[a.index(flag) + 1]
is_create = lambda a: a[:2] == ("worktree", "create")
out, calls = task({"text": "  " + REQ + "\n", "files": [{"name": "../../etc/passwd", "data": B64}, {"name": "passwd", "data": B64},
                                                       {"name": ".hidden log", "data": B64}]})
assert out == {"ok": True, "name": "retry-stuck-outbound-txs-after-a-churn", "repo": "widgets"}, out
d = dash.TASKS / out["name"]
assert (d / "request.md").read_text() == REQ + "\n"
assert sorted(f.name for f in (d / "files").iterdir()) == ["2-passwd", "hidden-log", "passwd"]  # basenames only, never outside files/
assert (d / "files" / "passwd").read_bytes() == b"panic: boom\n"
c = next(a for a in calls if is_create(a))
assert opt(c, "--comment") == "Retry stuck outbound txs after a churn." and opt(c, "--agent") == "claude" and "--no-parent" in c
assert not dash.greenlight.ISSUE_GOAL_RE.match(opt(c, "--comment")) and not dash.greenlight.PLAN_RE.match(opt(c, "--comment"))
p = opt(c, "--prompt")
assert REQ in p and f"{d}/files: passwd, 2-passwd, hidden-log" in p and "draft MR" in p
assert ("worktree", "set", "--worktree", "id:w9", "--display-name", out["name"]) in calls
assert "task started (claude, 3 files)" in dash.greenlight.history_file(out["name"]).read_text()
out, calls = task({"text": REQ}, ws={"w2": ({"displayName": "retry-stuck-outbound-txs-after-a-churn-2"}, {})})  # dir and workspace taken
assert out["name"] == "retry-stuck-outbound-txs-after-a-churn-3" and "attached" not in opt(next(a for a in calls if is_create(a)), "--prompt")
out, calls = task({"text": "Fix the flake"}, fail={lambda a: is_create(a) and "claude" in a: "You've hit your usage limit"})  # claude out: codex
assert out["ok"] and [opt(a, "--agent") for a in calls if is_create(a)] == ["claude", "codex"]
out, calls = task({"text": "Fix it"}, fail={lambda a: is_create(a) and "claude" in a: "bad repo"})  # any other error: no codex retry
assert "bad repo" in out["error"] and len([a for a in calls if is_create(a)]) == 1
out, calls = task({"text": "Fix them"}, fail={lambda a: a[:2] == ("worktree", "set"): "busy"})  # the rename is cosmetic
assert out["ok"]
for bad, why in (({"text": " \n "}, "describe"), ({"text": "x", "files": [{"name": "a", "data": "javascript:1"}]}, "didn't come through"),
                 ({"text": "x", "files": [{"name": "a", "data": B64}] * 2 + [{"name": "big", "data": "data:application/octet-stream;base64," + "A" * (21 << 20)}]}, "15 MB")):
    out, calls = task(bad)
    assert why in out["error"] and not calls, (bad, out)
with mock.patch.object(dash, "repos", return_value=REPO):
    assert "no repo" in dash.task_new("nope", {"text": "x"})["error"]
# leftover planner workspaces from the old Plan view: greenlight still never drives one, cleanup still keeps it
assert dash.greenlight.evaluate({"goal": "Plan: Retry", "branch": "plan-x"}, None, None)["hidden"]
import cleanup  # a planner has no MR and nothing of its own on its branch: cleanup must not take it for done work
assert cleanup.evaluate({"path": "/x", "branch": "refs/heads/plan-x", "comment": "Plan: Retry"}, None, set())[0] == "keep"
# GitHub PRs: live facts come from gh (checks become jobs), and the puck carries the "#N" ref and forge
GH_MR = {"iid": 23, "state": "opened", "head_pipeline": {"status": "failed", "web_url": "https://github.com/o/r/pull/23/checks"},
         "_checks": [{"name": "unit", "stage": "test", "status": "failed"}], "_discussions": [], "_approved_by": ["alice"],
         "has_conflicts": False, "milestone": None, "labels": ["ui"], "updated_at": "t", "_forge": "github"}
with mock.patch.object(dash.greenlight, "mr_details", return_value=GH_MR) as md, \
        mock.patch.object(dash.greenlight, "gl_all", side_effect=AssertionError("no GitLab call for a GitHub PR")):
    f = dash.mr_facts("o/r", 23, "github")
    assert md.call_args.args == ("o/r", 23, "github")
    assert f["jobs"] == [{"name": "unit", "stage": "test", "status": "failed"}] and f["pipeline"] == "failed" and f["approved_by"] == ["alice"]
(dash.greenlight.STATE / "last.json").write_text(json.dumps({"ts": 1, "rows": [
    {"id": "w1", "name": "ws-gh", "path": "/nonexistent", "project": "octo/greenlight", "forge": "github", "goal": "Merge MR for #20",
     "mr": {"iid": 23, "ref": "#23", "forge": "github", "web_url": "u", "state": "opened", "role": "author"},
     "next": {"step": "ready", "owner": "greenlight", "action": "mark ready for review"}, "agent": {"state": "none", "type": None, "last": ""}}]}))
with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws-gh", "worktreeId": "w1", "path": "/nonexistent", "comment": "Merge MR for #20",
                                                                  "branch": "refs/heads/b"}, {})}):
    pk = dash.snapshot()["pucks"][0]
assert (pk["mr"]["ref"], pk["mr"]["forge"], pk["state"], pk["steps"][0]) == ("#23", "github", "queued", "mr_open")
# the hover card's button is written ahead: a snapshot carries the cached step only while it's for the puck's current state
assert pk["suggestion"] is None  # nothing written yet: the app queues `suggest`, the card says "Please wait…"
LIVE = {"w1": ({"displayName": "ws-gh", "worktreeId": "w1", "path": "/nonexistent", "comment": "Merge MR for #20", "branch": "refs/heads/b"}, {})}
def suggestion(cached):
    (dash.DASH / "suggest").mkdir(parents=True, exist_ok=True)
    (dash.DASH / "suggest" / "ws-gh.json").write_text(json.dumps(cached))
    with mock.patch.object(dash, "live_agents", return_value=LIVE):
        return dash.snapshot()["pucks"][0]["suggestion"]
assert suggestion({"sig": pk["sig"], "label": "Mark ready", "prompt": "Mark #23 ready."}) == {"label": "Mark ready", "prompt": "Mark #23 ready."}
assert suggestion({"sig": "stale", "label": "Mark ready", "prompt": "Mark #23 ready."}) is None  # the state moved on since
assert suggestion({"sig": pk["sig"], "prompt": "Mark #23 ready."}) is None  # an old entry without a label gets rewritten
with mock.patch.object(dash, "snapshot", return_value={"pucks": [pk]}), mock.patch.object(dash, "ask", side_effect=AssertionError("asked the model for a cached step")):
    (dash.DASH / "suggest" / "ws-gh.json").write_text(json.dumps({"sig": pk["sig"], "label": "Mark ready", "prompt": "Mark #23 ready."}))
    assert dash.suggest("ws-gh")["label"] == "Mark ready"
print("ok")
# suggest: the button's short label, then the prompt
assert dash.split_label("Fix failing lint job\nFix the gofmt error in x.go and push.") == ("Fix failing lint job", "Fix the gofmt error in x.go and push.")
assert dash.split_label("Summarize the open review threads now please\nDo it.") == ("Summarize the open review", "Do it.")
assert dash.split_label("Just one line of prompt.") == ("", "Just one line of prompt.")

# the terminal's sidebar: every ADE merged; a missing one is skipped, a shared path goes to the first ADE
import types, sys as _sys
def _boom(*a): raise FileNotFoundError("orca")
_wt = lambda ade, **k: {"worktrees": [{"path": "/r/a", "displayName": "a", "repo": "r", "branch": "refs/heads/x", "agents": [{"state": "working", "updatedAt": 2}, {"state": "done", "updatedAt": 1}]},
                                      {"path": "/r/b", "displayName": "b", "repo": "r", "isArchived": True},
                                      {"path": f"/{ade}", "repo": "", "isMainWorktree": True}]}
_orca, dash.greenlight.orca_cli = dash.greenlight.orca_cli, _boom
for _a in ("cmux", "herdr", "t3"):
    _sys.modules[f"ade_{_a}"] = types.SimpleNamespace(call=lambda *a, _a=_a: _wt(_a))
_last = (dash.greenlight.STATE / "last.json").read_text()
(dash.greenlight.STATE / "last.json").write_text(json.dumps({"ts": 1, "rows": [{"path": "/r/a", "name": "r-a", "project": "r",
    "agent": {"state": "waiting"}, "next": {"step": "ci_green", "owner": "agent", "action": ""},
    "mr": {"ref": "!7", "state": "opened", "pipeline": "failed", "threads": 2, "rebase": True, "author": "x", "by": {"name": "x", "avatar": "u"}}}]}))
_t = dash.trees()
(dash.greenlight.STATE / "last.json").write_text(_last)
dash.greenlight.orca_cli = _orca
assert [(t["ade"], t["path"]) for t in _t] == [("cmux", "/cmux"), ("herdr", "/herdr"), ("t3", "/t3"), ("cmux", "/r/a")], _t
_mr = _t[-1].pop("mr")
assert _mr["ref"] == "!7" and _mr["threads"] == 2 and _mr["rebase"] and "author" not in _mr and _mr["by"] == {"name": "x", "avatar": "u"}, _mr  # the card byline
# the live agent (working) wins over the scan's (waiting): not "needs you"
assert _t[-1] == {"ade": "cmux", "project": "r", "name": "a", "path": "/r/a", "branch": "x", "main": False, "agent": "working",
                  "goal": "", "pinned": False, "active": None, "since": None, "created": None,
                  "puck": "r-a", "state": "working", "why": "agent working"}, _t[-1]

# the workspace menu's Delete: by path, the worktree only; a dirty or unpushed tree (or a pinned one) needs --force, main never goes
import subprocess as _sp
_repo = Path(tempfile.mkdtemp()) / "repo"
_g = lambda *a, cwd=_repo: _sp.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)
_sp.run(["git", "init", "-q", str(_repo)], check=True); _g("commit", "-q", "--allow-empty", "-m", "init")
_wt_path = _repo.parent / "wt"; _g("worktree", "add", "-q", "-b", "feat", str(_wt_path))
(_wt_path / "new.txt").write_text("x")
_calls = []
def _ade(*a):
    _calls.append(a)
    return {"worktrees": [{"path": str(_repo), "worktreeId": "m", "isMainWorktree": True}, {"path": str(_wt_path), "worktreeId": "w1", "isPinned": _pin[0]}]}
_pin = [False]
_sys.modules["ade_cmux"] = types.SimpleNamespace(call=_ade)
_i = dash.tree_info(str(_wt_path))
assert _i["dirty"] == 1 and _i["unpushed"] == 1 and _i["untracked"] == 1 and _i["files"] == [["??", "new.txt"]] and _i["commits"][0].endswith(" init"), _i  # the init commit: no remote
assert isinstance(dash.created(str(_wt_path)), int) and dash.created("/nope") is None
_r = dash.tree_rm(str(_wt_path))
assert _r["risks"] == ["1 uncommitted file", "1 commit no remote has (the local branch keeps them)"], _r
assert not [c for c in _calls if c[1] == "rm"]
assert dash.tree_rm(str(_repo), force=True) == {"error": "the repo's main checkout is never removed"}
assert dash.tree_rm(str(_wt_path), force=True) == {"ok": True}
assert _calls[-1] == ("worktree", "rm", "--worktree", "id:w1", "--force"), _calls[-1]
_pin[0] = True; (_wt_path / "new.txt").unlink(); _g("update-ref", "refs/remotes/origin/feat", "HEAD")
assert dash.tree_rm(str(_wt_path))["risks"] == ["it's pinned"]
assert dash.tree_goal(str(_wt_path), "  ship   it \n") == {"ok": True} and _calls[-1][-2:] == ("--comment", "ship it")
# Save to repo: the main checkout's .greenlight.json, read back by trees(); a worktree or a non-object is refused
assert dash.tree_cfg(str(_repo), '{"name": "Greenlight", "look": {"strip": "#222"}}') == {"ok": True, "cfg": {"name": "Greenlight", "look": {"strip": "#222"}}}
assert dash.repo_cfg(str(_repo)) == {"name": "Greenlight", "look": {"strip": "#222"}} and dash.repo_cfg(str(_wt_path)) is None
assert "error" in dash.tree_cfg(str(_wt_path), "{}") and "error" in dash.tree_cfg(str(_repo), "[1]")
# the card's Diff counts (staged and unstaged, no origin/HEAD here: against HEAD), and Delete's teardown: a failure or a changed file warns
(_wt_path / "a.txt").write_text("1\n2\n"); _g("add", "a.txt", cwd=_wt_path)
assert (lambda i: (i["added"], i["removed"]))(dash.tree_info(str(_wt_path))) == (2, 0)
dash.tree_cfg(str(_repo), '{"teardown": "exit 3"}')
assert dash.tree_rm(str(_wt_path), True, "exit 3")["warn"].startswith("Teardown failed (exit 3")
assert dash.tree_rm(str(_wt_path), True, "true")["warn"] == "Teardown skipped: the repo's .greenlight.json changed"
print("ok")

# --- Settings: a repo's lifecycle (bundled → yours → reset), instructions, memory notes kept in the MEMORY.md index
import json
from pathlib import Path
dash.MEMORY = Path(tempfile.mkdtemp())
g = dash.settings("lifecycle-get", "greenlight", "")
assert g["source"] == "bundled" and "solo" in g["options"], g
dash.settings("lifecycle-set", "greenlight", json.dumps({**g, "rules": "Always add a test."}))
g2 = dash.settings("lifecycle-get", "greenlight", "")
assert (g2["source"], g2["rules"], g2["intro"]) == ("yours", "Always add a test.", g["intro"])
assert [s["key"] for s in g2["stages"]] == [s["key"] for s in g["stages"]]
assert dash.settings("lifecycle-reset", "greenlight", "")["source"] == "bundled"
assert dash.settings("lifecycle-get", "../x", "")["error"]
assert dash.settings("instructions-set", "-", "be terse")["text"] == "be terse\n"
dash.settings("memory-set", "a-note", "---\nname: a-note\ndescription: hi there\n---\nfact")
dash.settings("memory-set", "a-note", "---\ndescription: v2\n---\nfact")
assert (dash.MEMORY / "MEMORY.md").read_text() == "- [a-note](a-note.md) — v2\n"
assert dash.settings("memory-delete", "a-note", "")["notes"] == [] and "a-note" not in (dash.MEMORY / "MEMORY.md").read_text()
assert dash.settings("memory-set", "MEMORY", "x")["error"]
print("settings ok")

# the hover card's moves: most urgent first; vars filled once, missing → "none"; yours can pull in the default
r = {"project": "octo/greenlight", "branch": "b", "path": "/w/b", "next": {"owner": "me", "step": "review", "action": ""},
     "mr": {"state": "opened", "role": "review", "ref": "!58", "title": "{sha} in a title", "target_branch": "main", "sha": "abc123", "reviewed_sha": None}}
mv = dash.moves(r)
assert [m["kind"] for m in mv] == ["review"] and mv[0]["repo"] == "greenlight" and not mv[0]["own"], mv
assert "abc123" in mv[0]["prompt"] and "none" in mv[0]["prompt"] and "{" not in mv[0]["prompt"].replace("{sha} in a title", "")
assert dash.moves({**r, "next": {"owner": "them", "step": "review"}}) == []
assert dash.moves({**r, "mr": {**r["mr"], "state": "merged"}}) == []
dash.settings("prompt-set", "greenlight/review", "Mine for {mr} at {sha}, last {last_review_sha}, {threads}.\n{default}")
mv = dash.moves(r)[0]
assert mv["own"] and mv["prompt"].startswith("Mine for !58 at abc123, last none, {threads}.\n") and len(mv["prompt"]) > 500, mv["prompt"][:80]
g = dash.settings("prompt-get", "greenlight/review", "")
assert g["own"] and g["text"].startswith("Mine") and "{last_review_sha}" in g["default"] and "last_review_sha" in g["vars"], g
assert not dash.settings("prompt-set", "greenlight/review", "  ")["own"] and not dash.settings("prompt-get", "greenlight/review", "")["own"]
assert dash.settings("prompt-get", "../x/review", "")["error"] and dash.settings("prompt-get", "greenlight/nope", "")["error"]
# your own MR: conflicts › ci › threads › review; merge only when none of those apply; a reviewer's MR gets none of the fixes
mine = {**r, "next": {"owner": "greenlight", "step": "merge", "action": "merge", "cmd": ["glab", "mr", "merge", "58", "-R", "octo/greenlight", "--yes", "--squash"]},
        "mr": {**r["mr"], "role": "author", "conflicts": True, "pipeline": "failed", "pipeline_url": "https://ci/1",
               "failed_jobs": [{"name": "unit", "stage": "test", "url": "https://ci/1/j"}], "todo_threads": ['sam, a.py:3: "why?"', "sam"]}}
mv = dash.moves(mine)
assert [m["kind"] for m in mv] == ["conflicts", "ci", "threads"] and mv[2]["n"] == 2, mv
assert "- unit (test): https://ci/1/j" in mv[1]["prompt"] and "https://ci/1" in mv[1]["prompt"] and '- sam, a.py:3: "why?"\n- sam' in mv[2]["prompt"]
mv = dash.moves({**mine, "mr": {**mine["mr"], "role": "review"}})
assert [m["kind"] for m in mv] == ["merge"] and mv[0]["how"] == "Squash", mv
assert dash.moves({**mine, "mr": {**mine["mr"], "role": "review"}, "next": {**mine["next"], "cmd": None}}) == []  # no cmd: greenlight won't merge it now
# auto-run: per repo, per kind, with a gate; never on a held/parked goal; merge gets True
assert dash.settings("autorun-set", "../x", "{}")["error"]
a = dash.settings("autorun-set", "greenlight", json.dumps({"ci": 1, "threads": 9, "nope": 0, "merge": 0}))
assert a["on"] == {"ci": 1, "merge": 0} and [k["kind"] for k in a["kinds"]] == ["ci", "conflicts", "threads", "review", "merge"], a
mv = {m["kind"]: m for m in dash.moves(mine)}
assert mv["ci"]["auto"].startswith(dash.AUTO_HEAD + "You have my OK to push") and mv["ci"]["prompt"] in mv["ci"]["auto"] and mv["ci"]["sha"] == "abc123"
assert "auto" not in mv["conflicts"] and "auto" not in mv["threads"]
assert not any("auto" in m for m in dash.moves({**mine, "goal": "fix it (hold)"}))
assert dash.moves({**mine, "mr": {**mine["mr"], "role": "review"}})[0]["auto"] is True
dash.settings("autorun-set", "greenlight", "{}")
assert not any("auto" in m for m in dash.moves(mine))
# Merge: greenlight's own cmd, pinned to the head it saw; then the snapshot says merged so the card stops offering it
_ran, _run = [], dash.greenlight.run
dash.greenlight.run = lambda cmd, **k: _ran.append(cmd) or ""
_lj = dash.greenlight.STATE / "last.json"; _old = _lj.read_text() if _lj.exists() else None
_lj.write_text(json.dumps({"ts": 1, "rows": [mine]}))
assert dash.tree_merge("/w/b")["error"] and not _ran  # conflicts/CI/threads outrank merge, so it isn't offered
_lj.write_text(json.dumps({"ts": 1, "rows": [{**mine, "mr": {**mine["mr"], "conflicts": False, "pipeline": "success", "todo_threads": []}}]}))
assert dash.tree_merge("/w/b") == {"ok": True, "ref": "!58", "target": "main", "how": "Squash"}
assert _ran == [mine["next"]["cmd"] + ["--sha", "abc123"]], _ran
assert dash.tree_merge("/w/b")["error"] and dash.tree_merge("/w/x")["error"] and len(_ran) == 1  # merged now; unknown path
dash.greenlight.run = _run
_lj.write_text(_old) if _old is not None else _lj.unlink()
print("moves ok")

# Basement: ports, diff, new-workspace input checks
assert dash.listeners("p10\ncnode\nf3\nn*:5173\nf4\nn127.0.0.1:8080\np11\nn[::1]:3000\n") == {10: ("node", {5173, 8080}), 11: ("", {3000})}
assert dash.under([(2, 1), (3, 2), (9, 8)], [1]) == {1, 2, 3}
assert dash.BRANCH_RE.fullmatch("feat/ports-1") and not dash.BRANCH_RE.fullmatch("-rf") and not dash.BRANCH_RE.fullmatch("a b")
assert dash.tree_new("/nowhere", {"name": "../x"})["error"] and dash.tree_new("/nowhere", {"name": "ok", "base": "--x"})["error"]
import subprocess as _sp
with tempfile.TemporaryDirectory() as _d:
    _g = lambda *a: _sp.run(["git", "-C", _d, *a], capture_output=True, check=True)
    _g("init", "-q"); _g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
    open(os.path.join(_d, "f.txt"), "w").write("hi\n"); _g("add", "f.txt")
    _df = dash.tree_diff(_d, "head")
    assert _df["files"] == [["f.txt", "1", "0"]] and "+hi" in _df["patch"] and not _df["cut"], _df
    open(os.path.join(_d, "new.py"), "w").write("a\nb\n"); open(os.path.join(_d, "empty"), "w").close()  # untracked: an agent's new files
    assert dash.tree_diff(_d, "head")["files"] == [["f.txt", "1", "0"], ["empty", "0", "0"], ["new.py", "2", "0"]], dash.tree_diff(_d, "head")
    assert dash.tree_diff(_d, "head")["status"] == ["A", "U", "U"]  # staged new file, then the untracked ones
    assert dash.tree_diff(_d, "branch")["error"]  # no origin to compare with
    assert dash.hook("exit 3", _d).startswith("exit 3") and dash.hook("true", _d) is None
print("basement ok")

# CI view: runs bucketed per UTC day (empty days kept), canceled apart, durations of finished runs only, a shared MR once
_run = lambda id, ts, st, dur, ws="a", project="acme/widgets", ref="!7": {"id": id, "ts": ts, "status": st, "dur": dur, "ws": ws,
                                                                        "project": project, "ref": ref, "web_url": "u", "url": "p" + id}
_runs = [_run("1", "2026-11-01T23:59:00Z", "success", 20.0), _run("2", "2026-11-01T01:00:00Z", "failed", 4.0),
         _run("3", "2026-11-03T10:00:00Z", "success", 30.0), _run("3", "2026-11-03T10:00:00Z", "success", 30.0, ws="b"),  # two workspaces, one MR
         _run("4", "2026-11-03T11:00:00Z", "canceled", 9.0), _run("5", "2026-11-03T12:00:00Z", "running", None),
         _run("6", "2026-11-03T13:00:00Z", "success", 10.0, project="acme/other", ref="!7"),  # same ref, another project: its own MR
         _run("7", "2026-10-31T13:00:00Z", "failed", 1.0)]  # before the window
_ag = dash.ci_aggregate(_runs, "2026-11-01", "2026-11-03")
assert [d["day"] for d in _ag["days"]] == ["2026-11-01", "2026-11-02", "2026-11-03"]
assert [(d["success"], d["failed"], d["canceled"], d["running"]) for d in _ag["days"]] == [(1, 1, 0, 0), (0, 0, 0, 0), (2, 0, 1, 1)]
assert _ag["days"][2]["median_success"] == 20.0 and _ag["days"][1]["median_success"] is None and _ag["days"][0]["median_failed"] == 4.0
assert _ag["totals"] == {"success": 3, "failed": 1, "canceled": 1, "running": 1, "runs": 6,
                         "median_success": 20.0, "p90_success": 30.0, "median_failed": 4.0}, _ag["totals"]
_w = {(m["project"], m["ref"]): m for m in _ag["mrs"]}
assert len(_w) == 2 and [m["project"] for m in _ag["mrs"]] == ["acme/other", "acme/widgets"]  # newest run first
_m = _w[("acme/widgets", "!7")]
assert (_m["success"], _m["failed"], _m["canceled"], _m["running"]) == (2, 1, 1, 1) and _m["median"] == 20.0
assert _m["statuses"] == ["failed", "success", "success", "canceled", "running"] and _m["last"]["status"] == "running"
# fetching: a finished pipeline's duration is asked once, then served from `seen`; a running one is asked again next time
_rows = [{"name": "a", "project": "acme/widgets", "forge": "gitlab", "mr": {"iid": 7, "ref": "!7", "web_url": "u"}},
         {"name": "gone", "project": "acme/widgets", "mr": None}]
_pls = [{"id": 1, "status": "success", "created_at": "2026-11-02T00:00:00Z", "web_url": "p1"},
        {"id": 2, "status": "running", "created_at": "2026-11-02T01:00:00Z", "web_url": "p2"},
        {"id": 3, "status": "failed", "created_at": "2026-10-01T00:00:00Z", "web_url": "p3"}]
with mock.patch.object(dash.greenlight, "gl_all", return_value=_pls), mock.patch.object(dash.greenlight, "gl", return_value={"duration": 90}) as _gl:
    _seen = {}
    _rs, _er = dash.ci_runs(_rows, "2026-11-01T00:00:00Z", _seen)
    assert not _er and [(x["id"], x["status"], x["dur"]) for x in _rs] == [("gl1", "success", 1.5), ("gl2", "running", None)]
    assert _gl.call_count == 1 and list(_seen) == ["gl1"]
    _rs, _ = dash.ci_runs(_rows, "2026-11-01T00:00:00Z", _seen)
    assert _gl.call_count == 1 and _rs[0]["ws"] == "a" and _rs[0]["dur"] == 1.5
with mock.patch.object(dash.greenlight, "gl_all", side_effect=RuntimeError("403")):
    assert dash.ci_runs(_rows, "2026-11-01T00:00:00Z") == ([], ["a: 403"])  # one MR's error is reported, not raised
print("ci ok")
