#!/usr/bin/env python3
"""Confidence-gated autonomy: how sure greenlight is of the answer to a question, from what you answered before,
and what it does about it. Also the anti-slop gate on the questions nobody asked for (idle check-ins, the watch).

A question of a known kind (KINDS) gets a proposal before it's pinged:

    P = how likely you'd pick its top option: past answers to the same kind (decisions.jsonl) plus the
        system-one model's pick (decide.choice, given those answers and memory/MEMORY.md as context),
        capped by how often proposals like it turned out right (calibrate)
    Tier 1  P >= 0.95, the option is inert, and you opted the kind in (Settings → Autonomy):
            greenlight answers it itself, logs it in autonomy.jsonl, and the app can ask you again
    Tier 2  0.65 <= P < 0.95 (or 0.95+ but not opted in): asked, with that option preselected and a badge
            ("you chose it 6 of 7 times")
    Tier 3  P < 0.65, or the option is high risk (a merge you held, a sign-off, a deploy): asked as before

A sign-off, a held merge's "merge" and the like are never answered for you or preselected, and the signoff and merge-held
kinds can't be opted in at all (not even their "not yet"): the lifecycle's human gates stay human.

    autonomy.py propose --kind K [--key K] <text ending in an Options line>   # the proposal, JSON (nothing saved)
    autonomy.py calibrate [--json]      # per kind: how often proposals were right, by tier; graduation candidates
    autonomy.py gate --kind K [<text>]  # may a proactive question go out now? JSON {ok, score, ...}; exit 1 when held
    autonomy.py settings [--json]       # opted-in kinds, gate threshold, calibration, candidates, recent auto acts
    autonomy.py set [--auto K on|off]... [--gate 0.35]
    autonomy.py undo <audit id>         # ask again after an auto answer; does not reverse an action
    autonomy.py log [--json]            # autonomy.jsonl: auto answers, held pings, undos

State in $GREENLIGHT_HOME: autonomy.json (settings), autonomy.jsonl (audit). Stdlib only; never imports greenlight.py
at load (undo does, lazily, to ask again). scripts/decide.py (system one) is optional: without it, or when it fails,
proposals rest on past answers alone.
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions  # noqa: E402

try:
    import decide  # system one: decide.choice / decide.score. Owned by its own PR; optional here
except ImportError:
    decide = None

STATE = decisions.STATE
ROOT = Path(__file__).resolve().parent.parent
MEMORY = ROOT / "memory" / "MEMORY.md"
KINDS = ("delete-workspace", "keep-draft", "merge-held", "milestone", "agent-stuck", "signoff")
GATES = ("signoff", "merge-held")  # the lifecycle's human gates: never answered for you, whatever the history says
PROACTIVE = ("check-in", "watch")  # questions greenlight raises on its own, gated by gate()
T1, T2 = decisions.T1, decisions.T2
MODEL_WEIGHT = 4      # the model's pick counts as this many past answers
MIN_BAND = 5          # proposals in a tier before its measured accuracy caps P
GRADUATE = 10         # tier-1-strength proposals, right >= T1 of the time, before a kind is suggested for opt-in
HALF_LIFE = 14 * 86400  # the gate forgets a dismissal (and an answer) by half every two weeks
STALE = 24 * 3600     # a proactive question left unanswered this long was ignored
DEFAULTS = {"auto": [], "gate": 0.35}
RUBRIC = ("Is this message worth interrupting the user for right now? 1 when it is concrete and new: it names a "
          "specific workspace or MR, says what changed, and asks for one decision only they can make. 0 when it is "
          "filler, a status recap, a repeat, or something greenlight could do or wait out by itself.")
PREFLIGHT_RUBRICS = {
    "check-in": "Given this idle interval and the user's recent response to check-ins, is it useful to ask what work to do next? "
                "The board-based question has not been drafted yet. Score the timing, not whether this description names a workspace.",
    "watch": "Do these newly changed workspaces appear to need a decision only the user can make now? "
             "This is a preflight list, not the final question. Do not require an Options line.",
}

# Only these complete answers are inert enough to answer without the user. Other actions still get a suggestion.
NOOP_RE = re.compile(r"^\s*(?:keep draft|keep it a draft|keep workspace|keep the workspace)\s*[.!]?\s*$", re.I)
HIGH_RE = re.compile(r"\b(?:merge[sd]?|deploy(?:ed)?|sign(?:ed)?[ -]?off|approve[sd]?|release[sd]?|publish(?:ed)?|force|ship)\b", re.I)
GONE_RE = re.compile(r"\b(?:delete[sd]?|remove[sd]?|close[sd]?|discard(?:ed)?|drop(?:ped)?|kill(?:ed)?|abandon(?:ed)?|wipe[sd]?|reset|stop)\b", re.I)


def risk(option):
    """low (inert), medium (needs confirmation), or high (a human gate)."""
    if HIGH_RE.search(option):
        return "high"
    if GONE_RE.search(option):
        return "medium"
    return "low" if NOOP_RE.fullmatch(option) else "medium"


def settings():
    try:
        s = json.loads((STATE / "autonomy.json").read_text())
    except (OSError, ValueError):
        s = {}
    return DEFAULTS | {k: v for k, v in s.items() if k in DEFAULTS}


def save_settings(s):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / f"autonomy.json.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(s, indent=1))
    os.replace(tmp, STATE / "autonomy.json")


@contextlib.contextmanager
def settings_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "autonomy-settings.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def audit_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "autonomy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def audit(ev):
    STATE.mkdir(parents=True, exist_ok=True)
    ev = {"id": f"{time.time_ns():x}-{os.getpid():x}", "ts": int(time.time())} | ev
    with audit_lock():
        with (STATE / "autonomy.jsonl").open("a") as fh:
            fh.write(json.dumps(ev) + "\n")
    return ev


def audit_log():
    f = STATE / "autonomy.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def amend_audit(aid, **changes):
    with audit_lock():
        log = audit_log()
        e = next(x for x in log if x.get("id") == aid)
        e.update(changes)
        tmp = STATE / "autonomy.jsonl.tmp"
        tmp.write_text("".join(json.dumps(x) + "\n" for x in log))
        os.replace(tmp, STATE / "autonomy.jsonl")
        return e


def norm(s):
    return " ".join(str(s).lower().split())


def picked(r):
    """The option a past record's answer chose, as text, or None (typed something else, dismissed, unanswered)."""
    if r.get("answer") is None:
        return None
    c, opts = r.get("choice"), r.get("options") or []
    if isinstance(c, int) and 0 <= c < len(opts):
        return opts[c]
    return None


def match(label, options):
    """Index of a past option label among this question's options: same text, else the same first word ("delete" ~ "delete all 3")."""
    low = [norm(o) for o in options]
    lab = norm(label)
    if lab in low:
        return low.index(lab)
    first = lab.split(" ")[0] if lab else ""
    hits = [i for i, o in enumerate(low) if o.split(" ")[0] == first]
    return hits[0] if len(hits) == 1 else None


def human(r):
    """Answered by the user (not by autonomy, not dismissed)."""
    return r.get("answer") is not None and r.get("answer_via") != "auto"


def tally(kind, options, recs):
    """Past answers to this kind, as counts per current option, plus how many there were in all (incl. typed answers)."""
    counts, n = [0] * len(options), 0
    for r in recs:
        if r.get("kind") != kind or not human(r):
            continue
        n += 1
        label = picked(r)
        i = match(label, options) if label is not None else None
        if i is not None:
            counts[i] += 1
    return counts, n


def notes(kind, question, recs, limit=8):
    """What the model is told: the last answers to this kind, and memory/MEMORY.md lines that mention it."""
    past = [r for r in recs if r.get("kind") == kind and human(r)][-limit:]
    lines = [f"Past {kind} questions and the user's answers (oldest first):"]
    lines += [f"- {' '.join(r['question'].split())[:200]} → {r['answer']}" for r in past] or ["- none yet"]
    try:
        mem = MEMORY.read_text() if MEMORY.exists() else ""
    except OSError:
        mem = ""
    words = {w for w in re.findall(r"[a-z][a-z-]{3,}", f"{kind} {question}".lower())} - {"this", "that", "with", "from", "have"}
    hits = [l.strip() for l in mem.splitlines() if l.strip() and (kind in l.lower() or len(words & set(re.findall(r"[a-z][a-z-]{3,}", l.lower()))) >= 2)]
    if hits:
        lines += ["The user's standing decisions (memory/MEMORY.md):"] + [f"- {h[:200]}" for h in hits[:limit]]
    return "\n".join(lines)


def model_choice(question, options, context):
    """decide.choice, or None when system one isn't installed, fails, or is the unconfigured local fallback."""
    if decide is None:
        return None
    try:
        d = decide.choice(STATE, question, options, context=context)
    except Exception as e:
        print(f"autonomy: decide.choice: {e}", file=sys.stderr)
        return None
    try:
        i = int(d["index"])
        p = float((d.get("probs") or {}).get(options[i], d.get("confidence", 0)))
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if not 0 <= i < len(options) or d.get("provider") == "local":
        return None  # the local fallback is a guess by design: the past answers decide alone
    return {"index": i, "prob": max(0.0, min(1.0, p)), "provider": d.get("provider") or "?"}


def tier_of(p, risk_, opted):
    if risk_ == "high" or p < T2:
        return 3
    return 1 if p >= T1 and risk_ == "low" and opted else 2


calibrate = decisions.calibrate


def candidates(cal=None, s=None):
    """Kinds worth opting in: not yet, and their tier-1-strength proposals were right at least T1 of the time."""
    cal, s = calibrate() if cal is None else cal, settings() if s is None else s
    out = []
    for kind in KINDS:
        if kind in GATES:
            continue
        b = cal.get(kind, {}).get("bands", {}).get("auto", {})
        if kind not in s["auto"] and b.get("n", 0) >= GRADUATE and b["acc"] >= T1:
            out.append({"kind": kind, "n": b["n"], "right": b["right"], "acc": b["acc"]})
    return out


def propose(kind, text, recs=None, s=None):
    """The proposal for a question of this kind (text may end in an Options line), or None when it isn't one we
    propose for (an unknown kind, no options)."""
    question, options = decisions.split_options(text)
    kind = decisions.slug(kind or "")
    if kind not in KINDS or len(options) < 2:
        return None
    recs = decisions.load() if recs is None else recs
    s = settings() if s is None else s
    counts, n = tally(kind, options, recs)
    m = model_choice(question, options, notes(kind, question, recs))
    w = MODEL_WEIGHT if m else 0
    k = len(options)
    # the past answers as a Dirichlet(1) posterior, with the model's pick worth w answers of its own
    post = [(c + 1 + (w * m["prob"] if m and i == m["index"] else w * (1 - m["prob"]) / (k - 1) if m else 0)) / (n + k + w)
            for i, c in enumerate(counts)]
    i = max(range(k), key=lambda j: (post[j], counts[j], -j))
    p = post[i]
    why = []
    cal = calibrate(recs).get(kind)
    for _ in range(2):  # a band whose proposals measured below its floor caps P at what it measured (and re-tiers)
        b = (cal or {}).get("bands", {}).get(decisions.band(p))
        floor = T1 if p >= T1 else T2 if p >= T2 else 0
        if b and b["n"] >= MIN_BAND and b["acc"] < floor:
            why.append(f"capped at {b['acc']:.2f}: {b['right']} of {b['n']} such proposals were right")
            p = b["acc"]
    r = risk(options[i])
    opted = kind in s["auto"] and kind not in GATES
    t = 3 if kind in GATES else tier_of(p, r, opted)
    if r != "low":
        why.append(f"{options[i]!r} is {r} risk: {'always asked' if r == 'high' else 'never answered for you'}")
    elif p >= T1 and not opted:
        why.append(f"{kind} isn't opted in to auto answers")
    return {"kind": kind, "choice": options[i], "index": i, "prob": round(p, 3), "tier": t, "risk": r,
            "provider": m["provider"] if m else "history", "count": n, "agree": counts[i], "why": "; ".join(why)}


def quiet_propose(kind, text):
    """propose() that never fails its caller: a ping still goes out, plain, when this breaks."""
    try:
        return propose(kind, text)
    except Exception as e:
        print(f"autonomy: {e}", file=sys.stderr)
        return None


def outcome(r, now):
    """A proactive question's end: 'acted' (answered), 'dismissed' (closed with ✕, cleared, superseded or left a day), None (still waiting)."""
    if r.get("answer") is not None:
        return "acted"
    if r.get("closed") or now - r["ts"] > STALE:
        return "dismissed"
    return None


def gate(kind, text="", recs=None, s=None, now=None):
    """May a proactive question of this kind (an idle check-in, the watch, a --proactive ping) interrupt the user now?
    Score: acted-on versus dismissed history of the kind (decayed, Beta(1,1)), halved per one still waiting
    unanswered, and with the system-one model's read of the text (decide.score against RUBRIC) when there is text."""
    now = time.time() if now is None else now
    recs = decisions.load() if recs is None else recs
    s = settings() if s is None else s
    kind = decisions.slug(kind or "")
    acted = dismissed = 0.0
    waiting = 0
    for r in recs:
        if r.get("kind") != kind:
            continue
        o = outcome(r, now)
        w = 0.5 ** (max(0, now - r["ts"]) / HALF_LIFE)
        if o == "acted":
            acted += w
        elif o == "dismissed":
            dismissed += w
        else:
            waiting += 1
    hist = (acted + 1) / (acted + dismissed + 2)
    score = hist * 0.5 ** waiting
    provider = "history"
    if text and decide is not None:
        try:
            d = decide.score(STATE, PREFLIGHT_RUBRICS.get(kind, RUBRIC), context=text)
            if d.get("provider") != "local":
                model_score = max(0.0, min(1.0, float(d["score"])))
                score = min(score, (score + model_score) / 2)  # content may hold a ping; repeated dismissals still veto it
                provider = d.get("provider") or "?"
        except Exception as e:
            print(f"autonomy: decide.score: {e}", file=sys.stderr)
    ok = score >= s["gate"]
    return {"ok": ok, "kind": kind, "score": round(score, 3), "threshold": s["gate"], "acted": round(acted, 2),
            "dismissed": round(dismissed, 2), "waiting": waiting, "provider": provider}


def act(rec_id, prop, text, key=None, ws=()):
    """Tier 1: answer the record for the user, and log it with what undo needs. Returns the audit entry."""
    if not rec_id:
        raise ValueError("auto answer has no saved decision record")
    entry = None
    try:
        with decisions.book() as recs:
            for r in recs:
                if r["id"] == rec_id and decisions.is_open(r):
                    decisions.settle(r, prop["choice"], "auto")
                    entry = audit({"act": "auto", "record": rec_id, "kind": prop["kind"], "choice": prop["choice"],
                                   "prob": prop["prob"], "provider": prop["provider"], "text": text, "key": key, "ws": list(ws)})
                    break
            else:
                raise ValueError(f"auto answer record {rec_id} is not open")
    except Exception:
        if entry:
            amend_audit(entry["id"], cancelled=int(time.time()))
        raise
    return entry


def undo(aid):
    """Reask an auto answer, then record the user's correction for calibration."""
    hit = [e for e in audit_log() if e.get("id") == aid and e.get("act") == "auto"]
    if not hit:
        sys.exit(f"autonomy.py undo: no auto answer {aid}")
    e = hit[0]
    if e.get("undone"):
        return e
    import greenlight  # lazily: its import reads config
    q, opts = decisions.split_options(e["text"])
    asked = greenlight.ping(f"Undo: greenlight answered {e['choice']!r} for you; it's yours now.\n{q}\nOptions: {' | '.join(opts)}",
                            e.get("key"), e.get("ws") or (), e["kind"], ask=True)
    if not asked or not asked.get("asked"):
        raise RuntimeError("re-ask was not recorded")
    return amend_audit(aid, undone=int(time.time()), reasked_record=asked["asked"])


def report(s=None):
    s = settings() if s is None else s
    cal = calibrate()
    recent = [e for e in audit_log() if e.get("act") == "auto" and not e.get("cancelled")][-20:]
    return {"auto": s["auto"], "gate": s["gate"], "kinds": [{"kind": k, "eligible": k not in GATES, "opted": k in s["auto"] and k not in GATES,
            **{f: cal.get(k, {}).get(f) for f in ("n", "right", "acc")}, "bands": cal.get(k, {}).get("bands")} for k in KINDS],
            "candidates": candidates(cal, s), "recent": recent[::-1]}


def main():
    a, flags, pos = sys.argv[1:], {}, []
    it = iter(a)
    for x in it:
        if x in ("--kind", "--gate"):
            flags[x] = next(it, None)
        elif x == "--auto":  # --auto <kind> on|off, repeatable
            flags.setdefault(x, []).append((next(it, None), next(it, None)))
        elif x.startswith("--"):
            flags[x] = True
        else:
            pos.append(x)
    cmd, pos = (pos[0], pos[1:]) if pos else ("settings", [])
    opt, rest = flags.get, " ".join(pos)
    if cmd == "propose":
        print(json.dumps(propose(opt("--kind"), rest)))
    elif cmd == "gate":
        g = gate(opt("--kind"), rest)
        print(json.dumps(g))
        sys.exit(0 if g["ok"] else 1)
    elif cmd == "calibrate":
        cal = calibrate()
        if opt("--json"):
            return print(json.dumps({"kinds": cal, "candidates": candidates(cal)}, indent=1))
        for k, v in sorted(cal.items()):
            bands = "  ".join(f"{b} {x['right']}/{x['n']}" for b, x in v["bands"].items() if x["n"])
            print(f"{k:18} {v['right']}/{v['n']} right ({v['acc']:.0%})   {bands}")
        for c in candidates(cal):
            print(f"graduate? {c['kind']}: {c['right']} of {c['n']} tier-1 proposals right; `autonomy.py set --auto {c['kind']} on`")
    elif cmd == "set":
        with settings_lock():
            s = settings()
            for kind, on in opt("--auto") or []:
                if kind not in KINDS or kind in GATES or on not in ("on", "off"):
                    sys.exit(f"autonomy.py set --auto: one of {', '.join(k for k in KINDS if k not in GATES)}, then on|off")
                s["auto"] = sorted(set(s["auto"]) | {kind}) if on == "on" else [k for k in s["auto"] if k != kind]
            if opt("--gate") is not None:
                s["gate"] = max(0.0, min(1.0, float(opt("--gate"))))
            save_settings(s)
        print(json.dumps(s))
    elif cmd == "undo":
        if not pos:
            sys.exit("autonomy.py undo <audit id>")
        print(json.dumps(undo(pos[0])))
    elif cmd == "log":
        log = audit_log()
        if opt("--json"):
            return print(json.dumps(log, indent=1))
        for e in log:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
            what = (f"auto {e['kind']}: {e['choice']} (P {e['prob']}){' UNDONE' if e.get('undone') else ''}" if e.get("act") == "auto"
                    else f"held {e.get('kind')}: score {e.get('score')} < {e.get('threshold')}")
            print(f"{when}  {e['id']}  {what}  {' '.join(str(e.get('text', '')).split())[:70]}")
    else:  # settings
        r = report()
        if opt("--json"):
            return print(json.dumps(r))
        print(f"auto answers: {', '.join(r['auto']) or 'none'}   ping gate: {r['gate']}")
        for k in r["kinds"]:
            print(f"  {k['kind']:18} {'on ' if k['opted'] else 'off'}  {k['right'] or 0}/{k['n'] or 0} proposals right")


if __name__ == "__main__":
    main()
