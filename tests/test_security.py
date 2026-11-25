"""Security & cross-layer contract tests for Greenlight (Python ↔ Swift ↔ HTML)."""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SEC_HOME = tempfile.mkdtemp(prefix="gl-sec-")
atexit.register(shutil.rmtree, _SEC_HOME, True)
os.environ["GREENLIGHT_HOME"] = _SEC_HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_safety
import decide


def swift_text():
    return "\n".join(p.read_text() for p in check_safety.swift_files())


# 1. decide.post rejects non-HTTP(S) URL schemes and non-loopback cleartext http://
for bad_url in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "http://api.example.com/v1"):
    try:
        decide.post(bad_url, {}, {}, 1)
    except ValueError as e:
        assert "unsupported URL scheme" in str(e) or "cleartext http://" in str(e), e
    else:
        raise AssertionError(f"decide.post accepted unsafe URL: {bad_url}")

# 1b. decide.finish rejects Python booleans (isinstance(True, int) == True)
try:
    decide.finish({"type": "bool"}, True, "jev")
except ValueError:
    pass
else:
    raise AssertionError("decide.finish accepted bool as probability")

# 1c. harness.argv: bypass requires strict True, tools=False disables tools/bypass, interactive uses --
import harness
harness.CONFIG.setdefault("harnesses", {})["claude"] = {"bypass": "false"}
cmd_safe, _ = harness.argv("claude", "hi")
assert "--permission-mode" in cmd_safe and "acceptEdits" in cmd_safe, cmd_safe
harness.CONFIG["harnesses"]["claude"] = {"bypass": True}
cmd_toolless, _ = harness.argv("claude", "hi", tools=False)
assert "--tools" in cmd_toolless and "" in cmd_toolless and "bypassPermissions" not in cmd_toolless, cmd_toolless
assert "--" in harness.interactive("claude", "--dangerously-skip-permissions")
harness.CONFIG["harnesses"].pop("claude", None)

# 1d. audit.py: non-LF line separators cannot bypass published_lines / secrets / test_signals, and origin_push_url blocks options/schemes
import audit
cr_diff = "diff --git a/a.py b/a.py\n@@ -0,0 +1,2 @@\n+foo\rdiff --git a/b.py b/b.py\n+glpat-1234567890abcdefghij\n"
assert audit.secrets(cr_diff, diff=True), "CR diff header injection bypassed audit.secrets!"
ls_diff = "diff --git a/a.py b/a.py\n@@ -0,0 +1,1 @@\n+hello\u2028glpat-1234567890abcdefghij\n"
assert audit.secrets(ls_diff, diff=True), "Unicode line separator bypassed audit.secrets!"
try:
    audit.live_thread_snapshot("o/r", 1, "github", "@/etc/passwd")
except ValueError:
    pass
else:
    raise AssertionError("audit.live_thread_snapshot accepted @file thread_id")
from unittest import mock
for bad_remote in ("--upload-pack=id@github.com:o/r.git", "ext://github.com/o/r.git", "file://github.com/o/r.git"):
    with mock.patch.object(audit, "git", return_value=bad_remote + "\n"):
        assert audit.origin_push_url("/tmp", "o/r", "github") is None, f"accepted unsafe push URL: {bad_remote}"

# 1e. diag.py: leak(strict=True) fails closed on broken leak.re; clean() rejects free-text keys and NaN
import diag
with tempfile.TemporaryDirectory() as dtmp:
    old_home, old_leak = os.environ.get("GREENLIGHT_HOME"), diag._leak
    try:
        os.environ["GREENLIGHT_HOME"] = dtmp
        (Path(dtmp) / "leak.re").write_text("SECRET_WORD\n[unclosed\n")
        diag._leak = False
        try:
            diag.gate()
        except re.error:
            pass
        else:
            raise AssertionError("diag.gate() failed open on invalid leak.re!")
        diag._leak = False
        (Path(dtmp) / "leak.re").unlink()
        c = diag.clean({"kind": "x", "/Users/alice/secret": 1, "bad_float": float("nan"), "secs": 1.25})
        assert "/Users/alice/secret" not in c and "bad_float" not in c and c["secs"] == 1.25, c
    finally:
        if old_home is None:
            os.environ.pop("GREENLIGHT_HOME", None)
        else:
            os.environ["GREENLIGHT_HOME"] = old_home
        diag._leak = old_leak

# 1f. auto.py rejects path traversal automation names
import auto
for bad_name in ("../pwn", "/tmp/pwn", "a/b", "-flag"):
    try:
        auto.valid_name(bad_name)
    except ValueError:
        pass
    else:
        raise AssertionError(f"auto.valid_name accepted {bad_name!r}")

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
    env = {**os.environ, "GREENLIGHT_HOME": str(home), "GREENLIGHT_ADE": "orca", "GREENLIGHT_TMUX_SOCKET": f"gl-sec-{os.getpid()}",
           "GREENLIGHT_USAGE": "off", "ORCA_CLI_COMMAND": str(orca), "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}  # the socket: never the real Basement's
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

# 5. Swift shellPath preserves ~ expansion & no stray UserDefaults.standard leaks remain outside Config/cleanup
config_swift = (ROOT / "app" / "Sources" / "Config.swift").read_text()
assert "func shellPath(" in config_swift and "shellPath(greenlightHome)" in config_swift and "shellPath(greenlightCode)" in config_swift
for sf in check_safety.swift_files():
    for i, line in enumerate(sf.read_text().splitlines(), 1):
        if "UserDefaults.standard" in line:
            allowed = (sf.name == "Config.swift" and "let ud:" in line) or (sf.name == "Config.swift" and "removePersistentDomain" in line) \
                or (sf.name == "App.swift" and "NSWindow Frame" in line)  # AppKit keeps window frames there, --uitest or not
            assert allowed, f"stray UserDefaults.standard in {sf.name}:{i}: {line.strip()}"

# 6. audit.py review handles argv cleanly and passes diff= (not outbound=) to contracts()
assert audit.main(["review", "--path", str(ROOT), "--deterministic-only"]) == 0
sample_diff = "diff --git a/scripts/foo.py b/scripts/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
with mock.patch.object(audit, "branch_diff", return_value=sample_diff), \
        mock.patch.object(audit, "contracts", return_value={"ok": True, "failed": []}) as mc:
    assert audit.main(["review", "--path", str(ROOT), "--target", "main"]) == 0
    assert mc.called
    assert mc.call_args.kwargs.get("diff") == sample_diff, f"expected diff= kwarg, got {mc.call_args}"
    assert "outbound" not in mc.call_args.kwargs, f"raw diff must not be passed as outbound=: {mc.call_args}"
with mock.patch.object(audit, "branch_diff", return_value=None):
    assert audit.main(["review", "--path", str(ROOT), "--target", "main"]) == 4

# 7. ade_basement: atomic remember(), prefix-boundary worktrees(), -- end-of-options send(), 0o600 O_EXCL launch() brief
import ade_basement  # noqa: E402
import ade_herdr  # noqa: E402
import dash  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    base_tmp = Path(td)
    with mock.patch.object(ade_basement, "REPOS", base_tmp / "repos.json"), \
            mock.patch.object(ade_basement, "BRIEFS", base_tmp / "briefs"):
        ade_basement.remember("/repo/a")
        ade_basement.remember("/repo/a")
        assert json.loads((base_tmp / "repos.json").read_text()) == ["/repo/a"]
        fake_panes = [
            {"handle": "%1", "cwd": "/w/app/sub", "title": "✳ Done", "at": "1700000000"},
            {"handle": "%2", "cwd": "/w/app2", "title": "⠋ Run", "at": "1700000001"},
        ]
        with mock.patch.object(ade_basement, "panes", return_value=fake_panes), \
                mock.patch.object(ade_basement, "goals", return_value={"/w/app": "g1"}), \
                mock.patch.object(ade_basement, "git_info", side_effect=lambda p: {"repoId": "/repo/a", "path": p}), \
                mock.patch.object(ade_basement, "git", return_value="worktree /w/app\nworktree /w/app2\n"), \
                mock.patch.object(ade_basement, "screen", return_value=["❯ "]):
            wts = {w["path"]: w for w in ade_basement.worktrees()}
            assert [a["handle"] for a in wts["/w/app"]["agents"]] == ["%1"]
            assert [a["handle"] for a in wts["/w/app2"]["agents"]] == ["%2"]
        tmux_calls = []
        with mock.patch.object(ade_basement, "tmux", side_effect=lambda *a: (tmux_calls.append(a), "%9")[1]), \
                mock.patch.object(ade_basement.subprocess, "run") as mrun, \
                mock.patch.object(ade_basement.time, "sleep"):
            ade_basement.send("%1", "-l-flag", False)
            assert tmux_calls[-1] == ("send-keys", "-t", "%1", "-l", "--", "-l-flag")
            ade_basement.send("%1", "hello\nworld", True)
            assert any("load-buffer" in c.args[0] for c in mrun.call_args_list)
            assert any("delete-buffer" in c.args[0] for c in mrun.call_args_list)
            ade_basement.launch("/w/app", "claude", "line1\nline2")
            briefs = list((base_tmp / "briefs").glob("*.md"))
            assert len(briefs) == 1 and (briefs[0].stat().st_mode & 0o777) == 0o600

    # 8. ade_herdr: -- option separator on send() and 0o600 O_EXCL brief in launch()
    with mock.patch.object(ade_herdr, "BRIEFS", base_tmp / "hbriefs"):
        hcalls = []
        with mock.patch.object(ade_herdr, "agents", return_value={"p1": {"pane_id": "p1"}}), \
                mock.patch.object(ade_herdr, "herdr", side_effect=lambda *a, **kw: (hcalls.append(a), {})[1]):
            ade_herdr.send("p1", "--evil", True)
            ade_herdr.send("p2", "--evil", True)
            ade_herdr.send("p2", "--evil", False)
            assert hcalls == [
                ("agent", "prompt", "p1", "--", "--evil"),
                ("pane", "run", "p2", "--", "--evil"),
                ("pane", "send-text", "p2", "--", "--evil"),
            ]
            res = ade_herdr.launch("ws1", "claude", "first\nsecond", pane="p:1")
            assert res == {"terminal": {"handle": "p:1"}}
            hbriefs = list((base_tmp / "hbriefs").glob("*.md"))
            assert len(hbriefs) == 1 and (hbriefs[0].stat().st_mode & 0o777) == 0o600

    # 9. dash._codex_entries & dash.transcript_files: defensive JSON parsing and transcript discovery
    fake_home = base_tmp / "fakehome"
    ws_path = "/w/demo"
    cdir = fake_home / ".claude/projects" / "-w-demo"
    cdir.mkdir(parents=True)
    (cdir / "s1.jsonl").write_text("{}\n")
    xdir = fake_home / ".codex/sessions/2026/11/21"
    xdir.mkdir(parents=True)
    rollout = xdir / "rollout-1.jsonl"
    rollout.write_text("\n".join([
        json.dumps({"payload": {"cwd": ws_path}}),
        "not json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"payload": "not-a-dict"}),
        json.dumps({"payload": {"type": "message", "role": "user", "content": ["bad", {"text": "<sys>"}, {"text": "hi"}]}}),
        json.dumps({"payload": {"type": "function_call", "name": "bash", "arguments": "ls -la"}}),
    ]) + "\n")
    entries = dash._codex_entries(rollout)
    assert entries == [("user", "hi"), ("tool", "bash ls -la")], entries
    assert dash._codex_entries(base_tmp / "missing.jsonl") == []
    with mock.patch.object(dash.Path, "home", return_value=fake_home):
        tfiles = dash.transcript_files(ws_path)
        assert {k for _, k in tfiles} == {"claude", "codex"}, tfiles

print("ok  test_security.py passed")
