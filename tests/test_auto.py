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


def test_execute_precheck_failure_is_failed_not_skipped(gl_home, monkeypatch):
    # exit 1 is "nothing to do"; any other code means the precheck broke (greenlight.py precheck's 2: its scan died)
    pings = []
    monkeypatch.setattr(auto, "ping", lambda text, **kw: pings.append((text, kw)))
    monkeypatch.setattr(auto, "agent", lambda *a: (_ for _ in ()).throw(AssertionError("no agent after a failed precheck")))
    auto.save({"x": {"schedule": "* * * * *", "prompt": "hi", "precheck": "echo 'scan failed (GitLab login expired): invalid_grant'; exit 2"}})
    auto.execute("x")
    auto.save({"x": {"schedule": "* * * * *", "prompt": "hi", "precheck": "exit 3"}})
    auto.execute("x")
    runs = [json.loads(line) for line in (auto.DIR / "runs.jsonl").read_text().splitlines()]
    assert [(r["status"], r["summary"]) for r in runs] == [
        ("failed", "scan failed (GitLab login expired): invalid_grant"), ("failed", "precheck exited 3")]
    assert len(pings) == 2 and "invalid_grant" in pings[0][0] and pings[0][1] == {"kind": "automation-failed"}


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


def test_model_picks_the_provider(gl_home, monkeypatch):
    # an automation with only a --model runs on the harness that model names; a harness prefix is dropped for it
    monkeypatch.setitem(auto.CONFIG, "agents", ["claude", "codex"])
    assert auto.provider({"model": "gpt-6-luna"}) == "codex" and auto.provider({"model": "gemini-4-neon-low"}) == "agy"
    assert auto.provider({"model": "agy:claude-opus-5-5-high"}) == "agy" and auto.provider({}) == "claude"
    assert auto.provider({"provider": "codex", "model": "claude-opus-5-5"}) == "codex"  # an explicit provider wins
    assert auto.own_model({"model": "agy:claude-opus-5-5-high"}, "agy") == "claude-opus-5-5-high"
    assert auto.own_model({"model": "mystery-1"}, "codex") == "mystery-1" and auto.own_model({}, "codex") is None
    seen = []
    def argv(engine, text, model=None, **kw):
        seen.append((engine, model))
        raise ValueError("stop")
    monkeypatch.setattr(auto.harness, "argv", argv)
    auto.agent({"prompt": "p", "model": "agy:claude-opus-5-5-high"}, "agy", io_log := __import__("io").StringIO())
    auto.agent({"prompt": "p", "model": "agy:claude-opus-5-5-high"}, "claude", io_log)  # a fallback harness: its own default
    assert seen == [("agy", "claude-opus-5-5-high"), ("claude", False)], seen


def test_reply_parses_stream_json_and_preserves_plain_lines():
    raw = "\n".join([
        "plain stderr note",
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "pytest"}}]}}),
        json.dumps({"type": "result", "is_error": False, "result": "All green", "usage": {"input_tokens": 12, "output_tokens": 5}}),
    ])
    text, tokens = auto.reply("claude", raw)
    assert "plain stderr note" in text and "All green" in text
    assert tokens.get("in_tokens") == 12 and tokens.get("out_tokens") == 5


def test_orchestrator_success_and_timeout(gl_home):
    import subprocess

    log_path = gl_home / "orch.log"
    fake_proc = mock.MagicMock()
    fake_proc.pid = 42424
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = ("tick done\n", None)
    with mock.patch.object(auto.subprocess, "Popen", return_value=fake_proc), log_path.open("w") as fh:
        out, code = auto.orchestrator({"timeout": 1}, fh)
    assert code == 0 and "tick done" in out

    slow_proc = mock.MagicMock()
    slow_proc.pid = 42425
    slow_proc.poll.return_value = None
    slow_proc.communicate.side_effect = [
        subprocess.TimeoutExpired("orchestrate", 60),
        subprocess.TimeoutExpired("orchestrate", 5, output=b"partial"),
    ]
    with (
        mock.patch.object(auto.subprocess, "Popen", return_value=slow_proc),
        mock.patch.object(auto, "kill_process_tree") as m_kill,
        log_path.open("w") as fh,
    ):
        out, code = auto.orchestrator({"timeout": 1}, fh)
    assert code == 124 and "partial" in out and "[timed out]" in out
    m_kill.assert_called_once()
    slow_proc.kill.assert_called_once()


def test_spawn_tick_alive_and_auto_candidates(gl_home, monkeypatch, capsys):
    import os

    # alive(): invalid name, non-positive pid, live pid (self), dead/non-numeric pid
    assert not auto.alive("../bad")
    assert not auto.alive("missing")
    auto.DIR.mkdir(parents=True, exist_ok=True)
    (auto.DIR / "zero.pid").write_text("0")
    assert not auto.alive("zero")
    (auto.DIR / "neg.pid").write_text("-5")
    assert not auto.alive("neg")
    (auto.DIR / "bad.pid").write_text("not-a-pid")
    assert not auto.alive("bad")
    (auto.DIR / "me.pid").write_text(str(os.getpid()))
    assert auto.alive("me")
    with mock.patch.object(auto.os, "kill", side_effect=ProcessLookupError):
        assert not auto.alive("me")

    # _auto_candidates(): respects provider, appends CONFIG["agents"], skips pi without bypass, reorders capped
    monkeypatch.setitem(auto.CONFIG, "agents", ["claude", "codex", "pi"])
    monkeypatch.setitem(auto.CONFIG, "harnesses", {"pi": {"bypass": False}})
    with mock.patch.object(auto.harness, "is_capped", side_effect=lambda h, *_a, **_kw: "cooldown" if h == "claude" else None):
        cands = auto._auto_candidates({"model": "claude-opus-5-5", "effort": "high"})
    assert [h for h, _, _ in cands] == ["codex", "claude"]  # claude moved last; pi excluded without bypass
    monkeypatch.setitem(auto.CONFIG, "harnesses", {"pi": {"bypass": True}})
    with mock.patch.object(auto.harness, "is_capped", return_value=None):
        cands2 = auto._auto_candidates({"provider": "agy", "model": "claude-opus-5-5"})
    assert cands2[0][0] == "agy" and "pi" in [h for h, _, _ in cands2]

    # spawn & tick
    auto.save({
        "due-job": {"schedule": "* * * * *", "prompt": "check", "enabled": True},
        "off-job": {"schedule": "* * * * *", "prompt": "skip", "enabled": False},
        "bad name!": {"schedule": "* * * * *", "prompt": "ignored", "enabled": True},
    })
    with mock.patch.object(auto.subprocess, "Popen") as m_popen, mock.patch.object(auto, "alive", return_value=False):
        auto.tick(now=1700000000)
    assert m_popen.call_count == 1
    assert (auto.DIR / "last_tick").exists()
    assert "due-job: started" in capsys.readouterr().out

    with mock.patch.object(auto, "alive", return_value=True):
        auto.spawn("due-job")
    assert "still running, skipped" in capsys.readouterr().out


def test_execute_orchestrator_permissions_and_cli(gl_home, monkeypatch, capsys):
    import stat
    import sys

    auto.save({"orch": {"schedule": "* * * * *", "kind": "orchestrator", "enabled": True}})
    pings = []
    with (
        mock.patch.object(auto, "orchestrator", return_value=("failed hard", 1)),
        mock.patch.object(auto, "ping", side_effect=lambda msg, **kw: pings.append((msg, kw))),
    ):
        auto.execute("orch")
    runs = [json.loads(ln) for ln in (auto.DIR / "runs.jsonl").read_text().splitlines()]
    assert runs[-1]["status"] == "failed" and runs[-1]["engine"] == "orchestrator"
    assert len(pings) == 1 and pings[0][1]["kind"] == "automation-failed"
    # SEC-11: 0700 on auto/ and 0600 on auto/<name>/*.log
    assert stat.S_IMODE(auto.DIR.stat().st_mode) == 0o700
    logs = list((auto.DIR / "orch").glob("*.log"))
    assert logs and stat.S_IMODE(logs[0].stat().st_mode) == 0o600

    def run_cli(*args):
        monkeypatch.setattr(sys, "argv", ["auto.py", *args])
        auto.main()
        return capsys.readouterr().out

    out = run_cli("add", "nightly", "--schedule", "0 2 * * *", "--prompt", "run audit", "--enabled")
    assert '"nightly"' in out
    assert "run audit" in run_cli("show", "nightly")
    assert "nightly" in run_cli("list")
    # BUG-CORE-11: trailing flag with no value must not raise IndexError
    run_cli("edit", "nightly", "--disabled", "--orchestrator", "--model")
    assert auto.load()["nightly"]["kind"] == "orchestrator" and auto.load()["nightly"]["enabled"] is False
    run_cli("edit", "nightly", "--agentic", "--prompt", "back to agent")
    assert "kind" not in auto.load()["nightly"]

    with mock.patch.object(auto, "spawn") as m_spawn:
        run_cli("run", "nightly")
        m_spawn.assert_called_once_with("nightly")

    # BUG-CORE-8: runs CLI tolerates blank and corrupt JSON lines
    (auto.DIR / "runs.jsonl").write_text(
        "\n{bad json\n"
        + json.dumps({"ts": 1700000000, "name": "nightly", "status": "ok", "engine": "claude", "secs": 4, "summary": "done"})
        + "\n   \n"
    )
    assert "nightly" in run_cli("runs", "nightly")
    assert "removed nightly" in run_cli("rm", "nightly")
    assert "nightly" not in auto.load()

    with (
        mock.patch.object(auto.Path, "home", return_value=gl_home),
        mock.patch.object(auto.subprocess, "run") as m_run,
        mock.patch.object(auto.sys, "platform", "darwin"),
    ):
        assert "launch agent loaded" in run_cli("install")
        assert (gl_home / "Library/LaunchAgents/greenlight-auto.plist").is_file()
        assert m_run.call_count == 2

    with (
        mock.patch.object(auto.Path, "home", return_value=gl_home),
        mock.patch.object(auto.subprocess, "run") as m_run,
        mock.patch.object(auto.sys, "platform", "linux"),
    ):
        assert "greenlight-auto.timer enabled" in run_cli("install")
        assert (gl_home / ".config/systemd/user/greenlight-auto.service").is_file()
        assert (gl_home / ".config/systemd/user/greenlight-auto.timer").is_file()
        assert m_run.call_count == 2
