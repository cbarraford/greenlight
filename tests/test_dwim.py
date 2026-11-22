"""python3 tests/test_dwim.py: the DWIM fast path routes board commands direct only when they're safe and clear, and undoes them."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import json, subprocess, tempfile, time, types; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # never the real ~/.greenlight
import concurrent.futures as cf
from pathlib import Path
from unittest import mock
import dwim

dwim.decide = None  # the grammar alone, whatever decide.py is installed on this box


def puck(name, iid=None, repo="app", goal="", pipeline=None, conflicts=False, hidden=False, agent="done", moves=(), life=None, **k):
    mr = {"iid": iid, "ref": f"#{iid}", "title": k.pop("title", name), "pipeline": pipeline, "conflicts": conflicts, "target_branch": "main"} if iid else {}
    return {"name": name, "repo": repo, "goal": goal, "mr": mr, "hidden": hidden, "agent": {"state": agent},
            "moves": [{"kind": m, "prompt": f"{m} prompt for {name}"} for m in moves], "life": life or {}, "path": k.pop("path", ""), **k}


BOARD = [puck("auth-login", 42, goal="Fix the auth login flow", pipeline="failed", moves=["ci"]),
         puck("auth-tokens", 43, goal="Rotate auth tokens", pipeline="success"),
         puck("billing", 44, goal="Billing page (hold until the api lands)", pipeline="failed", moves=["ci"], agent="working"),
         puck("search", 45, goal="Search box", conflicts=True, moves=["conflicts"]),
         puck("old-thing", 46, goal="Old thing (parked)", hidden=True),
         puck("docs", 12, repo="site", goal="Docs")]


def plan(text, board=BOARD, focus=None, source="voice"):
    return dwim.plan({"text": text, "pucks": board, "focus": focus, "source": source})


# the goal's three examples
r = plan("park the two auth ones")
assert r["route"] == "direct" and [a["ws"] for a in r["actions"]] == ["auth-login", "auth-tokens"] and r["verb"] == "park", r
assert r["say"] == "Parked auth-login and auth-tokens." and r["toast"] == "Parked auth-login and auth-tokens" and not r["launch"]
r = plan("kick off CI fixes on the red ones")
assert r["route"] == "direct" and r["launch"] and [a["ws"] for a in r["actions"]] == ["auth-login"], r
assert r["actions"][0]["prompt"] == "ci prompt for auth-login"  # the dash.py move's own prompt
assert r["skipped"] == [{"ws": "billing", "why": "its agent is working"}] and "Skipped billing: its agent is working." in r["say"]
r = plan("hold #42")
assert r["route"] == "direct" and r["actions"] == [{"verb": "hold", "ws": "auth-login", "ref": "#42"}]
assert plan("Can you please hold #42?")["route"] == "direct"  # politeness isn't a question
assert plan("park #42?")["route"] == "chief"  # a bare question isn't consent to act
assert plan("don't merge #42")["actions"][0]["verb"] == "hold"  # "don't merge" is a hold, not a merge
assert plan("release the hold on auth-login")["actions"][0]["verb"] == "release"  # apply checks the fresh goal
r = plan("unhold billing")  # its goal says hold in its own words: never rewritten
assert r["route"] == "direct" and not r["actions"] and "edit the goal" in r["say"]
assert plan("release billing")["route"] == "chief"  # a bare "release" could mean ship it
assert plan("lift the hold on billing", [puck("billing", 44, goal="Billing page (hold)")])["actions"][0]["verb"] == "release"
assert plan("unpark old-thing")["actions"][0]["ws"] == "old-thing"
assert plan("unpark old-thing", [puck("old-thing", 46, goal="stale", goal_live="Old thing (parked)", hidden=True)])["actions"][0]["expected_goal"] == "Old thing (parked)"
parked = [puck("sleeping-a", 47, goal="A (parked)", hidden=True), puck("sleeping-b", 48, goal="B (parked)", hidden=True), puck("active", 49, goal="Active")]
assert [a["ws"] for a in plan("unpark the two parked ones", parked)["actions"]] == ["sleeping-a", "sleeping-b"]
assert [a["ws"] for a in plan("unpark the parked ones", parked)["actions"]] == ["sleeping-a", "sleeping-b"]
assert plan("park old-thing")["route"] == "chief"  # a parked one is off the board for everything but unpark
assert plan("rebase search")["actions"][0]["prompt"] == "conflicts prompt for search"
assert plan("fix the merge conflicts on search")["actions"][0]["verb"] == "rebase"  # "merge conflicts" isn't a merge
assert plan("park it", focus="search")["actions"][0]["ws"] == "search"
assert plan("park this")["route"] == "chief"  # nowhere to point "this"
assert plan("rebase the 45 one", [puck("mr-45", 7, conflicts=True, moves=["conflicts"]), puck("mr-46", 8)])["route"] == "chief"  # 45 is a count-sized number
assert [a["ws"] for a in plan("rebase the 5107 one", [puck("mr-5107", 7, conflicts=True, moves=["conflicts"]), puck("mr-5108", 8)])["actions"]] == ["mr-5107"]
assert plan("park auth-login and billing-api", [puck("auth-login", 42)])["route"] == "chief"  # don't park only the name that matched
assert plan("park auth-login and old-thing")["route"] == "chief"  # a parked explicit target cannot be silently dropped
assert plan("hold #42 or #43")["route"] == "chief"  # don't treat a choice as two actions
assert plan("hold #42 and this", [puck("auth-login", 42), puck("search", 43)], focus="search")["route"] == "chief"
assert plan("hold #42, red ones", [puck("auth-login", 42, pipeline="failed"), puck("billing", 44, pipeline="failed")])["route"] == "chief"
assert plan("hold this and the red ones", [puck("auth-login", 42, pipeline="failed"), puck("billing", 44, pipeline="failed")], focus="auth-login")["route"] == "chief"
assert plan("park the red ones", [puck("red", 42, pipeline="success"), puck("auth-login", 44, pipeline="failed")])["route"] == "chief"
assert plan("hold #42 and 1", [puck("auth-login", 42), puck("first", 1)])["route"] == "chief"
assert [a["ws"] for a in plan("hold #42 and #1", [puck("auth-login", 42), puck("first", 1)])["actions"]] == ["auth-login", "first"]
assert plan("hold #42 and release the hold on #43")["route"] == "chief"
assert plan("park #42 and unpark #46")["route"] == "chief"
assert plan("hold #42 when CI fails", [puck("auth-login", 42, goal="CI fails when tests run")])["route"] == "chief"
assert plan("park #42 for 5 minutes", [puck("auth-login", 42, goal="Fix auth in 5 minutes")])["route"] == "chief"
assert "it says 0 but 1 match" in plan("park 0 red ones", [puck("auth-login", 42, pipeline="failed")])["why"]
assert "it says 0 but 1 match" in plan("park zero red ones", [puck("zero-tests", 42, goal="zero regressions", pipeline="failed")])["why"]
assert plan("hold #42 not #43")["route"] == "chief"  # don't treat an excluded target as another action
assert [a["ws"] for a in plan("hold the api ones", [puck("api-server", 42), puck("capital-forecast", 43)])["actions"]] == ["api-server"]
assert plan("hold this", [puck("same", 42, path="/one"), puck("same", 43, path="/two")], focus="same")["route"] == "chief"
assert plan("park-api", [puck("park-api", 42)])["route"] == "chief"
assert plan("park park-api", [puck("park-api", 42)])["actions"][0]["ws"] == "park-api"
assert plan("hold #42", [puck("button", 42, goal="Fix the Hold button")])["actions"][0]["verb"] == "hold"
assert dwim._goal_after("hold", "Fix the Hold button") == "Fix the Hold button (hold)"

# guards: destructive, reasoning, ambiguous, too broad
for text, why in [("merge #42", "isn't something"), ("close the auth ones", "isn't something"), ("delete search", "isn't something"),
                  ("approve #43", "isn't something"), ("mark #42 ready", "isn't something"), ("why is #42 red?", "question"),
                  ("park the auth ones if CI fails", "condition"), ("don't park search", "not to do"), ("park and hold search", "more than one"),
                  ("park three auth ones", "says 3 but 2 match"), ("park the zebra ones", "nothing on the board"), ("park", "which workspaces"),
                  ("hold #99", "no workspace has #99"), ("hello there", "no board action"), ("tell me about auth", "question"), ("", "empty")]:
    r = plan(text)
    assert r["route"] == "chief" and why in r["why"], (text, r)
two = BOARD + [puck("docs-api", 42, repo="site")]
assert "open in 2 repos" in plan("hold #42", two)["why"]
many = [puck(f"svc-{i}", 100 + i, pipeline="failed", moves=["ci"]) for i in range(7)]
assert "more than the fast path" in plan("fix ci on the red ones", many)["why"]
assert plan("a" * 400)["route"] == "chief"
assert plan("hold #42", focus=None, source="bogus")["source"] == "chat"

# System One (decide.py) through one batch: its route and per-workspace yes/no beat the grammar; its local fallback is no opinion
calls = []
def fake(answers, delay=0):
    def batch(state, questions):
        calls.append((state, questions)); time.sleep(delay)
        return {k: answers(k, q) for k, q in questions.items()}
    return types.SimpleNamespace(batch=batch)
sure = lambda k, q: ({"choice": "direct_board_action", "index": 0, "probs": {}, "confidence": 0.9, "provider": "jev"} if k == "route" else
                     {"choice": "park", "confidence": 0.9, "provider": "jev"} if k == "verb" else
                     {"value": q["context"].startswith("auth-tokens"), "prob": 0.95, "provider": "jev"})
with mock.patch.object(dwim, "decide", fake(sure)):
    r = plan("park the token one")  # "token" is in auth-tokens's goal; System One agrees, and says no to the rest
    assert r["route"] == "direct" and [a["ws"] for a in r["actions"]] == ["auth-tokens"] and r["provider"] == "jev", r
    st, qs = calls[-1]
    assert qs["route"]["options"] == ["direct_board_action", "needs_chief_reasoning"] and qs["route"]["kind"] == "choice"
    assert {"route", "verb"} < set(qs) and all(q["kind"] == "bool" for k, q in qs.items() if k.startswith("is:")) and "board" in st
    assert not any(q.get("context", "").startswith("old-thing") for q in qs.values())  # hidden isn't a candidate
    r = plan("park the tokens one with the auth")  # System One's yes overrides a substring miss
    assert [a["ws"] for a in r["actions"]] == ["auth-tokens"]
    calls.clear()
    assert plan("merge #42")["route"] == "chief" and not calls  # the guards don't even ask
all_yes = lambda k, q: ({"choice": "direct_board_action", "confidence": .9, "provider": "jev"} if k == "route" else
                        {"choice": "park", "confidence": .9, "provider": "jev"} if k == "verb" else
                        {"value": True, "prob": .95, "provider": "jev"})
with mock.patch.object(dwim, "decide", fake(all_yes)):
    assert plan("park auth-login and billing-api", [puck("auth-login", 42)])["route"] == "chief"
    assert plan("park auth-login and the red ones", [puck("auth-login", 42), puck("api", 43, pipeline="failed")])["route"] == "chief"
wrong_target = lambda k, q: ({"choice": "direct_board_action", "confidence": .9, "provider": "jev"} if k == "route" else
                             {"choice": "park", "confidence": .9, "provider": "jev"} if k == "verb" else
                             {"value": q["context"].startswith("billing"), "prob": .95, "provider": "jev"})
with mock.patch.object(dwim, "decide", fake(wrong_target)):
    r = plan("park the auth ones")
    assert r["route"] == "chief" and "disagree" in r["why"], r
thirteen = [puck(f"service-{i}", 100 + i, goal="unrelated") for i in range(13)]
some_yes = lambda k, q: ({"choice": "direct_board_action", "confidence": .9, "provider": "jev"} if k == "route" else
                         {"choice": "hold", "confidence": .9, "provider": "jev"} if k == "verb" else
                         {"value": k in ("is:0", "is:12"), "prob": .95, "provider": "jev"})
with mock.patch.object(dwim, "decide", fake(some_yes)):
    r = plan("hold the migration ones", thirteen)
    assert r["route"] == "chief" and "every possible workspace" in r["why"], r
twins = [puck("search", 42, path="/one", goal="Login auth"), puck("search", 43, path="/two", goal="Billing")]
twin_pick = lambda k, q: ({"choice": "direct_board_action", "confidence": .9, "provider": "jev"} if k == "route" else
                          {"choice": "park", "confidence": .9, "provider": "jev"} if k == "verb" else
                          {"value": "path /one" in q["context"], "prob": .95, "provider": "jev"})
with mock.patch.object(dwim, "decide", fake(twin_pick)):
    r = plan("park the login one", twins)
    assert [a.get("path") for a in r["actions"]] == ["/one"] and len([k for k in calls[-1][1] if k.startswith("is:")]) == 2, r
chief = lambda k, q: {"choice": "needs_chief_reasoning", "confidence": 0.8, "provider": "haiku"} if k == "route" else {"choice": "none", "confidence": 0.2, "provider": "haiku"}
with mock.patch.object(dwim, "decide", fake(chief)):
    assert plan("hold #42")["why"] == "System One says it needs reasoning"
local = lambda k, q: {"choice": "needs_chief_reasoning", "confidence": 0.95, "provider": "local"} if k == "route" else {"value": False, "prob": 0.99, "provider": "local"}
with mock.patch.object(dwim, "decide", fake(local)):
    assert plan("park the two auth ones")["route"] == "direct"  # the unconfigured fallback is no opinion, however it's dressed
other = lambda k, q: {"choice": "hold", "confidence": 0.9, "provider": "jev"} if k == "verb" else {"choice": "direct_board_action", "confidence": 0.9, "provider": "jev"}
with mock.patch.object(dwim, "decide", fake(other)):
    assert "park or hold" in plan("park #42")["why"]  # the two readings disagree: Chief
    r = plan("put #42 on ice")  # no verb the grammar knows: System One's, when it's sure it's direct
    assert r["route"] == "direct" and r["verb"] == "hold"
with mock.patch.object(dwim, "decide", fake(sure, delay=1.5)):
    t = time.monotonic(); r = plan("park the two auth ones")
    assert time.monotonic() - t < 1 and r["route"] == "direct" and r["provider"] == "timeout", r  # late: the grammar answers alone
with mock.patch.object(dwim, "decide", types.SimpleNamespace(batch=lambda s, q: 1 / 0)):
    assert plan("hold #42")["provider"] == "error: ZeroDivisionError" and plan("hold #42")["route"] == "direct"
with mock.patch.object(dwim, "decide", fake(lambda k, q: "garbage")):
    assert plan("hold #42")["route"] == "direct"
assert dwim.sure_bool({"value": False, "prob": 0.1, "provider": "jev"}) is False and dwim.sure_bool({"value": True, "prob": 0.1, "provider": "jev"}) is None
assert dwim.sure_bool({"value": "yes", "prob": 0.9, "provider": "jev"}) is None and dwim.sure_choice({"choice": "x", "confidence": "?", "provider": "jev"}) is None

# apply: goal edits at once, recorded for undo; launches through dash.run; one failure doesn't stop the rest
goals = {"id1": "Fix the auth login flow", "id2": "Rotate auth tokens", "id3": "Search box"}
def ps():
    return {k: ({"worktreeId": k, "displayName": n, "comment": goals[k]}, {}) for k, n in [("id1", "auth-login"), ("id2", "auth-tokens"), ("id3", "search")]}
def orca(*a, **k):
    assert a[:3] == ("worktree", "set", "--worktree") and a[4] == "--comment"
    goals[a[3].removeprefix("id:")] = a[5]
dash = types.SimpleNamespace(live_agents=ps, run=mock.Mock(return_value={"ok": True, "agent": "claude"}),
                             snapshot=lambda: {"pucks": BOARD})
gl = types.SimpleNamespace(orca=orca, linked_number=lambda v: v, history_file=lambda n: Path(os.environ["GREENLIGHT_HOME"]) / "history" / f"{n}.md")
with mock.patch.dict(sys.modules, dash=dash, greenlight=gl):
    r = dwim.apply({"text": "park the two auth ones", "source": "voice", "actions": plan("park the two auth ones")["actions"]})
    assert r["ok"] and r["done"] == ["auth-login", "auth-tokens"] and r["undo"], r
    assert goals["id1"] == "Fix the auth login flow (parked)" and goals["id2"] == "Rotate auth tokens (parked)"
    assert "park from voice: “park the two auth ones”" in (Path(os.environ["GREENLIGHT_HOME"]) / "history" / "auth-login.md").read_text()
    goals["id2"] = "Rotate auth tokens, edited since (parked)"  # someone edited it after: undo leaves it
    u = dwim.undo(r["undo"])
    assert u == {"ok": True, "undone": ["auth-login"], "kept": ["auth-tokens"]} and goals["id1"] == "Fix the auth login flow", u
    assert dwim.undo(r["undo"])["kept"] == ["auth-tokens"]
    goals["id2"] = "Rotate auth tokens (parked)"
    assert dwim.undo(r["undo"])["undone"] == ["auth-tokens"]
    assert dwim.undo(r["undo"])["error"] == "nothing to undo" and dwim.undo("../../etc")["error"] == "bad undo id"
    r = dwim.apply({"text": "hold #42", "actions": [{"verb": "hold", "ws": "auth-login"}, {"verb": "hold", "ws": "gone"}, {"verb": "merge", "ws": "search"}]})
    assert not r["ok"] and r["done"] == ["auth-login"] and r["errors"] == ["gone: no such workspace"] and goals["id3"] == "Search box"  # merge isn't a verb
    r = dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login", "prompt": "fix it"}]})
    assert r["ok"] and r["undo"] is None and dash.run.call_args.args == ("auth-login", "ci prompt for auth-login", "id1")
    (dwim.DWIM / "launch" / f"{dwim._lock_for('id1').name}.json").unlink()
    dash.run.return_value = {"error": "its agent is still working; wait for it to finish"}
    assert dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login", "prompt": "x"}]})["errors"] == ["auth-login: its agent is still working; wait for it to finish"]
    def slow_launch(*a):
        time.sleep(.03)
        return {"ok": True, "agent": "codex"}
    with mock.patch.object(dash, "run", side_effect=slow_launch) as launches, cf.ThreadPoolExecutor(max_workers=2) as concurrent:
        results = list(concurrent.map(lambda _: dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login"}]}), range(2)))
    assert launches.call_count == 1 and sum(bool(r["done"]) for r in results) == 1 and any("just requested" in str(r["errors"]) for r in results), results
    assert dwim.apply({"actions": [{"verb": "hold", "ws": "auth-login"}]})["unchanged"] == ["auth-login"]  # already held: unchanged, no second "(hold)"
    assert goals["id1"].count("(hold)") == 1
    stale_release = plan("release the hold on #42", [puck("auth-login", 42, goal="Fix the auth login flow")])
    assert stale_release["actions"] and dwim.apply({"actions": stale_release["actions"]})["errors"] == ["auth-login: goal changed since planning"]
    fresh_release = plan("release the hold on #42", [puck("auth-login", 42, goal=goals["id1"])])
    assert dwim.apply({"actions": fresh_release["actions"]})["done"] == ["auth-login"]
    assert goals["id1"] == "Fix the auth login flow"
    with mock.patch.object(dash, "snapshot", return_value={"pucks": []}):
        before_calls = dash.run.call_count
        assert dwim.apply({"actions": [{"verb": "ci", "ws": "auth-login", "prompt": "stale"}]})["errors"] == ["auth-login: workspace or MR changed since planning"]
        assert dash.run.call_count == before_calls
    with mock.patch.object(dwim, "_note", side_effect=OSError("history unavailable")):
        r = dwim.apply({"actions": [{"verb": "hold", "ws": "search"}]})
    assert r["ok"] and r["undo"] and goals["id3"] == "Search box (hold)"
    assert dwim.undo(r["undo"])["undone"] == ["search"] and goals["id3"] == "Search box"
    multiline = "Ship feature\n\n- preserve A\n- preserve B"
    goals["id3"] = multiline
    r = dwim.apply({"actions": [{"verb": "park", "ws": "search"}]})
    assert goals["id3"] == multiline + " (parked)" and r["undo"]
    goals["id3"] = multiline.replace("\n\n", "\n") + " (parked)"  # a formatting-only edit since apply
    assert dwim.undo(r["undo"])["kept"] == ["search"] and goals["id3"] != multiline
    assert dwim._goal_after("unpark", multiline + " (parked)") == multiline
    assert dwim._goal_after("unpark", "Investigate (parked) label behavior (parked)") == "Investigate (parked) label behavior (parked)"
    assert dwim._goal_after("release", "Review (hold) marker semantics (hold)") == "Review (hold) marker semantics (hold)"
    assert dwim.applies("unpark", puck("search", goal="Investigate (parked) label behavior (parked)", hidden=True))
    assert dwim.applies("release", puck("search", goal="Review (hold) marker semantics (hold)"))
    goals["id3"] = "Search box"
    with mock.patch.object(dwim, "_write_undo", side_effect=OSError("disk full")):
        r = dwim.apply({"actions": [{"verb": "hold", "ws": "search"}]})
    assert not r["ok"] and goals["id3"] == "Search box" and "can't record Undo" in r["errors"][0]
    with mock.patch.object(gl, "orca", side_effect=RuntimeError("set failed")):
        r = dwim.apply({"undo": "def456def456", "actions": [{"verb": "hold", "ws": "search"}]})
    assert not r["ok"] and r["undo"] is None and goals["id3"] == "Search box"
    goals["id3"] = "Search box (hold)"  # a later independent edit must not become undoable by the failed command
    assert dwim.undo("def456def456")["error"] == "nothing to undo"
    goals["id3"] = "Search box"
    def set_then_fail(*a, **k):
        orca(*a, **k)
        raise RuntimeError("reply lost")
    with mock.patch.object(gl, "orca", side_effect=set_then_fail):
        r = dwim.apply({"undo": "fed654fed654", "actions": [{"verb": "hold", "ws": "search"}]})
    assert not r["ok"] and r["undo"] == "fed654fed654" and goals["id3"] == "Search box (hold)"
    assert dwim.undo(r["undo"])["undone"] == ["search"] and goals["id3"] == "Search box"
    def journaled_orca(*a, **k):
        if a[3] == "id:id3":
            records = [json.loads(f.read_text()) for f in (dwim.DWIM / "undo").glob("*.json")]
            assert any(any(e["after"] == a[5] for e in rec["edits"]) for rec in records)  # durable before Orca changes it
        return orca(*a, **k)
    with mock.patch.object(gl, "orca", side_effect=journaled_orca):
        r = dwim.apply({"undo": "abc123abc123", "actions": [{"verb": "hold", "ws": "search"}]})
    assert r["ok"] and r["undo"] == "abc123abc123" and dwim.undo(r["undo"])["undone"] == ["search"]
    def slow_orca(*a, **k):
        time.sleep(.03)
        return orca(*a, **k)
    with mock.patch.object(gl, "orca", side_effect=slow_orca), cf.ThreadPoolExecutor(max_workers=2) as concurrent:
        results = list(concurrent.map(lambda verb: dwim.apply({"actions": [{"verb": verb, "ws": "search"}]}), ["park", "hold"]))
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
    many_undos = [dwim.apply({"actions": [{"verb": "hold" if i % 2 == 0 else "release", "ws": "search", "expected_goal": goals["id3"]}]})["undo"] for i in range(31)]
    assert all(many_undos) and (dwim.DWIM / "undo" / f"{many_undos[0]}.json").exists()
    for uid in reversed(many_undos):
        assert dwim.undo(uid)["undone"] == ["search"]
    assert goals["id3"] == "Search box"
    goals["id3"] = "Search box (parked)"
    stale_unpark = plan("unpark search", [puck("search", 45, goal=goals["id3"], hidden=True)])
    assert stale_unpark["actions"] and stale_unpark["actions"][0]["expected_goal"] == goals["id3"]
    goals["id3"] = "Do not unpark until #67 merges (parked)"
    assert dwim.apply({"actions": stale_unpark["actions"]})["errors"] == ["search: goal changed since planning"]
    assert goals["id3"] == "Do not unpark until #67 merges (parked)"
    goals["id3"] = "Search box"
    goals["d1"], goals["d2"] = "First", "Second"
    dupes = {k: ({"worktreeId": k, "displayName": "duplicate", "path": path, "comment": goals[k]}, {})
             for k, path in [("d1", "/one"), ("d2", "/two")]}
    action = plan("hold #42", [puck("duplicate", 42, path="/one"), puck("duplicate", 43, path="/two")])["actions"][0]
    with mock.patch.object(dash, "live_agents", return_value=dupes), mock.patch.object(dash, "snapshot", return_value={"pucks": [
        {"name": "duplicate", "path": "/one", "mr": {"ref": "#42"}}, {"name": "duplicate", "path": "/two", "mr": {"ref": "#43"}}]}):
        assert dwim.apply({"actions": [action]})["ok"] and goals["d1"] == "First (hold)" and goals["d2"] == "Second"
    goals["d1"] = "First"
    with mock.patch.object(dash, "live_agents", return_value=dupes), mock.patch.object(dash, "snapshot", return_value={"pucks": [
        {"name": "duplicate", "path": "/one", "mr": {"ref": "#99"}}]}):
        assert dwim.apply({"actions": [action]})["errors"] == ["duplicate: workspace or MR changed since planning"] and goals["d1"] == "First"
    with mock.patch.object(dash, "live_agents", return_value=dupes):
        assert dwim.apply({"actions": [action | {"branch": "other"}]})["errors"] == ["duplicate: workspace or MR changed since planning"]
    dupes["d1"][0]["linkedPR"] = 99
    with mock.patch.object(dash, "live_agents", return_value=dupes):
        assert dwim.apply({"actions": [action]})["errors"] == ["duplicate: workspace or MR changed since planning"]
    with mock.patch.object(dash, "live_agents", return_value=dupes), mock.patch.object(dash, "snapshot", side_effect=FileNotFoundError):
        assert dwim.apply({"actions": [{"verb": "hold", "ws": "duplicate", "path": "/one"}]})["done"] == ["duplicate"]
log = [json.loads(l) for l in (Path(os.environ["GREENLIGHT_HOME"]) / "dwim" / "log.jsonl").read_text().splitlines()]
assert {"apply", "undo"} <= {e["op"] for e in log}

# the instant stage card: System One picks the template (~150 ms budget), facts fill it
life = {"stage": "verify", "sub_name": "Pipeline green", "pile": "yours", "do": "CI failed on #42.", "at": "3.2", "holder": "your agent"}
B2 = [puck("auth-login", 42, pipeline="failed", life=life), puck("auth-tokens", 43, pipeline="success", life={"pile": "waiting", "do": "Waiting on review"})]
c = dwim.stage({"text": "where is auth-login at?", "pucks": B2})
assert c["kind"] == "lifecycle" and [s["state"] for s in c["stages"]] == ["done", "done", "now", "todo", "todo"] and c["stages"][2]["detail"] == "Pipeline green", c
assert c["title"] == "CI failed on #42." and c["at"] == "3.2"
# when the puck carries its repo's own lifecycle steps, the lifecycle card renders those instead of the fixed 5 stages
B_custom = [puck("custom-ws", 50, steps=["mr_open", "qa", "merge", "deploy"], step_index=1,
                 life={"stage": "verify", "sub_name": "Author check", "pile": "yours", "do": "Meet qa.", "at": "3.4", "holder": "your agent"})]
c_cust = dwim.stage({"text": "where is custom-ws at?", "pucks": B_custom})
assert c_cust["kind"] == "lifecycle" and [(s["label"], s["state"]) for s in c_cust["stages"]] == [
    ("Open MR", "done"), ("Qa", "now"), ("Merge", "todo"), ("Deploy", "todo")], c_cust
c = dwim.stage({"text": "which CI status for auth-login?", "pucks": B2})
assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["auth-login"], c
c = dwim.stage({"text": "which ones are red", "pucks": B2})
assert c["kind"] == "table" and c["rows"] == [["auth-login", "#42", "failed", "CI failed on #42."]] and c["tones"] == ["bad"], c
c = dwim.stage({"text": "What's the status of CI?", "pucks": B2})
assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["auth-login", "auth-tokens"], c
c = dwim.stage({"text": "which ones are waiting?", "pucks": B2})
assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["auth-tokens"], c
c = dwim.stage({"text": "Which workspaces are parked?", "pucks": parked})
assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["sleeping-a", "sleeping-b"], c
c = dwim.stage({"text": "Which parked sleeping-a workspace?", "pucks": parked})
assert c["kind"] == "table" and [r[0] for r in c["rows"]] == ["sleeping-a"], c
c = dwim.stage({"text": "show all workspaces", "pucks": [puck(f"work-{i}", i) for i in range(1, 13)]})
assert c["kind"] == "table" and c["title"] == "10 of 12 workspaces", c
assert dwim.stage({"text": "show diff for auth-login", "pucks": [puck("auth-login", 42, path="/not/a/repo")], "focus": "auth-login"})["kind"] is None
assert dwim.stage({"text": "tell me a joke", "pucks": B2})["kind"] is None
assert dwim.stage({"text": "how's it going", "pucks": B2, "focus": "auth-login"})["kind"] == "lifecycle"  # "it" is the window you're in
with mock.patch.dict(sys.modules, dash=types.SimpleNamespace(history=lambda n: [{"ts": "2026-11-20 14:02", "text": "parked from voice"}])):
    c = dwim.stage({"text": "what happened on auth-login today", "pucks": B2})
    assert c["kind"] == "timeline" and c["items"] == [{"at": "11-20 14:02", "label": "parked from voice"}], c
d = tempfile.mkdtemp()
os.system(f"cd {d} && git init -q -b main && git commit -q --allow-empty -m init && git update-ref refs/remotes/origin/main HEAD && git checkout -q -b f "
          f"&& printf 'a\\nb\\n' > x.py && git add x.py && git -c user.email=t@t -c user.name=t commit -q -m x")
c = dwim.stage({"text": "what changed in auth-login", "pucks": [puck("auth-login", 42, path=d)]})
assert c["kind"] == "diff" and c["files"] == [{"path": "x.py", "add": 2, "del": 0}] and c["add"] == 2, c
assert c["title"] == "1 tracked file changed"
Path(d, "x.py").write_text("a\nb\nc\n")
Path(d, "y.py").write_text("z\n")
subprocess.run(["git", "-C", d, "add", "y.py"], check=True)
c = dwim.stage({"text": "what changed in auth-login", "pucks": [puck("auth-login", 42, path=d)]})
assert c["kind"] == "diff" and {f["path"]: f["add"] for f in c["files"]} == {"x.py": 3, "y.py": 1} and c["add"] == 4, c
pick = lambda k, q: {"choice": "table", "confidence": 0.9, "provider": "jev"}
with mock.patch.object(dwim, "decide", fake(pick)):
    assert dwim.stage({"text": "how are we doing", "pucks": B2})["kind"] == "table"  # no words for it: System One's pick
with mock.patch.object(dwim, "decide", fake(pick, delay=1)):
    t = time.monotonic(); c = dwim.stage({"text": "which ones are red", "pucks": B2})
    assert time.monotonic() - t < 0.4 and c["kind"] == "table" and c["provider"] == "timeout"
import greenlight
assert dwim.HOLD.pattern == greenlight.HOLD_RE.pattern and dwim.HOLD.flags == greenlight.HOLD_RE.flags  # copied so plan never imports greenlight
print("dwim ok")
