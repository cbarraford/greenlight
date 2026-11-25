#!/usr/bin/env python3
"""diag: Greenlight's local diagnostic trail, structured so an AI can read it and nothing private gets in.

    diag.py tail [-n N] [--kind K] [--since 1h] [--run ID]   the newest events, JSON lines
    diag.py errors [--since 24h]                            failures grouped by (component, kind, error, class, where)
    diag.py summary [--since 24h]                           version, environment, config shape, span timings, recent automations
    diag.py bundle [--since 7d] [--out F|-]                 a bug-report bundle (summary, errors, recent events), re-checked for leaks
    diag.py report --title T [--body-file F|-] [--execute]  preview the GitHub issue for a bug; --execute files it (gh)
    diag.py doctor                                          setup checks: forge auth, agent CLIs, disk, the automations timer
    diag.py doctor --quick                                  the same without network checks (update.py's smoke test)
    diag.py version                                         {version, schema, compat, protocol}: the app checks it matches

Events go to $GREENLIGHT_HOME/diag/events.jsonl, and the app's own events go to ~/Library/Logs/Greenlight/app.jsonl.
Both files rotate at 5 MB and keep three old copies. Nothing is uploaded.

Privacy holds by construction: a field is a number, a bool, or a string under a key in KEYS that matches TOKEN.
Free text (messages, argv, stderr, titles, goals, names) never fits that shape, so it is dropped and counted as
`dropped`. Names that events need to correlate go through h(), a salted hash; the salt never leaves this machine.
Errors are recorded by exception type, script frames and classify()'s class, never by message.
A bundle or report re-checks every string against audit.py's secret and personal-data patterns and leak.re: an event
that hits is left out, and if that check can't run, nothing is exported.
Stdlib only, and no greenlight import, so every script can use it. Writing never raises.
"""
import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MAX = 5 << 20  # ponytail: size rotation, no compression; enough for a local trail
KEEP = 3
TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")  # no spaces, slashes or @: not a sentence, path or address
LONG = re.compile(r"[A-Za-z0-9]{25,}")  # a segment this long is a token or hash, not an enum
HASH = re.compile(r"[0-9a-f]{10}")
# string-valued keys. An identifier (a workspace, repo, MR or automation) goes under a key ending in _h, from h().
KEYS = {"kind", "comp", "v", "run", "tool", "sub", "err", "msg_class", "op", "name", "gate", "verdict", "provider",
        "harness", "model", "status", "engine", "did", "reason", "cmd", "page", "to", "forge", "ade", "step", "owner",
        "state", "on", "verb", "host_kind", "macos", "phase", "ping_kind", "role", "check", "outcome", "draw", "by", "card"}
CLASSES = (  # first match wins: classify()
    ("rate_limit", r"rate.?limit|\b429\b|too many requests|usage limit|limit reached|out of (?:extra )?usage"),
    ("auth", r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|authenticat|not logged in|log ?in|credential|token|publickey|invalid_grant|\boauth"),
    ("permission", r"permission denied|EACCES|operation not permitted|read-only file system"),
    ("timeout", r"timed? ?out|timeout|deadline"),
    ("network", r"could not resolve|name or service not known|connection (?:reset|refused|closed)|network|unreachable|"
                r"TLS|SSL|unexpected EOF|broken pipe"),
    ("transient", r"\b50[0234]\b|bad gateway|service unavailable"),
    ("not_found", r"\b404\b|not found|no such file|does not exist|unknown command"),
    ("conflict", r"\b409\b|conflict|non-fast-forward|rejected|already exists|\b422\b"),
    ("parse", r"json|decode|parse|expecting value|invalid literal"),
)
CLASS_RES = [(c, re.compile(p, re.I)) for c, p in CLASSES]
_comp = None
_ver = None
_leak: Any = False
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}\Z")


def leak(strict=False):
    """The owner's private-terms regex ($GREENLIGHT_HOME/leak.re, one per line), or None.
    When strict=True (used by export gate()), any read/regex error raises so export fails closed."""
    global _leak
    if _leak is False or (strict and _leak is None):
        f = home() / "leak.re"
        try:
            text = f.read_text()
        except FileNotFoundError:
            _leak = None
            return None
        except OSError:
            if strict:
                raise
            return None
        try:
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            rx = re.compile("|".join(f"(?:{x})" for x in lines), re.I) if lines else None
            if rx and rx.search(""):
                raise re.error("leak.re matches empty string")
            _leak = rx
        except re.error:
            if strict:
                raise
            return None
    return _leak


def token(v):
    return bool(TOKEN.fullmatch(v)) and not LONG.search(v) and not (leak() and leak().search(v))


def home():
    return Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")


def path():
    return home() / "diag" / "events.jsonl"


def app_path():
    return Path.home() / "Library" / "Logs" / "Greenlight" / "app.jsonl"


def version():
    global _ver
    if _ver is None:
        root = HERE.parent
        try:
            _ver = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                                  text=True, timeout=3).stdout.strip() if (root / ".git").exists() else ""
        except (OSError, subprocess.SubprocessError):
            _ver = ""
        if not _ver:
            with contextlib.suppress(OSError):
                _ver = (root / "VERSION").read_text().strip()
        _ver = _ver if TOKEN.fullmatch(_ver or "") else "unknown"
    return _ver


def salt():
    f = home() / "diag" / "salt"
    with contextlib.suppress(OSError):
        s = f.read_bytes()
        if len(s) >= 16:
            return s
    # written whole, then linked in: a reader never sees it half-written, and two first runs agree on one salt
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"salt.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as w:
        w.write(secrets.token_bytes(32))
    try:
        os.link(tmp, f)
    except FileExistsError:  # another process won, or an empty one is left from a crash mid-write
        if len(f.read_bytes()) < 16:
            os.replace(tmp, f)
    finally:
        tmp.unlink(missing_ok=True)
    return f.read_bytes()


def h(value):
    """A name as a 10-hex salted hash: the same name matches across events, and the name itself is never written."""
    if value is None or value == "":
        return None
    try:
        d = hmac.new(salt(), str(value).encode(), hashlib.sha256).hexdigest()
        # never all digits: ten of them read as a phone number to the export check, which would leave the events out
        return next((d[i:i + 10] for i in range(len(d) - 9) if not d[i:i + 10].isdigit()), d[:10])
    except OSError:
        return None


def classify(text):
    """An error message as one class word, so the message itself is never stored."""
    text = str(text or "")
    return next((c for c, r in CLASS_RES if r.search(text)), "other")


def where(tb):
    """This codebase's frames in a traceback, innermost last: ["greenlight.py:112:run", ...]. Library frames are dropped."""
    out = []
    for f in traceback.extract_tb(tb):
        p = Path(f.filename)
        if (p.parent == HERE or p.parent == HERE.parent / "tests") and p.name != "diag.py":
            out.append(f"{p.name}:{f.lineno}:{f.name.strip('<>')}")
    return out[-6:]


def clean(fields):
    """Fields that fit the schema; the number dropped goes in `dropped`."""
    out: dict[str, Any] = {}
    dropped = 0
    lrx = leak()
    for k, v in fields.items():
        if v is None:
            continue
        if not isinstance(k, str) or not KEY_RE.fullmatch(k) or (lrx and lrx.search(k)):
            dropped += 1
            continue
        if isinstance(v, int):  # bool is a subclass of int; both are kept as-is
            out[k] = v
        elif isinstance(v, float):
            if math.isfinite(v):
                out[k] = round(v, 3)
            else:
                dropped += 1
        elif isinstance(v, str) and k.endswith("_h") and HASH.fullmatch(v):
            out[k] = v
        elif isinstance(v, str) and k in KEYS and token(v):
            out[k] = v
        elif isinstance(v, list) and k == "where" and all(isinstance(x, str) and token(x) for x in v):
            out[k] = v
        else:
            dropped += 1
    if dropped:
        out["dropped"] = out.get("dropped", 0) + dropped
    return out


def rotate(f):
    lock = f.with_name(".rotate.lock")
    with lock.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        with contextlib.suppress(OSError):
            if not f.exists() or f.stat().st_size <= MAX:
                return
        for i in range(KEEP - 1, 0, -1):
            with contextlib.suppress(OSError):
                os.replace(f"{f}.{i}", f"{f}.{i + 1}")
        with contextlib.suppress(OSError):
            os.replace(f, f"{f}.1")


def event(kind, **fields):
    """Append one event. Never raises."""
    try:
        f = path()
        f.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if f.stat().st_size > MAX:
                rotate(f)
        rec = clean({"ts": round(time.time(), 3), "v": version(), "comp": _comp or Path(sys.argv[0] or "py").stem,
                     "run": os.environ.get("GREENLIGHT_RUN"), "pid": os.getpid(), "kind": kind, **fields})
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)  # one write per line: O_APPEND keeps concurrent writers' lines whole
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - diagnostics must never break the thing they watch
        pass


def failure(e, tb=None):
    """An exception's recordable fields: type, class and frames, never its message."""
    return {"ok": False, "err": type(e).__name__, "msg_class": "timeout" if isinstance(e, subprocess.TimeoutExpired)
            else classify(e), "where": where(tb or e.__traceback__)}


@contextlib.contextmanager
def span(kind, **fields):
    """Time a block: yields a dict the block can add fields to (rc, rows, ...); records secs and ok, or the failure."""
    extra, t = {}, time.monotonic()
    try:
        yield extra
    except BaseException as e:
        if not isinstance(e, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            event(kind, **{**fields, **failure(e), **extra, "secs": time.monotonic() - t})  # what the block set wins
        raise
    event(kind, **{**fields, "ok": True, **extra, "secs": time.monotonic() - t})


def install(comp):
    """Call once from a script's __main__: names the component, starts or joins a run id, records uncaught exceptions."""
    global _comp
    _comp = comp
    os.environ.setdefault("GREENLIGHT_RUN", secrets.token_hex(4))  # children inherit it: one run id across processes
    prev = sys.excepthook

    def hook(t, e, tb):
        if not issubclass(t, KeyboardInterrupt):
            event("uncaught", **failure(e, tb))
        prev(t, e, tb)
    sys.excepthook = hook


# ---- reading

def since(s):
    if not s:
        return 0
    m = re.fullmatch(r"(\d+)([smhd])", s)
    if not m:
        raise SystemExit(f"--since: use like 30m, 2h, 7d (got {s!r})")
    return time.time() - int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def read(after=0):
    """Every event, backend and (when it's on this machine) app, oldest first."""
    out = []
    for base in (path(), app_path()):
        for f in [Path(f"{base}.{i}") for i in range(KEEP, 0, -1)] + [base]:
            with contextlib.suppress(OSError):
                for line in f.read_text(errors="replace").splitlines():
                    with contextlib.suppress(ValueError):
                        e = json.loads(line)
                        if isinstance(e, dict) and isinstance(e.get("ts", 0), (int, float)) and e.get("ts", 0) >= after:
                            out.append(e)
    return sorted(out, key=lambda e: e.get("ts", 0))


def failed(e):
    return e.get("ok") is False or "err" in e or e.get("kind") in ("uncaught", "js_error", "crash", "prev_crash")


def errors(events):
    groups = {}
    for e in filter(failed, events):
        k = (e.get("comp"), e.get("kind"), e.get("err"), e.get("msg_class"), (e.get("where") or [None])[-1])
        g = groups.setdefault(k, {"comp": k[0], "kind": k[1], "err": k[2], "msg_class": k[3], "where": e.get("where"),
                                  "count": 0, "first": e.get("ts"), "last": None, "last_run": None})
        g["count"] += 1
        g["last"], g["last_run"], g["where"] = e.get("ts"), e.get("run"), e.get("where") or g["where"]
    return sorted(groups.values(), key=lambda g: -g["count"])


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)


def spans(events):
    """Timing and failure rate per span kind (cmd split by tool, phase by name, audit by gate)."""
    groups = {}
    for e in events:
        if not isinstance(e.get("secs"), (int, float)) or isinstance(e.get("secs"), bool):
            continue
        label = str(e.get("kind")) + "".join(f".{e[k]}" for k in ("tool", "name", "gate") if k in e)
        groups.setdefault(label, []).append(e)
    return {k: {"n": len(v), "p50": pct([e["secs"] for e in v], .5), "p95": pct([e["secs"] for e in v], .95),
                "fail": round(sum(failed(e) for e in v) / len(v), 3)} for k, v in sorted(groups.items())}


def config_shape():
    """config.json's keys, with values only where they're enums, bools or numbers (never names or text)."""
    try:
        c = json.loads((home() / "config.json").read_text())
    except (OSError, ValueError):
        return {}
    shape: dict[str, Any] = {}
    for k, v in c.items():
        if isinstance(v, (bool, int, float)) or v is None:
            shape[k] = v
        elif k in ("merge_method", "llm_harness") and isinstance(v, str) and token(v):
            shape[k] = v
        elif k == "agents" and isinstance(v, list):
            shape[k] = [a for a in v if a in ("claude", "codex", "opencode", "pi", "gemini", "agy")]
        else:
            shape[k] = type(v).__name__
    return shape


def automations(n=10):
    out = []
    with contextlib.suppress(OSError):
        for line in (home() / "auto" / "runs.jsonl").read_text().splitlines()[-n:]:
            with contextlib.suppress(ValueError):
                r = json.loads(line)
                out.append(clean({"ts": r.get("ts"), "name_h": h(r.get("name")), "status": r.get("status"), "engine": r.get("engine"),
                                  "secs": r.get("secs"), **{k: r.get(k) for k in ("in_tokens", "out_tokens", "cost_cents")}}))
    return out


def crash_dir():
    return Path.home() / "Library" / "Logs" / "DiagnosticReports"


def crashes(after, n=5):
    """The app's macOS crash reports since `after`: exception, signal and the crashed thread's frames as image:symbol.
    Images by name, never path; a frame with a slash in it is dropped, and the export gate checks the rest.
    Only this machine's: with a remote host the bundle is built there, so the Mac's reports aren't in it."""
    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return -1
    out = []
    for f in sorted(crash_dir().glob("[Gg]reenlight*.ips"), key=_mtime)[-n:]:
        try:
            mt = f.stat().st_mtime
            if mt < after:
                continue
            head, body = f.read_text(errors="replace").split("\n", 1)
            hd, b = json.loads(head), json.loads(body)
            if not str(b.get("codeSigningID") or "").startswith("dev.greenlight."):
                continue  # another app whose name starts with Greenlight
            imgs = ["greenlight-dev" if str(i.get("name") or "").lower().startswith("greenlight-") else (i.get("name") or "?")
                    for i in b.get("usedImages", [])]
            th = b.get("threads", [])[b.get("faultingThread", 0)]
            frames = []
            for fr in th.get("frames", [])[:12]:
                i = fr.get("imageIndex")
                img = imgs[i] if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(imgs) else "?"
                x = f"{img}:{fr.get('symbol', hex(fr.get('imageOffset', 0)))}"[:160]
                if "/" not in x:
                    frames.append(x)
            ex = b.get("exception") or {}
            out.append({"ts": round(mt), "v": str(hd.get("build_version") or "")[:12], "exc": ex.get("type"),
                        "signal": ex.get("signal"), "frames": frames})
        except (OSError, ValueError, IndexError, AttributeError, TypeError):
            continue  # ponytail: an unreadable report is skipped, not reported
    return out


def doctor(quick=False):
    """Setup checks an AI can read: each {check, ok, ...}, numbers and enums only.
    quick: no network (skips glab/gh auth), for update.py's smoke test of a new version."""
    out = []

    def add(check, ok, **kw):
        out.append(clean({"check": check, "ok": ok, **kw}))
    for tool in () if quick else ("glab", "gh"):
        if not shutil.which(tool):
            add(f"{tool}_auth", None, state="missing")
            continue
        try:
            p = subprocess.run([tool, "auth", "status"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
            add(f"{tool}_auth", p.returncode == 0, rc=p.returncode, msg_class=classify(p.stderr + p.stdout) if p.returncode else None)
        except (OSError, subprocess.SubprocessError) as e:
            add(f"{tool}_auth", False, **{k: v for k, v in failure(e).items() if k != "ok"})
    try:
        agents = json.loads((home() / "config.json").read_text()).get("agents") or ["claude"]
    except (OSError, ValueError, AttributeError):
        agents = ["claude"]
    for a in agents if isinstance(agents, list) else []:
        if a in ("claude", "codex", "opencode", "pi", "gemini", "agy"):
            add(f"agent_{a}", bool(shutil.which(a)))
    with contextlib.suppress(OSError):
        free = shutil.disk_usage(home() if home().exists() else Path.home()).free / 1e9
        add("disk", free > 1, free_gb=round(free, 1))
    add("home_writable", os.access(home(), os.W_OK) if home().exists() else os.access(home().parent, os.W_OK))
    try:
        autos = json.loads((home() / "automations.json").read_text())
    except (OSError, ValueError):
        autos = {}
    if any(isinstance(a, dict) and a.get("enabled", True) for a in autos.values()):
        tick = home() / "auto" / "last_tick"
        age = time.time() - tick.stat().st_mtime if tick.exists() else None
        add("auto_timer", age is not None and age < 600, state="missing" if age is None else "stale" if age >= 600 else "fresh",
            age_secs=round(age) if age is not None else None)
    dead = 0
    for f in [*(home() / "auto").glob("*.pid"), *home().glob("orchestrator-phase-*.pid")]:
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            t = f.read_text()
            pid = int(json.loads(t).get("phase") if t.lstrip().startswith("{") else t)
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead += 1
            except PermissionError:
                pass
    add("stale_pids", dead == 0, n=dead)
    return {"ok": all(c.get("ok") is not False for c in out), "checks": out}


def usage(events):
    """Chat turns and automation runs per harness.model: counts, tokens in and out, cost (Claude's; the others report none)."""
    out = {}
    for e in events:
        if e.get("kind") in ("chat_turn", "automation") and (e["kind"] == "chat_turn" or e.get("engine") not in (None, "orchestrator")):
            key = f"{e.get('harness') or e.get('engine') or '?'}.{e.get('model') or 'default'}"
            u = out.setdefault(key, {"turns": 0, "runs": 0, "in_tokens": 0, "out_tokens": 0, "cost_usd": 0.0})
            u["turns" if e["kind"] == "chat_turn" else "runs"] += 1
            u["in_tokens"] += e.get("in_tokens") or 0
            u["out_tokens"] += e.get("out_tokens") or 0
            u["cost_usd"] = round(u["cost_usd"] + (e.get("cost_cents") or 0) / 100, 5)
    return out


def stage(events):
    """The voice stage (#208): draws by who asked (chief, fast, dwim, app) and by draw, and per turn the seconds from its start
    to its first card and to its last (a wait card or the turn's held card is neither; a turn with only those isn't timed). `fast_turns` is how many turns the fast model drew in."""
    by: dict[str, int] = {}
    draws: dict[str, int] = {}
    turns: dict[tuple, dict[str, Any]] = {}
    n = 0
    for e in events:
        if e.get("kind") != "stage":
            continue
        n += 1
        by[str(e.get("by"))] = by.get(str(e.get("by")), 0) + 1
        draws[str(e.get("draw"))] = draws.get(str(e.get("draw")), 0) + 1
        at = e.get("at")
        if "turn" not in e or not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        t = turns.setdefault((e.get("run"), e.get("turn")), {"first": None, "last": None, "fast": False})
        if e.get("draw") not in ("wait", "hold"):  # a wait card ("Listening" after a stop) or the held card isn't the answer
            t["first"] = at if t["first"] is None else min(t["first"], at)
            t["last"] = at if t["last"] is None else max(t["last"], at)
        t["fast"] = t["fast"] or e.get("by") == "fast"
    out: dict[str, Any] = {"draws": n, "by": by, "draw": draws, "turns": len(turns),
                           "fast_turns": sum(t["fast"] for t in turns.values())}
    for name, xs in (("first_draw", [t["first"] for t in turns.values() if t["first"] is not None]),
                     ("last_draw", [t["last"] for t in turns.values() if t["last"] is not None])):
        out[name] = {"n": len(xs), "p50": pct(xs, .5), "p95": pct(xs, .95)} if xs else {"n": 0}
    return out


def ade_name():
    """The workspace backend in use (ade.py), or "invalid" when GREENLIGHT_ADE names none."""
    try:
        import ade
        return ade.name()
    except Exception:  # noqa: BLE001 - a bad GREENLIGHT_ADE must not take the summary down
        return "invalid"


def summary(after, events=None):
    events = read(after) if events is None else events
    now = time.time()
    return {
        "version": version(),
        "env": {"os": platform.system(), "os_release": platform.release(), "python": platform.python_version(),
                "ade": ade_name(),
                "clis": {c: bool(shutil.which(c)) for c in ("glab", "gh", "claude", "codex", "opencode", "pi", "gemini", "agy")}},
        "config": config_shape(),
        "events": len(events),
        "errors_1h": sum(failed(e) for e in events if e.get("ts", 0) > now - 3600),
        "errors_24h": sum(failed(e) for e in events if e.get("ts", 0) > now - 86400),
        "top_errors": errors(events)[:10],
        "spans": spans(events),
        "usage": usage(events),
        "stage": stage(events),
        "automations": automations(),
        "crashes": crashes(after),
    }


# ---- export: a bug-report bundle and the issue it files. Only ever on the user's say-so.

REPO = "cbarraford/greenlight"
ISSUE_MAX = 60000  # GitHub's issue body limit is 65536
BUNDLE_EVENTS = 500


def strings(o):
    """Every string in a JSON value, dict keys included."""
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for k, v in o.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


def gate():
    """The export check: text -> the kind of thing it leaks, or None. Raises when it can't be built, so nothing exports."""
    import audit  # stdlib-only at import; late so writing events never needs it
    lrx = leak(strict=True)
    res = [*audit.SECRET_RES, *audit.PERSONAL_RES] + ([(lrx, "a private term")] if lrx else [])
    return lambda text: next((why for rx, why in res if rx.search(text)), None)


def bundle(after, app_events=(), hit=None):
    """Summary, errors and the newest events, every event re-cleaned and checked: one that hits the gate is left out."""
    hit = hit or gate()
    seen, kept, left_out = set(), [], 0
    for e in sorted([*read(after), *(x for x in app_events if isinstance(x, dict))], key=lambda e: e.get("ts", 0)):
        key = json.dumps(e, sort_keys=True)
        if key in seen:  # the app's events, when it shares this machine, arrive both ways
            continue
        seen.add(key)
        c = clean(e)  # written by an older version, or by the app: the schema is checked again
        if any(hit(x) for x in strings(c)):
            left_out += 1
        else:
            kept.append(c)
    summ = summary(after, kept)  # over the checked events only, the app's from a remote host included
    summ.pop("top_errors", None)  # the same as errors below
    summ["crashes"] = [c for c in summ["crashes"] if not any(hit(x) for x in strings(c))]
    b = {"manifest": {"format": 1, "created": round(time.time()), "version": version(), "since": round(after),
                      "events": len(kept), "left_out": left_out},
         "summary": summ, "doctor": doctor(), "errors": errors(kept), "events": kept[-BUNDLE_EVENTS:]}
    why = next((w for w in map(hit, strings({k: b[k] for k in ("summary", "doctor", "errors")})) if w), None)
    if why:
        raise SystemExit(f"diag: the summary would export {why}; nothing written")
    return b


def save(b, f=None):
    f = Path(f) if f else home() / "diag" / f"bundle-{time.strftime('%Y%m%d-%H%M%S')}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as w:
        json.dump(b, w, indent=1)
    return f


def issue_body(text, b):
    """The user's description, then the bundle's summary, errors and as many of the newest events as fit."""
    head = (text.strip() or "_(no description)_") + f"\n\n---\nGreenlight `{b['manifest']['version']}`, diagnostics from `diag.py report`" \
        f" ({b['manifest']['events']} events, {b['manifest']['left_out']} left out by the privacy check).\n\n"

    def render(ev):
        d = {"summary": b["summary"], "doctor": b.get("doctor"), "errors": b["errors"][:20], "events": ev}
        return head + "<details><summary>Diagnostics</summary>\n\n```json\n" + json.dumps(d, indent=1) + "\n```\n</details>\n"
    ev = b["events"][-200:]
    out = render(ev)
    while len(out) > ISSUE_MAX and ev:  # ponytail: drops events in halves, oldest first; fine at 200
        ev = ev[len(ev) // 2 + 1:]
        out = render(ev)
    if len(out) > ISSUE_MAX:  # still too long without events: cut the JSON, and keep the fence and details closed
        tail = "\n…(cut: the full data is in `diag.py bundle`)\n```\n</details>\n"
        out = out[:ISSUE_MAX - len(tail)] + tail
    return out


def report(title, text, app_events=(), execute=False, inline=False, shown=None):
    """Preview (default) or file the bug as an issue on REPO. The title and description are checked like the events.
    `shown`: the body a preview returned, filed as it was seen (checked again) instead of rebuilt with newer events."""
    hit = gate()
    for what, t in (("title", title), ("description", text), ("report", shown)):
        why = hit(t or "")
        if why:
            return {"ok": False, "blocked": what, "reason": f"the {what} contains {why}: take it out and try again"}
    if not (title or "").strip():
        return {"ok": False, "blocked": "title", "reason": "a bug report needs a title"}
    b = bundle(since("7d"), app_events, hit)
    out = {"ok": True, "repo": REPO, "title": title.strip(), "body": shown[:ISSUE_MAX] if execute and shown else issue_body(text or "", b),
           "bundle": str(save(b, home() / "diag" / "bundle-report.json"))}  # one file, overwritten: previews don't pile up
    if inline:
        out["data"] = b
    if not execute:
        return {**out, "preview": True}
    if not shutil.which("gh"):
        import urllib.parse
        q = urllib.parse.urlencode({"title": out["title"], "body": (text or "").strip()[:4000] + "\n\n(Attach the diagnostics bundle from `diag.py bundle`.)"})
        return {**out, "filed": False, "url": f"https://github.com/{REPO}/issues/new?{q}"}
    with span("report", tool="gh") as d:
        p = subprocess.run(["gh", "issue", "create", "--repo", REPO, "--title", out["title"], "--body-file", "-"],
                           input=out["body"], capture_output=True, text=True, timeout=60)
        d.update(rc=p.returncode, msg_class=classify(p.stderr) if p.returncode else None)
    if p.returncode:
        return {**out, "ok": False, "filed": False, "reason": (p.stderr.strip() or "gh issue create failed")[-500:]}
    return {**out, "filed": True, "url": p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="diag.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tail")
    t.add_argument("-n", type=int, default=50)
    t.add_argument("--kind")
    t.add_argument("--since")
    t.add_argument("--run")
    for name in ("errors", "summary"):
        sub.add_parser(name).add_argument("--since", default="24h")
    b = sub.add_parser("bundle")
    b.add_argument("--since", default="7d")
    b.add_argument("--out", help="a file, or - for stdout (default: a new file in $GREENLIGHT_HOME/diag)")
    sub.add_parser("doctor").add_argument("--quick", action="store_true", help="no network checks")
    sub.add_parser("version")
    r = sub.add_parser("report")
    r.add_argument("--title", default="")
    r.add_argument("--body-file", help="the description: a file, or - for stdin")
    r.add_argument("--json", action="store_true", help='stdin is {"title", "body", "app_events", "shown"} (the app); the result includes the bundle')
    r.add_argument("--execute", action="store_true", help="file it; without this, only a preview")
    a = ap.parse_args(argv)
    if a.cmd == "tail":
        ev = [e for e in read(since(a.since)) if (not a.kind or e.get("kind") == a.kind) and (not a.run or e.get("run") == a.run)]
        for e in ev[-a.n:]:
            print(json.dumps(e))
    elif a.cmd == "errors":
        print(json.dumps(errors(read(since(a.since))), indent=1))
    elif a.cmd == "version":
        import update  # lazy: update imports diag
        print(json.dumps(update.version_info()))
    elif a.cmd == "doctor":
        d = doctor(a.quick)
        print(json.dumps(d, indent=1))
        sys.exit(0 if d["ok"] else 1)
    elif a.cmd == "bundle":
        bd = bundle(since(a.since))
        if a.out == "-":
            print(json.dumps(bd, indent=1))
        elif a.out:
            print(save(bd, a.out))
        else:
            print(save(bd))
    elif a.cmd == "report":
        if a.json:
            j = json.loads(sys.stdin.read() or "{}")
            j = j if isinstance(j, dict) else {}
            title, text, app = str(j.get("title") or ""), str(j.get("body") or ""), j.get("app_events") or []
            shown = j.get("shown") if isinstance(j.get("shown"), str) else None
        else:
            title, app, shown = a.title, [], None
            try:
                text = sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text() if a.body_file else ""
            except OSError as e:
                print(json.dumps({"ok": False, "reason": f"can't read the description: {e.strerror or type(e).__name__}"}))
                sys.exit(1)
        try:
            res = report(title, text, app if isinstance(app, list) else [], a.execute, inline=a.json, shown=shown)
        except ImportError:  # failed closed: say why, as JSON, so the app can show it
            res = {"ok": False, "reason": "the privacy check (audit.py) couldn't load, so nothing was exported"}
        except SystemExit as e:
            res = {"ok": False, "reason": str(e.code)}
        print(json.dumps(res, indent=1))
        sys.exit(0 if res["ok"] else 1)
    else:
        print(json.dumps(summary(since(a.since)), indent=1))


if __name__ == "__main__":
    install("diag")
    main()
