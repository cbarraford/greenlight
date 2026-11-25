"""Tests for scripts/log_view.py (the agent log view's cost and share redaction) and dash.py's logextras/logshare."""
from __future__ import annotations

import io
import json
import re
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("GREENLIGHT_HOME", tempfile.mkdtemp(prefix="gl-logview-import-"))
import diag  # noqa: E402
import log_view  # noqa: E402

# Built at run time so this file's own text never looks like a secret, an address or a home folder to the publish audit
HOME = "/" + "home"
AT = "@"
KEY = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz012345"


def _write(p: Path, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def _claude_msg(mid, model, i, o, cr=0, cw=0):
    return {"type": "assistant", "message": {"id": mid, "model": model, "role": "assistant",
            "usage": {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}}


def test_price_matches_the_longest_known_prefix():
    assert log_view.price("claude-opus-5-5") == (4.0, 20.0, 0.20)
    assert log_view.price("claude-opus-5") == (5.0, 25.0, 0.50)
    assert log_view.price("claude-sonnet-4-6") == (3.0, 15.0, 0.30)
    assert log_view.price("gpt-5-codex") is None
    assert log_view.price("") is None


def test_claude_usage_dedupes_streamed_messages_and_prices_them(tmp_path):
    f = _write(tmp_path / "s.jsonl", [
        _claude_msg("m1", "claude-opus-5-5", 10, 5, cr=1000, cw=200),
        _claude_msg("m1", "claude-opus-5-5", 10, 50, cr=1000, cw=200),  # the same message, streamed: counted once, its fullest
        _claude_msg("m2", "claude-opus-5-5", 2, 100),
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        "not json",
    ])
    f.write_text(f.read_text() + "{broken\n")
    u = log_view.claude_usage(f)
    assert u["in"] == 10 + 1000 + 200 + 2 and u["out"] == 150 and u["cache_read"] == 1000 and u["cache_write"] == 200
    want = (12 * 4.0 + 200 * 4.0 * 1.25 + 1000 * 0.20 + 150 * 20.0) / 1e6 * 100
    assert u["cost_cents"] == pytest.approx(want)
    assert u["models"] == {"claude-opus-5-5"}


def test_an_unpriced_model_leaves_no_partial_cost(tmp_path):
    f = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 1, 1), _claude_msg("b", "mystery-model", 100, 100)])
    assert log_view.claude_usage(f)["cost_cents"] is None


def test_codex_usage_takes_the_last_running_total_and_no_cost(tmp_path):
    f = _write(tmp_path / "rollout.jsonl", [
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 900, "cached_input_tokens": 400,
                                                                                               "output_tokens": 30, "reasoning_output_tokens": 5}}}},
    ])
    u = log_view.codex_usage(f)
    assert (u["in"], u["out"], u["cache_read"], u["cost_cents"]) == (900, 35, 400, None)
    assert log_view.codex_usage(_write(tmp_path / "empty.jsonl", [{"payload": {"type": "other"}}]))["in"] == 0


def test_usage_sums_sessions_and_drops_cost_when_any_is_unpriced(tmp_path):
    c1 = _write(tmp_path / "a.jsonl", [_claude_msg("a", "claude-sonnet-5-5", 1000, 100)])
    c2 = _write(tmp_path / "b.jsonl", [_claude_msg("b", "claude-sonnet-5-5", 1000, 100)])
    empty = _write(tmp_path / "c.jsonl", [])
    u = log_view.usage([(c1, "claude"), (c2, "claude"), (empty, "claude"), (tmp_path / "missing.jsonl", "claude")])
    assert u["sessions"] == 2 and u["in_tokens"] == 2000 and u["out_tokens"] == 200 and u["estimated"]
    assert u["cost_cents"] == pytest.approx(round((2000 * 2.0 + 200 * 10.0) / 1e6 * 100, 2))
    cx = _write(tmp_path / "x.jsonl", [{"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 5, "output_tokens": 1}}}}])
    u = log_view.usage([(c1, "claude"), (cx, "codex"), (c2, "agy")])
    assert u["sessions"] == 2 and u["cost_cents"] is None and not u["estimated"]
    none = log_view.usage([])
    assert none["sessions"] == 0 and none["cost_cents"] is None


def test_huge_transcript_reads_only_its_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(log_view, "TAIL", 400)
    rows = [_claude_msg(f"m{k}", "claude-haiku-5-5", 1, 1) for k in range(40)]
    u = log_view.claude_usage(_write(tmp_path / "big.jsonl", rows))
    assert 0 < u["out"] < 40


def test_redact_takes_out_private_details():
    text = "\n".join([
        f"⏺ Bash(cat {HOME}/alice/work/app/.env)",
        f"  ⎿  OPENAI_KEY={KEY}",
        f"To git{AT}github.com:acme/widgets.git",
        f"see https://ci.example.com/run/42?token=x and mail bob{AT}acme-corp.io",
        "ssh build-01.corp then 10.0.0.12",
        "cd ~/src/thing && ls",
        "  9 passed in 2.10s ~5 min",
    ])
    out, n = log_view.redact(text)
    for gone in (f"{HOME}/alice", KEY[:20], f"git{AT}github.com", "ci.example.com", f"bob{AT}acme-corp.io", "build-01.corp", "10.0.0.12", "~/src"):
        assert gone not in out, gone
    for kept in ("[path]", "[repo]", "[url]", "[email]", "[host]", "[ip]", "9 passed in 2.10s ~5 min"):
        assert kept in out, kept
    assert n >= 7


def test_redact_uses_the_owners_private_terms(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path))
    (tmp_path / "leak.re").write_text("zorblax\n")
    monkeypatch.setattr(diag, "_leak", False)
    out, n = log_view.redact("deploying Zorblax to prod")
    assert out == "deploying [private] to prod" and n == 1
    (tmp_path / "leak.re").write_text("(unclosed\n")
    monkeypatch.setattr(diag, "_leak", False)
    with pytest.raises(re.error):  # a broken leak.re fails closed: nothing is shared
        log_view.redact("anything")
    monkeypatch.setattr(diag, "_leak", False)


def test_cli_redact_and_usage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["log_view.py", "redact"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("ls /" + "Users/carol/x"))
    log_view.main()
    assert json.loads(capsys.readouterr().out) == {"text": "ls [path]", "removed": 1}
    import dash
    f = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 1, 1)])
    monkeypatch.setattr(dash, "transcript_files", lambda p: [(f, "claude")])
    monkeypatch.setattr(sys, "argv", ["log_view.py", "usage", "/w/x"])
    log_view.main()
    assert json.loads(capsys.readouterr().out)["sessions"] == 1
    monkeypatch.setattr(sys, "argv", ["log_view.py", "nope"])
    with pytest.raises(SystemExit):
        log_view.main()


def test_dash_logextras_and_logshare(tmp_path, monkeypatch, capsys):
    import dash
    f = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 1000, 10)])
    monkeypatch.setattr(dash, "live_agents", lambda: {"k": ({"displayName": "ws1", "path": str(tmp_path)}, None)})
    monkeypatch.setattr(dash, "transcript_files", lambda p: [(f, "claude")])
    calls = []
    monkeypatch.setattr(dash, "tree_diff", lambda p, mode: calls.append(mode) or ({"error": "no trunk"} if mode == "branch" else {"mode": "head", "files": []}))
    out = dash.log_extras("ws1")
    assert out["usage"]["sessions"] == 1 and out["files"] == {"mode": "head", "files": []} and calls == ["branch", "head"]
    assert dash.log_extras("nope") == {"error": "no workspace nope"}
    monkeypatch.setattr(sys, "argv", ["dash.py", "logshare", "ws1"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"cat {HOME}/dave/notes"))
    dash.main()
    assert json.loads(capsys.readouterr().out) == {"name": "ws1", "text": "cat [path]", "removed": 1}
    monkeypatch.setattr(sys, "argv", ["dash.py", "logextras", "ws1"])
    dash.main()
    assert json.loads(capsys.readouterr().out)["usage"]["in_tokens"] == 1000
    monkeypatch.setattr(dash, "log_extras", lambda n: 1 / 0)
    monkeypatch.setattr(dash, "oops", lambda a, e: None)
    dash.main()
    assert "error" in json.loads(capsys.readouterr().out)
