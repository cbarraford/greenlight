"""Tests for scripts/harness.py argv builders, routing, and chat.Norm mappers across all harnesses."""
import json

import pytest

import greenlight
import harness
from chat import Norm


@pytest.fixture()
def harness_env(gl_home, monkeypatch):
    monkeypatch.setattr(harness, "binary", lambda n: n)
    return harness.CONFIG.setdefault("harnesses", {})


def test_argv_safe_defaults(harness_env):
    assert harness.argv("claude", "hi", model="m", sid="s1", system="SYS")[0] == [
        "claude", "-p", "--permission-mode", "acceptEdits", "--append-system-prompt", "SYS", "--model", "m", "--resume", "s1",
    ]
    cmd, text = harness.argv("codex", "hi", effort="max", system="SYS", stream=True)
    assert cmd == [
        "codex", "exec", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write",
        "--json", "-c", "model_reasoning_effort=high", "-",
    ], cmd
    assert text == "SYS\n\nhi"  # no system flag: the first message carries it...
    assert harness.argv("codex", "hi", sid="t1", system="SYS") == (
        ["codex", "exec", "resume", "t1", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write", "-"],
        "hi",
    )  # ...not a resumed one
    assert harness.argv("opencode", "hi", model="p/m", effort="high", sid="ses_1", stream=True)[0] == [
        "opencode", "run", "--format", "json", "-m", "p/m", "--variant", "high", "-s", "ses_1",
    ]
    with pytest.raises(ValueError, match="bypass"):
        harness.argv("pi", "hi")


def test_argv_bypass_and_interactive(harness_env):
    # bypass is opt-in, per harness
    harness_env.update(claude={"bypass": True}, codex={"bypass": True}, opencode={"bypass": True}, pi={"bypass": True})
    assert harness.argv("claude", "x")[0][2:4] == ["--permission-mode", "bypassPermissions"]
    assert "--dangerously-bypass-approvals-and-sandbox" in harness.argv("codex", "x")[0]
    assert harness.argv("opencode", "x")[0] == ["opencode", "run", "--auto"]
    assert harness.argv("pi", "x", effort="max", sid="u1", stream=True)[0] == [
        "pi", "-p", "--mode", "json", "--thinking", "xhigh", "--session", "u1",
    ]
    assert harness.interactive("opencode", "go") == ["opencode", "--prompt", "go"]
    assert harness.interactive("pi", "") == ["pi"]


def test_norm_opencode_events():
    # opencode run --format json (shapes from opencode 1.18's run command)
    n = Norm()
    assert n.opencode({"type": "step_start", "sessionID": "ses_1", "part": {}}) == []
    s = n.opencode({
        "type": "tool_use",
        "part": {"id": "p1", "tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}, "output": "a\nb", "title": "ls"}},
    })
    assert [e["e"] for e in s] == ["step", "step_done"] and s[1]["ok"] and s[0]["detail"] == "ls", s
    assert n.opencode({"type": "text", "part": {"text": "pong"}})[1] == {"e": "text", "delta": "pong", "whole": True} and n.text == "pong"
    assert n.opencode({"type": "step_finish", "part": {"tokens": {"input": 10, "output": 2, "cache": {"read": 90, "write": 0}}}}) == [
        {"e": "usage", "context": 100}
    ]
    err = n.opencode({"type": "error", "error": {"name": "APIError", "data": {"message": "This API key has expired."}}})[0]
    assert err["e"] == "done" and not err["ok"] and "expired" in err["text"]


def test_norm_pi_events():
    # pi --mode json (pi-agent-core AgentEvent)
    p = Norm()
    assert p.pi({"type": "message_update", "assistantMessageEvent": {"type": "text_start"}}) == [{"e": "text_reset"}]
    assert p.pi({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "po"}}) == [{"e": "text", "delta": "po"}]
    s = p.pi({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}})[0]
    assert s["e"] == "step" and s["detail"] == "ls"
    assert p.pi({"type": "tool_execution_end", "toolCallId": "c1", "result": {"content": [{"type": "text", "text": "x"}]}, "isError": False})[0]["out"] == "x"
    p.pi({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "pong"}],
            "stopReason": "stop",
            "usage": {"input": 5, "cacheRead": 50, "cacheWrite": 0},
        },
    })
    done = p.pi({"type": "agent_end", "messages": []})[0]
    assert done["ok"] and done["text"] == "pong" and done["context"] == 55, done


def test_gemini_argv_and_interactive(harness_env):
    # gemini: the text rides on stdin (no -p: a non-TTY stdin is headless), the system prompt in the first message
    cmd, text = harness.argv("gemini", "hi", model="gemini-4-neon", effort="high", system="SYS", stream=True)
    assert cmd == ["gemini", "--approval-mode", "auto_edit", "--skip-trust", "--output-format", "stream-json", "-m", "gemini-4-neon"], cmd
    assert text == "SYS\n\nhi"
    assert harness.argv("gemini", "hi", sid="uuid-1", system="SYS") == (
        ["gemini", "--approval-mode", "auto_edit", "--skip-trust", "--resume", "uuid-1"],
        "hi",
    )
    harness_env.update(gemini={"bypass": True})
    assert harness.argv("gemini", "x")[0] == ["gemini", "--approval-mode", "yolo", "--skip-trust"]
    assert harness.interactive("gemini", "go") == ["gemini", "-i", "go"]
    assert harness.interactive("gemini", "") == ["gemini"]


def test_route_model_to_harness(harness_env):
    # route: which harness runs a model id (chat's model, dash.py's --model)
    assert harness.route("claude-haiku-5-5") == ("claude", "claude-haiku-5-5") and harness.route(None) == ("claude", None)
    assert harness.route("gemini-4-neon-medium") == ("agy", "gemini-4-neon-medium") and harness.route("gemini") == ("gemini", None)
    assert harness.route("gemini-4-neon") == ("agy", "gemini-4-neon-medium")  # old saved choice
    assert harness.route("codex") == ("codex", None)
    assert harness.route("gemini-4-neon-medium", "codex") == ("agy", "gemini-4-neon-medium")  # a Gemini pick wins over llm_harness
    assert harness.route("claude-haiku-5-5", "codex") == ("codex", None)  # a Claude name means nothing to codex: its default


def test_norm_gemini_events_and_retry():
    # gemini --output-format stream-json (JsonStreamEvent shapes from gemini-cli-core 0.70's output/types)
    g = Norm()
    assert g.gemini({"type": "init", "timestamp": "t", "session_id": "uuid-1", "model": "gemini-4-neon"}) == []
    assert g.gemini({"type": "message", "role": "user", "content": "hi"}) == []
    assert g.gemini({"type": "message", "role": "assistant", "content": "Let me ", "delta": True}) == [
        {"e": "text_reset"},
        {"e": "text", "delta": "Let me "},
    ]
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
    assert done == {"e": "done", "ok": True, "text": "pong", "context": 0, "window": None, "ms": 800}, done

    f = Norm()
    f.gemini({"type": "error", "severity": "error", "message": "You have exhausted your daily quota on this model."})
    bad = f.gemini({"type": "result", "status": "error", "stats": {}})[0]
    assert not bad["ok"] and "quota" in bad["text"], bad
    assert Norm().gemini({"type": "result", "status": "error", "error": {"type": "API", "message": "model not found"}})[0]["text"] == "model not found"

    # a retry (same Norm): the new run's first delta resets the reply, not appends to the ignored one
    k = Norm()
    k.gemini({"type": "message", "role": "assistant", "content": "No response requested.", "delta": True})
    k.text = ""  # turn() before the retry
    k.gemini({"type": "init", "session_id": "uuid-2", "model": "gemini-4-neon"})
    assert k.gemini({"type": "message", "role": "assistant", "content": "real", "delta": True})[0] == {"e": "text_reset"}


def test_limit_and_dead_regexes():
    assert greenlight.LIMIT_RE.search("You have exhausted your daily quota on this model.")
    assert greenlight.LIMIT_RE.search("Your quota will reset after 3h")
    for other in ("OSError: [Errno 122] Disk quota exceeded", "grpc StatusCode.RESOURCE_EXHAUSTED", "Quota exceeded for quota metric 'reads'"):
        assert not greenlight.LIMIT_RE.search(other), other  # someone else's quota isn't the agent's usage limit
    assert greenlight.DEAD_RE.search("API key not valid. Please pass a valid API key.")
    assert greenlight.DEAD_RE.search("API key expired. Please renew the API key.")


def test_agy_argv_and_norm_events(harness_env):
    cmd, text = harness.argv("agy", "hi", model="gemini-4-neon-medium", system="SYS", stream=True)
    assert cmd == ["agy", "--mode", "accept-edits", "--input-format", "stream-json", "--output-format", "stream-json", "--model", "gemini-4-neon-medium"]
    assert json.loads(text)["message"]["content"] == "SYS\n\nhi"
    cmd, text = harness.argv("agy", "again", sid="conv-1", system="SYS")
    assert cmd == ["agy", "--mode", "accept-edits", "--input-format", "stream-json", "--output-format", "stream-json", "--conversation", "conv-1"]
    assert json.loads(text)["message"]["content"] == "again"

    a = Norm()
    assert a.agy({"event": "init", "conversation_id": "conv-1"}) == []
    assert a.agy({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "pong"}}) == [
        {"e": "text_reset"},
        {"e": "text", "delta": "pong"},
    ]
    step = {
        "event": "step_update",
        "step_update": {"step_index": 2, "step_type": "tool", "state": "ACTIVE", "tool_name": "run_command", "tool_info": {"parameters": {"CommandLine": "pwd"}}},
    }
    assert a.agy(step)[0]["detail"] == "pwd"
    step["step_update"].update(state="DONE", tool_info={"output": "Command exited with code 0.\n/tmp"})
    assert a.agy(step)[0]["ok"]
    step["step_update"].update(state="ERROR", tool_info={"error": "permission denied"})
    assert not a.agy(step)[0]["ok"]
    usage = a.agy({
        "event": "step_update",
        "step_update": {"step_type": "agent_response", "state": "DONE", "usage": {"input_tokens": 2217, "cache_read_tokens": 6819}},
    })
    assert usage == [{"e": "usage", "context": 9036}]
    assert a.agy({"event": "result", "result": {"status": "SUCCESS", "response": "pong", "duration_seconds": 1.5}})[0] == {
        "e": "done",
        "ok": True,
        "text": "pong",
        "context": 9036,
        "window": None,
        "ms": 1500,
    }
    denied = Norm().agy({"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]}})[0]
    assert not denied["ok"] and "command" in denied["text"]


def test_harness_default_model(harness_env):
    # a harness's default model (harnesses.<name>.model): headless runs, and Orca launches (agy is Orca's "antigravity")
    harness_env["agy"] = {"model": "gemini-4-neon-high"}
    cmd = harness.argv("agy", "hi")[0]
    assert cmd[cmd.index("--model") + 1] == "gemini-4-neon-high", cmd
    assert harness.argv("agy", "hi", model="gemini-3.8-flash-medium")[0][-1] == "gemini-3.8-flash-medium"  # asked for wins
    assert "--model" not in harness.argv("codex", "hi")[0]  # none set: the CLI's own
    assert greenlight.orca_agent(("agent", "launch", "--agent", "agy", "--prompt", "p")) == [
        "agent", "launch", "--agent", "antigravity", "--prompt", "p", "--model", "gemini-4-neon-high"]
    assert greenlight.orca_agent(("agent", "launch", "--agent", "agy", "--model", "x"))[-2:] == ["--model", "x"]
    assert greenlight.orca_agent(("agent", "launch", "--agent", "claude")) == ["agent", "launch", "--agent", "claude"]
    assert greenlight.orca_agent(("worktree", "ps")) == ["worktree", "ps"]
