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
  agy       POST daily-cloudcode-pa.googleapis.com/v1internal (the host agy itself calls; prod answers an unused plan) with
            ~/.gemini/antigravity-cli/antigravity-oauth-token: retrieveUserQuotaSummary's grouped 5h and weekly windows
            (Claude and GPT models), retrieveUserQuota's per-model pools for the rest (Gemini), loadCodeAssist's tier as the plan
  gemini    the same methods on cloudcode-pa.googleapis.com with Gemini CLI's ~/.gemini/oauth_creds.json: one window per
            model pool; an API-key or Vertex sign-in is n/a (billed, no plan quota)
  others    n/a: no usage source yet
Google's API answers only the client a token belongs to, by User-Agent: each request names agy or Gemini CLI.
"""
import contextlib
import http.client
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

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


def _num(v: object) -> TypeGuard[int | float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _json(p):
    try:
        with open(p) as f:
            d = json.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise Failed("couldn't read sign-in file", "permission") from e
    except ValueError as e:
        raise Failed("unreadable sign-in file", "parse") from e
    return d if isinstance(d, dict) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward OAuth Bearer tokens across HTTP 3xx redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _get(url, headers, body=None):
    """GET url, or POST body as JSON when it's given (Google's v1internal methods): its JSON object, or Failed."""
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Accept": "application/json", "User-Agent": "greenlight-usage", **({"Content-Type": "application/json"} if data else {}), **headers}
    req = urllib.request.Request(url, data=data, headers=hdrs)  # noqa: S310 - fixed https URLs
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise Failed("rate limited: trying again in 5 minutes", "rate_limit", RATE_WAIT) from e
        if e.code in (401, 403):
            raise Failed("signed out or the token expired", "auth") from e
        raise Failed(f"the usage API answered HTTP {e.code}", "http") from e
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
        raise Failed("can't reach the usage API", "network") from e
    except ValueError as e:
        raise Failed("unreadable answer from the usage API", "parse") from e
    if not isinstance(d, dict):
        raise Failed("unreadable answer from the usage API", "parse")
    return d


def _epoch(v):
    """An ISO time or epoch seconds/ms -> epoch seconds (None when it isn't one)."""
    if _num(v):
        return int(v / 1000 if v > 1e12 else v)
    if isinstance(v, str) and v:
        with contextlib.suppress(ValueError):
            s = v[:-1] + "+00:00" if v.endswith(("Z", "z")) else v
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return int(dt.timestamp())
    return None


def _win(label, pct, resets):
    if not _num(pct):
        return None
    return {"label": label, "pct": round(max(0.0, min(100.0, float(pct))), 1), "resets": _epoch(resets)}


def _span_label(secs):
    if not _num(secs) or secs <= 0:
        return "Limit"
    if secs < 3600:
        return f"{max(1, round(secs / 60))}m"
    if abs(secs - 604800) < 3600:
        return "Week"
    return f"{round(secs / 3600)}h" if secs < 86400 * 2 else f"{round(secs / 86400)}d"


def _expired(ms):
    t = _epoch(ms)
    return t is not None and t > 0 and t < time.time()


# claude ---------------------------------------------------------------------------------------------------------------

def claude_windows(d):
    """oauth/usage's answer -> windows: the 5h and weekly ones, then per-model weekly pools (Fable, Opus, Sonnet)."""
    out = [w for k, label in CLAUDE_KEYS
           if isinstance(v := d.get(k), dict) and (w := _win(label, v.get("utilization"), v.get("resets_at")))]
    seen = {w["label"] for w in out}
    for lim in d.get("limits") if isinstance(d.get("limits"), list) else []:
        if not isinstance(lim, dict) or not isinstance(scope := lim.get("scope"), dict):
            continue
        model = scope.get("model")
        name = model.get("display_name") if isinstance(model, dict) else None
        if isinstance(name, str) and name and name not in seen and (w := _win(name, lim.get("percent"), lim.get("resets_at"))):
            seen.add(name)
            out.append(w)
    return out


def _keychain():
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        raise Failed("couldn't read Claude Code's Keychain item", "keychain", KEYCHAIN_WAIT) from e
    if p.returncode == 44:  # errSecItemNotFound: no Keychain item
        return None
    if p.returncode:  # Deny (128), locked/non-interactive (36), auth failed (51): back off 6h so it can't nag
        raise Failed("couldn't read Claude Code's Keychain item", "keychain", KEYCHAIN_WAIT)
    try:
        d = json.loads(p.stdout)
    except ValueError as e:
        raise Failed("unreadable Keychain item", "parse", KEYCHAIN_WAIT) from e
    return d if isinstance(d, dict) else None


def _claude_api(token):
    return claude_windows(_get(CLAUDE_URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}))


def claude():
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    o = (_json(root / ".credentials.json") or {}).get("claudeAiOauth")
    if not (isinstance(o, dict) and o.get("accessToken")):
        o = (_keychain() or {}).get("claudeAiOauth")
    if not (isinstance(o, dict) and o.get("accessToken")):
        raise SignedOut()
    if _expired(o.get("expiresAt")):
        raise Failed("token expired: Claude Code renews it on its next run", "expired")
    sub = o.get("subscriptionType")
    plan = {"plan": f"Claude {sub.title()}"} if isinstance(sub, str) and sub else {}
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
        wm = w.get("window_minutes")
        secs = w.get("limit_window_seconds") or w.get("window_seconds") or (wm * 60 if _num(wm) else 0)
        resets = w.get("reset_at") or w.get("resets_at")
        rin = w.get("resets_in_seconds")
        if rin is None:
            rin = w.get("reset_after_seconds")
        if resets is None and _num(rin):
            resets = (at or time.time()) + rin
        if win := _win(_span_label(secs), w.get("used_percent"), resets):
            out.append(win)
    return out


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
    """The last valid rate_limits a rollout recorded, with its windows and time: (rate_limits, windows, epoch) or None."""
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
            if isinstance(rl, dict):
                at = _epoch(e.get("timestamp")) or int(f.stat().st_mtime)
                if wins := codex_windows(rl, at):
                    return rl, wins, at
    return None


def codex_rollout(root):
    for f in _rollouts(root):
        with contextlib.suppress(OSError):
            if hit := _rate_limits_in(f):
                rl, wins, at = hit
                if not any(not w["resets"] or w["resets"] > time.time() for w in wins):  # no reset time: unknown, not reset
                    continue  # every window it saw has reset since: keep looking in other recent rollouts
                return {"windows": wins, "src": "rollout", "at": at, **_plan(rl.get("plan_type"))}
    return None


def codex():
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        tok = (_json(root / "auth.json") or {}).get("tokens")
    except Failed as e:  # torn mid-write or unreadable: the rollouts still say; without them it's a failed read, not a sign-out
        if fall := codex_rollout(root):
            return {**fall, "note": f"{e.note}; from the last session instead", "_fail": e.note}
        raise
    if isinstance(tok, dict) and tok.get("access_token"):
        try:
            return _codex_api(tok["access_token"], tok.get("account_id"))
        except Failed as e:
            if fall := codex_rollout(root):
                return {**fall, "note": f"{e.note}; from the last session instead", "_fail": e.note,
                        **({"wait": e.wait} if e.wait > TTL else {})}
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
    raw_prov = (_json(root / "settings.json") or {}).get("defaultProvider")
    prov = raw_prov if isinstance(raw_prov, str) and raw_prov else None
    candidates = [prov] if prov else ["anthropic", "openai-codex"]
    plans = [(p, a) for p in candidates
             if p in ("anthropic", "openai-codex") and isinstance(a := auth.get(p), dict) and a.get("type") == "oauth" and a.get("access")]
    live = [(p, a) for p, a in plans if not _expired(a.get("expires"))]
    if plans and not live:
        raise Failed("token expired: pi renews it on its next run", "expired")
    if live:
        p, a = live[0]
        if p == "anthropic":
            return {"windows": _claude_api(a["access"]), "src": "api", "plan": "Claude plan"}
        return {"plan": "ChatGPT plan", **_codex_api(a["access"], a.get("accountId"))}
    raise Unavailable(f"pi's provider ({prov}) reports no plan usage" if prov
                      else "pi isn't signed in to a Claude or ChatGPT plan")


# agy, gemini (Google) -------------------------------------------------------------------------------------------------

AGY_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:"
GEMINI_URL = "https://cloudcode-pa.googleapis.com/v1internal:"
GOOGLE_SPANS = {"5h": "5h", "weekly": "Week", "daily": "Day"}
GOOGLE_META = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
OTHERS = ("Claude", "GPT")  # model families that aren't Gemini


def _family(model):
    m = model.lower()
    for k, name in (("claude", "Claude"), ("gpt", "GPT"), ("flash-lite", "Lite"), ("flash", "Flash"), ("pro", "Pro")):
        if k in m:
            return name
    return "Gemini"


def _pool_label(fams):
    if len(fams) == 1:
        return next(iter(fams))
    if not fams & set(OTHERS):
        return "Gemini"
    return "Claude" if fams <= set(OTHERS) else "Models"


def google_pools(buckets, covered=frozenset()):
    """retrieveUserQuota's per-model buckets -> one window per pool (the models sharing a quota report the same fraction and
    reset), leaving out the families in `covered` (a summary group already shows them) and the editor's tab models."""
    pools: dict[tuple[float, object], dict[str, set[str]]] = {}
    for b in buckets if isinstance(buckets, list) else []:
        mid = b.get("modelId") if isinstance(b, dict) else None
        if not isinstance(mid, str) or mid.startswith("tab_") or mid.endswith("_vertex") or not _num(f := b.get("remainingFraction")):
            continue
        fam = _family(mid)
        if fam in covered or (fam not in OTHERS and "Gemini" in covered):
            continue
        pool = pools.setdefault((round(float(f), 4), b.get("resetTime")), {"fams": set(), "ids": set()})
        pool["fams"].add(fam)
        pool["ids"].add(mid)
    out = [(w, p) for (f, reset), p in pools.items() if (w := _win(_pool_label(p["fams"]), (1 - f) * 100, reset))]
    labels = [w["label"] for w, _ in out]
    for w, p in out:  # two pools of one family (Gemini CLI's per-model quotas): name each by its model
        if labels.count(w["label"]) > 1:
            w["label"] = min(p["ids"], key=len).removeprefix("gemini-")[:10]
    return sorted((w for w, _ in out), key=lambda w: w["label"])


def _summary_wins(g, prefix):
    order = list(GOOGLE_SPANS)
    bs = [(str(b.get("window")), b) for b in g.get("buckets") or [] if isinstance(b, dict) and _num(b.get("remainingFraction"))]
    bs.sort(key=lambda kb: order.index(kb[0]) if kb[0] in order else len(order))
    return [w for k, b in bs if (w := _win(prefix + GOOGLE_SPANS.get(k, "Limit"), (1 - b["remainingFraction"]) * 100, b.get("resetTime")))]


def google_summary(d):
    """retrieveUserQuotaSummary's groups -> (windows, the model families they cover, a note naming what they count).
    One group's windows are 5h and Week; with several, each is prefixed with its group's first word."""
    groups = [g for g in d.get("groups") if isinstance(g, dict)] if isinstance(d.get("groups"), list) else []
    wins, covered, names = [], set(), []
    for g in groups:
        name = str(g.get("displayName") or "")
        if got := _summary_wins(g, f"{name.split()[0]} " if len(groups) > 1 and name.strip() else ""):
            wins += got
            names.append(name)
            text = f"{name} {g.get('description') or ''}".lower()
            covered |= {fam for fam in (*OTHERS, "Gemini") if fam.lower() in text}
    note = f"{' and '.join(w['label'] for w in wins)}: {names[0]}" if len(names) == 1 and names[0] else None
    return wins, covered, note


def _google_plan(lca):
    tier = next((t for k in ("paidTier", "currentTier") if isinstance(t := lca.get(k), dict) and isinstance(t.get("name"), str) and t["name"]), None)
    return {"plan": tier["name"]} if tier else {}


def _google_try(fn):
    try:
        return fn(), None
    except Failed as e:
        return None, e


def agy():
    t = (_json(Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token") or {}).get("token")
    if not (isinstance(t, dict) and isinstance(t.get("access_token"), str) and t["access_token"]):
        raise SignedOut()
    if _expired(t.get("expiry")):
        raise Failed("token expired: agy renews it on its next run", "expired")
    hdr = {"Authorization": f"Bearer {t['access_token']}", "User-Agent": "antigravity (greenlight-usage)"}
    summary, e1 = _google_try(lambda: google_summary(_get(AGY_URL + "retrieveUserQuotaSummary", hdr, {})))
    quota, e2 = _google_try(lambda: _get(AGY_URL + "retrieveUserQuota", hdr, {}))
    if summary is None and quota is None:
        raise e1 or e2 or Failed("no answer from the usage API")
    wins, covered, note = summary or ([], frozenset(), None)
    pools = google_pools(quota.get("buckets"), covered) if quota else []
    lca, _ = _google_try(lambda: _get(AGY_URL + "loadCodeAssist", hdr, {"metadata": {**GOOGLE_META, "ideType": "ANTIGRAVITY"}}))
    note = note if pools else None  # the note says which models the 5h and weekly windows count when other pools sit beside them
    return {"windows": wins + pools, "src": "api", **_google_plan(lca or {}), **({"note": note} if note else {})}


def _gemini_auth(root):
    sec = (_json(root / "settings.json") or {}).get("security")
    auth = sec.get("auth") if isinstance(sec, dict) else None
    return auth.get("selectedType") if isinstance(auth, dict) else None


def gemini():
    root = Path.home() / ".gemini"
    if _gemini_auth(root) in ("gemini-api-key", "vertex-ai"):
        raise Unavailable("Gemini CLI signs in with an API key: that's billed per use, with no plan quota")
    c = _json(root / "oauth_creds.json") or {}
    if not (isinstance(c.get("access_token"), str) and c["access_token"]):
        raise SignedOut()
    if _expired(c.get("expiry_date")):
        raise Failed("token expired: Gemini CLI renews it on its next run", "expired")
    hdr = {"Authorization": f"Bearer {c['access_token']}", "User-Agent": f"GeminiCLI/greenlight-usage ({sys.platform})"}
    env_proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
    lca = _get(GEMINI_URL + "loadCodeAssist", hdr, {"cloudaicompanionProject": env_proj, "metadata": {**GOOGLE_META, "duetProject": env_proj}})
    proj = lca.get("cloudaicompanionProject")
    proj = proj.get("id") if isinstance(proj, dict) else proj
    q = _get(GEMINI_URL + "retrieveUserQuota", hdr, {"project": proj} if isinstance(proj, str) and proj else {})
    return {"windows": google_pools(q.get("buckets")), "src": "api", **_google_plan(lca)}


SOURCES = {"claude": claude, "codex": codex, "opencode": opencode, "pi": pi, "agy": agy, "gemini": gemini}


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
            sp.update(state="failed", reason=e.code, ok=False, err="Failed")  # a failed read, counted by `diag.py errors`
            return {**base, "state": "failed", "note": e.note, "next": now + e.wait, "wait": e.wait}
        except Exception as e:  # an answer shaped like nothing we know: this provider fails, the others keep their meters
            sp.update(state="failed", reason="unexpected", **diag.failure(e))
            return {**base, "state": "failed", "note": "unexpected answer from the usage source", "next": now + TTL}
        sp["state"] = "ok"
    wait = out.pop("wait", TTL)
    return {**base, "state": "ok", "at": out.pop("at", now), **out, "next": now + wait, **({"wait": wait} if wait > TTL else {})}


def merge(old, new):
    """A failed refresh keeps the last windows it had (shown with their age). An older rollout fallback never clobbers a fresher reading."""
    fail_note = new.get("_fail")
    clean = {k: v for k, v in new.items() if k != "_fail"}
    if not (isinstance(old, dict) and old.get("windows")):
        return clean
    if clean["state"] == "failed":
        return {**clean, **{k: old[k] for k in ("windows", "at", "plan", "src") if k in old}}
    if clean.get("src") == "rollout" and _num(old.get("at")) and _num(clean.get("at")) and old["at"] > clean["at"]:
        kept = {**clean, **{k: old[k] for k in ("windows", "at", "plan", "src") if k in old}}
        if fail_note:
            kept.update(state="failed", note=fail_note)
        return kept
    return clean


def due(e, since, now):
    if not isinstance(e, dict):
        return True
    nxt, tried, wait = e.get("next", 0), e.get("tried", 0), e.get("wait", 0)
    if not (_num(nxt) and _num(tried) and _num(wait)):
        return True
    if now >= nxt:
        return True
    return since > tried and now - tried >= 10 and wait <= TTL  # a ↻ scan, not in a backoff


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
    p = d.get("p") if isinstance(d, dict) else None
    return p if isinstance(p, dict) else {}


def _save(d):
    tmp = STATE / f"usage.json.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATE / "usage.json")
    finally:
        tmp.unlink(missing_ok=True)


def view(e):
    return {k: e[k] for k in ("id", "name", "state", "plan", "note", "windows", "at", "src") if k in e}


def refresh(entries, names, since=0, force=False):
    now = int(time.time())
    todo = [a for a in names if force or due(entries.get(a), since, now)]
    if not todo:
        return False
    live = []
    for a in todo:
        if harness.installed(a):
            live.append(a)
        else:
            entries[a] = {"id": a, "state": "out", "tried": now, "next": now + TTL * 5}
    if live:
        with ThreadPoolExecutor(max_workers=len(live)) as ex:
            for a, new in zip(live, ex.map(fetch, live), strict=True):
                entries[a] = merge(entries.get(a), new)
    return True


def current(agents=None, refresh_ok=True, since=0, force=False):
    """The footer's providers, in config `agents` order. GREENLIGHT_USAGE=off (the tests) reads nothing."""
    if os.environ.get("GREENLIGHT_USAGE") == "off":
        return []
    if agents is None:
        cfg = CONFIG.get("agents")
        raw = cfg if isinstance(cfg, (list, tuple)) and cfg else ["claude", "codex"]
    else:
        raw = agents if isinstance(agents, (list, tuple)) else ["claude", "codex"]
    names = list(dict.fromkeys(c for a in raw if isinstance(a, str) and (c := harness.canonical(a).strip())))
    entries = None
    if refresh_ok:
        try:
            with _lock() as mine:
                if mine:
                    entries = _load()
                    if refresh(entries, names, since or 0, force):
                        with contextlib.suppress(OSError):
                            _save({"p": entries})
        except OSError:
            pass
    if entries is None:
        entries = _load()
    return [view(entries[a]) for a in names if isinstance(entries.get(a), dict) and entries[a].get("state") in ("ok", "failed", "na")]


if __name__ == "__main__":
    diag.install("usage")
    print(json.dumps(current(force="--force" in sys.argv[1:]), indent=1))
