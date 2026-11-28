"""Unit tests for scripts/agent_hook.py: shell segmentation, git bypass detection, and Claude/Antigravity hooks."""
import io
import json
import subprocess
from unittest import mock

import agent_hook


def test_split_shell_segments_handles_operators_quotes_continuations_and_unclosed():
    assert agent_hook._split_shell_segments("echo a && git status | cat; ls || true") == [
        ["echo", "a"],
        ["git", "status"],
        ["cat"],
        ["ls"],
        ["true"],
    ]
    assert agent_hook._split_shell_segments('git commit -m "a && b; c" && git push') == [
        ["git", "commit", "-m", "a && b; c"],
        ["git", "push"],
    ]
    assert agent_hook._split_shell_segments("git commit \\\n  --no-verify -m hi") == [
        ["git", "commit", "--no-verify", "-m", "hi"],
    ]
    # Unclosed quote falls back to whitespace split without raising ValueError
    segs = agent_hook._split_shell_segments('git commit --no-verify -m "unterminated')
    assert segs == [["git", "commit", "--no-verify", "-m", '"unterminated']]
    assert agent_hook._split_shell_segments("") == []


def test_strip_env_wrappers_and_parse_git_segment():
    assert agent_hook._strip_env_wrappers(["FOO=1", "BAR=baz", "env", "command", "nohup", "time", "git", "status"]) == [
        "git",
        "status",
    ]
    # sudo is not stripped as an env wrapper
    assert agent_hook._strip_env_wrappers(["sudo", "git", "status"]) == ["sudo", "git", "status"]

    assert agent_hook._parse_git_segment(["echo", "git"]) == (None, [], None)
    assert agent_hook._parse_git_segment(["/usr/bin/git", "-C", "/tmp/r", "--no-pager", "commit", "-m", "hi"]) == (
        "commit",
        ["-m", "hi"],
        None,
    )
    assert agent_hook._parse_git_segment(["git", "--git-dir", ".git"]) == (None, [], None)

    sub, args, reason = agent_hook._parse_git_segment(["git", "-c", "core.hooksPath=/dev/null", "commit"])
    assert sub is None and args == [] and reason and "hooksPath" in reason

    sub2, args2, reason2 = agent_hook._parse_git_segment(["git", "-cCore.HooksPath=/tmp", "push"])
    assert sub2 is None and args2 == [] and reason2 and "hooksPath" in reason2


def test_short_flags_and_detect_hook_bypass_edge_cases():
    assert agent_hook._short_flags_contain_n("-n") is True
    assert agent_hook._short_flags_contain_n("-anm") is True
    assert agent_hook._short_flags_contain_n("-mn") is False  # -m consumes 'n' as message text
    assert agent_hook._short_flags_contain_n("-am") is False

    # Allowed commands: -- terminates options, push -n is --dry-run, -m value contains --no-verify
    assert agent_hook.detect_hook_bypass("git commit -- -n --no-verify") is None
    assert agent_hook.detect_hook_bypass("git push -n origin main") is None
    assert agent_hook.detect_hook_bypass("git commit -m --no-verify") is None
    assert agent_hook.detect_hook_bypass("git status --no-verify") is None

    # Blocked commands via wrappers, pipelines, and short flags
    assert agent_hook.detect_hook_bypass("FOO=1 env git commit -an -m msg") is not None
    assert agent_hook.detect_hook_bypass("ls && /usr/local/bin/git push origin main --no-verify") is not None


def test_read_payload_and_run_cmd_error_handling(tmp_path):
    with mock.patch("sys.stdin", io.StringIO("")):
        assert agent_hook._read_payload() == {}
    with mock.patch("sys.stdin", io.StringIO("{not json")):
        assert agent_hook._read_payload() == {}
    with mock.patch("sys.stdin", io.StringIO("[1, 2]")):
        assert agent_hook._read_payload() == {}
    with mock.patch("sys.stdin", io.StringIO('{"ok": true}')):
        assert agent_hook._read_payload() == {"ok": True}

    with mock.patch.object(agent_hook.subprocess, "run", side_effect=subprocess.TimeoutExpired(["x"], 1)):
        rc, msg = agent_hook._run_cmd(["x"], tmp_path)
    assert rc == 1 and "TimeoutExpired" in msg


def test_fast_lint_checks_all_app_shell_scripts_and_claude_post_edit(tmp_path):
    # BUG-INFRA-15: _fast_lint must shellcheck all app/*.sh (including release.sh), scripts/*.sh, and .githooks/*
    (tmp_path / "app").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".githooks").mkdir()
    (tmp_path / "app" / "build.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (tmp_path / "app" / "release.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (tmp_path / "scripts" / "sync.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (tmp_path / ".githooks" / "pre-commit").write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    (tmp_path / "scripts" / "check_safety.py").write_text("import sys\nsys.exit(0)\n")

    seen: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=25):
        seen.append(list(cmd))
        return 0, ""

    with (
        mock.patch.object(agent_hook.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        mock.patch.object(agent_hook, "_run_cmd", side_effect=fake_run),
    ):
        assert agent_hook._fast_lint(tmp_path) == []

    sc_cmds = [c for c in seen if c[0] == "shellcheck"]
    assert len(sc_cmds) == 1
    assert str(tmp_path / "app" / "build.sh") in sc_cmds[0]
    assert str(tmp_path / "app" / "release.sh") in sc_cmds[0]
    assert str(tmp_path / "scripts" / "sync.sh") in sc_cmds[0]
    assert str(tmp_path / ".githooks" / "pre-commit") in sc_cmds[0]

    # handle_claude_post_edit runs shellcheck on .sh and .githooks/* files and propagates failures
    with (
        mock.patch.object(agent_hook.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
        mock.patch.object(agent_hook, "_run_cmd", return_value=(1, "SC2086 quote var")) as m_run,
    ):
        rc1, msg1 = agent_hook.handle_claude_post_edit(
            {"tool_input": {"file_path": "app/release.sh"}, "cwd": str(tmp_path)}, root=tmp_path
        )
        rc2, msg2 = agent_hook.handle_claude_post_edit(
            {"tool_input": {"file_path": ".githooks/pre-commit"}, "cwd": str(tmp_path)}, root=tmp_path
        )
    assert rc1 == 2 and "SC2086" in msg1
    assert rc2 == 2 and "SC2086" in msg2
    assert m_run.call_count == 2
    assert agent_hook.handle_claude_post_edit({"tool_input": {"file_path": "missing.py"}, "cwd": str(tmp_path)}, root=tmp_path) == (0, "")
    assert agent_hook.handle_claude_post_edit({"tool_input": {}}, root=tmp_path) == (0, "")
    assert json.loads(json.dumps(agent_hook.handle_agy_pre_tool({}))) == {"decision": "ask"}
