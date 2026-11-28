"""Tests for scripts/decide.py: providers/fallbacks against fake servers, terminal-state classifier, goal intents, and agent overlay."""
import contextlib
import datetime as dt
import io
import json
import os
import stat
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

import ade_common
import decide
import greenlight
import harness
from conftest import SCRIPTS

FAKE_CLAUDE_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import json,sys,os\n"
    "sys.stdin.read()\n"
    "a=sys.argv; s=json.loads(a[a.index('--json-schema')+1])\n"
    "open(os.environ['GREENLIGHT_HOME']+'/claude-args.json','w').write(json.dumps(a))\n"
    "out={k:({'p_yes':0.9} if 'p_yes' in v['properties'] else "
    "{o:(0.8 if o=='o1' else 0.2/(len(v['properties'])-1)) for o in v['properties']}) for k,v in s['properties'].items()}\n"
    "print(json.dumps({'is_error':False,'subtype':'success','structured_output':out}))\n"
)


def _ins(q):
    i = q.get("instructions")
    return i["question"] if isinstance(i, dict) else i


@pytest.fixture()
def decide_env(gl_home, monkeypatch):
    for key in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    fake_claude = gl_home / "fakeclaude"
    fake_claude.write_text(FAKE_CLAUDE_SCRIPT)
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    (gl_home / "config.json").write_text(json.dumps({"harnesses": {"claude": {"bin": str(fake_claude)}}}))
    monkeypatch.setitem(greenlight.CONFIG.setdefault("harnesses", {}), "claude", {"bin": str(fake_claude)})
    monkeypatch.setitem(harness.CONFIG.setdefault("harnesses", {}), "claude", {"bin": str(fake_claude)})

    seen: list[dict] = []
    reply: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            code, out = reply["fn"](self.path, body)
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    def configure(**c):
        d = json.loads((gl_home / "config.json").read_text())
        d["system_one"] = {"url": f"{url}/v1/systemone", "anthropic_url": f"{url}/v1/messages", "timeout": 5, **c}
        (gl_home / "config.json").write_text(json.dumps(d))
        (gl_home / "decide-cache.json").unlink(missing_ok=True)
        seen.clear()

    def jev_answers(fn):
        reply["fn"] = lambda path, body: (
            200,
            {"model": "jev-1", "usage": {"input_tokens": 1, "output_tokens": 1}, "answers": {k: fn(q) for k, q in body["questions"].items()}},
        )

    env = types.SimpleNamespace(
        home=gl_home,
        url=url,
        seen=seen,
        reply=reply,
        fake_claude=fake_claude,
        configure=configure,
        jev_answers=jev_answers,
    )
    try:
        yield env
    finally:
        srv.shutdown()
        srv.server_close()


def test_nonfinite_provider_answer_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            decide.finish({"type": "bool"}, bad, "jev")


def test_local_provider_defaults_and_validation(decide_env):
    assert decide.configured() == "local" and decide.chain() == ["local"]
    assert decide.chain({"provider": "local", "fallback": "haiku"}) == ["local"]
    assert decide.chain({"provider": "jev", "fallback": "jev"}) == ["jev", "local"]
    b = decide.bool("anything", "Is it?")
    assert b == {"value": False, "prob": 0.5, "provider": "local"}, b
    c = decide.choice("x", "Which?", ["a", "b", "c", "d"])
    assert c["choice"] == "a" and c["index"] == 0 and c["confidence"] == 0.25 and set(c["probs"]) == {"a", "b", "c", "d"} and c["provider"] == "local"
    s = decide.score("x", "How good?")
    assert s["score"] == 0.5 and s["provider"] == "local"
    bt = decide.batch("x", {"a": {"question": "yes?"}, "b": {"question": "which?", "options": ["p", "q"]}, "c": {"rubric": ["bad", "ok", "good"]}})
    assert set(bt) == {"a", "b", "c"} and "prob" in bt["a"] and "choice" in bt["b"] and "score" in bt["c"]
    assert decide.batch("x", {}) == {}
    for bad in ({"type": "nope"}, {"options": ["one"]}, {"rubric": ["one"]}):
        with pytest.raises(ValueError):
            decide.norm_q(bad)
    assert not (decide_env.home / "decide-cache.json").exists()


def test_jev_provider_requests_cache_and_errors(decide_env, monkeypatch):
    decide_env.configure(provider="jev")
    assert decide.batch("x", {"q": {"question": "?"}})["q"]["error"].startswith("jev: no TypeSafe key")

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    decide_env.jev_answers(
        lambda q: {
            "noul": {"type": "noul", "noul": 0.91},
            "choice": {"type": "choice", "choice": "b", "confidence": 0.7, "probabilities": {"a": 0.1, "b": 0.7, "c": 0.2}},
            "score": {"type": "score", "score": 3.0, "confidence": 0.6, "probabilities": {"0": 0, "1": 0, "2": 0.2, "3": 0.6, "4": 0.2}},
        }[q["type"]]
    )
    r = decide.bool("I was charged twice", "Is this about billing?", context="a support inbox")
    assert r == {"value": True, "prob": 0.91, "provider": "jev"}, r
    req = decide_env.seen[-1]
    assert req["path"] == "/v1/systemone" and req["headers"]["authorization"] == "Bearer ts-test"
    assert req["body"]["model"] == "jev-latest" and req["body"]["state"] == "I was charged twice"
    q = req["body"]["questions"]["q0"]
    assert q["type"] == "noul" and q["instructions"] == {"question": "Is this about billing?", "context": "a support inbox"}

    r = decide.choice("s", "Which?", {"a": "first", "b": "second", "c": ""})
    assert r["choice"] == "b" and r["index"] == 1 and r["confidence"] == 0.7 and r["provider"] == "jev"
    assert decide_env.seen[-1]["body"]["questions"]["q0"]["criteria"] == {"a": "first", "b": "second", "c": "c"}

    r = decide.score("s", "How finished is it?")
    assert r["score"] == 0.75 and r["provider"] == "jev", r
    assert decide_env.seen[-1]["body"]["questions"]["q0"]["criteria"] == decide.LEVELS
    n = len(decide_env.seen)
    decide.score("s", "How finished is it?")
    assert len(decide_env.seen) == n  # cached

    q_norm = {"q": decide.norm_q({"question": "Hold?"})}
    c = decide.conf()
    assert decide._cache_key("jev", c, "same", q_norm) != decide._cache_key("jev", {**c, "url": decide_env.url + "/new"}, "same", q_norm)

    env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
    writers = [
        subprocess.Popen([sys.executable, "-c", "import decide,sys; decide._cache_put(sys.argv[1], {'v': sys.argv[1]})", f"parallel-{i}"], env=env)
        for i in range(12)
    ]
    assert all(p.wait() == 0 for p in writers)
    assert all(decide._cache_get(f"parallel-{i}") == {"v": f"parallel-{i}"} for i in range(12))

    r = decide.batch("s", {"weird name/with:chars": {"question": "?"}, "two": {"options": ["a", "b", "c"], "question": "?"}})
    assert r["weird name/with:chars"]["prob"] == 0.91 and r["two"]["choice"] == "b" and len(decide_env.seen) == n + 1

    decide.bool("x" * 30000, "long?")
    assert decide_env.seen[-1]["headers"].get("typesafe-danger-zone-long-context") == "true"

    decide_env.reply["fn"] = lambda path, body: (200, {"answers": {}})
    assert decide.bool("new state 1", "?")["error"] == "jev: jev: no answer for q"
    decide_env.reply["fn"] = lambda path, body: (401, {"detail": "bad key"})
    r = decide.bool("new state 2", "?")
    assert r["provider"] == "local" and "HTTP 401" in r["error"], r
    decide_env.reply["fn"] = lambda path, body: (200, {"answers": {"q0": {"type": "choice", "probabilities": {"a": 5, "b": -1, "zzz": 9}}}})
    r = decide.choice("new state 3", "?", ["a", "b"])
    assert r["probs"] == {"a": 1.0, "b": 0.0} and r["choice"] == "a", r
    decide_env.reply["fn"] = lambda path, body: (200, {"answers": {"q0": {"type": "choice", "probabilities": {}}}})
    assert decide.choice("new state 4", "?", ["a", "b"])["provider"] == "local"
    decide_env.reply["fn"] = lambda path, body: (200, {"answers": {"q0": {"type": "choice", "probabilities": {"finished": 0.2}}}})
    assert decide.choice("new state 5", "State?", ["working", "finished"])["provider"] == "local"


def test_jev_key_file_permissions_rotation_and_env_override(decide_env, monkeypatch):
    decide_env.configure(provider="jev")
    real_subproc_run = subprocess.run
    spawned = []

    def spy_run(cmd, *a, **kw):
        spawned.append(cmd)
        return real_subproc_run(cmd, *a, **kw)

    with mock.patch.object(decide.subprocess, "run", side_effect=spy_run):
        assert decide.jev_key() is None and not any(c and c[0] == "security" for c in spawned)

    decide.set_key("  file-key \n")
    f = decide_env.home / "secrets" / "typesafe"
    assert decide.jev_key() == "file-key" and stat.S_IMODE(f.stat().st_mode) == 0o600
    decide.set_key("new-key")
    assert decide.jev_key() == "new-key"
    decide_env.jev_answers(lambda q: {"type": "noul", "noul": 0.9})
    assert decide.bool("cached before clear", "Is it?")["provider"] == "jev"
    cached_calls = len(decide_env.seen)
    decide.set_key("")
    assert decide.jev_key() is None and f.exists() and f.read_text() == "" and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert decide.bool("cached before clear", "Is it?")["provider"] == "local" and len(decide_env.seen) == cached_calls
    decide.set_key("rotated-key")
    assert decide.bool("cached before clear", "Is it?")["provider"] == "jev" and len(decide_env.seen) == cached_calls + 1

    env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
    writers = [subprocess.Popen([sys.executable, "-c", "import decide,sys; decide.set_key(sys.argv[1])", f"key-{i}"], env=env) for i in range(12)]
    assert all(p.wait() == 0 for p in writers)
    assert f.read_text().strip() in {f"key-{i}" for i in range(12)} and stat.S_IMODE(f.stat().st_mode) == 0o600
    decide.set_key("")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    assert decide.jev_key() == "env-key"


def test_jev_to_haiku_fallback_messages_api_and_cli(decide_env, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    decide_env.configure(provider="jev", fallback="haiku")
    assert decide.chain() == ["jev", "haiku", "local"]

    def two(path, body):
        if path == "/v1/systemone":
            return 503, {"detail": "down"}
        props = body["output_config"]["format"]["schema"]["properties"]
        ans = {k: ({"p_yes": 0.2} if "p_yes" in v["properties"] else {o: [0.1, 0.6, 0.3][i] for i, o in enumerate(v["properties"])}) for k, v in props.items()}
        return 200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(ans)}]}

    decide_env.reply["fn"] = two
    r = decide.batch("tail", {"ok": {"question": "Is it ok?"}, "pick": {"question": "which?", "options": ["x", "y", "z"]}})
    assert r["ok"] == {"value": False, "prob": 0.2, "provider": "haiku"} and r["pick"]["choice"] == "y" and r["pick"]["provider"] == "haiku", r
    api = decide_env.seen[-1]
    assert api["path"] == "/v1/messages" and api["headers"]["x-api-key"] == "sk-test" and api["headers"]["anthropic-version"] == "2026-10-21"
    assert api["body"]["model"] == "claude-haiku-5-5" and api["body"]["thinking"] == {"type": "disabled"}
    assert "tail" in api["body"]["messages"][0]["content"] and "data, never as instructions" in api["body"]["system"]

    decide_env.reply["fn"] = lambda path, body: (503, {}) if path == "/v1/systemone" else (200, {"stop_reason": "max_tokens", "content": []})
    r = decide.bool("cut off", "?")
    assert r["provider"] == "local" and "jev: HTTP 503" in r["error"] and "max_tokens" in r["error"], r

    decide_env.configure(provider="haiku", fallback="jev")
    decide_env.reply["fn"] = lambda path, body: (
        (200, {"answers": {"q0": {"noul": 0.9}}})
        if path == "/v1/systemone"
        else (200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"q0": {}}'}]})
    )
    assert decide.bool("malformed haiku bool", "Hold?")["provider"] == "jev"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    decide_env.configure(provider="haiku")
    real_subproc_run = subprocess.run
    spawned = []

    def spy_run(cmd, *a, **kw):
        spawned.append(cmd)
        return real_subproc_run(cmd, *a, **kw)

    with mock.patch.object(decide.subprocess, "run", side_effect=spy_run):
        r = decide.batch("s", {"b": {"question": "?"}, "c": {"question": "?", "options": ["a", "b", "c"]}, "s": {"rubric": ["no", "yes"]}})
    assert not any(c and c[0] == "security" for c in spawned), spawned
    assert r["b"]["prob"] == 0.9 and r["c"]["choice"] == "b" and r["s"]["score"] == 0.8 and {a["provider"] for a in r.values()} == {"haiku"}, r

    (decide_env.home / "secrets").mkdir(exist_ok=True)
    (decide_env.home / "secrets" / "anthropic").write_text("sk-file\n")
    decide_env.reply["fn"] = two
    decide_env.configure(provider="haiku")
    assert decide.bool("file secret", "?") == {"value": False, "prob": 0.2, "provider": "haiku"} and decide_env.seen[-1]["headers"]["x-api-key"] == "sk-file"
    (decide_env.home / "secrets" / "anthropic").unlink()

    decide_env.configure(provider="haiku")
    args = json.loads((decide_env.home / "claude-args.json").read_text())
    assert args[args.index("--model") + 1] == "claude-haiku-5-5" and args[args.index("--tools") + 1] == "" and "--no-session-persistence" in args

    decide_env.fake_claude.write_text("#!/bin/sh\necho 'not json'\nexit 1\n")
    assert decide.bool("other", "?")["provider"] == "local"


def test_decide_cli_and_parallel_config_saves(decide_env, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    decide_env.configure(provider="local")
    decide_env.reply["fn"] = lambda path, body: (503, {})

    def cli(*a, stdin=""):
        out = io.StringIO()
        with monkeypatch.context() as mp, contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            mp.setattr(sys, "stdin", io.StringIO(stdin))
            code = decide.main(list(a))
        return code, out.getvalue()

    assert json.loads(cli("bool", "q?", "--state", "s")[1])["provider"] == "local"
    assert json.loads(cli("choice", "q?", "a", "b", stdin="state")[1])["probs"] == {"a": 0.5, "b": 0.5}
    assert cli("choice", "q?", "a")[0] == 2
    assert "score" in json.loads(cli("score", "bad", "good", "--state", "s")[1])
    assert set(json.loads(cli("batch", stdin=json.dumps({"state": "s", "questions": {"x": {"question": "?"}}}))[1])) == {"x"}
    code, out = cli("config", "--provider", "jev", "--fallback", "haiku")
    assert code == 0 and json.loads(out)["chain"] == ["jev", "haiku", "local"] and json.loads(out)["key"] is True
    assert json.loads((decide_env.home / "config.json").read_text())["harnesses"]
    assert json.loads(cli("config", "--fallback", "none")[1])["chain"] == ["jev", "local"]
    assert cli("config", "--provider", "gpt")[0] == 2
    assert cli("test")[0] == 1  # jev is set but its fake answers aren't right
    decide_env.configure(provider="local")
    assert cli("test")[0] == 0

    for _ in range(3):
        decide_env.configure(provider="local")
        saves = [
            subprocess.Popen([sys.executable, str(Path(decide.__file__)), "config", flag, value], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            for flag, value in (("--provider", "jev"), ("--fallback", "haiku"))
        ]
        assert all(p.communicate(timeout=5) and p.returncode == 0 for p in saves)
        assert decide.chain() == ["jev", "haiku", "local"]


def test_terminal_screen_state_patterns_and_drafts(decide_env):
    decide_env.configure(provider="local")
    screens = [
        ("claude", ["● Done, tests pass.", "", "╭──╮", "❯ ", "╰──╯"], "finished"),
        ("claude", ["✶ Skedaddling… (3s · esc to interrupt)", "❯ "], "working"),
        ("claude", ["Do you want to proceed?", "❯ 1. Yes", "  2. No"], "waiting_for_user"),
        ("claude", ["API Error: 401 OAuth access token has been revoked", "❯ "], "logged_out"),
        ("claude", ["You've hit your usage limit · resets 5pm", "❯ "], "rate_limited"),
        ("claude", ["❯ fix the tests please"], "unsent_draft"),
        ("claude", ["Done. Changes:", "1. Fixed the parser", "2. Added a test", "❯ "], "finished"),
        ("claude", ['⏺ Background command "npm test" completed (exit code 0)', "", "❯ "], "finished"),
        ("codex", ["• Working (12s • esc to interrupt)", "› Ask Codex to do anything"], "working"),
        ("codex", ["• Working (12s • esc to interrupt)", "› Ask Codex to do anything", "? for shortcuts"], "working"),
        ("codex", ["› Implement the thing"], "unsent_draft"),
        ("codex", ["› Ask Codex to do anything"], "finished"),
        ("codex", ["› Ask anything about auth"], "unsent_draft"),
        ("codex", ["Allow command?", "› 1. Yes, proceed", "  2. No"], "waiting_for_user"),
        ("codex", ["Choose a branch:", "1. main", "2. feature", "› "], "waiting_for_user"),
        ("codex", ["■ You've hit your usage limit. Try again in 3 hours.", "› "], "rate_limited"),
        ("codex", ["Not logged in. Run `codex login` to sign in.", "› "], "logged_out"),
        ("codex", ["• Working (3s • esc to interrupt)", "■ You've hit your usage limit.", "› "], "rate_limited"),
        ("claude", ["API Error: 401 Unauthorized", "✶ Retrying… (2s · esc to interrupt)"], "working"),
        ("claude", ["✶ Earlier… (3s · esc to interrupt)"] + [f"output {i}" for i in range(12)] + ["❯ "], "finished"),
        ("claude", ["✶ Working… (3s · esc to interrupt)", "● Done, tests pass.", "❯ "], "finished"),
        ("claude", ["Do you want to proceed?", "❯ 1. Yes", "  2. No", "User selected Yes.", "● Done.", "❯ "], "finished"),
        ("codex", ["Allow command?", "› 1. Yes", "  2. No", "Command allowed.", "Done.", "› Ask Codex to do anything"], "finished"),
        ("claude", ["Do you want to proceed?", "❯ 1. Yes", "Done.", "─" * 30, "❯", "─" * 30], "finished"),
        ("gemini", ["Allow execution?", "● 1. Yes", "Done.", "╭────╮", "│ > Type your message │", "╰────╯"], "finished"),
        ("codex", ["Not logged in. Run `codex login`.", "Successfully logged in", "Done.", "› "], "finished"),
        ("pi", ["Error: 429 Too Many Requests", "Retry succeeded", "Task completed", ">"], "finished"),
        ("pi", ["429 Too Many Requests", "Request retried and completed", "Tests passed", "❯ "], "finished"),
        ("codex", ["API Error: 401 Unauthorized", "Fallback token accepted", "Changes complete.", "› "], "finished"),
        ("codex", ["E AssertionError: expected HTTP 401, got HTTP 403", "1 failed in 0.05s", "› "], "finished"),
        ("codex", ["GET /api: HTTP 401 Unauthorized", "› "], "finished"),
        ("codex", ["Error: 401 Unauthorized", "› "], "finished"),
        ("codex", ["pytest tests/test_auth.py", "Error: 401 Unauthorized", "› "], "finished"),
        ("codex", ["curl localhost:8080", "Error: 429 Too Many Requests", "› "], "finished"),
        ("codex", ["HTTP 401 Unauthorized", "› "], "finished"),
        ("codex", ["Please log in to GitHub to open the PR.", "› "], "finished"),
        ("codex", ["You are not logged in to GitHub; please run gh auth login.", "› "], "finished"),
        ("codex", ["Fixed the app's API Error: 401 response.", "› "], "finished"),
        ("codex", ["Updated the message ‘OAuth access token has been revoked’", "› "], "finished"),
        ("codex", ["Fixed the app's 429 Too Many Requests response.", "› "], "finished"),
        ("codex", ["API Error: 401", "Retried with refreshed credentials", "Wrote src/app.py", "Done.", "› "], "finished"),
        ("codex", ["API Error: 401", "Reconnected", "Finished the task", "› "], "finished"),
        ("codex", ["API Error: 401", "Reconnected", "I fixed the bug and the tests are green.", "› "], "finished"),
        ("codex", ["API Error: 401", "Reconnected", "› "], "finished"),
        ("codex", ["Error: 429 Too Many Requests", "Reconnected", "Finished the task", "› "], "finished"),
        ("codex", ["API Error: 401", "Retried with refreshed credentials", "› "], "finished"),
        ("codex", ["API Error: 401 Unauthorized"] + [f"Traceback detail {i}: auth request failed" for i in range(9)] + ["› "], "finished"),
        ("codex", ["Error: 429 Too Many Requests"] + [f"Traceback detail {i}: quota request failed" for i in range(9)] + ["› "], "finished"),
        ("codex", ["Changes complete.", "The button now says Allow this action.", "› "], "finished"),
        ("codex", ["Allow command?", "› 1. Yes", "  2. No", "API Error: 401 Unauthorized"], "finished"),
        ("codex", ["Allow command?", "› 1. Yes", "  2. No", "You've hit your usage limit"], "rate_limited"),
        ("codex", ["API Error: 401 Unauthorized", "Error: 429 Too Many Requests: rate limit exceeded", "› "], "finished"),
        ("claude", ["Do you want to proceed?", "❯ 1. Yes", "  2. No", "❯ fix the tests please"], "unsent_draft"),
        ("codex", ["Allow command?", "› 1. Yes", "  2. No", "Esc to cancel", "› "], "waiting_for_user"),
        ("gemini", ["Allow execution of: rm -rf build?", "● 1. Yes, allow once", "  2. No"], "waiting_for_user"),
        ("gemini", ["╭────╮", "│ > Type your message or @path/to/file │", "╰────╯"], "finished"),
        ("gemini", ["│ > draft here │"], "unsent_draft"),
        ("gemini", ["✕ [API Error: You have exhausted your daily quota on this model.]", "│ > Type your message │"], "rate_limited"),
        ("gemini", ["⠼ Thinking about it (esc to cancel, 4s)"], "working"),
        ("opencode", ["┃ Build  claude-haiku", "  ⠋ Working  esc interrupt"], "working"),
        ("opencode", ["Error: ENOENT cannot open file foo.txt, retrying"] * 6 + ["┃ "], "stuck_looping"),
        ("codex", ["Error: ENOENT cannot open file foo.txt, retrying"] * 5 + ["› Ask Codex to do anything"], "stuck_looping"),
        ("opencode", ["Error: ENOENT cannot open file foo.txt, retrying"] * 5 + ["Recovered; tests pass.", "┃ "], "finished"),
        ("codex", ["$ npm test", "Error: ENOENT cannot open package.json; retrying test command"] * 5 + ["› "], "stuck_looping"),
        ("pi", [f"Downloading file {i} of 5" for i in range(1, 6)] + [">"], "finished"),
        ("pi", [f"Wrote generated artifact {i}" for i in range(101, 106)] + [">"], "finished"),
        ("opencode", ["Allow command?", "● 1. Yes", "User selected Yes.", "Done.", "┃"], "finished"),
        ("opencode", ["Allow command?", "● 1. Yes", "Wrote file", "┃"], "finished"),
        ("opencode", ["Allow command?", "● 1. Yes", "Wrote file"], "finished"),
        ("opencode", ["PASS integration test suite A"] * 5 + ["┃"], "finished"),
        ("pi", ["Error: 429 Too Many Requests: rate limit exceeded", ">"], "finished"),
        (
            "claude",
            ["Posted the replies:"]
            + [f"Comment: https://forge.example/acme/widgets/-/merge_requests/{i}#note_{i}7" for i in range(5)]
            + ["✻ Brewed for 39s · done 6:45 PM", "─" * 40, "❯", "─" * 40],
            "finished",
        ),
        ("pi", ["Session restored from ~/.pi/sessions/abc", "Ready.", ">"], "finished"),
    ]
    for agent, lines, want in screens:
        got = ade_common.agent_state(lines, agent)
        assert got["state"] == want and got["provider"] == "local", (agent, lines, got)
        assert abs(sum(got["probs"].values()) - 1) < 0.01 and set(got["probs"]) == set(ade_common.STATES)

    assert ade_common.agent_state(["❯ half typed"])["draft"] == "half typed"
    assert ade_common.draft(["❯ fix CI", "", "╰──╯"]) == "fix CI"
    assert ade_common.draft(["› Ask anything about auth"]) == "Ask anything about auth"
    assert ade_common.draft(["❯ fix tests please", "✶ Working… (3s · esc to interrupt)", "PASS test_foo"]) == ""
    assert ade_common.draft(["❯ fix tests", "  PASS test_foo"]) == "fix tests\nPASS test_foo"
    assert ade_common.draft(["❯ fix the parser", "  Changes complete."]) == "fix the parser\nChanges complete."
    assert ade_common.draft(["❯ Fix CI", "  Running tests before merging is required"]) == "Fix CI\nRunning tests before merging is required"
    assert ade_common.draft(["› Do not merge yet", "? for shortcuts"]) == "Do not merge yet"
    assert ade_common.draft(["› Do not merge yet", "  ? for shortcuts"]) == "Do not merge yet"
    assert ade_common.screen_state(["› Do not merge yet", "? for shortcuts"])["state"] == "unsent_draft"
    assert ade_common.draft(["❯ fix the parser", "  and update the tests"]) == "fix the parser\nand update the tests"
    assert ade_common.draft(["│ >             │", "│ Please fix CI │", "╰────╯"]) == "Please fix CI"
    assert ade_common.screen_state(["│ >             │", "│ Please fix CI │", "╰────╯"])["state"] == "unsent_draft"
    assert ade_common.agent_state([])["state"] == "finished"
    assert not greenlight.LIMIT_RE.search("agent started\nError: 429 Too Many Requests")
    assert ade_common.screen_state(["Which branch should I use?", "› "])["state"] == "waiting_for_user"
    assert ade_common.screen_state(["Please choose a branch before I continue?", "› "])["state"] == "waiting_for_user"
    assert ade_common.screen_state(["Which branch should I use?", "Done.", "› "])["state"] == "finished"
    assert ade_common.screen_state(["Please choose a branch before I continue?", "Done.", "› "])["state"] == "finished"
    assert ade_common.agent_state(["\x1b[1m✶ Skedaddling…\x1b[0m (3s · esc to interrupt)"])["state"] == "working"
    assert ade_common.draft(["│ > 1. Yes │"]) == ""
    assert ade_common.screen_state(["❯ x"])["prob"] > greenlight.SURE > ade_common.screen_state(["done", "❯ "])["prob"]


def test_terminal_screen_state_with_jev_model(decide_env, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    decide_env.configure(provider="jev")
    decide_env.jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.9 if k == "stuck_looping" else 0.1 / 6) for k in q["criteria"]}})
    long = [f"line {i}" for i in range(200)]
    got = ade_common.agent_state([*long, "", ""], "codex")
    assert got == {"state": "stuck_looping", "prob": 0.9, "probs": got["probs"], "provider": "jev"}, got
    sent = decide_env.seen[-1]["body"]
    assert sent["state"].splitlines() == long[-60:]
    assert set(sent["questions"]["q0"]["criteria"]) == set(ade_common.STATES) and "codex" in sent["questions"]["q0"]["instructions"]["context"]
    decide_env.reply["fn"] = lambda path, body: (500, {})
    assert ade_common.agent_state(["❯ "], "claude")["provider"] == "local"


def test_goal_intents_and_natural_dates(decide_env, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    u = lambda n: {"username": n}
    mr = lambda **kw: {
        "iid": 1,
        "state": "opened",
        "author": u("octo"),
        "draft": False,
        "has_conflicts": False,
        "assignees": [],
        "reviewers": [],
        "head_pipeline": {"status": "success", "web_url": "u"},
        "detailed_merge_status": "mergeable",
        "_discussions": [],
        "_approved_by": ["alice"],
        "title": "Add a widget",
    } | kw
    ws = lambda goal: {"project": "acme/widgets", "goal": goal, "comment": goal, "branch": "b"}

    def nxt(m, w):
        r = greenlight.evaluate(w, m, None)
        return (r["step"], r["owner"], r["action"])

    decide_env.configure(provider="local")
    assert greenlight.intents(ws("Merge !1: Add a widget (hold)"), mr())["is_held"]
    assert not greenlight.intents(ws("Merge !1: Add a widget, but wait for Bob's review first"), mr())["is_held"]
    assert greenlight.intents(ws("x (revisit 2099-01-02)"), mr())["revisit_date"] == "2099-01-02"
    assert nxt(mr(), ws("x (revisit 2099-01-02)"))[0] == "parked"
    assert nxt(mr(), ws("x (parked)"))[1] == "parked"

    decide_env.configure(provider="jev")
    probs: dict[str, float] = {}
    decide_env.jev_answers(lambda q: {"type": "noul", "noul": probs.get(next(k for k, (t, _) in greenlight.INTENTS.items() if t == _ins(q)), 0.0)})

    probs.update(is_held=0.92)
    g = ws("Merge !1: Add a widget, but wait for Bob's review first")
    assert nxt(mr(), g)[:2] == ("merge", "me")
    probs.update(is_held=0.0)
    assert nxt(mr(), ws("Merge !1: Add a widget (hold until 3.2)"))[:2] == ("merge", "me")
    asked = {_ins(q) for q in decide_env.seen[-1]["body"]["questions"].values()}
    assert greenlight.INTENTS["is_held"][0] not in asked
    probs.update(is_held=0.59)
    assert nxt(mr(), ws("Merge !1: Add a widget soonish"))[:2] == ("merge", "ci")
    probs.update(is_held=0, stay_draft=0.8)
    assert nxt(mr(draft=True), ws("Merge !1: Add a widget, leave it in draft for now"))[:2] == ("ready", "me")
    probs.update(stay_draft=0, is_parked=0.8)
    assert nxt(mr(), ws("Merge !1: Add a widget; maybe pause this"))[1] != "parked"
    probs.update(is_parked=0.95)
    assert nxt(mr(), ws("Merge !1: Add a widget; on ice for now"))[1] == "parked"
    assert nxt(mr(state="merged"), ws("Merge !1: Add a widget; on ice for now"))[1] == "done"
    probs.update(is_parked=0, revisit=0.9)
    step = nxt(mr(), ws("Merge !1: Add a widget, come back to it on Dec 3 2099"))
    assert step[:2] == ("parked", "parked") and step[2] == "parked until 2099-12-03", step
    assert nxt(mr(), ws("Merge !1: Add a widget, come back to it some day"))[1] == "ci"
    assert greenlight.intents(ws("Finish by 2026-11-21; come back to this on 2026-12-03"), mr())["revisit_date"] == "2026-12-03"
    assert greenlight.intents(ws("Finish by 2026-11-21 and come back to this on Dec 3, 2099"), mr())["revisit_date"] == "2099-12-03"
    assert greenlight.intents(ws("Shipped 2026-10-01; revisit Dec 3"), mr())["revisit_date"] != "2026-10-01"
    assert greenlight.intents(ws("Start after Nov 21; revisit Dec 3"), mr())["revisit_date"] == "2026-12-03"
    assert greenlight.intents(ws("Ship on Dec 1 and revisit two weeks later"), mr())["revisit_date"] is None
    assert greenlight.intents(ws("Revisit next Friday after the 2026-11-21 release"), mr())["revisit_date"] == "2026-11-27"
    assert greenlight.intents(ws("Revisit this after CI goes green, and have the release ready by December 3, 2099"), mr())["revisit_date"] is None

    probs.update(is_parked=0.95)
    both = greenlight.intents(ws("Pause this work until Dec 3, 2099"), mr())
    assert both["revisit_date"] == "2099-12-03" and not both["is_parked"]
    probs.update(is_parked=0.9, revisit=0.69)
    weak_date = greenlight.intents(ws("Pause this work until Dec 3, 2099"), mr())
    assert weak_date["revisit_date"] == "2099-12-03" and not weak_date["is_parked"], weak_date
    probs.update(is_parked=0.95, revisit=0.9)
    status = greenlight.intents(ws("Park this indefinitely; revisit its status on Dec 3, 2099"), mr())
    assert status["revisit_date"] == "2099-12-03" and status["is_parked"]
    assert nxt(mr(), ws("Park this indefinitely; revisit its status on Dec 3, 2099"))[:2] == ("parked", "parked")
    assert greenlight.evaluate(ws("Park this indefinitely; revisit its status on Dec 3, 2099"), mr(), None).get("hidden")

    for goal in (
        "Park this until Bob approves; revisit its status on Dec 3, 2099",
        "Pause this work until CI is green; revisit its status on Dec 3, 2099",
        "Park this until Bob approves; revisit on Dec 3, 2099 to check status",
        "Park this; revisit on Dec 3, 2099 to check status",
        "Pause this until CI is green; revisit on Dec 3, 2099",
    ):
        status = greenlight.intents(ws(goal), mr())
        assert status["revisit_date"] == "2099-12-03" and status["is_parked"], status

    status = greenlight.intents(ws("Park this until Bob approves; come back tomorrow to check progress"), mr())
    assert status["revisit_date"] and status["is_parked"], status
    abbrev = greenlight.intents(ws("Pause this work until Dec. 3, 2099"), mr())
    assert abbrev["revisit_date"] == "2099-12-03" and not abbrev["is_parked"], abbrev
    assert greenlight.work_resumes_on("Pause this work until Dec. 3, 2099") == "2099-12-03"
    assert greenlight.work_resumes_on("Pause this work until 3rd January") is not None
    after = greenlight.intents(ws("Pause this work until after Dec 3, 2099"), mr())
    assert after["revisit_date"] == "2099-12-04" and not after["is_parked"], after
    aside = greenlight.intents(ws("Set this aside until Dec 3, 2099"), mr())
    assert aside["revisit_date"] == "2099-12-03" and not aside["is_parked"], aside
    assert nxt(mr(), ws("Set this aside until Dec 3, 2099"))[0] == "parked"

    for phrase in (
        "Don't work on this until Dec 3, 2099",
        "Don’t work on this until Dec 3, 2099",
        "Do not work on this until Dec 3, 2099",
    ):
        paused = ws(phrase)
        result = greenlight.intents(paused, mr())
        assert result["revisit_date"] == "2099-12-03" and not result["is_parked"], (phrase, result)
        assert paused["_revisit"]["resumes_work"] and nxt(mr(), ws(phrase))[0] == "parked"

    probs.update(is_parked=0.95, revisit=0.69)
    weak_pause = greenlight.intents(ws("Don't work on this until Dec 3, 2099"), mr())
    assert weak_pause["revisit_date"] == "2099-12-03" and not weak_pause["is_parked"], weak_pause
    probs.update(is_parked=0.95, revisit=0.9)
    elapsed_pause = greenlight.intents(ws("Don't work on this until 2020-12-03"), mr())
    assert not elapsed_pause["is_parked"] and nxt(mr(), ws("Don't work on this until 2020-12-03"))[0] != "parked"
    mixed = greenlight.intents(ws("Park this work until Bob approves; hold merge until Dec 3, 2099"), mr())
    assert mixed["is_parked"] and mixed["revisit_date"] == "2099-12-03", mixed
    assert greenlight.work_resumes_on("Pause this work; come back tomorrow") == greenlight.when("tomorrow")

    dated_ws = ws("Pause this work; come back tomorrow")
    dated = greenlight.intents(dated_ws, mr())
    assert dated["revisit_date"] and not dated["is_parked"], dated
    assert dated_ws["_revisit"]["resumes_work"]
    next_day = ws("Pause this work; come back tomorrow")
    next_day["_revisit"] = dated_ws["_revisit"]
    with monkeypatch.context() as mp:
        mp.setattr(greenlight, "when", lambda text, today=None: "2099-12-31")
        assert not greenlight.intents(next_day, mr())["is_parked"]

    probs.update(is_parked=0)
    active = ws("Finish implementation now; revisit its status on Dec 3, 2099")
    assert greenlight.intents(active, mr())["revisit_date"] == "2099-12-03"
    assert nxt(mr(), ws(active["goal"]))[0] != "parked"
    assert nxt(mr(), ws("Revisit Dec 3, 2099"))[0] == "parked"

    first = ws("come back tomorrow")
    first_date = greenlight.intents(first, mr())["revisit_date"]
    assert first_date and first["_revisit"] == {"text": first["goal"], "date": first_date, "resumes_work": True}
    second = ws(first["goal"])
    second["_revisit"] = first["_revisit"]
    with monkeypatch.context() as mp:
        mp.setattr(greenlight, "when", lambda text, today=None: "2099-12-31")
        assert greenlight.intents(second, mr())["revisit_date"] == first_date

    probs.clear()
    assert nxt(mr(), ws("Merge !1: Add a widget"))[1] == "ci"
    t = "Add a widget, hold the line"
    assert greenlight.intents(ws(f"Merge !1: {t}"), mr(title=t)) == {"is_held": False, "stay_draft": False, "is_parked": False, "revisit_date": None}
    assert t not in decide_env.seen[-1]["body"]["state"]

    decide_env.configure(provider="local")
    off = ws(first["goal"])
    off["_revisit"] = first["_revisit"]
    assert greenlight.intents(off, mr())["revisit_date"] is None and "_revisit" not in off

    # when(): natural dates
    d0 = dt.date(2026, 11, 20)  # a Friday
    for text, want in [
        ("revisit 2026-12-01", "2026-12-01"),
        ("on Dec 3", "2026-12-03"),
        ("after 3rd January", "2027-01-04"),
        ("march 5, 2027", "2027-03-05"),
        ("tomorrow", "2026-11-21"),
        ("in 2 weeks", "2026-12-04"),
        ("in a day", "2026-11-21"),
        ("next week", "2026-11-23"),
        ("until monday", "2026-11-23"),
        ("next friday", "2026-11-27"),
        ("feb 30", None),
        ("soon", None),
    ]:
        assert greenlight.when(text, d0) == want, (text, greenlight.when(text, d0))
    assert greenlight.when("revisit Feb 29", dt.date(2024, 3, 1)) == "2028-02-29"
    assert greenlight.when("come back in 3 months", d0) == "2027-02-20"
    assert greenlight.when("next Friday after the 2026-11-21 release", d0) == "2026-11-27"
    assert greenlight.when("revisit this next Friday after Dec 3, 2099", d0) == "2099-12-04"
    assert greenlight.work_resumes_on("Pause work; revisit this next Friday after Dec 3, 2099") == "2099-12-04"


def test_agent_overlay_local_and_model(decide_env, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    decide_env.configure(provider="local")
    tails: dict[str, list[str]] = {}
    monkeypatch.setattr(greenlight, "orca", lambda *a, **k: {"terminal": {"tail": tails["other"] if "other" in a else tails["t"]}})
    greenlight.CONFIG["agents"] = ["claude", "codex", "gemini"]
    terms = {"w": [{"handle": "term-1", "agentIdentity": "codex", "lastOutputAt": 1}]}

    def overlay(tail, state="done", owner="agent", typ="codex", action="Fix CI"):
        tails["t"] = tail
        if terms.get("w"):
            terms["w"][0]["agentIdentity"] = typ
        pane = terms.get("w", [{}])[0]
        key = f"{pane['tabId']}:{pane['leafId']}" if pane.get("tabId") and pane.get("leafId") else None
        w = {"id": "w", "next": {"owner": owner, "action": action}, "agent": {"state": state, "type": typ, "pane_key": key, "last": "Which one?"}}
        greenlight.agent_overlay(w, terms)
        return w

    w = overlay(["Not logged in. Run `codex login`.", "› "])
    assert w["next"]["owner"] == "me" and "logged out" in w["next"]["action"] and "term-1" in w["next"]["action"]

    terms["w"].append({"handle": "other", "agentIdentity": "claude", "lastOutputAt": 2})
    tails["other"] = ["API Error: 401 Unauthorized"]
    w = overlay(["All done.", "› "])
    assert w["next"]["owner"] == "agent" and w["agent"]["terminal"] == "term-1"
    terms["w"].pop()

    terms["w"][0].update(tabId="tab-1", leafId="leaf-1")
    terms["w"].append({"handle": "other", "agentIdentity": "codex", "tabId": "tab-2", "leafId": "leaf-2", "lastOutputAt": 3})
    tails["t"] = ["All done.", "› "]
    w = {"id": "w", "next": {"owner": "agent", "action": "Fix CI"}, "agent": {"state": "done", "type": "codex", "pane_key": "tab-1:leaf-1", "last": ""}}
    greenlight.agent_overlay(w, terms)
    assert w["next"]["owner"] == "agent" and w["agent"]["terminal"] == "term-1"
    terms["w"].pop()

    w = overlay(["You've hit your usage limit.", "› "])
    assert w["next"]["agent"] == "claude" and "[codex out of tokens: dispatch with claude]" in w["next"]["action"]
    w = overlay(["You've hit your usage limit.", "❯ "], typ="claude")
    assert w["next"]["agent"] == "codex"
    w = overlay(["• Working (3s • esc to interrupt)"])
    assert w["next"]["owner"] == "working"
    w = overlay(["Not logged in. Run `codex login`.", "› "], state="working")
    assert w["next"]["owner"] == "me" and "logged out" in w["next"]["action"]
    w = overlay(["You've hit your usage limit.", "› "], state="working")
    assert "out of tokens" in w["next"]["action"]
    w = overlay(["You've hit your usage limit.", "› "], state="waiting")
    assert "out of tokens" in w["next"]["action"] and w["next"]["agent"] == "claude"

    greenlight.CONFIG["agents"] = ["codex"]
    w = overlay(["You've hit your usage limit.", "› "], state="working")
    assert w["next"]["owner"] == "me" and "wait for reset" in w["next"]["action"]
    greenlight.CONFIG["agents"] = ["claude", "codex", "gemini"]

    w = overlay(["› half a brief"])
    assert w["next"]["owner"] == "me" and "unsent" in w["next"]["action"]
    w = overlay(["oops: same error again and again here"] * 6)
    assert w["next"]["owner"] == "me" and "loop" in w["next"]["action"]
    w = overlay(["All done.", "› "])
    assert w["next"] == {"owner": "agent", "action": "Fix CI"} and w["agent"]["screen"]["state"] == "finished"
    w = overlay(["Which one?", "› 1. Keep", "  2. Port"], state="waiting")
    assert w["next"]["owner"] == "me" and w["next"]["action"].startswith("agent is waiting on you")
    w = overlay(["› fix the tests"], state="waiting")
    assert w["next"]["owner"] == "me" and "draft sits unsent" in w["next"]["action"]
    w = overlay(["Which branch should I use?", "› "], state="done")
    assert w["next"]["owner"] == "me" and "waiting on you" in w["next"]["action"]
    w = overlay(["Choose a branch:", "1. main", "2. feature", "› "], state="done")
    assert w["next"]["owner"] == "me" and "waiting on you" in w["next"]["action"]
    w = overlay(["Please choose a branch before I continue?", "› "], state="done")
    assert w["next"]["owner"] == "me" and "waiting on you" in w["next"]["action"]
    w = overlay(["✶ Running tests… (40s · esc to interrupt)"], state="waiting")
    assert w["agent"]["state"] == "working" and w["next"]["owner"] == "working"
    w = overlay(["Not logged in. Run `codex login`.", "❯ "], state="waiting")
    assert "logged out" in w["next"]["action"]
    w = overlay(["Done.", "❯ "], state="waiting")
    assert w["next"]["owner"] == "me"
    w = overlay(["✶ Running tests… (40s · esc to interrupt)"], state="waiting", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "working"
    w = overlay(["✶ Running tests… (40s · esc to interrupt)"], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "working"
    w = overlay(["Done.", "› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "greenlight"

    w = {"id": "w", "next": {"owner": "greenlight", "action": "merge"}, "agent": {"state": "working", "type": "codex", "last": ""}}
    tails["t"] = ["Done.", "› "]
    greenlight.agent_overlay(w, terms)
    assert w["next"]["owner"] == "working"
    w = {"id": "w", "next": {"owner": "agent", "action": "Fix CI"}, "agent": {"state": "waiting", "type": "codex", "last": "Approve command?"}}
    tails["t"] = ["• Working (3s • esc to interrupt)"]
    greenlight.agent_overlay(w, terms)
    assert w["next"]["owner"] == "me" and w["agent"]["state"] == "waiting"

    w = overlay(["Fixed CI and updated the tests.", "› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "greenlight"
    w = overlay(["The fix is in place.", "› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "greenlight"
    w = overlay(["The fix is in place.", "› "], state="working")
    assert w["agent"]["state"] == "done" and w["next"]["owner"] == "agent"
    w = overlay(["Fixed CI.", "› ", "? for shortcuts"], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "greenlight"
    w = overlay(["Fixed CI.", "╭────╮", "│ > Type your message │", "╰────╯"], state="working", owner="greenlight", action="merge", typ="gemini")
    assert w["next"]["owner"] == "greenlight"
    w = overlay(["Fixed CI.", "┃"], state="working", owner="greenlight", action="merge", typ="opencode")
    assert w["next"]["owner"] == "greenlight"
    w = overlay(["Fixed CI.", "╭──╮", "❯", "╰──╯"], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "greenlight"
    w = overlay(["Fixed CI and updated the tests.", "› "], state="working")
    assert w["agent"]["state"] == "done" and w["next"]["owner"] == "agent"
    w = overlay(["› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "working"
    w = overlay(["Running tests...", "› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "working"
    w = overlay(["Error: 401 Unauthorized", "› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "working" and w["agent"]["screen"]["state"] == "finished"
    for tail in (["Not logged in. Run `codex login`.", "› "], ["You've hit your usage limit", "› "], ["› half typed"]):
        w = overlay(tail, state="working", owner="greenlight", action="merge")
        assert w["next"]["owner"] == "me" and w["agent"]["state"] == "done", w

    decide_env.configure(provider="jev")
    decide_env.jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.4 if k == "working" else 0.6 / 6) for k in q["criteria"]}})
    w = overlay(["Fixed CI.", "› "], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "greenlight"
    decide_env.jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.9 if k == "finished" else 0.1 / 6) for k in q["criteria"]}})
    w = overlay(["Running tests..."], state="working", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "working"
    w = overlay(["Running tests..."], state="working")
    assert w["next"]["owner"] == "working" and w["agent"]["state"] == "working"
    w = overlay(["Allow command?", "› 1. Yes", "  2. No"], state="waiting")
    assert w["next"]["owner"] == "me" and w["agent"]["screen"]["state"] == "waiting_for_user", w
    w = overlay(["• Working (3s • esc to interrupt)", "› Ask Codex to do anything"], state="working")
    assert w["next"]["owner"] == "working" and w["agent"]["screen"]["state"] == "working", w
    w = overlay(["Error: ENOENT cannot open file foo.txt, retrying"] * 5 + ["› Ask Codex to do anything"], state="working")
    assert w["agent"]["screen"]["state"] == "stuck_looping" and w["next"]["owner"] == "me", w
    for failure in ("Not logged in. Run `codex login`.", "You've hit your usage limit"):
        w = overlay([failure, "› "], state="working")
        assert w["agent"]["screen"]["state"] in ("logged_out", "rate_limited") and w["agent"]["screen"]["provider"] == "local", w
    w = overlay(["› fix the tests please"], state="working")
    assert w["next"]["owner"] == "me" and "unsent" in w["next"]["action"]

    decide_env.jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.6 if k == "finished" else 0.4 / 6) for k in q["criteria"]}})
    w = overlay(["Not logged in. Run `codex login`.", "❯ "], state="done")
    assert w["next"]["owner"] == "me" and "logged out" in w["next"]["action"]

    decide_env.jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.9 if k == "finished" else 0.1 / 6) for k in q["criteria"]}})
    w = overlay(["⏺ Background command finished", "Done.", "❯ "], state="waiting")
    assert w["agent"]["state"] == "done" and w["next"] == {"owner": "agent", "action": "Fix CI"}, w
    w = overlay(["Finished final handoff", "› "], state="working")
    assert w["agent"]["state"] == "done" and w["next"]["owner"] == "agent"
    w = overlay(["Done.", "❯ "], state="waiting", owner="greenlight", action="merge")
    assert w["next"]["owner"] == "me" and w["next"]["action"].startswith("agent is waiting on you")
    w = overlay(["x"], state="waiting", owner="ci", action="pipeline running")
    assert w["next"]["owner"] == "me"

    terms.clear()
    w = overlay(["You've hit your usage limit."])
    assert w["next"] == {"owner": "agent", "action": "Fix CI"}


def test_haiku_provider_on_codex_and_agy(decide_env, monkeypatch):
    # haiku_model may be any model: a Codex or agy one runs on that CLI, toolless, never the Messages API
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    asked = []
    def ask(prompt, model=None, effort=None, timeout=None, cwd=None, **kw):
        asked.append((model, cwd, timeout))
        props = json.loads(prompt.split("JSON schema, no prose and no code fence:\n", 1)[1].split("\n\n", 1)[0])["properties"]
        return "Sure:\n" + json.dumps({k: {"p_yes": 0.8} for k in props}) + "\n"
    monkeypatch.setattr(harness, "ask", ask)
    for model, want in (("gpt-6-luna", "codex:gpt-6-luna"), ("gemini-3.8-flash-low", "agy:gemini-3.8-flash-low"),
                        ("agy:claude-sonnet-5-5-low", "agy:claude-sonnet-5-5-low")):
        decide_env.configure(provider="haiku", haiku_model=model)
        r = decide.bool("state", "Is it?")
        assert r["provider"] == "haiku" and r["value"] and asked[-1][0] == want and asked[-1][1] is None, (model, r, asked)
    assert decide_env.seen == []  # no Messages API call for a non-Claude model
    # _deadline caps timeout for agent_answer; manual haiku_model=None falls back to HAIKU cleanly
    decide_env.configure(provider="haiku", haiku_model="gpt-6-luna")
    decide.batch("state_dl", {"q": {"kind": "bool", "question": "ok?"}}, timeout=12)
    assert asked[-1][2] <= 12
    # claude:<model> still takes the Messages API fast path, and haiku_model=None doesn't crash
    decide_env.reply["fn"] = lambda path, body: (200, {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps({k: {"p_yes": 0.9} for k in body["output_config"]["format"]["schema"]["properties"]})}],
    })
    decide_env.configure(provider="haiku", haiku_model="claude:claude-haiku-5-5")
    r_api = decide.bool("state", "Is it?")
    assert r_api["provider"] == "haiku" and r_api["value"] and len(decide_env.seen) == 1
    assert decide_env.seen[0]["body"]["model"] == "claude-haiku-5-5"
    decide_env.configure(provider="haiku", haiku_model=None)
    assert decide.bool("state2", "Is it?")["provider"] == "haiku"
    monkeypatch.setattr(harness, "ask", lambda *a, **k: "not json")
    decide_env.configure(provider="haiku", haiku_model="gpt-6-luna")
    r = decide.bool("state", "Is it?")
    assert r["provider"] == "local" and "wasn't JSON" in r["error"], r


def test_config_sets_haiku_model(decide_env, capsys):
    assert decide.main(["config", "--haiku-model", "gpt-6-luna"]) == 0
    assert json.loads(capsys.readouterr().out)["haiku_model"] == "gpt-6-luna"
    assert decide.conf()["haiku_model"] == "gpt-6-luna"


def test_haiku_cli_respects_deadline_cap(decide_env, monkeypatch):
    # BUG-CORE-11: haiku() claude -p subprocess caps timeout with c.get("_deadline")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    decide_env.configure(provider="haiku", haiku_model="claude-haiku-5-5")
    seen_timeouts = []

    def fake_run(cmd, *, timeout=None, **_kw):
        seen_timeouts.append(timeout)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"structured_output": {"q0": {"p_yes": 0.9}}}), "")

    with mock.patch.object(decide.subprocess, "run", side_effect=fake_run):
        res = decide.batch("state-dl", {"q": {"type": "bool", "question": "ok?"}}, timeout=12)
    assert res["q"]["provider"] == "haiku" and res["q"]["value"] is True
    assert seen_timeouts == [12.0]
