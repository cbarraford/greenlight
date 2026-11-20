#!/usr/bin/env python3
"""Tests for chat.py: python3 -m pytest test_chat.py"""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import json, os, stat, subprocess, sys, time
import chat
from chat import Norm, step_title


def test_norm():
    n = Norm()
    ev = lambda d: [e["e"] for e in n.claude(d)]
    assert ev({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}}}}) == ["usage"]
    assert n.context == 152
    out = n.claude({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls", "description": "List"}}]}})
    assert out == [{"e": "step", "id": "t1", "title": "List", "detail": "ls"}], out
    out = n.claude({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "12", "is_error": False}]}})
    assert out == [{"e": "step_done", "id": "t1", "ok": True, "out": "12"}], out
    assert ev({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}}) == ["text_reset"]
    assert "text" in ev({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}})
    assert ev({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"post_tokens": 7810}}) == ["usage"] and n.context == 7810
    done = n.claude({"type": "result", "result": "12 entries", "is_error": False, "duration_ms": 3056, "modelUsage": {"m": {"contextWindow": 1000000}}})[0]
    assert done["e"] == "done" and done["window"] == 1000000 and done["text"] == "12 entries", done

    c = Norm()
    assert c.codex({"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "/usr/bin/zsh -lc 'ls ~'"}})[0]["detail"] == "ls ~"
    assert c.codex({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "exit_code": 0, "aggregated_output": "x"}})[0]["ok"]
    c.codex({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "done"}})
    assert c.codex({"type": "error", "message": "Reconnecting... 2/5 (401)"})[0]["e"] == "status"
    assert c.codex({"type": "turn.completed", "usage": {"input_tokens": 30640}})[0]["text"] == "done"

    assert step_title("Read", {"file_path": "/a/b/SKILL.md"}) == ("Read SKILL.md", "/a/b/SKILL.md")
    assert step_title("mcp__x__do_thing", {"q": "z"}) == ("do thing", "z")


# A fake `claude -p`: logs each prompt it gets, and answers a first (un-retried) message the way the real
# bug did: a task-notification queued into the turn, then "No response requested."
FAKE = """#!/usr/bin/env python3
import json, sys
msg = sys.stdin.read()
open(sys.argv[0] + ".log", "a").write(json.dumps(msg) + "\\n")
def out(**d):
    d["session_id"] = "s1"
    print(json.dumps(d))
ignored = "BUG" in msg and ("previous reply ignored" not in msg or "STUBBORN" in msg)
if ignored:
    out(type="user", message={"role": "user", "content": "<task-notification> status: stopped. NOT user input. No human input has been received."})
reply = "No response requested." if ignored else "Deleted it."
out(type="assistant", message={"content": [{"type": "text", "text": reply}]})
out(type="result", result=reply, is_error=False, duration_ms=10)
"""


def turn(tmp_path, monkeypatch, msg):
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    events = []
    monkeypatch.setattr(chat.harness, "binary", lambda n: str(fake))
    monkeypatch.setattr(chat, "DIR", str(tmp_path))
    monkeypatch.setattr(chat, "PIDFILE", str(tmp_path / "pid"))
    monkeypatch.setattr(chat, "emit", lambda **e: events.append(e))
    chat.turn(msg, "claude-haiku-5-5", None)
    prompts = [json.loads(l) for l in open(str(fake) + ".log")]
    return events, prompts


def test_notification_turn_retries_once(tmp_path, monkeypatch):
    events, prompts = turn(tmp_path, monkeypatch, "(Replying to your question: delete it?) delete BUG")
    assert len(prompts) == 2, prompts
    assert prompts[0] == chat.WRAP + "(Replying to your question: delete it?) delete BUG"
    assert prompts[1] == chat.RETRY_PREFIX + prompts[0]
    done = [e for e in events if e["e"] == "done"]
    assert len(done) == 1 and done[0]["text"] == "Deleted it.", events
    assert not any(e.get("text") == "No response requested." for e in events)


def test_retry_never_loops(tmp_path, monkeypatch):
    events, prompts = turn(tmp_path, monkeypatch, "round 8 BUG STUBBORN")
    assert len(prompts) == 2, prompts
    assert [e["text"] for e in events if e["e"] == "done"] == ["No response requested."]


def test_normal_turn_no_retry(tmp_path, monkeypatch):
    events, prompts = turn(tmp_path, monkeypatch, "status update")
    assert prompts == [chat.WRAP + "status update"]
    assert [e["text"] for e in events if e["e"] == "done"] == ["Deleted it."]


def test_ignored_pattern():
    for s in ("", "  ", "No response requested.", "no response requested"):
        assert chat.IGNORED.match(s), s
    assert not chat.IGNORED.match("No response requested. But here's the status: …")


def test_background_shells():
    n = Norm()
    n.claude({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "sleep 15", "run_in_background": True}}]}})
    out = n.claude({"type": "system", "subtype": "task_started", "task_id": "b1", "tool_use_id": "tu1", "description": "Sleep 15", "is_backgrounded": True})
    assert out[0]["e"] == "bg" and out[0]["shells"][0]["cmd"] == "sleep 15" and out[0]["shells"][0]["title"] == "Sleep 15", out
    assert n.claude({"type": "system", "subtype": "background_tasks_changed", "tasks": []}) == [{"e": "bg", "shells": []}]


def test_agent_title():
    assert chat.agent_title({"prompt": '<pasted_content id="x">\n/goal Fix the lint job\nmore</pasted_content>'}) == "Fix the lint job"
    assert chat.agent_title({"taskTitle": "Resolve threads", "prompt": "x"}) == "Resolve threads"


def test_stop_shell(tmp_path, monkeypatch):
    # a stand-in claude process with one background shell under it
    parent = subprocess.Popen([sys.executable, "-c", "import subprocess; subprocess.run(['sh', '-c', 'sleep 301; echo bgmarker'])"])
    time.sleep(0.5)
    monkeypatch.setattr(chat, "PIDFILE", str(tmp_path / "pid"))
    monkeypatch.setattr(chat, "BGFILE", str(tmp_path / "bg.json"))
    (tmp_path / "pid").write_text(str(parent.pid))
    (tmp_path / "bg.json").write_text(json.dumps([{"id": "b1", "title": "t", "cmd": "sleep 301; echo bgmarker", "started": 0}]))
    try:
        assert chat.stop_shell("b1")
        parent.wait(timeout=5)  # its only child died, so it exits
        assert not chat.stop_shell("nope")
    finally:
        parent.kill()


if __name__ == "__main__":
    test_norm()
    print("ok")


def test_thinking_phrase_per_stretch():
    n = Norm()
    think = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}}}
    said = lambda d: [e["text"] for e in n.phrase(n.claude(d)) if e["e"] == "status"]
    first = said(think)
    assert len(first) == 1 and first[0] in chat.THINKING
    assert all(said(think) == first for _ in range(20))  # held while the stretch lasts, not cycled
    n.phrase(n.claude({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {}}}}))  # usage alone doesn't end it
    assert said(think) == first
    n.phrase(n.claude({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}]}}))
    second = said(think)  # a new stretch after a step: a fresh, different phrase
    assert second[0] in chat.THINKING and second != first
    n.phrase([{"e": "text", "delta": "x"}])
    assert said(think) != second
    assert n.phrase([{"e": "status", "text": "Compacting"}])[0]["text"] == "Compacting"  # other statuses pass through
    c = Norm()  # codex's reasoning item, and a fresh turn's Norm, pick from the same pool
    assert c.phrase(c.codex({"type": "item.started", "item": {"id": "r", "type": "reasoning"}}))[0]["text"] in chat.THINKING


def test_thinking_phrases_fit():
    assert len(set(chat.THINKING)) == len(chat.THINKING) >= 40
    assert all(p.endswith("…") and len(p) <= 18 for p in chat.THINKING)
