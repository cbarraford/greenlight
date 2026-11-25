#!/usr/bin/env python3
"""Delete Orca workspaces whose work has landed or been abandoned.

    cleanup.py scan [--json]      # list candidates + kept/skipped workspaces, change nothing
    cleanup.py precheck           # Orca precheck: exit 0 = candidates exist (start the agent), 1 = skip
    cleanup.py apply [--json]     # remove every candidate via `orca worktree rm`

A workspace is a candidate when its MR/PR is merged or closed, or is someone else's MR that I've
approved (or, for a review checkout, one GitLab won't let me approve), or (no MR/PR at all) when the branch tip is already contained in origin/<default branch>.
Its MRs are the union of: the linked GitLab MR (or GitHub PR) when Orca has one, the MR named by the
workspace comment ("Review !N" / "Merge !N") or an `mr-N` branch, and every MR/PR whose source branch
is the branch's upstream name (falling back to the local name). The newest of them decides: if it is
open the workspace is kept; an older open MR behind a newer merged/closed one is stale and ignored.

Orca can list one worktree twice (one entry without the linked MR); entries are merged by path first,
so the stale twin can't keep a finished workspace alive. A detached HEAD is only considered when it
names an MR (review checkouts); every other detached tree is kept.

Nothing is removed unless nothing can be lost. Never touched: the repo's main checkout, detached
HEADs (e.g. cache trees), pinned workspaces, the default branch, and anything whose state
can't be observed. A candidate is skipped (and reported) when it has uncommitted or untracked
changes, a local tip that no MR/PR head, pushed MR version or remote contains, or a terminal that printed output
in the last ACTIVE_MINUTES.

A merged-looking branch with no MR could just be a fresh branch with no commits yet, so that case
also requires the branch to have been pushed under its own name, or the workspace to be idle for
FRESH_HOURS.

Stdlib only. Needs `glab` (gitlab.com) and `gh` (github.com) authenticated.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ade  # noqa: E402
import blocking  # noqa: E402
import diag  # noqa: E402
import external  # noqa: E402
import greenlight  # noqa: E402

ACTIVE_MINUTES = 30
FRESH_HOURS = 48
ME = greenlight.ME
LOG = greenlight.STATE / "log.jsonl"
NOW_MS = time.time() * 1000
MR_REF = re.compile(r"^(?:Review|Merge) [!#](\d+)\b")  # greenlight's workspace comments: "Review !1768: ..." / "Merge #12: ..."
MR_BRANCH = re.compile(r"^mr-(\d+)$")  # review checkouts of someone else's MR: `mr-1768`


def run(cmd, cwd=None, timeout=60, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def orca(*args):
    return greenlight.orca(*args, timeout=300)  # rm of a big tree is slow


def git(path, *args, check=True):
    return run(["git", "-C", path, *args], check=check).stdout.strip()


def is_ancestor(path, a, b):
    return run(["git", "-C", path, "merge-base", "--is-ancestor", a, b], check=False).returncode == 0


class Repo:
    """Per-repo facts shared by every worktree of that repo: forge, project, default branch."""

    def __init__(self, path):
        self.path = path
        self.error = None
        try:
            url = git(path, "remote", "get-url", "origin")
            run(["git", "-C", path, "fetch", "--quiet", "--prune", "origin"], timeout=180)
            head = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
            if not head:
                run(["git", "-C", path, "remote", "set-head", "origin", "--auto"], check=False)
                head = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            self.base = head  # e.g. origin/develop
            self.default = head.split("/", 1)[1]
        except Exception as e:  # no origin, offline, ...
            self.error = str(e)
            return
        u = url.removesuffix(".git")
        if u.startswith("git@"):
            host, proj = u[4:].split(":", 1)
        else:
            parsed = urllib.parse.urlparse(u)
            host, proj = (parsed.hostname or ""), parsed.path.lstrip("/")
        self.host, self.project, self._gh_me = host, proj, None
        if host not in ("gitlab.com", "github.com"):
            self.error = f"unsupported forge {host}"

    def mrs(self, branch, iids=()):
        """[{label, iid, state, sha}] for the given MR/PR numbers plus every MR/PR whose source branch is
        `branch` (skipped when None), deduped, newest first; state normalised to open/merged/closed."""
        if self.host == "gitlab.com":
            pid = urllib.parse.quote(self.project, safe="")
            ms = []
            for iid in iids:
                try:
                    ms.append(json.loads(run(["glab", "api", f"projects/{pid}/merge_requests/{iid}"]).stdout))
                except RuntimeError as e:
                    if "404" not in str(e):  # a comment can name an MR that isn't in this project
                        raise
            if branch:
                q = urllib.parse.urlencode({"source_branch": branch, "state": "all", "order_by": "created_at"})
                ms += json.loads(run(["glab", "api", f"projects/{pid}/merge_requests?{q}"]).stdout)
            norm = {"opened": "open", "locked": "open", "merged": "merged", "closed": "closed"}
            out = {}
            for m in ms:
                if m["iid"] in out:
                    continue
                state = norm[m["state"]]
                if state == "open" and m["author"]["username"] != ME:
                    # Review checkout: my approval finishes my part, same as a merge would.
                    ap = json.loads(run(["glab", "api", f"projects/{pid}/merge_requests/{m['iid']}/approvals"]).stdout)
                    blk = blocking.cached()
                    if blk is None or f"{self.project}!{m['iid']}" in blk:
                        pass  # still blocked on me, or blocking roster cache unavailable (fail closed): keep it
                    elif any(a["user"]["username"] == ME for a in ap.get("approved_by", [])):
                        state = "approved by me"
                    elif not ap.get("user_can_approve", True) and m["source_branch"] != branch:
                        state = "not mine to approve"  # review checkout, my commits are in it: nothing left for me
                out[m["iid"]] = {"label": f"!{m['iid']}", "iid": m["iid"], "state": state, "sha": m.get("sha")}
            return sorted(out.values(), key=lambda m: -m["iid"])
        fields = "number,state,headRefOid,headRefName,author,latestReviews"
        ps = [json.loads(run(["gh", "pr", "view", str(n), "-R", self.project, "--json", fields]).stdout) for n in iids]
        if branch:
            ps += json.loads(run(["gh", "pr", "list", "-R", self.project, "--head", branch, "--state", "all",
                                  "--json", fields]).stdout)
        me = None
        if any(p["state"] == "OPEN" for p in ps):
            if getattr(self, "_gh_me", None) is None:
                self._gh_me = json.loads(run(["gh", "api", "user"]).stdout)["login"]
            me = self._gh_me
        # gh states are OPEN / MERGED / CLOSED: a merged or closed PR finishes the workspace like a merged/closed MR.
        # Review checkout: my approval finishes my part. ponytail: GitHub has no "can't approve" case to mirror
        out = {p["number"]: {"label": f"#{p['number']}", "iid": p["number"], "sha": p["headRefOid"], "state":
                             "approved by me" if p["state"] == "OPEN" and (p["author"] or {}).get("login") != me
                             and any((r.get("author") or {}).get("login") == me and r.get("state") == "APPROVED" for r in p["latestReviews"])
                             else p["state"].lower()} for p in reversed(ps)}
        return sorted(out.values(), key=lambda m: -m["iid"])

    def pushed_heads(self, iid):
        """Every head the MR has had (GitLab diff versions). GitLab rebases an MR before merging and then
        deletes its branch, so a checkout of an earlier push is in neither the final head nor any remote."""
        if self.host != "gitlab.com":
            return []
        pid = urllib.parse.quote(self.project, safe="")
        vs = json.loads(run(["glab", "api", f"projects/{pid}/merge_requests/{iid}/versions?per_page=100"]).stdout)
        return [v["head_commit_sha"] for v in vs if v.get("head_commit_sha")]


def post_merge_left(path, project):
    """The lifecycle's steps after `merge` greenlight hasn't seen done for this workspace, or None. Unsure -> keep."""
    try:
        post = greenlight.post_merge(project)
    except Exception:  # noqa: BLE001 - a lifecycle we can't read: keep rather than delete mid-step
        return "post-merge steps (lifecycle unreadable)"
    if not post:
        return None
    try:
        rows = json.loads((greenlight.STATE / "last.json").read_text())["rows"]
    except (OSError, ValueError, KeyError):
        rows = []
    nxt = next((r["next"] for r in rows if os.path.normpath(r["path"]) == os.path.normpath(path)), {})
    if nxt.get("owner") == "done":
        return None
    return nxt["step"] if nxt.get("step") in post else ", ".join(post)  # not scanned since the merge: all of them


def mr_numbers(wt, branch, repo):
    """MR/PR numbers the workspace itself names: Orca's link, then its comment or an mr-N branch."""
    link = wt.get("linkedGitLabMR" if repo.host == "gitlab.com" else "linkedPR")
    ns = [link.get("number") if isinstance(link, dict) else link,  # newer Orca links a PR as {"number": 39, ...}
          *(int(m[1]) for m in [MR_REF.match(wt.get("comment") or ""), MR_BRANCH.match(branch)] if m)]
    return list(dict.fromkeys(n for n in ns if n))


def evaluate(wt, repo, busy):
    """-> (verdict, reason) with verdict in candidate / keep / skip."""
    path, branch = wt["path"], (wt.get("branch") or "").removeprefix("refs/heads/")
    if wt.get("isMainWorktree"):
        return "keep", "main checkout"
    if not branch and not MR_REF.match(wt.get("comment") or "") and not wt.get("linkedGitLabMR") and not wt.get("linkedPR"):
        return "keep", "detached HEAD"  # e.g. cache trees; a review checkout names its MR
    if wt.get("isPinned"):
        return "keep", "pinned"
    if (wt.get("comment") or "").startswith("Plan:"):
        return "keep", "a planner: the Plan view owns it"
    if not os.path.isdir(path):
        return "skip", "path missing on disk"
    if repo.error:
        return "skip", f"repo: {repo.error}"
    if branch and branch == repo.default:
        return "keep", "default branch"

    remote_name = branch and git(path, "config", f"branch.{branch}.merge", check=False).removeprefix("refs/heads/")
    pushed_as_self = bool(branch) and remote_name == branch
    lookup = remote_name if remote_name and remote_name != repo.default else branch
    tip = git(path, "rev-parse", "HEAD")

    try:
        mrs = repo.mrs(lookup or None, mr_numbers(wt, branch, repo))
    except Exception as e:
        return "skip", f"MR lookup failed: {e}"

    if mrs:
        newest = mrs[0]  # an open MR older than a merged one for the same work is stale; a closed duplicate never supersedes an open MR
        open_mrs = [m for m in mrs if m["state"] == "open"]
        if newest["state"] == "open" or (open_mrs and newest["state"] != "merged"):
            active = newest if newest["state"] == "open" else open_mrs[0]
            return "keep", f"MR {active['label']} is open"
        if newest["state"] == "merged" and (left := post_merge_left(path, repo.project)):
            return "keep", f"MR {newest['label']} merged, still to do: {left}"
        reason = f"MR {newest['label']} {newest['state']}"
        if open_mrs:
            reason += f" (older {', '.join(m['label'] for m in open_mrs)} open, stale)"
        # Local commits must already be on the forge: in a finished MR head (squash merges delete the remote
        # branch), in an earlier pushed version of the MR (GitLab rebases before merging), or in a remote.
        finished = [m for m in mrs if m["state"] != "open"]
        contains = lambda sha: sha and (tip == sha or is_ancestor(path, tip, sha))
        safe = (any(contains(m["sha"]) for m in finished)
                or not git(path, "rev-list", "-1", tip, "--not", "--remotes")
                or any(contains(sha) for m in finished for sha in repo.pushed_heads(m["iid"])))
        if not safe:
            return "skip", f"{reason}, but local commits aren't in the MR head or any remote"
    elif not branch:
        return "keep", "detached HEAD, its MR wasn't found"
    else:
        comment = (wt.get("comment") or "").strip()
        if greenlight.PARKED_RE.search(comment) or greenlight.AUTOMATION_RE.search(comment):
            return "keep", "no MR/PR, parked or automation workspace"
        if m_rev := greenlight.REVISIT_RE.search(comment):
            if m_rev.group(1) >= time.strftime("%Y-%m-%d"):
                return "keep", f"no MR/PR, revisit scheduled for {m_rev.group(1)}"
        if not is_ancestor(path, tip, repo.base):
            return "keep", "no MR/PR, branch not merged"
        idle_h = (NOW_MS - (wt.get("lastActivityAt") or NOW_MS)) / 3.6e6
        if not pushed_as_self and idle_h < FRESH_HOURS:
            return "keep", f"no MR/PR, tip in {repo.base} but never pushed and idle only {idle_h:.0f}h (fresh branch?)"
        reason = f"no MR/PR, branch merged into {repo.base}"

    if git(path, "status", "--porcelain", "--untracked-files=normal"):
        return "skip", f"{reason}, but has uncommitted/untracked changes"
    if any(i in busy for i in wt.get("ids") or [wt["id"]]):
        return "skip", f"{reason}, but a terminal was active in the last {ACTIVE_MINUTES}m"
    if (ext := outside_agent(path)):
        return "skip", f"{reason}, but {ext} runs in it outside Greenlight"
    return "candidate", reason


ALL_PATHS: list = []  # every worktree this run listed (scan() fills it)


def outside_agent(path, fresh=False, paths=None):
    """The agent another ADE or a plain terminal runs in path, under the native engine (#195 passive coexistence),
    else None. Orca's own agents are in its terminal list (the busy check)."""
    if not ade.is_native():
        return None
    try:  # against every worktree: whether one of our agents is in its own pane's worktree depends on the nested ones
        every, p = [*(ALL_PATHS if paths is None else paths), path], path.rstrip("/")
        owned = external.by_worktree(every, external.agents(fresh=fresh, strict=True))
        # at or under it: removing it removes the worktrees nested inside it too
        found = [a for w, v in owned.items() if w == p or w.startswith(p + "/") for a in v]
    except external.Unknown:
        return "an agent (the process check failed, so it can't be ruled out)"
    return found[0]["agent"] if found else None


def dedupe(wts):
    """One entry per worktree path. Orca can list a worktree twice, one entry without its linked MR; merge
    the twins so the richer facts win (linked MR, comment, pinned) and every id counts for terminal activity."""
    groups = {}
    for w in wts:
        groups.setdefault(os.path.normpath(w["path"]), []).append(w)
    out = []
    for group in groups.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        group.sort(key=lambda w: not (w.get("linkedGitLabMR") or w.get("linkedPR")))
        w = dict(group[0])
        for k in ("linkedGitLabMR", "linkedPR", "comment", "branch", "displayName"):
            w[k] = next((g[k] for g in group if g.get(k)), w.get(k))
        for k in ("isPinned", "isMainWorktree"):
            w[k] = any(g.get(k) for g in group)
        w["lastActivityAt"] = max((g.get("lastActivityAt") or 0) for g in group) or None
        w["ids"] = [g["id"] for g in group]
        out.append(w)
    return out


def scan():
    wts = dedupe(orca("worktree", "list", "--limit", "10000")["worktrees"])
    ALL_PATHS[:] = [w["path"] for w in wts if w.get("path")]
    cutoff = time.time() * 1000 - ACTIVE_MINUTES * 60_000
    busy = {t["worktreeId"] for t in orca("terminal", "list", "--limit", "10000")["terminals"]
            if (t.get("lastOutputAt") or 0) > cutoff}
    # Group by repo; worktree list gives the repo id, the main checkout gives its path.
    roots = {w["repoId"]: w["path"] for w in wts if w.get("isMainWorktree")}
    for r in orca("repo", "list")["repos"]:
        roots.setdefault(r["id"], r["path"])
    with ThreadPoolExecutor(8) as ex:
        repos = dict(zip(roots, ex.map(Repo, roots.values())))

    def one(w):
        repo = repos.get(w["repoId"])
        tip = git(w["path"], "rev-parse", "HEAD", check=False) if os.path.isdir(w["path"]) else ""
        try:
            verdict, reason = evaluate(w, repo, busy) if repo else ("skip", "unknown repo")
        except Exception as e:
            verdict, reason = "skip", f"error: {e}"
        return {"id": w["id"], "ids": w.get("ids") or [w["id"]], "name": w.get("displayName") or Path(w["path"]).name,
                "path": w["path"], "tip": tip, "branch": (w.get("branch") or "").removeprefix("refs/heads/"),
                "verdict": verdict, "reason": reason}

    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, wts))


def log(entry):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), **entry}) + "\n")
    diag.event("cleanup", ws_h=diag.h(entry.get("name")), ok=entry.get("removed"),  # not reason: it's text with names in it
               msg_class=diag.classify(str(entry["error"])) if entry.get("error") else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "precheck", "apply"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        rows = scan()
    except Exception as e:
        print(f"scan failed: {e}", file=sys.stderr)
        sys.exit(1 if a.cmd == "precheck" else 2)
    cands = [r for r in rows if r["verdict"] == "candidate"]

    if a.cmd == "precheck":
        print(f"{len(cands)} workspace(s) to delete")
        sys.exit(0 if cands else 1)

    if a.cmd == "apply":
        for r in cands:
            try:
                # Re-verify worktree state immediately before deletion to close the scan→apply TOCTOU window.
                if os.path.isdir(r["path"]):
                    cur_tip = git(r["path"], "rev-parse", "HEAD", check=False)
                    if r.get("tip") and cur_tip and cur_tip != r["tip"]:
                        raise RuntimeError("HEAD changed since scan")
                    if git(r["path"], "status", "--porcelain", "--untracked-files=normal", check=False):
                        raise RuntimeError("uncommitted/untracked changes appeared since scan")
                    cutoff = time.time() * 1000 - ACTIVE_MINUTES * 60_000
                    live_busy = {t["worktreeId"] for t in orca("terminal", "list", "--limit", "10000")["terminals"]
                                 if (t.get("lastOutputAt") or 0) > cutoff}
                    if any(i in live_busy for i in r.get("ids") or [r["id"]]):
                        raise RuntimeError("terminal became active since scan")
                    if outside_agent(r["path"], fresh=True):
                        raise RuntimeError("an agent outside Greenlight started in it since scan")
                orca("worktree", "rm", "--worktree", f"id:{r['id']}")
                r["removed"] = True
            except Exception as e:
                r["removed"], r["error"] = False, str(e)[-300:]
            log({k: r[k] for k in ("name", "path", "branch", "reason", "removed")} | ({"error": r["error"]} if not r["removed"] else {}))

    if a.json:
        print(json.dumps(rows, indent=1))
        return
    for verdict in ("candidate", "skip", "keep"):
        group = [r for r in rows if r["verdict"] == verdict]
        if not group:
            continue
        title = {"candidate": "DELETED" if a.cmd == "apply" else "WOULD DELETE", "skip": "SKIPPED (needs a human)",
                 "keep": "KEPT"}[verdict]
        print(f"\n## {title} ({len(group)})")
        for r in sorted(group, key=lambda r: r["path"]):
            mark = "" if verdict != "candidate" or a.cmd != "apply" else ("✓ " if r["removed"] else "✗ ")
            print(f"- {mark}{r['name']} [{r['branch'] or '-'}] — {r['reason']}" + (f" — {r['error']}" if r.get("error") else ""))


if __name__ == "__main__":
    diag.install("cleanup")
    main()
