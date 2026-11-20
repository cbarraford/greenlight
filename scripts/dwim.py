#!/usr/bin/env python3
"""dwim: the sub-second fast path for board actions said in voice mode, typed in the chat, or typed in the ⌘J palette.

    dwim.py plan   < {text, source, focus?, pucks}   one JSON line: route it straight to the board ("direct") or to Chief ("chief"),
                                                     and for a direct one the actions, the toast and the line to say
    dwim.py apply  < {text, actions}                 run a direct plan's actions; one JSON line with an undo id
    dwim.py undo <id>                                put back what that apply changed (goals it edited and nobody has edited since)
    dwim.py stage  < {text, focus?, pucks}           one JSON line: an instant stage card (table, timeline, diff, lifecycle) drawn
                                                     from the snapshot's facts while Chief thinks, or {"kind": null}

"park the two auth ones", "kick off CI fixes on the red ones", "hold #42": a verb, the workspaces it's about, and nothing to reason
about. The words are read two ways at once: a small grammar here (always), and decide.py's System One (scripts/decide.py, when it is
installed and configured) through one decide.batch: the route (decide.choice direct_board_action vs needs_chief_reasoning), the verb,
and a yes/no per candidate workspace, all in parallel, under a deadline. Either can send it to Chief; neither alone can act on a
destructive or ambiguous request:
  - only these verbs go direct: park, unpark, hold, release (the hold), and an agent to fix CI, rebase, or answer threads
  - merge, close, delete, approve, ready, deploy … never go direct (Chief asks first), nor a question, a condition or a negation
  - a count that doesn't match ("the two auth ones" when three match), no match, a #N in two repos, or more than MAX_BULK: Chief
  - a goal edit is applied at once and undone by Undo; an agent launch waits out the app's Undo window before it starts
decide.py's local fallback (unconfigured) answers with low confidence, which this reads as no opinion: the grammar decides alone.

Stdlib only. `plan` and `stage` never call the forge or Orca: the app hands them its latest snapshot. `apply` and `undo` edit goals
through Orca and launch agents through dash.py.
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import decide  # system-one-decide's scripts/decide.py
except ImportError:  # not merged or not synced yet: the grammar decides alone
    decide = None
import lifestage  # noqa: E402  (stdlib only, cheap)

STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
DWIM = STATE / "dwim"
DEADLINE = 0.6  # seconds decide.batch gets before the grammar answers alone: the whole plan stays under a second
STAGE_DEADLINE = 0.15  # the stage template pick: "One minute" is already up, so it only helps if it's quick
SURE = 0.7  # decide's confidence (or prob) that counts as an opinion; below, the grammar decides
MAX_BULK = 5  # more workspaces than this in one command goes to Chief, who confirms
UNDO_KEEP = 30

# The verbs that may go direct, in the order they're tried: the un- forms first ("unpark" mustn't read as "park").
VERBS = {
    "unpark": r"\bun-?park\b|\bun-?shelve\b|\bbring (?:\w+ ){0,4}back\b|\bunsnooze\b",
    "release": r"\bun-?hold\b|\brelease (?:the )?hold\b|\blift (?:the )?hold\b|\bremove (?:the )?hold\b|\bok to merge\b|\bclear (?:the )?hold\b",
    "park": r"\bpark\b|\bshelve\b|\bsnooze\b|\bput (?:\w+ ){0,4}aside\b",
    "hold": r"\bhold\b|\bdo ?n[o'’]?t merge\b|\bdo not merge\b|\bfreeze\b",
    "ci": r"\bfix(?:es|ing)? (?:the |its |their )?(?:ci|build|builds|pipelines?|tests?|red)\b|\bci fix(?:es)?\b|\bfix(?:es)? (?:for )?ci\b|\bget (?:\w+ ){0,3}green\b",
    "rebase": r"\brebase\b|\b(?:fix|resolve)(?:es)? (?:the |its |their )?(?:merge )?conflicts?\b",
    "threads": r"\b(?:address|answer|fix|resolve|handle)(?:es)? (?:the |its |their )?(?:review |open )?(?:threads|comments|feedback|review)\b",
}
VERB_RES = {k: re.compile(v, re.I) for k, v in VERBS.items()}
LAUNCH = {"ci": "ci", "rebase": "conflicts", "threads": "threads"}  # verb → the dash.py move kind whose prompt the agent gets
DONE_WORDS = {"park": "Parked", "unpark": "Unparked", "hold": "Held", "release": "Released the hold on",
              "ci": "Starting CI fixes on", "rebase": "Starting rebases on", "threads": "Starting thread fixes on"}
SAY_WORDS = {"park": "Parked", "unpark": "Unparked", "hold": "Holding", "release": "Released", "ci": "Fixing CI on",
             "rebase": "Rebasing", "threads": "Answering threads on"}
# Never direct: outward-facing, destructive or irreversible. Chief asks first. Phrases that only look like these are cut out first.
NOT_DESTRUCTIVE = re.compile(r"\bmerge conflicts?\b|\bdo ?n[o'’]?t merge\b|\bdo not merge\b|\bok to merge\b|\bremove (?:the )?hold\b", re.I)
DESTRUCTIVE = re.compile(r"\b(?:merge[sd]?|merging|close[sd]?|closing|delete[sd]?|deleting|remove[sd]?|drop(?:ped)?|archive[sd]?|kill(?:ed)?|"
                         r"abandon(?:ed)?|force|reset|revert(?:ed)?|wipe|discard|deploy(?:ed)?|ship|approve[sd]?|undraft|ready|push|"
                         r"cancel(?:led)?|stop|restart|rename|retarget|squash|cherry-?pick)\b", re.I)
REASONING = re.compile(r"\?\s*$|^\s*(?:why|what|how|which|who|when|where|should|could|would|can|is|are|does|do you|tell|explain|"
                       r"show|summari[sz]e|compare|list)\b|\b(?:if|unless|until|after|before|once|then|instead|because|except|but)\b", re.I)
NEGATION = re.compile(r"\b(?:don'?t|do not|never|no longer|stop)\b(?! merge)", re.I)
REF_RE = re.compile(r"(?:(?<![\w])[#!]|\b(?:pr|mr|pull request|merge request)\s*#?)(\d{1,7})\b", re.I)
HERE_RE = re.compile(r"\b(?:this(?: one)?|it|here|current(?: one)?|that one)\b", re.I)
NUMS = {"one": 1, "single": 1, "two": 2, "both": 2, "pair": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
COUNT_RE = re.compile(r"\b(?:the |those |these )?(" + "|".join(NUMS) + r"|\d{1,2})\b(?! *(?:min|minute|hour|day|sec)s?\b)", re.I)
FILTERS = {  # status words → a fact on the puck
    "red": (re.compile(r"\b(?:red|failing|failed|broken|busted)\b", re.I), lambda p: (p.get("mr") or {}).get("pipeline") == "failed"),
    "conflicts": (re.compile(r"\b(?:conflicted|conflicting|with conflicts|in conflict)\b", re.I), lambda p: bool((p.get("mr") or {}).get("conflicts"))),
    "drafts": (re.compile(r"\bdrafts?\b", re.I), lambda p: bool((p.get("mr") or {}).get("draft"))),
    "parked": (re.compile(r"\bparked\b", re.I), lambda p: bool(p.get("hidden"))),
    "idle": (re.compile(r"\b(?:idle|stuck|stalled|quiet)\b", re.I), lambda p: p.get("state") in ("quiet", "crashed")),
}
STOP = set("""a an the on of in for to and or with my our your all every each ones one those these them that this it its their please now
           just go ahead can you could would kick off start run fix fixes get put back aside workspace workspaces ws prs pr mrs mr pull request
           requests branch branches agent agents some few couple ci build builds pipeline pipelines tests test review threads comments feedback
           conflicts conflict merge rebase hold park unpark release lift remove clear shelve snooze unhold freeze ok do don't dont not
           green red failing failed broken busted conflicted conflicting drafts draft parked idle stuck quiet stalled up let's lets""".split())
POLITE = re.compile(r"^(?:(?:hey|ok|okay|so|and|now|chief)[,\s]+)*(?:(?:can|could|would|will) you (?:please )?|please |go ahead and |let'?s )|[\s,]*(?:please|thanks|thank you)?[.!?\s]*$", re.I)
SOURCES = ("voice", "chat", "jump")
HOLD = re.compile(r"(?<![\w-])(?:hold|do ?n[o'’]t merge)(?![\w-])", re.I)  # greenlight.HOLD_RE: not imported, plan stays light


# ---- decide.py, under a deadline -------------------------------------------------------------------------------------

def _local(questions):
    """decide.py's own contract with no opinion: what the router gets when decide.py is missing, broken or late."""
    out = {}
    for k, q in questions.items():
        if q.get("kind") == "choice":
            opts = q.get("options") or [""]
            out[k] = {"choice": opts[0], "index": 0, "probs": {o: 1 / len(opts) for o in opts}, "confidence": 0.0, "provider": "none"}
        elif q.get("kind") == "score":
            out[k] = {"score": 0.5, "provider": "none"}
        else:
            out[k] = {"value": False, "prob": 0.5, "provider": "none"}
    return out


_pool = cf.ThreadPoolExecutor(max_workers=MAX_BULK + 1)


def ask(state, questions, deadline=DEADLINE):
    """decide.batch(state, questions) within `deadline` seconds; each answer missing or malformed is no opinion.
    Each question is {"kind": "choice"|"bool"|"score", "question", "options"?, "context"?} (decide.batch's dict of dicts)."""
    if not questions:
        return {}, "none", 0
    t = time.monotonic()
    got, provider = {}, "none"
    if decide is not None and hasattr(decide, "batch"):
        f = _pool.submit(decide.batch, state, questions)
        try:
            got = f.result(timeout=deadline) or {}
            provider = next((a.get("provider") for a in got.values() if isinstance(a, dict) and a.get("provider")), "decide")
        except cf.TimeoutError:
            provider = "timeout"
        except Exception as e:  # decide.py's own failure is never the user's: the grammar answers
            provider = f"error: {type(e).__name__}"
            got = {}
    base = _local(questions)
    out = {k: (got[k] if isinstance(got.get(k), dict) else base[k]) for k in questions}
    return out, provider, round((time.monotonic() - t) * 1000)


def sure_choice(a):
    """A choice answer's pick when decide is confident and it isn't the local fallback; else None."""
    if not isinstance(a, dict) or str(a.get("provider", "")).lower() in ("local", "none", ""):
        return None
    try:
        return a.get("choice") if float(a.get("confidence", 0)) >= SURE else None
    except (TypeError, ValueError):
        return None


def sure_bool(a):
    """True/False when decide is confident and it isn't the local fallback; else None. `prob` is read as P(true) when it disagrees
    with `value` the other way round, so either reading of the contract gives the same answer on a confident reply."""
    if not isinstance(a, dict) or str(a.get("provider", "")).lower() in ("local", "none", "") or not isinstance(a.get("value"), bool):
        return None
    try:
        p = float(a.get("prob", 0.5))
    except (TypeError, ValueError):
        return None
    if a["value"]:
        return True if p >= SURE else None
    return False if p >= SURE or p <= 1 - SURE else None


# ---- reading the words ------------------------------------------------------------------------------------------------

def words(s):
    return re.findall(r"[a-z0-9][a-z0-9_.+-]*", s.lower())


def facts_of(p):
    """A puck in a line: what decide.py and Chief are told about a candidate workspace."""
    mr = p.get("mr") or {}
    bits = [p.get("name", ""), f"repo {p.get('repo', '')}", f"goal: {(p.get('goal') or '')[:140]}"]
    if mr:
        bits.append(f"{mr.get('ref') or '#' + str(mr.get('iid'))} {mr.get('title', '')[:100]} (ci {mr.get('pipeline') or 'none'}"
                    f"{', conflicts' if mr.get('conflicts') else ''}{', draft' if mr.get('draft') else ''})")
    if p.get("hidden"):
        bits.append("parked")
    return " · ".join(b for b in bits if b)


def hay(p):
    mr = p.get("mr") or {}
    return " ".join([p.get("name", ""), p.get("branch", ""), p.get("goal", ""), mr.get("title", "") or "", p.get("repo", ""),
                     " ".join(mr.get("labels") or [])]).lower()


def grammar(text, pucks, focus=None):
    """What the words alone say: {verbs, refs, names, filters, count, tokens, here, why} (why: a reason it must go to Chief)."""
    t = POLITE.sub("", " ".join(text.split())).strip()
    g = {"verbs": [], "refs": [], "names": [], "filters": [], "count": None, "tokens": [], "here": False, "why": None}
    for v, r in VERB_RES.items():
        if r.search(t):
            g["verbs"].append(v)
    if "release" in g["verbs"]:
        g["verbs"] = [v for v in g["verbs"] if v != "hold"]  # "release the hold" says hold too
    if "unpark" in g["verbs"]:
        g["verbs"] = [v for v in g["verbs"] if v != "park"]
    # the workspaces named outright, longest first, cut out of the text so their words aren't read as anything else
    rest = t
    for p in sorted(pucks, key=lambda p: -len(p.get("name", ""))):
        n = p.get("name", "")
        if len(n) >= 3 and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", rest, re.I):
            g["names"].append(n)
            rest = re.sub(rf"(?<![\w-]){re.escape(n)}(?![\w-])", " ", rest, flags=re.I)
    g["refs"] = [int(m) for m in REF_RE.findall(rest)]
    rest = REF_RE.sub(" ", rest)
    cleared = NOT_DESTRUCTIVE.sub(" ", rest)
    if m := DESTRUCTIVE.search(cleared):
        g["why"] = f"“{m[0]}” isn't something the fast path does on its own"
    elif REASONING.search(rest):
        g["why"] = "it's a question or has a condition"
    elif NEGATION.search(NOT_DESTRUCTIVE.sub(" ", rest)):
        g["why"] = "it says not to do something"
    elif len(g["verbs"]) > 1:
        g["why"] = f"it asks for more than one thing ({', '.join(g['verbs'])})"
    for k, (r, _) in FILTERS.items():
        if r.search(rest):
            g["filters"].append(k)
    if m := COUNT_RE.search(rest):
        w = m[1].lower()
        g["count"] = NUMS.get(w) or (int(w) if w.isdigit() else None)
    g["here"] = bool(HERE_RE.search(rest)) and not g["names"] and not g["refs"] and bool(focus)
    bare = NOT_DESTRUCTIVE.sub(" ", rest)
    for r in VERB_RES.values():
        bare = r.sub(" ", bare)
    g["tokens"] = [] if g["names"] or g["refs"] or g["here"] else \
        [w for w in words(bare) if w not in STOP and w not in NUMS and (not w.isdigit() or len(w) >= 3) and len(w) > 1]  # a long number may be part of a name
    return g


def structural(g, pucks, focus):
    """The workspaces the names, #refs, "this" and status words leave (before the free words are read), or (None, why)."""
    verb = g["verbs"][0] if g["verbs"] else None
    pool = [p for p in pucks if verb == "unpark" or "parked" in g["filters"] or not p.get("hidden")]
    if g["names"]:
        pool = [p for p in pool if p.get("name") in g["names"]]
    if g["refs"]:
        pool = [p for p in pool if (p.get("mr") or {}).get("iid") in g["refs"]]
        for n in g["refs"]:
            hits = [p for p in pool if (p.get("mr") or {}).get("iid") == n]
            if len({p.get("repo") for p in hits}) > 1:
                return None, f"#{n} is open in {len(hits)} repos: {', '.join(sorted(p['name'] for p in hits))}"
            if not hits:
                return None, f"no workspace has #{n}"
    if g["here"]:
        pool = [p for p in pool if p.get("name") == focus]
    for f in g["filters"]:
        if f != "parked" or verb != "unpark":
            pool = [p for p in pool if FILTERS[f][1](p)]
    if not (g["names"] or g["refs"] or g["here"] or g["filters"] or g["tokens"]):
        return None, "it doesn't say which workspaces"
    return pool, None


def lexical(g, pool):
    return [p for p in pool if all(w in hay(p) for w in g["tokens"])]


def pick_targets(g, pool, answers):
    """The workspaces meant, out of structural()'s pool, or (None, why) when it isn't clear."""
    if g["tokens"]:
        lex = lexical(g, pool)
        sem = {p["name"]: sure_bool(answers.get("is:" + p["name"])) for p in pool if "is:" + p["name"] in answers}
        if any(v is not None for v in sem.values()):  # System One read the phrase: its yes/no beats the substring match
            pool = [p for p in pool if sem.get(p["name"]) or (sem.get(p["name"]) is None and p in lex)]
        else:
            pool = lex
    if not pool:
        return None, "nothing on the board matches"
    if g["count"] and g["count"] != len(pool):
        return None, f"it says {g['count']} but {len(pool)} match: {', '.join(sorted(p['name'] for p in pool)[:8])}"
    if len(pool) > MAX_BULK:
        return None, f"{len(pool)} workspaces is more than the fast path does at once"
    return pool, None


def applies(verb, p):
    """None when the verb applies to this workspace, else why it's skipped."""
    goal, ag = p.get("goal") or "", (p.get("agent") or {}).get("state")
    hold = HOLD
    if verb == "park" and p.get("hidden"):
        return "already parked"
    if verb == "unpark" and not p.get("hidden"):
        return "isn't parked"
    if verb == "hold" and hold.search(goal):
        return "already on hold"
    if verb == "release":
        if not hold.search(goal):
            return "isn't on hold"
        if hold.search(re.sub(r"\(hold\)", "", goal, flags=re.I)):
            return "its goal says hold in its own words: edit the goal"
    if verb in LAUNCH:
        if ag in ("working", "waiting"):
            return f"its agent is {ag}"
        if not any(m.get("kind") == LAUNCH[verb] for m in p.get("moves") or []):
            return {"ci": "CI isn't failing", "rebase": "has no conflicts", "threads": "has no open threads"}[verb]
    return None


def joined(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def plan(o):
    """The route for one command, with timings. See the module doc."""
    t0 = time.monotonic()
    text, src = (o.get("text") or "").strip(), o.get("source") if o.get("source") in SOURCES else "chat"
    pucks, focus = [p for p in o.get("pucks") or [] if isinstance(p, dict) and p.get("name")], o.get("focus") or None
    chief = lambda why, **k: {"route": "chief", "why": why, "text": text, "source": src, **k}
    if not text or len(text) > 300 or "\n" in text.strip():
        return chief("too long or empty for the fast path", ms={"total": 0})
    g = grammar(text, pucks, focus)
    gram_ms = round((time.monotonic() - t0) * 1000)
    if g["why"]:
        return chief(g["why"], ms={"grammar": gram_ms, "total": gram_ms})
    pool, why = structural(g, pucks, focus)
    if why:
        return chief(why, verb=g["verbs"][:1] and g["verbs"][0], ms={"grammar": gram_ms, "total": gram_ms})
    # the candidates System One is asked about: the ones the structural words leave, capped
    state = {"board": [facts_of(p) for p in pucks[:40]], "focus": focus, "source": src}
    qs = {"route": {"kind": "choice", "question": f"The user said: “{text}”. Is it a direct board action (park, unpark, hold, release a hold, "
                                                  "start an agent to fix CI, rebase or answer threads, on named workspaces), or does it need "
                                                  "Chief to reason, look things up or confirm?",
                    "options": ["direct_board_action", "needs_chief_reasoning"]},
          "verb": {"kind": "choice", "question": f"Which board action does “{text}” ask for?", "options": [*VERBS, "none"]}}
    if g["tokens"]:
        phrase = " ".join(g["tokens"])
        lex = lexical(g, pool)
        for p in (lex + [p for p in pool if p not in lex])[:12]:  # the substring matches first, then the rest of the pool
            qs["is:" + p["name"]] = {"kind": "bool", "question": f"Does “{phrase}” in “{text}” refer to this workspace?", "context": facts_of(p)}
    answers, provider, dec_ms = ask(state, qs)
    ms = {"grammar": gram_ms, "decide": dec_ms}
    route, verb_d = sure_choice(answers.get("route")), sure_choice(answers.get("verb"))
    if route == "needs_chief_reasoning":
        return chief("System One says it needs reasoning", provider=provider, ms=ms | {"total": round((time.monotonic() - t0) * 1000)})
    verb = g["verbs"][0] if g["verbs"] else None
    if verb_d and verb_d != "none" and verb and verb_d != verb:
        return chief(f"unclear whether it means {verb} or {verb_d}", provider=provider, ms=ms | {"total": round((time.monotonic() - t0) * 1000)})
    if not verb and verb_d in VERBS and route == "direct_board_action":
        verb = verb_d  # System One read a verb the grammar has no words for ("put the auth ones on ice")
        g["verbs"] = [verb]
    if not verb:
        return chief("no board action in it", provider=provider, ms=ms | {"total": round((time.monotonic() - t0) * 1000)})
    targets, why = pick_targets(g, pool, answers)
    ms["total"] = round((time.monotonic() - t0) * 1000)
    if why:
        return chief(why, provider=provider, verb=verb, ms=ms)
    acts, skipped = [], []
    for p in targets:
        if w := applies(verb, p):
            skipped.append({"ws": p["name"], "why": w})
            continue
        a = {"verb": verb, "ws": p["name"], "ref": (p.get("mr") or {}).get("ref")}
        if verb in LAUNCH:
            a["prompt"] = next(m["prompt"] for m in p["moves"] if m.get("kind") == LAUNCH[verb])
        acts.append(a)
    names = [a["ws"] for a in acts]
    skip = "; ".join(f"{s['ws']}: {s['why']}" for s in skipped)
    say = (f"{SAY_WORDS[verb]} {joined(names)}." if names else "Nothing to do.") + (f" Skipped {skip}." if skip else "")
    return {"route": "direct", "text": text, "source": src, "verb": verb, "actions": acts, "skipped": skipped,
            "toast": f"{DONE_WORDS[verb]} {joined(names)}" if names else f"Nothing to do: {skip}",
            "say": say, "launch": verb in LAUNCH and bool(acts), "provider": provider, "ms": ms}


# ---- doing it -----------------------------------------------------------------------------------------------------------

def _goal_after(verb, goal):
    goal = " ".join((goal or "").split())
    if verb == "park":
        return goal if re.search(r"\(parked\)", goal, re.I) else f"{goal} (parked)".strip()
    if verb == "unpark":
        return " ".join(re.sub(r"\(parked\)", "", goal, flags=re.I).split())
    if verb == "hold":
        return goal if HOLD.search(goal) else f"{goal} (hold)".strip()
    if verb == "release":
        return " ".join(re.sub(r"\(hold\)", "", goal, flags=re.I).split())
    return goal


def _note(name, text):
    import greenlight
    hf = greenlight.history_file(name)
    hf.parent.mkdir(parents=True, exist_ok=True)
    with hf.open("a") as h:
        h.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {text}\n")


def apply(o):
    """Run a direct plan's actions in parallel. Goal edits record what they replaced, for undo."""
    import dash
    import greenlight
    t0 = time.monotonic()
    acts = [a for a in o.get("actions") or [] if isinstance(a, dict) and a.get("verb") in VERBS and isinstance(a.get("ws"), str)]
    if not acts:
        return {"ok": True, "done": [], "errors": []}
    live = {w["displayName"]: w for w, _ in dash.live_agents().values()}
    said = " ".join((o.get("text") or "").split())[:200]

    def one(a):
        w = live.get(a["ws"])
        if not w:
            return {"ws": a["ws"], "error": "no such workspace"}
        if a["verb"] in LAUNCH:
            r = dash.run(a["ws"], a.get("prompt") or "")
            return {"ws": a["ws"], **({"error": r["error"]} if r.get("error") else {"agent": r.get("agent")})}
        before = w.get("comment") or ""
        after = _goal_after(a["verb"], before)
        if after == " ".join(before.split()):
            return {"ws": a["ws"], "unchanged": True}
        greenlight.orca("worktree", "set", "--worktree", f"id:{w['worktreeId']}", "--comment", after)
        _note(a["ws"], f"{a['verb']} from {o.get('source') or 'the chat'}: “{said}”")
        return {"ws": a["ws"], "id": w["worktreeId"], "before": before, "after": after}

    out = list(_pool.map(lambda a: _safe(one, a), acts))
    done = [r for r in out if not r.get("error")]
    undo = None
    edits = [r for r in done if "after" in r]
    if edits:
        undo = uuid.uuid4().hex[:12]
        (DWIM / "undo").mkdir(parents=True, exist_ok=True)
        (DWIM / "undo" / f"{undo}.json").write_text(json.dumps({"at": time.time(), "text": said, "edits": edits}))
        for f in sorted((DWIM / "undo").glob("*.json"), key=lambda f: f.stat().st_mtime)[:-UNDO_KEEP]:
            f.unlink(missing_ok=True)
    res = {"ok": not any(r.get("error") for r in out), "undo": undo, "done": [r["ws"] for r in done],
           "errors": [f"{r['ws']}: {r['error']}" for r in out if r.get("error")], "ms": round((time.monotonic() - t0) * 1000)}
    log({"op": "apply", "text": said, "verb": acts[0]["verb"], "done": res["done"], "errors": res["errors"], "ms": res["ms"]})
    return res


def _safe(f, a):
    try:
        return f(a)
    except Exception as e:
        return {"ws": a.get("ws"), "error": str(e)[:200]}


def undo(uid):
    """Put each goal back, unless someone edited it since (then it's left, and said)."""
    import dash
    import greenlight
    if not re.fullmatch(r"[0-9a-f]{12}", uid or ""):
        return {"error": "bad undo id"}
    f = DWIM / "undo" / f"{uid}.json"
    if not f.exists():
        return {"error": "nothing to undo"}
    rec = json.loads(f.read_text())
    live = {w["worktreeId"]: w for w, _ in dash.live_agents().values()}
    back, kept = [], []
    for e in rec["edits"]:
        w = live.get(e["id"])
        if not w or " ".join((w.get("comment") or "").split()) != " ".join(e["after"].split()):
            kept.append(e["ws"])
            continue
        greenlight.orca("worktree", "set", "--worktree", f"id:{e['id']}", "--comment", e["before"])
        _note(e["ws"], f"undid: “{rec.get('text', '')}”")
        back.append(e["ws"])
    f.unlink(missing_ok=True)
    log({"op": "undo", "text": rec.get("text"), "back": back, "kept": kept})
    return {"ok": True, "undone": back, "kept": kept}


def log(e):
    try:
        DWIM.mkdir(parents=True, exist_ok=True)
        with (DWIM / "log.jsonl").open("a") as h:
            h.write(json.dumps({"ts": int(time.time()), **e}) + "\n")
    except OSError:
        pass


# ---- the instant stage card ----------------------------------------------------------------------------------------------

TEMPLATES = ("table", "timeline", "diff", "lifecycle", "none")
TEMPLATE_WORDS = [
    ("diff", re.compile(r"\b(?:diff|changed?|changes|what did (?:it|they) (?:do|change)|files? touched|lines?)\b", re.I)),
    ("lifecycle", re.compile(r"\b(?:where (?:is|are|does)|stage|lifecycle|how far|status of|progress|close to|left (?:on|for))\b", re.I)),
    ("timeline", re.compile(r"\b(?:history|timeline|when|happened|today|yesterday|lately|recently|since)\b", re.I)),
    ("table", re.compile(r"\b(?:which|list|all|board|overview|everything|red|failing|waiting|needs? me|status|ci)\b", re.I)),
]
CI_WORD = {"failed": "failed", "success": "passed", "running": "running", "pending": "pending", "canceled": "canceled", None: "—"}


def _named(text, pucks, focus):
    for p in sorted(pucks, key=lambda p: -len(p.get("name", ""))):
        if len(p.get("name", "")) >= 3 and re.search(rf"(?<![\w-]){re.escape(p['name'])}(?![\w-])", text, re.I):
            return p
    refs = [int(m) for m in REF_RE.findall(text)]
    hits = [p for p in pucks if (p.get("mr") or {}).get("iid") in refs]
    if len(hits) == 1:
        return hits[0]
    return next((p for p in pucks if p.get("name") == focus), None) if focus and HERE_RE.search(text) else None


def card_table(text, pucks, one):
    rows = [p for p in pucks if not p.get("hidden")]
    for f, (r, test) in FILTERS.items():
        if r.search(text):
            rows = [p for p in rows if test(p)]
    rows = sorted(rows, key=lambda p: (p.get("life") or {}).get("pile") != "yours")[:10]
    if not rows:
        return None
    tone = lambda p: "bad" if (p.get("mr") or {}).get("pipeline") == "failed" or p.get("state") == "crashed" else \
        "now" if (p.get("life") or {}).get("pile") == "yours" else "ok" if (p.get("life") or {}).get("pile") == "done" else ""
    return {"kind": "table", "eyebrow": "the board", "title": f"{len(rows)} workspace{'s' * (len(rows) != 1)}",
            "columns": ["Workspace", "MR", "CI", "Next"],
            "rows": [[p["name"], (p.get("mr") or {}).get("ref") or "—", CI_WORD.get((p.get("mr") or {}).get("pipeline"), "—"),
                      ((p.get("life") or {}).get("do") or p.get("action") or "")[:80]] for p in rows],
            "tones": [tone(p) for p in rows]}


def card_lifecycle(text, pucks, one):
    if not one:
        return None
    life = one.get("life") or {}
    keys = [s[0] for s in lifestage.STAGES]
    i = keys.index(life["stage"]) if life.get("stage") in keys else -1
    done = life.get("pile") == "done"
    stages = [{"label": name, "state": "done" if done or j < i else "now" if j == i else "todo",
               "detail": (life.get("sub_name") or "") if j == i else ""} for j, (_, name, _) in enumerate(lifestage.STAGES)]
    return {"kind": "lifecycle", "eyebrow": one["name"], "title": life.get("do") or one.get("action") or one["name"],
            "stages": stages, "at": life.get("at") or "", "holder": life.get("holder") or ""}


def card_timeline(text, pucks, one):
    if not one:
        return None
    try:
        import dash
        hist = dash.history(one["name"])[:6]
    except Exception:
        hist = []
    if not hist:
        return None
    return {"kind": "timeline", "eyebrow": one["name"], "title": "Lately", "items": [{"at": e["ts"][5:], "label": e["text"][:120]} for e in hist]}


def card_diff(text, pucks, one):
    if not one or not one.get("path"):
        return None
    import subprocess
    base = (one.get("mr") or {}).get("target_branch") or "main"
    try:
        out = subprocess.run(["git", "-C", one["path"], "diff", "--numstat", f"origin/{base}...HEAD"], capture_output=True, text=True, timeout=0.5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    files = []
    for line in out.splitlines():
        a, d, path = (line.split("\t", 2) + ["", ""])[:3]
        files.append({"path": path, "add": int(a) if a.isdigit() else 0, "del": int(d) if d.isdigit() else 0})
    if not files:
        return None
    files.sort(key=lambda f: -(f["add"] + f["del"]))
    return {"kind": "diff", "eyebrow": one["name"], "title": f"{len(files)} file{'s' * (len(files) != 1)} changed",
            "files": files[:10], "add": sum(f["add"] for f in files), "del": sum(f["del"] for f in files)}


CARDS = {"table": card_table, "lifecycle": card_lifecycle, "timeline": card_timeline, "diff": card_diff}


def stage(o):
    """The instant card for what was just said: System One picks a template (~150 ms), facts fill it. {"kind": None} for none."""
    t0 = time.monotonic()
    text = (o.get("text") or "").strip()
    pucks, focus = [p for p in o.get("pucks") or [] if isinstance(p, dict) and p.get("name")], o.get("focus") or None
    if not text or not pucks:
        return {"kind": None, "ms": 0}
    one = _named(text, pucks, focus)
    words_pick = next((k for k, r in TEMPLATE_WORDS if r.search(text)), None)
    state = {"board": [facts_of(p) for p in pucks[:30]], "focus": focus, "named": one and one["name"]}
    a, provider, dec_ms = ask(state, {"t": {"kind": "choice", "question": f"The user asked: “{text}”. Which card shows the answer best "
                                                                       "while the full answer is written?", "options": list(TEMPLATES)}},
                              deadline=STAGE_DEADLINE)
    pick = sure_choice(a["t"]) or words_pick
    spec = None
    for k in ([pick] if pick in CARDS else []) + (["lifecycle"] if one and pick != "none" else []):  # one workspace named: at least where it is
        if spec := CARDS[k](text, pucks, one):
            break
    return (spec or {"kind": None}) | {"provider": provider, "ms": round((time.monotonic() - t0) * 1000), "decide_ms": dec_ms}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "undo":
        print(json.dumps(undo(sys.argv[2] if len(sys.argv) > 2 else "")))
        return
    if cmd not in ("plan", "apply", "stage"):
        raise SystemExit(__doc__)
    try:
        o = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        o = {}
    if not isinstance(o, dict):
        o = {}
    r = {"plan": plan, "apply": apply, "stage": stage}[cmd](o)
    if cmd == "plan":
        log({"op": "plan", "text": r["text"][:200], "route": r["route"], "why": r.get("why"), "verb": r.get("verb"), "ms": r.get("ms"),
             "provider": r.get("provider")})
    print(json.dumps(r), flush=True)
    os._exit(0)  # a late decide.batch thread mustn't hold the answer up


if __name__ == "__main__":
    main()
