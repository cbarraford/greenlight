#!/usr/bin/env python3
"""Unit tests for scripts/bughunt.py and the extended stress/chaos fixtures in scripts/uitest.py."""
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
                # Verify cyclic deterministic behavior of chaos orca stub: ok -> alt -> malformed -> exit 1 -> ok
                orca = str(bindir / "orca")
                r1 = subprocess.run([orca, "ps"], capture_output=True, text=True)
                r2 = subprocess.run([orca, "ps"], capture_output=True, text=True)
                r3 = subprocess.run([orca, "ps"], capture_output=True, text=True)
                r4 = subprocess.run([orca, "ps"], capture_output=True, text=True)
                assert r1.returncode == 0 and json.loads(r1.stdout)["ok"] is True
                assert r2.returncode == 0 and any(w["displayName"] == "churn-ws" for w in json.loads(r2.stdout)["result"]["worktrees"])
                assert r3.returncode == 0 and "{broken-json" in r3.stdout
                assert r4.returncode == 1
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
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(wt), check=True)
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


def main():
    try:
        test_normalize_and_fingerprint()
        print("ok  normalize & fingerprint")
        test_rank_dedupe_and_top2_selection()
        print("ok  priority ranking, dedupe & top-2 PR selection")
        test_shrink_trace_ddmin()
        print("ok  delta-debugging trace shrinker")
        test_fixtures_and_offline_stubs()
        print("ok  all 6 uitest fixtures & offline harness stubs")
        test_perf_oracles()
        print("ok  performance & leak oracles")
        test_diag_reader_and_llm_parser()
        print("ok  isolated diag reader & LLM JSON parser")
        test_launchd_plist_and_report_and_trace_io()
        print("ok  launchd plist, report writer & trace I/O")
        test_verify_patch_requires_test_file()
        print("ok  repro-first verification gate (single & multi-commit)")
        test_remote_quoting_plist_escaping_and_pid_lock()
        print("ok  remote path quoting, plist XML safety & atomic PID lock")
        print("all test_bughunt checks passed")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
