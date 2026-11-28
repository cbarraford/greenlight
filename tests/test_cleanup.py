"""Tests for scripts/cleanup.py: which workspaces cleanup.py deletes, on canned Orca/GitLab/git data.

Fixtures replay misses from earlier runs: a merged MR kept, a rebased-then-deleted branch, and
approved-by-me review checkouts (kept as "detached HEAD" / "no MR/PR").
"""
import json
import subprocess
import urllib.parse
from unittest import mock

import cleanup


def _mr(iid, state="merged", branch="b", sha=None, author=None):
    return {
        "iid": iid,
        "state": state,
        "source_branch": branch,
        "sha": sha or f"head{iid}",
        "author": {"username": author or cleanup.ME},
    }


class World:
    """A fake GitLab project plus fake git worktrees. Every git/glab call cleanup.py makes goes through run()."""

    def __init__(self, mrs=(), approvals=None, versions=None, cant=()):
        self.mrs = {m["iid"]: m for m in mrs}
        self.approvals = approvals or {}  # iid -> [username]
        self.versions = versions or {}  # iid -> [head sha], newest first
        self.cant = set(cant)  # iids GitLab won't let me approve
        self.trees = {}  # path -> {merge, tip, dirty, unpushed, ancestors}
        self.calls = []

    def tree(self, path, tip="tip", merge=None, dirty=False, unpushed=False, ancestors=()):
        self.trees[path] = {"merge": merge, "tip": tip, "dirty": dirty, "unpushed": unpushed, "ancestors": set(ancestors)}

    def run(self, cmd, cwd=None, timeout=60, check=True):
        self.calls.append(cmd)

        def ok(out, rc=0):
            return subprocess.CompletedProcess(cmd, rc, out if isinstance(out, str) else json.dumps(out), "")

        if cmd[0] == "glab":
            path = urllib.parse.urlparse(cmd[2])
            parts = path.path.split("/")  # projects, pid, merge_requests[, iid[, approvals|versions]]
            if len(parts) == 3:
                branch = urllib.parse.parse_qs(path.query)["source_branch"][0]
                return ok(sorted((m for m in self.mrs.values() if m["source_branch"] == branch), key=lambda m: -m["iid"]))
            iid = int(parts[3])
            if iid not in self.mrs:
                raise RuntimeError(f"glab api {cmd[2]}: 404 Not Found")
            if len(parts) == 4:
                return ok(self.mrs[iid])
            if parts[4] == "approvals":
                return ok({
                    "approved_by": [{"user": {"username": u}} for u in self.approvals.get(iid, [])],
                    "user_can_approve": iid not in self.cant,
                })
            return ok([{"head_commit_sha": s} for s in self.versions.get(iid, [])])
        assert cmd[:2] == ["git", "-C"], cmd
        t, sub = self.trees[cmd[2].rstrip("/")], cmd[3]
        if sub == "config":
            return ok(f"refs/heads/{t['merge']}" if t["merge"] else "", 0 if t["merge"] else 1)
        if sub == "rev-parse":
            return ok(t["tip"])
        if sub == "status":
            return ok(" M x.go" if t["dirty"] else "")
        if sub == "rev-list":  # rev-list -1 <tip> --not --remotes: non-empty = commits no remote has
            return ok(t["tip"] if t["unpushed"] else "")
        if sub == "merge-base":  # --is-ancestor a b
            a, b = cmd[5], cmd[6]
            return ok("", 0 if a == b or (a, b) in t["ancestors"] or (b == "origin/develop" and a == "base") else 1)
        raise AssertionError(cmd)


def _repo(project="acme/widgets"):
    r = cleanup.Repo.__new__(cleanup.Repo)
    r.path, r.error, r.host, r.project, r.base, r.default = "/r", None, "gitlab.com", project, "origin/develop", "develop"
    return r


def _evaluate(world, wt, rp=None, busy=()):
    wt = {"id": wt["path"], **wt}
    with mock.patch.object(cleanup, "run", side_effect=world.run), mock.patch.object(cleanup.os.path, "isdir", return_value=True):
        return cleanup.evaluate(wt, rp or _repo(), set(busy))


def _scan(world, wts, busy=(), rp=None):
    """Full scan() on a canned `orca worktree list`, so the per-path dedupe runs too."""
    rp = rp or _repo()
    now = cleanup.NOW_MS
    res = {
        ("worktree", "list"): {"worktrees": wts},
        ("terminal", "list"): {"terminals": [{"worktreeId": i, "lastOutputAt": now} for i in busy]},
        ("repo", "list"): {"repos": [{"id": "r", "path": "/r"}]},
    }
    with (
        mock.patch.object(cleanup, "orca", side_effect=lambda *a: res[a[:2]]),
        mock.patch.object(cleanup, "Repo", return_value=rp),
        mock.patch.object(cleanup, "run", side_effect=world.run),
        mock.patch.object(cleanup.os.path, "isdir", return_value=True),
    ):
        return {r["path"]: (r["verdict"], r["reason"], r["id"]) for r in cleanup.scan()}


def test_duplicate_worktree_entries_and_stale_open_mr(gl_home):
    # Orca lists a worktree twice, one entry with linkedGitLabMR null
    world = World([_mr(5302, "merged", "me/guards", sha="t5302"), _mr(5290, "opened", "me/guards")])
    world.tree("/w/guards", tip="t5302", merge="me/guards")
    linked = {
        "id": "a",
        "repoId": "r",
        "path": "/w/guards",
        "branch": "refs/heads/me/guards",
        "linkedGitLabMR": 5302,
        "comment": "Merge !5302: GUARDS guards",
    }
    twin = {"id": "b", "repoId": "r", "path": "/w/guards/", "branch": "refs/heads/me/guards", "linkedGitLabMR": None}

    assert _evaluate(world, twin) == ("candidate", "MR !5302 merged (older !5290 open, stale)"), _evaluate(world, twin)
    assert _evaluate(world, linked) == ("candidate", "MR !5302 merged (older !5290 open, stale)")
    assert _evaluate(world, {**linked, "linkedGitLabMR": 5290, "comment": ""})[0] == "candidate"

    # both entries: one row per path, keeping the linked entry's id (the one `orca worktree rm` gets)
    rows = _scan(world, [twin, linked])
    assert list(rows) == ["/w/guards"], rows
    assert rows["/w/guards"] == ("candidate", "MR !5302 merged (older !5290 open, stale)", "a"), rows
    assert _scan(world, [twin, linked], busy=["b"])["/w/guards"][0] == "skip"
    assert _scan(world, [{**twin, "isPinned": True}, linked])["/w/guards"][:2] == ("keep", "pinned")
    assert cleanup.dedupe([twin, linked])[0]["ids"] == ["a", "b"]
    assert cleanup.dedupe([linked])[0] is linked

    # a newer open MR on the branch is real work in progress, not stale
    world2 = World([_mr(5302, "merged", "me/guards", sha="t5302"), _mr(5310, "opened", "me/guards")])
    world2.tree("/w/guards", tip="t5302", merge="me/guards")
    assert _evaluate(world2, twin) == ("keep", "MR !5310 is open")


def test_rebased_merged_mr_and_version_history_lookup(gl_home):
    # GitLab rebased the MR before merging and deleted the branch; local tip is a pushed MR version
    world = World([_mr(78, "merged", "fix-019", sha="rebased78")], versions={78: ["rebased78", "pushed78", "older78"]})
    world.tree("/w/fix-019", tip="pushed78", merge="fix-019", unpushed=True)
    sl019 = {"id": "s", "path": "/w/fix-019", "branch": "refs/heads/fix-019"}
    assert _evaluate(world, sl019, _repo("acme/widgets-private")) == ("candidate", "MR !78 merged")

    # tip behind a pushed version
    world.tree("/w/fix-019", tip="local", merge="fix-019", unpushed=True, ancestors=[("local", "older78")])
    assert _evaluate(world, sl019)[0] == "candidate"

    # commit that was never pushed anywhere is still kept for a human
    world.tree("/w/fix-019", tip="never-pushed", merge="fix-019", unpushed=True)
    assert _evaluate(world, sl019) == ("skip", "MR !78 merged, but local commits aren't in the MR head or any remote")

    # version history is only fetched when the cheap checks fail
    world.calls.clear()
    world.tree("/w/fix-019", tip="rebased78", merge="fix-019")
    assert _evaluate(world, sl019)[0] == "candidate" and not any("versions" in " ".join(c) for c in world.calls)


def test_review_workspaces_approved_and_cant_approve(gl_home):
    me = cleanup.ME
    nl = _repo("acme/ops/deployer")
    world = World(
        [
            _mr(1766, "opened", "bob/gateway-service-account", sha="h1766", author="bob"),
            _mr(1768, "opened", "bob/operator-namespaced-writes", sha="h1768", author="bob"),
            _mr(1785, "merged", "eve/zcash-zebra-7.0.0", sha="h1785", author="frank"),
            _mr(1791, "opened", "fix/operator-container-images", sha="h1791", author="frank"),
            _mr(1792, "opened", "fix/other", sha="h1792", author="frank"),
        ],
        approvals={1766: ["review-bot", me, "erin"], 1768: [me], 1791: [me], 1792: ["carol"]},
        versions={1785: ["h1785", "old1785"]},
    )
    world.tree("/nl/review-ops-1766", tip="h1766")
    r1766 = {"path": "/nl/review-ops-1766", "branch": "", "comment": "Review !1766: docs: tidy the README"}
    assert _evaluate(world, r1766, nl) == ("candidate", "MR !1766 approved by me")

    world.tree("/nl/review-ops-1768", tip="h1768")
    assert _evaluate(world, {"path": "/nl/review-ops-1768", "branch": "refs/heads/mr-1768"}, nl) == ("candidate", "MR !1768 approved by me")

    world.tree("/nl/review-1791", tip="h1791")
    assert _evaluate(world, {"path": "/nl/review-1791", "branch": None, "linkedGitLabMR": 1791}, nl) == ("candidate", "MR !1791 approved by me")

    world.tree("/nl/review-ops-1785", tip="old1785", unpushed=True)
    assert _evaluate(world, {"path": "/nl/review-ops-1785", "comment": "Review !1785: lint"}, nl) == ("candidate", "MR !1785 merged")

    world.tree("/nl/review-1792", tip="h1792")
    assert _evaluate(world, {"path": "/nl/review-1792", "comment": "Review !1792: x"}, nl) == ("keep", "MR !1792 is open")

    # other projects too (widgets-private review-priv-151, detached + linked)
    priv_world = World([_mr(151, "opened", "fix/tss-startup", sha="h151", author="erin")], approvals={151: [me]})
    priv_world.tree("/p/review-priv-151", tip="h151")
    assert _evaluate(
        priv_world,
        {"path": "/p/review-priv-151", "linkedGitLabMR": 151, "comment": "Review !151: fix"},
        _repo("acme/widgets-private"),
    ) == ("candidate", "MR !151 approved by me")

    # one GitLab won't let me approve: the review checkout goes, a workspace on its source branch stays
    cant_world = World([_mr(196, "opened", "sync/develop", sha="h196", author="frank")], cant={196})
    cant_world.tree("/p/mr-196", tip="h196")
    assert _evaluate(cant_world, {"path": "/p/mr-196", "branch": "refs/heads/mr-196"}, _repo("acme/widgets-private")) == (
        "candidate",
        "MR !196 not mine to approve",
    )
    cant_world.tree("/p/sync", tip="h196", merge="sync/develop")
    assert _evaluate(cant_world, {"path": "/p/sync", "branch": "refs/heads/sync/develop"}, _repo("acme/widgets-private")) == (
        "keep",
        "MR !196 is open",
    )


def test_safety_guards_dirty_unpushed_busy_pinned_main_and_detached(gl_home):
    me = cleanup.ME
    nl = _repo("acme/ops/deployer")
    world = World([_mr(1766, "opened", "bob/gateway-service-account", sha="h1766", author="bob")], approvals={1766: ["review-bot", me, "erin"]})
    r1766 = {"path": "/nl/review-ops-1766", "branch": "", "comment": "Review !1766: docs: tidy the README"}

    world.tree("/nl/review-ops-1766", tip="h1766", dirty=True)
    assert _evaluate(world, r1766, nl) == ("skip", "MR !1766 approved by me, but has uncommitted/untracked changes")
    world.tree("/nl/review-ops-1766", tip="mine", unpushed=True)
    assert _evaluate(world, r1766, nl)[0] == "skip"
    world.tree("/nl/review-ops-1766", tip="h1766")
    assert _evaluate(world, r1766, nl, busy=["/nl/review-ops-1766"])[0] == "skip"
    assert _evaluate(world, {**r1766, "isPinned": True}, nl) == ("keep", "pinned")
    assert _evaluate(world, {**r1766, "isMainWorktree": True}, nl) == ("keep", "main checkout")

    # detached trees that name no MR (cache trees) are never looked at
    world.calls.clear()
    assert _evaluate(world, {"path": "/w/widgets", "branch": None, "comment": ""}, nl) == ("keep", "detached HEAD")
    assert not world.calls

    # a comment naming an MR that isn't in this project: nothing found, a detached tree is kept
    world.tree("/nl/gone", tip="x")
    assert _evaluate(world, {"path": "/nl/gone", "comment": "Review !9999: elsewhere"}, nl) == ("keep", "detached HEAD, its MR wasn't found")

    # a branch with no MR still needs to be merged into the default branch
    world.tree("/nl/wip", tip="wip", merge="wip")
    assert _evaluate(world, {"path": "/nl/wip", "branch": "refs/heads/wip"}, nl) == ("keep", "no MR/PR, branch not merged")
    assert _evaluate(world, {"path": "/nl/dev", "branch": "refs/heads/develop"}, nl) == ("keep", "default branch")


def test_post_merge_lifecycle_steps(gl_home):
    # steps after the merge: a lifecycle with `deploy` keeps a merged workspace until it's signed off
    world = World([_mr(31, "merged", "old-deploy", sha="h31")])
    world.tree("/w/old-deploy", tip="h31", merge="old-deploy")
    od = {"path": "/w/old-deploy", "branch": "refs/heads/old-deploy"}
    (cleanup.greenlight.STATE / "last.json").write_text(
        json.dumps({"rows": [{"path": "/w/old-deploy", "next": {"step": "deploy", "owner": "me", "action": "deploy: x Sign it off?"}}]})
    )
    assert cleanup.greenlight.post_merge("cbarraford/greenlight") == []
    assert _evaluate(world, od, _repo("cbarraford/greenlight")) == ("candidate", "MR !31 merged")

    (cleanup.greenlight.STATE / "lifecycles").mkdir(exist_ok=True)
    (cleanup.greenlight.STATE / "lifecycles/gizmos.md").write_text("# gizmos\n\n1. `merge` — x\n2. `deploy` — Prod runs it. Done: `signoff`\n")
    assert _evaluate(world, od, _repo("acme/gizmos")) == ("keep", "MR !31 merged, still to do: deploy")


def test_github_review_pr_states_and_mr_numbers(gl_home):
    # GitHub: someone else's open PR I approved counts as done for my review checkout; my own stays open
    r = object.__new__(cleanup.Repo)
    r.host, r.project = "github.com", "o/r"
    prs = [
        {
            "number": 7,
            "state": "OPEN",
            "headRefOid": "h7",
            "headRefName": "mr-7",
            "author": {"login": "al"},
            "latestReviews": [{"author": {"login": "gh-me"}, "state": "APPROVED"}],
        },
        {"number": 8, "state": "OPEN", "headRefOid": "h8", "headRefName": "x", "author": {"login": "gh-me"}, "latestReviews": []},
    ]

    def fake_run(cmd, **k):
        payload = {"login": "gh-me"} if cmd[1] == "api" else next(p for p in prs if str(p["number"]) == cmd[3])
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    with mock.patch.object(cleanup, "run", side_effect=fake_run):
        assert [(m["label"], m["state"]) for m in r.mrs(None, [7, 8])] == [("#8", "open"), ("#7", "approved by me")]
    assert cleanup.mr_numbers({"comment": "Review #7: x"}, "mr-7", r) == [7]


def test_repo_init_forges_and_errors(gl_home):
    # TEST-ADD-4: Repo.__init__ SSH GitLab, HTTPS GitHub, missing origin/HEAD -> set-head, unsupported forge, git error
    def mk_run(url, head_seq=("origin/develop",), fail_fetch=False):
        heads = iter(head_seq)

        def fake_run(cmd, **_kw):
            if "get-url" in cmd:
                return subprocess.CompletedProcess(cmd, 0, url + "\n", "")
            if "fetch" in cmd:
                if fail_fetch:
                    raise RuntimeError("offline")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if "symbolic-ref" in cmd:
                val = next(heads, "")
                if not val and _kw.get("check", True):
                    raise RuntimeError("no origin/HEAD")
                return subprocess.CompletedProcess(cmd, 0 if val else 1, val + "\n" if val else "", "")
            if "set-head" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return fake_run

    with mock.patch.object(cleanup, "run", side_effect=mk_run("git" + "@gitlab.com:acme/widgets.git")):
        r = cleanup.Repo("/r")
    assert r.error is None and (r.host, r.project, r.default, r.base) == ("gitlab.com", "acme/widgets", "develop", "origin/develop")

    with mock.patch.object(cleanup, "run", side_effect=mk_run("https://github.com/octo/app.git", head_seq=("", "origin/main"))):
        r = cleanup.Repo("/r")
    assert r.error is None and (r.host, r.project, r.default) == ("github.com", "octo/app", "main")

    with mock.patch.object(cleanup, "run", side_effect=mk_run("https://bitbucket.org/a/b.git")):
        r = cleanup.Repo("/r")
    assert r.error == "unsupported forge bitbucket.org"

    with mock.patch.object(cleanup, "run", side_effect=mk_run("git" + "@gitlab.com:a/b.git", fail_fetch=True)):
        r = cleanup.Repo("/r")
    assert r.error == "offline"


def test_scan_log_and_cli_toctou_fail_closed(gl_home, tmp_path, monkeypatch, capsys):
    import stat
    import sys
    import time
    from pathlib import Path

    import pytest

    # 1. scan() + log() with 0600 permissions
    wt_dir = tmp_path / "wt1"
    wt_dir.mkdir()

    def fake_orca_scan(*args):
        if args[:2] == ("worktree", "list"):
            return {"worktrees": [
                {"id": "w-main", "repoId": "r1", "path": str(tmp_path / "main"), "isMainWorktree": True, "branch": "main"},
                {"id": "w1", "repoId": "r1", "path": str(wt_dir), "isMainWorktree": False, "branch": "refs/heads/feat-1", "displayName": "feat-1"},
                {"id": "w-unk", "repoId": "r-missing", "path": str(tmp_path / "none"), "isMainWorktree": False, "branch": "x"},
            ]}
        if args[:2] == ("terminal", "list"):
            return {"terminals": [{"worktreeId": "w-main", "lastOutputAt": time.time() * 1000}]}
        if args[:2] == ("repo", "list"):
            return {"repos": [{"id": "r1", "path": str(tmp_path / "main")}]}
        return {}

    with (
        mock.patch.object(cleanup, "orca", side_effect=fake_orca_scan),
        mock.patch.object(cleanup, "Repo", lambda path: mock.MagicMock(path=path)),
        mock.patch.object(cleanup, "git", lambda *_a, **_kw: "abc123"),
        mock.patch.object(cleanup, "evaluate", lambda w, _r, _b: ("candidate", "merged") if w["id"] == "w1" else ("keep", "main")),
    ):
        rows = cleanup.scan()
    by_id = {r["id"]: r for r in rows}
    assert by_id["w1"]["verdict"] == "candidate" and by_id["w1"]["tip"] == "abc123"
    assert by_id["w-unk"]["verdict"] == "skip" and by_id["w-unk"]["reason"] == "unknown repo"

    cleanup.log({"name": "feat-1", "path": str(wt_dir), "branch": "feat-1", "reason": "merged", "removed": True})
    assert stat.S_IMODE(cleanup.LOG.stat().st_mode) == 0o600
    logged = json.loads(cleanup.LOG.read_text().strip())
    assert logged["name"] == "feat-1" and logged["removed"] is True

    # 2. CLI scan, precheck, apply TOCTOU guards + BUG-CORE-5 git check=True fail-closed
    wt_ok = tmp_path / "wt-ok"
    wt_dirty = tmp_path / "wt-dirty"
    wt_moved = tmp_path / "wt-moved"
    wt_busy = tmp_path / "wt-busy"
    wt_giterr = tmp_path / "wt-giterr"
    for d in (wt_ok, wt_dirty, wt_moved, wt_busy, wt_giterr):
        d.mkdir()

    def make_rows():
        return [
            {"id": "w-ok", "ids": ["w-ok"], "name": "ok", "path": str(wt_ok), "tip": "t1", "branch": "b1", "verdict": "candidate", "reason": "merged"},
            {"id": "w-dirty", "ids": ["w-dirty"], "name": "dirty", "path": str(wt_dirty), "tip": "t2", "branch": "b2", "verdict": "candidate", "reason": "merged"},
            {"id": "w-moved", "ids": ["w-moved"], "name": "moved", "path": str(wt_moved), "tip": "t3", "branch": "b3", "verdict": "candidate", "reason": "merged"},
            {"id": "w-busy", "ids": ["w-busy"], "name": "busy", "path": str(wt_busy), "tip": "t4", "branch": "b4", "verdict": "candidate", "reason": "merged"},
            {"id": "w-giterr", "ids": ["w-giterr"], "name": "giterr", "path": str(wt_giterr), "tip": "t5", "branch": "b5", "verdict": "candidate", "reason": "merged"},
            {"id": "w-keep", "ids": ["w-keep"], "name": "keep", "path": str(tmp_path / "k"), "tip": "", "branch": "main", "verdict": "keep", "reason": "main"},
        ]

    monkeypatch.setattr(cleanup, "scan", make_rows)
    monkeypatch.setattr(sys, "argv", ["cleanup.py", "scan"])
    cleanup.main()
    out = capsys.readouterr().out
    assert "WOULD DELETE (5)" in out and "KEPT (1)" in out

    monkeypatch.setattr(sys, "argv", ["cleanup.py", "precheck"])
    with pytest.raises(SystemExit) as ex:
        cleanup.main()
    assert ex.value.code == 0
    capsys.readouterr()

    seen_checks = []

    def fake_git(path, *args, check=True):
        seen_checks.append((Path(path).name, args[0], check))
        if path == str(wt_giterr):
            if check:
                raise RuntimeError("git rev-parse failed")
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return "t3-new" if path == str(wt_moved) else {"ok": "t1", "dirty": "t2", "busy": "t4"}.get(Path(path).name.split("-")[-1], "")
        if args[0] == "status":
            return " M file.py" if path == str(wt_dirty) else ""
        return ""

    removed_ids = []

    def fake_orca(*args):
        if args[:2] == ("terminal", "list"):
            return {"terminals": [{"worktreeId": "w-busy", "lastOutputAt": time.time() * 1000}]}
        if args[:2] == ("worktree", "rm"):
            removed_ids.append(args[-1])
            return {}
        return {}

    monkeypatch.setattr(cleanup, "git", fake_git)
    monkeypatch.setattr(cleanup, "orca", fake_orca)
    monkeypatch.setattr(sys, "argv", ["cleanup.py", "apply", "--json"])
    cleanup.main()
    res = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert res["w-ok"]["removed"] is True and removed_ids == ["id:w-ok"]
    assert res["w-dirty"]["removed"] is False and "uncommitted" in res["w-dirty"]["error"]
    assert res["w-moved"]["removed"] is False and "HEAD changed" in res["w-moved"]["error"]
    assert res["w-busy"]["removed"] is False and "terminal became active" in res["w-busy"]["error"]
    assert res["w-giterr"]["removed"] is False and "git rev-parse failed" in res["w-giterr"]["error"]
    assert all(chk is True for _, _, chk in seen_checks)
