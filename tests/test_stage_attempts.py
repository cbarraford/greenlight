"""Tests for unified stage-attempt tracking across ws_state, dash, orchestrate, and dwim."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dash  # noqa: E402
import greenlight  # noqa: E402
import orchestrate  # noqa: E402
import ws_state  # noqa: E402


def _row(
    *,
    name: str = "fix-ci",
    wid: str = "wt-1",
    path: str = "/tmp/fix-ci",
    step: str = "ci_green",
    owner: str = "agent",
    sha: str = "aaaa1111",
    iid: int = 42,
    agent_state: str = "idle",
    action: str = "pipeline failed",
    goal: str = "Fix tests",
) -> dict:
    return {
        "id": wid,
        "name": name,
        "card": name,
        "path": path,
        "project": "org/repo",
        "forge": "gitlab",
        "branch": "fix-ci",
        "goal": goal,
        "agent": {"state": agent_state, "type": "claude", "last": "pytest failed on test_foo"},
        "next": {"step": step, "owner": owner, "action": action, "agent": "claude"},
        "mr": {
            "iid": iid,
            "ref": f"!{iid}",
            "forge": "gitlab",
            "state": "opened",
            "role": "author",
            "sha": sha,
            "title": "Fix tests",
            "web_url": f"https://gitlab.example/org/repo/-/merge_requests/{iid}",
            "target_branch": "main",
            "pipeline": "failed" if step == "ci_green" else "success",
            "failed_jobs": [{"name": "unit", "stage": "test", "url": "https://gitlab.example/jobs/1"}] if step == "ci_green" else [],
        },
    }


def _init_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    git = lambda *a: subprocess.run(["git", "-C", str(repo_dir), *a], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo_dir / "a.txt").write_text("base\n")
    git("add", "a.txt")
    git("commit", "-m", "init")
    return git("rev-parse", "HEAD")


def test_option_b_thresholds_and_resets(gl_home: Path) -> None:
    r = _row(sha="sha1")
    assert ws_state.stage_attempts(ws_state.load("fix-ci"), r) is None

    # 1 strike on the exact same commit -> stuck (Option B)
    c = ws_state.record_attempt("fix-ci", step="ci_green", sig="ci_green@sha1", agent="claude", source="center", kind="ci", sha="sha1", mr_iid=42)
    att = ws_state.stage_attempts(c, r)
    assert att is not None
    assert att["count"] == 1
    assert att["same_sha"] == 1
    assert att["stuck"] is True
    assert "Failed on this commit" in att["reason"]

    # While agent is still working on that attempt, it's in-flight and not stuck yet
    assert ws_state.stage_attempts(c, _row(sha="sha1", agent_state="working")) is None

    # A new commit (sha2) still on ci_green: 1 prior attempt on old sha -> not stuck yet
    att2 = ws_state.stage_attempts(c, _row(sha="sha2"))
    assert att2 is not None
    assert att2["count"] == 1
    assert att2["same_sha"] == 0
    assert att2["stuck"] is False

    # 2nd attempt on sha2, then pushed sha3 -> 2 failures on same step -> stuck (Option B)
    c = ws_state.record_attempt("fix-ci", step="ci_green", sig="ci_green@sha2", agent="codex", source="basement", kind="ci", sha="sha2", mr_iid=42)
    att3 = ws_state.stage_attempts(c, _row(sha="sha3"))
    assert att3 is not None
    assert att3["count"] == 2
    assert att3["same_sha"] == 0
    assert att3["stuck"] is True
    assert "Failed 2× on this step" in att3["reason"]

    # Advancing to another step clears stuck status automatically
    assert ws_state.stage_attempts(c, _row(step="threads", sha="sha3")) is None

    # Explicit reset clears attempts on current step
    c = ws_state.reset_attempts("fix-ci", step="ci_green")
    assert ws_state.stage_attempts(c, _row(sha="sha1")) is None


def test_quota_handoff_not_stuck_on_first_try(gl_home: Path) -> None:
    r = _row(sha="sha1", action="claude out of tokens; switching")
    r["next"]["agent"] = "codex"
    c = ws_state.record_attempt("fix-ci", step="ci_green", sig="ci_green@sha1", agent="claude", source="center", kind="ci", sha="sha1", mr_iid=42)
    att = ws_state.stage_attempts(c, r)
    assert att is not None
    assert att["count"] == 1
    assert att["stuck"] is False


def test_local_dirty_and_unpushed_detection(gl_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    base_sha = _init_git_repo(repo)
    # Add an unpushed commit and a dirty file
    (repo / "b.txt").write_text("fix\n")
    subprocess.run(["git", "-C", str(repo), "add", "b.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "local fix"], check=True, capture_output=True)
    (repo / "dirty.txt").write_text("wip\n")

    r = _row(path=str(repo), sha=base_sha)
    c = ws_state.record_attempt("fix-ci", step="ci_green", sig=f"ci_green@{base_sha}", agent="claude", source="basement", kind="ci", sha=base_sha, mr_iid=42)
    att = ws_state.stage_attempts(c, r, check_local=True)
    assert att is not None
    assert att["stuck"] is True
    assert att["local"]["dirty"] == 1
    assert att["local"]["ahead"] == 1
    assert "1 uncommitted file" in att["reason"]
    assert "1 unpushed commit" in att["reason"]

    # Advice and retry_context both mention the local work
    adv = dash.advice(r, attempts=att)
    assert "local work" in adv
    ctx = ws_state.retry_context(c, att)
    assert "Prior attempt failed" in ctx
    assert "1 uncommitted file" in ctx


def test_moves_enriches_prompt_and_suppresses_auto_when_stuck(gl_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _row(sha="sha1")
    c = ws_state.record_attempt("fix-ci", step="ci_green", sig="ci_green@sha1", agent="claude", source="center", kind="ci", sha="sha1", mr_iid=42)
    att = ws_state.stage_attempts(c, r)
    monkeypatch.setattr(dash, "autorun", lambda: {"repo": {"ci": 0}})
    monkeypatch.setattr(greenlight, "lifecycle_doc", lambda _p: {"rules": "", "checklist": "", "stages": []})

    mvs = dash.moves(r, card=c, attempts=att)
    assert len(mvs) == 1
    assert mvs[0]["kind"] == "ci"
    assert "Prior attempt failed" in mvs[0]["prompt"]
    assert "auto" not in mvs[0], "auto-run must not loop on a stuck stage"


def test_unified_center_basement_and_orchestrate_sharing(gl_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _row(sha="sha1")
    (gl_home / "last.json").write_text(json.dumps({"ts": 1000, "rows": [r]}))

    # Basement records an attempt via dash.tree_attempt
    res = dash.tree_attempt(r["path"], json.dumps({"kind": "ci", "agent": "claude", "source": "basement"}))
    assert res["ok"] is True
    assert res["attempts"]["stuck"] is True

    # Orchestrator fork() immediately sees Basement's attempt
    card = ws_state.load("fix-ci")
    why = orchestrate.fork(r, card)
    assert why is not None and "didn't move it" in why

    # Reset via dash.tree_reset_attempts clears it for both UI and orchestrator
    reset = dash.tree_reset_attempts(r["path"])
    assert reset["ok"] is True
    assert orchestrate.fork(r, ws_state.load("fix-ci")) is None

    # dash.run() records an attempt, appends retry context on repeat, and invalidates suggest cache
    launched: list[str] = []
    monkeypatch.setattr(dash, "live_agents", lambda: {"wt-1": ({"worktreeId": "wt-1", "displayName": "fix-ci", "path": r["path"]}, {"state": "idle"})})
    monkeypatch.setattr(greenlight, "orca", lambda *a: launched.append(a[-1]))
    cache = dash.DASH / "suggest" / "fix-ci.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"sig": "old", "label": "Old", "prompt": "Old prompt", "ok": True}))

    out1 = dash.run("fix-ci", "Fix the failing unit test", kind="ci", source="center")
    assert out1["ok"] is True
    assert not cache.exists(), "suggest cache must be invalidated after launch"
    assert "Prior attempt failed" not in launched[0]

    # Second launch automatically gets the prior-attempt failure context
    out2 = dash.run("fix-ci", "Fix the failing unit test", kind="ci", source="center")
    assert out2["ok"] is True
    assert "Prior attempt failed" in launched[1]


def test_sig_changes_with_attempts(gl_home: Path) -> None:
    p = {"state": "needs", "step": "ci_green", "owner": "agent", "agent": {"state": "idle"}, "mr": {"state": "opened"}}
    s0 = dash.sig(p)
    s1 = dash.sig(p | {"attempts": {"count": 1, "stuck": True}})
    assert s0 != s1
