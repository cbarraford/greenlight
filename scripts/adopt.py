#!/usr/bin/env python3
"""Give every open MR/PR of mine, one I'm a requested reviewer on, or (config `blocking`) one I'm blocking (see
blocking.py), a workspace (so it gets a hex).

    adopt.py scan [--json]            # candidates and why each is skipped; changes nothing
    adopt.py precheck                 # exit 0 = at least one workspace to create, 1 = none
    adopt.py apply [--json] [--cap N] # create them, write tombstones, ping what needs me
    adopt.py dismiss <project>!<iid>... # tombstone them: never adopted while open

A candidate is an open MR across all of GitLab with me as author or reviewer, plus every MR
blocking.py says is blocked on me, for any reason (re-read from GitLab, kept only while it's open). If
that scan fails, adopt runs on the author/reviewer queries alone and keeps any tombstone it can't recheck.
Guards, in order: covered (a workspace in ~/.greenlight/last.json already has it, by MR or by source branch)
→ tombstoned (its workspace was deleted while the MR was open; a review re-request, or a block on me
after the MR changed, clears it) → someone else's MR I've approved (or can't approve) and am not blocking → someone else's
draft → no repo in the ADE for its project (pinged once per project) → branch checked out in another worktree
(pinged) → at most CAP creates per run.

Authored MRs check out their source branch tracking origin; review MRs get `mr-<N>` from the MR head
ref. Goal "Merge !N: <title>" / "Review !N: <title>", no agent, `--setup skip`: the lifecycle decides
what happens next. greenlight.py stays read-only; this file owns ~/.greenlight/dismissed.json and adopt.json.

Stdlib only. Needs `glab` (GitLab repos) and/or `gh` (GitHub repos) authenticated; blocking.py is GitLab-only.
"""

import argparse
import calendar
import contextlib
import functools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blocking  # noqa: E402
import diag  # noqa: E402
import greenlight  # noqa: E402

ME = greenlight.ME
CAP = greenlight.CONFIG["adopt"].get("cap", 10)
ROLES = {"author": "author_username", "reviewer": "reviewer_username"}
DISMISSED = greenlight.STATE / "dismissed.json"  # {"<project>!<iid>": {"ts", "why"}}: deleted while the MR was open
STATE_FILE = greenlight.STATE / "adopt.json"  # {"seen": {key: {id, name}}, "created": {key: ts}}
LOG = greenlight.STATE / "log.jsonl"


def git(path, *args, check=True):
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=180)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout.strip()


def load(f, default):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return default


def save(f, data):
    import uuid
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, f)


_BAD_BRANCH_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


def _valid_branch(branch):
    """True when `branch` is a safe git branch name (no leading `-`, control/whitespace, `..`, or invalid ref chars)."""
    if not isinstance(branch, str) or not branch or branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".")):
        return False
    if branch == "@" or _BAD_BRANCH_RE.search(branch):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in branch.split("/"))


# --- project mapping ---------------------------------------------------------------------------

def parse_remote(url):
    """git@host:a/b.git | https://user@host/a/b | ssh://git@host:22/a/b | host/a/b -> (host, "a/b")."""
    u = (url or "").strip().removesuffix("/").removesuffix(".git")
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+)$", u)
    return (m[1], m[2]) if m else (None, None)


def repo_map():
    """{project path: {"id", "path", "forge"}} for every gitlab.com/github.com repo the ADE knows, worktrees or not.
    ponytail: keyed by path alone, so the same owner/repo on both forges collides (first one wins)"""
    out = {}
    for r in greenlight.local_first(greenlight.orca("repo", "list")["repos"]):  # first one wins: this machine's (#160)
        if r.get("path") and not os.path.isdir(r["path"]):  # a moved/renamed checkout Orca still lists shares its remote
            continue
        key = (r.get("gitRemoteIdentity") or {}).get("canonicalKey")
        if not key and r.get("path"):  # cmux/herdr/T3 shims, or an Orca repo without the field
            key = git(r["path"], "remote", "get-url", "origin", check=False)
        host, proj = parse_remote(key)
        if host in ("gitlab.com", "github.com"):
            out.setdefault(proj, {"id": r["id"], "path": r["path"], "forge": host.split(".")[0]})
    return out


def checked_out(path):
    """{branch: worktree path} for every worktree of the repo at path."""
    out, wt = {}, None
    for line in git(path, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            wt = line[9:]
        elif line.startswith("branch "):
            out[line[7:].removeprefix("refs/heads/")] = wt
    return out


# --- candidates ------------------------------------------------------------------------------

def project_of(m):
    full = (m.get("references") or {}).get("full") or ""
    if "!" in full:
        return full.rsplit("!", 1)[0]
    return m["web_url"].split("gitlab.com/", 1)[1].split("/-/", 1)[0]


def cand(m):
    proj = project_of(m)
    mine = m["author"]["username"] == ME
    return {"key": f"{proj}!{m['iid']}", "project": proj, "project_id": m["project_id"], "iid": m["iid"], "forge": "gitlab",
            "title": m["title"], "source_branch": m["source_branch"], "draft": bool(m.get("draft")),
            "role": "author" if mine else "review", "author": m["author"]["username"],
            "updated_at": m.get("updated_at") or "", "sha": m.get("sha"), "web_url": m["web_url"]}


GH_SEARCH = """query($q: String!) { search(type: ISSUE, first: 100, query: $q) { nodes { ... on PullRequest {
  number title isDraft headRefName headRefOid updatedAt url author { login } repository { nameWithOwner } } } } }"""
GH_ROLES = {"author": "author:@me", "reviewer": "review-requested:@me"}
gh_login = functools.cache(lambda: greenlight.gh("api", "user")["login"])  # ME may be the GitLab name


def gh_cands(role):
    """Open PRs I wrote or was asked to review, in cand()'s shape. The key keeps `!` like GitLab's (adopt.json,
    dismissed.json and greenlight's rows all use <project>!<n>)."""
    d = greenlight.gh("api", "graphql", "-f", f"q=is:pr is:open archived:false {GH_ROLES[role]}", "-f", f"query={GH_SEARCH}")
    return [{"key": f"{p['repository']['nameWithOwner']}!{p['number']}", "project": p["repository"]["nameWithOwner"],
             "project_id": None, "iid": p["number"], "forge": "github", "title": p["title"], "source_branch": p["headRefName"],
             "draft": p["isDraft"], "role": "author" if role == "author" else "review", "author": (p["author"] or {}).get("login"),
             "updated_at": p["updatedAt"], "sha": p["headRefOid"], "web_url": p["url"]}
            for p in d["data"]["search"]["nodes"] if p]


def gh_renamed(repos):
    """{GitHub's current owner/name: the ADE's} for GitHub repos renamed since they were cloned. Search reports a PR
    under the new name, the ADE's remote (so greenlight's rows) keeps the old one, and GitHub redirects the old to the new."""
    out = {}
    for proj, r in repos.items():
        if r["forge"] != "github":
            continue
        try:
            name = greenlight.gh("api", f"repos/{proj}")["full_name"]
        except Exception:  # noqa: BLE001 - gone or no access: its PRs stay unmatched, as before
            continue
        if name.lower() != proj.lower():
            out[name] = proj
    return out


def candidates(forges=("gitlab", "github")):
    """-> (open MRs/PRs I wrote, was asked to review, or am blocking, deduped, newest activity first; whether the
    blocking scan answered). Each MR I'm blocking carries c["block"] = its reason. Only the forges the ADE has
    repos on are asked, so a GitHub-only setup never needs glab (and vice versa)."""
    seen = {}
    for r in greenlight.CONFIG["adopt"].get("roles", ROLES):
        for c in ([cand(m) for m in greenlight.gl_all(f"merge_requests?scope=all&state=opened&{ROLES[r]}={ME}")]
                  if "gitlab" in forges else []) + (gh_cands(r) if "github" in forges else []):
            seen.setdefault(c["key"], c)
    blocks = blocking.blocks() if "gitlab" in forges else {}
    for key, b in sorted((blocks or {}).items()):
        if key not in seen:
            proj, iid = key.rsplit("!", 1)
            try:
                m = greenlight.gl(f"projects/{greenlight.pid(proj)}/merge_requests/{iid}")
            except Exception:  # noqa: BLE001 - gone or no access since the scan: skip it
                continue
            if m.get("state") != "opened":  # the scan is cached for minutes: it may have merged since
                continue
            seen[key] = cand(m)
        seen[key]["block"] = b["reason"]
    return sorted(seen.values(), key=lambda c: c["updated_at"], reverse=True), blocks is not None


def iso_ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) if s else 0


def review_done(c):
    """Why someone else's MR needs no review workspace from me (approved, or GitLab won't let me), else None."""
    if c["forge"] == "github":  # GitHub never blocks approving a PR with your commits in it
        revs = greenlight.gh("pr", "view", str(c["iid"]), "-R", c["project"], "--json", "latestReviews")["latestReviews"]
        return "approved by me" if any((r.get("author") or {}).get("login") == gh_login() and r["state"] == "APPROVED" for r in revs) else None
    ap = greenlight.gl(f"projects/{c['project_id']}/merge_requests/{c['iid']}/approvals")
    if any(a["user"]["username"] == ME for a in ap.get("approved_by", [])):
        return "approved by me"
    if not ap.get("user_can_approve", True):
        return "I can't approve it (my commits are in it)"


def rerequested(c, tomb):
    """A tombstone clears when the MR changed after it and either I'm blocking it or (a review) I'm back to
    unreviewed."""
    if iso_ts(c["updated_at"]) <= tomb["ts"]:
        return False
    if c.get("block"):
        return True
    if c["role"] != "review":
        return False
    if c["forge"] == "github":
        rr = greenlight.gh("pr", "view", str(c["iid"]), "-R", c["project"], "--json", "reviewRequests")["reviewRequests"]
        return any(r.get("login") == gh_login() for r in rr)
    revs = greenlight.gl(f"projects/{c['project_id']}/merge_requests/{c['iid']}/reviewers")
    return any(r["user"]["username"] == ME and r.get("state") == "unreviewed" for r in revs)


# --- what's already covered ------------------------------------------------------------------

def interval_s():
    """The greenlight automation's period (`*/N` minutes), default 30 min."""
    sched = load(greenlight.STATE / "automations.json", {}).get("greenlight", {}).get("schedule", "")
    m = re.match(r"\*/(\d+) ", sched)
    return int(m[1]) * 60 if m else 1800


def current_rows():
    """-> (rows, ts) from last.json, or a fresh scan when that's older than one automation interval."""
    last = load(greenlight.STATE / "last.json", {})
    if last.get("rows") is not None and time.time() - last.get("ts", 0) <= interval_s():
        return last["rows"], last["ts"]
    res = greenlight.scan(save=False)
    return res["rows"], res["ts"]


def row_keys(rows):
    """{"<project>!<iid>": {id, name}} for rows tied to an open MR."""
    out = {}
    for r in rows:
        iid = (r.get("mr") or {}).get("iid") or r.get("linked_mr")
        if iid and (r.get("mr") or {}).get("state", "opened") == "opened":
            out[f"{r['project']}!{iid}"] = {"id": r["id"], "name": r["name"]}
    return out


def removed_by_cleanup(name):
    for line in (LOG.read_text().splitlines() if LOG.exists() else []):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("name") == name and e.get("removed"):
            return True
    return False


# --- the plan --------------------------------------------------------------------------------

def plan(rows=None, rows_ts=None, cap=CAP, now=None):
    """Decide everything, change nothing. -> {"items", "create", "dismissed", "seen", "pings"}."""
    now = now or time.time()
    if rows is None:
        rows, rows_ts = current_rows()
    rows_ts = rows_ts or now
    st = load(STATE_FILE, {})
    seen_before, created = st.get("seen", {}), st.get("created", {})
    cur = row_keys(rows)
    ids = {r["id"] for r in rows}
    covered_branch = {(r["project"], r["branch"]) for r in rows}
    repos, busy_cache, items, create = repo_map(), {}, [], []
    cands, blocks_ok = candidates({r["forge"] for r in repos.values()})
    if renamed := (gh_renamed(repos) if any(c["forge"] == "github" and c["project"] not in repos for c in cands) else {}):
        for c in cands:  # back to the ADE's name, the one greenlight's rows use, so a renamed repo's PR still reads as covered
            if c["forge"] == "github" and c["project"] in renamed:
                c.update(project=renamed[c["project"]], key=f"{renamed[c['project']]}!{c['iid']}")
    # ponytail: GitHub PRs only in repos the ADE has. Old PRs on forks scatter across GitHub, and a no-repo ping each is noise
    cands = [c for c in cands if c["forge"] != "github" or c["project"] in repos]
    open_keys = {c["key"] for c in cands}

    # Tombstones: drop ones whose MR closed; add workspaces that vanished while their MR stayed open.
    # Without the blocking scan, an MR missing from open_keys may be a blocked-only one that's still open: keep
    # its tombstone and its seen entry until a run that can tell.
    dismissed = {k: v for k, v in load(DISMISSED, {}).items() if k in open_keys or not blocks_ok}
    for k, v in seen_before.items():
        if k in open_keys and k not in cur and v["id"] not in ids and k not in dismissed:
            dismissed[k] = {"ts": int(now), "why": "cleanup" if removed_by_cleanup(v["name"]) else "manual"}

    for c in cands:
        k = c["key"]
        verdict, reason = "skip", None
        if k in cur or (c["project"], c["source_branch"]) in covered_branch:
            reason = "covered: a workspace already has it"
        elif created.get(k, 0) > rows_ts:
            reason = "covered: created by an earlier adopt run, not scanned yet"
        elif k in dismissed and not rerequested(c, dismissed[k]):
            reason = f"tombstoned ({dismissed[k]['why']}): its workspace was removed"
        else:
            if k in dismissed:
                c["cleared"] = True  # re-requested: the tombstone goes on apply
            if c["role"] == "review" and not c.get("block") and (done := review_done(c)):
                reason = done
            elif c["role"] == "review" and c["draft"]:
                reason = "someone else's draft"
            elif c["project"] not in repos:
                verdict, reason = "no-repo", f"no repo in the ADE for {c['project']}"
            else:
                repo = repos[c["project"]]
                host_info = greenlight.resolve_repo_host(c["project"], path=repo.get("path"))
                if sys.platform != "darwin" and host_info["target"] == "mac":
                    verdict, reason = "skip", f"{c['project']} runs on Mac (host={host_info['mode']})"
                else:
                    c["repo"] = repo
                    if repo["path"] not in busy_cache:
                        busy_cache[repo["path"]] = checked_out(repo["path"])
                    busy = busy_cache[repo["path"]]
                    want_b = f"mr-{c['iid']}" if c["role"] == "review" else c["source_branch"]
                    if c["role"] == "author" and not _valid_branch(want_b):
                        verdict, reason = "skip", "invalid source branch name"
                    else:
                        hit = want_b if want_b and want_b in busy else None
                        if hit:
                            verdict, reason = "busy", f"{hit} is checked out in {busy[hit]}"
                        elif len(create) >= cap:
                            verdict, reason = "deferred", f"over the {cap}-per-run cap: next run"
                        else:
                            verdict, reason = "create", f"{c['role']} MR with no workspace" + (
                                f" (I'm blocking it: {c['block']})" if c.get("block") else "")
                            c["name"] = ws_name(c)
                            create.append(c)
        c.update(verdict=verdict, reason=reason)
        items.append(c)

    pings = []
    no_repo = {}
    for i in items:
        if i["verdict"] == "no-repo":
            no_repo.setdefault(i["project"], []).append(i)
    for proj, group in sorted(no_repo.items()):
        mrs = " ".join(greenlight.ref(i["forge"], i["iid"]) for i in group)
        pings.append((f"@no-repo:{proj}", f"adopt: {proj} has no repo in the ADE, so {len(group)} open MR(s) get no "
                      f"workspace: {mrs}. Add the repo to the ADE?\n{group[0]['web_url']}\n"
                      "Options: add the repo | ignore its MRs"))
    busy = [i for i in items if i["verdict"] == "busy"]
    if busy:
        pings.append(("@adopt-busy", "adopt: can't create a workspace, the branch is checked out elsewhere: "
                      + "; ".join(f"{i['key']} ({i['reason']})" for i in busy) + "\nOptions: free the branches | leave them"))
    dismissed = {k: v for k, v in dismissed.items() if not any(i["key"] == k and i.get("cleared") for i in items)}
    seen = cur if blocks_ok else {**{k: v for k, v in seen_before.items() if k not in open_keys}, **cur}
    return {"items": items, "create": create, "dismissed": dismissed, "seen": seen, "pings": pings}


# --- creating --------------------------------------------------------------------------------

def ws_name(c):
    return c["source_branch"] if c["role"] == "author" else f"mr-{c['iid']}"


def create_args(c, base, name=None):
    name = name or ws_name(c)
    goal = f"{'Merge' if c['role'] == 'author' else 'Review'} {greenlight.ref(c['forge'], c['iid'])}: {c['title']}"
    repo = c["repo"]["id"]
    return ["worktree", "create", "--name", name, "--repo", repo, "--base-branch", base, "--comment", goal, "--setup", "skip"]


def _ref_sha(path, branch):
    p = subprocess.run(["git", "-C", path, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                       capture_output=True, text=True, timeout=60)
    return p.stdout.strip() if p.returncode == 0 else ""


def _prepare_branch(path, want, base):
    """Handle a preserved local branch (from `--no-delete-branch`) before Worktrunk creates a worktree.

    Returns `(create_name, restore_sha)`. If `want` already exists locally and is not checked out:
    - when it is an ancestor of `base`, delete the stale ref so Worktrunk can create `want` cleanly
      (and return `old_sha` so callers can restore it if creation fails);
    - when it has diverged commits, leave `refs/heads/<want>` intact and pick a free `<want>-N` name.
    """
    if not base or base.startswith("-") or not os.path.isdir(path):
        return want, None
    old_sha = _ref_sha(path, want)
    if not old_sha or want in checked_out(path):
        return want, None
    anc = subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", old_sha, base],
                         capture_output=True, text=True, timeout=60)
    if anc.returncode == 0:
        subprocess.run(["git", "-C", path, "branch", "-D", "--", want],
                       capture_output=True, text=True, timeout=60, check=True)
        return want, old_sha
    n = 2
    while _ref_sha(path, f"{want}-{n}"):
        n += 1
    return f"{want}-{n}", None


def ensure_branch(path, want, upstream=None):
    """Put the new worktree on local branch `want` (Orca may pick `<name>-2`), tracking upstream."""
    if not _valid_branch(want):
        raise ValueError(f"invalid branch name: {want!r}")
    if upstream is not None and not _valid_branch(upstream):
        raise ValueError(f"invalid upstream branch name: {upstream!r}")
    cur = git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if cur != want:
        exists = git(path, "rev-parse", "--verify", "-q", f"refs/heads/{want}", check=False)
        if not exists:
            git(path, "switch", "-c", want)
        elif subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", f"refs/heads/{want}", "HEAD"],
                            capture_output=True, text=True, timeout=60).returncode == 0:
            git(path, "switch", "-C", want)  # want is behind HEAD: moving it loses nothing
        else:
            want = cur  # a diverged local branch of that name: leave it alone, keep Orca's
        if cur and cur != want:
            git(path, "branch", "-D", "--", cur, check=False)  # same commit as HEAD
    if upstream:
        git(path, "branch", f"--set-upstream-to=origin/{upstream}", "--", want)
    return want


def create(c):
    path = c["repo"]["path"]
    if c["role"] == "author":
        sb = c.get("source_branch")
        if not _valid_branch(sb):
            raise ValueError(f"invalid source branch name: {sb!r}")
        git(path, "fetch", "origin", "--", f"refs/heads/{sb}")
        base, want, upstream = f"origin/{sb}", sb, sb
    else:
        want, upstream = f"mr-{c['iid']}", None
        head = f"refs/pull/{c['iid']}/head" if c["forge"] == "github" else f"refs/merge-requests/{c['iid']}/head"
        git(path, "fetch", "origin", head)
        base = git(path, "rev-parse", "FETCH_HEAD")
    create_name, restore_sha = _prepare_branch(path, want, base)
    try:
        res = greenlight.orca(*create_args(c, base, name=create_name))
    except Exception:
        if restore_sha:
            subprocess.run(["git", "-C", path, "branch", "--", want, restore_sha],
                           capture_output=True, text=True, timeout=60)
        raise
    wt = (res.get("worktree") or res)
    branch = ensure_branch(wt["path"], want, upstream) if wt.get("path") else None
    return {"path": wt.get("path"), "branch": branch}


def log(entry):
    LOG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(LOG.parent, 0o700)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"ts": int(time.time()), **entry}) + "\n")
    diag.event("adopt", did=str(entry.get("did") or "").replace(" ", "_"), ws_h=diag.h(entry.get("ws")), mr_h=diag.h(entry.get("mr")),
               role=entry.get("role"), msg_class=diag.classify(str(entry["error"])) if entry.get("error") else None)


def apply(p):
    import fcntl
    now = int(time.time())
    DISMISSED.parent.mkdir(parents=True, exist_ok=True)
    with (DISMISSED.parent / "adopt.lock").open("a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        cleared = {i["key"] for i in p.get("items") or () if i.get("cleared")}
        cur_d = {k: v for k, v in load(DISMISSED, {}).items() if k not in cleared}
        save(DISMISSED, cur_d | p["dismissed"])
    st = load(STATE_FILE, {})
    created = {k: t for k, t in st.get("created", {}).items() if k not in p["seen"]}
    for c in p["create"]:
        try:
            out = create(c)
            c.update(created=True, **out)
            created[c["key"]] = now
            r = greenlight.ref(c["forge"], c["iid"])
            log({"ws": c["name"], "mr": r, "did": "adopt", "project": c["project"], "role": c["role"]})
            greenlight.note(c["name"], f"adopted {r} ({c['role']}): no workspace existed")
        except Exception as e:
            c.update(created=False, error=str(e)[-300:])
            log({"ws": c["name"], "mr": greenlight.ref(c["forge"], c["iid"]), "did": "adopt failed", "error": c["error"]})
    save(STATE_FILE, {"seen": p["seen"], "created": created})
    with contextlib.redirect_stdout(sys.stderr):  # ping's "skipped" note mustn't pollute --json
        for key, text in p["pings"]:
            greenlight.ping(text, key)


def main():
    ap = argparse.ArgumentParser()
    if sys.argv[1:2] == ["dismiss"]:  # adopt.py dismiss <project>!<iid>...: never adopt these (the no-repo ping's "ignore")
        import fcntl
        DISMISSED.parent.mkdir(parents=True, exist_ok=True)
        with (DISMISSED.parent / "adopt.lock").open("a+") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            save(DISMISSED, load(DISMISSED, {}) | {k: {"ts": int(time.time()), "why": "ignored"} for k in sys.argv[2:]})
        return
    ap.add_argument("cmd", choices=["scan", "precheck", "apply"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cap", type=int, default=CAP)
    a = ap.parse_args()
    try:
        p = plan(cap=a.cap)
    except Exception as e:
        print(f"adopt plan failed: {e}", file=sys.stderr)
        sys.exit(1 if a.cmd == "precheck" else 2)
    if a.cmd == "precheck":
        print(f"{len(p['create'])} workspace(s) to adopt")
        sys.exit(0 if p["create"] else 1)
    if a.cmd == "apply":
        apply(p)
    items = p["items"]
    failed = a.cmd == "apply" and any(i.get("created") is False for i in p["create"])
    if a.json:
        print(json.dumps([{k: v for k, v in i.items() if k != "repo"} for i in items], indent=1))
        if failed:
            sys.exit(1)
        return
    titles = {"create": "CREATED" if a.cmd == "apply" else "WOULD CREATE", "deferred": "DEFERRED (cap)",
              "busy": "BRANCH BUSY (pinged)", "no-repo": "NO ADE REPO (pinged)", "skip": "SKIPPED"}
    for verdict, title in titles.items():
        group = [i for i in items if i["verdict"] == verdict]
        if not group:
            continue
        print(f"\n## {title} ({len(group)})")
        for i in group:
            mark = "" if verdict != "create" or a.cmd != "apply" else ("✓ " if i.get("created") else "✗ ")
            print(f"- {mark}{i['key']} [{i['role']}{'/' + i['block'] if i.get('block') else ''}] {i['title'][:60]} — {i['reason']}"
                  + (f" — {i['error']}" if i.get("error") else ""))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    diag.install("adopt")
    main()
