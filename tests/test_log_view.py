"""Tests for scripts/log_view.py (the agent log view's cost and share redaction) and dash.py's logextras/logshare."""
from __future__ import annotations

import io
import json
import re
import os
import sys
import tempfile
import time
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
    assert (u["in"], u["out"], u["cache_read"], u["cost_cents"]) == (900, 30, 400, None), "reasoning is counted in output already"
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


def test_redact_takes_out_credential_assignments_whatever_the_value():
    pw = "correct-horse-" + "battery-staple"
    out, n = log_view.redact(f"DATABASE_PASSWORD={pw}\nexport GH_TOKEN='{pw}'\napi_key: {pw}\nauth = [secret]\ntokens used: 40")
    assert pw not in out and n == 3
    assert "DATABASE_PASSWORD=[secret]" in out and "GH_TOKEN=[secret]" in out and "api_key: [secret]" in out
    assert out.endswith("tokens used: 40"), "a word that merely contains one isn't a credential"
    out, _ = log_view.redact('{"password": "correct horse battery staple", "user": "x"}\nDB_PASSWORD=correct horse battery staple')
    assert "horse" not in out and "battery" not in out, out
    assert out.startswith('{"password": [secret], "user": "x"}')
    out, _ = log_view.redact("curl -H 'Authorization: Bearer q9Zx7LmP2vR8wq' x")
    assert "q9Zx7" not in out
    out, _ = log_view.redact("mysql --password hunter2 -u root; gh --token abc --verbose")
    assert out == "mysql --password [secret] -u root; gh --token [secret] --verbose", out


def test_redact_takes_out_any_absolute_path_but_the_systems():
    out, _ = log_view.redact("cat /workspace/acme/private/roadmap.md /etc/ssl/private/key.pem; /usr/bin/env python3 -c 1/2; and/or")
    assert out == "cat [path] [path]; /usr/bin/env python3 -c 1/2; and/or", out


def test_redact_takes_paths_with_spaces_trailing_slashes_and_bracketed_secrets():
    out, _ = log_view.redact("cat /" + "Users/Jane Doe/Payroll 2025/plan.pdf; ls /acme-internal/ ; TOKEN=[abc123]; KEY=[secret]")
    assert "Doe" not in out and "Payroll" not in out and "acme-internal" not in out and "abc123" not in out, out
    assert out.endswith("KEY=[secret]"), out
    for spaced in ("/" + "Users/Jane Doe/Payroll 2025/plan.pdf", "/" + "Users/Jane Doe", "~/My Project", "/tmp/My Project"):
        assert log_view.redact(f"cat {spaced}")[0] == "cat [path]", spaced
    assert log_view.redact("cat /" + "Users/jane/my project/plan.pdf")[0] == "cat [path]", "a lowercase spaced folder goes too"
    for spaced in ('cd "/' + 'Users/Jane Doe/secret plans"', "/" + "Users/Jane Doe/Acme Payroll 2025", "/" + "Users/Jane Doe; ls"):
        out = log_view.redact(spaced)[0]
        assert not re.search(r"Doe|plans|Payroll|2025", out), out
    for v in ("password:\n  hunter2", '{"password":\n  "hunter2"}', "export AWS_SECRET_ACCESS_KEY=\nhunter2", "password: |\n  hunter2\nnext: 1",
              "password: >-\n    hunter2", "private_key:\n  - hunter2", "ssh_keys:\n  - hunter2",
              "password:\n  - a\n  # note\n  - hunter2\n", "password:\r\n  - hunter2\r\n", "password:\r\n  hunter2"):
        assert "hunter2" not in log_view.redact(v)[0], v
    assert log_view.redact("password: |\n  hunter2\nnext: 1")[0].endswith("next: 1")
    for v in ("+  password: |\n+    hunter2\n+  next: 1", "password: |\n  a\n\n  hunter2\nnext: 1", "password: !!binary |\n  hunter2",
              "password: &p\n  hunter2", "password: |2\n  hunter2",
              "password: | # copied from vault\n  hunter2\n", "password: |\n\n  hunter2\n"):
        assert "hunter2" not in log_view.redact(v)[0], v
    out = log_view.redact("db:\n  password: |\n    hunter2\n  port: 5432\nother: z")[0]
    assert out == "db:\n  password: [secret]\n  port: 5432\nother: z", "a block ends where its indent does"
    assert "# Next steps" in log_view.redact("Auth:\n- fixed login\n# Next steps\n- write docs\n")[0], "a heading ends a list"
    for slow in ("token" * 20000 + ":", "a_token" * 14000 + "x\n", "x.token" * 14000 + ": |", "a.b" * 30000, "a@b" + ".c" * 40000):
        t = time.time(); log_view.redact(slow)
        assert time.time() - t < 2, slow[:20]  # a line past 8000 characters is broken up first
    for kept in ("DB_PASSWORD=\nAPI_URL=x", "password:\nnext: 1", "$ export TOKEN=\n$ ls"):
        assert log_view.redact(kept)[0].split("\n")[1] == kept.split("\n")[1], kept
    out, _ = log_view.redact("password:\n  - hunter2\n  - swordfish\nnext: 1")
    assert "hunter2" not in out and "swordfish" not in out and out.endswith("next: 1"), out
    for bad in ("/tmp/x " + "A " * 24999 + "A/", "/tmp/x " + "Ab " * 16666 + "Ab'", "/a/b" + " Ab" * 20000):
        t = time.time(); log_view.redact(bad)
        assert time.time() - t < 0.5, len(bad)
    assert log_view.redact("cp /acme/a.txt docs/b.txt")[0] == "cp [path]", "so a relative path after one goes with it: privacy first"
    assert log_view.redact("cat /srv/app/x.py Then I fixed it.")[0] == "cat [path] Then I fixed it.", "a sentence after a path stays"
    assert "def456" not in log_view.redact("API_KEYS=[abc123, def456]")[0]
    for v in ("PASSWORD=[x]hunter2", 'tokens = [["a"],["c","d"]]'):
        assert "hunter2" not in log_view.redact(v)[0] and '"c"' not in log_view.redact(v)[0], v
    assert log_view.redact("ls /tmp/x\nfoo/bar\t/var/log/syslog\tand/or")[0] == "ls [path]\nfoo/bar\t[path]\tand/or", "a path never runs into the next line"


def test_usage_cache_saves_a_growing_sessions_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(log_view, "_cache_file", lambda: tmp_path / "c.json")
    f = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 10, 1)])
    log_view.usage([(f, "claude")])
    with f.open("a") as fh:
        fh.write(json.dumps(_claude_msg("b", "claude-opus-5-5", 5, 1)) + "\n")
    log_view.usage([(f, "claude")])
    saved = json.loads((tmp_path / "c.json").read_text())[str(f)]["st"]["offset"]
    assert saved == f.stat().st_size, "the new offset is saved, so the next poll doesn't re-read the growth"


def test_redact_leaves_slash_commands_and_endpoints():
    out, _ = log_view.redact(f"> /goal fix the build; run /review now; GET /api/v1/users 200; /bin-x{HOME}/alice")
    assert out.startswith("> /goal fix the build; run /review now; GET [path] 200; [path]"), out  # a status code isn't a name


def test_redact_takes_a_url_whole_brackets_included():
    out, _ = log_view.redact("see (https://ci.example.com/job/(internal)/artifact?signature=abc123def). Done")
    assert out == "see ([url]). Done", out


def test_redact_takes_out_the_workspaces_own_name_and_path():
    out, n = log_view.redact("On branch secret-proj-fix in /srv/w/secret-proj-fix", ("secret-proj-fix", "/srv/w/secret-proj-fix"))
    assert "secret-proj" not in out and n == 2, out


def test_usage_counts_a_resumed_sessions_carried_messages_once_and_an_unreadable_session_drops_the_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(log_view, "_cache_file", lambda: tmp_path / "c.json")
    old = _write(tmp_path / "a.jsonl", [_claude_msg("m1", "claude-opus-5-5", 100, 10)])
    os.utime(old, (1, 1))
    new = _write(tmp_path / "b.jsonl", [_claude_msg("m1", "claude-opus-5-5", 100, 10), _claude_msg("m2", "claude-opus-5-5", 5, 1)])
    u = log_view.usage([(new, "claude"), (old, "claude")])
    assert (u["in_tokens"], u["out_tokens"], u["sessions"]) == (105, 11, 2)
    agy = _write(tmp_path / "transcript.jsonl", [{"x": 1}])
    assert log_view.usage([(new, "claude"), (agy, "agy")])["cost_cents"] is None, "an agy session's tokens are unknown: no partial cost"


def test_usage_starts_over_on_a_replaced_file_and_prunes_gone_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(log_view, "_cache_file", lambda: tmp_path / "c.json")
    f = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 10, 1)])
    assert log_view.usage([(f, "claude")])["in_tokens"] == 10
    f.unlink()
    _write(f, [_claude_msg("z", "claude-opus-5-5", 3, 1), _claude_msg("y", "claude-opus-5-5", 4, 1), _claude_msg("x", "claude-opus-5-5", 5, 1)])
    assert log_view.usage([(f, "claude")])["in_tokens"] == 12, "another file at the same path, longer: read from its start"
    g = _write(tmp_path / "g.jsonl", [_claude_msg("g", "claude-opus-5-5", 1, 1)])
    log_view.usage([(g, "claude")])
    f.unlink()
    log_view.usage([(g, "claude")])
    assert set(json.loads((tmp_path / "c.json").read_text())) == {str(g)}, "a deleted transcript's entry goes"
    before = (tmp_path / "c.json").stat().st_mtime_ns
    log_view.usage([(g, "claude")])
    assert (tmp_path / "c.json").stat().st_mtime_ns == before, "nothing changed: no write"


def test_subagent_transcripts_count_their_tokens_but_not_as_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(log_view, "_cache_file", lambda: tmp_path / "c.json")
    main = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 10, 1)])
    sub = _write(tmp_path / "agent-1.jsonl", [_claude_msg("b", "claude-opus-5-5", 5, 1)])
    u = log_view.usage([(main, "claude"), (sub, "claude-sub")])
    assert (u["in_tokens"], u["sessions"]) == (15, 1)


def test_redact_catches_prompts_env_paths_file_urls_and_windows_homes(monkeypatch):
    monkeypatch.setattr(log_view, "_hosts", lambda: ["devbox.example.org", "devbox"])
    lines = [f"alice{AT}mini:~/orca/workspaces/x$ ls", f"PATH=/usr/bin:{HOME}/alice/bin", "open file:///" + "Users/alice/x.txt",
             f"ssh alice{AT}buildbox", "C:\\" + "Users\\alice\\x", "ping devbox", "react@18 and lodash"]
    out, _ = log_view.redact("\n".join(lines))
    for gone in ("alice", "mini:", "buildbox", "devbox"):
        assert gone not in out, (gone, out)
    assert "react@18" in out, "a package version isn't a user@host"


def test_usage_reads_only_what_a_live_transcript_added(tmp_path, monkeypatch):
    monkeypatch.setattr(log_view, "_cache_file", lambda: tmp_path / "cache" / "usage.json")
    f = _write(tmp_path / "s.jsonl", [_claude_msg("a", "claude-opus-5-5", 10, 1)])
    starts = []
    real = log_view._read
    monkeypatch.setattr(log_view, "_read", lambda p, off=0: starts.append(off) or real(p, off))
    assert log_view.usage([(f, "claude")])["in_tokens"] == 10
    size = f.stat().st_size
    assert log_view.usage([(f, "claude")])["in_tokens"] == 10 and starts == [0, size], "the second poll starts where the first ended"
    with f.open("a") as fh:  # the live message streams: its usage again, fuller, then a new one; then half a line
        fh.write(json.dumps(_claude_msg("a", "claude-opus-5-5", 10, 7)) + "\n" + json.dumps(_claude_msg("b", "claude-opus-5-5", 5, 1)) + "\n" + '{"message": {"usa')
    u = log_view.usage([(f, "claude")])
    assert (u["in_tokens"], u["out_tokens"]) == (15, 8), "a's fuller usage replaces its first, once"
    f.write_text("")  # rewritten shorter: read again from the start
    assert log_view.usage([(f, "claude")])["sessions"] == 0
    (tmp_path / "cache" / "usage.json").write_text("not json")
    _write(f, [_claude_msg("a", "claude-opus-5-5", 10, 1)])
    assert log_view.usage([(f, "claude")])["in_tokens"] == 10, "a broken cache is only a miss"


def test_redact_takes_out_this_accounts_name_but_not_inside_other_words(monkeypatch):
    monkeypatch.setattr(log_view, "_user", lambda: "pat")
    monkeypatch.setattr(log_view, "_hosts", list)
    out, n = log_view.redact("git push origin pat/fix-retry; Pat said ok; compatible patterns")
    assert out == "git push origin [user]/fix-retry; [user] said ok; compatible patterns" and n == 2
    out, _ = log_view.redact("ls projects/-home-pat-orca-ws; tmux attach -t pat_dev; git checkout pat-fix-191")
    assert "pat" not in out.replace("[user]", ""), out
    monkeypatch.setattr(log_view, "_user", lambda: "ci")  # too short to be told from ordinary text
    assert log_view.redact("ci passed")[0] == "ci passed"


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
    monkeypatch.setattr(dash, "_ws_path", lambda n: str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["dash.py", "logshare", "ws1", "--req", "7"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"cat {HOME}/dave/notes"))
    dash.main()
    assert json.loads(capsys.readouterr().out) == {"name": "ws1", "req": 7, "text": "cat [path]", "removed": 1}
    monkeypatch.setattr(sys, "argv", ["dash.py", "logextras", "ws1"])
    dash.main()
    assert json.loads(capsys.readouterr().out)["usage"]["in_tokens"] == 1000
    monkeypatch.setattr(dash, "log_extras", lambda n: 1 / 0)
    monkeypatch.setattr(dash, "oops", lambda a, e: None)
    dash.main()
    assert "error" in json.loads(capsys.readouterr().out)
