"""Tests for steward mode on workspace cards, diff re-review, merge drift guard, and per-MR activity log."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import audit  # noqa: E402
import autonomy  # noqa: E402
import dash  # noqa: E402
import greenlight  # noqa: E402
import orchestrate  # noqa: E402
import ws_state  # noqa: E402


def _make_row(
    iid: int,
    *,
    forge: str = "github",
    project: str = "octo/greenlight",
    name: str | None = None,
    sha: str = "aaaa1111bbbb2222",
    target: str = "main",
    state: str = "opened",
    step: str = "ci_green",
    owner: str = "ci",
    action: str = "pipeline running",
    cmd: list[str] | None = None,
    agent_state: str = "none",
    mergeable: bool = False,
    goal: str | None = None,
    path: str = "/tmp/wt",
) -> dict[str, Any]:
    ref = f"#{iid}" if forge == "github" else f"!{iid}"
    ws_name = name or f"ws-{iid}"
    return {
        "id": f"wt-{iid}",
        "name": ws_name,
        "card": ws_name,
        "path": path,
        "forge": forge,
        "project": project,
        "repo": project.rsplit("/", 1)[-1],
        "branch": f"branch-{iid}",
        "goal": goal if goal is not None else f"Merge {ref}: Feature {iid}",
        "comment": goal if goal is not None else f"Merge {ref}: Feature {iid}",
        "agent": {"state": agent_state, "type": "claude" if agent_state != "none" else None, "last": ""},
        "mr": {
            "iid": iid, "ref": ref, "forge": forge, "state": state, "draft": False,
            "title": f"Feature {iid}", "web_url": f"https://example.test/{project}/pull/{iid}",
            "role": "author", "author": "me", "sha": sha, "source_branch": f"branch-{iid}",
            "target_branch": target, "pipeline": "running" if owner == "ci" else "success",
            "conflicts": False, "threads": 0, "approvals": 1, "approvals_required": 1,
            "mergeable": mergeable, "rebase": False, "labels": [],
        },
        "next": {"step": step, "owner": owner, "action": action, "cmd": cmd, "lifecycle": "greenlight.md"},
    }


def _write_last(home: Path, rows: list[dict[str, Any]]) -> None:
    (home / "last.json").write_text(json.dumps({"ts": int(time.time()), "rows": rows}))


def test_steward_card_flag_lifecycle_and_cli(
    gl_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _make_row(10, sha="1111aaaa", target="main")
    _write_last(gl_home, [row])

    # Enable via MR ref CLI
    monkeypatch.setattr(sys, "argv", ["ws_state.py", "steward", "#10", "on", "--json"])
    ws_state.main()
    out = json.loads(capsys.readouterr().out)
    assert out["steward"] is True
    assert out["head"] == "1111aaaa"
    assert out["target"] == "main"
    assert out["activity"][0]["summary"] == "Steward mode enabled"

    # Status & log CLI (plain text)
    monkeypatch.setattr(sys, "argv", ["ws_state.py", "steward", "#10", "status"])
    ws_state.main()
    assert "steward=on" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["ws_state.py", "steward", "#10", "log"])
    ws_state.main()
    assert "Steward mode enabled" in capsys.readouterr().out

    # Switching to a merged MR automatically finishes steward mode
    merged_row = _make_row(10, sha="1111aaaa", target="main", state="merged", step="done", owner="done")
    card, _ = ws_state.observe(merged_row)
    assert card["steward"] is False
    assert any("merged" in e["summary"] for e in ws_state.activity_of(card))

    # Re-enable, then switch workspace to a different MR -> resets steward mode and clears old MR activity
    ws_state.set_steward("ws-10", True, row=row)
    other_mr = _make_row(99, name="ws-10", sha="9999bbbb", target="main")
    other_mr["id"] = row["id"]
    card2, _ = ws_state.observe(other_mr)
    assert card2["steward"] is False
    assert not any("Steward mode enabled" in e["summary"] for e in ws_state.activity_of(card2))

    # Unknown workspace typo raises LookupError instead of creating an orphan card file
    with pytest.raises(LookupError):
        ws_state.resolve_card_key("nonexistent-typo")
    assert not ws_state.path("nonexistent-typo").exists()

    # read_only recognizes steward mutations
    assert greenlight.read_only("steward #10") is False
    assert greenlight.read_only("what is the status of #10?") is True


def test_rereview_guard_fail_closed_contract_fix_and_pass(
    gl_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = tmp_path / "wt-20"
    wt.mkdir()
    pings: list[tuple[str, str]] = []
    launched: list[tuple[str, str, str]] = []

    monkeypatch.setattr(greenlight, "ping", lambda text, key=None, ws=(), kind=None, **kw: pings.append((key or "", kind or "")) or {"asked": "1"})
    monkeypatch.setattr(greenlight, "pinged", dict)
    monkeypatch.setattr(greenlight, "resolve", lambda *a: None)
    monkeypatch.setattr(greenlight, "orca", lambda *a: {"worktrees": [
        {"worktreeId": "wt-20", "branch": "branch-20", "comment": "Merge #20: Feature 20", "agents": []}
    ]})
    monkeypatch.setattr(orchestrate, "live_worktree", lambda r, snap=None: {
        "worktreeId": r["id"], "branch": r["branch"], "comment": r["comment"], "agents": []
    })
    monkeypatch.setattr(orchestrate, "current_mr_state", orchestrate.scanned_mr_state)
    monkeypatch.setattr(orchestrate, "launch", lambda agent, wid, prompt: launched.append((agent, wid, prompt)))

    # 1. Start steward while agent is working -> working->waiting keeps expecting_from_sha -> done sets pending_rereview_sha
    r_work = _make_row(20, sha="sha0001", agent_state="working", step="ci_green", owner="working", path=str(wt))
    _write_last(gl_home, [r_work])
    ws_state.observe(r_work)
    ws_state.set_steward("ws-20", True, row=r_work)

    r_wait = _make_row(20, sha="sha0001", agent_state="waiting", step="ci_green", owner="me", path=str(wt))
    c_wait, _ = ws_state.observe(r_wait)
    assert c_wait["steward_expecting_from_sha"] == "sha0001"
    assert c_wait["steward_pending_rereview_sha"] == ""

    r_done = _make_row(20, sha="sha0002", agent_state="done", step="merge", owner="greenlight", mergeable=True, path=str(wt))
    card, _ = ws_state.observe(r_done)
    assert card["steward_pending_rereview_sha"] == "sha0002"

    # orchestrate.plan schedules rereview (orchestrate is the sole actor)
    acts = orchestrate.plan([r_done], {"ws-20": card}, judge=False)
    assert acts[0]["do"] == "rereview"
    assert acts[0]["sha"] == "sha0002"

    # 2. Audit error (ok=False, failed=[]) -> FAIL CLOSED: never advances reviewed_sha, keeps pending_rereview_sha, pings @rereview
    monkeypatch.setattr(audit, "merge_audit", lambda *a, **kw: {"ok": False, "failed": [], "error": "git fetch failed"})
    res_err = orchestrate.act([r_done], {"ws-20": card}, acts)
    assert any("fail-closed" in x for x in res_err)
    c_err = ws_state.load("ws-20")
    assert c_err["steward_reviewed_sha"] != "sha0002"
    assert c_err["steward_pending_rereview_sha"] == "sha0002"
    assert pings[-1][1] == "agent-stuck"
    assert pings[-1][0].startswith("@rereview:")

    # Unverified local fallback also fails closed
    monkeypatch.setattr(audit, "merge_audit", lambda *a, **kw: {"ok": True, "unverified": True, "failed": []})
    res_unv = orchestrate.act([r_done], {"ws-20": c_err}, acts)
    assert any("fail-closed" in x for x in res_unv)
    assert ws_state.load("ws-20")["steward_reviewed_sha"] != "sha0002"

    # Next tick still schedules rereview (never skips silently to merge)
    acts2 = orchestrate.plan([r_done], {"ws-20": c_err}, judge=False)
    assert acts2[0]["do"] == "rereview"

    # 3. Contract failure -> dispatches fix WITHOUT advancing reviewed_sha; if agent finishes with same sha, escalates fail-closed
    monkeypatch.setattr(audit, "merge_audit", lambda *a, **kw: {
        "ok": False, "failed": [{"contract": "NoScopeCreep", "check": "diff_matches_goal", "p_broken": 0.92}],
    })
    res_fix = orchestrate.act([r_done], {"ws-20": c_err}, acts2)
    assert any("dispatched" in x for x in res_fix)
    assert len(launched) == 1
    assert "NoScopeCreep" in launched[0][2]
    c_fix = ws_state.load("ws-20")
    assert c_fix["steward_expecting_from_sha"] == "sha0002"
    assert c_fix["steward_reviewed_sha"] != "sha0002"  # never marks contract-failed SHA as reviewed

    # Agent exits without pushing -> still fails closed (no second dispatch loop, no silent merge)
    c_nopush, _ = ws_state.observe(r_done)
    acts_np = orchestrate.plan([r_done], {"ws-20": c_nopush}, judge=False)
    assert acts_np[0]["do"] == "rereview"
    res_np = orchestrate.act([r_done], {"ws-20": c_nopush}, acts_np)
    assert any("fail-closed" in x for x in res_np)
    assert len(launched) == 1  # did not double-dispatch

    # 4. New agent works, pushes sha0003, and finishes -> merge_audit passes -> reviewed_sha advances and merge proceeds
    r_work2 = _make_row(20, sha="sha0002", agent_state="working", step="merge", owner="working", path=str(wt))
    ws_state.observe(r_work2)
    r_fixed = _make_row(20, sha="sha0003", agent_state="done", step="merge", owner="greenlight", mergeable=True,
                        cmd=["gh", "pr", "merge", "20", "-R", "octo/greenlight", "--merge"], path=str(wt))
    card3, _ = ws_state.observe(r_fixed)
    monkeypatch.setattr(audit, "merge_audit", lambda *a, **kw: {"ok": True, "failed": []})
    acts3 = orchestrate.plan([r_fixed], {"ws-20": card3}, judge=False)
    assert acts3[0]["do"] == "rereview"
    res_ok = orchestrate.act([r_fixed], {"ws-20": card3}, acts3)
    assert any("re-reviewed" in x for x in res_ok)
    c_ok = ws_state.load("ws-20")
    assert c_ok["steward_reviewed_sha"] == "sha0003"
    assert c_ok["steward_pending_rereview_sha"] == ""

    # Now normal orchestrate plan proceeds to the merge command
    acts4 = orchestrate.plan([r_fixed], {"ws-20": c_ok}, judge=False)
    assert acts4[0]["do"] == "cmd"


def test_checked_merge_and_orchestrate_drift_guard_benefits_every_merge(
    gl_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = tmp_path / "wt-30"
    wt.mkdir()
    pings: list[tuple[str, str]] = []
    subproc_cmds: list[list[str]] = []

    monkeypatch.setattr(greenlight, "ping", lambda text, key=None, ws=(), kind=None, **kw: pings.append((key or "", kind or "")) or {"asked": "1"})
    monkeypatch.setattr(greenlight, "pinged", dict)
    monkeypatch.setattr(greenlight, "resolve", lambda *a: None)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subproc_cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    # Even WITHOUT steward mode enabled, audit.checked_merge catches live head/target drift, pauses card, and pings merge-held (human-only)
    assert "merge-held" in autonomy.GATES
    row = _make_row(30, sha="aaaa3030", target="main", step="merge", owner="greenlight", mergeable=True, path=str(wt))
    _write_last(gl_home, [row])
    ws_state.observe(row)
    subproc_cmds.clear()

    cmd = ["gh", "pr", "merge", "30", "-R", "octo/greenlight", "--merge"]
    monkeypatch.setattr(audit, "forge_refs", lambda *a: ("bbbb9999", "main", "toid1"))
    rc = audit.checked_merge("github", "octo/greenlight", "30", "main", "aaaa3030", "toid1", cmd)
    assert rc == 4
    assert subproc_cmds == []
    c = ws_state.load("ws-30")
    assert c["steward_drift"]["kind"] == "head_changed"
    assert c["steward_drift"]["sha"] == "bbbb9999"
    assert pings[-1][1] == "merge-held"

    # Unconfirmed drift also blocks checked_merge even if refs match the new SHA, AND blocks orchestrate.guard_merge
    monkeypatch.setattr(audit, "forge_refs", lambda *a: ("bbbb9999", "main", "toid1"))
    assert audit.checked_merge("github", "octo/greenlight", "30", "main", "bbbb9999", "toid1", cmd) == 4
    r_new = _make_row(30, sha="bbbb9999", target="main", step="merge", owner="greenlight", mergeable=True, path=str(wt))
    _, why = orchestrate.guard_merge(r_new, cmd)
    assert why and "paused" in why

    # Confirm drift -> non-stewarded card clears drift AND leaves baselines empty (no frozen baseline poisoning future commits)
    c_conf, pk = ws_state.confirm_drift("ws-30")
    assert pk and pk.startswith("@drift:")
    assert c_conf["steward_drift"] is None
    assert c_conf["steward_head"] == ""

    # Enable steward on bbbb9999 -> confirm_drift sets baselines and schedules re-review, and stale scan waits cleanly
    ws_state.set_steward("ws-30", True, row=r_new)
    monkeypatch.setattr(audit, "forge_refs", lambda *a: ("cccc8888", "main", "toid1"))
    assert audit.checked_merge("github", "octo/greenlight", "30", "main", "bbbb9999", "toid1", cmd) == 4
    # Enabling steward while drift is active must NOT clear steward_drift
    ws_state.set_steward("ws-30", True, row=r_new)
    assert ws_state.load("ws-30")["steward_drift"]["sha"] == "cccc8888"

    c_conf2, _ = ws_state.confirm_drift("ws-30")
    assert c_conf2["steward_head"] == "cccc8888"
    assert c_conf2["steward_pending_rereview_sha"] == "cccc8888"
    acts_stale = orchestrate.plan([r_new], {"ws-30": c_conf2}, judge=False)
    assert acts_stale[0]["do"] == "skip"
    assert "catch up" in acts_stale[0]["why"]
    # check_merge_drift on stale scan rejects WITHOUT mutating steward_drift backwards
    assert "stale" in (audit.check_merge_drift("github", "octo/greenlight", "30", "main", "bbbb9999") or "")
    assert ws_state.load("ws-30")["steward_drift"] is None

    # Target-branch change triggers target_changed drift ONCE; next tick skips without flooding activity
    r_tgt = _make_row(30, sha="cccc8888", target="release-2", step="merge", owner="greenlight", mergeable=True, path=str(wt))
    acts_tgt = orchestrate.plan([r_tgt], {"ws-30": c_conf2}, judge=False)
    assert acts_tgt[0]["do"] == "drift"
    assert acts_tgt[0]["kind"] == "target_changed"
    orchestrate.act([r_tgt], {"ws-30": c_conf2}, acts_tgt)
    c_paused = ws_state.load("ws-30")
    assert c_paused["steward_drift"]["kind"] == "target_changed"
    acts_again = orchestrate.plan([r_tgt], {"ws-30": c_paused}, judge=False)
    assert acts_again[0]["do"] == "skip"

    # Answering the drift ping in decisions.jsonl automatically schedules confirm_drift
    # Also verify unanswered decision records (answered: None) never raise TypeError!
    drift_pk = c_paused["steward_drift"]["ping_key"]
    (gl_home / "decisions.jsonl").write_text(json.dumps({
        "id": "d0", "key": drift_pk, "choice": None, "answer": None, "answered": None,
    }) + "\n")
    acts_ans = orchestrate.plan([r_tgt], {"ws-30": c_paused}, judge=False)
    assert acts_ans[0]["do"] == "skip"
    (gl_home / "decisions.jsonl").write_text(
        json.dumps({"id": "d0", "key": drift_pk, "choice": None, "answer": None, "answered": None}) + "\n"
        + json.dumps({"id": "d1", "key": drift_pk, "choice": 0, "answer": "Confirm target release-2", "answered": int(time.time()) + 1}) + "\n"
    )
    acts_conf = orchestrate.plan([r_tgt], {"ws-30": c_paused}, judge=False)
    assert acts_conf[0]["do"] == "confirm_drift"
    orchestrate.act([r_tgt], {"ws-30": c_paused}, acts_conf)
    assert ws_state.load("ws-30")["steward_drift"] is None

    # Stale checked_merge right after confirm_drift returns 4 WITHOUT re-creating steward_drift!
    monkeypatch.setattr(audit, "forge_refs", lambda *a: ("cccc8888", "release-2", "toid2"))
    assert audit.checked_merge("github", "octo/greenlight", "30", "main", "cccc8888", "toid1", cmd) == 4
    assert ws_state.load("ws-30")["steward_drift"] is None

    # Partial (None, None, None) from forge_refs must NOT write bogus None drift
    monkeypatch.setattr(audit, "forge_refs", lambda *a: (None, None, None))
    assert audit.checked_merge("github", "octo/greenlight", "30", "release-2", "cccc8888", "toid2", cmd) == 4
    assert ws_state.load("ws-30")["steward_drift"] is None

    # Disabling steward clears baselines so future commits don't trip false drift
    ws_state.set_steward("ws-30", False, row=r_tgt)
    assert ws_state.load("ws-30")["steward_head"] == ""
    assert ws_state.load("ws-30")["steward_target"] == ""

    # Confirm exact matching refs -> checked_merge succeeds & logs merge activity
    subproc_cmds.clear()
    monkeypatch.setattr(audit, "forge_refs", lambda *a: ("cccc8888", "release-2", "toid2"))
    rc_ok = audit.checked_merge("github", "octo/greenlight", "30", "release-2", "cccc8888", "toid2", cmd)
    assert rc_ok == 0
    assert len(subproc_cmds) == 1
    assert any("Merged #30" in e["summary"] for e in ws_state.activity_of(ws_state.load("ws-30")))


def test_multi_worktree_same_mr_drift_guard(gl_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wt1, wt2 = tmp_path / "wt-a", tmp_path / "wt-b"
    wt1.mkdir()
    wt2.mkdir()
    monkeypatch.setattr(greenlight, "ping", lambda *a, **kw: {"asked": "1"})
    monkeypatch.setattr(greenlight, "pinged", dict)
    r1 = _make_row(55, name="ws-55a", sha="1111aaaa", target="main", path=str(wt1))
    r2 = _make_row(55, name="ws-55b", sha="1111aaaa", target="main", path=str(wt2))
    r2["id"] = "wt-55b"
    _write_last(gl_home, [r1, r2])
    ws_state.observe(r1)
    ws_state.observe(r2)
    ws_state.set_steward("ws-55a", True, row=r1)

    # Even with two worktrees linked to #55, check_merge_drift still enforces ws-55a's baseline
    err = audit.check_merge_drift("github", "octo/greenlight", "55", "main", "2222bbbb")
    assert err and "head changed" in err
    assert ws_state.load("ws-55a")["steward_drift"]["kind"] == "head_changed"


def test_dash_snapshot_activity_and_steward_op(gl_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wt = tmp_path / "wt-40"
    wt.mkdir()
    row = _make_row(40, sha="cccc4040", target="main", step="ci_green", owner="ci", path=str(wt))
    _write_last(gl_home, [row])
    ws_state.observe(row)

    monkeypatch.setattr(greenlight, "pinged", dict)
    monkeypatch.setattr(greenlight, "resolve", lambda *a: None)
    monkeypatch.setattr(dash, "live_agents", lambda: {
        row["id"]: ({"worktreeId": row["id"], "displayName": row["name"], "path": str(wt),
                     "repo": "greenlight", "branch": "branch-40", "comment": row["comment"]}, None)
    })
    monkeypatch.setattr(dash, "cached_page", lambda *a: {})
    monkeypatch.setattr(dash, "cached_suggest", lambda *a: {})
    monkeypatch.setattr(dash.blocking, "cached", dict)

    on_res = dash.steward_op("on", "#40")
    assert on_res["ok"] is True
    assert on_res["steward"] is True
    assert len(on_res["activity"]) >= 1

    snap = dash.snapshot()
    assert "stewards" not in snap  # no separate top-level stewards store
    puck = snap["pucks"][0]
    assert puck["steward"] is True
    assert any("Steward mode enabled" in e["summary"] for e in puck["activity"])

    off_res = dash.steward_op("off", "ws-40")
    assert off_res["steward"] is False
    assert dash.snapshot()["pucks"][0]["steward"] is False


def test_rereview_uncertain_launch_and_same_sha_ping_stability(
    gl_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Uncertain re-review launches keep pending=True (blocking peers and settling via plan()), and same-SHA resets keep active @rereview pings."""
    r = _make_row(50, sha="aaaa11110000", target="main", step="ci_green", owner="agent", path=str(tmp_path))
    _write_last(gl_home, [r])
    ws_state.observe(r, "contract", [])
    ws_state.set_steward("ws-50", True, row=r)
    with ws_state.lock():
        c = ws_state.load("ws-50")
        c["steward_pending_rereview_sha"] = "aaaa11110000"
        c["steward_reviewed_sha"] = "000000000000"
        ws_state.save(c)

    monkeypatch.setattr(audit, "merge_audit", lambda *a, **kw: {"ok": False, "failed": [{"contract": "Tests", "check": "tests", "p_broken": 0.9}]})
    monkeypatch.setattr(greenlight, "orca", lambda *a: {"worktrees": [{"worktreeId": r["id"], "branch": r["branch"], "comment": r["comment"], "agents": []}]})
    monkeypatch.setattr(orchestrate, "live_worktree", lambda row, snap=None: {"worktreeId": row["id"], "branch": row["branch"], "comment": row["comment"], "agents": []})
    monkeypatch.setattr(orchestrate, "current_mr_state", orchestrate.scanned_mr_state)
    monkeypatch.setattr(orchestrate, "dispatch", lambda *a, **kw: (None, "orca IPC timeout"))
    monkeypatch.setattr(greenlight, "note", lambda *a, **kw: None)

    out = orchestrate.act([r], {r["card"]: ws_state.load(r["card"])}, [{"ws": "ws-50", "id": r["id"], "do": "rereview", "sha": "aaaa11110000", "target": "main"}])
    assert any("dispatch failed" in line for line in out), out
    card = ws_state.load("ws-50")
    assert any(d.get("sig") == "rereview@aaaa11110000" and d.get("pending") for d in card["dispatches"])

    # Next tick (< 120s): _steward_plan_guard yields to plan()'s pending wait instead of re-auditing/re-dispatching
    acts = orchestrate.plan([r], {r["card"]: card}, judge=False)
    assert acts[0]["do"] == "skip" and "may still be starting" in acts[0]["why"]

    # After 120s, plan() emits the interrupted-launch fork; acting on it opens @stuck and settles pending=False
    pings_sent: list[str] = []
    monkeypatch.setattr(greenlight, "ping", lambda text, key=None, **kw: pings_sent.append(key or "") or {"asked": "1"})
    monkeypatch.setattr(greenlight, "pinged", dict)
    with ws_state.lock():
        c = ws_state.load("ws-50")
        c["dispatches"][0]["ts"] -= 180
        ws_state.save(c)
    acts_old = orchestrate.plan([r], {r["card"]: ws_state.load("ws-50")}, judge=False)
    assert acts_old[0]["do"] == "fork" and acts_old[0]["pending_sig"] == "rereview@aaaa11110000"
    orchestrate.act([r], {r["card"]: ws_state.load("ws-50")}, acts_old)
    assert len(pings_sent) == 1 and pings_sent[0].startswith("@stuck:ws-50:")

    # Next tick (unanswered @stuck): _steward_plan_guard holds instead of bypassing @stuck or opening a competing @rereview ping
    monkeypatch.setattr(ws_state, "decision_records", list)
    acts_wait = orchestrate.plan([r], {r["card"]: ws_state.load("ws-50")}, judge=False)
    assert acts_wait[0]["do"] == "skip" and "user's answer" in acts_wait[0]["why"]

    # Answering choice=0 retries cleanly, clearing the interrupted record and @stuck fork
    monkeypatch.setattr(ws_state, "decision_records", lambda: [{"key": pings_sent[0], "choice": 0, "answered": int(time.time()) + 5}])
    acts_retry = orchestrate.plan([r], {r["card"]: ws_state.load("ws-50")}, judge=False)
    assert acts_retry[0]["do"] == "rereview" and acts_retry[0].get("clear_interrupted")
    monkeypatch.setattr(audit, "merge_audit", lambda *a, **kw: {"ok": True, "failed": []})
    res_retry = orchestrate.act([r], {r["card"]: ws_state.load("ws-50")}, acts_retry)
    assert any("re-reviewed" in x for x in res_retry)
    assert not ws_state.load("ws-50")["forks"]

    # Non-pending rereview@{sha} survives same-SHA step_changed, but clears on goal_changed
    with ws_state.lock():
        c = ws_state.load("ws-50")
        c["steward_pending_rereview_sha"] = "aaaa11110000"
        c["steward_reviewed_sha"] = "000000000000"
        c["dispatches"] = [{"ts": int(time.time()), "agent": "claude", "step": "ci_green", "sig": "rereview@aaaa11110000", "pending": False}]
        ws_state.save(c)
    r_step2 = _make_row(50, sha="aaaa11110000", target="main", step="threads", owner="agent", path=str(tmp_path))
    ws_state.observe(r_step2, "contract", [])
    assert any(d.get("sig") == "rereview@aaaa11110000" for d in ws_state.load("ws-50")["dispatches"])
    r_goal2 = _make_row(50, sha="aaaa11110000", target="main", step="threads", owner="agent", goal="Updated goal #50", path=str(tmp_path))
    ws_state.observe(r_goal2, "contract", [])
    assert not any(d.get("sig") == "rereview@aaaa11110000" for d in ws_state.load("ws-50")["dispatches"])

    # Same-SHA _state_reset keeps active @rereview ping, whereas same-SHA target_changed or new SHA sweeps it
    open_pings = {"@rereview:ws-50:aaaa11110000"}
    resolved = []
    monkeypatch.setattr(greenlight, "pinged", lambda: set(open_pings))
    monkeypatch.setattr(greenlight, "resolve", lambda k, why="": (open_pings.discard(k), resolved.append((k, why))))
    monkeypatch.setattr(ws_state, "decision_records", list)
    r_same = _make_row(50, sha="aaaa11110000", target="main", step="ready", owner="me", goal="Updated goal #50", path=str(tmp_path))
    orchestrate.phase_cards([r_same])
    assert "@rereview:ws-50:aaaa11110000" in open_pings and not resolved

    r_goal3 = _make_row(50, sha="aaaa11110000", target="main", step="ready", owner="me", goal="Third goal #50", path=str(tmp_path))
    orchestrate.phase_cards([r_goal3])
    assert "@rereview:ws-50:aaaa11110000" not in open_pings and any(k == "@rereview:ws-50:aaaa11110000" for k, _ in resolved)

    open_pings.add("@rereview:ws-50:aaaa11110000")
    resolved.clear()
    r_target_drift = _make_row(50, sha="aaaa11110000", target="release", step="ready", owner="me", goal="Third goal #50", path=str(tmp_path))
    orchestrate.phase_cards([r_target_drift])
    assert "@rereview:ws-50:aaaa11110000" not in open_pings and any(k == "@rereview:ws-50:aaaa11110000" for k, _ in resolved)
