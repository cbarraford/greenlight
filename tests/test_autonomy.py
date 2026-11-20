"""python3 test_autonomy.py: confidence-gated autonomy (autonomy.py): proposals from past answers and system one, the three
tiers, calibration, graduation, undo, and the anti-slop gate on proactive pings."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import contextlib, io, json, subprocess, time, types
from pathlib import Path
import autonomy, decisions, greenlight

HOME = Path(os.environ["GREENLIGHT_HOME"])
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "autonomy.py"
autonomy.decide = None  # no system one unless a test puts one in
autonomy.MEMORY = HOME / "MEMORY.md"  # never the checkout's own memory/


def reset():
    for f in ("decisions.jsonl", "autonomy.jsonl", "autonomy.json", "outbox.jsonl", "pinged.json"):
        (HOME / f).unlink(missing_ok=True)


def past(kind, answer, options, n, ago=3600, via="ping"):
    """n answered records of this kind, as the app would have left them."""
    now = time.time()
    with decisions.book() as recs:
        for i in range(n):
            r = decisions.record(f"{kind} question {i}?\nOptions: {' | '.join(options)}", via, kind=kind)
            r["ts"] = int(now - ago)
            r["id"] += f"-{kind}-{i}-{len(recs)}"
            decisions.settle(r, answer, "inbox")
            recs.append(r)


def outbox():
    f = HOME / "outbox.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def cli(*args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=os.environ | {"PYTHONPATH": ""})


KD = "\nOptions: mark ready | keep draft"

# risk: only inert answers are low; actions need confirmation; human gates are high
assert [autonomy.risk(o) for o in ("keep draft", "not yet", "keep holding")] == ["low"] * 3
assert [autonomy.risk(o) for o in ("mark ready", "set 3.22", "nudge it", "keep the branch and delete workspace")] == ["medium"] * 4
assert autonomy.risk("keep draft and merge") == "high"
assert [autonomy.risk(o) for o in ("merge", "merge anyway", "deployed, sign off all", "approve")] == ["high"] * 4
assert [autonomy.risk(o) for o in ("delete", "delete all 3", "close it")] == ["medium"] * 3

# an unknown kind, or no options: no proposal
reset()
assert autonomy.propose("review-request", "x?" + KD) is None and autonomy.propose("keep-draft", "no options") is None

# no history, no model: a coin flip, so it's asked (tier 3)
p = autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)
assert (p["tier"], p["prob"], p["count"], p["provider"]) == (3, 0.5, 0, "history"), p

# one past answer: likely, preselected (tier 2) with the count the badge shows
past("keep-draft", "keep draft", ["mark ready", "keep draft"], 1)
p = autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)
assert (p["tier"], p["choice"], p["index"], p["count"], p["agree"]) == (2, "keep draft", 1, 1, 1), p

# 20 the same way: 95%+ sure, but the kind isn't opted in, so still only preselected
past("keep-draft", "keep draft", ["mark ready", "keep draft"], 19)
p = autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)
assert p["prob"] >= 0.95 and p["tier"] == 2 and "opted in" in p["why"], p
# opted in: answered for you (tier 1)
autonomy.save_settings(autonomy.settings() | {"auto": ["keep-draft"]})
assert autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)["tier"] == 1
# a past option that matches by its first word counts ("delete" ~ "delete all 3"); a typed answer counts only in the total
past("delete-workspace", "delete", ["delete", "keep"], 3)
p = autonomy.propose("delete-workspace", "3 finished. Delete them?\nOptions: delete all 3 | keep")
assert (p["choice"], p["agree"], p["count"]) == ("delete all 3", 3, 3), p

# a human gate never goes past tier 3, however unanimous: signoff's "deployed", merge-held's "merge"
past("signoff", "deployed", ["deployed", "not yet"], 30)
past("merge-held", "merge", ["merge", "hold"], 30)
autonomy.save_settings(autonomy.settings() | {"auto": ["keep-draft", "signoff", "merge-held"]})  # even if the file says so
for kind, opts in (("signoff", "deployed | not yet"), ("merge-held", "merge | hold")):
    p = autonomy.propose(kind, f"x?\nOptions: {opts}")
    assert p["tier"] == 3 and p["risk"] == "high" and p["prob"] > 0.95, p
# ...and a delete is never answered for you, only preselected
past("delete-workspace", "delete", ["delete", "keep"], 30)
autonomy.save_settings(autonomy.settings() | {"auto": ["delete-workspace"]})
p = autonomy.propose("delete-workspace", "done. Delete it?\nOptions: delete | keep")
assert (p["tier"], p["risk"]) == (2, "medium"), p
assert autonomy.propose("keep-draft", "Ready?\nOptions: mark ready | keep draft and merge")["tier"] == 3
# signoff and merge-held can't be opted in at all
autonomy.save_settings({"auto": [], "gate": 0.35})
assert cli("set", "--auto", "signoff", "on").returncode != 0 and autonomy.settings()["auto"] == []
assert cli("set", "--auto", "keep-draft", "on").returncode == 0 and autonomy.settings()["auto"] == ["keep-draft"]
assert cli("set", "--auto", "merge-held", "on").returncode != 0
assert [k["eligible"] for k in autonomy.report()["kinds"] if k["kind"] in autonomy.GATES] == [False, False]


# system one: its pick counts as MODEL_WEIGHT answers; the context it gets holds the past answers and MEMORY.md
class Fake(types.SimpleNamespace):
    def choice(self, state, question, options, *, context=""):
        self.seen = context
        if self.boom:
            raise RuntimeError("network down")
        return {"choice": options[self.i], "index": self.i, "probs": {o: (self.p if j == self.i else 1 - self.p) for j, o in enumerate(options)},
                "confidence": self.p, "provider": self.provider}

    def score(self, state, rubric, *, context=""):
        return {"score": self.s, "provider": self.provider}


reset()
autonomy.MEMORY.write_text("- [Milestones](m.md) — milestone: always the next release train, never a hotfix one\n")
past("milestone", "3.23", ["3.23", "hotfix"], 2)
autonomy.decide = Fake(i=0, p=0.99, provider="jev", boom=False, s=1.0)
p = autonomy.propose("milestone", "fix-1 has no milestone.\nOptions: 3.23 | hotfix")
assert p["provider"] == "jev" and p["choice"] == "3.23" and abs(p["prob"] - (4 * 0.99 + 3) / 8) < 1e-3, p
assert "3.23" in autonomy.decide.seen and "release train" in autonomy.decide.seen, autonomy.decide.seen
# the unconfigured local fallback is a guess by design: ignored. A failing decide: history alone
for fake in (Fake(i=1, p=0.99, provider="local", boom=False, s=1.0), Fake(i=0, p=0.99, provider="jev", boom=True, s=1.0)):
    autonomy.decide = fake
    with contextlib.redirect_stderr(io.StringIO()):
        p = autonomy.propose("milestone", "fix-1 has no milestone.\nOptions: 3.23 | hotfix")
    assert (p["provider"], p["choice"], p["prob"]) == ("history", "3.23", 0.75), p
autonomy.decide = None


# greenlight.py ping: tier 1 answers, logs and tells the app; tier 2 tells the app what to preselect; tier 3 is a plain ping
reset()
past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
out = io.StringIO()
with contextlib.redirect_stdout(out):
    res = greenlight.ping("feat-9 (!9): still a draft. Mark it ready?" + KD, "feat-9", kind="keep-draft")
assert res["auto"] == "keep draft" and out.getvalue().startswith("auto: keep draft") and f"autonomy.py undo {res['id']}" in out.getvalue(), out.getvalue()
[ev] = outbox()
assert ev["auto"]["id"] == res["id"] and ev["auto"]["choice"] == "keep draft" and ev["key"] == "feat-9" and ev["text"].endswith(KD), ev
rec = decisions.load()[-1]
assert (rec["answer"], rec["answer_via"], rec["choice"], rec["proposal"]["tier"]) == ("keep draft", "auto", 1, 1), rec
[a] = autonomy.audit_log()
assert (a["act"], a["record"], a["choice"], a["kind"]) == ("auto", rec["id"], "keep draft", "keep-draft"), a
assert "feat-9" not in greenlight.pinged()  # nothing waits on the user
# its own answer isn't evidence: the tally is the 25 human answers
assert autonomy.tally("keep-draft", ["mark ready", "keep draft"], decisions.load()) == ([0, 25], 25)
# an unchallenged auto answer isn't a measured success
assert "keep-draft" not in decisions.calibrate()
# repeated scans do not add auto answers, records, or notifications for the same question
with contextlib.redirect_stdout(io.StringIO()):
    assert greenlight.ping("feat-9 (!9): still a draft. Mark it ready?" + KD, "feat-9", kind="keep-draft") is None
assert len(outbox()) == 1 and len(autonomy.audit_log()) == 1 and len(decisions.load()) == 26

# undo: marked undone, counted a miss, and the question goes to the user again (no auto, no preselect)
original_ping = greenlight.ping
greenlight.ping = lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox unavailable"))
try:
    try:
        autonomy.undo(res["id"])
        assert False, "undo should surface a failed re-ask"
    except OSError:
        pass
finally:
    greenlight.ping = original_ping
assert not autonomy.audit_log()[0].get("undone")  # retry must still be possible
with contextlib.redirect_stdout(io.StringIO()):
    autonomy.undo(res["id"])
assert autonomy.audit_log()[0]["undone"]
assert decisions.calibrate()["keep-draft"]["bands"]["auto"] == {"n": 1, "right": 0, "acc": 0.0}
again = outbox()[-1]
assert "auto" not in again and "suggest" not in again and again["text"].startswith("Undo: greenlight answered 'keep draft'") and again["text"].endswith(KD), again
assert decisions.load()[-1]["answer"] is None and "feat-9" in greenlight.pinged()
assert autonomy.tally("keep-draft", ["mark ready", "keep draft"], decisions.load())[0] == [0, 25]  # Undo cannot change human history
assert cli("undo", "nope").returncode != 0
# a grouped (@) ping answered for you asks again on undo, though its key was pinged within the flap window
assert autonomy.propose("keep-draft", "x?" + KD)["tier"] == 1  # one correction is not enough to cap the band
past("keep-draft", "keep draft", ["mark ready", "keep draft"], 20)
with contextlib.redirect_stdout(io.StringIO()):
    res = greenlight.ping("4 drafts still drafts. Ready?" + KD, "@drafts-2", kind="keep-draft")
    assert res and res.get("auto"), res
    autonomy.undo(res["id"])
assert outbox()[-1]["key"] == "@drafts-2" and outbox()[-1]["text"].startswith("Undo:"), outbox()[-1]

# tier 2: preselected in the app, with the numbers for its badge
reset()
past("agent-stuck", "nudge it", ["nudge it", "stop it"], 3)
greenlight.ping("fix-2: agent idle 3h.\nOptions: nudge it | stop it", "fix-2", kind="agent-stuck")
ev = outbox()[-1]
assert ev["suggest"] == {"choice": "nudge it", "index": 0, "prob": 0.8, "count": 3, "agree": 3}, ev
assert decisions.load()[-1]["proposal"]["tier"] == 2 and decisions.load()[-1]["answer"] is None
# a kind it doesn't know, or none given: as before
greenlight.ping("mr-7: review?\nOptions: agent review | skip", "mr-7", kind="review-request")
assert "suggest" not in outbox()[-1] and "proposal" not in decisions.load()[-1]

# calibration by band, and a band that measured below its floor caps P (and re-tiers it)
reset()
autonomy.save_settings({"auto": ["milestone"], "gate": 0.35})
past("milestone", "3.23", ["3.23", "later"], 100)
with decisions.book() as recs:
    for i in range(6):  # six 0.97-sure milestone proposals, three of them wrong
        r = decisions.record(f"old milestone {i}?\nOptions: 3.23 | later", "ping", kind="milestone")
        r["id"] += f"-m{i}"
        r["proposal"] = {"choice": "3.23", "index": 0, "prob": 0.97, "tier": 1, "provider": "history"}
        decisions.settle(r, "3.23" if i % 2 else "later", "inbox")
        recs.append(r)
cal = decisions.calibrate()["milestone"]
assert (cal["n"], cal["right"], cal["bands"]["auto"]["acc"]) == (6, 3, 0.5), cal
p = autonomy.propose("milestone", "fix-3 has no milestone.\nOptions: 3.23 | later")
assert p["tier"] == 3 and p["prob"] == 0.5 and "capped" in p["why"], p
r = cli("calibrate")
assert r.returncode == 0 and "milestone" in r.stdout and "auto 3/6" in r.stdout, r
assert "milestone" in subprocess.run([sys.executable, str(SCRIPT.parent / "decisions.py"), "calibrate"], capture_output=True, text=True).stdout

# graduation: ten sure proposals, all right, for a kind not opted in
reset()
with decisions.book() as recs:
    for i in range(10):
        r = decisions.record(f"keep it a draft {i}?" + KD, "ping", kind="keep-draft")
        r["id"] += f"-g{i}"
        r["proposal"] = {"choice": "keep draft", "index": 1, "prob": 0.96, "tier": 2, "provider": "jev"}
        decisions.settle(r, "keep draft", "inbox")
        recs.append(r)
assert [c["kind"] for c in autonomy.candidates()] == ["keep-draft"]
autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
assert autonomy.candidates() == []
s = json.loads(cli("settings", "--json").stdout)
assert s["auto"] == ["keep-draft"] and [k for k in s["kinds"] if k["kind"] == "keep-draft"][0]["acc"] == 1.0, s

# the anti-slop gate: no history passes; dismissed check-ins hold the next; they fade; acting on them lifts it
reset()
now = time.time()
assert autonomy.gate("check-in", now=now)["ok"] and autonomy.gate("check-in", now=now)["score"] == 0.5
with decisions.book() as recs:
    for i in range(3):
        r = decisions.record(f"Work on fix-{i} next?\nOptions: fix-{i} | later", "chat", kind="check-in")
        r["id"] += f"-c{i}"
        r["closed"] = "dismissed" if i else "superseded"
        recs.append(r)
g = autonomy.gate("check-in", now=now)
assert not g["ok"] and g["dismissed"] > 2.9 and g["score"] == 0.2, g
assert autonomy.gate("check-in", now=now + 28 * 86400)["ok"]  # four weeks on, three dismissals weigh 0.75
assert autonomy.gate("watch", now=now)["ok"]  # another kind's history isn't this one's
past("check-in", "fix-1", ["fix-1", "later"], 3, ago=0, via="chat")
assert autonomy.gate("check-in", now=now)["ok"]
# one still waiting halves it; an unanswered one a day old counts as ignored
with decisions.book() as recs:
    r = decisions.record("Work on fix-9 next?\nOptions: fix-9 | later", "chat", kind="watch")
    r["id"] += "-w"
    recs.append(r)
assert autonomy.gate("watch", now=now)["waiting"] == 1 and autonomy.gate("watch", now=now)["score"] == 0.25
g = autonomy.gate("watch", now=now + 2 * 86400)
assert (g["waiting"], g["dismissed"] > 0.9) == (0, True), g
# the model's read of the text counts half, when there's text and a real provider
autonomy.decide = Fake(i=0, p=0.5, provider="haiku", boom=False, s=0.0)
assert autonomy.gate("watch", "status recap, nothing new", now=now)["score"] == 0.125
autonomy.decide = Fake(i=0, p=0.5, provider="local", boom=False, s=0.0)
assert autonomy.gate("watch", "status recap", now=now)["score"] == 0.25
autonomy.decide = None
# the gate's threshold is the user's: 0 lets everything through
autonomy.save_settings({"auto": [], "gate": 0.0})
assert autonomy.gate("check-in", now=now)["ok"]
autonomy.save_settings({"auto": [], "gate": 0.35})
r = cli("gate", "--kind", "watch")
assert r.returncode == 1 and json.loads(r.stdout)["ok"] is False, r

# greenlight.py ping --proactive (or a check-in/watch kind): held when the gate says so, nothing sent, logged
reset()
with decisions.book() as recs:
    for i in range(4):
        r = decisions.record(f"idea {i}\nOptions: do it | no", "ping", kind="check-in")
        r["id"] += f"-p{i}"
        r["closed"] = "dismissed"
        recs.append(r)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    assert greenlight.ping("You could tidy fix-1's commits.\nOptions: do it | no", "@tidy", kind="check-in")["held"]
assert out.getvalue().startswith("held: check-in") and outbox() == [] and len(decisions.load()) == 4
assert autonomy.audit_log()[-1]["act"] == "held"
r = subprocess.run([sys.executable, str(SCRIPT.parent / "greenlight.py"), "ping", "--proactive", "--kind", "watch", "--key", "@idea",
                    "Maybe look at fix-2?\nOptions: ok | no"], capture_output=True, text=True)
assert r.returncode == 0 and len(outbox()) == 1, (r, outbox())  # watch has no history: it goes out
# a lifecycle question is never gated, however often its kind was dismissed
with decisions.book() as recs:
    for i in range(5):
        r = decisions.record(f"merge {i}?\nOptions: merge | hold", "ping", kind="merge-held")
        r["id"] += f"-h{i}"
        r["closed"] = "dismissed"
        recs.append(r)
greenlight.ping("fix-1: held. Merge?\nOptions: merge | hold", "fix-1", kind="merge-held")
assert outbox()[-1]["text"].startswith("fix-1: held")

# the app's ✕ closes the record as dismissed; the check-in and watch turns tag their questions
reset()
greenlight.ping("fix-4: draft. Ready?" + KD, "fix-4", kind="keep-draft")
r = subprocess.run([sys.executable, str(SCRIPT.parent / "decisions.py"), "dismiss"], input=json.dumps({"text": "fix-4: draft. Ready?", "key": "fix-4", "ping": True}),
                   capture_output=True, text=True)
assert r.returncode == 0 and decisions.load()[-1]["closed"] == "dismissed" and decisions.load()[-1]["answer"] is None, (r, decisions.load())
assert decisions.turn_kind("(Greenlight's idle check-in, not typed by the user: …)") == "check-in"
assert decisions.turn_kind("(Greenlight's watch, not typed by the user: these…)") == "watch"
assert decisions.turn_kind("what's next?") is None
decisions.said("Work on fix-5 next.\nOptions: fix-5 | later", kind="check-in")
assert decisions.load()[-1]["kind"] == "check-in" and decisions.load()[-1]["via"] == "chat"
assert decisions.dismiss("Work on fix-5 next.", ping=False)["closed"] == "dismissed"
assert decisions.dismiss("nothing like it", "k") is None

print("ok")
