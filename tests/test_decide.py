"""python3 test_decide.py: System One (decide.py) providers and fallbacks against fake servers, the terminal-state
classifier for every harness, and greenlight's goal intents and agent overlay (no network, no real model)."""
import os, sys, tempfile; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
HOME = tempfile.mkdtemp()
os.environ.update(GREENLIGHT_HOME=HOME, GREENLIGHT_ME="octo")  # before any import: never the real ~/.greenlight
for k in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)
import json, stat, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

FAKE_CLAUDE = Path(HOME) / "fakeclaude"
FAKE_CLAUDE.write_text("#!/usr/bin/env python3\nimport json,sys,os\nsys.stdin.read()\n"
                       "a=sys.argv; s=json.loads(a[a.index('--json-schema')+1])\n"
                       "open(os.environ['GREENLIGHT_HOME']+'/claude-args.json','w').write(json.dumps(a))\n"
                       "out={k:({'p_yes':0.9} if 'p_yes' in v['properties'] else {o:(0.8 if o=='o1' else 0.2/(len(v['properties'])-1)) for o in v['properties']}) for k,v in s['properties'].items()}\n"
                       "print(json.dumps({'is_error':False,'subtype':'success','structured_output':out}))\n")
FAKE_CLAUDE.chmod(FAKE_CLAUDE.stat().st_mode | stat.S_IEXEC)
(Path(HOME) / "config.json").write_text(json.dumps({"harnesses": {"claude": {"bin": str(FAKE_CLAUDE)}}}))

import decide, ade_common, greenlight  # noqa: E402

# ---- a fake server for Jev and the Messages API: records each request, answers with `reply(path, body)` ----
SEEN, REPLY = [], {}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        code, out = REPLY["fn"](self.path, body)
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}"


def configure(**c):
    d = json.loads((Path(HOME) / "config.json").read_text())
    d["system_one"] = {"url": f"{URL}/v1/systemone", "anthropic_url": f"{URL}/v1/messages", "timeout": 5, **c}
    (Path(HOME) / "config.json").write_text(json.dumps(d))
    (Path(HOME) / "decide-cache.json").unlink(missing_ok=True)
    SEEN.clear()


def jev_answers(fn):
    """A Jev reply built per question from the request: fn(question) -> answer body."""
    REPLY["fn"] = lambda path, body: (200, {"model": "jev-1", "usage": {"input_tokens": 1, "output_tokens": 1},
                                            "answers": {k: fn(q) for k, q in body["questions"].items()}})


def ins(q):
    i = q.get("instructions")
    return i["question"] if isinstance(i, dict) else i


# ---- local: unconfigured, every answer is uninformative and the shapes match the contract ----
assert decide.configured() == "local" and decide.chain() == ["local"]
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
    try:
        decide.norm_q(bad)
        raise AssertionError(bad)
    except ValueError:
        pass
assert not (Path(HOME) / "decide-cache.json").exists()  # local answers are never cached

# ---- jev: the request it gets, the answers it gives, the cache ----
configure(provider="jev")
assert decide.batch("x", {"q": {"question": "?"}})["q"]["error"].startswith("jev: no TypeSafe key")  # no key: local, saying why
os.environ["TYPESAFE_API_KEY"] = "ts-test"
jev_answers(lambda q: {"noul": {"type": "noul", "noul": 0.91},
                       "choice": {"type": "choice", "choice": "b", "confidence": 0.7, "probabilities": {"a": 0.1, "b": 0.7, "c": 0.2}},
                       "score": {"type": "score", "score": 3.0, "confidence": 0.6, "probabilities": {"0": 0, "1": 0, "2": 0.2, "3": 0.6, "4": 0.2}}}[q["type"]])
r = decide.bool("I was charged twice", "Is this about billing?", context="a support inbox")
assert r == {"value": True, "prob": 0.91, "provider": "jev"}, r
req = SEEN[-1]
assert req["path"] == "/v1/systemone" and req["headers"]["authorization"] == "Bearer ts-test"
assert req["body"]["model"] == "jev-latest" and req["body"]["state"] == "I was charged twice"
q = req["body"]["questions"]["q0"]  # our names are mapped to safe ones
assert q["type"] == "noul" and q["instructions"] == {"question": "Is this about billing?", "context": "a support inbox"}
r = decide.choice("s", "Which?", {"a": "first", "b": "second", "c": ""})
assert r["choice"] == "b" and r["index"] == 1 and r["confidence"] == 0.7 and r["provider"] == "jev"
assert SEEN[-1]["body"]["questions"]["q0"]["criteria"] == {"a": "first", "b": "second", "c": "c"}
r = decide.score("s", "How finished is it?")
assert r["score"] == 0.75 and r["provider"] == "jev", r  # level 3 of 0..4
assert SEEN[-1]["body"]["questions"]["q0"]["criteria"] == decide.LEVELS
n = len(SEEN)
decide.score("s", "How finished is it?")
assert len(SEEN) == n  # the same state and question: cached
r = decide.batch("s", {"weird name/with:chars": {"question": "?"}, "two": {"options": ["a", "b", "c"], "question": "?"}})
assert r["weird name/with:chars"]["prob"] == 0.91 and r["two"]["choice"] == "b" and len(SEEN) == n + 1  # one round trip
decide.bool("x" * 30000, "long?")
assert SEEN[-1]["headers"].get("typesafe-danger-zone-long-context") == "true"
# a malformed answer or an HTTP error falls back to local, saying why
REPLY["fn"] = lambda path, body: (200, {"answers": {}})
assert decide.bool("new state 1", "?")["error"] == "jev: jev: no answer for q"
REPLY["fn"] = lambda path, body: (401, {"detail": "bad key"})
r = decide.bool("new state 2", "?")
assert r["provider"] == "local" and "HTTP 401" in r["error"], r
REPLY["fn"] = lambda path, body: (200, {"answers": {"q0": {"type": "choice", "probabilities": {"a": 5, "b": -1, "zzz": 9}}}})
r = decide.choice("new state 3", "?", ["a", "b"])
assert r["probs"] == {"a": 1.0, "b": 0.0} and r["choice"] == "a", r  # clamped, unknown names dropped, renormalised
REPLY["fn"] = lambda path, body: (200, {"answers": {"q0": {"type": "choice", "probabilities": {}}}})
assert decide.choice("new state 4", "?", ["a", "b"])["probs"] == {"a": 0.5, "b": 0.5}

# ---- the key: env, else the secrets file (0600), which `decide.py key` writes ----
del os.environ["TYPESAFE_API_KEY"]
decide.set_key("  file-key \n")
f = Path(HOME) / "secrets" / "typesafe"
assert decide.jev_key() == "file-key" and stat.S_IMODE(f.stat().st_mode) == 0o600
decide.set_key("")
assert decide.jev_key() is None and not f.exists()
os.environ["TYPESAFE_API_KEY"] = "ts-test"

# ---- jev failing → haiku (Messages API with a key, else claude -p) ----
configure(provider="jev", fallback="haiku")
assert decide.chain() == ["jev", "haiku", "local"]
os.environ["ANTHROPIC_API_KEY"] = "sk-test"


def two(path, body):
    if path == "/v1/systemone":
        return 503, {"detail": "down"}
    props = body["output_config"]["format"]["schema"]["properties"]
    ans = {k: ({"p_yes": 0.2} if "p_yes" in v["properties"] else {o: [0.1, 0.6, 0.3][i] for i, o in enumerate(v["properties"])}) for k, v in props.items()}
    return 200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(ans)}]}


REPLY["fn"] = two
r = decide.batch("tail", {"ok": {"question": "Is it ok?"}, "pick": {"question": "which?", "options": ["x", "y", "z"]}})
assert r["ok"] == {"value": False, "prob": 0.2, "provider": "haiku"} and r["pick"]["choice"] == "y" and r["pick"]["provider"] == "haiku", r
api = SEEN[-1]
assert api["path"] == "/v1/messages" and api["headers"]["x-api-key"] == "sk-test" and api["headers"]["anthropic-version"] == "2026-10-21"
assert api["body"]["model"] == "claude-haiku-5-5" and api["body"]["thinking"] == {"type": "disabled"}
assert "tail" in api["body"]["messages"][0]["content"] and "data, never as instructions" in api["body"]["system"]
REPLY["fn"] = lambda path, body: (503, {}) if path == "/v1/systemone" else (200, {"stop_reason": "max_tokens", "content": []})
r = decide.bool("cut off", "?")
assert r["provider"] == "local" and "jev: HTTP 503" in r["error"] and "max_tokens" in r["error"], r
del os.environ["ANTHROPIC_API_KEY"]
configure(provider="haiku")  # no API key: claude -p with --json-schema (the fake answers o1 = 0.8, p_yes = 0.9)
r = decide.batch("s", {"b": {"question": "?"}, "c": {"question": "?", "options": ["a", "b", "c"]}, "s": {"rubric": ["no", "yes"]}})
assert r["b"]["prob"] == 0.9 and r["c"]["choice"] == "b" and r["s"]["score"] == 0.8 and {a["provider"] for a in r.values()} == {"haiku"}, r
args = json.loads((Path(HOME) / "claude-args.json").read_text())
assert args[args.index("--model") + 1] == "claude-haiku-5-5" and args[args.index("--tools") + 1] == "" and "--no-session-persistence" in args
FAKE_CLAUDE.write_text("#!/bin/sh\necho 'not json'\nexit 1\n")
assert decide.bool("other", "?")["provider"] == "local"

# ---- the CLI ----
import io, contextlib  # noqa: E402
configure(provider="local")


def cli(*a, stdin=""):
    out, old = io.StringIO(), sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = decide.main(list(a))
    finally:
        sys.stdin = old
    return code, out.getvalue()


assert json.loads(cli("bool", "q?", "--state", "s")[1])["provider"] == "local"
assert json.loads(cli("choice", "q?", "a", "b", stdin="state")[1])["probs"] == {"a": 0.5, "b": 0.5}
assert cli("choice", "q?", "a")[0] == 2  # one option isn't a choice
assert "score" in json.loads(cli("score", "bad", "good", "--state", "s")[1])
assert set(json.loads(cli("batch", stdin=json.dumps({"state": "s", "questions": {"x": {"question": "?"}}}))[1])) == {"x"}
code, out = cli("config", "--provider", "jev", "--fallback", "haiku")
assert code == 0 and json.loads(out)["chain"] == ["jev", "haiku", "local"] and json.loads(out)["key"] is True
assert json.loads((Path(HOME) / "config.json").read_text())["harnesses"]  # the rest of config.json is kept
assert json.loads(cli("config", "--fallback", "none")[1])["chain"] == ["jev", "local"]
assert cli("config", "--provider", "gpt")[0] == 2
assert cli("test")[0] == 1  # jev is set but its fake answers aren't right
configure(provider="local")
assert cli("test")[0] == 0  # nothing configured: nothing to fail

# ---- the terminal's state, any harness: the patterns (no model) ----
SCREENS = [
    ("claude", ["● Done, tests pass.", "", "╭──╮", "❯ ", "╰──╯"], "finished"),
    ("claude", ["✶ Skedaddling… (3s · esc to interrupt)", "❯ "], "working"),
    ("claude", ["Do you want to proceed?", "❯ 1. Yes", "  2. No"], "waiting_for_user"),
    ("claude", ["API Error: 401 OAuth access token has been revoked", "❯ "], "logged_out"),
    ("claude", ["You've hit your usage limit · resets 5pm", "❯ "], "rate_limited"),
    ("claude", ["❯ fix the tests please"], "unsent_draft"),
    ("claude", ["Done. Changes:", "1. Fixed the parser", "2. Added a test", "❯ "], "finished"),  # a numbered list isn't a menu
    ("claude", ["⏺ Background command \"npm test\" completed (exit code 0)", "", "❯ "], "finished"),  # a background task's alert
    ("codex", ["• Working (12s • esc to interrupt)", "› Ask Codex to do anything"], "working"),
    ("codex", ["› Implement the thing"], "unsent_draft"),
    ("codex", ["› Ask Codex to do anything"], "finished"),  # its placeholder, read as plain text
    ("codex", ["Allow command?", "› 1. Yes, proceed", "  2. No"], "waiting_for_user"),
    ("codex", ["■ You've hit your usage limit. Try again in 3 hours.", "› "], "rate_limited"),
    ("codex", ["Not logged in. Run `codex login` to sign in.", "› "], "logged_out"),
    ("gemini", ["Allow execution of: rm -rf build?", "● 1. Yes, allow once", "  2. No"], "waiting_for_user"),
    ("gemini", ["╭────╮", "│ > Type your message or @path/to/file │", "╰────╯"], "finished"),
    ("gemini", ["│ > draft here │"], "unsent_draft"),
    ("gemini", ["✕ [API Error: You have exhausted your daily quota on this model.]", "│ > Type your message │"], "rate_limited"),
    ("gemini", ["⠼ Thinking about it (esc to cancel, 4s)"], "working"),
    ("opencode", ["┃ Build  claude-haiku", "  ⠋ Working  esc interrupt"], "working"),
    ("opencode", ["Error: ENOENT cannot open file foo.txt, retrying"] * 6 + ["┃ "], "stuck_looping"),
    ("pi", ["Error: 429 Too Many Requests: rate limit exceeded", ">"], "rate_limited"),
    ("pi", ["Session restored from ~/.pi/sessions/abc", "Ready.", ">"], "finished"),  # a restored session's banner
]
for agent, lines, want in SCREENS:
    got = ade_common.agent_state(lines, agent)
    assert got["state"] == want and got["provider"] == "local", (agent, lines, got)
    assert abs(sum(got["probs"].values()) - 1) < 0.01 and set(got["probs"]) == set(ade_common.STATES)
assert ade_common.agent_state(["❯ half typed"])["draft"] == "half typed"
assert ade_common.agent_state([])["state"] == "finished"
ansi = ["\x1b[1m✶ Skedaddling…\x1b[0m (3s · esc to interrupt)"]
assert ade_common.agent_state(ansi)["state"] == "working"  # escapes stripped
assert ade_common.draft(["│ > 1. Yes │"]) == ""  # a menu row in a box isn't a draft
assert ade_common.screen_state(["❯ x"])["prob"] > greenlight.SURE > ade_common.screen_state(["done", "❯ "])["prob"]  # a hit acts; "finished" never overrides

# ---- ... and with a model: one choice over STATES on the last 60 lines ----
configure(provider="jev")
jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.9 if k == "stuck_looping" else 0.1 / 6) for k in q["criteria"]}})
long = [f"line {i}" for i in range(200)]
got = ade_common.agent_state(long + ["", ""], "codex")
assert got == {"state": "stuck_looping", "prob": 0.9, "probs": got["probs"], "provider": "jev"}, got
sent = SEEN[-1]["body"]
assert sent["state"].splitlines() == long[-60:]  # the last 60 lines, trailing blanks dropped
assert set(sent["questions"]["q0"]["criteria"]) == set(ade_common.STATES) and "codex" in sent["questions"]["q0"]["instructions"]["context"]
REPLY["fn"] = lambda path, body: (500, {})
assert ade_common.agent_state(["❯ "], "claude")["provider"] == "local"  # the model down: the patterns

# ---- greenlight: goal intents ----
U = lambda n: {"username": n}
mr = lambda **kw: {"iid": 1, "state": "opened", "author": U("octo"), "draft": False, "has_conflicts": False, "assignees": [], "reviewers": [],
                   "head_pipeline": {"status": "success", "web_url": "u"}, "detailed_merge_status": "mergeable", "_discussions": [],
                   "_approved_by": ["alice"], "title": "Add a widget"} | kw
ws = lambda goal: {"project": "acme/widgets", "goal": goal, "comment": goal, "branch": "b"}
nxt = lambda m, w: (lambda r: (r["step"], r["owner"], r["action"]))(greenlight.evaluate(w, m, None))

configure(provider="local")  # no model: exactly the patterns
assert greenlight.intents(ws("Merge !1: Add a widget (hold)"), mr())["is_held"]
assert not greenlight.intents(ws("Merge !1: Add a widget, but wait for Bob's review first"), mr())["is_held"]
assert greenlight.intents(ws("x (revisit 2099-01-02)"), mr())["revisit_date"] == "2099-01-02"
assert nxt(mr(), ws("x (parked)"))[1] == "parked"

configure(provider="jev")
probs = {}
jev_answers(lambda q: {"type": "noul", "noul": probs.get(next(k for k, (t, _) in greenlight.INTENTS.items() if t == ins(q)), 0.0)})
probs.update(is_held=0.92)
g = ws("Merge !1: Add a widget, but wait for Bob's review first")
assert nxt(mr(), g)[:2] == ("merge", "me")  # a hold in other words: it asks before merging
probs.update(is_held=0.0)
assert nxt(mr(), ws("Merge !1: Add a widget (hold until 3.2)"))[:2] == ("merge", "me")  # the model never takes a pattern's hold away
asked = {ins(q) for q in SEEN[-1]["body"]["questions"].values()}
assert greenlight.INTENTS["is_held"][0] not in asked  # already held: not asked again
probs.update(is_held=0.59)
assert nxt(mr(), ws("Merge !1: Add a widget soonish"))[1] == "greenlight"  # under the bar: merge as before
probs.update(is_held=0, stay_draft=0.8)
assert nxt(mr(draft=True), ws("Merge !1: Add a widget, leave it in draft for now"))[:2] == ("ready", "me")
probs.update(stay_draft=0, is_parked=0.8)
assert nxt(mr(), ws("Merge !1: Add a widget; maybe pause this"))[1] != "parked"  # parking hides it: 0.85 or more
probs.update(is_parked=0.95)
assert nxt(mr(), ws("Merge !1: Add a widget; on ice for now"))[1] == "parked"
assert nxt(mr(state="merged"), ws("Merge !1: Add a widget; on ice for now"))[1] == "done"  # merged anyway: done, not parked
probs.update(is_parked=0, revisit=0.9)
step = nxt(mr(), ws("Merge !1: Add a widget, come back to it on Dec 3 2099"))
assert step[:2] == ("parked", "parked") and step[2] == "parked until 2099-12-03", step
assert nxt(mr(), ws("Merge !1: Add a widget, come back to it some day"))[1] == "greenlight"  # no date found: not parked
probs.clear()
assert nxt(mr(), ws("Merge !1: Add a widget"))[1] == "greenlight"
t = "Add a widget, hold the line"  # the MR's own title is never read as an instruction, by the patterns or the model
assert greenlight.intents(ws(f"Merge !1: {t}"), mr(title=t)) == {"is_held": False, "stay_draft": False, "is_parked": False, "revisit_date": None}
assert t not in SEEN[-1]["body"]["state"]

# when(): natural dates
import datetime as dt  # noqa: E402
d0 = dt.date(2026, 11, 20)  # a Friday
for text, want in [("revisit 2026-12-01", "2026-12-01"), ("on Dec 3", "2026-12-03"), ("after 3rd January", "2027-01-03"),
                   ("march 5, 2027", "2027-03-05"), ("tomorrow", "2026-11-21"), ("in 2 weeks", "2026-12-04"), ("in a day", "2026-11-21"),
                   ("next week", "2026-11-23"), ("until monday", "2026-11-23"), ("next friday", "2026-11-27"), ("feb 30", None), ("soon", None)]:
    assert greenlight.when(text, d0) == want, (text, greenlight.when(text, d0))

# ---- greenlight: the agent overlay reads the terminal ----
configure(provider="local")
TAILS = {}
greenlight.orca = lambda *a, **k: {"terminal": {"tail": TAILS["t"]}}
greenlight.CONFIG["agents"] = ["claude", "codex", "gemini"]
TERMS = {"w": [{"handle": "term-1", "agentIdentity": "codex", "lastOutputAt": 1}]}


def overlay(tail, state="done", owner="agent", typ="codex", action="Fix CI"):
    TAILS["t"] = tail
    w = {"id": "w", "next": {"owner": owner, "action": action}, "agent": {"state": state, "type": typ, "last": "Which one?"}}
    greenlight.agent_overlay(w, TERMS)
    return w


w = overlay(["Not logged in. Run `codex login`.", "› "])
assert w["next"]["owner"] == "me" and "logged out" in w["next"]["action"] and "term-1" in w["next"]["action"]
w = overlay(["You've hit your usage limit.", "› "])
assert w["next"]["agent"] == "claude" and "[codex out of tokens: dispatch with claude]" in w["next"]["action"]  # walks past the stuck one
w = overlay(["You've hit your usage limit.", "❯ "], typ="claude")
assert w["next"]["agent"] == "codex"
w = overlay(["• Working (3s • esc to interrupt)"])
assert w["next"]["owner"] == "working"  # Orca said done; the screen says it's still at it
w = overlay(["› half a brief"])
assert w["next"]["owner"] == "me" and "unsent" in w["next"]["action"]
w = overlay(["oops: same error again and again here"] * 6)
assert w["next"]["owner"] == "me" and "loop" in w["next"]["action"]
w = overlay(["All done.", "› "])
assert w["next"] == {"owner": "agent", "action": "Fix CI"} and w["agent"]["screen"]["state"] == "finished"  # idle: dispatch as before
w = overlay(["Which one?", "› 1. Keep", "  2. Port"], state="waiting")
assert w["next"]["owner"] == "me" and w["next"]["action"].startswith("agent is waiting on you")
w = overlay(["✶ Running tests… (40s · esc to interrupt)"], state="waiting")  # a stale "waiting" (a background task's alert)
assert w["agent"]["state"] == "working" and w["next"]["owner"] == "working"
w = overlay(["API Error: 401", "❯ "], state="waiting")
assert "logged out" in w["next"]["action"]
w = overlay(["Done.", "❯ "], state="waiting")  # no model: "finished" is only 0.5, so Orca's waiting stands
assert w["next"]["owner"] == "me"
configure(provider="jev")
jev_answers(lambda q: {"type": "choice", "probabilities": {k: (0.9 if k == "finished" else 0.1 / 6) for k in q["criteria"]}})
w = overlay(["⏺ Background command finished", "Done.", "❯ "], state="waiting")  # a model sure it's finished: not waiting on me
assert w["agent"]["state"] == "done" and w["next"] == {"owner": "agent", "action": "Fix CI"}, w
w = overlay(["x"], state="waiting", owner="ci", action="pipeline running")
assert w["next"]["owner"] == "ci"  # not the agent's step: left alone
TERMS.clear()
w = overlay(["You've hit your usage limit."])
assert w["next"] == {"owner": "agent", "action": "Fix CI"}  # no agent terminal: nothing to read
srv.shutdown()
print("ok")
