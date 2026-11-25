"""Tests for scripts/diag.py: privacy filtering, salted hashes, spans, rotation, doctor, usage, crashes, and reports."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import auto
import chat
import diag
import greenlight
from conftest import ROOT

FAKE_TOKEN = "ghp_" + "A" * 36
FAKE_HOME_PATH = "/" + "Users/someone/src/x"
FAKE_EMAIL = "dev" + "@" + "corp.test"
FAKE_APP_EMAIL = "a" + "@" + "b.co"
FAKE_PHONE = "555-" + "234-5678"
FAKE_APP_PATH = "/" + "Users/someone/Applications/Greenlight.app"
FAKE_SYM_PATH = "/" + "Users/someone/x"
FAKE_REPO_PATH = "/" + "Users/someone/repo"
PRIVATE = [FAKE_TOKEN, FAKE_HOME_PATH, FAKE_EMAIL, "zebracornium project", "zebracornium", "Fix login for ACME users"]


def _events():
    return [json.loads(x) for x in diag.path().read_text().splitlines()]


@pytest.fixture()
def diag_env(gl_home, monkeypatch):
    (gl_home / "leak.re").write_text("zebracorn\n")
    monkeypatch.setattr(diag, "app_path", lambda: gl_home / "no-app.jsonl")  # don't read the real app's log
    monkeypatch.setattr(diag, "crash_dir", lambda: gl_home / "crashes")  # nor the real crash reports
    monkeypatch.setattr(diag, "_leak", False)
    return gl_home


def test_free_text_dropped_and_token_fields_kept(diag_env):
    for s in PRIVATE:
        diag.event("probe", name=s, err=s, extra=s, where=[s])
    diag.event("probe", tool="glab", sub="api", rc=1, ok=False, ws_h=diag.h("my-secret-branch"))
    text = diag.path().read_text()
    for s in [*PRIVATE, "my-secret-branch"]:
        assert s not in text, s
    ev = _events()
    assert all(e["dropped"] >= 3 for e in ev[: len(PRIVATE)]), ev
    assert ev[-1]["tool"] == "glab" and ev[-1]["rc"] == 1 and len(ev[-1]["ws_h"]) == 10 and "dropped" not in ev[-1]
    assert diag.event("probe", name_h="not-a-hash") is None and _events()[-1].get("dropped") == 1  # _h keys take only h()'s output


def test_salted_hashes_and_permissions(diag_env):
    a = diag.h("ws-1")
    assert a == diag.h("ws-1") != diag.h("ws-2")
    assert oct((diag_env / "diag" / "salt").stat().st_mode & 0o777) == "0o600"
    (diag_env / "diag" / "salt").write_bytes(b"other")
    assert diag.h("ws-1") != a
    assert not any(diag.h(f"ws-{i}").isdigit() for i in range(3000))  # an all-digit hash looks like a phone number on export


def test_classify_error_messages():
    assert diag.classify("HTTP 429 Too Many Requests") == "rate_limit"
    assert diag.classify("glab: 401 Unauthorized") == "auth"
    assert diag.classify('Oauth2: "invalid_grant" expired') == "auth"
    assert diag.classify("ssh: Could not resolve hostname x") == "network"
    assert diag.classify("502 Bad Gateway") == "transient"
    assert diag.classify("weird") == "other"


def test_span_install_errors_and_summary(diag_env):
    with diag.span("cmd", tool="gh") as s:
        s["rc"] = 0
    e = _events()[-1]
    assert e["kind"] == "cmd" and e["ok"] is True and e["rc"] == 0 and e["secs"] >= 0

    with pytest.raises(RuntimeError), diag.span("cmd", tool="gh"):
        raise RuntimeError(f"gh pr view 12: HTTP 404 for {FAKE_REPO_PATH}")
    e = _events()[-1]
    assert e["ok"] is False and e["err"] == "RuntimeError" and e["msg_class"] == "not_found", e
    assert e["where"] and e["where"][-1].startswith("test_diag.py:"), e
    assert "someone" not in json.dumps(e)

    # install: an uncaught exception is recorded, and a child process joins the parent's run id
    child_py = diag_env / "child.py"
    child_py.write_text(f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import diag; diag.event('child')\n")
    code = (
        f"import sys, subprocess; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import diag; diag.install('probe'); "
        f"subprocess.run([sys.executable, {str(child_py)!r}]); raise ValueError('secret text')"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ})
    assert p.returncode == 1 and "ValueError" in p.stderr  # the original hook still prints the traceback
    child, unc = _events()[-2:]
    assert child["kind"] == "child" and unc["kind"] == "uncaught" and unc["err"] == "ValueError" and unc["comp"] == "probe", (child, unc, p.stderr)
    assert child["run"] == unc["run"] and "secret" not in diag.path().read_text()

    # reading: errors group, summary has its keys
    g = diag.errors(diag.read())
    assert any(x["err"] == "RuntimeError" and x["msg_class"] == "not_found" for x in g), g
    (diag_env / "config.json").write_text(
        json.dumps({"me": "octo", "merge_method": "squash", "agents": ["claude", "x"], "require_human_approval": False, "blocking": {"team": ["a"]}})
    )
    s = diag.summary(0)
    assert {"version", "env", "config", "errors_24h", "top_errors", "spans", "automations"} <= set(s)
    assert s["config"] == {
        "me": "str",
        "merge_method": "squash",
        "agents": ["claude"],
        "require_human_approval": False,
        "blocking": "dict",
    }, s["config"]
    assert s["spans"]["cmd.gh"]["n"] == 2 and s["spans"]["cmd.gh"]["fail"] == 0.5


def test_event_log_rotation(diag_env, monkeypatch):
    monkeypatch.setattr(diag, "MAX", 2000)
    for _ in range(300):
        diag.event("fill", rc=0)
    files = sorted(p.name for p in diag.path().parent.glob("events.jsonl*"))
    assert files == ["events.jsonl", "events.jsonl.1", "events.jsonl.2", "events.jsonl.3"], files
    assert diag.path().stat().st_size < 2000 + 200


def test_doctor_checks_and_auto_timer(diag_env, monkeypatch):
    bin_ = diag_env / "bin"
    bin_.mkdir()
    (bin_ / "glab").write_text("#!/bin/sh\necho '401 Unauthorized' >&2; exit 1\n")
    (bin_ / "gh").write_text(f'#!/bin/sh\n[ "$1" = auth ] && exit 0\necho "$@" > {diag_env}/gh.args; cat > {diag_env}/gh.body; echo https://github.com/x/y/issues/9\n')
    for x in ("glab", "gh"):
        (bin_ / x).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    (diag_env / "config.json").write_text(json.dumps({"agents": ["codex"]}))
    (diag_env / "automations.json").write_text(json.dumps({"nightly": {"schedule": "0 3 * * *"}}))
    (diag_env / "auto").mkdir(exist_ok=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (diag_env / "auto" / "nightly.pid").write_text(str(dead.pid))
    (diag_env / "orchestrator-phase-1.pid").write_text("{}")  # no pid in it: skipped, not a crash

    orig_which = shutil.which  # hide a host-installed codex in /usr/bin so agent_codex sees none
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: None if c == "codex" else orig_which(c, *a, **k))
    d = {c["check"]: c for c in diag.doctor()["checks"]}
    assert d["glab_auth"] == {"check": "glab_auth", "ok": False, "rc": 1, "msg_class": "auth"}, d
    assert d["gh_auth"]["ok"] and d["agent_codex"]["ok"] is False and d["disk"]["ok"] and d["home_writable"]["ok"], d
    assert d["auto_timer"] == {"check": "auto_timer", "ok": False, "state": "missing"} and d["stale_pids"]["n"] == 1, d

    auto.tick()  # a 3am automation isn't due now, but the tick still leaves its heartbeat
    assert {c["check"]: c for c in diag.doctor()["checks"]}["auto_timer"]["state"] == "fresh"


def test_usage_rollup_and_auto_reply_and_execute(diag_env, monkeypatch):
    # usage: chat turns and automation runs roll up per harness.model; orchestrator and skipped runs aren't agent runs
    for o, c in ((1200, 5.123), (800, 3.0)):
        diag.event("chat_turn", ok=True, harness="claude", model="opus", in_tokens=9000, out_tokens=o, cost_cents=c, secs=2.0)
    diag.event("chat_turn", ok=False, harness="codex", secs=1.0)
    diag.event("automation", status="ok", engine="codex", in_tokens=500, out_tokens=50, secs=9.0)
    diag.event("automation", status="ok", engine="orchestrator", secs=9.0)
    diag.event("automation", status="skipped", secs=0.1)
    u = diag.summary(0)["usage"]
    assert u == {
        "claude.opus": {"turns": 2, "runs": 0, "in_tokens": 18000, "out_tokens": 2000, "cost_usd": 0.08123},
        "codex.default": {"turns": 1, "runs": 1, "in_tokens": 500, "out_tokens": 50, "cost_usd": 0.0},
    }, u

    # automations stream JSON: reply() keeps the reply, step titles and plain stderr lines, and reads the tokens
    claude = "\n".join(
        json.dumps(x)
        for x in [
            {"type": "system", "subtype": "init", "session_id": "s"},
            {
                "type": "result",
                "result": "All green.",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7},
                "total_cost_usd": 0.0123,
            },
        ]
    )
    text, tok = auto.reply("claude", "warning: plain stderr\n42\n" + claude)
    assert text == "warning: plain stderr\n42\nAll green." and tok == {"in_tokens": 100, "out_tokens": 7, "cost_cents": 1.23}, (text, tok)
    assert auto.reply("claude", "Error: not logged in") == ("Error: not logged in", {})
    assert auto.reply("codex", '{"type": "item.started", "item": {"type": "command_execution"}}\nboom') == ("boom", {})

    # end to end: a fake claude on PATH streams JSON; the run's record, log and event carry tokens, and a limit still falls back
    bin_ = diag_env / "bin"
    bin_.mkdir()
    (bin_ / "claude").write_text(
        f"#!/bin/sh\ncat >/dev/null\necho 'note: plain stderr' >&2\n"
        f"[ -f {diag_env}/limit ] && {{ echo \"You've hit your usage limit\" >&2; exit 1; }}\n"
        f"cat <<'J'\n{claude}\nJ\n"
    )
    (bin_ / "codex").write_text("#!/bin/sh\ncat >/dev/null\necho 'codex says done'\n")
    for x in ("claude", "codex"):
        (bin_ / x).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    (diag_env / "automations.json").write_text(json.dumps({"t": {"schedule": "0 3 * * *", "prompt": "hi", "provider": "claude", "model": "opus"}}))

    auto.execute("t")
    r = json.loads((diag_env / "auto" / "runs.jsonl").read_text().splitlines()[-1])
    assert (r["status"], r["engine"], r["in_tokens"], r["out_tokens"], r["cost_cents"], r["model"]) == ("ok", "claude", 100, 7, 1.23, "opus"), r
    assert Path(r["log"]).read_text() == "$ claude\nnote: plain stderr\nAll green.\n[exit 0]\n", Path(r["log"]).read_text()
    ev = [e for e in diag.read(0) if e["kind"] == "automation"][-1]
    assert (ev["in_tokens"], ev["out_tokens"], ev["cost_cents"], ev["model"]) == (100, 7, 1.23, "opus"), ev
    assert diag.summary(0)["usage"]["claude.opus"]["runs"] == 1
    assert diag.automations()[-1]["in_tokens"] == 100

    (diag_env / "limit").touch()
    auto.execute("t")
    r = json.loads((diag_env / "auto" / "runs.jsonl").read_text().splitlines()[-1])
    assert (r["status"], r["engine"], r["summary"]) == ("ok", "codex", "codex says done") and "in_tokens" not in r, r


def test_codex_rollout_usage_delta(diag_env, monkeypatch):
    # codex: a resumed thread's usage is the thread's total, so the turn's is the difference from its rollout's last token_count
    monkeypatch.setattr(chat, "CODEX_SESSIONS", str(diag_env / "codex"))
    (diag_env / "codex" / "2026" / "11" / "21").mkdir(parents=True)
    (diag_env / "codex" / "2026" / "11" / "21" / "rollout-x-th1.jsonl").write_text(
        json.dumps({"payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1000, "output_tokens": 100}}}}) + "\n"
    )
    n = chat.Norm()
    n.base = chat.codex_base("th1")
    n.codex({"type": "turn.completed", "usage": {"input_tokens": 1600, "output_tokens": 160}})
    assert (n.tin, n.tout) == (600, 60) and chat.codex_base("none") is None, (n.tin, n.tout)
    monkeypatch.setattr(chat, "TURN", {"harness": "codex", "model": "gpt", "t": 0, "norm": n})
    with contextlib.redirect_stdout(io.StringIO()):
        chat.emit(e="done", ok=True, text="x", context=5, window=None, ms=None)
    ev = [e for e in diag.read(0) if e["kind"] == "chat_turn"][-1]
    assert (ev["harness"], ev["in_tokens"], ev["out_tokens"]) == ("codex", 600, 60) and "cost_cents" not in ev, ev


def test_crash_reports_bundle_and_report_flow(diag_env, monkeypatch):
    bin_ = diag_env / "bin"
    bin_.mkdir()
    (bin_ / "gh").write_text(f'#!/bin/sh\n[ "$1" = auth ] && exit 0\necho "$@" > {diag_env}/gh.args; cat > {diag_env}/gh.body; echo https://github.com/x/y/issues/9\n')
    (bin_ / "glab").write_text("#!/bin/sh\nexit 1\n")  # else bundle's doctor runs a host /usr/bin/glab, which waits on gitlab.com
    for x in ("glab", "gh"):
        (bin_ / x).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")

    # crash reports: the crashed thread's frames by image name and symbol; a frame naming a path is dropped
    crash = diag_env / "crashes"
    crash.mkdir()
    (crash / "Greenlight-2026-11-21-101010.ips").write_text(
        json.dumps({"build_version": "abc1234"})
        + "\n"
        + json.dumps(
            {
                "codeSigningID": "dev.greenlight.app",
                "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"},
                "faultingThread": 1,
                "usedImages": [{"name": "Greenlight", "path": FAKE_APP_PATH}],
                "threads": [
                    {"frames": []},
                    {
                        "frames": [
                            {"imageIndex": 0, "symbol": "AppDelegate.scan()"},
                            {"imageIndex": 0, "symbol": FAKE_SYM_PATH},
                            {"imageIndex": 7, "imageOffset": 16},
                        ]
                    },
                ],
            }
        )
    )
    (crash / "Greenlight-bad.ips").write_text("not json")
    (crash / "GreenlightPhotos-2026.ips").write_text("{}\n" + json.dumps({"codeSigningID": "com.other.photos"}))
    c = diag.crashes(0)
    assert c == [{"ts": c[0]["ts"], "v": "abc1234", "exc": "EXC_BAD_ACCESS", "signal": "SIGSEGV", "frames": ["Greenlight:AppDelegate.scan()", "?:0x10"]}], c

    # export: a bundle re-checks every event, and leaves out one that now hits (a phone-shaped token, a term added to leak.re since)
    diag.event("probe", name=FAKE_PHONE)  # a token by shape, a phone number by audit's patterns
    diag.event("probe", name="quokka")
    (diag_env / "leak.re").write_text("zebracorn\nquokka\n")
    diag._leak = False
    app = [
        {"ts": 1e10, "kind": "js_error", "comp": "app", "name": "TypeError", "line": 3},
        {"ts": 1e10, "kind": "x", "comp": "app", "to": FAKE_APP_EMAIL},
    ]
    app += [{"ts": 1e10, "kind": "ssh_exit", "comp": "app", "verb": v, "rc": 2} for v in ("dash.stream", "greenlight.scan")]
    app += app[-1:]  # and an exact repeat, as when the app shares this machine
    b = diag.bundle(0, app)
    assert sorted(e["verb"] for e in b["events"] if e["kind"] == "ssh_exit") == ["dash.stream", "greenlight.scan"], b["events"][-4:]
    text = json.dumps(b)
    assert FAKE_PHONE not in text and "quokka" not in text and FAKE_APP_EMAIL not in text and "zebracorn" not in text, text[-800:]
    assert b["manifest"]["left_out"] == 1 and any(e.get("name") == "TypeError" for e in b["events"]), b["manifest"]
    assert not any("to" in e for e in b["events"] if e.get("kind") == "x")
    assert set(b) == {"manifest", "summary", "doctor", "errors", "events"} and "top_errors" not in b["summary"]
    assert b["summary"]["crashes"][0]["exc"] == "EXC_BAD_ACCESS" and "someone" not in text

    # report: the user's own text is checked too, and blocks; a preview files nothing; --execute runs gh on the protocol's repo
    r = diag.report("Crash on scan", f"my email is {FAKE_EMAIL}")
    assert r == {"ok": False, "blocked": "description", "reason": "the description contains an email address: take it out and try again"}, r
    assert diag.report("quokka broke", "")["blocked"] == "title"
    r = diag.report("Crash on scan", "It crashed after Rescan.")
    assert r["ok"] and r["preview"] and r["body"].startswith("It crashed after Rescan.") and len(r["body"]) <= diag.ISSUE_MAX
    assert Path(r["bundle"]).exists() and oct(Path(r["bundle"]).stat().st_mode & 0o777) == "0o600"
    r = diag.report("Crash on scan", "It crashed after Rescan.", execute=True)
    assert r["filed"] and r["url"] == "https://github.com/x/y/issues/9", r
    assert (diag_env / "gh.args").read_text().split()[:4] == ["issue", "create", "--repo", diag.REPO]
    assert (diag_env / "gh.body").read_text() == r["body"]
    assert diag.report("Crash on scan", "x", execute=True, shown=r["body"] + f" {FAKE_EMAIL}")["blocked"] == "report"
    diag.report("Crash on scan", "x", execute=True, shown="as previewed")
    assert (diag_env / "gh.body").read_text() == "as previewed"  # the shown body is filed, not a rebuild with newer events

    monkeypatch.setenv("PATH", "/nonexistent")
    r = diag.report("Crash on scan", "It crashed.", execute=True)
    assert r["filed"] is False and r["url"].startswith(f"https://github.com/{diag.REPO}/issues/new?"), r

    # the check fails closed: if audit can't load, nothing is exported
    monkeypatch.setitem(sys.modules, "audit", None)
    n = len(list((diag_env / "diag").glob("bundle-*")))
    for fn in (lambda: diag.bundle(0), lambda: diag.report("t", "x")):
        with pytest.raises(ImportError):
            fn()
    assert len(list((diag_env / "diag").glob("bundle-*"))) == n


def test_review_fixes_and_cmd_retry_span(diag_env, monkeypatch):
    # a codex limit is still seen as one (the first done, not turn.failed's dict)
    codex_limit = "\n".join(
        json.dumps(x)
        for x in [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "error", "message": "You've hit your usage limit"},
            {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}},
        ]
    )
    txt, _ = auto.reply("codex", codex_limit)
    assert auto.LIMIT_RE.search(txt), txt

    # an empty salt (a crash mid-write) is replaced, not used
    (diag_env / "diag").mkdir(exist_ok=True)
    (diag_env / "diag" / "salt").write_bytes(b"")
    assert len(diag.salt()) == 32 and diag.salt() == diag.salt()

    # a malformed line doesn't break the readers
    with open(diag.path(), "a") as f:
        f.write(json.dumps({"ts": "x", "kind": "cmd"}) + "\n" + json.dumps({"ts": 9e12, "secs": 1}) + "\n")
    diag.summary(0)
    diag.errors(diag.read())
    diag.spans(diag.read())

    # a too-long report keeps its fence and details closed
    fake = {"manifest": {"version": "v", "events": 0, "left_out": 0}, "summary": {"x": "y" * (diag.ISSUE_MAX + 10)}, "errors": [], "events": []}
    body = diag.issue_body("t", fake)
    assert len(body) <= diag.ISSUE_MAX and body.endswith("```\n</details>\n"), body[-80:]

    # a missing body file is a JSON reason, not a traceback
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts/diag.py"), "report", "--title", "t", "--body-file", "/nonexistent/x"],
        capture_output=True,
        text=True,
        env={**os.environ, "GREENLIGHT_HOME": str(diag_env)},
    )
    assert p.returncode == 1 and json.loads(p.stdout)["ok"] is False and "Traceback" not in p.stderr, (p.stdout, p.stderr)

    # a cmd span that fails once (transient) and then succeeds is ok, with no stale rc or msg_class
    flag = diag_env / "flaky"
    monkeypatch.setattr(greenlight.time, "sleep", lambda _s: None)
    out = greenlight.run(
        ["/bin/sh", "-c", f'PATH=/bin:/usr/bin; [ -e {flag} ] && echo fine || {{ touch {flag}; echo "502 Bad Gateway" >&2; exit 1; }}'],
        tries=2,
    )
    assert out.strip() == "fine"
    last = [e for e in diag.read() if e.get("kind") == "cmd"][-1]
    assert last.get("ok") is True and last["tries"] == 2 and "rc" not in last and "msg_class" not in last, last


def test_stage_summary(diag_env, monkeypatch):
    # #208 phase 0: the app's stage draws roll up by source and draw, with per-turn times to the first card and the last draw
    app = diag_env / "app.jsonl"
    monkeypatch.setattr(diag, "app_path", lambda: app)
    rows = [
        {"draw": "wait", "by": "app", "turn": 1, "n": 1, "at": 0.01},  # the wait card isn't the first card
        {"draw": "spec", "by": "dwim", "card": "table", "turn": 1, "n": 2, "at": 1.5},
        {"draw": "text", "by": "chief", "turn": 1, "n": 3, "at": 9.0},
        {"draw": "wait", "by": "app", "turn": 2, "n": 1, "at": 0.0},
        {"draw": "spec", "by": "fast", "card": "list", "turn": 2, "n": 2, "at": 4.0, "mid": True, "req_secs": 1.2},
        {"draw": "spec", "by": "fast", "card": "stat", "turn": 2, "n": 3, "at": 6.0, "req_secs": 1.4},
        {"draw": "spec", "by": "app", "card": "list"},  # a workspace card outside any turn: counted, not timed
        {"draw": "wait", "by": "app", "turn": 3, "n": 1, "at": 0.0},  # stopped a second in: only wait cards, never timed
        {"draw": "wait", "by": "app", "turn": 3, "n": 2, "at": 1.0},
    ]
    app.write_text("".join(json.dumps({"ts": 100 + i, "comp": "app", "run": "abcd1234", "kind": "stage", **r}) + "\n"
                           for i, r in enumerate(rows)))
    s = diag.summary(0)["stage"]
    assert s == {
        "draws": 9, "by": {"app": 5, "dwim": 1, "chief": 1, "fast": 2}, "draw": {"wait": 4, "spec": 4, "text": 1},
        "turns": 3, "fast_turns": 1,
        "first_draw": {"n": 2, "p50": 4.0, "p95": 4.0}, "last_draw": {"n": 2, "p50": 9.0, "p95": 9.0},
    }, s
    assert "stage" not in diag.summary(0)["spans"]  # `at` isn't a span's `secs`
    # the new string fields pass the privacy filter as tokens; free text under them doesn't
    assert diag.clean({"draw": "text", "by": "chief", "card": "table"}) == {"draw": "text", "by": "chief", "card": "table"}
    assert diag.clean({"card": "Fix login for ACME users"}) == {"dropped": 1}
    assert diag.stage([]) == {"draws": 0, "by": {}, "draw": {}, "turns": 0, "fast_turns": 0,
                              "first_draw": {"n": 0}, "last_draw": {"n": 0}}
