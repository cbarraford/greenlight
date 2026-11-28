"""Tests for scripts/autonomy.py: confidence-gated autonomy, tiers, calibration, graduation, undo, and anti-slop gate."""
import concurrent.futures
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import autonomy
import decisions
import greenlight
from conftest import SCRIPTS

SCRIPT = SCRIPTS / "autonomy.py"
KD = "\nOptions: mark ready | keep draft"


class FakeDecide(types.SimpleNamespace):
    def choice(self, state, question, options, *, context=""):
        assert isinstance(state, str), f"decide.choice state must be JSON-serializable str, got {type(state)}"
        self.seen = state
        if self.boom:
            raise RuntimeError("network down")
        return {
            "choice": options[self.i],
            "index": self.i,
            "probs": {o: (self.p if j == self.i else 1 - self.p) for j, o in enumerate(options)},
            "confidence": self.p,
            "provider": self.provider,
        }

    def score(self, state, rubric, *, context=""):
        assert isinstance(state, str), f"decide.score state must be JSON-serializable str, got {type(state)}"
        self.scored_text = state
        self.scored_rubric = rubric
        return {"score": self.s, "provider": self.provider}


@pytest.fixture()
def autonomy_env(gl_home, monkeypatch):
    monkeypatch.setattr(autonomy, "decide", None)
    monkeypatch.setattr(autonomy, "MEMORY", gl_home / "MEMORY.md")
    return gl_home


def _reset(home: Path):
    for f in ("decisions.jsonl", "autonomy.jsonl", "autonomy.json", "outbox.jsonl", "pinged.json"):
        (home / f).unlink(missing_ok=True)


def _past(kind, answer, options, n, ago=3600, via="ping"):
    """Record n answered decisions of this kind, as the app would have left them."""
    now = time.time()
    with decisions.book() as recs:
        for i in range(n):
            r = decisions.record(f"{kind} question {i}?\nOptions: {' | '.join(options)}", via, kind=kind)
            r["ts"] = int(now - ago)
            r["id"] += f"-{kind}-{i}-{len(recs)}"
            decisions.settle(r, answer, "inbox")
            recs.append(r)


def _outbox(home: Path):
    f = home / "outbox.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def _cli(*args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=dict(os.environ))


def test_risk_classification():
    # risk: only inert answers are low; actions need confirmation; human gates are high
    assert [autonomy.risk(o) for o in ("keep draft", "keep workspace")] == ["low"] * 2
    assert [
        autonomy.risk(o)
        for o in ("no", "not yet", "keep holding", "mark ready", "set 3.22", "nudge it", "keep the branch and delete workspace")
    ] == ["medium"] * 7
    assert autonomy.risk("keep draft and merge") == "high"
    assert [autonomy.risk(o) for o in ("merge", "merge anyway", "deployed, sign off all", "approve")] == ["high"] * 4
    assert [autonomy.risk(o) for o in ("delete", "delete all 3", "close it")] == ["medium"] * 3


def test_propose_history_tiers_and_human_gates(autonomy_env):
    # an unknown kind, or no options: no proposal
    assert autonomy.propose("review-request", "x?" + KD) is None
    assert autonomy.propose("keep-draft", "no options") is None

    # no history, no model: a coin flip, so it's asked (tier 3)
    p = autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)
    assert (p["tier"], p["prob"], p["count"], p["provider"]) == (3, 0.5, 0, "history"), p

    # one past answer: likely, preselected (tier 2) with the count the badge shows
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 1)
    p = autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)
    assert (p["tier"], p["choice"], p["index"], p["count"], p["agree"]) == (2, "keep draft", 1, 1, 1), p

    # 20 the same way: 95%+ sure, but the kind isn't opted in, so still only preselected
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 19)
    p = autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)
    assert p["prob"] >= 0.95 and p["tier"] == 2 and "opted in" in p["why"], p

    # opted in: answered for you (tier 1)
    autonomy.save_settings(autonomy.settings() | {"auto": ["keep-draft"]})
    assert autonomy.propose("keep-draft", "feat-9 is a draft. Ready?" + KD)["tier"] == 1

    # a past option that matches by its first word counts ("delete" ~ "delete all 3")
    _past("delete-workspace", "delete", ["delete", "keep"], 3)
    p = autonomy.propose("delete-workspace", "3 finished. Delete them?\nOptions: delete all 3 | keep")
    assert (p["choice"], p["agree"], p["count"]) == ("delete all 3", 3, 3), p

    # a human gate never goes past tier 3, however unanimous: signoff's "deployed", merge-held's "merge"
    _past("signoff", "deployed", ["deployed", "not yet"], 30)
    _past("merge-held", "merge", ["merge", "hold"], 30)
    autonomy.save_settings(autonomy.settings() | {"auto": ["keep-draft", "signoff", "merge-held"]})
    for kind, opts in (("signoff", "deployed | not yet"), ("merge-held", "merge | hold")):
        p = autonomy.propose(kind, f"x?\nOptions: {opts}")
        assert p["tier"] == 3 and p["risk"] == "high" and p["prob"] > 0.95, p

    # Even generic labels cannot preselect a human-gated action.
    _past("merge-held", "Yes", ["Yes", "Later"], 30)
    assert autonomy.propose("merge-held", "Merge this held PR?\nOptions: Yes | Later")["tier"] == 3

    # ...and a delete is never answered for you, only preselected
    _past("delete-workspace", "delete", ["delete", "keep"], 30)
    autonomy.save_settings(autonomy.settings() | {"auto": ["delete-workspace"]})
    p = autonomy.propose("delete-workspace", "done. Delete it?\nOptions: delete | keep")
    assert (p["tier"], p["risk"]) == (2, "medium"), p
    assert autonomy.propose("keep-draft", "Ready?\nOptions: mark ready | keep draft and merge")["tier"] == 3

    # signoff and merge-held can't be opted in at all
    autonomy.save_settings({"auto": [], "gate": 0.35})
    assert _cli("set", "--auto", "signoff", "on").returncode != 0 and autonomy.settings()["auto"] == []
    assert _cli("set", "--auto", "keep-draft", "on").returncode == 0 and autonomy.settings()["auto"] == ["keep-draft"]
    assert _cli("set", "--auto", "merge-held", "on").returncode != 0
    assert [k["eligible"] for k in autonomy.report()["kinds"] if k["kind"] in autonomy.GATES] == [False, False]


def test_propose_with_system_one_and_fallbacks(autonomy_env, monkeypatch):
    autonomy.MEMORY.write_text("- [Milestones](m.md) — milestone: always the next release train, never a hotfix one\n")
    _past("milestone", "3.23", ["3.23", "hotfix"], 2)
    fake = FakeDecide(i=0, p=0.99, provider="jev", boom=False, s=1.0)
    monkeypatch.setattr(autonomy, "decide", fake)
    p = autonomy.propose("milestone", "fix-1 has no milestone.\nOptions: 3.23 | hotfix")
    assert p["provider"] == "jev" and p["choice"] == "3.23" and abs(p["prob"] - (4 * 0.99 + 3) / 8) < 1e-3, p
    assert "3.23" in fake.seen and "release train" in fake.seen, fake.seen

    # the unconfigured local fallback is a guess by design: ignored. A failing decide: history alone
    for bad_fake in (FakeDecide(i=1, p=0.99, provider="local", boom=False, s=1.0), FakeDecide(i=0, p=0.99, provider="jev", boom=True, s=1.0)):
        monkeypatch.setattr(autonomy, "decide", bad_fake)
        with contextlib.redirect_stderr(io.StringIO()):
            p = autonomy.propose("milestone", "fix-1 has no milestone.\nOptions: 3.23 | hotfix")
        assert (p["provider"], p["choice"], p["prob"]) == ("history", "3.23", 0.75), p


def test_ping_tier1_auto_and_undo_atomicity(autonomy_env, monkeypatch):
    home = autonomy_env
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
    autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = greenlight.ping("feat-9 (!9): still a draft. Mark it ready?" + KD, "feat-9", kind="keep-draft")
    assert res["auto"] == "keep draft" and out.getvalue().startswith("auto: keep draft") and f"autonomy.py undo {res['id']}" in out.getvalue(), out.getvalue()
    [ev] = _outbox(home)
    assert ev["auto"]["id"] == res["id"] and ev["auto"]["choice"] == "keep draft" and ev["key"] == "feat-9" and ev["text"].endswith(KD), ev
    rec = decisions.load()[-1]
    assert (rec["answer"], rec["answer_via"], rec["choice"], rec["proposal"]["tier"]) == ("keep draft", "auto", 1, 1), rec
    [a] = autonomy.audit_log()
    assert (a["act"], a["record"], a["choice"], a["kind"]) == ("auto", rec["id"], "keep draft", "keep-draft"), a
    assert "feat-9" not in greenlight.pinged()
    assert autonomy.tally("keep-draft", ["mark ready", "keep draft"], decisions.load()) == ([0, 25], 25)
    assert "keep-draft" not in decisions.calibrate()

    # repeated scans do not add auto answers, records, or notifications for the same question
    with contextlib.redirect_stdout(io.StringIO()):
        assert greenlight.ping("feat-9 (!9): still a draft. Mark it ready?" + KD, "feat-9", kind="keep-draft") is None
    assert len(_outbox(home)) == 1 and len(autonomy.audit_log()) == 1 and len(decisions.load()) == 26

    # undo: marked undone, counted a miss, and the question goes to the user again (no auto, no preselect)
    original_ping = greenlight.ping
    with monkeypatch.context() as mp:
        mp.setattr(greenlight, "ping", lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox unavailable")))
        with pytest.raises(OSError, match="outbox unavailable"):
            autonomy.undo(res["id"])
    assert not autonomy.audit_log()[0].get("undone")

    before = len(_outbox(home))
    with monkeypatch.context() as mp:
        mp.setattr(decisions, "asked", lambda *a, **kw: (_ for _ in ()).throw(OSError("decision record unavailable")))
        with pytest.raises(OSError, match="decision record unavailable"):
            autonomy.undo(res["id"])
    assert len(_outbox(home)) == before and not autonomy.audit_log()[0].get("undone")

    def ping_with_concurrent_audit(*args, **kw):
        result = original_ping(*args, **kw)
        autonomy.audit({"act": "held", "kind": "watch", "text": "another process wrote this while Undo reasked"})
        return result

    with monkeypatch.context() as mp, contextlib.redirect_stdout(io.StringIO()):
        mp.setattr(greenlight, "ping", ping_with_concurrent_audit)
        autonomy.undo(res["id"])
    assert autonomy.audit_log()[0]["undone"]
    assert autonomy.audit_log()[-1]["act"] == "held"  # Undo preserved a concurrent audit append
    assert "keep-draft" not in decisions.calibrate()
    again = _outbox(home)[-1]
    assert "auto" not in again and "suggest" not in again and again["text"].startswith("Undo: greenlight answered 'keep draft'") and again["text"].endswith(KD), again
    assert decisions.load()[-1]["answer"] is None and f"@undo:{res['id']}" in greenlight.pinged()
    assert autonomy.tally("keep-draft", ["mark ready", "keep draft"], decisions.load())[0] == [0, 25]

    question = decisions.split_options(again["text"])[0]
    decisions.heard(f'(Replying to your question: "{question}")\nkeep draft')
    assert decisions.calibrate()["keep-draft"]["bands"]["auto"] == {"n": 1, "right": 1, "acc": 1.0}
    assert _cli("undo", "nope").returncode != 0

    # a grouped (@) ping answered for you asks again on undo, though its key was pinged within the flap window
    assert autonomy.propose("keep-draft", "x?" + KD)["tier"] == 1
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 20)
    with contextlib.redirect_stdout(io.StringIO()):
        res = greenlight.ping("4 drafts still drafts. Ready?" + KD, "@drafts-2", kind="keep-draft")
        assert res and res.get("auto"), res
        autonomy.undo(res["id"])
    assert _outbox(home)[-1]["key"] == f"@undo:{res['id']}" and _outbox(home)[-1]["text"].startswith("Undo:"), _outbox(home)[-1]

    # Re-asking an old auto answer must not supersede a newer question about that workspace.
    _reset(home)
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
    autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
    with contextlib.redirect_stdout(io.StringIO()):
        old_auto = greenlight.ping("Original draft question?" + KD, "feat-9", kind="keep-draft")
        greenlight.ping("A newer review question?\nOptions: review | later", "feat-9", kind="review-request")
        autonomy.undo(old_auto["id"])
    assert any(r["question"] == "A newer review question?" and decisions.is_open(r) for r in decisions.load())
    assert _outbox(home)[-1]["key"] == f"@undo:{old_auto['id']}"
    greenlight.close_stale([])
    assert f"@undo:{old_auto['id']}" not in greenlight.pinged()
    assert any(r["key"] == f"@undo:{old_auto['id']}" and r["closed"] == "resolved: workspaces no longer waiting on you" for r in decisions.load())


def test_failed_writes_race_and_commit_rollback(autonomy_env, monkeypatch):
    home = autonomy_env
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
    autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})

    with monkeypatch.context() as mp, contextlib.redirect_stdout(io.StringIO()):
        mp.setattr(decisions, "asked", lambda *a, **kw: (_ for _ in ()).throw(OSError("decision record unavailable")))
        with pytest.raises(OSError, match="decision record unavailable"):
            greenlight.ping("write failed?" + KD, "@write-failed", kind="keep-draft")
    assert not _outbox(home) and not autonomy.audit_log()

    _reset(home)
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
    autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
    with monkeypatch.context() as mp, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        mp.setattr(autonomy, "audit", lambda *a, **kw: (_ for _ in ()).throw(OSError("audit unavailable")))
        greenlight.ping("audit failed?" + KD, "@audit-failed", kind="keep-draft")
    assert "auto" not in _outbox(home)[-1] and decisions.load()[-1]["answer"] is None

    _reset(home)
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
    autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
    with monkeypatch.context() as mp:
        mp.setattr(greenlight, "outbox", lambda *a, **kw: (_ for _ in ()).throw(OSError("outbox unavailable")))
        with pytest.raises(OSError, match="outbox unavailable"):
            greenlight.ping("outbox failed?" + KD, "@outbox-failed", kind="keep-draft")
    saved = autonomy.audit_log()[0]
    assert decisions.load()[-1]["answer_via"] == "auto" and _outbox(home) == []
    with contextlib.redirect_stdout(io.StringIO()):
        recovered = greenlight.ping("outbox failed?" + KD, "@outbox-failed", kind="keep-draft")
    assert recovered["id"] == saved["id"] and len(autonomy.audit_log()) == 1 and len(_outbox(home)) == 1

    # Two scan processes racing for one question still make one auto decision.
    _reset(home)
    _past("keep-draft", "keep draft", ["mark ready", "keep draft"], 25)
    autonomy.save_settings({"auto": ["keep-draft"], "gate": 0.35})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, contextlib.redirect_stdout(io.StringIO()):
        results = list(pool.map(lambda _: greenlight.ping("race?" + KD, "@race", kind="keep-draft"), range(2)))
    assert sum(bool(r and r.get("auto")) for r in results) == 1 and len(autonomy.audit_log()) == 1 and len(_outbox(home)) == 1

    # A decision commit that fails after audit creation leaves no reported auto answer.
    _reset(home)
    rid = decisions.asked("Failed commit?" + KD, kind="keep-draft")
    original_book = decisions.book

    @contextlib.contextmanager
    def fail_commit():
        with original_book() as recs:
            yield recs
            raise OSError("decision commit unavailable")

    with monkeypatch.context() as mp:
        mp.setattr(decisions, "book", fail_commit)
        with pytest.raises(OSError, match="decision commit unavailable"):
            autonomy.act(rid, {"kind": "keep-draft", "choice": "keep draft", "prob": 0.99, "provider": "history"}, "Failed commit?" + KD)
    assert decisions.load()[-1]["answer"] is None and autonomy.audit_log()[-1]["cancelled"]
    assert autonomy.report()["recent"] == []


def test_ping_tier2_preselect_and_unknown_kind(autonomy_env):
    home = autonomy_env
    _past("agent-stuck", "nudge it", ["nudge it", "stop it"], 3)
    greenlight.ping("fix-2: agent idle 3h.\nOptions: nudge it | stop it", "fix-2", kind="agent-stuck")
    ev = _outbox(home)[-1]
    assert ev["suggest"] == {"choice": "nudge it", "index": 0, "prob": 0.8, "count": 3, "agree": 3}, ev
    assert decisions.load()[-1]["proposal"]["tier"] == 2 and decisions.load()[-1]["answer"] is None

    greenlight.ping("mr-7: review?\nOptions: agent review | skip", "mr-7", kind="review-request")
    assert "suggest" not in _outbox(home)[-1] and "proposal" not in decisions.load()[-1]


def test_calibration_band_cap_and_graduation(autonomy_env):
    home = autonomy_env
    autonomy.save_settings({"auto": ["milestone"], "gate": 0.35})
    _past("milestone", "3.23", ["3.23", "later"], 100)
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
    r = _cli("calibrate")
    assert r.returncode == 0 and "milestone" in r.stdout and "auto 3/6" in r.stdout, r
    assert "milestone" in subprocess.run([sys.executable, str(SCRIPTS / "decisions.py"), "calibrate"], capture_output=True, text=True).stdout

    # graduation: ten sure proposals, all right, for a kind not opted in
    _reset(home)
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
    s = json.loads(_cli("settings", "--json").stdout)
    assert s["auto"] == ["keep-draft"] and [k for k in s["kinds"] if k["kind"] == "keep-draft"][0]["acc"] == 1.0, s


def test_anti_slop_gate_and_proactive_pings(autonomy_env, monkeypatch):
    home = autonomy_env
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

    with monkeypatch.context() as mp:
        mp.setattr(autonomy, "decide", FakeDecide(i=0, p=0.5, provider="haiku", boom=False, s=1.0))
        assert not autonomy.gate("check-in", "a very appealing check-in", now=now)["ok"]
    assert autonomy.gate("check-in", now=now + 28 * 86400)["ok"]  # four weeks on, three dismissals weigh 0.75
    assert autonomy.gate("watch", now=now)["ok"]  # another kind's history isn't this one's
    _past("check-in", "fix-1", ["fix-1", "later"], 3, ago=0, via="chat")
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
    with monkeypatch.context() as mp:
        mp.setattr(autonomy, "decide", FakeDecide(i=0, p=0.5, provider="haiku", boom=False, s=0.0))
        assert autonomy.gate("watch", "status recap, nothing new", now=now)["score"] == 0.125
        mp.setattr(autonomy, "decide", FakeDecide(i=0, p=0.5, provider="local", boom=False, s=0.0))
        assert autonomy.gate("watch", "status recap", now=now)["score"] == 0.25

    # the gate's threshold is the user's: 0 lets everything through
    autonomy.save_settings({"auto": [], "gate": 0.0})
    assert autonomy.gate("check-in", now=now)["ok"]
    autonomy.save_settings({"auto": [], "gate": 0.35})
    r = _cli("gate", "--kind", "watch")
    assert r.returncode == 1 and json.loads(r.stdout)["ok"] is False, r

    # greenlight.py ping --proactive (or a check-in/watch kind): held when the gate says so, nothing sent, logged
    _reset(home)
    first = greenlight.ping("An optional idea?\nOptions: yes | no", "@generic-1", proactive=True)
    assert first and decisions.load()[-1]["kind"] == "proactive"
    decisions.dismiss("An optional idea?", "@generic-1")
    assert greenlight.ping("Another optional idea?\nOptions: yes | no", "@generic-2", proactive=True)["held"]
    assert len(_outbox(home)) == 1

    _reset(home)
    with decisions.book() as recs:
        for i in range(4):
            r = decisions.record(f"idea {i}\nOptions: do it | no", "ping", kind="check-in")
            r["id"] += f"-p{i}"
            r["closed"] = "dismissed"
            recs.append(r)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert greenlight.ping("You could tidy fix-1's commits.\nOptions: do it | no", "@tidy", kind="check-in")["held"]
    assert out.getvalue().startswith("held: check-in") and _outbox(home) == [] and len(decisions.load()) == 4
    assert autonomy.audit_log()[-1]["act"] == "held"
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "greenlight.py"), "ping", "--proactive", "--kind", "watch", "--key", "@idea", "Maybe look at fix-2?\nOptions: ok | no"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and len(_outbox(home)) == 1, (r, _outbox(home))

    # a lifecycle question is never gated, however often its kind was dismissed
    with decisions.book() as recs:
        for i in range(5):
            r = decisions.record(f"merge {i}?\nOptions: merge | hold", "ping", kind="merge-held")
            r["id"] += f"-h{i}"
            r["closed"] = "dismissed"
            recs.append(r)
    greenlight.ping("fix-1: held. Merge?\nOptions: merge | hold", "fix-1", kind="merge-held")
    assert _outbox(home)[-1]["text"].startswith("fix-1: held")


def test_dismiss_turn_kind_and_clear_all(autonomy_env, monkeypatch):
    home = autonomy_env
    greenlight.ping("fix-4: draft. Ready?" + KD, "fix-4", kind="keep-draft")
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "decisions.py"), "dismiss"],
        input=json.dumps({"text": "fix-4: draft. Ready?", "key": "fix-4", "ping": True}),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and decisions.load()[-1]["closed"] == "dismissed" and decisions.load()[-1]["answer"] is None, (r, decisions.load())
    assert decisions.turn_kind("(Greenlight's idle check-in, not typed by the user: …)") == "check-in"
    assert decisions.turn_kind("(Greenlight's watch, not typed by the user: these…)") == "watch"
    assert decisions.turn_kind("what's next?") is None
    decisions.said("Work on fix-5 next.\nOptions: fix-5 | later", kind="check-in")
    assert decisions.load()[-1]["kind"] == "check-in" and decisions.load()[-1]["via"] == "chat"
    assert decisions.dismiss("Work on fix-5 next.", ping=False)["closed"] == "dismissed"
    assert decisions.dismiss("nothing like it", "k") is None

    # Clear all: a list, closed as "cleared", which the gate doesn't hold against the kind
    _reset(home)
    with decisions.book() as recs:
        for i in range(4):
            r = decisions.record(f"idea {i}?\nOptions: do it | no", "ping", key=f"@c{i}", kind="check-in")
            r["id"] += f"-c{i}"
            recs.append(r)
    monkeypatch.setattr(sys, "argv", ["decisions.py", "dismiss"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([{"text": f"idea {i}?", "key": f"@c{i}", "reason": "cleared"} for i in range(4)])))
    decisions.main()
    assert [x["closed"] for x in decisions.load()] == ["cleared"] * 4, decisions.load()
    assert not greenlight.ping("You could tidy fix-1's commits.\nOptions: do it | no", "@tidy", kind="check-in").get("held")


def test_autonomy_cli_corrupt_log_and_key_flag(autonomy_env, monkeypatch, capsys):
    from unittest import mock

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["autonomy.py", *args])
        autonomy.main()
        return capsys.readouterr().out

    # settings (text + json)
    assert "auto answers:" in run("settings")
    s_json = json.loads(run("settings", "--json"))
    assert "auto" in s_json and "gate" in s_json

    # set --auto + --gate + human gate rejection
    set_out = json.loads(run("set", "--auto", "delete-workspace", "on", "--gate", "0.8"))
    assert "delete-workspace" in set_out["auto"] and set_out["gate"] == 0.8
    set_off = json.loads(run("set", "--auto", "delete-workspace", "off"))
    assert "delete-workspace" not in set_off["auto"]
    with pytest.raises(SystemExit):
        run("set", "--auto", "signoff", "on")

    # propose & gate (including --key flag consumed without corrupting prompt text)
    seen_texts = []
    def fake_propose(kind, text):
        seen_texts.append((kind, text))
        return {"choice": "keep", "prob": 0.9}

    with mock.patch.object(autonomy, "propose", side_effect=fake_propose):
        assert json.loads(run("propose", "--kind", "delete-workspace", "--key", "ws-1", "Delete?"))["choice"] == "keep"
    assert seen_texts == [("delete-workspace", "Delete?")]
    with mock.patch.object(autonomy, "gate", return_value={"ok": True, "score": 0.9}):
        with pytest.raises(SystemExit) as ex:
            run("gate", "--kind", "watch", "Ask?")
        assert ex.value.code == 0

    # calibrate (text + json)
    cal = {"delete-workspace": {"n": 10, "right": 9, "acc": 0.9, "bands": {"0.9+": {"n": 10, "right": 9}}}}
    with (
        mock.patch.object(autonomy, "calibrate", return_value=cal),
        mock.patch.object(autonomy, "candidates", return_value=[{"kind": "delete-workspace", "right": 9, "n": 10}]),
    ):
        assert "graduate? delete-workspace" in run("calibrate")
        assert json.loads(run("calibrate", "--json"))["kinds"]["delete-workspace"]["right"] == 9

    # BUG-CORE-8: audit_log tolerates blank and corrupt JSON lines
    (autonomy_env / "autonomy.jsonl").write_text(
        "\n"
        + json.dumps({"id": "a1", "ts": 1700000000, "act": "auto", "kind": "delete-workspace", "choice": "keep", "prob": 0.92, "text": "Keep?"}) + "\n"
        + "{corrupt json line\n"
        + "   \n"
        + json.dumps({"id": "a2", "ts": 1700000001, "act": "held", "kind": "watch", "score": 0.3, "threshold": 0.7, "text": "Held?"}) + "\n"
    )
    log_txt = run("log")
    assert "auto delete-workspace: keep" in log_txt and "held watch: score 0.3" in log_txt
    assert len(json.loads(run("log", "--json"))) == 2

    with mock.patch.object(autonomy, "undo", return_value={"ok": True, "id": "a1"}):
        assert json.loads(run("undo", "a1"))["ok"] is True
    with pytest.raises(SystemExit):
        run("undo")
