#!/usr/bin/env python3
"""log_view: the agent log view's extras (All output, app/dashboard.html): what the sessions cost, and a log you can share.

    log_view.py usage PATH        one JSON line: tokens (and an estimated cost) of the agent sessions that ran in PATH
    log_view.py redact < text     one JSON line: {text, removed}: the text with private details replaced by placeholders

Usage reads the sessions' own transcripts: Claude Code's message usage (deduplicated by message id, since a streamed message
repeats it) and Codex's last token_count per rollout. Cost is only for Claude models in PRICES, from list prices per million
tokens, so it is an estimate and is labelled one; Codex reports tokens only, and nothing is invented for it.
Redaction runs audit.py's secret and personal-data patterns, the owner's leak.re terms, this account's name, and paths,
hosts and URLs.
Stdlib only.
"""
from __future__ import annotations

import getpass
import json
import re
import socket
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402
import diag  # noqa: E402

# USD per million tokens: (input, output, cache read). A cache write is billed at 1.25x input (the 5-minute TTL Claude Code uses).
PRICES = (  # the first prefix that matches a model id wins: keep the longer ids first
    ("claude-fable-5-2", (8.0, 40.0, 0.20)),
    ("claude-mythos-5-2", (8.0, 40.0, 0.20)),
    ("claude-fable-5", (10.0, 50.0, 1.00)),
    ("claude-mythos-5", (10.0, 50.0, 1.00)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus-5", (5.0, 25.0, 0.50)),
    ("claude-opus-4", (5.0, 25.0, 0.50)),
    ("claude-sonnet-5-5", (2.0, 10.0, 0.10)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20)),
    ("claude-sonnet-4", (3.0, 15.0, 0.30)),
    ("claude-haiku-5-5", (0.10, 0.50, 0.01)),
    ("claude-haiku-4", (1.0, 5.0, 0.10)),
)
CACHE_WRITE = 1.25
TAIL = 8 << 20  # bytes read from the end of a huge transcript: usage is summed over what fits


def price(model: str) -> tuple[float, float, float] | None:
    m = (model or "").lower()
    return next((p for k, p in PRICES if m.startswith(k)), None)


def _num(x: Any) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 else 0


def _lines(path: Path):
    try:
        with path.open("rb") as fh:
            size = path.stat().st_size
            if size > TAIL:
                fh.seek(size - TAIL)
                fh.readline()  # the cut first line
            for raw in fh:
                if b'"usage"' in raw or b'"token_count"' in raw or b'"turn_context"' in raw:
                    try:
                        yield json.loads(raw)
                    except ValueError:
                        continue
    except OSError:
        return


def claude_usage(path: Path) -> dict[str, Any]:
    """One Claude Code transcript: {in, out, cache_read, cache_write, cost_cents|None, models}."""
    msgs: dict[str, tuple[str, dict]] = {}
    for d in _lines(path):
        m = d.get("message") if isinstance(d, dict) else None
        if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        mid = str(m.get("id") or d.get("uuid") or len(msgs))
        u = m["usage"]
        prev = msgs.get(mid)
        if prev is None or _num(u.get("output_tokens")) >= _num(prev[1].get("output_tokens")):
            msgs[mid] = (str(m.get("model") or ""), u)  # a streamed message repeats its usage: keep the fullest
    out: dict[str, Any] = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost_cents": 0.0, "models": set()}
    priced = True
    for model, u in msgs.values():
        i, o = _num(u.get("input_tokens")), _num(u.get("output_tokens"))
        cr, cw = _num(u.get("cache_read_input_tokens")), _num(u.get("cache_creation_input_tokens"))
        out["in"] += i + cr + cw
        out["out"] += o
        out["cache_read"] += cr
        out["cache_write"] += cw
        if model and not model.startswith("<"):
            out["models"].add(model)
        p = price(model)
        if p is None:
            priced = priced and not (i or o or cr or cw)
            continue
        out["cost_cents"] += (i * p[0] + cw * p[0] * CACHE_WRITE + cr * p[2] + o * p[1]) / 1e6 * 100
    if not priced:
        out["cost_cents"] = None
    return out


def codex_usage(path: Path) -> dict[str, Any]:
    """One Codex rollout: its last token_count's running total. Tokens only: no cost."""
    last, model = None, ""
    for d in _lines(path):
        p = d.get("payload") if isinstance(d, dict) else None
        if not isinstance(p, dict):
            continue
        if d.get("type") == "turn_context" and p.get("model"):
            model = str(p["model"])
        info = p.get("info") if p.get("type") == "token_count" else None
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            last = info["total_token_usage"]
    if not last:
        return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost_cents": None, "models": {model} - {""}}
    return {"in": _num(last.get("input_tokens")), "out": _num(last.get("output_tokens")),  # reasoning is part of output already
            "cache_read": _num(last.get("cached_input_tokens")), "cache_write": 0, "cost_cents": None, "models": {model} - {""}}


def _cache_file() -> Path:
    return diag.home() / "dash" / "usage-cache.json"


def _session_usage(f: Path, kind: str, cache: dict[str, Any]) -> dict[str, Any] | None:
    """One session's usage, from the cache while its transcript's size and mtime haven't changed (the view polls)."""
    if kind not in ("claude", "codex"):
        return None
    try:
        st = f.stat()
    except OSError:
        return None
    sig = [st.st_size, st.st_mtime_ns]
    hit = cache.get(str(f))
    if isinstance(hit, dict) and hit.get("sig") == sig and isinstance(hit.get("u"), dict):
        u = dict(hit["u"])
        u["models"] = set(u.get("models") or [])
        return u
    u = claude_usage(f) if kind == "claude" else codex_usage(f)
    cache[str(f)] = {"sig": sig, "u": {**u, "models": sorted(u["models"])}}
    return u


def _load_cache() -> dict[str, Any]:
    try:
        with _cache_file().open() as fh:
            c = json.load(fh)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, Any], keep: set[str]) -> None:
    f = _cache_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump({k: v for k, v in cache.items() if k in keep}, fh)
        tmp.replace(f)
    except OSError:
        pass


def usage(files: list[tuple[Path, str]]) -> dict[str, Any]:
    """The sessions' totals. cost_cents is None when any session with tokens has no price (Codex, an unknown model):
    a partial sum would read as the whole cost."""
    tot: dict[str, Any] = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "sessions": 0, "cost_cents": 0.0, "models": set()}
    cache = _load_cache()
    for f, kind in files:
        u = _session_usage(f, kind, cache)
        if not u or not (u["in"] or u["out"]):
            continue
        tot["sessions"] += 1
        tot["in_tokens"] += u["in"]
        tot["out_tokens"] += u["out"]
        tot["cache_read"] += u["cache_read"]
        tot["models"] |= u["models"]
        tot["cost_cents"] = None if tot["cost_cents"] is None or u["cost_cents"] is None else tot["cost_cents"] + u["cost_cents"]
    if tot["cost_cents"] is not None:
        tot["cost_cents"] = round(tot["cost_cents"], 2) if tot["sessions"] else None
    tot["models"] = sorted(tot["models"])[:6]
    tot["estimated"] = tot["cost_cents"] is not None
    if files:  # other workspaces' entries stay: the cache is shared
        _save_cache(cache, set(cache) - {str(f) for f, _ in files if not f.exists()})
    return tot


# ---------- sharing: a privacy-safe copy of the log ----------
URL_RE = re.compile(r"\b(?:https?|ssh|git|wss?|file|ftp)://[^\s'\"<>)\]]+", re.I)
SCP_RE = re.compile(r"\b[\w.-]+@[\w.-]+\.\w+:[\w./~-]+")  # git@host:owner/repo.git
USERHOST_RE = re.compile(r"\b[\w.-]+@[A-Za-z][\w-]*(?:\.[\w-]+)*\b")  # alice@buildbox, a shell prompt's user@host
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:local|lan|internal|corp|home|ts\.net)\b", re.I)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PATH_RE = re.compile(r"(?<![\w.])(?:~(?=/)|/(?:home|Users|root|tmp|private|var|opt|srv|mnt|Volumes))(?:/[^\s'\"`:,;)\]]*)?")
# NAME=value / NAME: value where the name says it's a credential, whatever the value looks like
ASSIGN_RE = re.compile(r"(?i)\b([A-Z0-9_.-]*(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIALS?|AUTH)"
                       r"[A-Z0-9_.-]*)(\s*[=:]\s*)(?![\s\[])([\"']?)[^\s\"']+\3")
TOKENISH_RE = re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|xox[abpr]|AKIA)[-_A-Za-z0-9]{12,}\b")


def _user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def _hosts() -> list[str]:
    try:
        full = socket.gethostname()
    except OSError:
        return []
    return sorted({full, full.split(".")[0]} - {"", "localhost"}, key=len, reverse=True)


def redact(text: str) -> tuple[str, int]:
    """(text, how many things were replaced). Order matters: secrets before URLs, URLs before paths and hosts."""
    n = 0

    def sub(rx: re.Pattern[str], repl: str, s: str) -> str:
        nonlocal n
        s, k = rx.subn(repl, s)
        n += k
        return s

    before = text
    text = audit.redact_secrets(text)
    n += sum(1 for rx, _ in audit.SECRET_RES for _ in rx.finditer(before))
    text = sub(TOKENISH_RE, "[token]", text)
    text = sub(ASSIGN_RE, r"\1\2[secret]", text)
    text = sub(URL_RE, "[url]", text)
    text = sub(SCP_RE, "[repo]", text)
    text = sub(audit.EMAIL_RE, "[email]", text)
    text = sub(USERHOST_RE, "[user@host]", text)
    text = sub(PATH_RE, "[path]", text)
    text = sub(audit.HOME_RE, "[path]", text)  # a Windows home, or one PATH_RE's lookbehind left
    text = sub(IP_RE, "[ip]", text)
    text = sub(HOST_RE, "[host]", text)
    text = sub(audit.PHONE_RE, "[phone]", text)
    for name, mark in ((_user(), "[user]"), *((h, "[host]") for h in _hosts())):
        if len(name) >= 3:  # this account's and this machine's names outside a path too: a branch, a prompt, a ssh line
            text = sub(re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.I), mark, text)
    if (rx := diag.leak(strict=True)) is not None:
        text = sub(rx, "[private]", text)
    return text, n


def main() -> None:
    a = sys.argv[1:]
    if a[:1] == ["usage"] and len(a) == 2:
        import dash  # noqa: PLC0415 (heavy: only for the CLI)
        print(json.dumps(usage(dash.transcript_files(a[1]))))
    elif a == ["redact"]:
        text, n = redact(sys.stdin.read(4 << 20))
        print(json.dumps({"text": text, "removed": n}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    diag.install("log_view")
    main()
