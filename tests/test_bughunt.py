#!/usr/bin/env python3
"""Unit tests for scripts/bughunt.py and the extended stress/chaos fixtures in scripts/uitest.py."""
import argparse
import atexit
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Isolate GREENLIGHT_HOME before importing Greenlight modules (AGENTS.md rule).
_TMP_HOME = Path(tempfile.mkdtemp(prefix="gl-test-bughunt-home-"))
atexit.register(shutil.rmtree, _TMP_HOME, True)
os.environ["GREENLIGHT_HOME"] = str(_TMP_HOME)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import bughunt  # noqa: E402
import uitest  # noqa: E402


def test_normalize_and_fingerprint():
    a = "TypeError at /var/folders/ab/cd/T/gl-uitest-12ab34cd/web/dashboard.html pid=1234 0xdeadbeef ts=1700000000123"
    b = "TypeError at /tmp/gl-uitest-ff00aa11/web/dashboard.html pid=9876 0x1234abcd ts=1700000099999"
    assert bughunt.normalize_error(a) == bughunt.normalize_error(b), (bughunt.normalize_error(a), bughunt.normalize_error(b))
    assert bughunt.make_fingerprint("js_error", a, "app/dashboard.html") == bughunt.make_fingerprint("js_error", b, "app/dashboard.html")
    assert bughunt.make_fingerprint("js_error", a) != bughunt.make_fingerprint("crash", a)


def test_rank_dedupe_and_top2_selection():
    findings = [
        # Soft visual issue -> never auto-PR
        bughunt.make_finding(
            oracle="visual_issue", track="llm_diff", fixture="default",
            title="Minor alignment glitch", summary="Button 2px off", reproducible=True,
        ),
        # Non-reproducible crash -> digest only, never auto-PR
        bughunt.make_finding(
            oracle="crash", track="fuzz", fixture="chaos",
            title="Flaky exit", summary="One-off exit rc=-11", reproducible=False,
        ),
        # Lower-priority reproducible console error
        bughunt.make_finding(
            oracle="console_error", track="fuzz", fixture="scale",
            title="Console error on scale", summary="console.error: missing key", reproducible=True,
        ),
        # Same JS error discovered on BOTH default and scale -> merged + cross-fixture boost
        bughunt.make_finding(
            oracle="js_error", track="fuzz", fixture="default",
            title="TypeError in dashboard", summary="Cannot read properties of null (reading 'foo')",
            where="app/dashboard.html", files=["app/dashboard.html"], reproducible=True,
        ),
        bughunt.make_finding(
            oracle="js_error", track="fuzz", fixture="scale",
            title="TypeError in dashboard", summary="Cannot read properties of null (reading 'foo')",
            where="app/dashboard.html", files=["app/dashboard.html"], reproducible=True,
        ),
        # High-priority security bug
        bughunt.make_finding(
            oracle="security", track="fuzz", fixture="adversarial",
            title="XSS in dashboard", summary="window.__pwned became 1",
            where="app/dashboard.html", reproducible=True,
        ),
        # High-priority baseline regression (already has open PR -> should be skipped!)
        bughunt.make_finding(
            oracle="baseline_regression", track="baseline", fixture="baseline",
            title="test-py failed", summary="make test-py exited 1", where="test-py", reproducible=True,
        ),
        # Medium-high python exception
        bughunt.make_finding(
            oracle="python_exception", track="fuzz", fixture="corrupt",
            title="KeyError in dash.py", summary="dash_error: KeyError in dash",
            where="scripts/dash.py:412", files=["scripts/dash.py"], reproducible=True,
        ),
    ]

    ranked = bughunt.merge_and_rank_findings(findings, recent_files={"app/dashboard.html"})
    # 8 raw -> 7 unique (the two identical JS errors merged)
    assert len(ranked) == 7, f"expected 7 merged findings, got {len(ranked)}"

    merged_js = next(f for f in ranked if f["oracle"] == "js_error")
    assert merged_js["fixtures"] == ["default", "scale"], merged_js["fixtures"]
    assert merged_js["recent_commit_overlap"] is True

    # Simulate the baseline_regression already having an open PR
    open_fp = next(f["fingerprint"] for f in ranked if f["oracle"] == "baseline_regression")
    candidates = bughunt.select_pr_candidates(ranked, existing_fps={open_fp}, known_fps=set())

    # Only reproducible hard oracles minus existing_fps: security, js_error (boosted), python_exception, console_error
    assert [c["oracle"] for c in candidates] == ["security", "js_error", "python_exception", "console_error"], [
        (c["oracle"], c["priority"]) for c in candidates
    ]

    # Verify strict Top-2 cap in open_top_draft_prs (dry-run)
    run_dir = Path(tempfile.mkdtemp(prefix="gl-bughunt-run-"))
    try:
        outcomes = bughunt.open_top_draft_prs(
            ranked, repo_dir=ROOT, base_ref="HEAD", run_dir=run_dir,
            max_prs=2, dry_run=True, existing_fps={open_fp},
        )
        assert len(outcomes) == 2, outcomes
        assert outcomes[0]["fingerprint"] == candidates[0]["fingerprint"]  # security (highest)
        assert outcomes[1]["fingerprint"] == candidates[1]["fingerprint"]  # boosted js_error (2nd highest)

        # If #1 fails verification, fall through to #3 so we still get top 2 valid Draft PRs!
        call_order = []

        def fake_patch(finding, **_kw):
            call_order.append(finding["oracle"])
            if finding["oracle"] == "security":
                return {"fingerprint": finding["fingerprint"], "title": finding["title"], "priority": finding["priority"],
                        "status": "verify_failed", "pr_url": None, "note": "no repro"}
            return {"fingerprint": finding["fingerprint"], "title": finding["title"], "priority": finding["priority"],
                    "status": "opened", "pr_url": f"https://example.invalid/pr/{finding['oracle']}", "note": "ok"}

        with mock.patch.object(bughunt, "patch_and_open_draft_pr", side_effect=fake_patch):
            out2 = bughunt.open_top_draft_prs(
                ranked, repo_dir=ROOT, base_ref="HEAD", run_dir=run_dir,
                max_prs=2, dry_run=False, existing_fps={open_fp},
            )
        assert call_order == ["security", "js_error", "python_exception"], call_order
        assert sum(1 for o in out2 if o["status"] == "opened") == 2
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_shrink_trace_ddmin():
    steps = [{"seq": i, "op": "action", "args": {"name": f"step-{i}"}} for i in range(40)]
    # Bug triggers iff BOTH step-7 and step-29 are present in order
    def triggers(cand):
        names = [s["args"]["name"] for s in cand]
        return "step-7" in names and "step-29" in names and names.index("step-7") < names.index("step-29")

    minimal = bughunt.shrink_trace(steps, triggers, max_attempts=60)
    names = [s["args"]["name"] for s in minimal]
    assert names == ["step-7", "step-29"], f"expected minimal 2-step trace, got {names}"


def test_fixtures_and_offline_stubs():
    for fx in uitest.FIXTURES:
        tmp = Path(tempfile.mkdtemp(prefix=f"gl-fx-{fx}-"))
        try:
            home, bindir, webdir = uitest._make_fixture(tmp, fx)
            assert (home / "config.json").is_file()
            assert (home / "last.json").is_file()
            assert (webdir / "dashboard.html").is_file()
            assert (webdir / "terminal.html").is_file()
            cfg = json.loads((home / "config.json").read_text())
            # Every harness binary must be pinned to the isolated offline stub
            for h_name in ("claude", "codex", "gemini", "opencode", "pi", "agy"):
                stub = Path(cfg["harnesses"][h_name]["bin"])
                assert stub.parent == bindir and os.access(stub, os.X_OK), (fx, h_name, stub)

            if fx == "scale":
                last = json.loads((home / "last.json").read_text())
                assert len(last["rows"]) == 50
            if fx == "corrupt":
                assert (home / "state" / "wt-ok.json").read_text() == "{truncated json"
            if fx == "chaos":
                # Dead orca stub is gone; glab/gh chaos stubs emit rate-limit 429 on cycle 3
                assert not (bindir / "orca").exists()
                glab = str(bindir / "glab")
                r1 = subprocess.run([glab, "mr", "list"], capture_output=True, text=True)
                r2 = subprocess.run([glab, "mr", "list"], capture_output=True, text=True)
                r3 = subprocess.run([glab, "mr", "list"], capture_output=True, text=True)
                assert r1.returncode == 0 and r2.returncode == 0
                assert r3.returncode == 1 and "429" in r3.stderr
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_perf_oracles():
    history = [
        {"soak": {"rss_max_mb": 120.0, "rpc_p95_ms": 40.0}},
        {"soak": {"rss_max_mb": 125.0, "rpc_p95_ms": 45.0}},
        {"soak": {"rss_max_mb": 122.0, "rpc_p95_ms": 42.0}},
    ]
    # Healthy metrics -> 0 findings
    healthy = {"rss_start_mb": 115.0, "rss_end_mb": 124.0, "rss_max_mb": 126.0, "rss_delta_mb": 9.0,
               "fds_start": 30, "fds_end": 32, "fds_delta": 2, "rpc_p95_ms": 48.0}
    assert bughunt.evaluate_perf_oracles(healthy, history) == []

    # Leaking & slow metrics -> flags RSS leak, FD leak, baseline RSS jump, RPC p95 jump
    bad = {"rss_start_mb": 120.0, "rss_end_mb": 230.0, "rss_max_mb": 235.0, "rss_delta_mb": 110.0,
           "fds_start": 30, "fds_end": 85, "fds_delta": 55, "rpc_p95_ms": 520.0}
    hits = bughunt.evaluate_perf_oracles(bad, history)
    assert len(hits) == 4, [h["title"] for h in hits]
    assert all(h["oracle"] == "perf_regression" and h["auto_pr"] for h in hits)


def test_diag_reader_and_llm_parser():
    tmp = Path(tempfile.mkdtemp(prefix="gl-diag-test-"))
    try:
        (tmp / "diag").mkdir()
        (tmp / "diag" / "app.jsonl").write_text(
            json.dumps({"kind": "hang", "secs": 3.4, "comp": "app"}) + "\n"
            + json.dumps({"kind": "js_error", "page": "dashboard", "comp": "app"}) + "\n"
        )
        (tmp / "diag" / "events.jsonl").write_text(
            json.dumps({"kind": "dash_error", "err": "KeyError", "msg_class": "other", "comp": "dash", "where": ["dash.py:99:snap"]}) + "\n"
        )
        hits = bughunt._read_isolated_diag_errors(tmp)
        oracles = {h["oracle"] for h in hits}
        assert oracles == {"hang", "js_error", "python_exception"}, hits
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    sample = 'Some prose\n```bughunt-json\n[{"title":"Broken filter","oracle":"commit_regression","summary":"Filter throws"}]\n```\nDone.'
    parsed = bughunt.parse_bughunt_json(sample)
    assert len(parsed) == 1 and parsed[0]["oracle"] == "commit_regression"
    assert bughunt.parse_bughunt_json("no json here") == []
    assert bughunt.parse_bughunt_json("```bughunt-json\n{bad\n```") == []


def test_launchd_plist_and_report_and_trace_io():
    xml = bughunt.build_launchd_plist(ROOT, hour=3, minute=15, max_prs=2, with_vm=True)
    pl = plistlib.loads(xml.encode())
    assert pl["Label"] == bughunt.LAUNCHD_LABEL
    assert pl["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert pl["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]
    assert "--max-prs" in pl["ProgramArguments"] and "--with-vm" in pl["ProgramArguments"]

    run_dir = Path(tempfile.mkdtemp(prefix="gl-report-test-"))
    try:
        tr = [{"seq": 0, "op": "action", "args": {"name": "floor", "to": "center"}}]
        tp = bughunt.save_trace_file(run_dir / "t.jsonl", "scale", tr)
        fx, loaded = bughunt.load_trace_file(tp)
        assert fx == "scale" and len(loaded) == 1 and loaded[0]["args"]["to"] == "center"

        f1 = bughunt.make_finding(
            oracle="js_error", track="fuzz", fixture="default",
            title="Test error", summary="boom", trace_path=str(tp), reproducible=True,
        )
        rp = bughunt.write_report(run_dir, [f1], [{"fingerprint": f1["fingerprint"], "priority": f1["priority"],
                                                   "title": f1["title"], "status": "dry_run", "pr_url": None, "note": "ok"}],
                                  {"soak": {"rss_start_mb": 100, "rss_end_mb": 102, "rss_max_mb": 104, "rss_delta_mb": 2}},
                                  [{"sha": "abc1234", "subject": "feat", "files": ["app/dashboard.html"]}], max_prs=2)
        text = rp.read_text()
        assert "Nightly Bug Hunt Report" in text and "Test error" in text
        assert (bughunt.bughunt_home() / "latest.md").is_file()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_verify_patch_requires_test_file():
    # Create a tiny temporary git repo with a production edit only (no tests/ edit) -> must fail repro-first gate
    wt = Path(tempfile.mkdtemp(prefix="gl-wt-verify-"))
    try:
        subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
        subprocess.run(["git", "config", "user.email", "bughunt-test"], cwd=str(wt), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(wt), check=True)
        (wt / "scripts").mkdir()
        (wt / "scripts" / "foo.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(wt), check=True)
        base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt), capture_output=True, text=True, check=True).stdout.strip()

        # Modify production file only, no tests/
        (wt / "scripts" / "foo.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "prod only"], cwd=str(wt), check=True)

        ok, msg = bughunt.verify_patch_worktree(wt, bughunt.make_finding(
            oracle="js_error", track="fuzz", fixture="default", title="t", summary="s", reproducible=True,
        ), base_ref=base_sha)
        assert not ok and "no test file" in msg.lower(), (ok, msg)

        # Multi-commit case: commit 1 adds test, commit 2 edits prod -> base_ref..HEAD MUST detect the test file!
        (wt / "tests").mkdir()
        (wt / "tests" / "test_foo.py").write_text("assert True\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add repro test"], cwd=str(wt), check=True)
        (wt / "scripts" / "foo.py").write_text("x = 3\n")
        subprocess.run(["git", "add", "."], cwd=str(wt), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "fix prod"], cwd=str(wt), check=True)

        # Pass Gate 1 (test presence) across 2 commits; fail only at Gate 2 because dummy repo has no check_safety.py
        ok2, msg2 = bughunt.verify_patch_worktree(wt, bughunt.make_finding(
            oracle="js_error", track="fuzz", fixture="default", title="t", summary="s", reproducible=True,
        ), base_ref=base_sha)
        assert not ok2 and "no test file" not in msg2.lower(), (ok2, msg2)
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_remote_quoting_plist_escaping_and_pid_lock():
    assert bughunt._quote_remote_path("~/.claude/skills/greenlight") == "~/.claude/skills/greenlight"
    assert bughunt._quote_remote_path("~/my repo; rm -rf /") == "~/'my repo; rm -rf /'"
    assert bughunt._quote_remote_path("/opt/a b&c") == "'/opt/a b&c'"

    # Plist handles XML special chars (&, <, >) safely via plistlib
    tricky = Path("/tmp/a&b<c>/repo")
    xml = bughunt.build_launchd_plist(tricky, hour=0, minute=59, max_prs=2)
    pl = plistlib.loads(xml.encode())
    assert pl["WorkingDirectory"] == str(tricky)

    # Reject invalid schedule times
    assert bughunt.install_schedule(host="local", hour=25, minute=0) == 2
    assert bughunt.install_schedule(host="local", hour=2, minute=60) == 2

    # Atomic PID lock prevents double acquisition by live PID and reclaims dead PID
    tmp = Path(tempfile.mkdtemp(prefix="gl-lock-test-"))
    try:
        lf = tmp / "run.pid"
        assert bughunt._acquire_pid_lock(lf) is True
        assert bughunt._acquire_pid_lock(lf) is False  # our own live PID holds it
        lf.write_text("999999")  # dead PID
        assert bughunt._acquire_pid_lock(lf) is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_uitest_session_trace_and_methods():
    sess = uitest.UITestSession(fixture="default", record_trace=True)
    try:
        assert sess.is_alive() is False
        with mock.patch.object(uitest, "rpc", return_value={"ok": True, "result": True}):
            assert sess.ping()["ok"] is True
            assert sess.state()["ok"] is True
            assert sess.action("floor", to="center")["ok"] is True
            assert sess.eval("dashboard", "1+1")["ok"] is True
            assert sess.click("dashboard", "#btn")["ok"] is True
            assert sess.type_text("dashboard", "#inp", "hello")["ok"] is True
            assert sess.wait_for("dashboard", "true", timeout=1.0) is True
            assert sess.screenshot(sess.root / "s.png")["ok"] is True
        assert [t["op"] for t in sess.trace] == ["action", "eval", "click", "type"]
        tp = sess.save_trace(sess.root / "out.jsonl")
        fx, steps = bughunt.load_trace_file(tp)
        assert fx == "default" and len(steps) == 4
    finally:
        shutil.rmtree(sess.root, ignore_errors=True)


def test_github_dedupe_commits_and_baseline():
    tmp = Path(tempfile.mkdtemp(prefix="gl-gh-test-"))
    try:
        # load_known_fingerprints (list & dict forms)
        (tmp / "known-issues.json").write_text(json.dumps(["aaa111111111"]))
        assert bughunt.load_known_fingerprints(tmp) == {"aaa111111111"}
        (tmp / "known-issues.json").write_text(json.dumps({"fingerprints": ["bbb222222222"]}))
        assert bughunt.load_known_fingerprints(tmp) == {"bbb222222222"}

        # _gh_repo_args parses SSH & HTTPS remotes
        cp_ssh = subprocess.CompletedProcess([], 0, stdout="git@github.com:acme/greenlight.git\n", stderr="")
        with mock.patch.object(bughunt.subprocess, "run", return_value=cp_ssh):
            assert bughunt._gh_repo_args(ROOT) == ["-R", "acme/greenlight"]

        # fetch_open_pr_fingerprints extracts from body and branch
        pr_json = json.dumps([
            {"number": 1, "body": "footer\nbughunt-fingerprint: deadbeef1234", "headRefName": "other"},
            {"number": 2, "body": "", "headRefName": "bughunt/2026-11-21-cafebabe5678"},
        ])
        with mock.patch.object(bughunt.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(bughunt, "_gh_repo_args", return_value=["-R", "acme/gl"]), \
             mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=pr_json, stderr="")):
            assert bughunt.fetch_open_pr_fingerprints(ROOT) == {"deadbeef1234", "cafebabe5678"}

        # get_recent_commits parses git log
        log_out = "===abc1234\tfix dashboard\napp/dashboard.html\nscripts/dash.py\n\n===def5678\ttweak\nREADME.md\n"
        with mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=log_out, stderr="")):
            commits, files = bughunt.get_recent_commits(ROOT, since_hours=48)
            assert len(commits) == 2 and "app/dashboard.html" in files

        # run_baseline captures failing and timed-out commands
        seq = [
            subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="bad", stderr="err"),
            subprocess.TimeoutExpired(["make"], 60, output=b"partial"),
        ]
        idx = {"i": 0}

        def fake_run(*_a, **_k):
            v = seq[min(idx["i"], len(seq) - 1)]
            idx["i"] += 1
            if isinstance(v, Exception):
                raise v
            return v

        with mock.patch.object(bughunt, "_has_playwright", return_value=False), \
             mock.patch.object(bughunt.shutil, "which", return_value="/usr/bin/ruff"), \
             mock.patch.object(bughunt.subprocess, "run", side_effect=fake_run):
            hits = bughunt.run_baseline(ROOT)
            assert len(hits) >= 2 and all(h["oracle"] == "baseline_regression" for h in hits)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeSession:
    """In-memory stand-in for UITestSession to exercise fuzz, soak, replay, and oracle paths quickly."""

    def __init__(self, fixture="default", binary=None, record_trace=True, alive=True,
                 web_errors=None, console_errors=None, pwned=False, layout_issues=None):
        self.fixture = fixture
        self.binary = binary
        self.record_trace = record_trace
        self.trace = []
        self.root = Path(tempfile.mkdtemp(prefix="gl-fake-sess-"))
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmux_socket = "gl-fake-tmux"
        self._alive = alive
        self.proc = mock.Mock(returncode=139 if not alive else None)
        self._web_errors = web_errors or {"dashboard": [], "terminal": []}
        self._console_errors = console_errors or {"dashboard": [], "terminal": []}
        self._pwned = pwned
        self._layout_issues = layout_issues or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)

    def is_alive(self):
        return self._alive

    def state(self):
        return {
            "ok": True,
            "webErrors": self._web_errors,
            "consoleErrors": self._console_errors,
            "metrics": {"rssMb": 120.5, "cpuSecs": 0.4, "openFds": 24, "domNodes": {"dashboard": 200, "terminal": 150}},
        }

    def action(self, name, **kwargs):
        if self.record_trace:
            self.trace.append({"seq": len(self.trace), "op": "action", "args": {"name": name, **kwargs}})
        return {"ok": True}

    def eval(self, target, js, record=True):
        if record and self.record_trace:
            self.trace.append({"seq": len(self.trace), "op": "eval", "args": {"target": target, "js": js}})
        if "__pwned" in js:
            return {"ok": True, "result": 1 if self._pwned else None}
        if "scrollWidth" in js:
            return {"ok": True, "result": self._layout_issues}
        if "querySelectorAll('button" in js:
            return {"ok": True, "result": ["#btn-1"]}
        if "textarea" in js:
            return {"ok": True, "result": ["#inp-1"]}
        return {"ok": True, "result": True}

    def click(self, target, selector):
        if self.record_trace:
            self.trace.append({"seq": len(self.trace), "op": "click", "args": {"target": target, "selector": selector}})
        return {"ok": True}

    def type_text(self, target, selector, text):
        if self.record_trace:
            self.trace.append({"seq": len(self.trace), "op": "type", "args": {"target": target, "selector": selector, "text": text}})
        return {"ok": True}

    def wait_for(self, target, pred, timeout=10.0):
        return True

    def screenshot(self, out_path, window="active"):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return {"ok": True, "path": str(out_path)}

    def save_trace(self, path):
        return bughunt.save_trace_file(Path(path), self.fixture, self.trace)


def test_oracles_fuzz_soak_replay_and_matrix():
    run_dir = Path(tempfile.mkdtemp(prefix="gl-matrix-test-"))
    try:
        # 1. Dead process -> crash oracle
        with _FakeSession(alive=False) as dead:
            (dead.root / "app.log").write_text("Segmentation fault")
            hits, _ = bughunt._check_session_oracles(dead, "default", "fuzz", set())
            assert len(hits) == 1 and hits[0]["oracle"] == "crash"

        # 2. Live session with JS error, console error, XSS, and diag hang
        with _FakeSession(
            web_errors={"dashboard": ["ReferenceError: x is not defined"], "terminal": []},
            console_errors={"dashboard": [], "terminal": ["console fail"]},
            pwned=True,
        ) as sick:
            (sick.home / "diag").mkdir(parents=True, exist_ok=True)
            (sick.home / "diag" / "app.jsonl").write_text(json.dumps({"kind": "hang", "secs": 3.1, "comp": "app"}) + "\n")
            hits, m = bughunt._check_session_oracles(sick, "adversarial", "fuzz", set(), shot_dir=run_dir / "shots")
            oracles = {h["oracle"] for h in hits}
            assert oracles == {"js_error", "console_error", "security", "hang"}, oracles
            assert m["rssMb"] == 120.5

        # 3. fuzz_session + tmux leak detection + layout issue
        def make_fake(fixture="default", binary=None, record_trace=True):
            return _FakeSession(
                fixture=fixture, binary=binary, record_trace=record_trace,
                web_errors={"dashboard": ["TypeError: boom"], "terminal": []} if fixture == "default" else None,
                layout_issues=["horizontal overflow: scrollWidth=1500 > innerWidth=1200"],
            )

        with mock.patch.object(bughunt.uitest, "UITestSession", side_effect=make_fake), \
             mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="")), \
             mock.patch.object(bughunt.time, "sleep", return_value=None):
            f_hits, trace, metrics = bughunt.fuzz_session("default", seed=7, steps=80, run_dir=run_dir)
            assert any(h["oracle"] == "js_error" for h in f_hits)
            assert any(h["oracle"] == "layout_issue" for h in f_hits)
            assert len(trace) > 0 and metrics["rss_max_mb"] == 120.5

            # replay_trace confirms the JS error
            ok, r_hits = bughunt.replay_trace(trace[:4], fixture="default", target_oracle="js_error")
            assert ok and any(h["oracle"] == "js_error" for h in r_hits)

            # run_soak exercises churn & metric collection
            s_hits, s_metrics = bughunt.run_soak(soak_secs=1, fixture="scale")
            assert s_metrics.get("rss_max_mb") == 120.5 and isinstance(s_hits, list)

            # run_fuzz_matrix runs parallel fixtures + replays & shrinks hard hits
            with mock.patch.object(bughunt.sys, "platform", "darwin"), \
                 mock.patch.object(bughunt.uitest, "build_test_binary", return_value=Path("/tmp/fake-bin")):
                m_hits, m_metrics = bughunt.run_fuzz_matrix(
                    fixtures=("default", "empty"), seed=1, steps=8, soak_secs=0, workers=2, run_dir=run_dir,
                )
                repro_js = [h for h in m_hits if h["oracle"] == "js_error" and h["reproducible"]]
                assert repro_js and repro_js[0]["trace_path"] is not None
                assert "fuzz" in m_metrics

        # tmux leak helper detects surviving session
        with mock.patch.object(bughunt.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, stdout="0: 1 windows\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]):
            leaks = bughunt._check_tmux_leak("gl-leak-sock", "default")
            assert len(leaks) == 1 and leaks[0]["oracle"] == "process_leak"
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_agent_llm_vm_and_patcher_pipeline():
    run_dir = Path(tempfile.mkdtemp(prefix="gl-agent-test-"))
    try:
        # Agent fallback on rate limit: claude hits usage limit -> falls back to codex
        with mock.patch.object(bughunt.harness, "argv", side_effect=lambda eng, p, stream=False: ([eng], p)), \
             mock.patch.object(bughunt.subprocess, "run", side_effect=[
                 subprocess.CompletedProcess([], 1, stdout="You've hit your usage limit", stderr=""),
                 subprocess.CompletedProcess([], 0, stdout='```bughunt-json\n[{"title":"Reg","oracle":"commit_regression","summary":"Broken","files":["app/dashboard.html"],"reproducible":true}]\n```', stderr=""),
             ]):
            out, rc, eng = bughunt.run_agent_with_fallback("hi", cwd=ROOT, agents=["claude", "codex"])
            assert rc == 0 and eng == "codex" and "bughunt-json" in out

        # LLM explorer parses structured findings
        with mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt, "run_agent_with_fallback", return_value=(
                 '```bughunt-json\n[{"title":"Broken tab","oracle":"commit_regression","summary":"Tab blank","files":["app/terminal.html"],"reproducible":true}]\n```',
                 0, "claude",
             )):
            llm_hits = bughunt.run_llm_explorer(ROOT, [{"sha": "abc", "subject": "ui", "files": ["app/terminal.html"]}], {"app/terminal.html"}, run_dir)
            assert len(llm_hits) == 1 and llm_hits[0]["auto_pr"] is True

        # Tier-2 VM HID failure becomes finding
        with mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt.shutil, "which", return_value="/opt/homebrew/bin/tart"), \
             mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="hid fail", stderr="")):
            vm_hits = bughunt.run_vm_hid(ROOT, run_dir)
            assert len(vm_hits) == 1 and vm_hits[0]["oracle"] == "baseline_regression"

        # patch_and_open_draft_pr full happy path (mocked git/gh/agent/verify)
        finding = bughunt.make_finding(
            oracle="js_error", track="fuzz", fixture="default",
            title="Null deref in dashboard", summary="TypeError: null", where="app/dashboard.html", reproducible=True,
        )

        def fake_subproc(cmd, **_kw):
            if cmd[0] == "git" and "rev-list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
            if cmd[0] == "git" and "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[0] == "gh" and "--label" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="could not add label: not found")
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/acme/gl/pull/99\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(bughunt.subprocess, "run", side_effect=fake_subproc), \
             mock.patch.object(bughunt, "run_agent_with_fallback", return_value=("fixed", 0, "claude")), \
             mock.patch.object(bughunt, "verify_patch_worktree", return_value=(True, "ruff: PASS\ntest-py: PASS")), \
             mock.patch.object(bughunt, "_gh_repo_args", return_value=["-R", "acme/gl"]):
            res = bughunt.patch_and_open_draft_pr(finding, repo_dir=ROOT, base_ref="origin/main", run_dir=run_dir, dry_run=False)
            assert res["status"] == "opened" and res["pr_url"].endswith("/pull/99"), res
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_remote_schedule_run_nightly_and_cli():
    # Remote install checks Darwin and delegates over SSH
    with mock.patch.object(bughunt, "_ssh_exec", side_effect=[
        subprocess.CompletedProcess([], 0, stdout="Linux\n", stderr=""),
    ]):
        assert bughunt.install_schedule(host="linux-box") == 1

    with mock.patch.object(bughunt, "_ssh_exec", side_effect=[
        subprocess.CompletedProcess([], 0, stdout="Darwin\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="installed\n", stderr=""),
    ]) as ssh_m:
        assert bughunt.install_schedule(host="mac-mini", repo="~/greenlight", hour=3, minute=0, max_prs=2, with_vm=True) == 0
        assert "cd ~/greenlight && python3 scripts/bughunt.py install --host local --hour 3 --minute 0 --max-prs 2 --with-vm" in ssh_m.call_args_list[1][0][1]

    with mock.patch.object(bughunt, "_ssh_exec", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as ssh_r:
        assert bughunt.run_remote("mac-mini", repo="~/greenlight", extra_args=["--dry-run"]) == 0
        assert "caffeinate -i python3 scripts/bughunt.py run --dry-run" in ssh_r.call_args[0][1]
        assert bughunt.show_status(host="mac-mini", repo="~/greenlight") == 0

    # Local status + full run_nightly dry-run with mocked tracks
    bughunt.append_perf_history({"ts": 1, "date": "2026-11-21", "findings": 0, "prs_opened": 0, "soak": {"rss_max_mb": 120, "rpc_p95_ms": 5}})
    assert bughunt.show_status() == 0

    bad_args = argparse.Namespace(fixtures="not_a_fixture")
    assert bughunt.run_nightly(bad_args) == 2

    with mock.patch.object(bughunt, "run_baseline", return_value=[]), \
         mock.patch.object(bughunt, "run_fuzz_matrix", return_value=([], {"soak": {}})), \
         mock.patch.object(bughunt, "run_llm_explorer", return_value=[]), \
         mock.patch.object(bughunt, "fetch_open_pr_fingerprints", return_value=set()), \
         mock.patch.object(bughunt.sys, "argv", ["bughunt.py", "run", "--dry-run", "--base", "HEAD", "--fixtures", "default"]):
        assert bughunt.main() == 0


def test_socket_eof_crash_excerpt_and_settle():
    # 1. uitest.rpc raises ConnectionResetError (subclass of OSError) on EOF or malformed JSON
    import socket as _socket

    tmp = Path(tempfile.mkdtemp(prefix="gl-sock-eof-"))
    try:
        sock_p = tmp / "eof.sock"
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_p))
        srv.listen(1)
        import threading

        def accept_and_close():
            conn, _ = srv.accept()
            conn.recv(1024)
            conn.close()

        t = threading.Thread(target=accept_and_close, daemon=True)
        t.start()
        try:
            uitest.rpc(sock_p, {"cmd": "ping"}, timeout=2.0)
            raise AssertionError("expected ConnectionResetError on EOF")
        except ConnectionResetError:
            pass
        finally:
            t.join(timeout=2)
            srv.close()

        # Malformed JSON from socket also raises ConnectionResetError
        sock_bad = tmp / "bad.sock"
        srv2 = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv2.bind(str(sock_bad))
        srv2.listen(1)

        def accept_and_garbage():
            conn, _ = srv2.accept()
            conn.recv(1024)
            conn.sendall(b"{not-json\n")
            conn.close()

        t2 = threading.Thread(target=accept_and_garbage, daemon=True)
        t2.start()
        try:
            uitest.rpc(sock_bad, {"cmd": "ping"}, timeout=2.0)
            raise AssertionError("expected ConnectionResetError on malformed JSON")
        except ConnectionResetError:
            pass
        finally:
            t2.join(timeout=2)
            srv2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 2. Mid-RPC socket EOF during replay_trace still records a reproducible crash once process exits
    class _CrashMidRpcSession(_FakeSession):
        def action(self, name, **kwargs):
            self._alive = False
            self.proc.returncode = -6
            raise ConnectionResetError("EOF from --uitest socket")

    with mock.patch.object(bughunt.uitest, "UITestSession", side_effect=lambda **_k: _CrashMidRpcSession()), \
         mock.patch.object(bughunt.time, "sleep", return_value=None):
        ok, hits = bughunt.replay_trace(
            [{"seq": 0, "op": "action", "args": {"name": "sendChat", "text": "hi"}}],
            fixture="default",
            target_oracle="crash",
        )
        assert ok and len(hits) == 1 and hits[0]["oracle"] == "crash", (ok, hits)

    # 2b. _settle_if_exiting + _check_session_oracles mid-state() crash vs live hang
    with _FakeSession(alive=True) as dying:
        def die_on_wait(timeout=None):
            dying._alive = False
            dying.proc.returncode = -11

        dying.proc.wait.side_effect = die_on_wait
        dying.state = mock.Mock(side_effect=ConnectionResetError("EOF"))
        hits_d, _ = bughunt._check_session_oracles(dying, "default", "fuzz", set())
        assert len(hits_d) == 1 and hits_d[0]["oracle"] == "crash", hits_d

    with _FakeSession(alive=True) as hung:
        hung.state = mock.Mock(side_effect=TimeoutError("rpc timeout"))
        hits_h, _ = bughunt._check_session_oracles(hung, "default", "fuzz", set())
        assert len(hits_h) == 1 and hits_h[0]["oracle"] == "hang", hits_h

    # 3. _extract_crash_excerpt preserves exception header + top frames (Core.notify), and head+tail fallback
    long_log = (
        ("startup log line\n" * 80)
        + "*** Terminating app due to uncaught exception 'NSInternalInconsistencyException', reason: 'bundleProxy nil'\n"
        + "*** First throw call stack:\n(\n"
        + "  0 CoreFoundation 0x1\n  1 Greenlight Core.notify +1972\n"
        + "".join(f"  {i} AppKit boilerplate frame 0x{i:08x}\n" for i in range(2, 90))
        + ")\nlibc++abi: terminating due to uncaught exception of type NSException\n"
    )
    excerpt = bughunt._extract_crash_excerpt(long_log, max_chars=2000)
    assert "NSInternalInconsistencyException" in excerpt, excerpt[:300]
    assert "Core.notify" in excerpt, excerpt[:500]
    assert "libc++abi:" in excerpt, excerpt[-300:]
    plain_long = "HEAD_MARKER " + ("x" * 4000) + " TAIL_MARKER"
    plain_ex = bughunt._extract_crash_excerpt(plain_long, max_chars=2000)
    assert "HEAD_MARKER" in plain_ex and "TAIL_MARKER" in plain_ex and "[truncated]" in plain_ex

    # 4. _reap_session_processes terminates both process group and detached descendants cleanly
    reap_dir = Path("/tmp/gl-uitest-reap1234-test")
    with mock.patch.object(uitest, "_find_session_pids", side_effect=[{50001}, {50001}, set()]), \
         mock.patch.object(uitest.os, "killpg", side_effect=[None, ProcessLookupError(), ProcessLookupError()]) as m_kpg, \
         mock.patch.object(uitest.os, "kill") as m_kill, \
         mock.patch.object(uitest.time, "sleep", return_value=None):
        uitest._reap_session_processes(reap_dir, pgid=50000)
        assert m_kpg.call_count >= 1 and m_kill.call_count >= 1

    # Fallback SIGKILL path when group/pids survive past deadline
    t_vals = iter([0.0, 1.0])
    with mock.patch.object(uitest, "_find_session_pids", return_value={50002}), \
         mock.patch.object(uitest.os, "killpg") as m_kpg2, \
         mock.patch.object(uitest.os, "kill") as m_kill2, \
         mock.patch.object(uitest.time, "monotonic", side_effect=lambda: next(t_vals, 2.0)), \
         mock.patch.object(uitest.time, "sleep", return_value=None):
        uitest._reap_session_processes(reap_dir, pgid=50000)
        assert m_kpg2.call_count == 2 and m_kill2.call_count == 2


def test_uv_no_project_chaos_filter_and_commit_overlap():
    # _python_cmd must pass --no-project when uv is installed so Track 0 never writes uv.lock
    with mock.patch.object(bughunt.shutil, "which", return_value="/opt/homebrew/bin/uv"):
        assert bughunt._python_cmd() == ["uv", "run", "--no-project", "python"]
    with mock.patch.object(bughunt.shutil, "which", return_value=None):
        assert bughunt._python_cmd() == [sys.executable]

    # Chaos fixture ignores expected forge 429 rate_limit RuntimeError in dash/greenlight, but keeps real bugs & non-chaos errors
    tmp = Path(tempfile.mkdtemp(prefix="gl-chaos-diag-"))
    try:
        (tmp / "diag").mkdir()
        (tmp / "diag" / "events.jsonl").write_text(
            json.dumps({
                "kind": "dash_error", "comp": "dash", "err": "RuntimeError", "msg_class": "rate_limit",
                "where": ["dash.py:610:snapshot", "greenlight.py:136:_run"],
            }) + "\n"
            + json.dumps({
                "kind": "uncaught", "comp": "greenlight", "err": "RuntimeError", "msg_class": "rate_limit",
                "where": ["greenlight.py:1147:scan", "greenlight.py:136:_run"],
            }) + "\n"
            + json.dumps({
                "kind": "dash_error", "comp": "dash", "err": "KeyError", "msg_class": "other",
                "where": ["dash.py:620:snapshot"],
            }) + "\n"
        )
        chaos_hits = bughunt._read_isolated_diag_errors(tmp, fixture="chaos")
        assert len(chaos_hits) == 1 and "KeyError" in chaos_hits[0]["summary"], chaos_hits
        default_hits = bughunt._read_isolated_diag_errors(tmp, fixture="default")
        assert len(default_hits) == 3, default_hits
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # _find_session_pids matches descendants referencing gl-uitest-* root and skips current/non-uitest dirs
    fake_ps = (
        f"  {os.getpid()}     1 python3 test_bughunt.py /tmp/gl-uitest-abc12345-xyz\n"
        "  50001     1 /bin/sh -c GREENLIGHT_HOME=/tmp/gl-uitest-abc12345-xyz/home python3 scripts/chat.py\n"
        "  50002 50001 python3 scripts/chat.py\n"
        "  50003     1 /bin/sh -c echo unrelated\n"
    )
    with mock.patch.object(uitest.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=fake_ps, stderr="")):
        assert uitest._find_session_pids(Path("/tmp/gl-uitest-abc12345-xyz")) == {50001, 50002}
        assert uitest._find_session_pids(Path("/tmp/not-uitest-dir")) == set()

    # Track B recent_commit_overlap must NOT be set unconditionally just because commits exist
    run_dir = Path(tempfile.mkdtemp(prefix="gl-overlap-test-"))
    try:
        payload = json.dumps({
            "status": "ok",
            "uitest_started": True,
            "checks_run": 5,
            "fixtures_tested": ["default"],
            "findings": [
                {"title": "Unrelated visual", "oracle": "visual_issue", "summary": "Off by 2px",
                 "where": "app/stage.html", "files": ["app/stage.html"], "reproducible": True},
                {"title": "Touched file", "oracle": "js_error", "summary": "TypeError",
                 "where": "app/dashboard.html", "files": ["app/dashboard.html"], "reproducible": True},
            ],
        })
        with mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt, "run_agent_with_fallback", return_value=(f"```bughunt-json\n{payload}\n```", 0, "claude")):
            st: dict = {}
            hits = bughunt.run_llm_explorer(
                ROOT,
                [{"sha": "abc", "subject": "dash", "files": ["app/dashboard.html"]}],
                {"app/dashboard.html"},
                run_dir,
                status_out=st,
            )
            assert st["status"] == "ok" and st["checks_run"] == 5, st
            by_oracle = {h["oracle"]: h for h in hits}
            assert by_oracle["visual_issue"]["recent_commit_overlap"] is False
            assert by_oracle["js_error"]["recent_commit_overlap"] is True
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_track_b_blocked_detection_permissions_and_report():
    # Scoped permissions added for claude in safe mode; explore always disallows Edit/Write
    with mock.patch.object(bughunt.harness, "conf", return_value={}):
        exp_cmd = bughunt._augment_agent_cmd("claude", ["claude", "-p"], mode="explore", add_dirs=[Path("/tmp/shots")])
        assert "--allowedTools" in exp_cmd and "--disallowedTools" in exp_cmd and "--add-dir" in exp_cmd, exp_cmd
        pat_cmd = bughunt._augment_agent_cmd("claude", ["claude", "-p"], mode="patch")
        assert "--allowedTools" in pat_cmd and "--disallowedTools" not in pat_cmd, pat_cmd
    with mock.patch.object(bughunt.harness, "conf", return_value={"bypass": True}):
        byp_cmd = bughunt._augment_agent_cmd("claude", ["claude", "-p"], mode="explore")
        assert "--disallowedTools" in byp_cmd and "--allowedTools" not in byp_cmd, byp_cmd

    # Blocked / false-green detection across all failure modes
    run_dir = Path(tempfile.mkdtemp(prefix="gl-blocked-test-"))
    try:
        # 1. Legacy [] with permission denial prose -> blocked
        denied_out = "Every Bash command was denied. Nothing ran, so this is not a clean result.\n```bughunt-json\n[]\n```"
        st1: dict = {}
        with mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt, "run_agent_with_fallback", return_value=(denied_out, 0, "claude")):
            bughunt.run_llm_explorer(ROOT, [], set(), run_dir, status_out=st1)
            assert st1["status"] == "blocked", st1

        # 2. Structured envelope with checks_run=0 -> blocked
        zero_out = '```bughunt-json\n{"status":"ok","uitest_started":false,"checks_run":0,"findings":[]}\n```'
        st2: dict = {}
        with mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt, "run_agent_with_fallback", return_value=(zero_out, 0, "claude")):
            bughunt.run_llm_explorer(ROOT, [], set(), run_dir, status_out=st2)
            assert st2["status"] == "blocked", st2

        # 3. Missing bughunt-json block -> error
        st3: dict = {}
        with mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt, "run_agent_with_fallback", return_value=("plain text only", 0, "claude")):
            bughunt.run_llm_explorer(ROOT, [], set(), run_dir, status_out=st3)
            assert st3["status"] == "error", st3

        # 4. Report MUST NOT claim clean run when a track is blocked, and must show "15 of 25 recent commits"
        commits = [{"sha": f"c{i:02d}", "subject": f"msg {i}", "files": ["README.md"]} for i in range(25)]
        rp = bughunt.write_report(
            run_dir, [], [], {}, commits, max_prs=2,
            track_status={"baseline": {"status": "ok", "detail": "2s"}, "llm_diff": {"status": "blocked", "detail": "denied"}},
            commits_inspected=15,
        )
        txt = rp.read_text()
        assert "Clean run" not in txt, txt
        assert "15 of 25 recent commits" in txt, txt
        assert "## Track Status" in txt and "`blocked`" in txt, txt
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_verification_gates_and_nightly_fault_isolation():
    # _verification_gates adds test-js & test-ui for app/ edits, omits for scripts-only edits
    with mock.patch.object(bughunt.shutil, "which", return_value="/usr/bin/ruff"), \
         mock.patch.object(bughunt, "_has_playwright", return_value=True), \
         mock.patch.object(bughunt.sys, "platform", "darwin"):
        py_only = [g[0] for g in bughunt._verification_gates(["scripts/dash.py", "tests/test_dash.py"])]
        assert "test-js" not in py_only and "test-ui" not in py_only, py_only
        app_html = [g[0] for g in bughunt._verification_gates(["app/dashboard.html", "tests/test_dashboard.cjs"])]
        assert "test-js" in app_html and "test-ui" in app_html, app_html
        app_swift = [g[0] for g in bughunt._verification_gates(["app/Sources/Core.swift", "tests/test_uitest.py"])]
        assert "test-js" in app_swift and "test-ui" in app_swift, app_swift

    # run_nightly still writes report.md when one track raises an unexpected exception
    with mock.patch.object(bughunt, "run_baseline", side_effect=RuntimeError("boom baseline")), \
         mock.patch.object(bughunt, "run_fuzz_matrix", return_value=([], {"soak": {}})), \
         mock.patch.object(bughunt, "run_llm_explorer", return_value=[]), \
         mock.patch.object(bughunt, "fetch_open_pr_fingerprints", return_value=set()), \
         mock.patch.object(bughunt.sys, "argv", ["bughunt.py", "run", "--dry-run", "--base", "HEAD", "--fixtures", "default"]):
        assert bughunt.main() == 0
        latest = (bughunt.bughunt_home() / "latest.md").read_text()
        assert "`baseline`:** `error`" in latest and "Clean run" not in latest, latest


def test_upgrades_pull_playwright_stub_backend_fuzz_and_flows():
    import datetime as _dt
    import dash as _dash

    # 1. --pull & NODE_PATH in launchd plist + _pull_repo_if_clean branches
    xml = bughunt.build_launchd_plist(ROOT, hour=2, minute=30, max_prs=1, node_path="/opt/pw/node_modules")
    pl = plistlib.loads(xml.encode())
    assert "--pull" in pl["ProgramArguments"]
    assert pl["EnvironmentVariables"]["NODE_PATH"] == "/opt/pw/node_modules"

    assert bughunt._pull_repo_if_clean(ROOT, "HEAD") == "skip_head"
    with mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="feature\n", stderr="")):
        assert bughunt._pull_repo_if_clean(ROOT, "origin/main") == "skip_branch"
    with mock.patch.object(bughunt.subprocess, "run", side_effect=[
        subprocess.CompletedProcess([], 0, stdout="main\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout=" M dirty.py\n", stderr=""),
    ]):
        assert bughunt._pull_repo_if_clean(ROOT, "origin/main") == "skip_dirty"
    with mock.patch.object(bughunt.subprocess, "run", side_effect=[
        subprocess.CompletedProcess([], 0, stdout="main\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="Already up to date.\n", stderr=""),
    ]):
        assert bughunt._pull_repo_if_clean(ROOT, "origin/main") == "pulled"
    with mock.patch.object(bughunt.subprocess, "run", side_effect=OSError("no git")):
        assert bughunt._pull_repo_if_clean(ROOT, "origin/main") == "pull_failed"

    # 2. Scripted UI flows detect stuck spinners + narrow layout issues cleanly
    class _StuckSpinnerSession(_FakeSession):
        def eval(self, target, js, record=True):
            if "ui.snap.pucks[0]" in js:
                return {"ok": True, "result": "fix-auth"}
            if "ui.page.loading" in js:
                return {"ok": True, "result": ["workspace page still loading"]}
            return super().eval(target, js, record=record)

    t_seq = iter([0.0, 10.0])
    with _StuckSpinnerSession(layout_issues=["empty state clipped under #dock"]) as sess, \
         mock.patch.object(bughunt.time, "monotonic", side_effect=lambda: next(t_seq, 20.0)), \
         mock.patch.object(bughunt.time, "sleep", return_value=None):
        s_hits = bughunt._run_scripted_flows(sess, "default", set(), None)
        oracles = {h["oracle"] for h in s_hits}
        assert "hang" in oracles and "layout_issue" in oracles, s_hits

    # 3. Mode-aware offline claude stub handles stream-json, Insights JSON, ask_user array, and suggest plain text
    tmp_stub = Path(tempfile.mkdtemp(prefix="gl-stub-mode-"))
    try:
        uitest._install_stub_harnesses(tmp_stub)
        cl = str(tmp_stub / "claude")
        p_stream = subprocess.run([cl, "-p", "--output-format", "stream-json"], input="hi", capture_output=True, text=True, check=True)
        lines = [json.loads(ln) for ln in p_stream.stdout.splitlines() if ln.strip()]
        assert len(lines) == 2 and lines[1]["type"] == "result", lines

        p_design = subprocess.run([cl, "-p"], input="Return ONLY JSON for Insights", capture_output=True, text=True, check=True)
        lay = _dash.validate(json.loads(p_design.stdout))
        assert len(lay["tiles"]) == 1 and lay["tiles"][0]["widget"] == "note", lay

        p_ask = subprocess.run([cl, "-p"], input="Return ONLY a JSON array of strings", capture_output=True, text=True, check=True)
        opts = json.loads(p_ask.stdout)
        assert isinstance(opts, list) and len(opts) == 2, opts

        p_sug = subprocess.run([cl, "-p"], input="Write the ONE prompt", capture_output=True, text=True, check=True)
        lbl, body = _dash.split_label(p_sug.stdout)
        assert lbl == "Check workspace" and "tests" in body, (lbl, body)
    finally:
        shutil.rmtree(tmp_stub, ignore_errors=True)

    # 4. Deterministic backend parser/validator fuzzer passes cleanly on real parsers & reports exceptions
    assert bughunt.run_backend_fuzz(seed=42) == []

    def boom(_x):
        raise ValueError("bad parser state")

    with mock.patch.object(bughunt, "_backend_fuzz_cases", return_value=[("dash.py:validate", boom, ({},))]):
        bf_hits = bughunt.run_backend_fuzz(seed=1)
        assert len(bf_hits) == 1 and bf_hits[0]["oracle"] == "python_exception" and bf_hits[0]["reproducible"] is True

    # 5. Track B memory (open PR titles), rotating daily focus, and compact diff stat
    with mock.patch.object(bughunt.shutil, "which", return_value="/usr/bin/gh"), \
         mock.patch.object(bughunt, "_gh_repo_args", return_value=["-R", "acme/gl"]), \
         mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess(
             [], 0, stdout=json.dumps([{"title": "[Nightly Bug Hunt] Existing issue"}]), stderr="",
         )):
        titles = bughunt.fetch_open_pr_titles(ROOT)
        assert titles == ["[Nightly Bug Hunt] Existing issue"]

    with mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="abc1234 fix\n a.py | 2 +-\n", stderr="")):
        ds = bughunt._get_recent_diff_stat(ROOT, [{"sha": "abc1234", "subject": "fix", "files": ["a.py"]}])
        assert "abc1234 fix" in ds

    f_d1 = bughunt._daily_llm_focus(_dt.date(2026, 11, 22))
    f_d2 = bughunt._daily_llm_focus(_dt.date(2026, 11, 23))
    assert f_d1 != f_d2
    prompt = bughunt._build_llm_explorer_prompt("- abc fix", Path("/tmp/s"), diff_stat=ds, known_titles=titles, focus_area=f_d1)
    assert "Recent diff summary" in prompt and "Already tracked open bug-hunter PRs" in prompt and f_d1 in prompt

    # 6. Playwright NODE_PATH auto-detection + WebKit browser probe + baseline inclusion
    pw_tmp = Path(tempfile.mkdtemp(prefix="gl-pw-detect-"))
    try:
        (pw_tmp / "node_modules" / "playwright-core").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"NODE_PATH": ""}, clear=False), \
             mock.patch.object(bughunt.shutil, "which", return_value="/usr/local/bin/node"):
            detected = bughunt._detect_playwright_node_path(pw_tmp)
            assert detected == str(pw_tmp / "node_modules"), detected
            env_map = bughunt._playwright_env(pw_tmp)
            assert env_map["NODE_PATH"] == str(pw_tmp / "node_modules")
        assert bughunt._has_playwright_browser("firefox", pw_tmp) is False
        with mock.patch.object(bughunt.shutil, "which", return_value="/usr/local/bin/node"), \
             mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            assert bughunt._has_playwright_browser("webkit", pw_tmp) is True
        seen_cmds = []
        with mock.patch.object(bughunt, "_has_playwright", return_value=True), \
             mock.patch.object(bughunt, "_has_playwright_browser", return_value=True), \
             mock.patch.object(bughunt.sys, "platform", "darwin"), \
             mock.patch.object(bughunt.shutil, "which", return_value="/usr/bin/ruff"), \
             mock.patch.object(bughunt.subprocess, "run", side_effect=lambda cmd, **_k: (seen_cmds.append(cmd), subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))[1]):
            assert bughunt.run_baseline(ROOT) == []
            assert ["make", "test-js"] in seen_cmds and ["make", "test-webkit"] in seen_cmds, seen_cmds
    finally:
        shutil.rmtree(pw_tmp, ignore_errors=True)


def test_sec7_and_infra_hardening_regressions(tmp_path):
    # SEC-7: _ssh_exec rejects leading-dash host and inserts -- before host
    for bad_host in ("", "-oProxyCommand=evil", "--bad"):
        try:
            bughunt._ssh_exec(bad_host, "true")
        except ValueError:
            pass
        else:
            raise AssertionError(f"_ssh_exec accepted unsafe host: {bad_host!r}")
    with mock.patch.object(bughunt.subprocess, "run", return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")) as m_ssh:
        bughunt._ssh_exec("macmini", "uname -s")
    cmd = m_ssh.call_args.args[0]
    assert cmd[cmd.index("--") + 1] == "macmini"

    # SEC-7: _pull_repo_if_clean rejects leading-dash branch and passes -- + refs/heads/<branch>
    assert bughunt._pull_repo_if_clean(tmp_path, "origin/--upload-pack=evil") == "pull_failed"
    with mock.patch.object(bughunt.subprocess, "run", side_effect=[
        subprocess.CompletedProcess([], 0, stdout="main\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    ]) as m_pull:
        assert bughunt._pull_repo_if_clean(tmp_path, "origin/main") == "pulled"
    assert m_pull.call_args_list[-1].args[0][-3:] == ["--", "origin", "refs/heads/main"]

    # BUG-INFRA-7: _acquire_pid_lock reclaims pid <= 0 / corrupt / dead, treats PermissionError as active
    lock = tmp_path / "run.pid"
    for bad_pid in ("0", "-1", "corrupt"):
        lock.write_text(bad_pid)
        assert bughunt._acquire_pid_lock(lock) is True
        assert int(lock.read_text()) == os.getpid()
        lock.unlink()
    lock.write_text("424242")
    with mock.patch.object(bughunt.os, "kill", side_effect=PermissionError("EPERM")):
        assert bughunt._acquire_pid_lock(lock) is False
    assert lock.exists()
    with mock.patch.object(bughunt.os, "kill", side_effect=ProcessLookupError("ESRCH")):
        assert bughunt._acquire_pid_lock(lock) is True
    lock.unlink()

    # BUG-INFRA-8: remote install_schedule passes --host local so remote GREENLIGHT_HOST never recurses
    ssh_cmds = []
    def fake_ssh(host, remote_cmd, stream=False):
        ssh_cmds.append((host, remote_cmd, stream))
        return subprocess.CompletedProcess(["ssh"], 0, stdout="Darwin\n", stderr="")
    with mock.patch.object(bughunt, "_ssh_exec", side_effect=fake_ssh):
        assert bughunt.install_schedule(host="macmini", repo="~/gl", hour=3, minute=15, max_prs=1) == 0
    assert "--host local" in ssh_cmds[1][1]

    # BUG-INFRA-9: git add -A failure in patch_and_open_draft_pr sets status="failed" instead of crashing
    finding = bughunt.make_finding(
        oracle="js_error", track="fuzz", fixture="default",
        title="Sample error", summary="boom", details="stack", where="app/dashboard.html", reproducible=True,
    )
    finding["priority"] = 90
    def fake_patch_run(cmd, **kw):
        if "worktree" in cmd and "add" in cmd:
            Path(cmd[-2]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M a.py\n", stderr="")
        if "add" in cmd and "-A" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="index.lock exists")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with (
        mock.patch.object(bughunt.subprocess, "run", side_effect=fake_patch_run),
        mock.patch.object(bughunt, "run_agent_with_fallback", return_value=("ok", 0, "claude")),
    ):
        res = bughunt.patch_and_open_draft_pr(finding, repo_dir=tmp_path, base_ref="HEAD", run_dir=tmp_path, dry_run=False)
    assert res["status"] == "failed" and "git add failed" in res["note"]

    # BUG-INFRA-9 + SEC-7: run_nightly suppresses git fetch TimeoutExpired and skips leading-dash base branch
    with (
        mock.patch.object(bughunt.subprocess, "run", side_effect=subprocess.TimeoutExpired(["git"], 30)),
        mock.patch.object(bughunt, "run_baseline", return_value=[]),
        mock.patch.object(bughunt, "run_backend_fuzz", return_value=[]),
        mock.patch.object(bughunt, "run_fuzz_matrix", return_value=([], {})),
        mock.patch.object(bughunt, "run_llm_explorer", return_value=[]),
        mock.patch.object(bughunt, "fetch_open_pr_fingerprints", return_value=set()),
        mock.patch.object(bughunt, "get_recent_commits", return_value=([], set())),
        mock.patch.object(bughunt.sys, "argv", ["bughunt.py", "run", "--dry-run", "--skip-llm", "--base", "origin/main", "--fixtures", "default"]),
    ):
        assert bughunt.main() == 0

    # BUG-INFRA-10: _check_tmux_leak suppresses missing/timed-out tmux
    with mock.patch.object(bughunt.subprocess, "run", side_effect=FileNotFoundError("tmux")):
        assert bughunt._check_tmux_leak("gl-test", "default") == []
    with mock.patch.object(bughunt.subprocess, "run", side_effect=subprocess.TimeoutExpired(["tmux"], 5)):
        assert bughunt._check_tmux_leak("gl-test", "default") == []

