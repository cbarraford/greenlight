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
print("test_diag ok")
