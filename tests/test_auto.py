"""Tests for scripts/auto.py: cron matching, precheck/fallback execution, process tree kill, and launchd plist."""
import json
import plistlib
import time
from unittest import mock

import auto


def _parse_time(s: str) -> time.struct_time:
    return time.strptime(s, "%Y-%m-%d %H:%M")


def test_cron_due_matching():
    # 2026-10-23 is a Friday (dow 5)
    assert auto.due("*/30 * * * *", _parse_time("2026-10-23 03:30"))
    assert not auto.due("*/30 * * * *", _parse_time("2026-10-23 03:31"))
    assert auto.due("30 7 * * 1", _parse_time("2026-10-26 07:30"))
    assert not auto.due("30 7 * * 1", _parse_time("2026-10-23 07:30"))
    assert auto.due("0 9 * * 1-5", _parse_time("2026-10-23 09:00"))
    assert not auto.due("0 9 * * 1-5", _parse_time("2026-10-24 09:00"))
    assert auto.due("0 0 * * 7", _parse_time("2026-10-25 00:00"))
    assert auto.due("0 0 * * 0", _parse_time("2026-10-25 00:00"))
    assert auto.due("0 0 1 * 5", _parse_time("2026-10-23 00:00"))  # dom and dow both set: either matches, like cron
    assert auto.due("15,45 8-10 * 10 *", _parse_time("2026-10-23 09:45"))
    assert not auto.due("15,45 8-10 * 10 *", _parse_time("2026-10-23 11:45"))


def test_execute_precheck_skip_and_claude_fallback(gl_home, monkeypatch):
    # a run: precheck skip, then claude hitting its limit falls back to codex
    auto.save({"x": {"schedule": "* * * * *", "prompt": "hi", "precheck": "exit 1"}})
    auto.execute("x")

    calls = []

    def fake_agent(a, engine, log):
        calls.append(engine)
        if engine == "claude":
            return ("You've hit your usage limit", 1, {})
        return ("done", 0, {})

    monkeypatch.setattr(auto, "agent", fake_agent)
    auto.save({"x": {"schedule": "* * * * *", "prompt": "hi", "precheck": "true"}})
    auto.execute("x")

    runs = [json.loads(line) for line in (auto.DIR / "runs.jsonl").read_text().splitlines()]
    assert [r["status"] for r in runs] == ["skipped", "ok"]
    assert runs[1]["engine"] == "codex"
    assert calls == ["claude", "codex"], runs
    assert not auto.alive("x")


def test_kill_process_tree_and_process_start(gl_home):
    root, phase = 12345, 12346
    (auto.STATE / f"orchestrator-phase-{root}.pid").write_text(str(phase))
    signals = []
    with (
        mock.patch.object(auto.os, "killpg", side_effect=lambda group, sig: signals.append((group, sig))),
        mock.patch.object(auto.subprocess, "run", side_effect=auto.subprocess.TimeoutExpired("ps", 5)),
    ):
        auto.kill_process_tree(root)
    assert [group for group, sig in signals if sig == auto.signal.SIGKILL] == [root]  # stale marker cannot authorize killing an unverified group

    (auto.STATE / f"orchestrator-phase-{root}.pid").write_text(
        json.dumps({"phase": phase, "phase_start": "phase-birth", "root_start": "tick-birth", "nonce": "current-tick"})
    )
    signals.clear()
    with (
        mock.patch.object(auto.os, "killpg", side_effect=lambda group, sig: signals.append((group, sig))),
        mock.patch.object(auto.os, "getpgid", return_value=phase),
        mock.patch.object(auto.subprocess, "run", side_effect=auto.subprocess.TimeoutExpired("ps", 5)),
        mock.patch.object(auto, "process_start", side_effect=lambda pid: {root: "tick-birth", phase: "phase-birth"}.get(pid)),
    ):
        auto.kill_process_tree(root, nonce="current-tick")
    assert [group for group, sig in signals if sig == auto.signal.SIGKILL] == [phase, root]

    with (
        mock.patch.object(auto.Path, "read_text", side_effect=OSError("no procfs")),
        mock.patch.object(auto.subprocess, "run", return_value=type("PS", (), {"stdout": "Fri Nov 20 21:00:00 2026\n"})()),
    ):
        assert auto.process_start(phase) == "Fri Nov 20 21:00:00 2026"


def test_launchd_plist_format():
    # the macOS timer's plist parses and runs `tick`
    pl = plistlib.loads(auto.PLIST.format(exe="/py", path="/a.py", env_path="/bin").encode())
    assert pl["ProgramArguments"] == ["/py", "/a.py", "tick"]
    assert pl["StartInterval"] == 60
