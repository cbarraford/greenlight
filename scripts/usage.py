"""Plan usage per agent provider: the footer's meters in Greenlight Center and the Basement (#193).

  current(agents=None, refresh_ok=True, since=0, force=False) -> [provider], in config `agents` order (installed and signed-in ones only)
  provider: {id, name, state: ok|failed|na, windows: [{label, pct, resets}], plan?, note?, at?, src?}
    pct: 0-100 used; resets: epoch seconds or None; at: when the windows were read (a failed refresh keeps the last ones)
  `usage.py [--force]` prints it as JSON.

Each answer is cached in $GREENLIGHT_HOME/usage.json for a minute (5 when rate-limited); a scan that started after the last
fetch (the footer's ↻) fetches again. Credentials stay where each CLI keeps them: read, never copied, never refreshed.

  claude    GET api.anthropic.com/api/oauth/usage with Claude Code's OAuth token (~/.claude/.credentials.json, or the
            macOS Keychain's "Claude Code-credentials"): 5h, week and per-model weekly pools (Fable, Opus, Sonnet)
  codex     GET chatgpt.com/backend-api/wham/usage with ~/.codex/auth.json; without it, or when that fails, the newest
            session rollout's rate_limits (no network)
  opencode  n/a: OpenCode publishes no usage API (OpenCode Go shows it only in its web console)
  pi        its default provider's plan: anthropic OAuth -> claude's endpoint, openai-codex OAuth -> codex's; else n/a
  others    n/a: no usage source yet
"""
import contextlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import diag
import harness
from greenlight import CONFIG, STATE

TTL, RATE_WAIT, KEYCHAIN_WAIT, TIMEOUT = 60, 300, 6 * 3600, 8
NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode", "pi": "Pi", "agy": "Antigravity", "gemini": "Gemini"}
CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_KEYS = (("five_hour", "5h"), ("seven_day", "Week"), ("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet"))


class SignedOut(Exception):
    """No sign-in at all: the provider is left out of the footer."""


class Unavailable(Exception):
    """Signed in, but nothing reports usage: the row shows n/a with why."""

    def __init__(self, note, plan=None):
        super().__init__(note)
        self.note, self.plan = note, plan


class Failed(Exception):
    """The source answered badly or not at all. code: a diag token; wait: seconds before the next try."""

    def __init__(self, note, code="error", wait=TTL):
        super().__init__(note)
        self.note, self.code, self.wait = note, code, wait


def _json(p):
    try:
        with open(p) as f:
            d = json.load(f)
    except OSError:
        return None
    except ValueError as e:
        raise Failed("unreadable sign-in file", "parse") from e
    return d if isinstance(d, dict) else None


def _get(url, headers):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "greenlight-usage", **headers})  # noqa: S310 - fixed https URLs
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - fixed https URLs
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise Failed("rate limited: trying again in 5 minutes", "rate_limit", RATE_WAIT) from e
        if e.code in (401, 403):
            raise Failed("signed out or the token expired", "auth") from e
        raise Failed(f"the usage API answered HTTP {e.code}", "http") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Failed("can't reach the usage API", "network") from e
    except ValueError as e:
        raise Failed("unreadable answer from the usage API", "parse") from e
    if not isinstance(d, dict):
        raise Failed("unreadable answer from the usage API", "parse")
    return d


def _epoch(v):
    """An ISO time or epoch seconds/ms -> epoch seconds (None when it isn't one)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v / 1000 if v > 1e12 else v) if math.isfinite(v) else None
    if isinstance(v, str) and v:
        with contextlib.suppress(ValueError):
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    return None


def _win(label, pct, resets):
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
        return None
    return {"label": label, "pct": round(max(0.0, min(100.0, float(pct))), 1), "resets": _epoch(resets)}


def _span_label(secs):
    if not isinstance(secs, (int, float)) or secs <= 0:
        return "Limit"
    if abs(secs - 604800) < 3600:
        return "Week"
    return f"{round(secs / 3600)}h" if secs < 86400 * 2 else f"{round(secs / 86400)}d"


def _expired(ms):
    return isinstance(ms, (int, float)) and not isinstance(ms, bool) and _epoch(ms) < time.time()


# claude ---------------------------------------------------------------------------------------------------------------

def claude_windows(d):
    """oauth/usage's answer -> windows: the 5h and weekly ones, then per-model weekly pools (Fable, Opus, Sonnet)."""
    out = [_win(label, v.get("utilization"), v.get("resets_at")) for k, label in CLAUDE_KEYS if isinstance(v := d.get(k), dict)]
    for lim in d.get("limits") if isinstance(d.get("limits"), list) else []:
        model = ((lim.get("scope") or {}).get("model") or {}) if isinstance(lim, dict) and isinstance(lim.get("scope"), dict) else {}
        name = model.get("display_name") if isinstance(model, dict) else None
        if isinstance(name, str) and name and name not in [w["label"] for w in out if w]:
            out.append(_win(name, lim.get("percent"), lim.get("resets_at")))
    return [w for w in out if w]


def _keychain():
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        raise Failed("couldn't read Claude Code's Keychain item", "keychain", KEYCHAIN_WAIT) from e
    if p.returncode:
        return None
    try:
        d = json.loads(p.stdout)
    except ValueError as e:
        raise Failed("unreadable Keychain item", "parse", KEYCHAIN_WAIT) from e
    return d if isinstance(d, dict) else None


def _claude_api(token):
    return claude_windows(_get(CLAUDE_URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}))


def claude():
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    d = _json(root / ".credentials.json") or _keychain() or {}
    o = d.get("claudeAiOauth")
    if not (isinstance(o, dict) and o.get("accessToken")):
        raise SignedOut()
    sub = o.get("subscriptionType")
    plan = {"plan": f"Claude {sub.title()}"} if isinstance(sub, str) and sub else {}
    if _expired(o.get("expiresAt")):
        raise Failed("token expired: Claude Code renews it on its next run", "expired")
    return {"windows": _claude_api(o["accessToken"]), "src": "api", **plan}


# codex ----------------------------------------------------------------------------------------------------------------

def codex_windows(rl, at=None):
    """A rate-limit object -> windows. The API names them primary_window/secondary_window (window in seconds); a rollout's
    rate_limits names them primary/secondary (window_minutes, resets_at or the older resets_in_seconds, counted from `at`,
    the record's time)."""
    out = []
    for k in ("primary_window", "secondary_window", "primary", "secondary"):
        w = rl.get(k)
        if not isinstance(w, dict):
            continue
        secs = w.get("limit_window_seconds") or (w.get("window_minutes") or 0) * 60
        resets = w.get("reset_at") or w.get("resets_at")
        if resets is None and isinstance(w.get("resets_in_seconds"), (int, float)):
            resets = (at or time.time()) + w["resets_in_seconds"]
        out.append(_win(_span_label(secs), w.get("used_percent"), resets))
    return [w for w in out if w]


def _plan(p):
    return {"plan": f"ChatGPT {p.title()}"} if isinstance(p, str) and p else {}


def _codex_api(token, account):
    d = _get(CODEX_URL, {"Authorization": f"Bearer {token}", **({"ChatGPT-Account-Id": account} if account else {})})
    rl = d.get("rate_limit") if isinstance(d.get("rate_limit"), dict) else {}
    return {"windows": codex_windows(rl), "src": "api", **_plan(d.get("plan_type"))}


def _mtime(f):
    try:
        return f.stat().st_mtime
    except OSError:  # gone since the glob
        return 0


def _rollouts(root, n=12):
    """The most recently written session rollouts, newest first. Codex files a session under the day it started, so a long one
    lives in an older folder: the last week's folders, by when each file was last written."""
    days = sorted((root / "sessions").glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9]"), reverse=True)[:7]  # zero-padded: a sort is by date
    return sorted((f for d in days for f in d.glob("rollout-*.jsonl")), key=_mtime, reverse=True)[:n]


def _rate_limits_in(f):
    """The last rate_limits a rollout recorded, with its time: (rate_limits, epoch) or None."""
    with open(f, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - (512 << 10)))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        with contextlib.suppress(ValueError):
            e = json.loads(line)
            p = e.get("payload") if isinstance(e, dict) else None
            rl = p.get("rate_limits") if isinstance(p, dict) else None
            if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")):
                return rl, _epoch(e.get("timestamp")) or int(f.stat().st_mtime)
    return None


def codex_rollout(root):
    for f in _rollouts(root):
        with contextlib.suppress(OSError):
            if hit := _rate_limits_in(f):
                rl, at = hit
                wins = codex_windows(rl, at)
                if not any(w["resets"] and w["resets"] > time.time() for w in wins):
                    return None  # every window it saw has reset since: nothing it says still holds
                return {"windows": wins, "src": "rollout", "at": at, **_plan(rl.get("plan_type"))}
    return None


def codex():
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        tok = ((_json(root / "auth.json") or {}).get("tokens") or {})
    except Failed:  # torn mid-write: the rollouts still say
        tok = {}
    if isinstance(tok, dict) and tok.get("access_token"):
        try:
            return _codex_api(tok["access_token"], tok.get("account_id"))
        except Failed as e:
            if fall := codex_rollout(root):
                return {**fall, "note": f"{e.note}; from the last session instead"}
            raise
    if fall := codex_rollout(root):
        return fall
    raise SignedOut()


# opencode, pi ---------------------------------------------------------------------------------------------------------

def opencode():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    d = _json(data / "opencode" / "auth.json")
    if not d:
        raise SignedOut()
    plan = "OpenCode Go" if "opencode-go" in d else "OpenCode Zen" if "opencode" in d else None
    raise Unavailable("OpenCode publishes no usage API: its web console shows the plan's usage", plan)


def pi():
    root = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    auth = _json(root / "auth.json") or {}
    if not auth:
        raise SignedOut()  # signed in to nothing: left out; signed in to a provider with no plan usage: n/a
    prov = (_json(root / "settings.json") or {}).get("defaultProvider")
    plans = [(p, a) for p in ([prov] if isinstance(prov, str) and prov else ["anthropic", "openai-codex"])
             if isinstance(a := auth.get(p), dict) and a.get("type") == "oauth" and a.get("access")]
    live = [(p, a) for p, a in plans if not _expired(a.get("expires"))]
    if plans and not live:
        raise Failed("token expired: pi renews it on its next run", "expired")
    for p, a in live[:1]:
        if p == "anthropic":
            return {"windows": _claude_api(a["access"]), "src": "api", "plan": "Claude plan"}
        if p == "openai-codex":
            return {**_codex_api(a["access"], a.get("accountId")), "plan": "ChatGPT plan"}
    raise Unavailable(f"pi's provider ({prov}) reports no plan usage" if isinstance(prov, str) and prov
                      else "pi isn't signed in to a Claude or ChatGPT plan")


SOURCES = {"claude": claude, "codex": codex, "opencode": opencode, "pi": pi}


# the cache ------------------------------------------------------------------------------------------------------------

def fetch(name):
    """One provider's usage now: an entry for the cache (state out: leave it out of the footer)."""
    now = int(time.time())
    base = {"id": name, "name": NAMES.get(name, name.title()), "tried": now}
    with diag.span("usage_fetch", provider=name) as sp:
        try:
            fn = SOURCES.get(name)
            if fn is None:
                raise Unavailable("Greenlight has no usage source for this agent yet")
            out = fn()
        except SignedOut:
            sp["state"] = "out"
            return {**base, "state": "out", "next": now + TTL * 5}
        except Unavailable as e:
            sp["state"] = "na"
            return {**base, "state": "na", "note": e.note, **({"plan": e.plan} if e.plan else {}), "next": now + TTL * 5}
        except Failed as e:
            sp.update(state="failed", reason=e.code)
            return {**base, "state": "failed", "note": e.note, "next": now + e.wait, "wait": e.wait}
        except Exception as e:  # an answer shaped like nothing we know: this provider fails, the others keep their meters
            sp.update(state="failed", reason="unexpected", **{k: v for k, v in diag.failure(e).items() if k != "ok"})
            return {**base, "state": "failed", "note": "unexpected answer from the usage source", "next": now + TTL}
        sp["state"] = "ok"
    return {**base, "state": "ok", "at": out.pop("at", now), **out, "next": now + TTL}


def merge(old, new):
    """A failed refresh keeps the last windows it had (shown with their age)."""
    if new["state"] == "failed" and old and old.get("windows"):
        return {**new, **{k: old[k] for k in ("windows", "at", "plan", "src") if k in old}}
    return new


def due(e, since, now):
    if not e:
        return True
    if now >= e.get("next", 0):
        return True
    return since > e.get("tried", 0) and now - e.get("tried", 0) >= 10 and e.get("wait", 0) <= TTL  # a ↻ scan, not in a backoff


@contextlib.contextmanager
def _lock():
    """One process refreshes at a time (the 5s stream and the Basement's trees both ask); the others read the cache."""
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "usage.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def _load():
    try:
        with open(STATE / "usage.json") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(d):
    tmp = STATE / f"usage.json.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STATE / "usage.json")


def view(e):
    return {k: e[k] for k in ("id", "name", "state", "plan", "note", "windows", "at", "src") if k in e}


def refresh(entries, names, since=0, force=False):
    now = int(time.time())
    todo = [a for a in names if force or due(entries.get(a), since, now)]
    if not todo:
        return False
    for a in [a for a in todo if not harness.installed(a)]:
        entries[a] = {"id": a, "state": "out", "tried": now, "next": now + TTL * 5}
        todo.remove(a)
    if todo:
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            for a, new in zip(todo, ex.map(fetch, todo), strict=True):
                entries[a] = merge(entries.get(a), new)
    return True


def current(agents=None, refresh_ok=True, since=0, force=False):
    """The footer's providers, in config `agents` order. GREENLIGHT_USAGE=off (the tests) reads nothing."""
    if os.environ.get("GREENLIGHT_USAGE") == "off":
        return []
    names = list(dict.fromkeys(harness.canonical(a) for a in (agents if agents is not None else CONFIG.get("agents") or ["claude", "codex"])
                               if isinstance(a, str)))
    d = _load()
    entries = d.get("p") if isinstance(d.get("p"), dict) else {}
    if refresh_ok:
        with _lock() as mine:
            if mine and refresh(entries, names, since or 0, force):
                _save({"p": entries})
    return [view(entries[a]) for a in names if isinstance(entries.get(a), dict) and entries[a].get("state") in ("ok", "failed", "na")]


if __name__ == "__main__":
    diag.install("usage")
    print(json.dumps(current(force="--force" in sys.argv[1:]), indent=1))
