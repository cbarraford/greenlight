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
# The Answer button must target the waiting pane, even when a finished review has the newest output.
_waiting = {"state": "waiting", "paneKey": "parent:leaf", "lastAssistantMessage": "Which branch?"}
_term = lambda tab, leaf, when: {"worktreeId": "w1", "agentIdentity": "claude", "tabId": tab, "leafId": leaf,
                                "handle": tab, "lastOutputAt": when}
_terminals = {"terminals": [_term("parent", "leaf", 10), _term("review", "leaf", 20)]}
with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws", "worktreeId": "w1"}, _waiting)}), \
        mock.patch.object(dash.greenlight, "orca", side_effect=[_terminals, {"terminal": {"tail": ["❯"]}}]) as o:
    assert dash.waiting_term("ws")[2] == "parent"
    assert o.call_args.args == ("terminal", "read", "--terminal", "parent", "--limit", "60")
with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws", "worktreeId": "w1"}, _waiting)}), \
        mock.patch.object(dash.greenlight, "orca", return_value=_terminals) as o:
    _waiting["paneKey"] = "missing:leaf"
    assert "no terminal" in dash.answer("ws", "yes")["error"] and o.call_count == 1
    _waiting["paneKey"] = "parent:leaf"
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
import os, time
# All output's refresh: an entry keeps its id as the session grows (the page appends only unseen ids), and Load earlier pages back
d = Path(tempfile.mkdtemp())
say = lambda n: json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"m{n}"}]}}) + "\n"
(d / "s1.jsonl").write_text("".join(say(n) for n in range(70)))
with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws", "path": "/w"}, {})}), \
        mock.patch.object(dash, "transcript_files", side_effect=lambda _: [(d / "s1.jsonl", "claude")]):
    a = dash.transcript("ws")
    assert [e["text"] for e in a["entries"]] == [f"m{n}" for n in range(10, 70)] and a["more"] == "s1:10"
    with (d / "s1.jsonl").open("a") as f:
        f.write(say(70))
    b = dash.transcript("ws")
    old = {e["id"]: e["text"] for e in a["entries"]}
    assert all(old.get(e["id"], e["text"]) == e["text"] for e in b["entries"]), "an entry's id changed meaning as the session grew"
    assert [e["text"] for e in b["entries"] if e["id"] not in old] == ["m70"]
    c = dash.transcript("ws", a["more"])
    assert c["before"] == "s1:10" and [e["text"] for e in c["entries"]][-1] == "m9" and c["entries"][0]["role"] == "sep" and c["more"] is None
    assert all(e["kind"] == "claude" for e in a["entries"]) and a["recent"] == []  # whose words each is; one session: nothing else recent
    # a second session written to just now (a review the agent started) rides along with the newest page, never starved out
    (d / "s0.jsonl").write_text("".join(say(100 + n) for n in range(25)))
    os.utime(d / "s0.jsonl", (time.time() - 5, time.time() - 5))
    with mock.patch.object(dash, "transcript_files", side_effect=lambda _: [(d / "s1.jsonl", "claude"), (d / "s0.jsonl", "codex")]), \
            mock.patch.object(dash, "_codex_entries", dash._claude_entries):
        r = dash.transcript("ws")
        assert [e["text"] for e in r["recent"]] == [f"m{100 + n}" for n in range(5, 25)] and r["recent"][0]["id"] == "s0:5" and r["recent"][0]["kind"] == "codex"
        os.utime(d / "s0.jsonl", (time.time() - 3600, time.time() - 3600))
        assert dash.transcript("ws")["recent"] == [] and dash.transcript("ws", "s1:10")["recent"] == []  # quiet for a while, or Load earlier: none
with mock.patch.object(dash, "live_agents", return_value={}):
    assert dash.transcript("gone", "s1:10") == {"error": "no workspace gone", "before": "s1:10"}  # a failed Load earlier is told from a failed refresh
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
assert all("gate push --execute" in opt(a, "--prompt") and "greenlight.py" in opt(a, "--prompt") for a in calls if is_create(a))
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
# a workspace the scan hasn't seen yet (just created) is a puck at once, with its working agent; every puck carries the ADE's repo name,
# which the dashboard matches against the picked projects (a window's paths are older than the workspace)
with mock.patch.object(dash, "live_agents", return_value={
        "w1": ({"displayName": "ws-gh", "worktreeId": "w1", "path": "/nonexistent", "comment": "Merge MR for #20", "branch": "refs/heads/b", "repo": "greenlight"}, {}),
        "w2": ({"displayName": "openai-live-voice", "worktreeId": "w2", "path": "/w/olv", "comment": "Switch voice", "branch": "refs/heads/olv", "repo": "greenlight",
                "agents": [{"state": "working"}]}, {"state": "working", "agentType": "claude"})}):
    _pk = {p["name"]: p for p in dash.snapshot()["pucks"]}
assert (_pk["ws-gh"]["repo"], _pk["openai-live-voice"]["repo"]) == ("greenlight", "greenlight")
assert (_pk["openai-live-voice"]["state"], _pk["openai-live-voice"]["agent"]["state"], _pk["openai-live-voice"]["agent"]["type"]) == ("working", "working", "claude")
# A finished review session may update after the parent session. Both PR workspaces must still reach the Agent view.
_active = {"state": "working", "agentType": "codex", "updatedAt": 10}
_finished = {"state": "done", "agentType": "claude", "updatedAt": 20}
_worktrees = [{"worktreeId": n, "displayName": n, "path": "/w/" + n, "branch": "refs/heads/" + n,
               "repo": "greenlight", "comment": "Merge PR #" + number, "agents": [_active, _finished]}
              for n, number in (("egress-guard", "65"), ("smart-orchestrator", "66"))]
with mock.patch.object(dash.greenlight, "orca", return_value={"worktrees": _worktrees}):
    _selected = dash.live_agents()
assert {n: a["state"] for n, (_, a) in _selected.items()} == {"egress-guard": "working", "smart-orchestrator": "working"}
with mock.patch.object(dash, "live_agents", return_value=_selected):
    _visible = {p["name"]: p for p in dash.snapshot()["pucks"]}
assert {n: _visible[n]["agent"]["state"] for n in _selected} == {"egress-guard": "working", "smart-orchestrator": "working"}
# lifestage.py's place for it rides on the puck (this row predates `life`, so it's worked out here), with the live agent's state on top
assert (pk["life"]["at"], pk["life"]["pile"], pk["life"]["holder"], pk["life"]["suggestion"]["label"]) == ("4.1", "working", "Greenlight", "Mark it ready")
with mock.patch.object(dash, "live_agents", return_value={"w1": ({"displayName": "ws-gh", "worktreeId": "w1", "path": "/nonexistent", "comment": "Merge MR for #20",
                                                                  "branch": "refs/heads/b"}, {"state": "waiting", "lastAssistantMessage": "Which one?"})}):
    asked = dash.snapshot()["pucks"][0]["life"]
assert (asked["at"], asked["pile"], asked["holder"], asked["suggestion"]["label"]) == ("4.1", "yours", "you", "Answer your agent")
# the hover card's button is written ahead: a snapshot carries the cached step only while it's for the puck's current state
assert pk["suggestion"] is None  # nothing written yet: the app queues `suggest`, the card says "Please wait…"
LIVE = {"w1": ({"displayName": "ws-gh", "worktreeId": "w1", "path": "/nonexistent", "comment": "Merge MR for #20", "branch": "refs/heads/b"}, {})}
def suggestion(cached):
    (dash.DASH / "suggest").mkdir(parents=True, exist_ok=True)
    (dash.DASH / "suggest" / "ws-gh.json").write_text(json.dumps(cached))
    with mock.patch.object(dash, "live_agents", return_value=LIVE):
        return dash.snapshot()["pucks"][0]["suggestion"]
assert suggestion({"sig": pk["sig"], "label": "Mark ready", "prompt": "Mark #23 ready."}) == {"label": "Mark ready", "prompt": "Mark #23 ready.", "advice": ""}
assert suggestion({"sig": "stale", "label": "Mark ready", "prompt": "Mark #23 ready."}) is None  # the state moved on since
assert suggestion({"sig": pk["sig"], "prompt": "Mark #23 ready."}) is None  # an old entry without a label gets rewritten
assert suggestion({"sig": pk["sig"], "label": "", "prompt": "There's an issue with the selected model (gemini-4-hydrogen-medium).", "advice": ""}) is None  # old model errors must be regenerated
assert suggestion({"sig": pk["sig"], "label": "", "prompt": "Mark #23 ready.", "advice": "", "ok": True}) == {"label": "", "prompt": "Mark #23 ready.", "advice": ""}  # a real one-line reply stays cached
with mock.patch.object(dash, "snapshot", return_value={"pucks": [pk]}), mock.patch.object(dash, "ask", side_effect=AssertionError("asked the model for a cached step")):
    (dash.DASH / "suggest" / "ws-gh.json").write_text(json.dumps({"sig": pk["sig"], "label": "Mark ready", "prompt": "Mark #23 ready."}))
    assert dash.suggest("ws-gh")["label"] == "Mark ready"
# a failed model run answers "": `claude -p` prints its error on stdout and exits 1, and that text became the console's
# suggestion, which was then launched into an agent as its task ("There's an issue with the selected model (gemini-...)")
_err = "There's an issue with the selected model (claude-gone-1). It may not exist or you may not have access to it. Run --model to pick a different model.\n"
_run = lambda rc, out: mock.patch.object(dash.subprocess, "run", return_value=_sp_run.CompletedProcess([], rc, stdout=out, stderr=""))
import subprocess as _sp_run
with _run(1, _err):
    assert dash.ask("p", "claude-gone-1", "low") == ""
with _run(0, "Mark ready\nMark #23 ready.\n"):
    assert dash.ask("p", "claude-haiku-5-5", "low") == "Mark ready\nMark #23 ready.\n"
(dash.DASH / "suggest" / "ws-gh.json").unlink()
with mock.patch.object(dash, "snapshot", return_value={"pucks": [pk]}), mock.patch.object(dash, "facts", return_value={}), _run(1, _err):
    _s = dash.suggest("ws-gh", model="claude-gone-1")
assert "prompt" not in _s and "no answer" in _s["error"] and "issue" not in json.dumps(_s) and not (dash.DASH / "suggest" / "ws-gh.json").exists(), _s  # nothing cached: asked again next time
# a one-line reply (no label line) is cached and answers the next call without asking again
with mock.patch.object(dash, "snapshot", return_value={"pucks": [pk]}), mock.patch.object(dash, "facts", return_value={}), _run(0, "Mark #23 ready.\n"):
    assert dash.suggest("ws-gh")["prompt"] == "Mark #23 ready."
with mock.patch.object(dash, "snapshot", return_value={"pucks": [pk]}), mock.patch.object(dash, "ask", side_effect=AssertionError("asked again for a cached one-line reply")):
    assert dash.suggest("ws-gh")["prompt"] == "Mark #23 ready."
(dash.DASH / "suggest" / "ws-gh.json").unlink()
# A failed Insights refresh must leave its last good layout in the cache and show an error.
_page_file = dash.DASH / "pages" / "ws-gh.json"
_page_file.parent.mkdir(parents=True, exist_ok=True)
_layout = {"puck": None, "tiles": [{"widget": "note", "title": "Last good", "props": {"text": "ok"}}], "wide": None}
_page_file.write_text(json.dumps({"v": dash.PAGE_V, "sig": dash.sig(pk), "layout": _layout}))
with mock.patch.object(dash, "facts", return_value={"puck": pk}), _run(1, _err):
    _page = dash.page("ws-gh", fresh=True, model="claude-gone-1")
assert _page["layout"] == _layout and "designer returned no layout" in _page["error"]
assert json.loads(_page_file.read_text())["layout"] == _layout
print("ok")
# advice: a rule when you're the blocker or an agent step has no agent; None -> suggest writes it; "" -> no advice at all
_r = lambda step, owner, action="", cmd=None, ag="none", mr=None: {"next": {"step": step, "owner": owner, "action": action, "cmd": cmd},
                                                                 "agent": {"state": ag}, "mr": mr}
assert dash.advice(_r("review", "me", "review it", mr={"pipeline": "failed"})).startswith("CI failed, so read the diff")
assert dash.advice(_r("review", "me", "review it", mr={"pipeline": "success"})).startswith("Read the diff")
assert "replies" in dash.advice(_r("review", "me", "author replied on your threads: re-review"))
assert "approv" not in dash.advice(_r("merge", "me", "mergeable, nothing blocks it: merge it?")).lower()  # no-approval repo
assert dash.advice(_r("merge", "me", "mergeable, nothing blocks it: merge it?")).startswith("Nothing blocks it")
assert "sign off deploy" in dash.advice(_r("deploy", "me", "deploy: x Sign it off?", ["python3", "g.py", "signoff", "p", "1", "deploy"]))
assert "rebase" in dash.advice(_r("merge", "me", "GitLab needs a rebase to merge (no conflict): rebase it?"))
assert dash.advice(_r("ci_green", "agent")) == "No agent is on it: run Fix CI."
assert dash.advice(_r("ci_green", "agent", ag="working")) == ""
assert dash.advice(_r("qa", "agent", "qa: x (add the label `qa-ok` when it's met)")) is None  # your own step: the model writes it
assert dash.advice(_r("approved", "reviewers")) == "" and dash.advice(_r("x", "greenlight", ag="waiting")).startswith("Answer")
# a needed advice line makes an old cached suggestion stale; a rule-covered puck doesn't ask for one
assert not dash.fresh_suggest({"sig": "s", "label": "L"}, {"sig": "s", "advice": None})
assert dash.fresh_suggest({"sig": "s", "label": "L", "advice": "Do x."}, {"sig": "s", "advice": None})
assert dash.fresh_suggest({"sig": "s", "label": "L", "advice": ""}, {"sig": "s", "advice": None})  # the model had none: final for this sig
assert dash.fresh_suggest({"sig": "s", "label": "L"}, {"sig": "s", "advice": "rule"})
assert dash.split_advice("Advice: Ask QA to sign it off.\nCheck the QA board for !5.") == ("Ask QA to sign it off.", "Check the QA board for !5.")
assert dash.split_advice("**Advice:** Do x.\nPrompt.") == ("Do x.", "Prompt.") and dash.split_advice("Just a prompt.") == ("", "Just a prompt.")
# progress: no MR is step 0 (mr_open is the implement step); a review checkout is one step; a goal-only tree has none
_p = lambda **k: dash.progress({"project": "o/r", "forge": "gitlab", "mr": None, "next": {"step": "freeform", "owner": "agent"}, **k})
assert _p()[2] == 0 and _p()[1][0] == "mr_open"
assert _p(mr={"role": "review"}, next={"step": "review", "owner": "me"})[1:] == (["review"], 0)
assert _p(forge=None)[1:] == ([], None)
# a merged MR with a step after merge: still your move, on that step, not done
_m = {"project": "o/r", "forge": "gitlab", "mr": {"state": "merged"}, "agent": {"state": "none"}, "next": {"step": "deploy", "owner": "me", "action": "deploy: x Sign it off?"}}
with mock.patch.object(dash.greenlight, "lifecycle", return_value=("o.md", ["mr_open", "merge", "deploy"])):
    assert dash.state_of(_m)[0] == "raised" and dash.progress(_m)[1:] == (["mr_open", "merge", "deploy"], 2)
print("advice ok")
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
    "agent": {"state": "waiting"}, "next": {"step": "ci_green", "owner": "agent", "action": ""}, "life": {"stage_name": "Review", "sub_name": "Respond"},
    "mr": {"ref": "!7", "state": "opened", "pipeline": "failed", "threads": 2, "rebase": True, "author": "x", "by": {"name": "x", "avatar": "u"}}}]}))
_t = dash.trees()
(dash.greenlight.STATE / "last.json").write_text(_last)
dash.greenlight.orca_cli = _orca
assert [(t["ade"], t["path"]) for t in _t] == [("cmux", "/cmux"), ("herdr", "/herdr"), ("t3", "/t3"), ("cmux", "/r/a")], _t
_mr = _t[-1].pop("mr")
assert [m["kind"] for m in _t[-1].pop("moves")] == ["ci"]  # its pipeline failed: Fix CI, though the MR isn't yours
assert _mr["ref"] == "!7" and _mr["threads"] == 2 and _mr["rebase"] and "author" not in _mr and _mr["by"] == {"name": "x", "avatar": "u"}, _mr  # the card byline
# the live agent (working) wins over the scan's (waiting): not "needs you"
assert _t[-1] == {"ade": "cmux", "project": "r", "name": "a", "path": "/r/a", "branch": "x", "main": False, "agent": "working",
                  "goal": "", "pinned": False, "active": None, "since": None, "created": None,
                  "puck": "r-a", "state": "working", "why": "agent working",
                  "steps": [], "step_index": None, "step": "ci_green", "owner": "agent", "advice": "", "life": {"stage_name": "Review", "sub_name": "Respond"}}, _t[-1]

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
assert g["source"] == "bundled" and "automerge" in g["options"], g
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
# conflicts › ci › threads › review, on your MR or one you review; merge only when none of those apply
mine = {**r, "next": {"owner": "greenlight", "step": "merge", "action": "merge", "cmd": ["glab", "mr", "merge", "58", "-R", "octo/greenlight", "--yes", "--squash"]},
        "mr": {**r["mr"], "role": "author", "conflicts": True, "pipeline": "failed", "pipeline_url": "https://ci/1",
               "failed_jobs": [{"name": "unit", "stage": "test", "url": "https://ci/1/j"}], "todo_threads": ['sam, a.py:3: "why?"', "sam"]}}
mv = dash.moves(mine)
assert [m["kind"] for m in mv] == ["conflicts", "ci", "threads"] and mv[2]["n"] == 2, mv
assert "- unit (test): https://ci/1/j" in mv[1]["prompt"] and "https://ci/1" in mv[1]["prompt"] and '- sam, a.py:3: "why?"\n- sam' in mv[2]["prompt"]
_clean = {"conflicts": False, "pipeline": "success", "todo_threads": []}
assert [m["kind"] for m in dash.moves({**mine, "mr": {**mine["mr"], "role": "review"}})] == ["conflicts", "ci", "threads"]  # a review's fixes too
mv = dash.moves({**mine, "mr": {**mine["mr"], **_clean, "role": "review"}})
assert [m["kind"] for m in mv] == ["merge"] and mv[0]["how"] == "Squash", mv
assert dash.moves({**mine, "mr": {**mine["mr"], **_clean, "role": "review"}, "next": {**mine["next"], "cmd": None}}) == []  # no cmd: greenlight won't merge it now
_rv = {**mine, "next": {**mine["next"], "owner": "me", "action": "mergeable, and nobody else reviews this repo: review it, then merge?"},
       "mr": {**mine["mr"], "conflicts": False, "pipeline": "success", "todo_threads": []}}
assert [m["kind"] for m in dash.moves(_rv)] == ["review", "merge"]  # a review doesn't hide Merge (a fix does)
_dr = {**_rv, "next": {"owner": "me", "step": "ready", "action": "goal says keep draft…", "cmd": ["gh", "pr", "ready", "58", "-R", "o/g"]}}
assert [m["kind"] for m in dash.moves(_dr)] == ["ready"] and dash.moves({**_dr, "next": {**_dr["next"], "owner": "greenlight"}}) == []  # greenlight undrafts it itself
# auto-run: per repo, per kind, with a gate; never on a held/parked goal; merge gets True
assert dash.settings("autorun-set", "../x", "{}")["error"]
a = dash.settings("autorun-set", "greenlight", json.dumps({"ci": 1, "threads": 9, "nope": 0, "merge": 0}))
assert a["on"] == {"ci": 1, "merge": 0} and [k["kind"] for k in a["kinds"]] == ["ci", "conflicts", "threads", "review", "merge"], a
mv = {m["kind"]: m for m in dash.moves(mine)}
assert mv["ci"]["auto"].startswith(dash.AUTO_HEAD + "You have my OK to push") and mv["ci"]["prompt"] in mv["ci"]["auto"] and mv["ci"]["sha"] == "abc123"
assert "auto" not in mv["conflicts"] and "auto" not in mv["threads"]
assert not any("auto" in m for m in dash.moves({**mine, "goal": "fix it (hold)"}))
assert "audit.py gate push --execute`" in mv["ci"]["auto"] and "gate post" not in mv["ci"]["auto"]  # the contracts and exact push in one command
_cm = {**mine, "mr": {**mine["mr"], **_clean, "role": "review"}}
with mock.patch.object(dash.audit, "kick") as _k, mock.patch.object(dash.audit, "same_target", return_value=True):  # an unattended merge waits on the semantic audit of this head
    _m = dash.moves(_cm)[0]
    assert "auto" not in _m and _m["audit"] == "auditing" and _k.call_args.args == ("/w/b", "abc123"), (_m, _k.call_args)
    assert _k.call_args.kwargs["spec"]["target"] == _cm["mr"].get("target_branch") and _k.call_args.kwargs["spec"]["goal"] == dash.audit.goal_of(_cm, _cm["mr"])
    _tag = dash.audit.ident(_cm, _cm["mr"])
    dash.audit.save("/w/b", "abc123", {"ok": False, "failed": [{"check": "diff_matches_goal", "contract": "NoScopeCreep", "p_broken": 0.9}]}, _tag)
    _m = dash.moves(_cm)[0]
    assert "auto" not in _m and _m["audit"] == "NoScopeCreep (diff_matches_goal, p=0.90)", _m  # held, the failed contract named
    dash.audit.save("/w/b", "abc124", {"ok": True, "failed": []}, _tag)  # another head's pass doesn't count
    assert "auto" not in dash.moves(_cm)[0]
    dash.audit.save("/w/b", "abc123", {"ok": True, "failed": []})  # nor one for another target/goal
    assert "auto" not in dash.moves(_cm)[0]
    dash.audit.save("/w/b", "abc123", {"ok": True, "failed": []}, _tag)
    assert dash.moves(_cm)[0]["auto"] is True
assert not any("auto" in m for m in dash.moves({**mine, "mr": {**mine["mr"], "role": "review"}}))  # never pushes unattended to someone else's branch
assert not any("auto" in m for m in dash.moves(_rv))  # auto-merge never jumps a pending review
dash.settings("autorun-set", "greenlight", "{}")
assert not any("auto" in m for m in dash.moves(mine))
# Merge: greenlight's own cmd, pinned to the head it saw; then the snapshot says merged so the card stops offering it
_ran, _run = [], dash.greenlight.run
dash.greenlight.run = lambda cmd, **k: _ran.append(cmd) or ""
_lj = dash.greenlight.STATE / "last.json"; _old = _lj.read_text() if _lj.exists() else None
pending_row = {**mine, "next": {**mine["next"], "owner": "ci", "action": "automerge waits on the semantic audit of abc123"},
               "mr": {**mine["mr"], **_clean}}
_lj.write_text(json.dumps({"ts": 1, "rows": [pending_row]}))
assert [m["kind"] for m in dash.moves(pending_row)] == ["merge"]
assert dash.tree_merge("/w/b")["ok"] and _ran[-1] == mine["next"]["cmd"] + ["--sha", "abc123"]
_ran.clear()
_lj.write_text(json.dumps({"ts": 1, "rows": [mine]}))
assert dash.tree_merge("/w/b")["error"] and not _ran  # conflicts/CI/threads outrank merge, so it isn't offered
_lj.write_text(json.dumps({"ts": 1, "rows": [{**mine, "mr": {**mine["mr"], "conflicts": False, "pipeline": "success", "todo_threads": []}}]}))
assert dash.tree_merge("/w/b") == {"ok": True, "ref": "!58", "target": "main", "how": "Squash"}
assert _ran == [mine["next"]["cmd"] + ["--sha", "abc123"]], _ran
assert dash.tree_merge("/w/b")["error"] and dash.tree_merge("/w/x")["error"] and len(_ran) == 1  # merged now; unknown path
_auto_row = {**mine, "forge": "gitlab", "mr": {**mine["mr"], **_clean, "iid": 58},
             "next": {**mine["next"], "cmd": [sys.executable, "audit.py", "checked-merge", "gitlab", mine["project"], "58", "main", "abc123", "base1", *mine["next"]["cmd"]]}}
dash.audit.save("/w/b", "abc123", {"ok": True, "failed": [], "target_oid": "base1"}, dash.audit.ident(_auto_row, _auto_row["mr"]))
_pending_auto = {**_auto_row, "next": {**_auto_row["next"], "cmd": mine["next"]["cmd"]}}
_lj.write_text(json.dumps({"ts": 1, "rows": [_pending_auto]}))
with mock.patch.object(dash, "moves", return_value=[{"kind": "merge", "how": "Squash", "target": "main", "auto": True}]), \
     mock.patch.object(dash.audit, "same_target", return_value=True):
    assert dash.tree_merge("/w/b", automatic=True)["ok"]
assert _ran[-1][2:9] == ["checked-merge", "gitlab", mine["project"], "58", "main", "abc123", "base1"]
assert _ran[-1][9:] == mine["next"]["cmd"]  # pending snapshot cannot bypass auto guard
_lj.write_text(json.dumps({"ts": 1, "rows": [_auto_row]}))
with mock.patch.object(dash, "moves", return_value=[{"kind": "merge", "how": "Squash", "target": "main", "auto": True}]):
    assert dash.tree_merge("/w/b")["ok"]
assert _ran[-1] == mine["next"]["cmd"] + ["--sha", "abc123"], _ran[-1]  # a human click ignores the auto audit receipt
_lj.write_text(json.dumps({"ts": 1, "rows": [_dr]}))
assert dash.tree_ready("/w/b")["ok"] and _ran[-1] == _dr["next"]["cmd"] and dash.tree_ready("/w/x")["error"] and len(_ran) == 4
_same_pr = {**mine, "id": "peer-id", "path": "/w/peer", "name": "peer",
            "mr": {**mine["mr"], "sha": "stale-head", "conflicts": False, "pipeline": "success", "todo_threads": []}}
_lj.write_text(json.dumps({"scan_id": "before", "scan_started_ns": 1,
                           "rows": [{**mine, "mr": {**mine["mr"], "conflicts": False, "pipeline": "success", "todo_threads": []}}, _same_pr]}))
_stub_run = dash.greenlight.run
def concurrent_merge(_cmd, **_kw):
    snap = json.loads(_lj.read_text())
    snap["scan_id"] = "other-merge"
    _lj.write_text(json.dumps(snap))
    return ""
dash.greenlight.run = concurrent_merge
assert dash.tree_merge("/w/b")["ok"]
assert json.loads(_lj.read_text())["rows"][0]["mr"]["state"] == "merged"
assert json.loads(_lj.read_text())["rows"][1]["mr"]["state"] == "merged"
assert json.loads(_lj.read_text())["scan_id"] not in ("before", "other-merge")
dash.greenlight.run = _stub_run
# Deploy: a merged MR's step after the merge runs its lifecycle Run command in the worktree, then signs off; a failure signs nothing off
import tempfile
_wt = tempfile.mkdtemp()
(dash.greenlight.STATE / "lifecycles").mkdir(exist_ok=True)
(dash.greenlight.STATE / "lifecycles/dep.md").write_text(dash.greenlight.lifecycle_md("dep", {"stages": [{"key": "merge"}, {"key": "deploy", "req": "Out.", "done": "signoff", "run": "echo hi > ran.txt"}]}))
_dep = {"path": _wt, "project": "acme/dep", "mr": {"state": "merged", "ref": "!9"}, "next": {"step": "deploy", "owner": "me", "cmd": ["python3", "greenlight.py", "signoff", "acme/dep", "9", "deploy"]}}
assert dash.moves(_dep) == [{"kind": "signoff", "step": "deploy", "run": "echo hi > ran.txt"}]
_lj.write_text(json.dumps({"ts": 1, "rows": [_dep]}))
assert dash.tree_signoff(_wt)["ok"] and open(_wt + "/ran.txt").read() == "hi\n" and _ran[-1] == _dep["next"]["cmd"] and len(_ran) == 5
(dash.greenlight.STATE / "lifecycles/dep.md").write_text(dash.greenlight.lifecycle_md("dep", {"stages": [{"key": "merge"}, {"key": "deploy", "done": "signoff", "run": "echo nope >&2; exit 3"}]}))
assert "exited 3: nope" in dash.tree_signoff(_wt)["error"] and len(_ran) == 5 and dash.tree_signoff("/w/x")["error"]
# a row scanned before greenlight.md dropped deploy: no Deploy button, and a click signs nothing off
_stale = {**_dep, "project": "cbarraford/greenlight", "next": {**_dep["next"], "cmd": ["python3", "greenlight.py", "signoff", "cbarraford/greenlight", "9", "deploy"]}}
assert dash.moves(_stale) == []
_lj.write_text(json.dumps({"ts": 1, "rows": [_stale]}))
assert "doesn't say" in dash.tree_signoff(_wt)["error"] and len(_ran) == 5
(dash.greenlight.STATE / "lifecycles/dep.md").write_text(dash.greenlight.lifecycle_md("dep", {"stages": [{"key": "deploy", "done": "signoff"}, {"key": "merge"}]}))
assert dash.moves(_dep) == []  # a step moved before the merge isn't asked after it either
# the chat's board: unfinished workspaces, yours first, finished ones counted
_b = lambda n, o, st="freeform": {"id": n, "name": n, "project": "o/g", "path": "/w/" + n, "agent": {"state": "idle"}, "next": {"step": st, "owner": o, "action": n + " act"}}
_w = lambda n, repo="g", **k: {"worktreeId": n, "displayName": n, "path": "/w/" + n, "repo": repo, "branch": "refs/heads/" + n, **k}
_live = lambda *ns: {n: (_w(n), {}) for n in ns}
_lj.write_text(json.dumps({"ts": 1, "rows": [_b("q", "agent"), _b("m", "me"), _b("d", "done", "done")]}))
with mock.patch.object(dash, "live_agents", return_value=_live("q", "m", "d")):
    assert dash.board().splitlines() == ["- m (no MR, o/g): freeform · on me · m act", "- q (no MR, o/g): freeform · on agent · q act · do: No agent is running: start one on the goal.",
                                         "(1 more finished)"], dash.board()
    assert {identity[0] for identity in dash.board(with_ids=True)[1].members} == {"q", "m", "d"}  # finished worktree remains in the window
    os.environ["GREENLIGHT_ONLY"] = '["/w/q"]'  # the focused window's workspaces only (an older app: a bare list of paths)
    assert dash.board().splitlines() == ["- q (no MR, o/g): freeform · on agent · q act · do: No agent is running: start one on the goal."], dash.board()
    assert dash.board(only=["/w/m", "/w/d"]).splitlines() == ["- m (no MR, o/g): freeform · on me · m act", "(1 more finished)"]
    del os.environ["GREENLIGHT_ONLY"]
# `ade worktree create` started a workspace and its agent after the scan: it's on the board at once, as on the dashboard, with the live
# agent; one deleted since the scan is gone; and the scan's stale agent state gives way to the live one
_new = _w("openai-live-voice", comment="Switch voice", agents=[{"state": "working"}])
_now = _live("q") | {"openai-live-voice": (_new, {"state": "working"}), "x": (_w("x", "other", comment="Elsewhere"), {"state": "working"})}
_now["q"] = (_now["q"][0], {"state": "working"})
with mock.patch.object(dash, "live_agents", return_value=_now):
    b = dash.board().splitlines()
    assert b == ["- q (no MR, o/g): freeform · on agent · q act · agent working",  # live: an agent is on it now, so no "start one"
                 "- openai-live-voice (no MR, g): freeform · on parked · no lifecycle: goal only · agent working",
                 "- x (no MR, other): freeform · on parked · no lifecycle: goal only · agent working"], b  # m and d were deleted
    # the window's scope as the app sends it now: its worktrees' paths as listed before the create, and their repos
    os.environ["GREENLIGHT_ONLY"] = json.dumps({"paths": ["/w/q"], "repos": ["g"]})
    assert [l.split(" ")[1] for l in dash.board().splitlines()] == ["q", "openai-live-voice"], dash.board()  # x's repo is another window's
    os.environ["GREENLIGHT_ONLY"] = json.dumps({"paths": ["/w/q"], "repos": []})
    assert [l.split(" ")[1] for l in dash.board().splitlines()] == ["q"]
    del os.environ["GREENLIGHT_ONLY"]
with mock.patch.object(dash, "live_agents", side_effect=RuntimeError("orca: not running")):  # the ADE not answering: the scan alone, as before
    assert dash.board().splitlines()[0] == "- m (no MR, o/g): freeform · on me · m act"
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

# forge: Chief's list of open MRs/PRs and issues, one line each with its status, age and the workspace already on it
now = 1_800_000_000
mr = dash.gl_item({"iid": 7, "source_branch": "b", "draft": True, "has_conflicts": True, "detailed_merge_status": "draft_status",
                   "references": {"full": "acme/widgets!7"}, "title": "Fix it", "web_url": "u", "updated_at": "2027-01-15T08:00:00.000Z"}, "mine")
assert dash.forge_line(mr, {"acme/widgets!7": "fix-it"}, now) == "- [acme/widgets!7](u) Fix it · draft · conflicts · updated today · workspace: fix-it", dash.forge_line(mr, {"acme/widgets!7": "fix-it"}, now)
iss = dash.gh_item({"number": 3, "title": "Bug", "url": "v", "updatedAt": "2027-01-10T08:00:00Z", "repository": {"nameWithOwner": "octo/app"},
                    "labels": {"nodes": [{"name": "bug"}]}, "closedByPullRequestsReferences": {"totalCount": 0}}, "issue")
assert dash.forge_line(iss, {}, now) == "- [octo/app#3](v) Bug · bug · no PR · updated 5d ago", dash.forge_line(iss, {}, now)
pr = dash.gh_item({"number": 4, "title": "Feat", "url": "w", "isDraft": False, "updatedAt": "", "reviewDecision": "CHANGES_REQUESTED", "mergeable": "CONFLICTING",
                   "repository": {"nameWithOwner": "octo/app"}, "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}}, "review")
assert dash.forge_line(pr, {}, now) == "- [octo/app#4](w) Feat · conflicts · CI failure · changes requested", dash.forge_line(pr, {}, now)

# record / history / page / stream resilience: corrupt state.json or cache never breaks dashboard updates
dash.DASH.mkdir(parents=True, exist_ok=True)
_sf = dash.DASH / "state.json"
_sf.write_text('{"alpha": ["working", "ci", "agent"]}]}')  # trailing bytes from a concurrent non-atomic write
_p1 = {"name": "alpha", "state": "needs", "step": "review", "owner": "me", "action": "review it"}
dash.record([_p1])
assert json.loads(_sf.read_text()) == {"alpha": ["needs", "review", "me"]}
assert not list(dash.DASH.glob(".state.json.*.tmp"))
_sf.write_text("[]")  # valid JSON, wrong shape
dash.record([_p1])
assert json.loads(_sf.read_text()) == {"alpha": ["needs", "review", "me"]}
dash.record([{**_p1, "state": "done", "step": "done", "owner": "done", "action": "merged"}])
_hist_file = dash.DASH / "alpha.jsonl"
with open(_hist_file, "a") as _hf:
    _hf.write("{corrupt line\n")
_hist = dash.history("alpha")
assert len(_hist) == 1 and "needs → done" in _hist[0]["text"], _hist
_page_cache = dash.DASH / "pages" / "alpha.json"
_page_cache.parent.mkdir(parents=True, exist_ok=True)
_page_cache.write_text("{corrupt")
with mock.patch.object(dash, "facts", return_value={"puck": {**_p1, "agent": {"state": "idle"}, "mr": None}}), \
     mock.patch.object(dash, "design", return_value={"puck": None, "tiles": [], "wide": None}):
    _pg = dash.page("alpha")
assert _pg["name"] == "alpha" and _pg["layout"] == {"puck": None, "tiles": [], "wide": None} and json.loads(_page_cache.read_text())["v"] == dash.PAGE_V
import contextlib as _cl, io as _io
_out = _io.StringIO()
with mock.patch.object(dash, "snapshot", return_value={"pucks": [_p1]}), \
     mock.patch.object(dash, "record", side_effect=OSError("disk full")), \
     mock.patch.object(dash, "oops") as _oops, \
     mock.patch.object(dash.time, "sleep", side_effect=StopIteration), \
     mock.patch.object(sys, "stdout", _out), \
     _cl.suppress(StopIteration):
    dash._stream(1)
assert json.loads(_out.getvalue().strip()) == {"pucks": [_p1]} and _oops.call_count == 1
_out2 = _io.StringIO()
with mock.patch.object(dash, "snapshot", side_effect=RuntimeError("boom")), \
     mock.patch.object(dash, "oops") as _oops2, \
     mock.patch.object(dash.time, "sleep", side_effect=StopIteration), \
     mock.patch.object(sys, "stdout", _out2), \
     _cl.suppress(StopIteration):
    dash._stream(1)
assert json.loads(_out2.getvalue().strip()) == {"error": "boom"} and _oops2.call_count == 1
with mock.patch.object(dash, "snapshot", side_effect=BrokenPipeError), \
     mock.patch.object(dash, "oops") as _oops3:
    dash._stream(1)
assert _oops3.call_count == 0
with mock.patch.object(dash.os, "replace", side_effect=OSError("fail")), _cl.suppress(OSError):
    dash._write_json(dash.DASH / "fail.json", {"a": 1})
assert not list(dash.DASH.glob(".fail.json.*.tmp"))
print("resilience ok")

# per-repo qualified lifecycles (org/repo) and pre-merge / pre-MR custom signoff moves
_org = dash.settings("lifecycle-set", "team-a/shared", json.dumps({
    "stages": [{"key": "rfc", "req": "RFC ready.", "done": "signoff"}, {"key": "mr_open", "req": ""},
               {"key": "legal", "req": "Legal ok.", "done": "signoff", "run": "echo ok > legal.txt"}, {"key": "merge", "req": ""}],
    "options": {"merge_method": "squash"}, "rules": "", "intro": ""}))
assert _org["source"] == "yours" and _org["options"]["merge_method"] == "squash"
assert (dash.greenlight.STATE / "lifecycles" / "team-a__shared.md").exists()
assert dash.settings("lifecycle-get", "team-a/../x", "")["error"]
assert dash.settings("lifecycle-reset", "team-a/shared", "")["source"] == "default"
(dash.greenlight.STATE / "lifecycles/pre.md").write_text(dash.greenlight.lifecycle_md("pre", {
    "stages": [{"key": "rfc", "req": "RFC.", "done": "signoff"}, {"key": "mr_open", "req": ""},
               {"key": "legal", "req": "Legal.", "done": "signoff", "run": "echo yes > pre.txt"}, {"key": "merge", "req": ""}]}))
_open_so = {"path": "/w/pre", "project": "acme/pre", "mr": {"state": "opened", "role": "author", "ref": "!3", "conflicts": False, "pipeline": "success", "todo_threads": []},
            "next": {"step": "legal", "owner": "me", "cmd": ["python3", "greenlight.py", "signoff", "acme/pre", "3", "legal"]}}
assert dash.moves(_open_so) == [{"kind": "signoff", "step": "legal", "run": "echo yes > pre.txt"}]
# urgent fix suppresses pre-merge signoff without misrouting to review
assert [m["kind"] for m in dash.moves({**_open_so, "mr": {**_open_so["mr"], "pipeline": "failed"}})] == ["ci"]
_pre_mr_so = {"name": "pre-ws", "path": "/w/pre", "project": "acme/pre", "forge": "github", "mr": None,
              "next": {"step": "rfc", "owner": "me", "cmd": ["python3", "greenlight.py", "signoff", "acme/pre", "@b", "rfc"]}}
assert dash.moves(_pre_mr_so) == [{"kind": "signoff", "step": "rfc", "run": ""}]
# after pre-MR stage passes, progress sits at mr_open (index 1), not back at index 0
assert dash.progress({**_pre_mr_so, "next": {"step": "freeform", "owner": "agent"}}) == (0.25, ["rfc", "mr_open", "legal", "merge"], 1)
# tree_signoff for pre-MR row returns workspace name as ref (never None)
_lj2 = dash.greenlight.STATE / "last.json"
_prev_lj2 = _lj2.read_text() if _lj2.exists() else None
_lj2.write_text(json.dumps({"ts": 1, "rows": [_pre_mr_so]}))
with mock.patch.object(dash.greenlight, "run", return_value=""):
    assert dash.tree_signoff("/w/pre") == {"op": "signoff", "path": "/w/pre", "ref": "pre-ws", "step": "rfc", "ok": True, "ran": ""}
_lj2.write_text(_prev_lj2) if _prev_lj2 is not None else _lj2.unlink()
print("per-repo lifecycle ok")

# snapshot() tolerates corrupt rows with None lastAssistantMessage/last or missing agent/next dicts
_lj3 = dash.greenlight.STATE / "last.json"
_prev_lj3 = _lj3.read_text() if _lj3.exists() else None
_lj3.write_text(json.dumps({
    "ts": 1,
    "rows": [
        {"id": "wt-null", "name": "null-msg", "path": "/w/null", "project": "acme/w", "goal": None,
         "agent": {"state": "waiting", "type": "claude", "last": None}, "mr": None,
         "next": {"step": "unknown", "owner": None, "action": None}},
        {"id": "wt-bare", "name": "bare-row", "path": "/w/bare", "project": "", "goal": "broken"},
        # wrong types: a number where the agent's last message and the next action's text go
        {"id": "wt-num", "name": "num-row", "path": "/w/num", "project": "acme/w", "goal": "g",
         "agent": {"state": "waiting", "last": 5}, "next": {"step": "ci_green", "owner": "agent", "action": 1}},
        {"id": "wt-list", "name": "list-path", "path": ["/w/list"], "project": "acme/w", "goal": "g"},  # takes the live path
    ],
}))
_live_corrupt = {
    "wt-null": ({"worktreeId": "wt-null", "displayName": "null-msg", "path": "/w/null", "repo": "w", "branch": "b"},
                {"state": "waiting", "agentType": "claude", "lastAssistantMessage": None}),
    "wt-bare": ({"worktreeId": "wt-bare", "displayName": "bare-row", "path": "/w/bare", "repo": "w", "branch": ""}, {}),
    "wt-num": ({"worktreeId": "wt-num", "displayName": "num-row", "path": "/w/num", "repo": "w", "branch": "n"},
               {"state": "waiting", "agentType": "codex"}),
    "wt-list": ({"worktreeId": "wt-list", "displayName": "list-path", "path": "/w/list", "repo": "w", "branch": "l"}, {}),
}
with mock.patch.object(dash, "live_agents", return_value=_live_corrupt), \
     mock.patch.object(dash.blocking, "cached", return_value={}):
    _corrupt_snap = dash.snapshot()
    assert len(_corrupt_snap["pucks"]) == 4
    assert {p["name"] for p in _corrupt_snap["pucks"]} == {"null-msg", "bare-row", "num-row", "list-path"}
    assert next(p for p in _corrupt_snap["pucks"] if p["name"] == "list-path")["path"] == "/w/list"
# trees() also tolerates corrupt/partial last.json rows missing agent/next/name or non-dict entries
_lj3.write_text(json.dumps({
    "ts": 1,
    "rows": [
        "not-a-dict",
        {"id": "wt-list", "name": "list-path", "path": ["/w/list"]},  # a path that can't be a dict key is skipped
        {"id": "wt-num", "name": "num-row", "path": "/w/num", "project": "acme/w", "next": {"step": "ci_green", "owner": "agent", "action": 1}},
        {"id": "wt-bare", "path": "/w/bare", "project": "", "goal": "broken"},
        {"id": "wt-null", "name": "null-msg", "path": "/w/null", "project": "acme/w", "agent": None, "next": None},
        # a forge and an open MR but no project: progress() and moves() read r["project"]
        {"id": "wt-noproj", "name": "no-project", "path": "/w/noproj", "forge": "gitlab", "mr": {"state": "opened", "ref": "!7"}},
    ],
}))
_corrupt_ws = {
    "worktrees": [
        {"worktreeId": "wt-bare", "displayName": "bare-row", "path": "/w/bare", "repo": "w", "branch": "main", "agents": []},
        {"worktreeId": "wt-null", "displayName": "null-msg", "path": "/w/null", "repo": "w", "branch": "feat", "agents": [{"state": "waiting"}]},
        {"worktreeId": "wt-noproj", "displayName": "no-project", "path": "/w/noproj", "repo": "w", "branch": "fix", "agents": []},
        {"worktreeId": "wt-num", "displayName": "num-row", "path": "/w/num", "repo": "w", "branch": "n", "agents": []},
    ]
}
with mock.patch.object(dash, "ADES", ("orca",)), \
     mock.patch.object(dash.greenlight, "orca_cli", return_value=_corrupt_ws), \
     mock.patch.object(dash.blocking, "cached", return_value={}):
    _corrupt_trees = {t["name"]: t for t in dash.trees()}
    assert set(_corrupt_trees) == {"bare-row", "null-msg", "no-project", "num-row"}
    assert _corrupt_trees["no-project"]["puck"] == "no-project" and _corrupt_trees["no-project"]["steps"], _corrupt_trees["no-project"]
    assert _corrupt_trees["bare-row"]["puck"] == "bare-row" and _corrupt_trees["bare-row"]["step"] == "freeform"
    assert _corrupt_trees["null-msg"]["state"] == "needs" and _corrupt_trees["null-msg"]["owner"] == "parked"
_lj3.write_text(_prev_lj3) if _prev_lj3 is not None else _lj3.unlink()
print("corrupt snapshot & trees ok")

# CLI `dash.py page` catches backend failures via oops() and emits JSON error instead of crashing
_page_out = _io.StringIO()
with mock.patch.object(sys, "argv", ["dash.py", "page", "fix-auth"]), \
     mock.patch.object(dash, "page", side_effect=RuntimeError("orca down")), \
     mock.patch.object(dash, "oops") as _page_oops, \
     mock.patch.object(sys, "stdout", _page_out):
    dash.main()
assert json.loads(_page_out.getvalue().strip()) == {"name": "fix-auth", "error": "orca down"} and _page_oops.call_count == 1
print("page cli resilience ok")

# `dash.py turn <mc-session>`: a Basement pane's screen through greenlight's own agent_state (the page's "Finished" badge)
import ade_basement  # noqa: E402
_seen = []
_screens = {"=mc-ask:": "⏺ I can fix it two ways.\nShould I keep the old flag?\n\n❯ \n? for shortcuts\n",
            "=mc-done:": "⏺ Pushed and opened the MR.\n\n❯ \n? for shortcuts\n",
            "=mc-limit:": "⏺ Working on it\nYou've hit your usage limit · resets 5pm\n\n❯ \n"}
_cap = lambda *a: _seen.append(a) or _screens[a[-1]]
import decide  # noqa: E402
with mock.patch.object(ade_basement, "tmux", side_effect=_cap), \
     mock.patch.object(decide, "choice", return_value={"provider": "local", "confidence": 0, "choice": "", "probs": {}}):  # the patterns, not a model
    assert dash.turn("mc-ask")["state"] == "waiting_for_user", "a question asked in prose isn't finished"
    assert dash.turn("mc-done")["state"] == "finished"
    assert dash.turn("mc-limit")["state"] == "rate_limited"
assert _seen[0] == ("capture-pane", "-p", "-t", "=mc-ask:"), _seen[0]
for _argv, _want in ((["dash.py", "turn", "mc-done"], {"session": "mc-done", "state": "finished"}),
                     (["dash.py", "turn", "mc-x"], {"session": "mc-x", "error": "tmux down"})):
    _turn_out = _io.StringIO()
    with mock.patch.object(sys, "argv", _argv), mock.patch.object(sys, "stdout", _turn_out), mock.patch.object(dash, "oops"), \
         mock.patch.object(ade_basement, "tmux", side_effect=_cap if _argv[2] == "mc-done" else RuntimeError("tmux down")):
        dash.main()
    _got = json.loads(_turn_out.getvalue())
    assert {k: _got[k] for k in _want} == _want, _got
with mock.patch.object(sys, "argv", ["dash.py", "turn", "bad;session"]), mock.patch.object(sys, "stdout", _io.StringIO()):
    try:
        dash.main()
        raise AssertionError("a session that isn't mc-… must be refused")
    except SystemExit:
        pass
print("turn ok")

# `dash.py trees` includes live Basement tmux agent pane states (`panes`), tolerating tmux failures
with mock.patch.object(ade_basement, "pane_states", return_value={"mc-a": {"state": "done", "title": "✳ Done"}}):
    assert dash.basement_panes() == {"mc-a": {"state": "done", "title": "✳ Done"}}
    _trees_out = _io.StringIO()
    with mock.patch.object(sys, "argv", ["dash.py", "trees"]), mock.patch.object(sys, "stdout", _trees_out), \
         mock.patch.object(dash, "trees", return_value=[]):
        dash.main()
    assert json.loads(_trees_out.getvalue()) == {"trees": [], "panes": {"mc-a": {"state": "done", "title": "✳ Done"}}}
with mock.patch.object(ade_basement, "pane_states", side_effect=OSError("no tmux")):
    assert dash.basement_panes() == {}
print("basement panes ok")
