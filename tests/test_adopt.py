"""Tests for scripts/adopt.py: which MRs adopt.py creates workspaces for, on canned GitLab/GitHub/Orca data."""
import json
import subprocess
from unittest import mock

import pytest

import adopt
import greenlight

NOW = 2_000_000_000  # 2033-05-18
TN = "acme/widgets"
PRIV = "acme/widgets-private"
REPO_TN = {"id": "r-tn", "path": "/w/widgets"}


def _mr(iid, author=None, proj=TN, branch=None, draft=False, updated="2033-05-18T03:00:00Z"):
    return {
        "iid": iid,
        "project_id": 7,
        "title": f"t{iid}",
        "source_branch": branch or f"b{iid}",
        "draft": draft,
        "author": {"username": author or greenlight.ME},
        "updated_at": updated,
        "sha": f"sha{iid}",
        "references": {"full": f"{proj}!{iid}"},
        "web_url": f"https://gitlab.com/{proj}/-/merge_requests/{iid}",
    }


def _row(iid, branch="x", wid=None, proj=TN):
    return {
        "id": wid or f"w{iid}",
        "name": f"ws{iid}",
        "project": proj,
        "branch": branch,
        "linked_mr": None,
        "mr": {"iid": iid, "state": "opened"} if iid else None,
    }


def _verdicts(p):
    return {i["key"].split("!")[1]: (i["verdict"], i["reason"].split(":")[0].split(" (")[0]) for i in p["items"]}


def _run_plan(authored, reviews, rows=(), approved=(), reviewers=None, busy=None, cap=adopt.CAP, puck=(), mrs=None, cant=()):
    """Run adopt.plan with canned GitLab/blocking data; returns (plan_result, looked_up_paths)."""
    me = greenlight.ME
    puck = puck if puck is None or isinstance(puck, dict) else dict.fromkeys(puck, "approval")
    lists = {"author_username": authored, "reviewer_username": reviews}
    looked_up: list[str] = []

    def fake_gl_all(path):
        return next(v for k, v in lists.items() if k in path)

    def fake_gl(path):
        iid = int(path.split("/merge_requests/")[1].split("/")[0])
        if path.endswith(f"/merge_requests/{iid}"):
            looked_up.append(path)
            if iid not in (mrs or {}):
                raise RuntimeError("404 Not found")
            return {"state": "opened"} | mrs[iid]
        if path.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": me}}] if iid in approved else [], "user_can_approve": iid not in cant}
        return [{"user": {"username": me}, "state": (reviewers or {}).get(iid, "reviewed")}]

    with (
        mock.patch.object(greenlight, "gl_all", side_effect=fake_gl_all),
        mock.patch.object(greenlight, "gl", side_effect=fake_gl),
        mock.patch.object(adopt, "repo_map", return_value={TN: {"id": "r-tn", "path": "/w/widgets", "forge": "gitlab"}}),
        mock.patch.object(adopt, "checked_out", return_value=busy or {}),
        mock.patch.object(
            adopt.blocking,
            "blocks",
            return_value=None if puck is None else {f"{p}!{i}": {"reason": r} for (p, i), r in puck.items()},
        ),
    ):
        return adopt.plan(list(rows), NOW - 60, cap=cap, now=NOW), looked_up


def test_parse_remote_and_repo_map(gl_home):
    parse = adopt.parse_remote
    assert parse("gitlab.com/acme/ops/review-agent") == ("gitlab.com", "acme/ops/review-agent")
    assert parse("git" + "@gitlab.com:acme/widgets.git") == ("gitlab.com", "acme/widgets")
    assert parse("https://gitlab.com/acme/widgets.git") == ("gitlab.com", "acme/widgets")
    assert parse("https://oauth2:tok" + "@gitlab.com/acme/crypto/sigs/") == ("gitlab.com", "acme/crypto/sigs")
    assert parse("ssh://git" + "@gitlab.com:22/acme/indexer.git") == ("gitlab.com", "acme/indexer")
    assert parse("") == (None, None)

    repos = {
        "repos": [
            # a renamed checkout Orca still lists, first, with the same remote: its missing path must not win
            {"id": "r-old", "path": "/w/old-widgets", "gitRemoteIdentity": {"canonicalKey": "gitlab.com/acme/widgets"}},
            {"id": "r-tn", "path": "/w/widgets", "gitRemoteIdentity": {"canonicalKey": "gitlab.com/acme/widgets"}},
            {"id": "r-nl", "path": "/w/deployer"},  # no identity (cmux/T3 shim shape): origin URL decides
            {"id": "r-gh", "path": "/w/jetty", "gitRemoteIdentity": {"canonicalKey": "github.com/glass-harbor/jetty"}},
        ]
    }
    origin = {"/w/deployer": "git" + "@gitlab.com:acme/ops/deployer.git"}

    def fake_git(path, *a, check=True):
        return origin.get(path, "") if a[:2] == ("remote", "get-url") else ""

    with mock.patch.object(greenlight, "orca", return_value=repos), mock.patch.object(adopt, "git", side_effect=fake_git), \
            mock.patch.object(adopt.os.path, "isdir", side_effect=lambda p: p != "/w/old-widgets"):
        assert adopt.repo_map() == {
            "acme/widgets": {"id": "r-tn", "path": "/w/widgets", "forge": "gitlab"},
            "acme/ops/deployer": {"id": "r-nl", "path": "/w/deployer", "forge": "gitlab"},
            "glass-harbor/jetty": {"id": "r-gh", "path": "/w/jetty", "forge": "github"},
        }


def test_plan_dedupe_verdicts_and_pings(gl_home):
    # dedupe: my MR where I'm also a reviewer shows up once, as mine
    p, _ = _run_plan([_mr(1)], [_mr(1)])
    assert [(i["key"], i["role"]) for i in p["items"]] == [("acme/widgets!1", "author")]

    adopt.save(
        adopt.DISMISSED,
        {
            "acme/widgets!6": {"ts": NOW - 3600, "why": "manual"},
            "acme/widgets!7": {"ts": NOW - 3600, "why": "cleanup"},
            "acme/widgets!10": {"ts": NOW - 3600, "why": "manual"},
            "acme/widgets!99": {"ts": NOW - 3600, "why": "manual"},  # !99 closed since: pruned
        },
    )
    p, _ = _run_plan(
        authored=[_mr(1), _mr(2, branch="feat"), _mr(3, draft=True), _mr(9, branch="busy-b"), _mr(10, updated="2033-05-18T04:00:00Z")],
        reviews=[
            _mr(4, "al"),
            _mr(5, "al", draft=True),
            _mr(6, "al", updated="2033-05-18T02:00:00Z"),
            _mr(7, "al", updated="2033-05-18T04:00:00Z"),
            _mr(8, "al", proj="acme/no-repo"),
            _mr(11, "al", proj="acme/no-repo"),
            _mr(12, "al"),
        ],
        rows=[_row(1), _row(None, branch="feat", wid="wf")],
        approved={4},
        reviewers={6: "unreviewed", 7: "unreviewed"},
        busy={"busy-b": "/w/widgets", "mr-12": "/w/widgets-old"},
    )
    assert _verdicts(p) == {
        "1": ("skip", "covered"),
        "2": ("skip", "covered"),
        "3": ("create", "author MR with no workspace"),
        "4": ("skip", "approved by me"),
        "5": ("skip", "someone else's draft"),
        "6": ("skip", "tombstoned"),
        "7": ("create", "review MR with no workspace"),
        "8": ("no-repo", "no repo in the ADE for acme/no-repo"),
        "9": ("busy", "busy-b is checked out in /w/widgets"),
        "10": ("skip", "tombstoned"),
        "11": ("no-repo", "no repo in the ADE for acme/no-repo"),
        "12": ("busy", "mr-12 is checked out in /w/widgets-old"),
    }, _verdicts(p)
    assert set(p["dismissed"]) == {"acme/widgets!6", "acme/widgets!10"}
    assert [k for k, _ in p["pings"]] == ["@no-repo:acme/no-repo", "@adopt-busy"]
    assert "!8 !11" in p["pings"][0][1] and "acme/widgets!9" in p["pings"][1][1]
    assert all(t.splitlines()[-1].startswith("Options: ") for _, t in p["pings"])
    assert p["seen"] == {"acme/widgets!1": {"id": "w1", "name": "ws1"}}


def test_plan_cap_tombstones_and_created_grace(gl_home):
    # cap: 12 creatable, 10 created, 2 left for the next run
    p, _ = _run_plan([_mr(i) for i in range(1, 13)], [])
    assert [i["verdict"] for i in p["items"]].count("create") == 10
    assert [i["verdict"] for i in p["items"]].count("deferred") == 2
    assert len(_run_plan([_mr(i) for i in range(1, 13)], [], cap=1)[0]["create"]) == 1

    # tombstones: a workspace that vanished while its MR stayed open; not when the MR closed or the workspace remains
    adopt.save(
        adopt.STATE_FILE,
        {
            "seen": {
                "acme/widgets!1": {"id": "w1", "name": "ws1"},  # gone, MR open
                "acme/widgets!2": {"id": "w2", "name": "ws2"},  # gone, MR merged
                "acme/widgets!3": {"id": "w3", "name": "ws3"},  # still there
                "acme/widgets!4": {"id": "w4", "name": "ws4"},  # gone, by cleanup.py
            }
        },
    )
    adopt.LOG.write_text(json.dumps({"name": "ws4", "removed": True}) + "\n")
    p, _ = _run_plan([_mr(1), _mr(3), _mr(4)], [], rows=[_row(3)])
    assert p["dismissed"] == {
        "acme/widgets!1": {"ts": NOW, "why": "manual"},
        "acme/widgets!4": {"ts": NOW, "why": "cleanup"},
    }
    assert _verdicts(p) == {"1": ("skip", "tombstoned"), "3": ("skip", "covered"), "4": ("skip", "tombstoned")}

    # an earlier apply created it but no scan has seen it yet: don't create it twice
    adopt.save(adopt.STATE_FILE, {"created": {"acme/widgets!5": NOW - 30}})
    assert _verdicts(_run_plan([_mr(5)], [])[0])["5"][0] == "skip"
    adopt.save(adopt.STATE_FILE, {"created": {"acme/widgets!5": NOW - 7200}})  # older than the scan: it's gone
    assert _verdicts(_run_plan([_mr(5)], [])[0])["5"][0] == "create"


def test_plan_blocks_on_me_and_scan_failure_recovery(gl_home):
    early = "2033-05-18T02:00:00Z"  # before the tombstones below
    adopt.save(
        adopt.DISMISSED,
        {
            "acme/widgets!26": {"ts": NOW - 3600, "why": "manual"},
            "acme/widgets!28": {"ts": NOW - 3600, "why": "cleanup"},
        },
    )
    p, looked_up = _run_plan(
        authored=[_mr(1)],
        reviews=[_mr(2, "al"), _mr(3, "al")],
        puck={
            (TN, 1): "conflict",
            (TN, 2): "approval",
            (TN, 20): "approval",
            (PRIV, 21): "approval",
            (TN, 22): "orphan",
            (TN, 23): "discussion",
            (TN, 24): "approval",
            (TN, 25): "approval",
            (TN, 26): "discussion",
            (TN, 27): "approval",
            (TN, 28): "discussion",
        },
        mrs={
            20: _mr(20, "al"),
            21: _mr(21, "al", proj=PRIV),
            22: _mr(22, "al") | {"state": "merged"},
            23: _mr(23, "al"),
            24: _mr(24, "al", draft=True),
            26: _mr(26, "al", updated=early),
            27: _mr(27, "al", branch="cov"),
            28: _mr(28, "al"),
        },
        rows=[_row(None, branch="cov", wid="wc")],
        approved={3, 23, 28},
    )
    assert _verdicts(p) == {
        "1": ("create", "author MR with no workspace"),
        "2": ("create", "review MR with no workspace"),
        "3": ("skip", "approved by me"),
        "20": ("create", "review MR with no workspace"),
        "21": ("no-repo", "no repo in the ADE for acme/widgets-private"),
        "23": ("create", "review MR with no workspace"),
        "24": ("skip", "someone else's draft"),
        "26": ("skip", "tombstoned"),
        "27": ("skip", "covered"),
        "28": ("create", "review MR with no workspace"),
    }, _verdicts(p)
    assert sorted(int(x.rsplit("/", 1)[1]) for x in looked_up) == [20, 21, 22, 23, 24, 25, 26, 27, 28]
    by = {i["iid"]: i for i in p["items"]}
    assert by[1]["block"] == "conflict" and "block" not in by[3] and by[20]["reason"].endswith("(I'm blocking it: approval)")
    assert by[20]["name"] == "mr-20" and adopt.create_args(by[20] | {"repo": REPO_TN}, "mr-20")[-3] == "Review !20: t20"
    assert set(p["dismissed"]) == {"acme/widgets!26"} and by[28]["cleared"]

    # the scan failed: author/reviewer queries alone, and nothing it can't see is forgotten (tombstones, seen workspaces)
    adopt.save(
        adopt.STATE_FILE,
        {
            "seen": {
                "acme/widgets!20": {"id": "w20", "name": "mr-20"},  # blocked-only, still there
                "acme/widgets!21": {"id": "w21", "name": "mr-21"},  # blocked-only, deleted
            }
        },
    )
    p, looked_up = _run_plan([_mr(1)], [], rows=[_row(20)], puck=None)
    assert _verdicts(p) == {"1": ("create", "author MR with no workspace")} and not looked_up
    assert set(p["dismissed"]) == {"acme/widgets!26", "acme/widgets!28"}
    assert set(p["seen"]) == {"acme/widgets!20", "acme/widgets!21"}

    # the scan's back: !21's workspace vanished while its MR stayed open, so it's tombstoned, not recreated
    p, _ = _run_plan(
        [_mr(1)],
        [],
        rows=[_row(20)],
        puck={(TN, 20), (TN, 21), (TN, 26)},
        mrs={20: _mr(20, "al"), 21: _mr(21, "al"), 26: _mr(26, "al", updated=early)},
    )
    assert _verdicts(p) == {
        "1": ("create", "author MR with no workspace"),
        "20": ("skip", "covered"),
        "21": ("skip", "tombstoned"),
        "26": ("skip", "tombstoned"),
    }, _verdicts(p)
    assert set(p["seen"]) == {"acme/widgets!20"}

    # a requested review GitLab won't let me approve (my commits are in it): no workspace, unless it's blocked on me
    adopt.save(adopt.STATE_FILE, {})
    adopt.save(adopt.DISMISSED, {})
    p, _ = _run_plan(authored=[], reviews=[_mr(13, "al"), _mr(14, "al")], cant={13, 14}, puck={(TN, 14): "discussion"})
    assert _verdicts(p) == {"13": ("skip", "I can't approve it"), "14": ("create", "review MR with no workspace")}, _verdicts(p)


def test_create_args_and_git_fetch_calls(gl_home):
    mine = {"role": "author", "iid": 5, "title": "Fix x", "source_branch": "me/x", "repo": REPO_TN, "sha": "abc", "forge": "gitlab"}
    theirs = mine | {"role": "review", "iid": 6, "title": "Add y", "source_branch": "al/y"}
    assert adopt.create_args(mine, "origin/me/x") == [
        "worktree", "create", "--name", "me/x", "--repo", "r-tn", "--base-branch", "origin/me/x",
        "--comment", "Merge !5: Fix x", "--setup", "skip",
    ]
    assert adopt.create_args(theirs, "mr-6") == [
        "worktree", "create", "--name", "mr-6", "--repo", "r-tn", "--base-branch", "mr-6",
        "--comment", "Review !6: Add y", "--setup", "skip",
    ]
    assert adopt.create_args(theirs, "abc") == [
        "worktree", "create", "--name", "mr-6", "--repo", "r-tn", "--base-branch", "abc",
        "--comment", "Review !6: Add y", "--setup", "skip",
    ]

    def calls(c):
        with mock.patch.object(adopt, "git") as g, mock.patch.object(greenlight, "orca", return_value={"worktree": {}}) as o:
            adopt.create(c)
        return [x.args[1:] for x in g.call_args_list], o.call_args.args

    g, o = calls(mine)
    assert g == [("fetch", "origin", "me/x")] and o[o.index("--base-branch") + 1] == "origin/me/x"
    g, o = calls(theirs)
    assert g == [("fetch", "origin", "refs/merge-requests/6/head")] and o[o.index("--base-branch") + 1] == "abc"
    with mock.patch.dict(adopt.os.environ, {"GREENLIGHT_ADE": "t3"}), \
            pytest.raises(RuntimeError, match="live ADE backends are retired"):
        calls(theirs)


def test_ensure_branch_on_real_git_repo(gl_home, tmp_path):
    # Orca named it x-2; it ends on x, tracking origin/x, and x-2 is gone
    def sh(*a, cwd=None):
        return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    g = tmp_path / "git"
    sh("git", "init", "-q", "--bare", str(g / "origin.git"))
    sh("git", "clone", "-q", str(g / "origin.git"), str(g / "main"))
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x", cwd=g / "main")
    sh("git", "push", "-q", "origin", "HEAD:refs/heads/x", cwd=g / "main")
    sh("git", "fetch", "-q", "origin", cwd=g / "main")
    sh("git", "worktree", "add", "-q", "-b", "x-2", str(g / "wt"), "origin/x", cwd=g / "main")
    assert adopt.ensure_branch(str(g / "wt"), "x", "x") == "x"
    assert sh("git", "rev-parse", "--abbrev-ref", "HEAD@{u}", cwd=g / "wt") == "origin/x"
    assert "x-2" not in sh("git", "branch", cwd=g / "main")

    # review: mr-6 exists locally (fetched from the MR head) and Orca made mr-6-2 off it
    sh("git", "branch", "mr-6", "origin/x", cwd=g / "main")
    sh("git", "worktree", "add", "-q", "-b", "mr-6-2", str(g / "wt6"), "mr-6", cwd=g / "main")
    assert adopt.ensure_branch(str(g / "wt6"), "mr-6") == "mr-6"
    assert sh("git", "branch", "--show-current", cwd=g / "wt6") == "mr-6"


def test_readopt_with_preserved_local_branch(gl_home, tmp_path):
    """Re-adopting an MR whose local branch was kept by `--no-delete-branch` succeeds without losing commits."""
    def sh(*a, cwd=None):
        return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    g = tmp_path / "git"
    sh("git", "init", "-q", "--bare", str(g / "origin.git"))
    sh("git", "clone", "-q", str(g / "origin.git"), str(g / "main"))
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c1", cwd=g / "main")
    c1 = sh("git", "rev-parse", "HEAD", cwd=g / "main")
    sh("git", "branch", "mr-6", c1, cwd=g / "main")  # preserved stale branch (no worktree)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c2", cwd=g / "main")
    c2 = sh("git", "rev-parse", "HEAD", cwd=g / "main")
    sh("git", "push", "-q", "origin", "HEAD:refs/merge-requests/6/head", cwd=g / "main")
    sh("git", "reset", "-q", "--hard", c1, cwd=g / "main")

    # Simulate native/Worktrunk create: fails if the target branch name already exists locally.
    def fake_orca(*args):
        name = args[args.index("--name") + 1]
        base = args[args.index("--base-branch") + 1]
        dest = g / f"wt-{name}"
        sh("git", "worktree", "add", "-q", "-b", name, str(dest), base, cwd=g / "main")
        return {"worktree": {"path": str(dest)}}

    cand6 = {"role": "review", "iid": 6, "title": "Add y", "source_branch": "al/y",
             "repo": {"id": str(g / "main"), "path": str(g / "main")}, "sha": c2, "forge": "gitlab"}

    # Rollback guard: if creation fails after clearing the stale ancestor ref, the ref is restored.
    with mock.patch.object(greenlight, "orca", side_effect=RuntimeError("boom")), pytest.raises(RuntimeError, match="boom"):
        adopt.create(cand6)
    assert sh("git", "rev-parse", "refs/heads/mr-6", cwd=g / "main") == c1

    # Ancestor preserved branch: cleanly replaced on `mr-6` at the updated MR head `c2`.
    with mock.patch.object(greenlight, "orca", side_effect=fake_orca):
        out = adopt.create(cand6)
    assert out["branch"] == "mr-6"
    assert sh("git", "rev-parse", "HEAD", cwd=out["path"]) == c2

    # Diverged preserved branch: local commits are never lost; adopt uses a free suffixed name (`mr-7-2`).
    sh("git", "checkout", "-q", "-b", "mr-7", c1, cwd=g / "main")
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "local-wip", cwd=g / "main")
    local_wip = sh("git", "rev-parse", "HEAD", cwd=g / "main")
    sh("git", "checkout", "-q", "--detach", c1, cwd=g / "main")  # un-check-out mr-7 (preserved without worktree)
    sh("git", "push", "-q", "origin", f"{c2}:refs/merge-requests/7/head", cwd=g / "main")
    cand7 = cand6 | {"iid": 7}
    with mock.patch.object(greenlight, "orca", side_effect=fake_orca):
        out7 = adopt.create(cand7)
    assert out7["branch"] == "mr-7-2"
    assert sh("git", "rev-parse", "HEAD", cwd=out7["path"]) == c2
    assert sh("git", "rev-parse", "refs/heads/mr-7", cwd=g / "main") == local_wip


def test_apply_saves_state_dedupes_pings_and_records_history(gl_home):
    # apply: tombstones saved, pings grouped and de-duped across runs (FLAP), seen recorded
    plan = {
        "items": [],
        "create": [],
        "dismissed": {"k!1": {"ts": 1, "why": "manual"}},
        "seen": {"k!2": {"id": "w", "name": "n"}},
        "pings": [("@no-repo:a/b", "adopt: a/b has no repo in the ADE"), ("@adopt-busy", "adopt: busy")],
    }
    adopt.apply(plan)
    adopt.apply(plan)
    out = [json.loads(line) for line in (gl_home / "outbox.jsonl").read_text().splitlines()]
    assert [o["key"] for o in out] == ["@no-repo:a/b", "@adopt-busy"]
    assert json.loads(adopt.DISMISSED.read_text()) == plan["dismissed"]
    assert json.loads(adopt.STATE_FILE.read_text())["seen"] == plan["seen"]

    # a created workspace is logged, noted in its history, and remembered until a scan sees it
    mine = {"role": "author", "iid": 5, "title": "Fix x", "source_branch": "me/x", "repo": REPO_TN, "sha": "abc", "forge": "gitlab"}
    c = dict(mine, key="acme/widgets!5", project="acme/widgets", name="me/x")
    with mock.patch.object(adopt, "create", return_value={"path": "/w/x", "branch": "me/x"}):
        adopt.apply(plan | {"create": [c], "pings": []})
    assert json.loads(adopt.LOG.read_text().splitlines()[-1])["did"] == "adopt"
    assert "adopted !5 (author)" in greenlight.history_file("me/x").read_text()
    assert "acme/widgets!5" in json.loads(adopt.STATE_FILE.read_text())["created"]


def test_github_adopt_and_renamed_repo(gl_home):
    # GitHub: a GitHub-only ADE asks gh (never glab), and a PR gets `Merge #N` and Orca's --pr
    def pr(n):
        return {
            "number": n,
            "title": f"t{n}",
            "isDraft": False,
            "headRefName": f"b{n}",
            "headRefOid": f"s{n}",
            "updatedAt": "2033-05-18T03:00:00Z",
            "url": f"https://github.com/o/r/pull/{n}",
            "author": {"login": "octo"},
            "repository": {"nameWithOwner": "o/r"},
        }

    def fake_gh(*a):
        nodes = [pr(5), {**pr(6), "repository": {"nameWithOwner": "x/fork"}}] if "author:@me" in " ".join(a) else []
        return {"data": {"search": {"nodes": nodes}}}

    with (
        mock.patch.object(greenlight, "gh", side_effect=fake_gh),
        mock.patch.object(greenlight, "gl_all", side_effect=AssertionError("glab")),
        mock.patch.object(adopt, "repo_map", return_value={"o/r": {"id": "r-gh", "path": "/w/r", "forge": "github"}}),
        mock.patch.object(adopt, "checked_out", return_value={}),
    ):
        p = adopt.plan([], NOW - 60, now=NOW)
    assert [i["key"] for i in p["items"]] == ["o/r!5"] and not p["pings"]  # x/fork isn't in the ADE: ignored, no ping
    c = p["create"][0]
    assert (c["key"], c["role"], c["name"]) == ("o/r!5", "author", "b5"), c
    args = adopt.create_args(c, "origin/b5")
    assert "Merge #5: t5" in args and "--pr" not in args, args

    # a renamed GitHub repo: search says new/r, the ADE's remote (and greenlight's rows) still old/r
    def gh_ren(*a):
        if a[:2] == ("api", "repos/old/r"):
            return {"full_name": "new/r"}
        nodes = [{**pr(n), "repository": {"nameWithOwner": "new/r"}} for n in (7, 8)] if "author:@me" in " ".join(a) else []
        return {"data": {"search": {"nodes": nodes}}}

    with (
        mock.patch.object(greenlight, "gh", side_effect=gh_ren),
        mock.patch.object(adopt, "repo_map", return_value={"old/r": {"id": "r-gh", "path": "/w/r", "forge": "github"}}),
        mock.patch.object(adopt, "checked_out", return_value={}),
    ):
        p = adopt.plan([{"id": "w7", "name": "b7", "project": "old/r", "branch": "b7", "linked_mr": 7}], NOW - 60, now=NOW)
    assert {i["key"]: i["verdict"] for i in p["items"]} == {"old/r!7": "skip", "old/r!8": "create"}, p["items"]
    assert p["create"][0]["repo"]["path"] == "/w/r" and not p["pings"]
