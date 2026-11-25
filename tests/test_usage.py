"""Tests for scripts/usage.py: provider parsing, rollout fallback, cache locking, self-healing, and UI block parity."""
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import usage  # noqa: E402

CLAUDE_REC = {
    "five_hour": None,
    "seven_day": {"utilization": 64.0, "resets_at": "2026-11-28T07:00:00.418281+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False, "utilization": 0.0},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 64, "resets_at": "2026-11-28T07:00:00.418281+00:00", "scope": None},
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 15,
            "resets_at": "2026-11-28T07:00:00.418439+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
        },
    ],
}
CLAUDE_NEAR = {
    "five_hour": {"utilization": 92.0, "resets_at": "2026-11-24T23:00:00+00:00"},
    "seven_day": {"utilization": 100.0, "resets_at": "2026-11-28T07:00:00+00:00"},
}
CODEX_REC = {
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 46, "limit_window_seconds": 604800, "reset_after_seconds": 175882, "reset_at": 1795739864},
        "secondary_window": None,
    },
    "additional_rate_limits": None,
}
CODEX_5H = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 81, "limit_window_seconds": 18000, "reset_at": 1795570000},
        "secondary_window": {"used_percent": 30, "limit_window_seconds": 604800, "reset_at": 1795739864},
    },
}


def _rollout_line(resets_at=None, ts="2026-11-24T20:00:00.000Z", pct=46.0):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": pct, "window_minutes": 10080, "resets_at": resets_at or int(time.time()) + 86400},
                "secondary": None,
                "plan_type": "pro",
            },
        },
    }


def _answers(by_url):
    """usage._get with recorded answers by URL (an exception instance is raised), and the calls it got."""
    calls = []

    def get(url, headers, body=None):
        calls.append((url, headers, body))
        a = by_url[url]
        if isinstance(a, Exception):
            raise a
        return a

    return get, calls


AGY_SUMMARY = {"groups": [{
    "displayName": "Claude and GPT models",
    "description": "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
    "buckets": [
        {"bucketId": "3p-weekly", "window": "weekly", "resetTime": "2026-12-01T05:08:18Z", "remainingFraction": 0.75},
        {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 1},
    ],
}]}
AGY_QUOTA = {"buckets": [
    {"resetTime": "2026-11-25T20:51:25Z", "tokenType": "WTUS", "modelId": "claude-opus-5-5-high", "remainingFraction": 1},
    {"resetTime": "2026-11-25T20:51:25Z", "tokenType": "WTUS", "modelId": "gpt-oss-120b-medium", "remainingFraction": 1},
    {"resetTime": "2026-11-25T18:40:40Z", "tokenType": "WTUS", "modelId": "gemini-3-flash", "remainingFraction": 0.875},
    {"resetTime": "2026-11-25T18:40:40Z", "tokenType": "WTUS", "modelId": "gemini-4-hydrogen-sonar", "remainingFraction": 0.875},
    {"tokenType": "WTUS", "modelId": "tab_flash_lite_preview", "remainingFraction": 1},
]}
GOOGLE_LCA = {"currentTier": {"id": "free-tier", "name": "Antigravity"}, "paidTier": {"id": "g1-ultra-lite-tier", "name": "Google AI Ultra"},
              "cloudaicompanionProject": "proj-1"}
GEMINI_QUOTA = {"buckets": [
    {"modelId": "gemini-2.5-pro", "tokenType": "REQUESTS", "remainingFraction": 0.4, "resetTime": "2026-11-26T00:00:00Z"},
    {"modelId": "gemini-2.5-pro_vertex", "tokenType": "REQUESTS", "remainingFraction": 0.4, "resetTime": "2026-11-26T00:00:00Z"},
    {"modelId": "gemini-3-pro-preview", "tokenType": "REQUESTS", "remainingFraction": 0.9, "resetTime": "2026-11-26T00:00:00Z"},
    {"modelId": "gemini-2.5-flash", "tokenType": "REQUESTS", "remainingFraction": 1, "resetTime": "2026-11-26T00:00:00Z"},
]}


@pytest.fixture
def provider_home(tmp_path, monkeypatch):
    for d in (".claude", ".codex", "share/opencode", "pi"):
        (tmp_path / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setattr(usage.sys, "platform", "linux")
    return tmp_path


def test_providers_happy_near_and_failures(provider_home, monkeypatch):
    future_ms = int((time.time() + 3600) * 1000)
    (provider_home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-c", "expiresAt": future_ms, "subscriptionType": "max"}})
    )
    (provider_home / ".codex" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "tok-x", "account_id": "acct-1"}})
    )
    (provider_home / "share" / "opencode" / "auth.json").write_text(json.dumps({"opencode-go": {"type": "api", "key": "k"}}))

    get, calls = _answers({usage.CLAUDE_URL: CLAUDE_REC, usage.CODEX_URL: CODEX_REC})
    monkeypatch.setattr(usage, "_get", get)
    c = usage.fetch("claude")
    assert c["state"] == "ok" and c["plan"] == "Claude Max" and c["src"] == "api"
    assert [(w["label"], w["pct"]) for w in c["windows"]] == [("Week", 64.0), ("Fable", 15.0)]
    assert c["windows"][0]["resets"] == int(datetime(2026, 11, 28, 7, 0, 0, 418281, tzinfo=UTC).timestamp())
    assert calls[-1][1]["Authorization"] == "Bearer tok-c" and calls[-1][1]["anthropic-beta"]

    x = usage.fetch("codex")
    assert x["state"] == "ok" and x["plan"] == "ChatGPT Pro"
    assert [(w["label"], w["pct"], w["resets"]) for w in x["windows"]] == [("Week", 46.0, 1795739864)]
    assert calls[-1][1]["ChatGPT-Account-Id"] == "acct-1"

    o = usage.fetch("opencode")
    assert o["state"] == "na" and o["plan"] == "OpenCode Go" and "no usage API" in o["note"]
    assert usage.fetch("agy")["state"] == "out" and usage.fetch("gemini")["state"] == "out"
    assert usage.fetch("cursor")["state"] == "na"
    assert usage.fetch("pi")["state"] == "out"

    monkeypatch.setattr(usage, "_get", _answers({usage.CLAUDE_URL: CLAUDE_NEAR, usage.CODEX_URL: CODEX_5H})[0])
    assert [(w["label"], w["pct"]) for w in usage.fetch("claude")["windows"]] == [("5h", 92.0), ("Week", 100.0)]
    assert [(w["label"], w["pct"]) for w in usage.fetch("codex")["windows"]] == [("5h", 81.0), ("Week", 30.0)]

    # 429 rate limit backs off 5m and merge() keeps prior windows
    monkeypatch.setattr(usage, "_get", _answers({usage.CLAUDE_URL: usage.Failed("rate limited", "rate_limit", usage.RATE_WAIT)})[0])
    f = usage.fetch("claude")
    assert f["state"] == "failed" and f["next"] - f["tried"] == usage.RATE_WAIT
    kept = usage.merge(c, f)
    assert kept["state"] == "failed" and kept["windows"] == c["windows"] and kept["at"] == c["at"] and kept["note"] == "rate limited"
    assert usage.view(kept)["state"] == "failed" and "next" not in usage.view(kept)

    # expired token makes no API call
    (provider_home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-c", "expiresAt": 1000}}))
    get, calls = _answers({})
    monkeypatch.setattr(usage, "_get", get)
    e = usage.fetch("claude")
    assert e["state"] == "failed" and "expired" in e["note"] and not calls


def _agy_token(home, expiry="2099-01-01T00:00:00.123456789-05:00"):
    d = home / ".gemini" / "antigravity-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "antigravity-oauth-token").write_text(json.dumps(
        {"token": {"access_token": "tok-g", "token_type": "Bearer", "refresh_token": "r", "expiry": expiry}, "auth_method": "consumer"}))


def test_agy_summary_pools_plan_and_failures(provider_home, monkeypatch):
    _agy_token(provider_home)
    q, s, lca = (usage.AGY_URL + m for m in ("retrieveUserQuota", "retrieveUserQuotaSummary", "loadCodeAssist"))
    get, calls = _answers({s: AGY_SUMMARY, q: AGY_QUOTA, lca: GOOGLE_LCA})
    monkeypatch.setattr(usage, "_get", get)
    a = usage.fetch("agy")
    assert a["state"] == "ok" and a["plan"] == "Google AI Ultra" and a["src"] == "api"
    # 5h before Week; the Claude and GPT buckets are the summary's group, not a pool of their own; the tab models are left out
    assert [(w["label"], w["pct"]) for w in a["windows"]] == [("5h", 0.0), ("Week", 25.0), ("Gemini", 12.5)]
    assert a["windows"][0]["resets"] is None and a["windows"][2]["resets"] == int(datetime(2026, 11, 25, 18, 40, 40, tzinfo=UTC).timestamp())
    assert a["note"] == "5h and Week: Claude and GPT models"
    assert all(c[2] is not None and c[1]["Authorization"] == "Bearer tok-g" and "antigravity" in c[1]["User-Agent"] for c in calls)

    # the summary fails: every pool from the per-model buckets; the plan's call failing only drops the plan
    get, _ = _answers({s: usage.Failed("x", "http"), q: AGY_QUOTA, lca: usage.Failed("x", "network")})
    monkeypatch.setattr(usage, "_get", get)
    a = usage.fetch("agy")
    assert a["state"] == "ok" and "plan" not in a and "note" not in a
    assert [(w["label"], w["pct"]) for w in a["windows"]] == [("Claude", 0.0), ("Gemini", 12.5)]

    # both fail: the first failure, with its backoff
    get, _ = _answers({s: usage.Failed("rate limited", "rate_limit", usage.RATE_WAIT), q: usage.Failed("y", "http"), lca: GOOGLE_LCA})
    monkeypatch.setattr(usage, "_get", get)
    f = usage.fetch("agy")
    assert f["state"] == "failed" and f["note"] == "rate limited" and f["next"] - f["tried"] == usage.RATE_WAIT

    # an expired token makes no call; a token file without one is signed out
    _agy_token(provider_home, "2020-01-01T00:00:00Z")
    get, calls = _answers({})
    monkeypatch.setattr(usage, "_get", get)
    e = usage.fetch("agy")
    assert e["state"] == "failed" and "expired" in e["note"] and not calls
    (provider_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text(json.dumps({"token": {}}))
    assert usage.fetch("agy")["state"] == "out"


def test_gemini_cli_pools_project_and_api_key(provider_home, monkeypatch):
    g = provider_home / ".gemini"
    g.mkdir()
    (g / "oauth_creds.json").write_text(json.dumps({"access_token": "tok-m", "expiry_date": int((time.time() + 3600) * 1000)}))
    lca, q = usage.GEMINI_URL + "loadCodeAssist", usage.GEMINI_URL + "retrieveUserQuota"
    get, calls = _answers({lca: {**GOOGLE_LCA, "paidTier": None}, q: GEMINI_QUOTA})
    monkeypatch.setattr(usage, "_get", get)
    m = usage.fetch("gemini")
    assert m["state"] == "ok" and m["plan"] == "Antigravity"
    # two Pro pools are named by their model; the _vertex twin is left out
    assert [(w["label"], w["pct"]) for w in m["windows"]] == [("2.5-pro", 60.0), ("3-pro-prev", 10.0), ("Flash", 0.0)]
    assert calls[-1][2] == {"project": "proj-1"} and calls[-1][1]["User-Agent"].startswith("GeminiCLI/")

    get, calls = _answers({lca: {"cloudaicompanionProject": {"id": "proj-2"}}, q: {"buckets": "junk"}})
    monkeypatch.setattr(usage, "_get", get)
    m = usage.fetch("gemini")
    assert m["state"] == "ok" and m["windows"] == [] and "plan" not in m and calls[-1][2] == {"project": "proj-2"}

    (g / "oauth_creds.json").write_text(json.dumps({"access_token": "tok-m", "expiry_date": 1000}))
    assert "expired" in usage.fetch("gemini")["note"]
    (g / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}))
    n = usage.fetch("gemini")
    assert n["state"] == "na" and "API key" in n["note"]
    (g / "settings.json").write_text(json.dumps({"security": "junk"}))
    (g / "oauth_creds.json").unlink()
    assert usage.fetch("gemini")["state"] == "out"


def test_google_summary_with_several_groups():
    d = {"groups": [
        {"displayName": "Claude and GPT models", "buckets": [{"window": "weekly", "remainingFraction": 0.5}]},
        {"displayName": "Gemini models", "buckets": [{"window": "daily", "remainingFraction": 0.2}, {"window": "odd", "remainingFraction": 1}, "junk"]},
        "junk",
    ]}
    wins, covered, note = usage.google_summary(d)
    assert [(w["label"], w["pct"]) for w in wins] == [("Claude Week", 50.0), ("Gemini Day", 80.0), ("Gemini Limit", 0.0)]
    assert covered == {"Claude", "GPT", "Gemini"} and note is None
    assert usage.google_pools(AGY_QUOTA["buckets"], covered) == []
    assert usage.google_summary({"groups": None}) == ([], set(), None)


def test_get_posts_json_bodies(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": 1}'

    def fake_open(req, timeout):
        seen.update(method=req.get_method(), data=req.data, ctype=req.get_header("Content-type"), ua=req.get_header("User-agent"))
        return Resp()

    monkeypatch.setattr(usage._OPENER, "open", fake_open)
    assert usage._get(usage.AGY_URL + "retrieveUserQuota", {"User-Agent": "antigravity"}, {}) == {"ok": 1}
    assert seen == {"method": "POST", "data": b"{}", "ctype": "application/json", "ua": "antigravity"}
    usage._get(usage.CLAUDE_URL, {})
    assert seen["method"] == "GET" and seen["data"] is None and seen["ctype"] is None and seen["ua"] == "greenlight-usage"


def test_claude_keychain_fallback_even_with_other_credentials_json(provider_home, monkeypatch):
    monkeypatch.setattr(usage.sys, "platform", "darwin")
    # Non-empty .credentials.json without claudeAiOauth must still fall back to Keychain
    (provider_home / ".claude" / ".credentials.json").write_text(json.dumps({"mcpOAuth": {"server": {"token": "x"}}}))
    future_ms = int((time.time() + 3600) * 1000)
    kc_json = json.dumps({"claudeAiOauth": {"accessToken": "tok-kc", "expiresAt": future_ms, "subscriptionType": "max"}})
    get, calls = _answers({usage.CLAUDE_URL: CLAUDE_REC})
    monkeypatch.setattr(usage, "_get", get)
    with mock.patch.object(usage.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=kc_json)):
        c = usage.fetch("claude")
        assert c["state"] == "ok" and calls[-1][1]["Authorization"] == "Bearer tok-kc"

    (provider_home / ".claude" / ".credentials.json").unlink()
    with mock.patch.object(usage.subprocess, "run", return_value=mock.Mock(returncode=44, stdout="")):
        assert usage.fetch("claude")["state"] == "out"
    with mock.patch.object(usage.subprocess, "run", return_value=mock.Mock(returncode=128, stdout="")):
        kd = usage.fetch("claude")
        assert kd["state"] == "failed" and kd["wait"] == usage.KEYCHAIN_WAIT and kd["next"] - kd["tried"] == usage.KEYCHAIN_WAIT


def test_codex_rollout_fallback_and_merge_does_not_regress(provider_home, monkeypatch):
    (provider_home / ".codex" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "tok-x", "account_id": "acct-1"}})
    )
    day = provider_home / ".codex" / "sessions" / "2026" / "11" / "24"
    day.mkdir(parents=True)
    rl = _rollout_line(ts="2026-11-24T20:00:00.000Z", pct=20.0)
    (day / "rollout-a.jsonl").write_text(json.dumps({"type": "session_meta"}) + "\n" + json.dumps(rl) + "\n" + json.dumps({"type": "x"}) + "\n")

    # API auth error falls back to rollout
    monkeypatch.setattr(usage, "_get", _answers({usage.CODEX_URL: usage.Failed("signed out or the token expired", "auth")})[0])
    r = usage.fetch("codex")
    assert r["state"] == "ok" and r["src"] == "rollout" and r["windows"][0]["pct"] == 20.0
    assert r["at"] == int(datetime(2026, 11, 24, 20, tzinfo=UTC).timestamp()) and "last session" in r["note"]

    # 429 carries RATE_WAIT backoff even on rollout fallback
    monkeypatch.setattr(usage, "_get", _answers({usage.CODEX_URL: usage.Failed("rate limited: trying again in 5 minutes", "rate_limit", usage.RATE_WAIT)})[0])
    r429 = usage.fetch("codex")
    assert r429["state"] == "ok" and r429["src"] == "rollout" and r429["next"] - r429["tried"] == usage.RATE_WAIT and r429["wait"] == usage.RATE_WAIT
    assert not usage.due(r429, r429["tried"] + 20, r429["tried"] + 20) and "wait" not in usage.view(r429)

    # Fresh cached API reading must NOT be clobbered by an older rollout on 429; preserves fresher windows & failed state
    fresher_api = {"id": "codex", "name": "Codex", "state": "ok", "src": "api", "plan": "ChatGPT Pro",
                   "at": r429["at"] + 3600, "windows": [{"label": "Week", "pct": 75.0, "resets": int(time.time()) + 86400}]}
    merged = usage.merge(fresher_api, r429)
    assert merged["state"] == "failed" and merged["src"] == "api" and merged["windows"][0]["pct"] == 75.0
    assert merged["at"] == fresher_api["at"] and merged["wait"] == usage.RATE_WAIT
    assert merged["note"] == "rate limited: trying again in 5 minutes" and "_fail" not in merged

    # A rollout that IS newer than the cached reading wins with state="ok"
    older_api = {**fresher_api, "at": r429["at"] - 3600}
    merged_newer = usage.merge(older_api, r429)
    assert merged_newer["state"] == "ok" and merged_newer["src"] == "rollout" and merged_newer["windows"][0]["pct"] == 20.0
    assert "_fail" not in merged_newer

    # Torn auth.json falls back to rollout (and preserves fresher API via merge too)
    (provider_home / ".codex" / "auth.json").write_text('{"tokens": {"access_')
    torn = usage.fetch("codex")
    assert torn["src"] == "rollout" and "last session" in torn["note"]
    assert usage.merge(fresher_api, torn)["windows"][0]["pct"] == 75.0 and usage.merge(fresher_api, torn)["state"] == "failed"

    (day / "rollout-a.jsonl").rename(day / "rollout-a.off")
    assert usage.fetch("codex")["state"] == "failed"

    # Trailing invalid rate_limits line does not hide an earlier valid line in the same rollout file
    invalid_rl = {"timestamp": "2026-11-24T21:00:00.000Z", "payload": {"rate_limits": {"primary": {"used_percent": None, "window_minutes": 300}}}}
    (day / "rollout-a.jsonl").write_text(json.dumps(rl) + "\n" + json.dumps(invalid_rl) + "\n")
    assert usage.fetch("codex")["windows"][0]["pct"] == 20.0

    # Window with no reset time is unknown, not reset
    (day / "rollout-a.jsonl").write_text(json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 40, "window_minutes": 300}}}}) + "\n")
    assert usage.fetch("codex")["windows"][0]["pct"] == 40.0

    # API-key-only (no auth.json) reads rollout; long sessions found by mtime across last 7 day folders
    (provider_home / ".codex" / "auth.json").unlink()
    sess = provider_home / ".codex" / "sessions" / "2026" / "11"
    for d in ("18", "22", "23", "24"):
        (sess / d).mkdir(parents=True, exist_ok=True)
    (sess / "18" / "rollout-long.jsonl").write_text(json.dumps(rl) + "\n")
    for i, d in enumerate(("22", "23", "24")):
        (sess / d / f"rollout-{d}.jsonl").write_text(json.dumps({"type": "x"}) + "\n")
        os.utime(sess / d / f"rollout-{d}.jsonl", (time.time() - 3600 * (3 - i), time.time() - 3600 * (3 - i)))
    (day / "rollout-a.jsonl").unlink()
    assert usage.fetch("codex")["windows"][0]["pct"] == 20.0

    # Older rollout whose windows have all reset doesn't hide an older-mtime rollout with live limits
    old = {
        "timestamp": "2026-11-20T08:00:00.000Z",
        "payload": {"rate_limits": {"primary": {"used_percent": 99.0, "window_minutes": 300, "resets_in_seconds": 300}}},
    }
    t = int(datetime(2026, 11, 20, 8, tzinfo=UTC).timestamp())
    assert usage.codex_windows(old["payload"]["rate_limits"], t)[0]["resets"] == t + 300
    (sess / "18" / "rollout-long.jsonl").unlink()
    (day / "rollout-a.jsonl").write_text(json.dumps(old) + "\n")
    assert usage.codex_rollout(provider_home / ".codex") is None and usage.fetch("codex")["state"] == "out"
    (day / "rollout-live.jsonl").write_text(json.dumps(rl) + "\n")
    os.utime(day / "rollout-live.jsonl", (time.time() - 600, time.time() - 600))
    os.utime(day / "rollout-a.jsonl", (time.time(), time.time()))
    assert usage.codex_rollout(provider_home / ".codex")["windows"][0]["pct"] == 20.0


def test_pi_providers_and_plan_preservation(provider_home, monkeypatch):
    future_ms = int((time.time() + 3600) * 1000)
    (provider_home / "pi" / "auth.json").write_text(json.dumps({"anthropic": {"type": "oauth", "access": "tok-p", "expires": future_ms}}))
    get, calls = _answers({usage.CLAUDE_URL: CLAUDE_REC})
    monkeypatch.setattr(usage, "_get", get)
    p = usage.fetch("pi")
    assert p["state"] == "ok" and p["windows"][0]["pct"] == 64.0 and calls[-1][1]["Authorization"] == "Bearer tok-p"

    (provider_home / "pi" / "settings.json").write_text(json.dumps({"defaultProvider": "openrouter"}))
    p = usage.fetch("pi")
    assert p["state"] == "na" and "openrouter" in p["note"]

    (provider_home / "pi" / "auth.json").write_text(json.dumps({"github-copilot": {"type": "oauth", "access": "old", "expires": 1000}}))
    (provider_home / "pi" / "settings.json").write_text(json.dumps({"defaultProvider": "github-copilot"}))
    assert usage.fetch("pi")["state"] == "na"

    (provider_home / "pi" / "settings.json").unlink()
    (provider_home / "pi" / "auth.json").write_text(json.dumps({
        "anthropic": {"type": "oauth", "access": "old", "expires": 1000},
        "openai-codex": {"type": "oauth", "access": "tok-r", "expires": future_ms},
    }))
    get, calls = _answers({usage.CODEX_URL: CODEX_REC})
    monkeypatch.setattr(usage, "_get", get)
    p = usage.fetch("pi")
    assert p["state"] == "ok" and calls[-1][1]["Authorization"] == "Bearer tok-r" and p["plan"] == "ChatGPT Pro"

    (provider_home / "pi" / "auth.json").write_text(json.dumps({"anthropic": {"type": "oauth", "access": "old", "expires": 1000}}))
    assert "expired" in usage.fetch("pi")["note"]

    # openai-codex keeps specific plan_type from API and falls back to "ChatGPT plan" when omitted
    (provider_home / "pi" / "auth.json").write_text(
        json.dumps({"openai-codex": {"type": "oauth", "access": "tok-q", "accountId": "acct-2", "expires": future_ms}})
    )
    (provider_home / "pi" / "settings.json").write_text(json.dumps({"defaultProvider": "openai-codex"}))
    no_plan = {"rate_limit": CODEX_REC["rate_limit"]}
    get, calls = _answers({usage.CODEX_URL: no_plan})
    monkeypatch.setattr(usage, "_get", get)
    p = usage.fetch("pi")
    assert p["windows"][0]["pct"] == 46.0 and calls[-1][1]["ChatGPT-Account-Id"] == "acct-2" and p["plan"] == "ChatGPT plan"


def test_http_errors_incomplete_read_redirects_and_permission_error(provider_home, monkeypatch):
    for code, want in ((429, "rate_limit"), (401, "auth"), (500, "http"), (302, "http")):
        with mock.patch.object(usage._OPENER, "open", side_effect=urllib.error.HTTPError("u", code, "x", {}, None)):
            with pytest.raises(usage.Failed) as ei:
                usage._get(usage.CLAUDE_URL, {})
            assert ei.value.code == want and (ei.value.wait == usage.RATE_WAIT) == (code == 429)

    rreq = urllib.request.Request(usage.CLAUDE_URL, headers={"Authorization": "Bearer secret"})
    assert usage._NoRedirect().redirect_request(rreq, None, 302, "Found", {}, "https://evil.example/") is None
    with pytest.raises(urllib.error.HTTPError) as ei:
        usage._OPENER.error("http", rreq, None, 302, "Found", {"Location": "https://evil.example/"})
    assert ei.value.code == 302

    for exc in (urllib.error.URLError("down"), http.client.IncompleteRead(b"part"), http.client.BadStatusLine("bad")):
        with mock.patch.object(usage._OPENER, "open", side_effect=exc):
            with pytest.raises(usage.Failed) as ei:
                usage._get(usage.CLAUDE_URL, {})
            assert ei.value.code == "network"

    # IncompleteRead triggers Codex rollout fallback instead of generic unexpected failure
    (provider_home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "tok-x"}}))
    day = provider_home / ".codex" / "sessions" / "2026" / "11" / "24"
    day.mkdir(parents=True)
    (day / "rollout-a.jsonl").write_text(json.dumps(_rollout_line(pct=33.0)) + "\n")
    with mock.patch.object(usage._OPENER, "open", side_effect=http.client.IncompleteRead(b"half")):
        r = usage.fetch("codex")
        assert r["state"] == "ok" and r["src"] == "rollout" and r["windows"][0]["pct"] == 33.0

    # PermissionError on a sign-in file raises Failed("permission"), not SignedOut
    cred = provider_home / ".claude" / ".credentials.json"
    cred.write_text("{}")
    with mock.patch("builtins.open", side_effect=PermissionError("denied")):
        f = usage.fetch("claude")
        assert f["state"] == "failed" and "sign-in file" in f["note"]


def test_boundary_helpers_and_unexpected_exception():
    with mock.patch.dict(usage.SOURCES, {"claude": mock.Mock(side_effect=TypeError("x"))}):
        u = usage.fetch("claude")
        assert u["state"] == "failed" and u["next"] > u["tried"] and "unexpected" in u["note"]

    assert usage._epoch(float("inf")) is None and usage._epoch(True) is None
    assert usage._epoch("2026-11-28T07:00:00") == int(datetime(2026, 11, 28, 7, tzinfo=UTC).timestamp())
    assert usage._epoch("2026-11-28T07:00:00z") == int(datetime(2026, 11, 28, 7, tzinfo=UTC).timestamp())
    assert usage._win("5h", float("nan"), 0) is None
    assert usage._expired(float("inf")) is False and usage._expired(0) is False and usage._expired(1000) is True
    assert usage._expired("2020-01-01T00:00:00Z") is True and usage._expired("2099-01-01T00:00:00Z") is False
    assert usage._span_label(True) == "Limit" and usage._span_label(float("nan")) == "Limit" and usage._span_label(float("inf")) == "Limit"
    assert usage._span_label(900) == "15m" and usage._span_label(1800) == "30m" and usage._span_label(10) == "1m"
    assert usage.codex_windows({"primary": {"used_percent": 12, "window_minutes": {"bad": 1}, "resets_in_seconds": False}}) == [
        {"label": "Limit", "pct": 12.0, "resets": None}
    ]
    assert usage.codex_windows({"primary": {"used_percent": 5, "window_seconds": 18000}})[0]["label"] == "5h"
    assert usage.codex_windows({"primary_window": {"used_percent": 25, "limit_window_seconds": 3600, "reset_after_seconds": 120}}, at=1000) == [
        {"label": "1h", "pct": 25.0, "resets": 1120}
    ]


def test_current_cache_self_healing_and_save_errors(monkeypatch):
    fetched = []

    def fake_fetch(name):
        fetched.append(name)
        now = int(time.time())
        if name == "codex":
            return {"id": name, "name": "Codex", "state": "out", "tried": now, "next": now + 300}
        return {"id": name, "name": name.title(), "state": "ok", "tried": now, "at": now, "windows": [], "next": now + 60}

    monkeypatch.setenv("GREENLIGHT_USAGE", "")
    monkeypatch.setattr(usage, "fetch", fake_fetch)
    monkeypatch.setattr(usage.harness, "installed", lambda n: n != "gemini")

    (usage.STATE / "usage.json").unlink(missing_ok=True)
    assert [p["id"] for p in usage.current(["pi", "codex", "claude", "gemini", "claude", "", "  "])] == ["pi", "claude"]
    assert sorted(fetched) == ["claude", "codex", "pi"]

    fetched.clear()
    assert [p["id"] for p in usage.current(["pi", "claude"])] == ["pi", "claude"] and not fetched

    usage.current(["pi", "claude"], since=time.time() + 30)
    assert not fetched

    e = json.loads((usage.STATE / "usage.json").read_text())
    for v in e["p"].values():
        v["tried"] -= 20
    (usage.STATE / "usage.json").write_text(json.dumps(e))
    usage.current(["pi", "claude"], since=time.time())
    assert sorted(fetched) == ["claude", "pi"]

    fetched.clear()
    usage.current(["claude"], force=True)
    assert fetched == ["claude"]
    assert usage.current(["claude"], refresh_ok=False)[0]["state"] == "ok"

    # Non-list CONFIG["agents"] falls back to defaults instead of splitting characters
    fetched.clear()
    monkeypatch.setitem(usage.CONFIG, "agents", "claude")
    assert [p["id"] for p in usage.current()] == ["claude"]
    assert usage.current([]) == []

    # Corrupt cache entries in usage.json self-heal instead of bricking meters
    fetched.clear()
    (usage.STATE / "usage.json").write_text(json.dumps({"p": {"claude": "bad", "pi": {"next": None, "tried": "x"}}}))
    assert [p["id"] for p in usage.current(["claude", "pi"])] == ["claude", "pi"]
    assert sorted(fetched) == ["claude", "pi"]

    # TOCTOU: another process writing usage.json before we acquire _lock() is seen after reloading inside the lock
    fetched.clear()
    (usage.STATE / "usage.json").unlink(missing_ok=True)
    orig_lock = usage._lock

    @usage.contextlib.contextmanager
    def racing_lock():
        with orig_lock() as mine:
            now = int(time.time())
            (usage.STATE / "usage.json").write_text(
                json.dumps({"p": {"claude": {"id": "claude", "name": "Claude", "state": "ok", "tried": now, "at": now, "windows": [], "next": now + 60}}})
            )
            yield mine

    with mock.patch.object(usage, "_lock", racing_lock):
        assert usage.current(["claude"])[0]["state"] == "ok" and not fetched

    # _save OSError cleans up temp file and current() still returns freshly fetched data
    fetched.clear()
    with mock.patch.object(usage.os, "replace", side_effect=OSError("rofs")):
        assert usage.current(["claude"], force=True)[0]["state"] == "ok"
    assert not list(usage.STATE.glob("usage.json.*"))

    # _lock OSError falls back to cached data rather than crashing
    with mock.patch.object(usage, "_lock", side_effect=OSError("rofs")):
        assert usage.current(["claude"])[0]["state"] == "ok"

    # Off switch and backoff due() check
    monkeypatch.setenv("GREENLIGHT_USAGE", "off")
    assert usage.current(["claude"]) == []
    assert not usage.due({"tried": 100, "next": 400, "wait": 300}, 200, 150)
    assert usage.due({"tried": 100, "next": 160}, 200, 150)
    assert usage.due("corrupt", 200, 150) and usage.due({"next": None}, 200, 150)


def test_dashboard_and_terminal_usage_blocks_stay_identical():
    """dashboard.html and terminal.html share inline CSS and JS for usage meters; guard against drift."""
    dash = (ROOT / "app" / "dashboard.html").read_text()
    term = (ROOT / "app" / "terminal.html").read_text()
    for start, end in (("/* usage-meters:start */", "/* usage-meters:end */"), ("// usage-meters:start", "// usage-meters:end")):
        assert start in dash and end in dash and start in term and end in term
        d_blk = dash.split(start, 1)[1].split(end, 1)[0]
        t_blk = term.split(start, 1)[1].split(end, 1)[0]
        assert d_blk == t_blk, f"usage-meters block drifted between dashboard.html and terminal.html ({start})"
