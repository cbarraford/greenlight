#!/usr/bin/env python3
"""harness.argv per harness, and chat's opencode/pi mappers on recorded event shapes: python3 test_harness.py"""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import harness
from chat import Norm

harness.binary = lambda n: n
H = harness.CONFIG.setdefault("harnesses", {})

# safe by default
assert harness.argv("claude", "hi", model="m", sid="s1", system="SYS")[0] == [
    "claude", "-p", "--permission-mode", "acceptEdits", "--append-system-prompt", "SYS", "--model", "m", "--resume", "s1"]
cmd, text = harness.argv("codex", "hi", effort="max", system="SYS", stream=True)
assert cmd == ["codex", "exec", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write", "--json", "-c", "model_reasoning_effort=high", "-"], cmd
assert text == "SYS\n\nhi"  # no system flag: the first message carries it...
assert harness.argv("codex", "hi", sid="t1", system="SYS") == (
    ["codex", "exec", "resume", "t1", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write", "-"], "hi")  # ...not a resumed one
assert harness.argv("opencode", "hi", model="p/m", effort="high", sid="ses_1", stream=True)[0] == [
    "opencode", "run", "--format", "json", "-m", "p/m", "--variant", "high", "-s", "ses_1"]
try:
    harness.argv("pi", "hi")
    raise AssertionError("pi ran without bypass")
except ValueError as e:
    assert "bypass" in str(e)

# bypass is opt-in, per harness
H.update(claude={"bypass": True}, codex={"bypass": True}, opencode={"bypass": True}, pi={"bypass": True})
assert harness.argv("claude", "x")[0][2:4] == ["--permission-mode", "bypassPermissions"]
assert "--dangerously-bypass-approvals-and-sandbox" in harness.argv("codex", "x")[0]
assert harness.argv("opencode", "x")[0] == ["opencode", "run", "--auto"]
assert harness.argv("pi", "x", effort="max", sid="u1", stream=True)[0] == [
    "pi", "-p", "--mode", "json", "--thinking", "xhigh", "--session", "u1"]
assert harness.interactive("opencode", "go") == ["opencode", "--prompt", "go"] and harness.interactive("pi", "") == ["pi"]

# opencode run --format json (shapes from opencode 1.18's run command)
n = Norm()
assert n.opencode({"type": "step_start", "sessionID": "ses_1", "part": {}}) == []
s = n.opencode({"type": "tool_use", "part": {"id": "p1", "tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}, "output": "a\nb", "title": "ls"}}})
assert [e["e"] for e in s] == ["step", "step_done"] and s[1]["ok"] and s[0]["detail"] == "ls", s
assert n.opencode({"type": "text", "part": {"text": "pong"}})[1] == {"e": "text", "delta": "pong", "whole": True} and n.text == "pong"
assert n.opencode({"type": "step_finish", "part": {"tokens": {"input": 10, "output": 2, "cache": {"read": 90, "write": 0}}}}) == [{"e": "usage", "context": 100}]
err = n.opencode({"type": "error", "error": {"name": "APIError", "data": {"message": "This API key has expired."}}})[0]
assert err["e"] == "done" and not err["ok"] and "expired" in err["text"]

# pi --mode json (pi-agent-core AgentEvent)
p = Norm()
assert p.pi({"type": "message_update", "assistantMessageEvent": {"type": "text_start"}}) == [{"e": "text_reset"}]
assert p.pi({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "po"}}) == [{"e": "text", "delta": "po"}]
s = p.pi({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}})[0]
assert s["e"] == "step" and s["detail"] == "ls"
assert p.pi({"type": "tool_execution_end", "toolCallId": "c1", "result": {"content": [{"type": "text", "text": "x"}]}, "isError": False})[0]["out"] == "x"
p.pi({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "pong"}], "stopReason": "stop",
                                         "usage": {"input": 5, "cacheRead": 50, "cacheWrite": 0}}})
done = p.pi({"type": "agent_end", "messages": []})[0]
assert done["ok"] and done["text"] == "pong" and done["context"] == 55, done
# gemini: the text rides on stdin (no -p: a non-TTY stdin is headless), the system prompt in the first message
cmd, text = harness.argv("gemini", "hi", model="gemini-4-neon", effort="high", system="SYS", stream=True)
assert cmd == ["gemini", "--approval-mode", "auto_edit", "--skip-trust", "--output-format", "stream-json", "-m", "gemini-4-neon"], cmd
assert text == "SYS\n\nhi"
assert harness.argv("gemini", "hi", sid="uuid-1", system="SYS") == (["gemini", "--approval-mode", "auto_edit", "--skip-trust", "--resume", "uuid-1"], "hi")
H.update(gemini={"bypass": True})
assert harness.argv("gemini", "x")[0] == ["gemini", "--approval-mode", "yolo", "--skip-trust"]
assert harness.interactive("gemini", "go") == ["gemini", "-i", "go"] and harness.interactive("gemini", "") == ["gemini"]

# route: which harness runs a model id (chat's model, dash.py's --model)
assert harness.route("claude-haiku-5-5") == ("claude", "claude-haiku-5-5") and harness.route(None) == ("claude", None)
assert harness.route("gemini-4-neon") == ("gemini", "gemini-4-neon") and harness.route("gemini") == ("gemini", None)
assert harness.route("codex") == ("codex", None)
assert harness.route("gemini-4-neon", "codex") == ("gemini", "gemini-4-neon")  # a Gemini pick wins over llm_harness
assert harness.route("claude-haiku-5-5", "codex") == ("codex", None)  # a Claude name means nothing to codex: its default

# gemini --output-format stream-json (JsonStreamEvent shapes from gemini-cli-core 0.70's output/types)
g = Norm()
assert g.gemini({"type": "init", "timestamp": "t", "session_id": "uuid-1", "model": "gemini-4-neon"}) == []
assert g.gemini({"type": "message", "role": "user", "content": "hi"}) == []
assert g.gemini({"type": "message", "role": "assistant", "content": "Let me ", "delta": True}) == [{"e": "text_reset"}, {"e": "text", "delta": "Let me "}]
assert g.gemini({"type": "message", "role": "assistant", "content": "look.", "delta": True}) == [{"e": "text", "delta": "look."}]
s = g.gemini({"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1", "parameters": {"command": "ls", "description": "List files"}})[0]
assert s["e"] == "step" and s["title"] == "List files" and s["detail"] == "ls" and s["call"] == "Bash(ls)", s
m = g.gemini({"type": "tool_use", "tool_name": "read_many_files", "tool_id": "t3", "parameters": {"include": ["attachments/a.txt", "b.md"]}})[0]
assert "attachments/a.txt" in m["detail"] and "attachments/a.txt" in m["call"], m  # attach.mentions reads detail/call: a read of an attachment is hidden
r = g.gemini({"type": "tool_result", "tool_id": "t1", "status": "success", "output": "a\nb"})[0]
assert r["e"] == "step_done" and r["ok"] and r["out"] == "a b", r
assert not g.gemini({"type": "tool_result", "tool_id": "t2", "status": "error", "error": {"type": "x", "message": "denied"}})[0]["ok"]
assert g.gemini({"type": "message", "role": "assistant", "content": "pong", "delta": True})[0] == {"e": "text_reset"}  # after a tool: a new block
assert g.gemini({"type": "error", "severity": "warning", "message": "Retrying"}) == [{"e": "status", "text": "Retrying"}]
done = g.gemini({"type": "result", "status": "success", "stats": {"input_tokens": 1200, "output_tokens": 9, "duration_ms": 800}})[0]
assert done == {"e": "done", "ok": True, "text": "pong", "context": 0, "window": None, "ms": 800}, done  # a run's summed input isn't context
f = Norm()
f.gemini({"type": "error", "severity": "error", "message": "You have exhausted your daily quota on this model."})
bad = f.gemini({"type": "result", "status": "error", "stats": {}})[0]
assert not bad["ok"] and "quota" in bad["text"], bad
assert Norm().gemini({"type": "result", "status": "error", "error": {"type": "API", "message": "model not found"}})[0]["text"] == "model not found"
import greenlight
assert greenlight.LIMIT_RE.search("You have exhausted your daily quota on this model.") and greenlight.LIMIT_RE.search("Your quota will reset after 3h")
for other in ("OSError: [Errno 122] Disk quota exceeded", "grpc StatusCode.RESOURCE_EXHAUSTED", "Quota exceeded for quota metric 'reads'"):
    assert not greenlight.LIMIT_RE.search(other), other  # someone else's quota isn't the agent's usage limit
assert greenlight.DEAD_RE.search("API key not valid. Please pass a valid API key.") and greenlight.DEAD_RE.search("API key expired. Please renew the API key.")
# a retry (same Norm): the new run's first delta resets the reply, not appends to the ignored one
k = Norm()
k.gemini({"type": "message", "role": "assistant", "content": "No response requested.", "delta": True})
k.text = ""  # turn() before the retry
k.gemini({"type": "init", "session_id": "uuid-2", "model": "gemini-4-neon"})
assert k.gemini({"type": "message", "role": "assistant", "content": "real", "delta": True})[0] == {"e": "text_reset"}
print("test_harness ok")
