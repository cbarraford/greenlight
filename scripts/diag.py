#!/usr/bin/env python3
"""diag: Greenlight's local diagnostic trail, structured so an AI can read it and nothing private gets in.

    diag.py tail [-n N] [--kind K] [--since 1h] [--run ID]   the newest events, JSON lines
    diag.py errors [--since 24h]                            failures grouped by (component, kind, error, class, where)
    diag.py summary [--since 24h]                           version, environment, config shape, span timings, recent automations
    diag.py bundle [--since 7d] [--out F|-]                 a bug-report bundle (summary, errors, recent events), re-checked for leaks
    diag.py report --title T [--body-file F|-] [--execute]  preview the GitHub issue for a bug; --execute files it (gh)

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
import hashlib
import hmac
import json
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

HERE = Path(__file__).resolve().parent
MAX = 5 << 20  # ponytail: size rotation, no compression; enough for a local trail
KEEP = 3
TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")  # no spaces, slashes or @: not a sentence, path or address
LONG = re.compile(r"[A-Za-z0-9]{25,}")  # a segment this long is a token or hash, not an enum
HASH = re.compile(r"[0-9a-f]{10}")
# string-valued keys. An identifier (a workspace, repo, MR or automation) goes under a key ending in _h, from h().
KEYS = {"kind", "comp", "v", "run", "tool", "sub", "err", "msg_class", "op", "name", "gate", "verdict", "provider",
        "harness", "model", "status", "engine", "did", "reason", "cmd", "page", "to", "forge", "ade", "step", "owner",
        "state", "on", "verb", "host_kind", "macos", "phase", "ping_kind", "role"}
CLASSES = (  # first match wins: classify()
    ("rate_limit", r"rate.?limit|\b429\b|too many requests|usage limit|limit reached|out of (?:extra )?usage"),
    ("auth", r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|authenticat|not logged in|log ?in|credential|token|publickey"),
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
_leak = False


def leak():
    """The owner's private-terms regex ($GREENLIGHT_HOME/leak.re, one per line), or None."""
    global _leak
    if _leak is False:
        try:
            lines = [x for x in (home() / "leak.re").read_text().splitlines() if x.strip()]
            _leak = re.compile("|".join(lines), re.I) if lines else None
        except (OSError, re.error):
            _leak = None
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
    try:
        return f.read_bytes()
    except OSError:
        f.parent.mkdir(parents=True, exist_ok=True)
        s = secrets.token_bytes(32)
        try:
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process just wrote it
            return f.read_bytes()
        with os.fdopen(fd, "wb") as w:
            w.write(s)
        return s


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
    out, dropped = {}, 0
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, (bool, int, float)):
            out[k] = round(v, 3) if isinstance(v, float) else v
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
                        if isinstance(e, dict) and e.get("ts", 0) >= after:
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
        if "secs" not in e:
            continue
        label = e["kind"] + "".join(f".{e[k]}" for k in ("tool", "name", "gate") if k in e)
        groups.setdefault(label, []).append(e)
    return {k: {"n": len(v), "p50": pct([e["secs"] for e in v], .5), "p95": pct([e["secs"] for e in v], .95),
                "fail": round(sum(failed(e) for e in v) / len(v), 3)} for k, v in sorted(groups.items())}


def config_shape():
    """config.json's keys, with values only where they're enums, bools or numbers (never names or text)."""
    try:
        c = json.loads((home() / "config.json").read_text())
    except (OSError, ValueError):
        return {}
    shape = {}
    for k, v in c.items():
        if isinstance(v, (bool, int, float)) or v is None:
            shape[k] = v
        elif k in ("merge_method", "llm_harness") and isinstance(v, str) and token(v):
            shape[k] = v
        elif k == "agents" and isinstance(v, list):
            shape[k] = [a for a in v if a in ("claude", "codex", "opencode", "pi", "gemini")]
        else:
            shape[k] = type(v).__name__
    return shape


def automations(n=10):
    out = []
    with contextlib.suppress(OSError):
        for line in (home() / "auto" / "runs.jsonl").read_text().splitlines()[-n:]:
            with contextlib.suppress(ValueError):
                r = json.loads(line)
                out.append(clean({"ts": r.get("ts"), "name_h": h(r.get("name")), "status": r.get("status"),
                                  "engine": r.get("engine"), "secs": r.get("secs")}))
    return out


def summary(after, events=None):
    events = read(after) if events is None else events
    now = time.time()
    return {
        "version": version(),
        "env": {"os": platform.system(), "os_release": platform.release(), "python": platform.python_version(),
                "ade": os.environ.get("GREENLIGHT_ADE") or "orca",
                "clis": {c: bool(shutil.which(c)) for c in ("glab", "gh", "claude", "codex", "opencode", "pi", "gemini", "agy")}},
        "config": config_shape(),
        "events": len(events),
        "errors_1h": sum(failed(e) for e in events if e.get("ts", 0) > now - 3600),
        "errors_24h": sum(failed(e) for e in events if e.get("ts", 0) > now - 86400),
        "top_errors": errors(events)[:10],
        "spans": spans(events),
        "automations": automations(),
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
    res = [*audit.SECRET_RES, *audit.PERSONAL_RES] + ([(leak(), "a private term")] if leak() else [])
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
    b = {"manifest": {"format": 1, "created": round(time.time()), "version": version(), "since": round(after),
                      "events": len(kept), "left_out": left_out},
         "summary": summ, "errors": errors(kept), "events": kept[-BUNDLE_EVENTS:]}
    why = next((w for w in map(hit, strings({"summary": b["summary"], "errors": b["errors"]})) if w), None)
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
        d = {"summary": b["summary"], "errors": b["errors"][:20], "events": ev}
        return head + "<details><summary>Diagnostics</summary>\n\n```json\n" + json.dumps(d, indent=1) + "\n```\n</details>\n"
    ev = b["events"][-200:]
    out = render(ev)
    while len(out) > ISSUE_MAX and ev:  # ponytail: drops events in halves, oldest first; fine at 200
        ev = ev[len(ev) // 2 + 1:]
        out = render(ev)
    return out[:ISSUE_MAX]


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
           "bundle": str(save(b))}
    if inline:
        out["data"] = b
    if not execute:
        return {**out, "preview": True}
    if not shutil.which("gh"):
        import urllib.parse
        q = urllib.parse.urlencode({"title": out["title"], "body": (text or "").strip() + "\n\n(Attach the diagnostics bundle from `diag.py bundle`.)"})
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
            title, text, app = str(j.get("title") or ""), str(j.get("body") or ""), j.get("app_events") or []
            shown = j.get("shown") if isinstance(j.get("shown"), str) else None
        else:
            title, app, shown = a.title, [], None
            text = sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text() if a.body_file else ""
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
