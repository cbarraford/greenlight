"""Tests for scripts/decisions.py: decision records (decisions.jsonl), chat/ping settlement, hooks, and chat.turn integration."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import chat
import dash
import decisions
import greenlight

OPTS = "\nOptions: merge | hold"


def _row(name, step, owner, ref, role="author"):
    return {
        "id": name + "-id",
        "name": name,
        "project": "acme/widgets",
        "mr": ref and {"ref": ref, "role": role},
        "next": {"step": step, "owner": owner, "action": "x"},
    }


@pytest.fixture()
def decisions_env(gl_home):
    (greenlight.STATE / "last.json").write_text(
        json.dumps(
            {
                "rows": [
                    _row("fix-001", "merge", "me", "!101"),
                    _row("fix-002", "merge", "me", "!102"),
                    _row("mr-7", "review", "me", "!7", "review"),
                    _row("auto-run-1", "automation", "me", None),
                    _row("feat-9", "ready", "me", "!9"),
                ]
            }
        )
    )
    return gl_home


def test_split_options():
    assert decisions.split_options("a?\n`Options: x | y |`") == ("a?", ["x", "y"])
    assert decisions.split_options("no options here") == ("no options here", [])


def test_ping_inbox_answer_and_inferred_kinds(decisions_env):
    # a ping about one workspace: the question, the options, and that workspace's situation as of the last scan
    greenlight.ping("fix-001: mergeable, but the goal says hold. Merge !101?" + OPTS, "fix-001", kind="Merge held")
    [r] = decisions.load()
    assert (r["via"], r["key"], r["kind"], r["options"]) == ("ping", "fix-001", "merge-held", ["merge", "hold"]), r
    assert r["question"] == "fix-001: mergeable, but the goal says hold. Merge !101?"
    assert {f: r[f] for f in decisions.FIELDS} == {
        "workspace": "fix-001",
        "worktree_id": "fix-001-id",
        "repo": "acme/widgets",
        "forge": None,
        "branch": None,
        "role": "author",
        "step": "merge",
        "owner": "me",
        "mr": "!101",
        "goal": None,
        "sha": None,
        "target_branch": None,
    }, r
    assert r["answer"] is None and r["closed"] is None and "workspaces" not in r

    # the answer the app sends back (an inbox chip, the reply field, a banner reply) lands in that same record
    assert decisions.heard('(Replying to your question: "fix-001: mergeable, but the goal says hold. Merge !101?")\nHold') == decisions.load()[0]
    [r] = decisions.load()
    assert (r["answer"], r["choice"], r["answer_via"]) == ("Hold", 1, "inbox") and r["answered"] >= r["ts"], r

    # the kind, when it isn't given: the lifecycle step the workspace is on, else an @key's name
    greenlight.ping("mr-7: new review request\nOptions: agent review | I'll review | skip", "mr-7")
    greenlight.ping("3 runs finished. Delete them?\nOptions: delete | keep", "@ci-health-runs-20261025")
    greenlight.ping("acme/gadgets has no repo\nOptions: add the repo | ignore its MRs", "@no-repo:acme/gadgets")
    recs = decisions.load()
    assert [r["kind"] for r in recs[1:]] == ["review", "ci-health-runs", "no-repo"], recs
    assert recs[1]["role"] == "review" and recs[2]["workspace"] is None

    # typed, not picked: no choice. The question is quoted whole, so one holding quotes and newlines still matches
    q = 'feat-9: the agent asks "squash or keep the 3 commits?"\nIt is green otherwise.'
    greenlight.ping(q + "\nOptions: squash | keep", "feat-9", kind="agent-stuck")
    decisions.heard(f'(Replying to your question: "{q}")\n  keep them, but\nreword the first  ')
    [r] = [rec for rec in decisions.load() if rec["question"] == q]
    assert (r["answer"], r["choice"], r["step"]) == ("keep them, but\nreword the first", None, "ready"), r

    # a newer ping on the same key replaces the open one (as in the inbox); the answer goes to the newer
    greenlight.ping("mr-7: author replied on your threads\nOptions: agent review | I'll review", "mr-7", kind="review-request")
    old, new = [r for r in decisions.load() if r["key"] == "mr-7"]
    assert old["closed"] == "superseded" and old["answer"] is None and new["closed"] is None
    decisions.heard('(Replying to your question: "mr-7: author replied on your threads")\nagent review')
    old, new = [r for r in decisions.load() if r["key"] == "mr-7"]
    assert old["answer"] is None and (new["answer"], new["choice"]) == ("agent review", 0)

    # resolved (the question no longer applies): closed with no answer. Answered late anyway: the answer is kept
    greenlight.resolve("@ci-health-runs-20261025", "runs deleted by hand")
    [r] = [r for r in decisions.load() if r["key"] == "@ci-health-runs-20261025"]
    assert r["closed"] == "resolved: runs deleted by hand" and r["answer"] is None
    decisions.heard('(Replying to your question: "3 runs finished. Delete them?")\ndelete')
    [r] = [r for r in decisions.load() if r["key"] == "@ci-health-runs-20261025"]
    assert (r["answer"], r["choice"], r["closed"]) == ("delete", 0, None), r


def test_multi_workspace_ping_notification_flap_and_backfill(decisions_env):
    # one ping about several workspaces: each one's situation, and what they share
    greenlight.ping("2 held merges are mergeable. Merge them?" + OPTS, "@held-merges", ["fix-001", "fix-002"], kind="merge-held")
    r = decisions.load()[-1]
    assert [w["workspace"] for w in r["workspaces"]] == ["fix-001", "fix-002"]
    assert [w["mr"] for w in r["workspaces"]] == ["!101", "!102"]
    assert (r["workspace"], r["mr"], r["repo"], r["step"], r["owner"]) == (None, None, "acme/widgets", "merge", "me"), r

    # a banner reply to a notification that isn't in the inbox
    n = len(decisions.load())
    decisions.heard('(Replying to your notification: "auto-run-1: finished, nothing new. Delete it?")\ndelete')
    r = decisions.load()[-1]
    assert len(decisions.load()) == n + 1
    assert (r["answer"], r["answer_via"], r["workspace"], r["step"]) == ("delete", "notification", "auto-run-1", "automation"), r

    # asked again while still open: not recorded twice. A flapping @key (pinged within a day): not sent, not recorded
    n = len(decisions.load())
    greenlight.ping("fyi: nothing to do")
    greenlight.ping("fyi: nothing to do")
    greenlight.ping("2 held merges are mergeable. Merge them?" + OPTS, "@held-merges", ["fix-001", "fix-002"])
    assert len(decisions.load()) == n + 1 and decisions.load()[-1]["options"] == []

    # A failed record write must not expose a question with no durable decision.
    with (
        mock.patch.object(decisions, "book", side_effect=OSError("disk full")),
        mock.patch.object(sys, "stderr"),
        pytest.raises(OSError, match="disk full"),
    ):
        greenlight.ping("fix-002: still sent" + OPTS, "fix-002")
    assert not json.loads((greenlight.STATE / "outbox.jsonl").read_text().splitlines()[-1])["text"].startswith("fix-002: still sent")
    assert len(decisions.load()) == n + 1

    # A question asked before Greenlight generates an MR goal keeps its later answer in the handoff.
    rid = decisions.asked("Review this?\nOptions: yes | no", key="fix-002", ws=["fix-002"])
    with decisions.book() as records:
        next(r for r in records if r["id"] == rid)["goal"] = ""
    decisions.backfilled_goal("fix-002-id", "!102", "Review #102: generated")
    assert next(r for r in decisions.load() if r["id"] == rid)["goal"] == "Review #102: generated"

    other_rid = decisions.asked("Deploy this?\nOptions: yes | no", key="other-repo", ws=["fix-002"])
    with decisions.book() as records:
        target = next(r for r in records if r["id"] == other_rid)
        target.update(goal="", repo="org/a", forge="github")
    decisions.backfilled_goal("fix-002-id", "!102", "Review !102: B", "org/b", "github")
    assert next(r for r in decisions.load() if r["id"] == other_rid)["goal"] == ""
    decisions.backfilled_goal("fix-002-id", "!102", "Review !102: A", "org/a", "github")
    assert next(r for r in decisions.load() if r["id"] == other_rid)["goal"] == "Review !102: A"


def test_menubar_chat_questions_and_multi_window_isolation(decisions_env):
    # the menubar chat: a reply ending in an Options line is a question; the next message answers it
    assert decisions.heard("what's the status of fix-001?") is None  # plain chat with nothing open: not an answer
    decisions.said("fix-002 is green and approved. Merge it?" + OPTS)
    assert decisions.said("All 3 dispatched.") is None
    r = decisions.load()[-1]
    assert (r["via"], r["kind"], r["workspace"], r["options"]) == ("chat", "merge", "fix-002", ["merge", "hold"]), r

    decisions.said("Sorry, I meant fix-001. Merge it?" + OPTS)  # asked again in other words: the first is superseded
    decisions.heard("merge")
    a, b = decisions.load()[-2:]
    assert a["closed"] == "superseded"
    assert (b["answer"], b["choice"], b["answer_via"], b["workspace"]) == ("merge", 0, "chat", "fix-001")
    assert decisions.heard("thanks") is None  # nothing open any more

    decisions.said("Park it?\nOptions: park | keep")
    decisions.forget_chat()  # Clear in the app
    assert decisions.load()[-1]["closed"] == "resolved: chat cleared" and decisions.heard("park") is None

    # a second L1 window's Chief (chat="chat-1"): its question and the main chat's never settle, supersede or clear each other
    main, other = decisions.said("Merge fix-001?" + OPTS), decisions.said("Merge fix-002?" + OPTS, chat="chat-1")
    assert decisions.heard("hold", chat="chat-1")["id"] == other and decisions.said("Merge fix-001 now?" + OPTS, chat="chat-1")
    decisions.forget_chat(chat="chat-1")
    assert [r for r in decisions.load() if r["id"] == main][0]["closed"] is None and decisions.heard("merge")["id"] == main

    # Identical wording in two chats: the inbox's dismiss closes only the question from that window.
    same = "Which workspace next?\nOptions: first | second"
    main, other = decisions.said(same), decisions.said(same, chat="chat-1")
    assert decisions.dismiss(same, ping=False, chat=None)["id"] == main
    assert next(r for r in decisions.load() if r["id"] == other)["closed"] is None
    assert decisions.dismiss(same, ping=False, chat="chat-1")["id"] == other

    # Chief's trailing dashboard highlight tag is hidden in the app and cannot hide the Options line from the record.
    tagged = decisions.said("Work on fix-002 next?\nOptions: fix-002 | later\n[hexes: fix-002]", kind="check-in")
    assert tagged and decisions.load()[-1]["kind"] == "check-in" and decisions.load()[-1]["options"] == ["fix-002", "later"]

    # Reusing an open ping updates the proposal to the one actually acted on.
    old = {"choice": "later", "index": 1, "prob": 0.7, "tier": 2}
    new = {"choice": "fix-002", "index": 0, "prob": 0.96, "tier": 1}
    rid = decisions.asked("Work next?\nOptions: fix-002 | later", key="fix-002", kind="agent-stuck", proposal=old)
    assert decisions.asked("Work next?\nOptions: fix-002 | later", key="fix-002", kind="agent-stuck", proposal=new) == rid
    assert next(r for r in decisions.load() if r["id"] == rid)["proposal"] == new
    changed = decisions.asked("Work next?\nOptions: later | fix-002", key="fix-002", kind="agent-stuck", proposal=new | {"index": 1})
    assert changed != rid and next(r for r in decisions.load() if r["id"] == rid)["closed"] == "superseded"
    assert next(r for r in decisions.load() if r["id"] == changed)["options"] == ["later", "fix-002"]

    plain = decisions.asked("Unkeyed choice?\nOptions: old | later")
    plain_changed = decisions.asked("Unkeyed choice?\nOptions: new | later")
    assert plain_changed != plain and next(r for r in decisions.load() if r["id"] == plain)["closed"] == "superseded"


def test_ask_user_question_hook_and_cli(decisions_env):
    # Seed a superseded record and an open record so CLI `list` and `list --open --json` verify both states
    decisions.asked("Initial?\nOptions: a | b", key="seed-k")
    decisions.asked("Replacement?\nOptions: a | b", key="seed-k")

    # AskUserQuestion (an on-demand run): the PostToolUse hook gets the questions and the answers at once
    ask = {
        "questions": [
            {
                "question": "feat-9 is green but its goal says stay draft. Take it out of draft?",
                "header": "Keep draft",
                "multiSelect": False,
                "options": [
                    {"label": "Keep draft", "description": "leave it"},
                    {"label": "Mark ready", "description": "greenlight marks it ready"},
                ],
            },
            {
                "question": "Which of these finished runs can I delete?",
                "header": "Delete runs",
                "multiSelect": True,
                "options": [
                    {"label": "auto-run-1", "description": "nothing new"},
                    {"label": "fix-002", "description": "merged"},
                    {"label": "mr-7", "description": "approved"},
                ],
            },
        ]
    }
    n = len(decisions.load())
    out = decisions.hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": ask,
            "tool_response": ask
            | {
                "answers": {
                    ask["questions"][0]["question"]: "Mark ready",
                    ask["questions"][1]["question"]: "auto-run-1, mr-7",
                },
                "annotations": {},
            },
        }
    )
    a, b = decisions.load()[n:]
    assert len(out) == 2 and a["id"] != b["id"]
    assert (a["via"], a["kind"], a["workspace"], a["step"], a["options"], a["answer"], a["choice"], a["answer_via"]) == (
        "ask",
        "keep-draft",
        "feat-9",
        "ready",
        ["Keep draft", "Mark ready"],
        "Mark ready",
        1,
        "ask",
    ), a
    assert (b["kind"], b["answer"], b["choice"]) == ("delete-runs", "auto-run-1, mr-7", [0, 2])
    assert len(b["workspaces"]) == 3 and b["workspace"] is None, b

    # the answers as the tool's plain text, and a typed ("Other") answer
    text = 'Your questions have been answered: "Goal for fix-002?"="Merge !102, then delete". You can now continue with these answers in mind.'
    [c] = decisions.hook(
        {
            "tool_name": "AskUserQuestion",
            "tool_response": text,
            "tool_input": {
                "questions": [{"question": "Goal for fix-002?", "header": "Goal", "options": [{"label": "Merge !102"}, {"label": "Park it"}]}]
            },
        }
    )
    assert (c["answer"], c["choice"], c["workspace"], c["kind"]) == ("Merge !102, then delete", None, "fix-002", "goal"), c
    assert decisions.hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == []

    # the hook command itself: silent and exit 0 whatever arrives, so it can never get in a session's way
    env = os.environ | {"GREENLIGHT_HOME": str(greenlight.STATE)}
    script = str(Path(decisions.__file__))
    n = len(decisions.load())
    for stdin in ("not json", "{}", json.dumps({"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"nope": 1}]}})):
        p = subprocess.run([sys.executable, script, "hook"], input=stdin, capture_output=True, text=True, env=env)
        assert (p.returncode, p.stdout, p.stderr) == (0, "", ""), p

    p = subprocess.run(
        [sys.executable, script, "hook"],
        capture_output=True,
        text=True,
        env=env,
        input=json.dumps(
            {
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [{"question": "Delete auto-run-1?", "header": "Delete", "options": [{"label": "yes"}, {"label": "no"}]}]
                },
                "tool_response": {"answers": {"Delete auto-run-1?": "yes"}},
            }
        ),
    )
    assert p.returncode == 0 and len(decisions.load()) == n + 1 and decisions.load()[-1]["answer"] == "yes"

    listing = subprocess.run([sys.executable, script, "list"], capture_output=True, text=True, env=env).stdout
    assert "Delete auto-run-1? → yes" in listing and "(superseded)" in listing, listing
    opened = json.loads(subprocess.run([sys.executable, script, "list", "--open", "--json"], capture_output=True, text=True, env=env).stdout)
    assert opened and all(r["answer"] is None and not r["closed"] for r in opened)


def test_end_to_end_chat_turn_partial_line_and_head_change(decisions_env, tmp_path, monkeypatch):
    # end to end through chat.py: a ping, then the app's reply message through a real turn (a fake `claude -p`)
    fake_script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "msg = sys.stdin.read()\n"
        'reply = "fix-002 is next. Merge it too?\\nOptions: merge | hold" if "ASK" in msg else "Done."\n'
        'print(json.dumps({"type": "result", "result": reply, "is_error": False, "duration_ms": 1, "session_id": "s1"}))\n'
    )
    chat_dir = tmp_path / "chat-run"
    chat_dir.mkdir()
    fake = chat_dir / "claude"
    fake.write_text(fake_script)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(chat.harness, "binary", lambda n: str(fake))
    monkeypatch.setattr(chat, "DIR", str(chat_dir))
    monkeypatch.setattr(chat, "PIDFILE", str(chat_dir / "pid"))
    monkeypatch.setattr(chat, "emit", lambda **e: None)

    allowed = dash.BoardAllow(
        {name: ("gitlab", "acme/widgets", ref, "", "", "", name + "-id", "") for name, ref in (("fix-001", "!101"), ("fix-002", "!102"))}
    )
    monkeypatch.setattr(dash, "board", lambda *a, **kw: ("- fix-001\n- fix-002", allowed))

    greenlight.ping("fix-001: approved and green, held by its goal. Merge !101?" + OPTS, "fix-001", kind="merge-held")
    chat.turn(
        '(Replying to your question: "fix-001: approved and green, held by its goal. Merge !101?")\nmerge ASK',
        "claude-haiku-5-5",
        None,
    )
    one, two = decisions.load()
    assert (one["via"], one["kind"], one["workspace"], one["repo"], one["role"], one["step"], one["owner"], one["options"], one["answer"], one["choice"]) == (
        "ping",
        "merge-held",
        "fix-001",
        "acme/widgets",
        "author",
        "merge",
        "me",
        ["merge", "hold"],
        "merge ASK",
        None,
    ), one
    assert (two["via"], two["workspace"], two["options"], two["answer"]) == ("chat", "fix-002", ["merge", "hold"], None), two

    chat.turn("hold", "claude-haiku-5-5", None)
    one, two = decisions.load()
    assert (two["answer"], two["choice"], two["answer_via"]) == ("hold", 1, "chat") and one["answer"] == "merge ASK"

    with mock.patch.object(sys, "argv", ["chat.py", "new"]):  # Clear: nothing open, nothing lost
        chat.main()
    assert [r["closed"] for r in decisions.load()] == [None, None]

    with (greenlight.STATE / "decisions.jsonl").open("a") as f:
        f.write("{partial record\n")
    assert len(decisions.load()) == 2
    decisions.asked("Can continue?" + OPTS, key="after-partial")
    assert len(decisions.load()) == 3  # an interrupted final line does not block a new answer

    head_row = _row("head-bound", "merge", "me", "!42")
    head_row["mr"]["sha"] = "head-a"
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": [head_row]}))
    old_id = decisions.asked("Merge this?" + OPTS, key="head-bound", ws=["head-bound"])
    head_row["mr"]["sha"] = "head-b"
    (greenlight.STATE / "last.json").write_text(json.dumps({"rows": [head_row]}))
    new_id = decisions.asked("Merge this?" + OPTS, key="head-bound", ws=["head-bound"])
    assert old_id != new_id and next(r for r in decisions.load() if r["id"] == old_id)["closed"] == "superseded"


def test_decisions_book_enforces_private_permissions(gl_home, monkeypatch):
    import stat

    priv_state = gl_home / "priv-dec"
    monkeypatch.setattr(decisions, "STATE", priv_state)
    decisions.asked("Keep draft?\nOptions: keep | ready", key="ws-1")
    assert stat.S_IMODE(priv_state.stat().st_mode) == 0o700
    assert stat.S_IMODE((priv_state / "decisions.jsonl").stat().st_mode) == 0o600
