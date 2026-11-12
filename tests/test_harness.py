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
assert n.opencode({"type": "text", "part": {"text": "pong"}})[1] == {"e": "text", "delta": "pong"} and n.text == "pong"
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
print("test_harness ok")
