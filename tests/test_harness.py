"""Tests for scripts/harness.py argv builders, routing, and chat.Norm mappers across all harnesses."""
import json

import pytest

import greenlight
import harness
from chat import Norm


@pytest.fixture()
def harness_env(gl_home, monkeypatch):
    monkeypatch.setattr(harness, "binary", lambda n: n)
    monkeypatch.setenv("CODEX_HOME", str(gl_home / "codex"))  # no models cache: an effort passes as asked
    return harness.CONFIG.setdefault("harnesses", {})


def test_argv_safe_defaults(harness_env):
    assert harness.argv("claude", "hi", model="m", sid="s1", system="SYS")[0] == [
        "claude", "-p", "--permission-mode", "acceptEdits", "--append-system-prompt", "SYS", "--model", "m", "--resume", "s1",
    ]
    cmd, text = harness.argv("codex", "hi", effort="max", system="SYS", stream=True)
    assert cmd == [
        "codex", "exec", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write",
        "--json", "-c", "model_reasoning_effort=max", "-",
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
    assert harness.interactive("opencode", "go") == ["opencode", "--auto", "--prompt", "go"]
    assert harness.interactive("pi", "") == ["pi"]


def test_interactive_carries_headless_permissions(harness_env):
    # a terminal launch gets the same safe/bypass args as a headless run (agy once launched with neither and stopped on
    # every command); with bypass on, agy and codex also trust the worktree for that session so their trust dialogs can't stall it
    for name in ("claude", "codex", "agy", "gemini", "opencode"):
        assert harness.interactive(name, "go")[1:1 + len(harness.H[name]["safe"])] == harness.H[name]["safe"], name
    harness_env.update(claude={"bypass": True}, codex={"bypass": True}, agy={"bypass": True, "model": "gemini-4-neon-high"})
    assert harness.interactive("claude", "go") == ["claude", "--permission-mode", "bypassPermissions", "--", "go"]
    assert harness.interactive("codex", "go") == ["codex", "--dangerously-bypass-approvals-and-sandbox", "--", "go"]
    assert harness.interactive("agy", "go") == ["agy", "--dangerously-skip-permissions", "--model", "gemini-4-neon-high", "-i", "go"]
    cmd = harness.interactive("agy", "go", cwd="/w/ws-1")
    assert cmd[:2] == ["agy", "--dangerously-skip-permissions"] and cmd[-2:] == ["-i", "go"], cmd
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"trustedWorkspaces": ["/w/ws-1"]}
    assert harness.interactive("codex", "go", cwd="/w/ws.1") == [
        "codex", "--dangerously-bypass-approvals-and-sandbox", "-c", 'projects={"/w/ws.1"={trust_level="trusted"}}', "--", "go"]
    assert harness.interactive("claude", "go", cwd="/w/ws-1") == harness.interactive("claude", "go")  # no session-only trust
    harness_env["agy"] = {"bypass": "yes"}  # strict True only, as headless
    assert harness.interactive("agy", "go", cwd="/w/ws-1") == ["agy", "--mode", "accept-edits", "-i", "go"]
    assert harness.interactive("pi", "go") == ["pi", "--", "go"]  # no gate either way


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
    assert harness.interactive("gemini", "go") == ["gemini", "--approval-mode", "yolo", "--skip-trust", "-i", "go"]
    assert harness.interactive("gemini", "") == ["gemini", "--approval-mode", "yolo", "--skip-trust"]


def test_route_model_to_harness(harness_env):
    # route: which harness runs a model id (chat's model, dash.py's --model)
    assert harness.route("claude-haiku-5-5") == ("claude", "claude-haiku-5-5") and harness.route(None) == ("claude", None)
    assert harness.route("gemini-4-neon-medium") == ("agy", "gemini-4-neon-medium") and harness.route("gemini") == ("gemini", None)
    assert harness.route("gemini-4-neon") == ("agy", "gemini-4-neon-medium")  # old saved choice
    assert harness.route("codex") == ("codex", None)
    assert harness.route("gemini-4-neon-medium", "codex") == ("agy", "gemini-4-neon-medium")  # a Gemini pick wins over llm_harness
    # every pick runs on the harness it names, whatever llm_harness is: claude, codex and agy for any model
    assert harness.route("claude-haiku-5-5", "codex") == ("claude", "claude-haiku-5-5")
    assert harness.route("gpt-6-luna") == ("codex", "gpt-6-luna") and harness.route("o4-mini", "agy") == ("codex", "o4-mini")
    assert harness.route("codex-mini-latest") == ("codex", "codex-mini-latest")
    assert harness.route("gpt-oss-120b-medium") == ("agy", "gpt-oss-120b-medium")  # agy's, not Codex's
    assert harness.route("agy:claude-opus-5-5-high") == ("agy", "claude-opus-5-5-high")  # a Claude model on agy
    assert harness.route("codex:some-new-model") == ("codex", "some-new-model") and harness.route("claude:opus") == ("claude", "opus")
    assert harness.route("opencode:p/m") == ("opencode", "p/m") and harness.route("agy:") == ("agy", None)
    assert harness.route("mystery-7", "codex") == ("codex", "mystery-7")  # an id that names no harness: llm_harness gets it
    assert harness.route("nope:x") == ("claude", "nope:x")  # not a harness prefix
    assert harness.harness_of("ollama") is None and harness.harness_of("") is None and harness.harness_of("orbit") is None


def test_codex_effort_follows_models_cache(harness_env, gl_home):
    (gl_home / "codex").mkdir()
    lv = lambda *e: [{"effort": x} for x in e]
    (gl_home / "codex" / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-old", "supported_reasoning_levels": lv("low", "medium", "high")},
        {"slug": "gpt-new", "supported_reasoning_levels": lv("low", "medium", "high", "xhigh", "max")},
        {"slug": "gpt-hidden", "visibility": "hide", "supported_reasoning_levels": lv("medium", "high")}]}))
    assert harness.codex_effort("gpt-old", "max") == "high" and harness.codex_effort("gpt-old", "xhigh") == "high"
    assert harness.codex_effort("gpt-new", "max") == "max" and harness.codex_effort("gpt-unknown", "max") == "max"
    assert harness.codex_effort("gpt-hidden", "low") == "medium"  # nothing at or below: its lowest, not highest
    assert harness.codex_effort(None, None) is None
    assert "model_reasoning_effort=high" in harness.argv("codex", "x", model="gpt-old", effort="max")[0]
    assert harness.argv("agy", "x", model="claude-opus-5-5", effort="high")[0][-2:] == ["--effort", "high"]
    assert "--effort" not in harness.argv("agy", "x", model="gemini-4-neon-low", effort="high")[0]  # its id carries one
    assert "--effort" not in harness.argv("agy", "x", model="gemini-4-neon-low")[0]


def test_models_lists_every_installed_harness(harness_env, gl_home, monkeypatch):
    (gl_home / "codex").mkdir()
    (gl_home / "codex" / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna"}, {"slug": "secret", "visibility": "hide"}, {"slug": "astra-9"}]}))
    monkeypatch.setattr(harness, "installed", lambda n: n in ("claude", "codex", "agy"))
    agy_out = "Fetching available models...\ngemini-4-neon-low\tGemini 4 Neon (Low)\nclaude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return harness.subprocess.CompletedProcess(cmd, 0, stdout=agy_out, stderr="")
    monkeypatch.setattr(harness.subprocess, "run", run)
    ms = {m["id"]: m for m in harness.models()}
    assert ms["claude-opus-5-5"]["harness"] == "claude" and ms["gpt-6-luna"] == {"id": "gpt-6-luna", "name": "GPT-6-Luna", "harness": "codex"}
    assert "secret" not in ms and "codex:secret" not in ms and ms["codex:astra-9"]["harness"] == "codex" and "codex" in ms
    assert ms["gemini-4-neon-low"]["name"] == "Gemini 4 Neon (Low)"
    assert ms["agy:claude-opus-5-5-high"] == {"id": "agy:claude-opus-5-5-high", "name": "Claude Opus 5.5 (High) · agy", "harness": "agy"}
    assert ms["agy"] == {"id": "agy", "name": "Antigravity default", "harness": "agy"}
    assert "opencode" not in ms and all(harness.route(i)[0] == m["harness"] for i, m in ms.items())
    harness.models()
    assert len(calls) == 1  # agy's list is cached for a day


def test_ask_runs_the_picked_harness_toolless(harness_env, monkeypatch):
    seen = []
    def run(cmd, **kw):
        seen.append(cmd)
        out = '{"event":"result","result":{"status":"SUCCESS","response":"hi"}}' if cmd[0] == "agy" else "plain\n"
        return harness.subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(harness.subprocess, "run", run)
    assert harness.ask("p", "agy:claude-sonnet-5-5-low") == "hi" and seen[-1][:2] == ["agy", "--input-format"]
    assert harness.ask("p", "gpt-6-luna", effort="low") == "plain\n"
    assert seen[-1][:2] == ["codex", "exec"] and "sandbox_mode=read-only" in seen[-1] and "gpt-6-luna" in seen[-1]
    assert harness.ask("p", None) == "plain\n" and seen[-1][-2:] == ["--model", "claude-haiku-5-5"]  # claude's cheap default
    assert harness.ask("p", None, default="codex") == "plain\n" and "-m" not in seen[-1]  # codex: its own default
    assert harness.answer("agy", '{"event":"result","result":{"status":"ERROR","response":"x"}}') == ""
    # Codex-only host: missing claude binary falls back cleanly to default harness
    monkeypatch.setattr(harness, "installed", lambda n: n == "codex")
    def missing_claude(cmd, **kw):
        seen.append(cmd)
        if cmd[0] == "claude":
            raise FileNotFoundError("claude")
        return harness.subprocess.CompletedProcess(cmd, 0, stdout="codex fallback\n", stderr="")
    monkeypatch.setattr(harness.subprocess, "run", missing_claude)
    assert harness.ask("p", "claude-haiku-5-5", default="codex") == "codex fallback\n" and seen[-1][:2] == ["codex", "exec"]
    monkeypatch.setattr(harness.subprocess, "run", lambda cmd, **kw: harness.subprocess.CompletedProcess(cmd, 1, stdout="error text", stderr=""))
    assert harness.ask("p", "claude-gone-1") == ""


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


def test_canonical_interactive_and_failover(harness_env, monkeypatch):
    # agy uses -i for interactive prompts; antigravity alias normalizes to agy; configured default models pass through
    assert harness.canonical("antigravity") == "agy" and harness.canonical("Claude") == "claude"
    assert harness.interactive("agy", "ship it") == ["agy", "--mode", "accept-edits", "-i", "ship it"]
    assert harness.interactive("antigravity", "ship it") == ["agy", "--mode", "accept-edits", "-i", "ship it"]
    assert harness.interactive("codex", "ship it") == ["codex", "-c", "sandbox_mode=workspace-write", "--", "ship it"]
    harness_env["agy"] = {"model": "gemini-4-neon-high"}
    harness_env["codex"] = {"model": "gpt-6-luna"}
    assert harness.interactive("agy", "hi") == ["agy", "--mode", "accept-edits", "--model", "gemini-4-neon-high", "-i", "hi"]
    assert harness.interactive("codex", "hi") == ["codex", "-c", "sandbox_mode=workspace-write", "-m", "gpt-6-luna", "--", "hi"]
    assert harness.interactive("agy", "") == ["agy", "--mode", "accept-edits", "--model", "gemini-4-neon-high"]

    # greenlight normalizes Orca's "antigravity" so rate-limit failover moves to the next agent instead of looping on agy
    assert greenlight.canonical_agent("antigravity") == "agy" and greenlight.canonical_agent("codex") == "codex"
    monkeypatch.setitem(greenlight.CONFIG, "agents", ["claude", "agy", "codex"])
    ws = {"id": "w1", "next": {"owner": "agent", "step": "ci_green", "action": "Fix CI"},
          "agent": {"state": "done", "type": "antigravity", "pane_key": None, "last": ""}}
    terms = {"w1": [{"handle": "t1", "agentIdentity": "antigravity", "lastOutputAt": 100}]}
    monkeypatch.setattr(greenlight, "orca", lambda *a, **k: {"terminal": {"tail": "You've hit your usage limit."}})
    greenlight.agent_overlay(ws, terms)
    assert ws["next"]["agent"] == "claude" and "agy out of tokens" in ws["next"]["action"]


def test_argv_claude_allows_approved_calls(harness_env):
    cmd, _ = harness.argv("claude", "hi", allow=("Bash(orca host list)", "WebFetch"))
    assert cmd[-3:] == ["--allowedTools", "Bash(orca host list)", "WebFetch"], cmd  # last: the flag takes every argument after it
    assert "--allowedTools" not in harness.argv("claude", "hi", allow=("Bash(x)",), tools=False)[0]
    assert "--allowedTools" not in harness.argv("codex", "hi", allow=("Bash(x)",))[0]


def test_fallback_chain_per_provider_dedupe_and_config(harness_env, monkeypatch):
    monkeypatch.setattr(harness, "installed", lambda n: n in ("claude", "agy", "codex"))
    monkeypatch.setitem(greenlight.CONFIG, "agents", ["claude", "codex", "agy"])
    # Explicit per-provider list: deduplicates by harness, skips primary harness and uninstalled ones, preserves order & effort
    chain = harness.fallback_chain(
        "claude-opus-5-5",
        effort="high",
        fallbacks=["claude-sonnet-5-5@low", "gemini-4-neon-high", "agy:claude-opus-5-5-high", "gpt-6.2-sol@medium", "gemini"],
    )
    assert chain == [
        ("claude", "claude-opus-5-5", "high"),
        ("agy", "gemini-4-neon-high", "high"),
        ("codex", "gpt-6.2-sol", "medium"),
    ]
    # Explicit empty list (--no-fallback) disables auto-fill
    assert harness.fallback_chain("claude-opus-5-5", effort="high", fallbacks=[]) == [("claude", "claude-opus-5-5", "high")]
    # Config model_fallbacks per slot + auto-fill of remaining configured providers
    monkeypatch.setitem(greenlight.CONFIG, "model_fallbacks", {
        "chat": ["gemini-4-neon-high", "gpt-6.2-sol@high"],
        "suggest": {"agy": "gemini-3.8-flash-medium", "codex": {"model": "gpt-6-luna", "effort": "low"}},
    })
    assert harness.fallback_chain("claude-opus-5-5", effort="medium", slot="chat") == [
        ("claude", "claude-opus-5-5", "medium"),
        ("agy", "gemini-4-neon-high", "medium"),
        ("codex", "gpt-6.2-sol", "high"),
    ]
    assert harness.fallback_chain("claude-haiku-5-5", effort="low", slot="suggest") == [
        ("claude", "claude-haiku-5-5", "low"),
        ("codex", "gpt-6-luna", "low"),
        ("agy", "gemini-3.8-flash-medium", "low"),
    ]


def test_proactive_usage_cap_cooldown_and_ordering(harness_env, gl_home, monkeypatch):
    monkeypatch.delenv("GREENLIGHT_USAGE", raising=False)
    now = 2_000_000_000.0
    usage_data = {
        "p": {
            "claude": {
                "id": "claude",
                "state": "ok",
                "at": int(now - 30),
                "windows": [
                    {"label": "5h", "pct": 100.0, "resets": int(now + 1800)},
                    {"label": "Opus", "pct": 100.0, "resets": int(now + 86400)},
                ],
            },
            "agy": {
                "id": "agy",
                "state": "ok",
                "at": int(now - 30),
                "note": "5h and Week: Claude and GPT",
                "windows": [
                    {"label": "5h", "pct": 100.0, "resets": int(now + 1800)},
                    {"label": "Gemini", "pct": 25.0, "resets": int(now + 1800)},
                ],
            },
            "codex": {
                "id": "codex",
                "state": "ok",
                "at": int(now - 30),
                "windows": [
                    {"label": "Week", "pct": 100.0, "resets": int(now - 10)},  # already reset
                ],
            },
        }
    }
    (gl_home / "usage.json").write_text(json.dumps(usage_data))
    # Claude 5h at 100% caps all Claude models until reset
    assert harness.is_capped("claude", "claude-haiku-5-5", now=now) == "usage_cap"
    # After 5h resets (now + 2000), Opus pool still caps Opus, but Haiku is uncapped again!
    assert harness.is_capped("claude", "claude-opus-5-5", now=now + 2000) == "usage_cap"
    assert harness.is_capped("claude", "claude-haiku-5-5", now=now + 2000) is None
    # Antigravity 5h (Claude and GPT) caps agy:claude-* but NOT gemini-4-neon-high
    assert harness.is_capped("agy", "claude-opus-5-5-high", now=now) == "usage_cap"
    assert harness.is_capped("agy", "gemini-4-neon-high", now=now) is None
    # Codex weekly window already reset -> not capped
    assert harness.is_capped("codex", "gpt-6.2-sol", now=now) is None

    # Runtime cooldown: record -> capped -> clear -> uncapped
    until = harness.record_limit("codex", "5-hour limit reached", model="gpt-6.2-sol", now=now)
    assert until == now + harness.LIMIT_COOLDOWN
    assert harness.is_capped("codex", "gpt-6.2-sol", now=now + 10) == "cooldown"
    assert harness.is_capped("codex", "gpt-6.2-sol", now=until + 1) is None
    status = harness.limits_status(now=now + 10)
    assert status["codex"]["capped"] and status["codex"]["reason"] == "cooldown"
    harness.clear_limit("codex")
    assert harness.is_capped("codex", "gpt-6.2-sol", now=now + 10) is None

    # order_by_availability moves capped providers after ready ones without dropping them
    chain = [("claude", "claude-opus-5-5", "high"), ("agy", "gemini-4-neon-high", "high"), ("codex", "gpt-6.2-sol", "high")]
    ordered, skipped = harness.order_by_availability(chain, now=now)
    assert [h for h, _, _ in ordered] == ["agy", "codex", "claude"]
    assert skipped == {"claude": "usage_cap"}


def test_ask_reactive_multi_provider_and_proactive_fallback(harness_env, gl_home, monkeypatch):
    monkeypatch.setattr(harness, "installed", lambda n: n in ("claude", "agy", "codex"))
    seen = []

    def multi_cap(cmd, **kw):
        seen.append(cmd[0])
        if cmd[0] == "claude":
            return harness.subprocess.CompletedProcess(cmd, 1, stdout="", stderr="You've hit your 5-hour limit.")
        if cmd[0] == "agy":
            return harness.subprocess.CompletedProcess(cmd, 1, stdout="You have exhausted your daily quota.", stderr="")
        return harness.subprocess.CompletedProcess(cmd, 0, stdout="codex saved the day\n", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", multi_cap)
    ans = harness.ask("hi", model="claude-opus-5-5", effort="high", fallbacks=["gemini-4-neon-high", "gpt-6.2-sol@medium"])
    assert ans == "codex saved the day\n"
    assert seen == ["claude", "agy", "codex"]
    # Both claude and agy now have runtime cooldowns recorded; codex was cleared
    assert harness.is_capped("claude") == "cooldown"
    assert harness.is_capped("agy") == "cooldown"
    assert harness.is_capped("codex") is None

    # Next call skips both cooled-down providers proactively and goes straight to codex!
    seen.clear()
    ans2 = harness.ask("hi again", model="claude-opus-5-5", effort="high", fallbacks=["gemini-4-neon-high", "gpt-6.2-sol@medium"])
    assert ans2 == "codex saved the day\n"
    assert seen == ["codex"]

    # Non-limit error does NOT trigger fallback
    harness.clear_limit("claude")
    seen.clear()

    def syntax_fail(cmd, **kw):
        seen.append(cmd[0])
        return harness.subprocess.CompletedProcess(cmd, 1, stdout="syntax error", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", syntax_fail)
    assert harness.ask("hi", model="claude-opus-5-5", fallbacks=["gpt-6.2-sol"]) == ""
    assert seen == ["claude"]


def test_chat_and_decide_and_dash_fallback(harness_env, gl_home, monkeypatch):
    import auto
    import chat
    import dash
    import decide

    monkeypatch.setattr(harness, "installed", lambda n: n in ("claude", "agy", "codex"))
    monkeypatch.setitem(greenlight.CONFIG, "agents", ["claude", "agy", "codex"])

    # dash._model_opts parses repeatable --fallback and --no-fallback
    assert dash._model_opts(["page", "w", "--model", "claude-haiku-5-5", "--fallback", "gemini-3.8-flash-medium", "--fallback", "gpt-6-luna@low"]) == {
        "model": "claude-haiku-5-5",
        "fallbacks": ["gemini-3.8-flash-medium", "gpt-6-luna@low"],
    }
    assert dash._model_opts(["page", "w", "--no-fallback"]) == {"fallbacks": []}

    # chat._turn_owned falls back across providers on rate limit and emits fallback event
    monkeypatch.setattr(chat, "DIR", str(gl_home / "chat"))
    (gl_home / "chat").mkdir(parents=True, exist_ok=True)
    events = []
    monkeypatch.setattr(chat, "emit", lambda **kw: events.append(kw))
    monkeypatch.setattr(chat, "board_note", lambda *a, **kw: "")
    monkeypatch.setattr(chat, "check_session_scope", lambda *a, **kw: None)
    monkeypatch.setattr(chat.decisions, "load", list)
    monkeypatch.setattr(chat.decisions, "quiet", lambda *a, **kw: None)

    tried = []

    def fake_chat_run(cmd, engine, text, norm, **kw):
        tried.append(engine)
        if engine == "claude":
            norm.pending_done = {"e": "done", "ok": False, "text": "You've hit your weekly limit", "context": 0, "window": None, "ms": 10}
            return None, "model failed"
        norm.reply = "answered by agy"
        norm.pending_done = {"e": "done", "ok": True, "text": "answered by agy", "context": 100, "window": None, "ms": 50}
        return "conv-agy", None

    monkeypatch.setattr(chat, "run", fake_chat_run)
    chat.TURN["fallbacks"] = ["gemini-4-neon-high", "gpt-6.2-sol@high"]
    done = chat._turn_owned("status?", "claude-opus-5-5", "high")
    assert done["ok"] and done["text"] == "answered by agy"
    assert tried == ["claude", "agy"]
    assert any(e.get("e") == "fallback" and e.get("from") == "claude" and e.get("engine") == "agy" for e in events)
    assert harness.is_capped("claude") == "cooldown"

    # Proactive skip on next chat turn: jumps straight to agy without calling claude
    tried.clear()
    events.clear()
    done2 = chat._turn_owned("next?", "claude-opus-5-5", "high")
    assert done2["ok"] and tried == ["agy"]
    assert any(e.get("e") == "fallback" and e.get("reason") == "cooldown" for e in events)
    harness.clear_limit("claude")
    chat.TURN.pop("fallbacks", None)

    # auto.py uses configured fallback model/effort for fallback provider
    monkeypatch.setitem(greenlight.CONFIG, "model_fallbacks", {"auto": ["gpt-6-luna@low"]})
    monkeypatch.setattr(auto, "FILE", gl_home / "automations.json")
    monkeypatch.setattr(auto, "DIR", gl_home / "auto")
    auto.save({"job": {"schedule": "* * * * *", "prompt": "check"}})
    auto_calls = []

    def fake_auto_agent(a, engine, log):
        auto_calls.append((engine, a.get("_fallback_model"), a.get("_fallback_effort")))
        if engine == "claude":
            return ("weekly limit reached", 1, {})
        return ("all good", 0, {})

    monkeypatch.setattr(auto, "agent", fake_auto_agent)
    auto.execute("job")
    assert auto_calls == [("claude", None, None), ("codex", "gpt-6-luna", "low")]
    harness.clear_limit("claude")

    # decide.haiku falls back via harness.ask when claude -p hits usage limit (without re-trying claude)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(decide, "_secret", lambda *a: "")
    monkeypatch.setattr(
        decide.subprocess,
        "run",
        lambda cmd, **kw: decide.subprocess.CompletedProcess(cmd, 1, stdout="", stderr="5-hour limit reached"),
    )
    ask_kwargs = []

    def fake_ask(prompt, **kw):
        ask_kwargs.append(kw)
        return '{"q0": {"p_yes": 0.9}}'

    monkeypatch.setattr(harness, "ask", fake_ask)
    res = decide.haiku("state", {"ok": {"type": "bool", "question": "ready?", "context": ""}}, decide.conf())
    assert res["ok"]["value"] is True and res["ok"]["prob"] == 0.9 and harness.is_capped("claude") == "cooldown"
    assert ask_kwargs[-1]["exclude"] == ("claude",) and ask_kwargs[-1]["slot"] == "decide"


def test_review_edge_cases(harness_env, gl_home, monkeypatch):
    import chat

    # Multiline & JSON auth errors are detected; prose mentioning "429" or "rate limit" is not a false positive
    assert harness.is_unavailable_error('warning: x\n{"type":"error","error":{"type":"authentication_error"}}')
    assert harness.is_unavailable_error("some prefix\nNot logged in")
    assert harness.fallback_reason("x", missing=True) == "missing"
    assert harness.is_limit_error("HTTP 429 Too Many Requests") and harness.is_limit_error("rate_limit_error")
    assert not harness.is_limit_error("File foo.py, line 429: ValueError")
    assert not harness.is_limit_error("test_rate_limit_middleware passed")

    # Cross-provider dict config keeps the target harness prefix; invalid keys ignored
    monkeypatch.setattr(harness, "installed", lambda n: n in ("claude", "agy", "codex", "pi"))
    monkeypatch.setitem(greenlight.CONFIG, "agents", ["claude", "agy", "codex"])
    monkeypatch.setitem(greenlight.CONFIG, "model_fallbacks", {
        "agy": "claude-opus-5-5-high",
        "unknown_tool": "foo",
        "codex": {"model": "gpt-6.2-sol", "effort": "high"},
    })
    assert harness.fallback_chain("claude-opus-5-5", effort="medium") == [
        ("claude", "claude-opus-5-5", "medium"),
        ("agy", "claude-opus-5-5-high", "medium"),
        ("codex", "gpt-6.2-sol", "high"),
    ]
    # Explicit pi without bypass is skipped instead of crashing later
    assert harness.fallback_chain("claude-opus-5-5", fallbacks=["pi", "gpt-6.2-sol"]) == [
        ("claude", "claude-opus-5-5", None),
        ("codex", "gpt-6.2-sol", None),
    ]

    # Flag parsing skips option values and rejects leading dashes
    assert harness.parse_fallback_flags(["send", "--name", "--no-fallback", "--fallback", "gpt-6-luna@low"], value_flags=("--name",)) == ["gpt-6-luna@low"]
    assert harness.parse_fallback_flags(["send", "--no-fallback"]) == []
    with pytest.raises(ValueError, match="bad --fallback"):
        harness.parse_fallback_flags(["send", "--fallback", "--inject"])

    # agy multi-group "Claude 5h" (note=None) caps Claude on agy, not Gemini; "daybreak" doesn't match "day"
    assert harness._window_applies("agy", "Claude 5h", "", "claude-opus-5-5-high") is True
    assert harness._window_applies("agy", "Claude 5h", "", "gemini-4-neon-high") is False
    assert harness._window_applies("claude", "Opus", "", "claude-haiku-5-5") is False
    assert harness._window_applies("pi", "Opus", "", "claude-haiku-5-5") is False

    # Chat mid-turn failover cleans up dangling steps, plan, and partial text before retrying fallback
    monkeypatch.setattr(chat, "DIR", str(gl_home / "chat2"))
    (gl_home / "chat2").mkdir(parents=True, exist_ok=True)
    emitted = []
    monkeypatch.setattr(chat, "emit", lambda **kw: emitted.append(kw))
    monkeypatch.setattr(chat, "board_note", lambda *a, **kw: "")
    monkeypatch.setattr(chat, "check_session_scope", lambda *a, **kw: None)
    monkeypatch.setattr(chat.decisions, "load", list)
    monkeypatch.setattr(chat.decisions, "quiet", lambda *a, **kw: None)

    def mid_stream_fail(cmd, engine, text, norm, **kw):
        if engine == "claude":
            norm.running = [("s1", "Bash")]
            norm.plan_keys = ["k1"]
            norm.text = "partial output"
            norm.pending_done = {"e": "done", "ok": False, "text": "HTTP 429 too many requests", "context": 0, "window": None, "ms": 5}
            return None, "model failed"
        norm.reply = "clean fallback reply"
        norm.pending_done = {"e": "done", "ok": True, "text": "clean fallback reply", "context": 10, "window": None, "ms": 20}
        return "sid-agy", None

    monkeypatch.setattr(chat, "run", mid_stream_fail)
    chat.TURN["fallbacks"] = ["gemini-4-neon-high"]
    done = chat._turn_owned("check", "claude-opus-5-5", "high")
    chat.TURN.pop("fallbacks", None)
    assert done["ok"] and done["text"] == "clean fallback reply"
    assert any(e.get("e") == "step_done" and e.get("id") == "s1" and not e.get("ok") for e in emitted)
    assert any(e.get("e") == "plan" and e.get("total") == 0 for e in emitted)
    assert any(e.get("e") == "text_reset" for e in emitted)


def test_resume_cmd_harnesses_and_validation(harness_env):
    sid = "0eb62452-d4f6-4e8d-a381-64ec2d0e3137"
    assert harness.resume_cmd("claude", sid) == ["claude", "--resume", sid]
    assert harness.resume_cmd("codex", sid) == ["codex", "resume", sid]
    assert harness.resume_cmd("agy", sid) == ["agy", "--resume", sid]
    assert harness.resume_cmd("antigravity", sid) == ["agy", "--resume", sid]
    assert harness.resume_cmd("claude-code", sid) == ["claude", "--resume", sid]
    assert harness.resume_cmd("gemini-cli", sid) == ["gemini", "--resume", sid]
    assert harness.resume_cmd("opencode", "ses_abc123") == ["opencode", "-s", "ses_abc123"]
    assert harness.resume_cmd("gemini", sid) == ["gemini", "--resume", sid]
    assert harness.resume_cmd("pi", sid) == ["pi", "--session", sid]
    assert harness.resume_cmd("claude", sid, binary_override="/opt/bin/claude") == ["/opt/bin/claude", "--resume", sid]
    for bad in ("", "-flag", "id;whoami", "id with spaces", "id\nnext", "../etc", "aé", "a" * 129):
        with pytest.raises(ValueError):
            harness.resume_cmd("claude", bad)
    with pytest.raises(ValueError):
        harness.resume_cmd("unknown", sid)
