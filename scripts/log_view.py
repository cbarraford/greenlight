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
import os
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
TAIL = 512 << 20  # a first read of a transcript past this size starts this far from its end; later reads go on from there


def price(model: str) -> tuple[float, float, float] | None:
    m = (model or "").lower()
    return next((p for k, p in PRICES if m.startswith(k)), None)


def _num(x: Any) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 else 0


def _read(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """The usage-bearing JSON lines from byte `offset` on, and the offset after the last whole line (a line still being
    written is read next time). A first read of a huge file starts TAIL bytes from its end."""
    objs: list[dict[str, Any]] = []
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if offset == 0 and size > TAIL:
                fh.seek(size - TAIL)
                offset = size - TAIL + len(fh.readline())  # the cut first line
            else:
                fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                if b'"usage"' in raw or b'"token_count"' in raw or b'"turn_context"' in raw:
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        objs.append(d)
    except OSError:
        pass
    return objs, offset


def _contrib(model: str, u: dict[str, Any]) -> tuple[int, int, int, int, float | None]:
    i, o = _num(u.get("input_tokens")), _num(u.get("output_tokens"))
    cr, cw = _num(u.get("cache_read_input_tokens")), _num(u.get("cache_creation_input_tokens"))
    p = price(model)
    cost = None if p is None else (i * p[0] + cw * p[0] * CACHE_WRITE + cr * p[2] + o * p[1]) / 1e6 * 100
    return i + cr + cw, o, cr, cw, cost


def _add(tot: dict[str, Any], model: str, u: dict[str, Any], sign: int) -> None:
    tin, tout, cr, cw, cost = _contrib(model, u)
    for k, v in (("in", tin), ("out", tout), ("cache_read", cr), ("cache_write", cw)):
        tot[k] += sign * v
    if cost is None:
        tot["unpriced"] += sign * (1 if tin or tout else 0)
    else:
        tot["cost"] += sign * cost


def claude_scan(path: Path, st: dict[str, Any] | None = None) -> dict[str, Any]:
    """A Claude Code transcript's running totals, carried on from `st` (what an earlier read left). A streamed message
    repeats its usage on consecutive lines: the fullest one counts, once."""
    st = st or {"offset": 0, "tot": {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "unpriced": 0, "models": []},
                "last": None}
    tot, last, models = st["tot"], st["last"], set(st["tot"]["models"])
    objs, st["offset"] = _read(path, st["offset"])
    for d in objs:
        m = d.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        mid, model, u = str(m.get("id") or d.get("uuid") or ""), str(m.get("model") or ""), m["usage"]
        if last and mid and last["id"] == mid:
            if _num(u.get("output_tokens")) < _num(last["usage"].get("output_tokens")):
                continue
            _add(tot, last["model"], last["usage"], -1)
        _add(tot, model, u, 1)
        last = {"id": mid, "model": model, "usage": u}
        if model and not model.startswith("<"):
            models.add(model)
    tot["models"], st["last"] = sorted(models), last
    return st


def _claude_out(st: dict[str, Any]) -> dict[str, Any]:
    t = st["tot"]
    return {"in": t["in"], "out": t["out"], "cache_read": t["cache_read"], "cache_write": t["cache_write"],
            "cost_cents": None if t["unpriced"] else t["cost"], "models": set(t["models"])}


def claude_usage(path: Path) -> dict[str, Any]:
    """One Claude Code transcript: {in, out, cache_read, cache_write, cost_cents|None, models}."""
    return _claude_out(claude_scan(path))


def codex_scan(path: Path, st: dict[str, Any] | None = None) -> dict[str, Any]:
    """A Codex rollout's last token_count running total (and model), carried on from `st`."""
    st = st or {"offset": 0, "last": None, "model": ""}
    objs, st["offset"] = _read(path, st["offset"])
    for d in objs:
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        if d.get("type") == "turn_context" and p.get("model"):
            st["model"] = str(p["model"])
        info = p.get("info") if p.get("type") == "token_count" else None
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            st["last"] = info["total_token_usage"]
    return st


def _codex_out(st: dict[str, Any]) -> dict[str, Any]:
    last, models = st["last"] or {}, {st["model"]} - {""}
    return {"in": _num(last.get("input_tokens")), "out": _num(last.get("output_tokens")),  # reasoning is part of output already
            "cache_read": _num(last.get("cached_input_tokens")), "cache_write": 0, "cost_cents": None, "models": models}


def codex_usage(path: Path) -> dict[str, Any]:
    """One Codex rollout: its last token_count's running total. Tokens only: no cost."""
    return _codex_out(codex_scan(path))


def _cache_file() -> Path:
    return diag.home() / "dash" / "usage-cache.json"


def _session_usage(f: Path, kind: str, cache: dict[str, Any]) -> dict[str, Any] | None:
    """One session's usage. The cache keeps each transcript's totals and how far it was read: a live session's new lines
    are the only ones read on the next poll; a file that shrank (rewritten) is read again from the start."""
    if kind not in ("claude", "codex"):
        return None
    try:
        size = f.stat().st_size
    except OSError:
        return None
    hit = cache.get(str(f))
    st = hit.get("st") if isinstance(hit, dict) and hit.get("kind") == kind and isinstance(hit.get("st"), dict) else None
    if st is None or not isinstance(st.get("offset"), int) or st["offset"] > size:
        st = None
    try:
        st = claude_scan(f, st) if kind == "claude" else codex_scan(f, st)
    except (KeyError, TypeError, AttributeError):  # a cache from another shape: start over
        st = claude_scan(f) if kind == "claude" else codex_scan(f)
    cache[str(f)] = {"kind": kind, "st": st}
    return _claude_out(st) if kind == "claude" else _codex_out(st)


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
URL_RE = re.compile(r"\b(?:https?|ssh|git|wss?|file|ftp)://[^\s'\"<>]+", re.I)
SCP_RE = re.compile(r"\b[\w.-]+@[\w.-]+\.\w+:[\w./~-]+")  # git@host:owner/repo.git
USERHOST_RE = re.compile(r"\b[\w.-]+@[A-Za-z][\w-]*(?:\.[\w-]+)*\b")  # alice@buildbox, a shell prompt's user@host
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:local|lan|internal|corp|home|ts\.net)\b", re.I)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PATH_RE = re.compile(r"(?<![\w.])(?:~(?=/)|/(?:home|Users|root|tmp|private|var|opt|srv|mnt|Volumes))(?:/[^\s'\"`:,;)\]]*)?")
# NAME=value / NAME: value where the name says it's a credential, whatever the value looks like
ASSIGN_RE = re.compile(r"(?i)([\"']?)\b([A-Z0-9_.-]*(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|[_-]KEY|API[_-]?KEY|"
                       r"CREDENTIALS?|AUTH)[A-Z0-9_.-]*)\1(\s*[=:]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|(?![\s\[])[^\n,;}]+)")  # the whole value: quoted, or to the line's end
FLAG_RE = re.compile(r"(?i)(--?[\w-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|auth)[\w-]*)(\s+)(?!-)\S+")  # mysql --password hunter2
BEARER_RE = re.compile(r"(?i)\b(Bearer|Basic|Token)\s+[\w.~+/=-]{8,}")
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


def _url(m: re.Match[str]) -> str:
    """[url], keeping only the punctuation after it that isn't the URL's: a closing bracket it never opened, a full stop."""
    u, tail = m.group(0), ""
    while u and (u[-1] in ".,;:!?" or u[-1] in ")]}" and u.count(u[-1]) > u.count({")": "(", "]": "[", "}": "{"}[u[-1]])):
        u, tail = u[:-1], u[-1] + tail
    return "[url]" + tail


def redact(text: str, terms: tuple[str, ...] = ()) -> tuple[str, int]:
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
    text = sub(BEARER_RE, r"\1 [secret]", text)
    text = sub(FLAG_RE, r"\1\2[secret]", text)
    text, k = URL_RE.subn(_url, text)
    n += k
    text = sub(SCP_RE, "[repo]", text)
    text = sub(audit.EMAIL_RE, "[email]", text)
    text = sub(USERHOST_RE, "[user@host]", text)
    text = sub(PATH_RE, "[path]", text)
    text = sub(audit.HOME_RE, "[path]", text)  # a Windows home, or one PATH_RE's lookbehind left
    text = sub(IP_RE, "[ip]", text)
    text = sub(HOST_RE, "[host]", text)
    text = sub(audit.PHONE_RE, "[phone]", text)
    text = sub(ASSIGN_RE, r"\1\2\1\3[secret]", text)  # after URLs: a query's token= is the URL's
    for term in sorted({t for t in terms if t and len(t) >= 3}, key=len, reverse=True):  # the workspace's own name and path
        text = sub(re.compile(re.escape(term), re.I), "[workspace]", text)
    for name, mark in ((_user(), "[user]"), *((h, "[host]") for h in _hosts())):
        if len(name) >= 3:  # this account's and this machine's names outside a path too: a branch, a prompt, a ssh line
            text = sub(re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.I), mark, text)  # alice-fix, -home-alice-x too
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
