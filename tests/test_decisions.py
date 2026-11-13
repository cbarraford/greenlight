"""python3 test_decisions.py: the decision record (decisions.jsonl): a question, its situation, and the answer in the same record."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import json, stat, subprocess, sys
from pathlib import Path
from unittest import mock
import chat, decisions, greenlight

recs = decisions.load
by_q = lambda q: [r for r in recs() if r["question"] == q]
O = "\nOptions: merge | hold"
row = lambda name, step, owner, ref, role="author": {
    "id": name + "-id", "name": name, "project": "acme/widgets", "mr": ref and {"ref": ref, "role": role}, "next": {"step": step, "owner": owner, "action": "x"}}
(greenlight.STATE / "last.json").write_text(json.dumps({"rows": [
    row("fix-001", "merge", "me", "!101"), row("fix-002", "merge", "me", "!102"), row("mr-7", "review", "me", "!7", "review"),
    row("auto-run-1", "automation", "me", None), row("feat-9", "ready", "me", "!9")]}))

assert decisions.split_options("a?\n`Options: x | y |`") == ("a?", ["x", "y"])
assert decisions.split_options("no options here") == ("no options here", [])

# a ping about one workspace: the question, the options, and that workspace's situation as of the last scan
greenlight.ping("fix-001: mergeable, but the goal says hold. Merge !101?" + O, "fix-001", kind="Merge held")
[r] = recs()
assert (r["via"], r["key"], r["kind"], r["options"]) == ("ping", "fix-001", "merge-held", ["merge", "hold"]), r
assert r["question"] == "fix-001: mergeable, but the goal says hold. Merge !101?"
assert {f: r[f] for f in decisions.FIELDS} == {"workspace": "fix-001", "repo": "acme/widgets", "role": "author", "step": "merge", "owner": "me", "mr": "!101"}, r
assert r["answer"] is None and r["closed"] is None and "workspaces" not in r

# the answer the app sends back (an inbox chip, the reply field, a banner reply) lands in that same record
assert decisions.heard('(Replying to your question: "fix-001: mergeable, but the goal says hold. Merge !101?")\nHold') == recs()[0]
[r] = recs()
assert (r["answer"], r["choice"], r["answer_via"]) == ("Hold", 1, "inbox") and r["answered"] >= r["ts"], r

# the kind, when it isn't given: the lifecycle step the workspace is on, else an @key's name
greenlight.ping("mr-7: new review request" + "\nOptions: agent review | I'll review | skip", "mr-7")
greenlight.ping("3 runs finished. Delete them?\nOptions: delete | keep", "@ci-health-runs-20261025")
greenlight.ping("acme/gadgets has no repo\nOptions: add the repo | ignore its MRs", "@no-repo:acme/gadgets")
assert [r["kind"] for r in recs()[1:]] == ["review", "ci-health-runs", "no-repo"], recs()
assert recs()[1]["role"] == "review" and recs()[2]["workspace"] is None

# typed, not picked: no choice. The question is quoted whole, so one holding quotes and newlines still matches
q = 'feat-9: the agent asks "squash or keep the 3 commits?"\nIt is green otherwise.'
greenlight.ping(q + "\nOptions: squash | keep", "feat-9", kind="agent-stuck")
decisions.heard(f'(Replying to your question: "{q}")\n  keep them, but\nreword the first  ')
[r] = by_q(q)
assert (r["answer"], r["choice"], r["step"]) == ("keep them, but\nreword the first", None, "ready"), r

# a newer ping on the same key replaces the open one (as in the inbox); the answer goes to the newer
greenlight.ping("mr-7: author replied on your threads" + "\nOptions: agent review | I'll review", "mr-7", kind="review-request")
old, new = [r for r in recs() if r["key"] == "mr-7"]
assert old["closed"] == "superseded" and old["answer"] is None and new["closed"] is None
decisions.heard('(Replying to your question: "mr-7: author replied on your threads")\nagent review')
old, new = [r for r in recs() if r["key"] == "mr-7"]
assert old["answer"] is None and (new["answer"], new["choice"]) == ("agent review", 0)

# resolved (the question no longer applies): closed with no answer. Answered late anyway: the answer is kept
greenlight.resolve("@ci-health-runs-20261025", "runs deleted by hand")
[r] = [r for r in recs() if r["key"] == "@ci-health-runs-20261025"]
assert r["closed"] == "resolved: runs deleted by hand" and r["answer"] is None
decisions.heard('(Replying to your question: "3 runs finished. Delete them?")\ndelete')
[r] = [r for r in recs() if r["key"] == "@ci-health-runs-20261025"]
assert (r["answer"], r["choice"], r["closed"]) == ("delete", 0, None), r

# one ping about several workspaces: each one's situation, and what they share
greenlight.ping("2 held merges are mergeable. Merge them?" + O, "@held-merges", ["fix-001", "fix-002"], kind="merge-held")
r = recs()[-1]
assert [w["workspace"] for w in r["workspaces"]] == ["fix-001", "fix-002"] and [w["mr"] for w in r["workspaces"]] == ["!101", "!102"]
assert (r["workspace"], r["mr"], r["repo"], r["step"], r["owner"]) == (None, None, "acme/widgets", "merge", "me"), r

# a banner reply to a notification that isn't in the inbox, and a reply to a ping sent before the record existed: both kept
n = len(recs())
decisions.heard('(Replying to your notification: "auto-run-1: finished, nothing new. Delete it?")\ndelete')
r = recs()[-1]
assert len(recs()) == n + 1 and (r["answer"], r["answer_via"], r["workspace"], r["step"]) == ("delete", "notification", "auto-run-1", "automation"), r

# asked again while still open: not recorded twice. A flapping @key (pinged within a day): not sent, not recorded
n = len(recs())
greenlight.ping("fyi: nothing to do")
greenlight.ping("fyi: nothing to do")
greenlight.ping("2 held merges are mergeable. Merge them?" + O, "@held-merges", ["fix-001", "fix-002"])
assert len(recs()) == n + 1 and recs()[-1]["options"] == []

# the record can fail (a full disk, a bad line): the ping still goes out
with mock.patch.object(decisions, "book", side_effect=OSError("disk full")), mock.patch.object(sys, "stderr"):
    greenlight.ping("fix-002: still sent" + O, "fix-002")
assert json.loads((greenlight.STATE / "outbox.jsonl").read_text().splitlines()[-1])["text"].startswith("fix-002: still sent")
assert len(recs()) == n + 1

# the menubar chat: a reply ending in an Options line is a question; the next message answers it (a chip or typed)
assert decisions.heard("what's the status of fix-001?") is None  # plain chat with nothing open: not an answer
decisions.said("fix-002 is green and approved. Merge it?" + O)
assert decisions.said("All 3 dispatched.") is None
r = recs()[-1]
assert (r["via"], r["kind"], r["workspace"], r["options"]) == ("chat", "merge", "fix-002", ["merge", "hold"]), r
decisions.said("Sorry, I meant fix-001. Merge it?" + O)  # asked again in other words: the first is superseded
decisions.heard("merge")
a, b = recs()[-2:]
assert a["closed"] == "superseded" and (b["answer"], b["choice"], b["answer_via"], b["workspace"]) == ("merge", 0, "chat", "fix-001")
assert decisions.heard("thanks") is None  # nothing open any more
decisions.said("Park it?\nOptions: park | keep")
decisions.forget_chat()  # Clear in the app
assert recs()[-1]["closed"] == "resolved: chat cleared" and decisions.heard("park") is None

# AskUserQuestion (an on-demand run): the PostToolUse hook gets the questions and the answers at once
ask = {"questions": [
    {"question": "feat-9 is green but its goal says stay draft. Take it out of draft?", "header": "Keep draft", "multiSelect": False,
     "options": [{"label": "Keep draft", "description": "leave it"}, {"label": "Mark ready", "description": "greenlight marks it ready"}]},
    {"question": "Which of these finished runs can I delete?", "header": "Delete runs", "multiSelect": True,
     "options": [{"label": "auto-run-1", "description": "nothing new"}, {"label": "fix-002", "description": "merged"}, {"label": "mr-7", "description": "approved"}]}]}
n = len(recs())
out = decisions.hook({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "tool_input": ask, "tool_response": ask | {
    "answers": {ask["questions"][0]["question"]: "Mark ready", ask["questions"][1]["question"]: "auto-run-1, mr-7"}, "annotations": {}}})
a, b = recs()[n:]
assert len(out) == 2 and a["id"] != b["id"]
assert (a["via"], a["kind"], a["workspace"], a["step"], a["options"], a["answer"], a["choice"], a["answer_via"]) == \
    ("ask", "keep-draft", "feat-9", "ready", ["Keep draft", "Mark ready"], "Mark ready", 1, "ask"), a
assert (b["kind"], b["answer"], b["choice"]) == ("delete-runs", "auto-run-1, mr-7", [0, 2]) and len(b["workspaces"]) == 3 and b["workspace"] is None, b
# the answers as the tool's plain text, and a typed ("Other") answer
text = 'Your questions have been answered: "Goal for fix-002?"="Merge !102, then delete". You can now continue with these answers in mind.'
[c] = decisions.hook({"tool_name": "AskUserQuestion", "tool_response": text, "tool_input": {"questions": [
    {"question": "Goal for fix-002?", "header": "Goal", "options": [{"label": "Merge !102"}, {"label": "Park it"}]}]}})
assert (c["answer"], c["choice"], c["workspace"], c["kind"]) == ("Merge !102, then delete", None, "fix-002", "goal"), c
assert decisions.hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == []
# the hook command itself: silent and exit 0 whatever arrives, so it can never get in a session's way
env = os.environ | {"GREENLIGHT_HOME": str(greenlight.STATE)}
script = str(Path(decisions.__file__))
n = len(recs())
for stdin in ("not json", "{}", json.dumps({"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"nope": 1}]}})):
    p = subprocess.run([sys.executable, script, "hook"], input=stdin, capture_output=True, text=True, env=env)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", ""), p
p = subprocess.run([sys.executable, script, "hook"], capture_output=True, text=True, env=env, input=json.dumps(
    {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Delete auto-run-1?", "header": "Delete", "options": [{"label": "yes"}, {"label": "no"}]}]},
     "tool_response": {"answers": {"Delete auto-run-1?": "yes"}}}))
assert p.returncode == 0 and len(recs()) == n + 1 and recs()[-1]["answer"] == "yes"
listing = subprocess.run([sys.executable, script, "list"], capture_output=True, text=True, env=env).stdout
assert "Delete auto-run-1? → yes" in listing and "(superseded)" in listing, listing
opened = json.loads(subprocess.run([sys.executable, script, "list", "--open", "--json"], capture_output=True, text=True, env=env).stdout)
assert opened and all(r["answer"] is None and not r["closed"] for r in opened)

# end to end through chat.py: a ping, then the app's reply message through a real turn (a fake `claude -p`)
FAKE = """#!/usr/bin/env python3
import json, sys
msg = sys.stdin.read()
reply = "fix-002 is next. Merge it too?\\nOptions: merge | hold" if "ASK" in msg else "Done."
print(json.dumps({"type": "result", "result": reply, "is_error": False, "duration_ms": 1, "session_id": "s1"}))
"""
tmp = Path(tempfile.mkdtemp())
fake = tmp / "claude"
fake.write_text(FAKE)
fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
chat.harness.binary = lambda n: str(fake)
chat.DIR, chat.PIDFILE, chat.emit = str(tmp), str(tmp / "pid"), lambda **e: None
(greenlight.STATE / "decisions.jsonl").unlink()
greenlight.ping("fix-001: approved and green, held by its goal. Merge !101?" + O, "fix-001", kind="merge-held")
chat.turn('(Replying to your question: "fix-001: approved and green, held by its goal. Merge !101?")\nmerge ASK', "claude-haiku-5-5", None)
one, two = recs()  # the ping, complete; and the question the reply went on to ask
assert (one["via"], one["kind"], one["workspace"], one["repo"], one["role"], one["step"], one["owner"], one["options"], one["answer"], one["choice"]) == \
    ("ping", "merge-held", "fix-001", "acme/widgets", "author", "merge", "me", ["merge", "hold"], "merge ASK", None), one
assert (two["via"], two["workspace"], two["options"], two["answer"]) == ("chat", "fix-002", ["merge", "hold"], None), two
chat.turn("hold", "claude-haiku-5-5", None)
one, two = recs()
assert (two["answer"], two["choice"], two["answer_via"]) == ("hold", 1, "chat") and one["answer"] == "merge ASK"
with mock.patch.object(sys, "argv", ["chat.py", "new"]):  # Clear: nothing open, nothing lost
    chat.main()
assert [r["closed"] for r in recs()] == [None, None]
print("test_decisions ok")
