"""Security & cross-layer contract tests for Greenlight (Python ↔ Swift ↔ HTML)."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_safety
import decide


def swift_text():
    return "\n".join(p.read_text() for p in check_safety.swift_files())


# 1. decide.post rejects non-HTTP(S) URL schemes (prevents file:// SSRF/read)
for bad_url in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"):
    try:
        decide.post(bad_url, {}, {}, 1)
    except ValueError as e:
        assert "unsupported URL scheme" in str(e), e
    else:
        raise AssertionError(f"decide.post accepted unsafe scheme: {bad_url}")

# 2. Swift ↔ HTML bridge contract: every cmd emitted by dashboard.html / terminal.html
#    must be handled by Swift's userContentController (mc) / termMessage (term).
swift = swift_text()

mc_handled = set(re.findall(r'cmd\s*==\s*"([^"]+)"', swift))
for m in re.finditer(r'\[([^\]]+)\]\.contains\(cmd\)', swift):
    mc_handled.update(re.findall(r'"([^"]+)"', m.group(1)))

dash_html = (ROOT / "app" / "dashboard.html").read_text()
dash_cmds = set(re.findall(r'post\(\s*\{\s*cmd\s*:\s*"([^"]+)"', dash_html))
missing_mc = dash_cmds - mc_handled
assert not missing_mc, f"dashboard.html posts commands not handled in Swift mc bridge: {sorted(missing_mc)}"

term_match = re.search(r"func termMessage\(.*?\n    \}\n", swift, re.S)
assert term_match, "could not locate termMessage in Swift sources"
term_handled = set(re.findall(r'case\s+"([^"]+)"\s*:', term_match.group(0)))

term_html = (ROOT / "app" / "terminal.html").read_text()
term_cmds = set(re.findall(r'post\(\s*\{\s*cmd\s*:\s*"([^"]+)"', term_html))
missing_term = term_cmds - term_handled
assert not missing_term, f"terminal.html posts commands not handled in Swift termMessage: {sorted(missing_term)}"

# 3. Adversarial input resilience in dash.py snapshot generation (JSON-safe, no shell execution)
with tempfile.TemporaryDirectory() as tmp:
    pwn = Path(tmp) / "pwned"
    nasty = f'"><img src=x onerror=alert(1)> $(touch {pwn}) `touch {pwn}` \'; echo x'
    home = Path(tmp) / "home"
    home.mkdir()
    bindir = Path(tmp) / "bin"
    bindir.mkdir()
    orca = bindir / "orca"
    orca.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'ok': True, 'result': {'worktrees': [{"
                    "'worktreeId': 'wt-1', 'displayName': " + json.dumps(nasty) + ", 'path': '/w/safe-ws', "
                    "'repo': 'widgets', 'branch': " + json.dumps(nasty) + ", 'comment': " + json.dumps(nasty) + ", "
                    "'agents': [{'state': 'waiting', 'agentType': 'claude', 'lastAssistantMessage': " + json.dumps(nasty) + "}]}]}}))\n")
    orca.chmod(0o755)
    last = {
        "ts": 1700000000,
        "rows": [{
            "id": "wt-1",
            "name": nasty,
            "path": "/w/safe-ws",
            "project": f"acme/{nasty}",
            "goal": nasty,
            "branch": nasty,
            "agent": {"state": "waiting", "type": "claude", "last": nasty, "since": 1700000000000},
            "mr": {"ref": "!99", "title": nasty, "state": "opened", "web_url": "https://example.invalid/mr/99"},
            "next": {"step": "review", "owner": "me", "action": nasty, "cmd": []},
        }],
    }
    (home / "last.json").write_text(json.dumps(last))
    env = {**os.environ, "GREENLIGHT_HOME": str(home), "GREENLIGHT_ADE": "orca",
           "ORCA_CLI_COMMAND": str(orca), "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "dash.py"), "snapshot"],
                       capture_output=True, text=True, env=env, timeout=20)
    assert p.returncode == 0, f"dash.py snapshot failed: {p.stderr}"
    snap = json.loads(p.stdout)
    assert isinstance(snap.get("pucks"), list) and len(snap["pucks"]) == 1, snap
    assert not pwn.exists(), "adversarial string executed shell command!"

# 4. Path traversal rejected by dash.py tree commands
p = subprocess.run([sys.executable, str(ROOT / "scripts" / "dash.py"), "tree", "rm", "/tmp/../../etc/passwd"],
                   capture_output=True, text=True, timeout=20)
out = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"error": p.stderr}
assert p.returncode != 0 or out.get("error"), f"path traversal was not rejected: {p.stdout} {p.stderr}"

print("ok  test_security.py passed")
