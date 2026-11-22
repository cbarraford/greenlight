"""Extended pytest coverage for scripts/auto.py (tick, reply, orchestrator, spawn, execute, CLI)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_BOOT = tempfile.mkdtemp(prefix="gl-auto-ext-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import auto  # noqa: E402


@pytest.fixture()
def isolated_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "STATE", tmp_path)
    monkeypatch.setattr(auto, "FILE", tmp_path / "automations.json")
    monkeypatch.setattr(auto, "DIR", tmp_path / "auto")
    return tmp_path


def test_reply_parses_stream_json_and_preserves_plain_lines():
    raw = "\n".join([
        "plain stderr note",
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "pytest"}}]}}),
        json.dumps({"type": "result", "is_error": False, "result": "All green", "usage": {"input_tokens": 12, "output_tokens": 5}}),
    ])
    text, tokens = auto.reply("claude", raw)
    assert "plain stderr note" in text and "All green" in text
    assert tokens.get("in_tokens") == 12 and tokens.get("out_tokens") == 5


def test_orchestrator_success_and_timeout(isolated_auto):
    log_path = isolated_auto / "orch.log"
    fake_proc = mock.MagicMock()
    fake_proc.pid = 42424
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = ("tick done\n", None)
    with mock.patch.object(auto.subprocess, "Popen", return_value=fake_proc), log_path.open("w") as fh:
        out, code = auto.orchestrator({"timeout": 1}, fh)
    assert code == 0 and "tick done" in out

    # Timeout path kills process tree and returns exit 124
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


def test_spawn_and_tick_respect_enabled_and_alive(isolated_auto, capsys):
    auto.save({
        "due-job": {"schedule": "* * * * *", "prompt": "check", "enabled": True},
        "off-job": {"schedule": "* * * * *", "prompt": "skip", "enabled": False},
        "bad name!": {"schedule": "* * * * *", "prompt": "ignored", "enabled": True},
    })
    with mock.patch.object(auto.subprocess, "Popen") as m_popen, mock.patch.object(auto, "alive", return_value=False):
        auto.tick(now=1700000000)
    assert m_popen.call_count == 1
    assert (isolated_auto / "auto" / "last_tick").exists()
    assert "due-job: started" in capsys.readouterr().out

    # Already running skips spawn
    with mock.patch.object(auto, "alive", return_value=True):
        auto.spawn("due-job")
    assert "still running, skipped" in capsys.readouterr().out


def test_execute_orchestrator_and_failure_ping(isolated_auto):
    auto.save({"orch": {"schedule": "* * * * *", "kind": "orchestrator", "enabled": True}})
    pings = []
    with (
        mock.patch.object(auto, "orchestrator", return_value=("failed hard", 1)),
        mock.patch.object(auto, "ping", side_effect=lambda msg, **kw: pings.append((msg, kw))),
    ):
        auto.execute("orch")
    runs = [json.loads(ln) for ln in (isolated_auto / "auto" / "runs.jsonl").read_text().splitlines()]
    assert runs[-1]["status"] == "failed" and runs[-1]["engine"] == "orchestrator"
    assert len(pings) == 1 and pings[0][1]["kind"] == "automation-failed"


def test_cli_crud_runs_and_install(isolated_auto, monkeypatch, capsys):
    def run_cli(*args):
        monkeypatch.setattr(sys, "argv", ["auto.py", *args])
        auto.main()
        return capsys.readouterr().out

    out = run_cli("add", "nightly", "--schedule", "0 2 * * *", "--prompt", "run audit", "--enabled")
    assert '"nightly"' in out
    assert '"nightly"' in run_cli("show", "nightly") or "run audit" in run_cli("show", "nightly")
    assert "nightly" in run_cli("list")
    run_cli("edit", "nightly", "--disabled", "--orchestrator")
    assert auto.load()["nightly"]["kind"] == "orchestrator" and auto.load()["nightly"]["enabled"] is False
    run_cli("edit", "nightly", "--agentic", "--prompt", "back to agent")
    assert "kind" not in auto.load()["nightly"]

    with mock.patch.object(auto, "spawn") as m_spawn:
        run_cli("run", "nightly")
        m_spawn.assert_called_once_with("nightly")

    (isolated_auto / "auto").mkdir(parents=True, exist_ok=True)
    (isolated_auto / "auto" / "runs.jsonl").write_text(json.dumps({
        "ts": 1700000000, "name": "nightly", "status": "ok", "engine": "claude", "secs": 4, "summary": "done",
    }) + "\n")
    assert "nightly" in run_cli("runs", "nightly")

    assert "removed nightly" in run_cli("rm", "nightly")
    assert "nightly" not in auto.load()

    # Install paths: darwin launchd + linux systemd (mocked home & subprocess)
    with (
        mock.patch.object(auto.Path, "home", return_value=isolated_auto),
        mock.patch.object(auto.subprocess, "run") as m_run,
        mock.patch.object(auto.sys, "platform", "darwin"),
    ):
        assert "launch agent loaded" in run_cli("install")
        assert (isolated_auto / "Library/LaunchAgents/greenlight-auto.plist").is_file()
        assert m_run.call_count == 2

    with (
        mock.patch.object(auto.Path, "home", return_value=isolated_auto),
        mock.patch.object(auto.subprocess, "run") as m_run,
        mock.patch.object(auto.sys, "platform", "linux"),
    ):
        assert "greenlight-auto.timer enabled" in run_cli("install")
        assert (isolated_auto / ".config/systemd/user/greenlight-auto.service").is_file()
        assert (isolated_auto / ".config/systemd/user/greenlight-auto.timer").is_file()
        assert m_run.call_count == 2
