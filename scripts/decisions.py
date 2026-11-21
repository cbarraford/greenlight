#!/usr/bin/env python3
"""The decision record: every question greenlight puts to the user, the situation it was asked in, and the answer.

One JSON object per line in ~/.greenlight/decisions.jsonl. The answer is written into the question's own record:

    {"id", "ts", "via": "ping" | "chat" | "ask", "key", "kind", "question", "options": [...],
     "workspace", "repo", "role", "step", "owner", "mr",    # the workspace it's about, as of the last scan
     "workspaces": [{...}, ...],                           # only when it's about several: the fields above then hold what they share
     "answer", "answered", "answer_via", "choice",         # choice: the option picked (its index), None for a typed answer
     "closed",                                             # ended with no answer: "superseded", "dismissed" (the inbox's ✕) or "resolved: <why>"
     "proposal"}                                           # autonomy.py's guess before it was asked: {choice, index, prob, tier, provider}; pings of a known kind only

Who writes it: `greenlight.py ping` / `resolve` (pings), chat.py (the menubar chat's own questions, and every answer
that comes back through it: an inbox chip, a banner reply, a typed message), and `decisions.py hook`, SKILL.md's
PostToolUse hook on AskUserQuestion (question and answer at once).

    decisions.py list [--open] [--json]    # the record, oldest first
    decisions.py calibrate [--json]        # per kind: how often autonomy.py's proposals matched your answer, by P band
    decisions.py dismiss < {"text", "key", "ping"}   # the app's ✕ on an inbox question
    decisions.py hook < hook JSON          # Claude Code's PostToolUse payload for one AskUserQuestion call

Stdlib only, and it never imports greenlight.py (whose import reads config and may call the forge).
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
FIELDS = ("workspace", "repo", "role", "step", "owner", "mr")
# what the app sends for an answer to a ping: Greenlight.swift answer() and reply(to:)
REPLY = {"inbox": '(Replying to your question: "', "notification": '(Replying to your notification: "'}
REPLY_RE = re.compile(r'\(Replying to your (?:question|notification): "(.*)"\)\n(.*)', re.S)


def file():
    return STATE / "decisions.jsonl"


def load():
    f = file()
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


@contextlib.contextmanager
def book():
    """Every record, under a lock. What the caller leaves in the list is written back."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "decisions.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a scan's ping and a chat answer can land together
        recs = load()
        yield recs
        tmp = file().with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in recs))
        os.replace(tmp, file())


def split_options(text):
    """("question", ["a", "b"]) from text ending in a line `Options: a | b`, as the app's splitOptions does."""
    lines = text.strip().split("\n")
    last = lines[-1].strip().strip("`")
    if not last.lower().startswith("options:"):
        return text.strip(), []
    return "\n".join(lines[:-1]).strip(), [o.strip() for o in last[8:].split("|") if o.strip()]


def rows():
    f = STATE / "last.json"
    try:
        return json.loads(f.read_text()).get("rows", []) if f.exists() else []
    except ValueError:
        return []


def situation(r):
    """A scan row as the situation a question was asked in."""
    mr, nxt = r.get("mr") or {}, r.get("next") or {}
    return {"workspace": r["name"], "repo": r.get("project"), "role": mr.get("role"), "step": nxt.get("step"),
            "owner": nxt.get("owner"), "mr": mr.get("ref")}


def situations(names=(), text=""):
    """The scanned workspaces a question is about: the ones named (name or id), else the ones its text names."""
    scanned = rows()
    by = {r["id"]: r for r in scanned} | {r["name"]: r for r in scanned}
    hit = [by[n] for n in names if n in by]
    if not hit and text:
        # ponytail: a name counts when it stands alone in the text; a 3-letter workspace called "fix" would over-match
        hit = [r for r in scanned if len(r["name"]) > 3 and re.search(rf"(?<![\w/.-]){re.escape(r['name'])}(?![\w/-])", text)]
    return [situation(r) for r in {r["id"]: r for r in hit}.values()]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def kind_of(kind, key, sits):
    """The sort of question, so the same question asked twice can be told apart from a new one. Given, else guessed."""
    if kind:
        return slug(kind)
    if key and key.startswith("@"):  # @no-repo:acme/x, @held-merges, @ci-health-runs-20261025
        return re.sub(r"(-[\d.]+[a-z]?)+$", "", slug(re.split(r"[:!]", key[1:])[0])) or "other"
    steps = {s["step"] for s in sits if s.get("step")}
    return steps.pop() if len(steps) == 1 else "other"


def choice(options, answer):
    low = [o.lower() for o in options]
    a = " ".join(answer.lower().split())
    return low.index(a) if a in low else None


def record(text, via, key=None, ws=(), kind=None, also=""):
    """An unanswered record for a question just asked (not yet saved). also: more text that may name its workspaces."""
    question, options = split_options(text)
    sits = situations(ws, f"{question}\n{also}")
    now = time.time()
    rec = {"id": f"{time.time_ns():x}-{os.getpid():x}", "ts": int(now), "via": via, "key": key, "kind": kind_of(kind, key, sits),
           "question": question, "options": options}
    # one workspace: its situation. Several: what they share (the repo, the step), and each one's own under "workspaces"
    rec |= {f: (v.pop() if len(v := {s[f] for s in sits}) == 1 else None) for f in FIELDS}
    if len(sits) > 1:
        rec["workspaces"] = sits
    return rec | {"answer": None, "answered": None, "answer_via": None, "choice": None, "closed": None}


def is_open(r):
    return r["answer"] is None and not r["closed"]


def asked(text, via="ping", key=None, ws=(), kind=None, chat=None, proposal=None):
    """Greenlight asked something. Returns the record's id. A newer question on the same key (or a newer chat
    question, in the same chat) supersedes the open one, as in the app's inbox; the same open ping asked again isn't recorded twice.
    chat: which chat asked (chat.py's CHAT_DIR name, a second L1 window's); None: the main one."""
    rec = record(text, via, key, ws, kind) | ({"chat": chat} if chat else {}) | ({"proposal": proposal} if proposal else {})
    with book() as recs:
        for r in recs:
            if not is_open(r) or r["via"] != via:
                continue
            if via == "ping" and r["question"] == rec["question"] and r["key"] == key and r["options"] == rec["options"]:
                r["kind"] = rec["kind"]
                if proposal:
                    r["proposal"] = proposal
                else:
                    r.pop("proposal", None)
                return r["id"]
            if (via == "ping" and ((key and r["key"] == key) or (key is None and r["key"] is None and r["question"] == rec["question"]))) or (via == "chat" and r.get("chat") == chat):
                r["closed"] = "superseded"
        recs.append(rec)
    return rec["id"]


def settle(r, text, via):
    r.update(answer=text, answered=int(time.time()), answer_via=via, choice=choice(r["options"], text), closed=None)


def heard(message, chat=None):
    """A message from the user arrived in the menubar chat. If it answers a question, write the answer into that
    question's record and return it. Otherwise (plain chat, nothing open) return None."""
    message = message.strip()
    for via, prefix in REPLY.items():
        if not message.startswith(prefix):
            continue
        with book() as recs:
            pings = [r for r in recs if r["via"] == "ping" and r["answer"] is None]
            # the question is quoted whole and may itself hold quotes and newlines: match it against what was asked
            for r in sorted(pings, key=lambda r: (is_open(r), r["ts"]), reverse=True):  # an open one first, then a closed one answered late
                head = f'{prefix}{r["question"]}")\n'
                if message.startswith(head) and message[len(head):].strip():
                    settle(r, message[len(head):].strip(), via)
                    return r
            m = REPLY_RE.fullmatch(message)  # asked before this record existed: keep the answer anyway
            if not m or not m[2].strip():
                return None
            r = record(m[1], "ping")
            settle(r, m[2].strip(), via)
            recs.append(r)
            return r
    with book() as recs:  # typing (or a chip) answers the open chat question, as in the app's submit()
        waiting = [r for r in recs if r["via"] == "chat" and is_open(r) and r.get("chat") == chat]  # this chat's: another window's Chief asked its own
        if not waiting or not message:
            return None
        settle(waiting[-1], message, "chat")
        return waiting[-1]


# the app's own turns (Greenlight.swift checkIn and watchTurn): their questions are the proactive kinds autonomy.py gates
TURNS = {"(Greenlight's idle check-in": "check-in", "(Greenlight's watch": "watch"}


def turn_kind(message):
    """The kind of question a chat turn's reply asks, from the message that started it: an idle check-in, the watch, else None."""
    head = (message or "").lstrip()[:200]
    return next((k for t, k in TURNS.items() if t in head), None)


def said(reply, chat=None, kind=None):
    """Greenlight's reply in the menubar chat: a question when it ends with an Options line (the app puts it in the inbox).
    kind: turn_kind() of the message it answered."""
    visible = re.sub(r"(\s*\[[a-z]+:[^\]\n]*\])+\s*\Z", "", reply or "").strip()
    return asked(visible, via="chat", chat=chat, kind=kind) if split_options(visible)[1] else None


def dismiss(text, key=None, ping=True, chat=None):
    """The inbox's ✕ on a question: close its open record as dismissed (autonomy.py's gate counts it against its kind)."""
    question = split_options(text)[0]
    with book() as recs:
        for r in reversed(recs):
            if (is_open(r) and r["via"] == ("ping" if ping else "chat") and r["question"] == question
                    and ((ping and r["key"] == key) or (not ping and r.get("chat") == chat))):
                r["closed"] = "dismissed"
                return r
    return None


T1, T2 = 0.95, 0.65  # autonomy.py's tiers: auto-answer at T1+, preselect at T2+


def band(p):
    return "auto" if p >= T1 else "suggest" if p >= T2 else "ask"


def calibrate(recs=None):
    """Empirical accuracy of autonomy.py's proposals, per kind: {kind: {n, right, acc, bands: {auto|suggest|ask: {n, right, acc}}}}.
    Right: you picked the proposed option. An auto answer is measured only when the user answers its re-ask."""
    recs = load() if recs is None else recs
    f = STATE / "autonomy.jsonl"
    reasked = {e.get("record"): e.get("reasked_record") for e in map(json.loads, f.read_text().splitlines() if f.exists() else [])
               if e.get("undone")}
    by_id = {r["id"]: r for r in recs}
    out = {}
    for r in recs:
        prop = r.get("proposal")
        if not prop or r["answer"] is None:
            continue
        verdict = by_id.get(reasked.get(r["id"])) if r["answer_via"] == "auto" else r
        if verdict is None or verdict["answer"] is None:
            continue
        right = verdict["choice"] == prop["index"]
        k = out.setdefault(r["kind"], {"n": 0, "right": 0, "bands": {b: {"n": 0, "right": 0} for b in ("auto", "suggest", "ask")}})
        for s in (k, k["bands"][band(prop["prob"])]):
            s["n"] += 1
            s["right"] += right
    for k in out.values():
        for s in [k, *k["bands"].values()]:
            s["acc"] = round(s["right"] / s["n"], 3) if s["n"] else None
    return out


def resolve(key, why):
    """The question on this key no longer applies (greenlight.py resolve): close it unanswered."""
    with book() as recs:
        for r in recs:
            if is_open(r) and r["key"] == key:
                r["closed"] = f"resolved: {why}"


def forget_chat(chat=None):
    """The chat was cleared (`chat.py new`): its open questions went with it."""
    with book() as recs:
        for r in recs:
            if is_open(r) and r["via"] == "chat" and r.get("chat") == chat:
                r["closed"] = "resolved: chat cleared"


def quiet(fn, *args, **kw):
    """Call one of the writers above without ever failing the caller: the ping still goes out, the chat turn still runs."""
    try:
        return globals()[fn](*args, **kw)
    except Exception as e:
        print(f"decision record: {e}", file=sys.stderr)


def hook(payload):
    """Claude Code's PostToolUse payload for AskUserQuestion: one complete record per question.
    The answers come as {question text: answer} in tool_response (what Claude Code's transcripts hold), else in
    tool_input; the plain-text form (`"question"="answer"`) is read too."""
    if payload.get("tool_name") != "AskUserQuestion":
        return []
    inp, res = payload.get("tool_input") or {}, payload.get("tool_response")
    answers = (res.get("answers") if isinstance(res, dict) else None) or inp.get("answers") or {}
    flat = res if isinstance(res, str) else ""
    out = []
    for i, q in enumerate(inp.get("questions") or []):
        labels = [o.get("label", "") for o in q.get("options") or []]
        ans = answers.get(q["question"])
        if ans is None and (m := re.search(re.escape(f'"{q["question"]}"="') + r'(.*?)"(?=[,.]|\s*$)', flat, re.S)):
            ans = m[1]
        # a multi-select names its workspaces in the options ("which of these runs can I delete?")
        r = record(q["question"], "ask", kind=q.get("header"), also="\n".join(labels))
        r["id"] += f"-{i}"
        r["options"] = labels
        if ans is not None:
            ans = ans if isinstance(ans, str) else ", ".join(map(str, ans))
            settle(r, ans, "ask")
            if q.get("multiSelect"):  # every option the answer holds
                r["choice"] = [n for n, l in enumerate(labels) if l and l.lower() in ans.lower()]
        out.append(r)
    if out:
        with book() as recs:
            recs += out
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "hook":
        try:  # a hook must never get in the session's way: silent, exit 0, whatever arrives
            hook(json.loads(sys.stdin.read() or "{}"))
        except Exception:
            pass
        return
    if cmd == "dismiss":
        try:  # the app's ✕: never in the way
            d = json.loads(sys.stdin.read() or "{}")
            dismiss(d["text"], d.get("key"), d.get("ping", True), d.get("chat") or None)
        except Exception as e:
            print(f"decision record: {e}", file=sys.stderr)
        return
    if cmd == "calibrate":
        cal = calibrate()
        if "--json" in sys.argv:
            return print(json.dumps(cal, indent=1))
        for k, v in sorted(cal.items()):
            bands = "  ".join(f"{b} {x['right']}/{x['n']}" for b, x in v["bands"].items() if x["n"])
            print(f"{k:18} {v['right']}/{v['n']} right ({v['acc']:.0%})   {bands}")
        return
    recs = [r for r in load() if "--open" not in sys.argv or is_open(r)]
    if "--json" in sys.argv:
        print(json.dumps(recs, indent=1))
        return
    for r in recs:
        about = r["workspace"] or (f"{len(r['workspaces'])} workspaces" if r.get("workspaces") else "-")
        end = r["answer"] if r["answer"] is not None else f"({r['closed'] or 'open'})"
        print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['ts']))}  {r['via']:4} {r['kind'][:18]:18} {about[:28]:28} "
              f"{' '.join(r['question'].split())[:70]} → {end[:60]}")


if __name__ == "__main__":
    main()
