"""Tests for scripts/dwim.py: DWIM fast-path planning, guards, System One arbitration, apply/undo, and instant stage cards."""
import concurrent.futures as cf
import json
import subprocess
import sys
import time
import types
from unittest import mock

import pytest

import dwim
import greenlight


def _puck(name, iid=None, repo="app", goal="", pipeline=None, conflicts=False, hidden=False, agent="done", moves=(), life=None, **k):
    mr = {"iid": iid, "ref": f"#{iid}", "title": k.pop("title", name), "pipeline": pipeline, "conflicts": conflicts, "target_branch": "main"} if iid else {}
    return {
        "name": name,
        "repo": repo,
        "goal": goal,
        "mr": mr,
        "hidden": hidden,
        "agent": {"state": agent},
        "moves": [{"kind": m, "prompt": f"{m} prompt for {name}"} for m in moves],
        "life": life or {},
        "path": k.pop("path", ""),
        **k,
    }


BOARD = [
    _puck("auth-login", 42, goal="Fix the auth login flow", pipeline="failed", moves=["ci"]),
    _puck("auth-tokens", 43, goal="Rotate auth tokens", pipeline="success"),
    _puck("billing", 44, goal="Billing page (hold until the api lands)", pipeline="failed", moves=["ci"], agent="working"),
    _puck("search", 45, goal="Search box", conflicts=True, moves=["conflicts"]),
    _puck("old-thing", 46, goal="Old thing (parked)", hidden=True),
    _puck("docs", 12, repo="site", goal="Docs"),
]


def _plan(text, board=BOARD, focus=None, source="voice"):
    return dwim.plan({"text": text, "pucks": board, "focus": focus, "source": source})


def _fake_decide(answers, calls, delay=0):
    def batch(state, questions):
        calls.append((state, questions))
        time.sleep(delay)
        return {k: answers(k, q) for k, q in questions.items()}

    return types.SimpleNamespace(batch=batch)


@pytest.fixture()
def dwim_env(gl_home, monkeypatch):
    monkeypatch.setattr(dwim, "decide", None)  # the grammar alone, whatever decide.py is installed on this box
    return gl_home


def test_plan_grammar_routing_and_targets(dwim_env):
    r = _plan("park the two auth ones")
    assert r["route"] == "direct" and [a["ws"] for a in r["actions"]] == ["auth-login", "auth-tokens"] and r["verb"] == "park", r
    assert r["say"] == "Parked auth-login and auth-tokens." and r["toast"] == "Parked auth-login and auth-tokens" and not r["launch"]

    r = _plan("kick off CI fixes on the red ones")
    assert r["route"] == "direct" and r["launch"] and [a["ws"] for a in r["actions"]] == ["auth-login"], r
    assert r["actions"][0]["prompt"] == "ci prompt for auth-login"
    assert r["skipped"] == [{"ws": "billing", "why": "its agent is working"}] and "Skipped billing: its agent is working." in r["say"]

    r = _plan("hold #42")
    assert r["route"] == "direct" and r["actions"] == [{"verb": "hold", "ws": "auth-login", "ref": "#42"}]
    assert _plan("Can you please hold #42?")["route"] == "direct"
    assert _plan("park #42?")["route"] == "chief"
    assert _plan("don't merge #42")["actions"][0]["verb"] == "hold"
    assert _plan("release the hold on auth-login")["actions"][0]["verb"] == "release"

    r = _plan("unhold billing")
    assert r["route"] == "direct" and not r["actions"] and "edit the goal" in r["say"]
    assert _plan("release billing")["route"] == "chief"
    assert _plan("lift the hold on billing", [_puck("billing", 44, goal="Billing page (hold)")])["actions"][0]["verb"] == "release"
    assert _plan("unpark old-thing")["actions"][0]["ws"] == "old-thing"
    assert _plan("unpark old-thing", [_puck("old-thing", 46, goal="stale", goal_live="Old thing (parked)", hidden=True)])["actions"][0]["expected_goal"] == "Old thing (parked)"

    parked = [
        _puck("sleeping-a", 47, goal="A (parked)", hidden=True),
        _puck("sleeping-b", 48, goal="B (parked)", hidden=True),
        _puck("active", 49, goal="Active"),
    ]
    assert [a["ws"] for a in _plan("unpark the two parked ones", parked)["actions"]] == ["sleeping-a", "sleeping-b"]
    assert [a["ws"] for a in _plan("unpark the parked ones", parked)["actions"]] == ["sleeping-a", "sleeping-b"]
    assert _plan("park old-thing")["route"] == "chief"
    assert _plan("rebase search")["actions"][0]["prompt"] == "conflicts prompt for search"
    assert _plan("fix the merge conflicts on search")["actions"][0]["verb"] == "rebase"
    assert _plan("park it", focus="search")["actions"][0]["ws"] == "search"
    assert _plan("park this")["route"] == "chief"
    assert _plan("rebase the 45 one", [_puck("mr-45", 7, conflicts=True, moves=["conflicts"]), _puck("mr-46", 8)])["route"] == "chief"
    assert [a["ws"] for a in _plan("rebase the 5107 one", [_puck("mr-5107", 7, conflicts=True, moves=["conflicts"]), _puck("mr-5108", 8)])["actions"]] == ["mr-5107"]
    assert _plan("park auth-login and billing-api", [_puck("auth-login", 42)])["route"] == "chief"
    assert _plan("park auth-login and old-thing")["route"] == "chief"
    assert _plan("hold #42 or #43")["route"] == "chief"
    assert _plan("hold #42 and this", [_puck("auth-login", 42), _puck("search", 43)], focus="search")["route"] == "chief"
    assert _plan("hold #42, red ones", [_puck("auth-login", 42, pipeline="failed"), _puck("billing", 44, pipeline="failed")])["route"] == "chief"
    assert _plan("hold this and the red ones", [_puck("auth-login", 42, pipeline="failed"), _puck("billing", 44, pipeline="failed")], focus="auth-login")["route"] == "chief"
    assert _plan("park the red ones", [_puck("red", 42, pipeline="success"), _puck("auth-login", 44, pipeline="failed")])["route"] == "chief"
    assert _plan("hold #42 and 1", [_puck("auth-login", 42), _puck("first", 1)])["route"] == "chief"
    assert [a["ws"] for a in _plan("hold #42 and #1", [_puck("auth-login", 42), _puck("first", 1)])["actions"]] == ["auth-login", "first"]
    assert _plan("hold #42 and release the hold on #43")["route"] == "chief"
    assert _plan("park #42 and unpark #46")["route"] == "chief"
    assert _plan("hold #42 when CI fails", [_puck("auth-login", 42, goal="CI fails when tests run")])["route"] == "chief"
    assert _plan("park #42 for 5 minutes", [_puck("auth-login", 42, goal="Fix auth in 5 minutes")])["route"] == "chief"
    assert "it says 0 but 1 match" in _plan("park 0 red ones", [_puck("auth-login", 42, pipeline="failed")])["why"]
    assert "it says 0 but 1 match" in _plan("park zero red ones", [_puck("zero-tests", 42, goal="zero regressions", pipeline="failed")])["why"]
    assert _plan("hold #42 not #43")["route"] == "chief"
    assert [a["ws"] for a in _plan("hold the api ones", [_puck("api-server", 42), _puck("capital-forecast", 43)])["actions"]] == ["api-server"]
    assert _plan("hold this", [_puck("same", 42, path="/one"), _puck("same", 43, path="/two")], focus="same")["route"] == "chief"
    assert _plan("park-api", [_puck("park-api", 42)])["route"] == "chief"
    assert _plan("park park-api", [_puck("park-api", 42)])["actions"][0]["ws"] == "park-api"
    assert _plan("hold #42", [_puck("button", 42, goal="Fix the Hold button")])["actions"][0]["verb"] == "hold"
    assert dwim._goal_after("hold", "Fix the Hold button") == "Fix the Hold button (hold)"


def test_plan_guards_destructive_ambiguous_and_broad(dwim_env):
    for text, why in [
        ("merge #42", "isn't something"),
        ("close the auth ones", "isn't something"),
        ("delete search", "isn't something"),
        ("approve #43", "isn't something"),
        ("mark #42 ready", "isn't something"),
        ("why is #42 red?", "question"),
        ("park the auth ones if CI fails", "condition"),
        ("don't park search", "not to do"),
        ("park and hold search", "more than one"),
        ("park three auth ones", "says 3 but 2 match"),
        ("park the zebra ones", "nothing on the board"),
        ("park", "which workspaces"),
        ("hold #99", "no workspace has #99"),
        ("hello there", "no board action"),
        ("tell me about auth", "question"),
        ("", "empty"),
    ]:
        r = _plan(text)
        assert r["route"] == "chief" and why in r["why"], (text, r)

    two = [*BOARD, _puck("docs-api", 42, repo="site")]
    assert "open in 2 repos" in _plan("hold #42", two)["why"]
    many = [_puck(f"svc-{i}", 100 + i, pipeline="failed", moves=["ci"]) for i in range(7)]
    assert "more than the fast path" in _plan("fix ci on the red ones", many)["why"]
    assert _plan("a" * 400)["route"] == "chief"
    assert _plan("hold #42", focus=None, source="bogus")["source"] == "chat"


def test_plan_with_system_one_and_fallbacks(dwim_env):
    calls: list = []

    def sure(k, q):
        if k == "route":
            return {"choice": "direct_board_action", "index": 0, "probs": {}, "confidence": 0.9, "provider": "jev"}
        if k == "verb":
            return {"choice": "park", "confidence": 0.9, "provider": "jev"}
        return {"value": q["context"].startswith("auth-tokens"), "prob": 0.95, "provider": "jev"}

    with mock.patch.object(dwim, "decide", _fake_decide(sure, calls)):
        r = _plan("park the token one")
        assert r["route"] == "direct" and [a["ws"] for a in r["actions"]] == ["auth-tokens"] and r["provider"] == "jev", r
        st, qs = calls[-1]
        assert qs["route"]["options"] == ["direct_board_action", "needs_chief_reasoning"] and qs["route"]["kind"] == "choice"
        assert {"route", "verb"} < set(qs) and all(q["kind"] == "bool" for k, q in qs.items() if k.startswith("is:")) and "board" in st
        assert not any(q.get("context", "").startswith("old-thing") for q in qs.values())
        r = _plan("park the tokens one with the auth")
        assert [a["ws"] for a in r["actions"]] == ["auth-tokens"]
        calls.clear()
        assert _plan("merge #42")["route"] == "chief" and not calls

    def all_yes(k, q):
        if k == "route":
            return {"choice": "direct_board_action", "confidence": 0.9, "provider": "jev"}
        if k == "verb":
            return {"choice": "park", "confidence": 0.9, "provider": "jev"}
        return {"value": True, "prob": 0.95, "provider": "jev"}

    with mock.patch.object(dwim, "decide", _fake_decide(all_yes, calls)):
        assert _plan("park auth-login and billing-api", [_puck("auth-login", 42)])["route"] == "chief"
        assert _plan("park auth-login and the red ones", [_puck("auth-login", 42), _puck("api", 43, pipeline="failed")])["route"] == "chief"

    def wrong_target(k, q):
        if k == "route":
            return {"choice": "direct_board_action", "confidence": 0.9, "provider": "jev"}
        if k == "verb":
            return {"choice": "park", "confidence": 0.9, "provider": "jev"}
        return {"value": q["context"].startswith("billing"), "prob": 0.95, "provider": "jev"}

    with mock.patch.object(dwim, "decide", _fake_decide(wrong_target, calls)):
        r = _plan("park the auth ones")
        assert r["route"] == "chief" and "disagree" in r["why"], r

    thirteen = [_puck(f"service-{i}", 100 + i, goal="unrelated") for i in range(13)]

    def some_yes(k, q):
        if k == "route":
            return {"choice": "direct_board_action", "confidence": 0.9, "provider": "jev"}
        if k == "verb":
            return {"choice": "hold", "confidence": 0.9, "provider": "jev"}
        return {"value": k in ("is:0", "is:12"), "prob": 0.95, "provider": "jev"}

    with mock.patch.object(dwim, "decide", _fake_decide(some_yes, calls)):
        r = _plan("hold the migration ones", thirteen)
        assert r["route"] == "chief" and "every possible workspace" in r["why"], r

    twins = [_puck("search", 42, path="/one", goal="Login auth"), _puck("search", 43, path="/two", goal="Billing")]

    def twin_pick(k, q):
        if k == "route":
            return {"choice": "direct_board_action", "confidence": 0.9, "provider": "jev"}
        if k == "verb":
            return {"choice": "park", "confidence": 0.9, "provider": "jev"}
        return {"value": "path /one" in q["context"], "prob": 0.95, "provider": "jev"}

    with mock.patch.object(dwim, "decide", _fake_decide(twin_pick, calls)):
        r = _plan("park the login one", twins)
        assert [a.get("path") for a in r["actions"]] == ["/one"] and len([k for k in calls[-1][1] if k.startswith("is:")]) == 2, r

    def chief(k, q):
        if k == "route":
            return {"choice": "needs_chief_reasoning", "confidence": 0.8, "provider": "haiku"}
        return {"choice": "none", "confidence": 0.2, "provider": "haiku"}

    with mock.patch.object(dwim, "decide", _fake_decide(chief, calls)):
        assert _plan("hold #42")["why"] == "System One says it needs reasoning"

    def local(k, q):
        if k == "route":
            return {"choice": "needs_chief_reasoning", "confidence": 0.95, "provider": "local"}
        return {"value": False, "prob": 0.99, "provider": "local"}

    with mock.patch.object(dwim, "decide", _fake_decide(local, calls)):
        assert _plan("park the two auth ones")["route"] == "direct"

    def other(k, q):
        if k == "verb":
            return {"choice": "hold", "confidence": 0.9, "provider": "jev"}
        return {"choice": "direct_board_action", "confidence": 0.9, "provider": "jev"}

    with mock.patch.object(dwim, "decide", _fake_decide(other, calls)):
        assert "park or hold" in _plan("park #42")["why"]
        r = _plan("put #42 on ice")
        assert r["route"] == "direct" and r["verb"] == "hold"

    with mock.patch.object(dwim, "decide", _fake_decide(sure, calls, delay=1.5)):
        t = time.monotonic()
        r = _plan("park the two auth ones")
        assert time.monotonic() - t < 1 and r["route"] == "direct" and r["provider"] == "timeout", r

    with mock.patch.object(dwim, "decide", types.SimpleNamespace(batch=lambda s, q: 1 / 0)):
        assert _plan("hold #42")["provider"] == "error: ZeroDivisionError" and _plan("hold #42")["route"] == "direct"
    with mock.patch.object(dwim, "decide", _fake_decide(lambda k, q: "garbage", calls)):
        assert _plan("hold #42")["route"] == "direct"

    assert dwim.sure_bool({"value": False, "prob": 0.1, "provider": "jev"}) is False
    assert dwim.sure_bool({"value": True, "prob": 0.1, "provider": "jev"}) is None
    assert dwim.sure_bool({"value": "yes", "prob": 0.9, "provider": "jev"}) is None
    assert dwim.sure_choice({"choice": "x", "confidence": "?", "provider": "jev"}) is None


def test_apply_undo_concurrency_and_durability(dwim_env):
    home = dwim_env
    goals = {"id1": "Fix the auth login flow", "id2": "Rotate auth tokens", "id3": "Search box"}

    def ps():
        return {k: ({"worktreeId": k, "displayName": n, "comment": goals[k]}, {}) for k, n in [("id1", "auth-login"), ("id2", "auth-tokens"), ("id3", "search")]}

    def orca(*a, **k):
        assert a[:3] == ("worktree", "set", "--worktree") and a[4] == "--comment"
        goals[a[3].removeprefix("id:")] = a[5]

    dash_mod = types.SimpleNamespace(live_agents=ps, run=mock.Mock(return_value={"ok": True, "agent": "claude"}), snapshot=lambda: {"pucks": BOARD})
    gl_mod = types.SimpleNamespace(orca=orca, linked_number=lambda v: v, history_file=lambda n: home / "history" / f"{n}.md")

    with mock.patch.dict(sys.modules, dash=dash_mod, greenlight=gl_mod):
        r = dwim.apply({"text": "park the two auth ones", "source": "voice", "actions": _plan("park the two auth ones")["actions"]})
        assert r["ok"] and r["done"] == ["auth-login", "auth-tokens"] and r["undo"], r
        assert goals["id1"] == "Fix the auth login flow (parked)" and goals["id2"] == "Rotate auth tokens (parked)"
        assert "park from voice: “park the two auth ones”" in (home / "history" / "auth-login.md").read_text()

        goals["id2"] = "Rotate auth tokens, edited since (parked)"  # someone edited it after: undo leaves it
        u = dwim.undo(r["undo"])
        assert u == {"ok": True, "undone": ["auth-login"], "kept": ["auth-tokens"]} and goals["id1"] == "Fix the auth login flow", u
        assert dwim.undo(r["undo"])["kept"] == ["auth-tokens"]
        goals["id2"] = "Rotate auth tokens (parked)"
        assert dwim.undo(r["undo"])["undone"] == ["auth-tokens"]
        assert dwim.undo(r["undo"])["error"] == "nothing to undo" and dwim.undo("../../etc")["error"] == "bad undo id"

        r = dwim.apply({"text": "hold #42", "actions": [{"verb": "hold", "ws": "auth-login"}, {"verb": "hold", "ws": "gone"}, {"verb": "merge", "ws": "search"}]})
        assert not r["ok"] and r["done"] == ["auth-login"] and r["errors"] == ["gone: no such workspace"] and goals["id3"] == "Search box"

        r = dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login", "prompt": "fix it"}]})
        assert r["ok"] and r["undo"] is None and dash_mod.run.call_args.args == ("auth-login", "ci prompt for auth-login", "id1")
        (dwim.DWIM / "launch" / f"{dwim._lock_for('id1').name}.json").unlink()

        dash_mod.run.return_value = {"error": "its agent is still working; wait for it to finish"}
        assert dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login", "prompt": "x"}]})["errors"] == [
            "auth-login: its agent is still working; wait for it to finish"
        ]

        def slow_launch(*a):
            time.sleep(0.03)
            return {"ok": True, "agent": "codex"}

        with mock.patch.object(dash_mod, "run", side_effect=slow_launch) as launches, cf.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login"}]}), range(2)))
        assert launches.call_count == 1 and sum(bool(r["done"]) for r in results) == 1 and any("just requested" in str(r["errors"]) for r in results), results

        assert dwim.apply({"actions": [{"verb": "hold", "ws": "auth-login"}]})["unchanged"] == ["auth-login"]
        assert goals["id1"].count("(hold)") == 1
        stale_release = _plan("release the hold on #42", [_puck("auth-login", 42, goal="Fix the auth login flow")])
        assert stale_release["actions"] and dwim.apply({"actions": stale_release["actions"]})["errors"] == ["auth-login: goal changed since planning"]
        fresh_release = _plan("release the hold on #42", [_puck("auth-login", 42, goal=goals["id1"])])
        assert dwim.apply({"actions": fresh_release["actions"]})["done"] == ["auth-login"]
        assert goals["id1"] == "Fix the auth login flow"

        with mock.patch.object(dash_mod, "snapshot", return_value={"pucks": []}):
            before_calls = dash_mod.run.call_count
            assert dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login", "prompt": "stale"}]})["errors"] == [
                "auth-login: workspace or MR changed since planning"
            ]
            assert dash_mod.run.call_count == before_calls

        with mock.patch.object(dwim, "_note", side_effect=OSError("history unavailable")):
            r = dwim.apply({"actions": [{"verb": "hold", "ws": "search"}]})
        assert r["ok"] and r["undo"] and goals["id3"] == "Search box (hold)"
        assert dwim.undo(r["undo"])["undone"] == ["search"] and goals["id3"] == "Search box"

        multiline = "Ship feature\n\n- preserve A\n- preserve B"
        goals["id3"] = multiline
        r = dwim.apply({"actions": [{"verb": "park", "ws": "search"}]})
        assert goals["id3"] == multiline + " (parked)" and r["undo"]
        goals["id3"] = multiline.replace("\n\n", "\n") + " (parked)"
        assert dwim.undo(r["undo"])["kept"] == ["search"] and goals["id3"] != multiline
        assert dwim._goal_after("unpark", multiline + " (parked)") == multiline
        assert dwim._goal_after("unpark", "Investigate (parked) label behavior (parked)") == "Investigate (parked) label behavior (parked)"
        assert dwim._goal_after("release", "Review (hold) marker semantics (hold)") == "Review (hold) marker semantics (hold)"
        assert dwim.applies("unpark", _puck("search", goal="Investigate (parked) label behavior (parked)", hidden=True))
        assert dwim.applies("release", _puck("search", goal="Review (hold) marker semantics (hold)"))

        goals["id3"] = "Search box"
        with mock.patch.object(dwim, "_write_undo", side_effect=OSError("disk full")):
            r = dwim.apply({"actions": [{"verb": "hold", "ws": "search"}]})
        assert not r["ok"] and goals["id3"] == "Search box" and "can't record Undo" in r["errors"][0]

        with mock.patch.object(gl_mod, "orca", side_effect=RuntimeError("set failed")):
            r = dwim.apply({"undo": "def456def456", "actions": [{"verb": "hold", "ws": "search"}]})
        assert not r["ok"] and r["undo"] is None and goals["id3"] == "Search box"
        goals["id3"] = "Search box (hold)"
        assert dwim.undo("def456def456")["error"] == "nothing to undo"

        goals["id3"] = "Search box"

        def set_then_fail(*a, **k):
            orca(*a, **k)
            raise RuntimeError("reply lost")

        with mock.patch.object(gl_mod, "orca", side_effect=set_then_fail):
            r = dwim.apply({"undo": "fed654fed654", "actions": [{"verb": "hold", "ws": "search"}]})
        assert not r["ok"] and r["undo"] == "fed654fed654" and goals["id3"] == "Search box (hold)"
        assert dwim.undo(r["undo"])["undone"] == ["search"] and goals["id3"] == "Search box"

        def journaled_orca(*a, **k):
            if a[3] == "id:id3":
                records = [json.loads(f.read_text()) for f in (dwim.DWIM / "undo").glob("*.json")]
                assert any(any(e["after"] == a[5] for e in rec["edits"]) for rec in records)
            return orca(*a, **k)

        with mock.patch.object(gl_mod, "orca", side_effect=journaled_orca):
            r = dwim.apply({"undo": "abc123abc123", "actions": [{"verb": "hold", "ws": "search"}]})
        assert r["ok"] and r["undo"] == "abc123abc123" and dwim.undo(r["undo"])["undone"] == ["search"]

        def slow_orca(*a, **k):
            time.sleep(0.03)
            return orca(*a, **k)

        with mock.patch.object(gl_mod, "orca", side_effect=slow_orca), cf.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda verb: dwim.apply({"actions": [{"verb": verb, "ws": "search"}]}), ["park", "hold"]))
        assert all(r["ok"] for r in results) and "(parked)" in goals["id3"] and "(hold)" in goals["id3"], (results, goals["id3"])

        goals["id3"] = "Search box"
        r = dwim.apply({"actions": [{"verb": "hold", "ws": "search"}]})
        with mock.patch.object(dwim, "_note", side_effect=OSError("history unavailable")):
            assert dwim.undo(r["undo"])["undone"] == ["search"] and goals["id3"] == "Search box"

        first = dwim.apply({"actions": [{"verb": "hold", "ws": "search"}]})["undo"]
        second = dwim.apply({"actions": [{"verb": "park", "ws": "search"}]})["undo"]
        assert dwim.undo(first)["kept"] == ["search"] and (dwim.DWIM / "undo" / f"{first}.json").exists()
        assert dwim.undo(second)["undone"] == ["search"] and goals["id3"] == "Search box (hold)"
        assert dwim.undo(first)["undone"] == ["search"] and goals["id3"] == "Search box"

        many_undos = [
            dwim.apply({"actions": [{"verb": "hold" if i % 2 == 0 else "release", "ws": "search", "expected_goal": goals["id3"]}]})["undo"]
            for i in range(31)
        ]
        assert all(many_undos) and (dwim.DWIM / "undo" / f"{many_undos[0]}.json").exists()
        for uid in reversed(many_undos):
            assert dwim.undo(uid)["undone"] == ["search"]
        assert goals["id3"] == "Search box"

        goals["id3"] = "Search box (parked)"
        stale_unpark = _plan("unpark search", [_puck("search", 45, goal=goals["id3"], hidden=True)])
        assert stale_unpark["actions"] and stale_unpark["actions"][0]["expected_goal"] == goals["id3"]
        goals["id3"] = "Do not unpark until #67 merges (parked)"
        assert dwim.apply({"actions": stale_unpark["actions"]})["errors"] == ["search: goal changed since planning"]
        assert goals["id3"] == "Do not unpark until #67 merges (parked)"

        goals["id3"] = "Search box"
        goals["d1"], goals["d2"] = "First", "Second"
        dupes = {k: ({"worktreeId": k, "displayName": "duplicate", "path": path, "comment": goals[k]}, {}) for k, path in [("d1", "/one"), ("d2", "/two")]}
        action = _plan("hold #42", [_puck("duplicate", 42, path="/one"), _puck("duplicate", 43, path="/two")])["actions"][0]
        with (
            mock.patch.object(dash_mod, "live_agents", return_value=dupes),
            mock.patch.object(
                dash_mod,
                "snapshot",
                return_value={"pucks": [{"name": "duplicate", "path": "/one", "mr": {"ref": "#42"}}, {"name": "duplicate", "path": "/two", "mr": {"ref": "#43"}}]},
            ),
        ):
            assert dwim.apply({"actions": [action]})["ok"] and goals["d1"] == "First (hold)" and goals["d2"] == "Second"

        goals["d1"] = "First"
        with (
            mock.patch.object(dash_mod, "live_agents", return_value=dupes),
            mock.patch.object(dash_mod, "snapshot", return_value={"pucks": [{"name": "duplicate", "path": "/one", "mr": {"ref": "#99"}}]}),
        ):
            assert dwim.apply({"actions": [action]})["errors"] == ["duplicate: workspace or MR changed since planning"] and goals["d1"] == "First"
        with mock.patch.object(dash_mod, "live_agents", return_value=dupes):
            assert dwim.apply({"actions": [action | {"branch": "other"}]})["errors"] == ["duplicate: workspace or MR changed since planning"]
        dupes["d1"][0]["linkedPR"] = 99
        with mock.patch.object(dash_mod, "live_agents", return_value=dupes):
            assert dwim.apply({"actions": [action]})["errors"] == ["duplicate: workspace or MR changed since planning"]
        with mock.patch.object(dash_mod, "live_agents", return_value=dupes), mock.patch.object(dash_mod, "snapshot", side_effect=FileNotFoundError):
            assert dwim.apply({"actions": [{"verb": "hold", "ws": "duplicate", "path": "/one"}]})["done"] == ["duplicate"]

    log = [json.loads(line) for line in (home / "dwim" / "log.jsonl").read_text().splitlines()]
    assert {"apply", "undo"} <= {e["op"] for e in log}


def test_stage_cards_diff_and_system_one(dwim_env, tmp_path):
    life = {"stage": "verify", "sub_name": "Pipeline green", "pile": "yours", "do": "CI failed on #42.", "at": "3.2", "holder": "your agent"}
    b2 = [_puck("auth-login", 42, pipeline="failed", life=life), _puck("auth-tokens", 43, pipeline="success", life={"pile": "waiting", "do": "Waiting on review"})]
    c = dwim.stage({"text": "where is auth-login at?", "pucks": b2})
    assert c["kind"] == "lifecycle" and [s["state"] for s in c["stages"]] == ["done", "done", "now", "todo", "todo"] and c["stages"][2]["detail"] == "Pipeline green", c
    assert c["title"] == "CI failed on #42." and c["at"] == "3.2"

    b_custom = [
        _puck(
            "custom-ws",
            50,
            steps=["mr_open", "qa", "merge", "deploy"],
            step_index=1,
            life={"stage": "verify", "sub_name": "Author check", "pile": "yours", "do": "Meet qa.", "at": "3.4", "holder": "your agent"},
        )
    ]
    c_cust = dwim.stage({"text": "where is custom-ws at?", "pucks": b_custom})
    assert c_cust["kind"] == "lifecycle" and [(s["label"], s["state"]) for s in c_cust["stages"]] == [
        ("Open MR", "done"),
        ("Qa", "now"),
        ("Merge", "todo"),
        ("Deploy", "todo"),
    ], c_cust

    c = dwim.stage({"text": "which CI status for auth-login?", "pucks": b2})
    assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["auth-login"], c
    c = dwim.stage({"text": "which ones are red", "pucks": b2})
    assert c["kind"] == "table" and c["rows"] == [["auth-login", "#42", "failed", "CI failed on #42."]] and c["tones"] == ["bad"], c
    c = dwim.stage({"text": "What's the status of CI?", "pucks": b2})
    assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["auth-login", "auth-tokens"], c
    c = dwim.stage({"text": "which ones are waiting?", "pucks": b2})
    assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["auth-tokens"], c

    parked = [
        _puck("sleeping-a", 47, goal="A (parked)", hidden=True),
        _puck("sleeping-b", 48, goal="B (parked)", hidden=True),
        _puck("active", 49, goal="Active"),
    ]
    c = dwim.stage({"text": "Which workspaces are parked?", "pucks": parked})
    assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["sleeping-a", "sleeping-b"], c
    c = dwim.stage({"text": "Which parked sleeping-a workspace?", "pucks": parked})
    assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["sleeping-a"], c
    c = dwim.stage({"text": "show all workspaces", "pucks": [_puck(f"work-{i}", i) for i in range(1, 13)]})
    assert c["kind"] == "table" and c["title"] == "10 of 12 workspaces", c
    assert dwim.stage({"text": "show diff for auth-login", "pucks": [_puck("auth-login", 42, path="/not/a/repo")], "focus": "auth-login"})["kind"] is None
    assert dwim.stage({"text": "tell me a joke", "pucks": b2})["kind"] is None
    assert dwim.stage({"text": "how's it going", "pucks": b2, "focus": "auth-login"})["kind"] == "lifecycle"

    with mock.patch.dict(sys.modules, dash=types.SimpleNamespace(history=lambda n: [{"ts": "2026-11-20 14:02", "text": "parked from voice"}])):
        c = dwim.stage({"text": "what happened on auth-login today", "pucks": b2})
        assert c["kind"] == "timeline" and c["items"] == [{"at": "11-20 14:02", "label": "parked from voice"}], c

    repo = tmp_path / "diff-repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("config", "user.name", "t")
    git("config", "user.email", "t@t")
    git("commit", "-q", "--allow-empty", "-m", "init")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-q", "-b", "f")
    (repo / "x.py").write_text("a\nb\n")
    git("add", "x.py")
    git("commit", "-q", "-m", "x")

    c = dwim.stage({"text": "what changed in auth-login", "pucks": [_puck("auth-login", 42, path=str(repo))]})
    assert c["kind"] == "diff" and c["files"] == [{"path": "x.py", "add": 2, "del": 0}] and c["add"] == 2, c
    assert c["title"] == "1 tracked file changed"
    (repo / "x.py").write_text("a\nb\nc\n")
    (repo / "y.py").write_text("z\n")
    git("add", "y.py")
    c = dwim.stage({"text": "what changed in auth-login", "pucks": [_puck("auth-login", 42, path=str(repo))]})
    assert c["kind"] == "diff" and {f["path"]: f["add"] for f in c["files"]} == {"x.py": 3, "y.py": 1} and c["add"] == 4, c

    calls: list = []
    pick = lambda k, q: {"choice": "table", "confidence": 0.9, "provider": "jev"}
    with mock.patch.object(dwim, "decide", _fake_decide(pick, calls)):
        assert dwim.stage({"text": "how are we doing", "pucks": b2})["kind"] == "table"
    with mock.patch.object(dwim, "decide", _fake_decide(pick, calls, delay=1)):
        t = time.monotonic()
        c = dwim.stage({"text": "which ones are red", "pucks": b2})
        assert time.monotonic() - t < 0.4 and c["kind"] == "table" and c["provider"] == "timeout"

    assert dwim.HOLD.pattern == greenlight.HOLD_RE.pattern and dwim.HOLD.flags == greenlight.HOLD_RE.flags
