#!/usr/bin/env python3
"""diag.py: free text never reaches the trail, hashes correlate, spans and uncaught errors record, rotation caps size.
python3 tests/test_diag.py"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = tempfile.mkdtemp()
os.environ.update(GREENLIGHT_HOME=HOME)
os.environ.pop("GREENLIGHT_RUN", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
(Path(HOME) / "leak.re").write_text("zebracorn\n")
import diag  # noqa: E402

diag.app_path = lambda: Path(HOME) / "no-app.jsonl"  # don't read the real app's log
diag.crash_dir = lambda: Path(HOME) / "crashes"  # nor the real crash reports
PRIVATE = ["ghp_" + "A" * 36, "/Users/someone/src/x", "dev@corp.test", "zebracornium project", "zebracornium", "Fix login for ACME users"]


def events():
    return [json.loads(x) for x in diag.path().read_text().splitlines()]


# free text in any field is dropped and counted, never written
for i, s in enumerate(PRIVATE):
    diag.event("probe", name=s, err=s, extra=s, where=[s])
diag.event("probe", tool="glab", sub="api", rc=1, ok=False, ws_h=diag.h("my-secret-branch"))
text = diag.path().read_text()
for s in PRIVATE + ["my-secret-branch"]:
    assert s not in text, s
ev = events()
assert all(e["dropped"] >= 3 for e in ev[:len(PRIVATE)]), ev
assert ev[-1]["tool"] == "glab" and ev[-1]["rc"] == 1 and len(ev[-1]["ws_h"]) == 10 and "dropped" not in ev[-1]
assert diag.event("probe", name_h="not-a-hash") is None and events()[-1].get("dropped") == 1  # _h keys take only h()'s output

# hashes: stable per salt, different across salts, and the salt is owner-only
a = diag.h("ws-1")
assert a == diag.h("ws-1") != diag.h("ws-2")
assert oct((Path(HOME) / "diag" / "salt").stat().st_mode & 0o777) == "0o600"
(Path(HOME) / "diag" / "salt").write_bytes(b"other")
assert diag.h("ws-1") != a

assert not any(diag.h(f"ws-{i}").isdigit() for i in range(3000))  # an all-digit hash looks like a phone number on export

# classify
assert diag.classify("HTTP 429 Too Many Requests") == "rate_limit"
assert diag.classify("glab: 401 Unauthorized") == "auth"
assert diag.classify("ssh: Could not resolve hostname x") == "network"
assert diag.classify("502 Bad Gateway") == "transient"
assert diag.classify("weird") == "other"

# span: secs and ok, or the failure (type, class, our frames) and the exception still raises
with diag.span("cmd", tool="gh") as s:
    s["rc"] = 0
e = events()[-1]
assert e["kind"] == "cmd" and e["ok"] is True and e["rc"] == 0 and e["secs"] >= 0
try:
    with diag.span("cmd", tool="gh"):
        raise RuntimeError("gh pr view 12: HTTP 404 for /Users/someone/repo")
except RuntimeError:
    pass
else:
    raise AssertionError("span swallowed the exception")
e = events()[-1]
assert e["ok"] is False and e["err"] == "RuntimeError" and e["msg_class"] == "not_found", e
assert e["where"] and e["where"][-1].startswith("test_diag.py:"), e
assert "someone" not in json.dumps(e)

# install: an uncaught exception is recorded, and a child process joins the parent's run id
child_py = Path(HOME, "child.py")
child_py.write_text(f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import diag; diag.event('child')\n")
code = (f"import sys, subprocess; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import diag; diag.install('probe'); "
        f"subprocess.run([sys.executable, {str(child_py)!r}]); raise ValueError('secret text')")
p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ})
assert p.returncode == 1 and "ValueError" in p.stderr  # the original hook still prints the traceback
child, unc = events()[-2:]
assert child["kind"] == "child" and unc["kind"] == "uncaught" and unc["err"] == "ValueError" and unc["comp"] == "probe", (child, unc, p.stderr)
assert child["run"] == unc["run"] and "secret" not in diag.path().read_text()

# reading: errors group, summary has its keys
g = diag.errors(diag.read())
assert any(x["err"] == "RuntimeError" and x["msg_class"] == "not_found" for x in g), g
(Path(HOME) / "config.json").write_text(json.dumps({"me": "octo", "merge_method": "squash", "agents": ["claude", "x"],
                                                     "require_human_approval": False, "blocking": {"team": ["a"]}}))
s = diag.summary(0)
assert {"version", "env", "config", "errors_24h", "top_errors", "spans", "automations"} <= set(s)
assert s["config"] == {"me": "str", "merge_method": "squash", "agents": ["claude"], "require_human_approval": False,
                       "blocking": "dict"}, s["config"]
assert s["spans"]["cmd.gh"]["n"] == 2 and s["spans"]["cmd.gh"]["fail"] == 0.5

# rotation at the cap, keeping KEEP old files
diag.MAX = 2000
for _ in range(300):
    diag.event("fill", rc=0)
files = sorted(p.name for p in diag.path().parent.glob("events.jsonl*"))
assert files == ["events.jsonl", "events.jsonl.1", "events.jsonl.2", "events.jsonl.3"], files
assert diag.path().stat().st_size < 2000 + 200
# doctor: fake forges on PATH (glab logged out), an automation whose timer never ticked, a pid file of a dead process
bin_ = Path(HOME, "bin"); bin_.mkdir()
(bin_ / "glab").write_text("#!/bin/sh\necho '401 Unauthorized' >&2; exit 1\n")
(bin_ / "gh").write_text(f"#!/bin/sh\n[ \"$1\" = auth ] && exit 0\necho \"$@\" > {HOME}/gh.args; cat > {HOME}/gh.body; echo https://github.com/x/y/issues/9\n")
for x in ("glab", "gh"):
    (bin_ / x).chmod(0o755)
os.environ["PATH"] = f"{bin_}:/usr/bin:/bin"
Path(HOME, "config.json").write_text(json.dumps({"agents": ["codex"]}))
Path(HOME, "automations.json").write_text(json.dumps({"nightly": {"schedule": "0 3 * * *"}}))
Path(HOME, "auto").mkdir(exist_ok=True)
dead = subprocess.Popen(["true"]); dead.wait()
Path(HOME, "auto", "nightly.pid").write_text(str(dead.pid))
Path(HOME, "orchestrator-phase-1.pid").write_text("{}")  # no pid in it: skipped, not a crash
_which = shutil.which  # a codex installed in /usr/bin would pass agent_codex: hide it so the check sees none
shutil.which = lambda c, *a, **k: None if c == "codex" else _which(c, *a, **k)
try:
    d = {c["check"]: c for c in diag.doctor()["checks"]}
finally:
    shutil.which = _which
assert d["glab_auth"] == {"check": "glab_auth", "ok": False, "rc": 1, "msg_class": "auth"}, d
assert d["gh_auth"]["ok"] and d["agent_codex"]["ok"] is False and d["disk"]["ok"] and d["home_writable"]["ok"], d
assert d["auto_timer"] == {"check": "auto_timer", "ok": False, "state": "missing"} and d["stale_pids"]["n"] == 1, d
import auto  # noqa: E402
auto.tick()  # a 3am automation isn't due now, but the tick still leaves its heartbeat
assert {c["check"]: c for c in diag.doctor()["checks"]}["auto_timer"]["state"] == "fresh"

# usage: chat turns and automation runs roll up per harness.model; orchestrator and skipped runs aren't agent runs
for o, c in ((1200, 5.123), (800, 3.0)):
    diag.event("chat_turn", ok=True, harness="claude", model="opus", in_tokens=9000, out_tokens=o, cost_cents=c, secs=2.0)
diag.event("chat_turn", ok=False, harness="codex", secs=1.0)
diag.event("automation", status="ok", engine="codex", in_tokens=500, out_tokens=50, secs=9.0)
diag.event("automation", status="ok", engine="orchestrator", secs=9.0)
diag.event("automation", status="skipped", secs=0.1)
u = diag.summary(0)["usage"]
assert u == {"claude.opus": {"turns": 2, "runs": 0, "in_tokens": 18000, "out_tokens": 2000, "cost_usd": 0.08123},
             "codex.default": {"turns": 1, "runs": 1, "in_tokens": 500, "out_tokens": 50, "cost_usd": 0.0}}, u

# automations stream JSON: reply() keeps the reply, step titles and plain stderr lines, and reads the tokens
claude = "\n".join(json.dumps(x) for x in [
    {"type": "system", "subtype": "init", "session_id": "s"},
    {"type": "result", "result": "All green.", "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7},
     "total_cost_usd": 0.0123}])
text, tok = auto.reply("claude", "warning: plain stderr\n42\n" + claude)
assert text == "warning: plain stderr\n42\nAll green." and tok == {"in_tokens": 100, "out_tokens": 7, "cost_cents": 1.23}, (text, tok)
assert auto.reply("claude", "Error: not logged in") == ("Error: not logged in", {})
assert auto.reply("codex", '{"type": "item.started", "item": {"type": "command_execution"}}\nboom') == ("boom", {})  # a parser KeyError: the line is skipped

# end to end: a fake claude on PATH streams JSON; the run's record, log and event carry tokens, and a limit still falls back
(bin_ / "claude").write_text("#!/bin/sh\ncat >/dev/null\necho 'note: plain stderr' >&2\n[ -f %s/limit ] && { echo \"You've hit your usage limit\" >&2; exit 1; }\n"
                             "cat <<'J'\n%s\nJ\n" % (HOME, claude))
(bin_ / "codex").write_text("#!/bin/sh\ncat >/dev/null\necho 'codex says done'\n")
for x in ("claude", "codex"):
    (bin_ / x).chmod(0o755)
os.environ["PATH"] = f"{bin_}:/usr/bin:/bin"
Path(HOME, "automations.json").write_text(json.dumps({"t": {"schedule": "0 3 * * *", "prompt": "hi", "provider": "claude", "model": "opus"}}))
auto.execute("t")
r = json.loads(Path(HOME, "auto", "runs.jsonl").read_text().splitlines()[-1])
assert (r["status"], r["engine"], r["in_tokens"], r["out_tokens"], r["cost_cents"], r["model"]) == ("ok", "claude", 100, 7, 1.23, "opus"), r
assert Path(r["log"]).read_text() == "$ claude\nnote: plain stderr\nAll green.\n[exit 0]\n", Path(r["log"]).read_text()
ev = [e for e in diag.read(0) if e["kind"] == "automation"][-1]
assert (ev["in_tokens"], ev["out_tokens"], ev["cost_cents"], ev["model"]) == (100, 7, 1.23, "opus"), ev
assert diag.summary(0)["usage"]["claude.opus"]["runs"] == 1
assert diag.automations()[-1]["in_tokens"] == 100
Path(HOME, "limit").touch()
auto.execute("t")
r = json.loads(Path(HOME, "auto", "runs.jsonl").read_text().splitlines()[-1])
assert (r["status"], r["engine"], r["summary"]) == ("ok", "codex", "codex says done") and "in_tokens" not in r, r
os.environ["PATH"] = f"{bin_}:/usr/bin:/bin"

# codex: a resumed thread's usage is the thread's total, so the turn's is the difference from its rollout's last token_count
import chat  # noqa: E402
chat.CODEX_SESSIONS = str(Path(HOME, "codex"))
Path(HOME, "codex", "2026", "11", "21").mkdir(parents=True)
Path(HOME, "codex", "2026", "11", "21", "rollout-x-th1.jsonl").write_text(json.dumps(
    {"payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1000, "output_tokens": 100}}}}) + "\n")
n = chat.Norm()
n.base = chat.codex_base("th1")
n.codex({"type": "turn.completed", "usage": {"input_tokens": 1600, "output_tokens": 160}})
assert (n.tin, n.tout) == (600, 60) and chat.codex_base("none") is None, (n.tin, n.tout)
chat.TURN.update(harness="codex", model="gpt", t=0, norm=n)
with contextlib.redirect_stdout(io.StringIO()):
    chat.emit(e="done", ok=True, text="x", context=5, window=None, ms=None)
ev = [e for e in diag.read(0) if e["kind"] == "chat_turn"][-1]
assert (ev["harness"], ev["in_tokens"], ev["out_tokens"]) == ("codex", 600, 60) and "cost_cents" not in ev, ev

# crash reports: the crashed thread's frames by image name and symbol; a frame naming a path is dropped
crash = Path(HOME, "crashes"); crash.mkdir()
(crash / "Greenlight-2026-11-21-101010.ips").write_text(json.dumps({"build_version": "abc1234"}) + "\n" + json.dumps({
    "codeSigningID": "dev.greenlight.app", "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"}, "faultingThread": 1,
    "usedImages": [{"name": "Greenlight", "path": "/Users/someone/Applications/Greenlight.app"}],
    "threads": [{"frames": []}, {"frames": [{"imageIndex": 0, "symbol": "AppDelegate.scan()"},
                                            {"imageIndex": 0, "symbol": "/Users/someone/x"}, {"imageIndex": 7, "imageOffset": 16}]}]}))
(crash / "Greenlight-bad.ips").write_text("not json")
(crash / "GreenlightPhotos-2026.ips").write_text("{}\n" + json.dumps({"codeSigningID": "com.other.photos"}))
c = diag.crashes(0)
assert c == [{"ts": c[0]["ts"], "v": "abc1234", "exc": "EXC_BAD_ACCESS", "signal": "SIGSEGV",
              "frames": ["Greenlight:AppDelegate.scan()", "?:0x10"]}], c

# export: a bundle re-checks every event, and leaves out one that now hits (a phone-shaped token, a term added to leak.re since)
diag.MAX = 5 << 20
diag.event("probe", name="555-234-5678")  # a token by shape, a phone number by audit's patterns
diag.event("probe", name="quokka")
(Path(HOME) / "leak.re").write_text("zebracorn\nquokka\n")
diag._leak = False
app = [{"ts": 1e10, "kind": "js_error", "comp": "app", "name": "TypeError", "line": 3}, {"ts": 1e10, "kind": "x", "comp": "app", "to": "a@b.co"}]
app += [{"ts": 1e10, "kind": "ssh_exit", "comp": "app", "verb": v, "rc": 2} for v in ("dash.stream", "greenlight.scan")]  # one ms
app += app[-1:]  # and an exact repeat, as when the app shares this machine
b = diag.bundle(0, app)
assert sorted(e["verb"] for e in b["events"] if e["kind"] == "ssh_exit") == ["dash.stream", "greenlight.scan"], b["events"][-4:]
text = json.dumps(b)
assert "555-234-5678" not in text and "quokka" not in text and "a@b.co" not in text and "zebracorn" not in text, text[-800:]
assert b["manifest"]["left_out"] == 1 and any(e.get("name") == "TypeError" for e in b["events"]), b["manifest"]
assert not any("to" in e for e in b["events"] if e.get("kind") == "x")  # the app's field failed the schema, so it went
assert set(b) == {"manifest", "summary", "doctor", "errors", "events"} and "top_errors" not in b["summary"]
assert b["summary"]["crashes"][0]["exc"] == "EXC_BAD_ACCESS" and "someone" not in text

# report: the user's own text is checked too, and blocks; a preview files nothing; --execute runs gh on the protocol's repo
r = diag.report("Crash on scan", "my email is dev@corp.test")
assert r == {"ok": False, "blocked": "description", "reason": "the description contains an email address: take it out and try again"}, r
assert diag.report("quokka broke", "")["blocked"] == "title"
r = diag.report("Crash on scan", "It crashed after Rescan.")
assert r["ok"] and r["preview"] and r["body"].startswith("It crashed after Rescan.") and len(r["body"]) <= diag.ISSUE_MAX
assert Path(r["bundle"]).exists() and oct(Path(r["bundle"]).stat().st_mode & 0o777) == "0o600"
r = diag.report("Crash on scan", "It crashed after Rescan.", execute=True)
assert r["filed"] and r["url"] == "https://github.com/x/y/issues/9", r
assert Path(HOME, "gh.args").read_text().split()[:4] == ["issue", "create", "--repo", diag.REPO]
assert Path(HOME, "gh.body").read_text() == r["body"]
assert diag.report("Crash on scan", "x", execute=True, shown=r["body"] + " dev@corp.test")["blocked"] == "report"
diag.report("Crash on scan", "x", execute=True, shown="as previewed")
assert Path(HOME, "gh.body").read_text() == "as previewed"  # the shown body is filed, not a rebuild with newer events
os.environ["PATH"] = "/nonexistent"
r = diag.report("Crash on scan", "It crashed.", execute=True)
assert r["filed"] is False and r["url"].startswith(f"https://github.com/{diag.REPO}/issues/new?"), r

# the check fails closed: if audit can't load, nothing is exported
sys.modules["audit"] = None
n = len(list(Path(HOME, "diag").glob("bundle-*")))
for f in (lambda: diag.bundle(0), lambda: diag.report("t", "x")):
    try:
        f()
    except ImportError:
        pass
    else:
        raise AssertionError("exported without the check")
assert len(list(Path(HOME, "diag").glob("bundle-*"))) == n
del sys.modules["audit"]
print("test_diag ok")

# review fixes: a codex limit is still seen as one (the first done, not turn.failed's dict)
codex_limit = "\n".join(json.dumps(x) for x in [{"type": "thread.started", "thread_id": "t"}, {"type": "error", "message": "You've hit your usage limit"},
                                                 {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}])
import auto as _auto  # noqa: E402
txt, _ = _auto.reply("codex", codex_limit)
assert _auto.LIMIT_RE.search(txt), txt
# an empty salt (a crash mid-write) is replaced, not used
(Path(HOME) / "diag" / "salt").write_bytes(b"")
assert len(diag.salt()) == 32 and diag.salt() == diag.salt()
# a malformed line doesn't break the readers
with open(diag.path(), "a") as f:
    f.write(json.dumps({"ts": "x", "kind": "cmd"}) + "\n" + json.dumps({"ts": 9e12, "secs": 1}) + "\n")
diag.summary(0), diag.errors(diag.read()), diag.spans(diag.read())
# a too-long report keeps its fence and details closed
fake = {"manifest": {"version": "v", "events": 0, "left_out": 0}, "summary": {"x": "y" * (diag.ISSUE_MAX + 10)}, "errors": [], "events": []}
body = diag.issue_body("t", fake)
assert len(body) <= diag.ISSUE_MAX and body.endswith("```\n</details>\n"), body[-80:]
# a missing body file is a JSON reason, not a traceback
p = subprocess.run([sys.executable, str(ROOT / "scripts/diag.py"), "report", "--title", "t", "--body-file", "/nonexistent/x"],
                   capture_output=True, text=True, env={**os.environ, "GREENLIGHT_HOME": HOME})
assert p.returncode == 1 and json.loads(p.stdout)["ok"] is False and "Traceback" not in p.stderr, (p.stdout, p.stderr)
print("ok  review fixes")
# a cmd span that fails once (transient) and then succeeds is ok, with no stale rc or msg_class
import greenlight as _gl  # noqa: E402
flag = Path(HOME, "flaky")
_gl.time.sleep, _sleep = (lambda s: None), _gl.time.sleep
try:
    assert _gl.run(["/bin/sh", "-c", f'PATH=/bin:/usr/bin; [ -e {flag} ] && echo fine || {{ touch {flag}; echo "502 Bad Gateway" >&2; exit 1; }}'], tries=2).strip() == "fine"
finally:
    _gl.time.sleep = _sleep
last = [e for e in diag.read() if e.get("kind") == "cmd"][-1]
assert last.get("ok") is True and last["tries"] == 2 and "rc" not in last and "msg_class" not in last, last
print("ok  cmd retry span")
