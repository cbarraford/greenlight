#!/usr/bin/env python3
"""diag.py: free text never reaches the trail, hashes correlate, spans and uncaught errors record, rotation caps size.
python3 tests/test_diag.py"""
import json
import os
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
# export: a bundle re-checks every event, and leaves out one that now hits (a phone-shaped token, a term added to leak.re since)
diag.MAX = 5 << 20
diag.event("probe", name="555-234-5678")  # a token by shape, a phone number by audit's patterns
diag.event("probe", name="quokka")
(Path(HOME) / "leak.re").write_text("zebracorn\nquokka\n")
diag._leak = False
app = [{"ts": 1e10, "kind": "js_error", "comp": "app", "name": "TypeError", "line": 3}, {"ts": 1e10, "kind": "x", "comp": "app", "to": "a@b.co"}]
b = diag.bundle(0, app)
text = json.dumps(b)
assert "555-234-5678" not in text and "quokka" not in text and "a@b.co" not in text and "zebracorn" not in text, text[-800:]
assert b["manifest"]["left_out"] == 1 and any(e.get("name") == "TypeError" for e in b["events"]), b["manifest"]
assert not any("to" in e for e in b["events"] if e.get("kind") == "x")  # the app's field failed the schema, so it went
assert set(b) == {"manifest", "summary", "errors", "events"} and "top_errors" not in b["summary"]

# report: the user's own text is checked too, and blocks; a preview files nothing; --execute runs gh on the protocol's repo
r = diag.report("Crash on scan", "my email is dev@corp.test")
assert r == {"ok": False, "blocked": "description", "reason": "the description contains an email address: take it out and try again"}, r
assert diag.report("quokka broke", "")["blocked"] == "title"
r = diag.report("Crash on scan", "It crashed after Rescan.")
assert r["ok"] and r["preview"] and r["body"].startswith("It crashed after Rescan.") and len(r["body"]) <= diag.ISSUE_MAX
assert Path(r["bundle"]).exists() and oct(Path(r["bundle"]).stat().st_mode & 0o777) == "0o600"
bin_ = Path(HOME, "bin"); bin_.mkdir()
(bin_ / "gh").write_text(f"#!/bin/sh\necho \"$@\" > {HOME}/gh.args; cat > {HOME}/gh.body; echo https://github.com/x/y/issues/9\n")
(bin_ / "gh").chmod(0o755)
os.environ["PATH"] = f"{bin_}:{os.environ['PATH']}"
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
