//! Port of scripts/cleanup.py. Delete Orca workspaces whose work has landed or been abandoned.
//!
//!     cleanup scan [--json]      # list candidates + kept/skipped workspaces, change nothing
//!     cleanup precheck           # Orca precheck: exit 0 = candidates exist (start the agent), 1 = skip
//!     cleanup apply [--json]     # remove every candidate via `orca worktree rm`
//!
//! A workspace is a candidate when its MR/PR is merged or closed, or is someone else's MR that I've
//! approved (or, for a review checkout, one GitLab won't let me approve), or (no MR/PR at all) when the branch tip is already contained in origin/<default branch>.
//! Its MRs are the union of: the linked GitLab MR (or GitHub PR) when Orca has one, the MR named by the
//! workspace comment ("Review !N" / "Merge !N") or an `mr-N` branch, and every MR/PR whose source branch
//! is the branch's upstream name (falling back to the local name). The newest of them decides: if it is
//! open the workspace is kept; an older open MR behind a newer merged/closed one is stale and ignored.
//!
//! Orca can list one worktree twice (one entry without the linked MR); entries are merged by path first,
//! so the stale twin can't keep a finished workspace alive. A detached HEAD is only considered when it
//! names an MR (review checkouts); every other detached tree is kept.
//!
//! Nothing is removed unless nothing can be lost. Never touched: the repo's main checkout, detached
//! HEADs (e.g. cache trees), pinned workspaces, the default branch, and anything whose state
//! can't be observed. A candidate is skipped (and reported) when it has uncommitted or untracked
//! changes, a local tip that no MR/PR head, pushed MR version or remote contains, or a terminal that printed output
//! in the last ACTIVE_MINUTES.
//!
//! A merged-looking branch with no MR could just be a fresh branch with no commits yet, so that case
//! also requires the branch to have been pushed under its own name, or the workspace to be idle for
//! FRESH_HOURS.
//!
//! Needs `glab` (gitlab.com) and `gh` (github.com) authenticated.
use crate::base::{self, STATE};
use crate::blocking::{self, k, list};
use crate::py::{self, Res};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::io::Write;
use std::path::Path;
use std::sync::LazyLock;

const ACTIVE_MINUTES: f64 = 30.0;
const FRESH_HOURS: f64 = 48.0;
// greenlight's workspace comments: "Review !1768: ..." / "Merge #12: ..."
static MR_REF: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:Review|Merge) [!#](\d+)\b").unwrap());
static MR_BRANCH: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^mr-(\d+)$").unwrap()); // review checkouts of someone else's MR: `mr-1768`

fn run(cmd: &[String], timeout: f64, check: bool) -> Res<py::Out> {
    let p = py::exec(cmd, None, Some(timeout), None, &[])?;
    if check && p.rc != 0 {
        let e = py::strip(if p.err.is_empty() { &p.out } else { &p.err });
        return py::err(format!("{}: {}", cmd.join(" "), py::head(e, 300)));
    }
    Ok(p)
}
fn cmd(xs: &[&str]) -> Vec<String> {
    base::s(xs)
}
fn orca(args: &[&str]) -> Res<Value> {
    match std::env::var("GREENLIGHT_ADE").ok().as_deref() {
        None | Some("orca") => base::orca_cli(&cmd(args), 300.0), // rm of a big tree is slow
        _ => base::orca_s(args),
    }
}
fn git(path: &str, args: &[&str], check: bool) -> Res<String> {
    let mut c = cmd(&["git", "-C", path]);
    c.extend(cmd(args));
    Ok(py::strip(&run(&c, 60.0, check)?.out).to_string())
}
fn is_ancestor(path: &str, a: &str, b: &str) -> Res<bool> {
    Ok(run(&cmd(&["git", "-C", path, "merge-base", "--is-ancestor", a, b]), 60.0, false)?.rc == 0)
}
fn json_of(c: &[String]) -> Res<Value> {
    py::loads(&run(c, 60.0, true)?.out)
}

/// urllib.parse.urlparse(u) -> (netloc, path). ponytail: no `;params` split.
fn urlparse(u: &str) -> (&str, &str) {
    let rest = match u.find(':') {
        Some(i) if u[..i].chars().next().is_some_and(|c| c.is_ascii_alphabetic())
            && u[..i].chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) => &u[i + 1..],
        _ => u,
    };
    let rest = &rest[..rest.find(['?', '#']).unwrap_or(rest.len())];
    match rest.strip_prefix("//") {
        Some(r) => {
            let end = r.find('/').unwrap_or(r.len());
            (&r[..end], &r[end..])
        }
        None => ("", rest),
    }
}

struct Mr {
    label: String,
    iid: Value,
    state: String,
    sha: Option<String>,
}

/// Per-repo facts shared by every worktree of that repo: forge, project, default branch.
#[derive(Default)]
struct Repo {
    error: Option<String>,
    base: String,
    default: String,
    host: String,
    project: String,
}

impl Repo {
    fn new(path: &str) -> Res<Repo> {
        let mut r = Repo::default();
        let facts = || -> Res<(String, String)> {
            let url = git(path, &["remote", "get-url", "origin"], true)?;
            run(&cmd(&["git", "-C", path, "fetch", "--quiet", "--prune", "origin"]), 180.0, true)?;
            let mut head = git(path, &["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], false)?;
            if head.is_empty() {
                run(&cmd(&["git", "-C", path, "remote", "set-head", "origin", "--auto"]), 60.0, false)?;
                head = git(path, &["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], true)?;
            }
            Ok((url, head))
        };
        let (url, head) = match facts() {
            Ok(x) => x,
            Err(e) => {
                // no origin, offline, ...
                r.error = Some(e.to_string());
                return Ok(r);
            }
        };
        match head.split_once('/') {
            Some((_, d)) => (r.base, r.default) = (head.clone(), d.to_string()), // e.g. origin/develop
            None => {
                r.error = Some("list index out of range".into());
                return Ok(r);
            }
        }
        let u = url.strip_suffix(".git").unwrap_or(&url);
        let (host, proj) = if let Some(x) = u.strip_prefix("git@") {
            match x.split_once(':') {
                Some(hp) => hp,
                None => return py::err(format!("{path}: can't parse origin URL {} (git@ without host:path)", py::repr_str(&url))),
            }
        } else {
            let (netloc, p) = urlparse(u);
            (netloc.rsplit('@').next().unwrap(), p.trim_start_matches('/'))
        };
        (r.host, r.project) = (host.to_string(), proj.to_string());
        if host != "gitlab.com" && host != "github.com" {
            r.error = Some(format!("unsupported forge {host}"));
        }
        Ok(r)
    }

    /// [{label, iid, state, sha}] for the given MR/PR numbers plus every MR/PR whose source branch is
    /// `branch` (skipped when None), deduped, newest first; state normalised to open/merged/closed.
    fn mrs(&self, branch: Option<&str>, iids: &[Value]) -> Res<Vec<Mr>> {
        let mut out: Vec<Mr> = vec![];
        if self.host == "gitlab.com" {
            let pid = base::pid(&self.project);
            let mut ms = vec![];
            for iid in iids {
                match json_of(&cmd(&["glab", "api", &format!("projects/{pid}/merge_requests/{}", py::str_(iid))])) {
                    Ok(m) => ms.push(m),
                    // a comment can name an MR that isn't in this project
                    Err(e) if py::kind(&*e) == "RuntimeError" && e.to_string().contains("404") => {}
                    Err(e) => return Err(e),
                }
            }
            if let Some(b) = branch {
                let q = py::urlencode(&[("source_branch", b), ("state", "all"), ("order_by", "created_at")]);
                match json_of(&cmd(&["glab", "api", &format!("projects/{pid}/merge_requests?{q}")]))? {
                    Value::Array(a) => ms.extend(a),
                    _ => return py::err("glab: the merge_requests list is not a JSON list"),
                }
            }
            for m in &ms {
                let iid = k(m, "iid")?;
                if out.iter().any(|o| &o.iid == iid) {
                    continue;
                }
                let st = k(m, "state")?;
                let mut state = match st.as_str() {
                    Some("opened" | "locked") => "open",
                    Some("merged") => "merged",
                    Some("closed") => "closed",
                    _ => return py::raise("KeyError", py::repr(st)),
                }
                .to_string();
                if state == "open" && py::str_(k(k(m, "author")?, "username")?) != base::me() {
                    // Review checkout: my approval finishes my part, same as a merge would.
                    let ap = json_of(&cmd(&["glab", "api", &format!("projects/{pid}/merge_requests/{}/approvals", py::str_(iid))]))?;
                    let key = format!("{}!{}", self.project, py::str_(iid));
                    let mut approved = false;
                    for a in list(&ap, "approved_by")? {
                        approved |= py::str_(k(k(a, "user")?, "username")?) == base::me();
                    }
                    if blocking::cached(blocking::MAX_AGE)?.is_some_and(|b| b.get(&key).is_some()) {
                        // still blocked on me: keep it
                    } else if approved {
                        state = "approved by me".into();
                    } else if !ap.get("user_can_approve").map_or(true, py::truthy) && Some(py::str_(k(m, "source_branch")?).as_str()) != branch {
                        state = "not mine to approve".into(); // review checkout, my commits are in it: nothing left for me
                    }
                }
                let sha = m.get("sha").and_then(Value::as_str).map(String::from);
                out.push(Mr { label: format!("!{}", py::str_(iid)), iid: iid.clone(), state, sha });
            }
        } else {
            let fields = "number,state,headRefOid,headRefName,author,latestReviews";
            let mut ps = vec![];
            for n in iids {
                ps.push(json_of(&cmd(&["gh", "pr", "view", &py::str_(n), "-R", &self.project, "--json", fields]))?);
            }
            if let Some(b) = branch {
                match json_of(&cmd(&["gh", "pr", "list", "-R", &self.project, "--head", b, "--state", "all", "--json", fields]))? {
                    Value::Array(a) => ps.extend(a),
                    _ => return py::err("gh pr list: not a JSON list"),
                }
            }
            let mut open = false;
            for p in &ps {
                open |= k(p, "state")? == "OPEN";
            }
            let me = if open { Some(py::str_(k(&json_of(&cmd(&["gh", "api", "user"]))?, "login")?)) } else { None };
            // gh states are OPEN / MERGED / CLOSED: a merged or closed PR finishes the workspace like a merged/closed MR.
            // Review checkout: my approval finishes my part. ponytail: GitHub has no "can't approve" case to mirror
            for p in &ps {
                let n = k(p, "number")?;
                if out.iter().any(|o| &o.iid == n) {
                    continue; // the first listing of a PR wins
                }
                let sha = k(p, "headRefOid")?.as_str().map(String::from);
                let st = py::str_(k(p, "state")?);
                let author = k(p, "author")?.get("login").map(py::str_);
                let mut mine = false;
                if st == "OPEN" && author != me {
                    for r in list(p, "latestReviews")? {
                        mine |= Some(py::str_(k(k(r, "author")?, "login")?)) == me && k(r, "state")? == "APPROVED";
                    }
                }
                let state = if mine { "approved by me".into() } else { st.to_lowercase() };
                out.push(Mr { label: format!("#{}", py::str_(n)), iid: n.clone(), state, sha });
            }
        }
        out.sort_by(|a, b| b.iid.as_f64().unwrap_or(0.0).total_cmp(&a.iid.as_f64().unwrap_or(0.0)));
        Ok(out)
    }

    /// Every head the MR has had (GitLab diff versions). GitLab rebases an MR before merging and then
    /// deletes its branch, so a checkout of an earlier push is in neither the final head nor any remote.
    fn pushed_heads(&self, iid: &Value) -> Res<Vec<String>> {
        if self.host != "gitlab.com" {
            return Ok(vec![]);
        }
        let pid = base::pid(&self.project);
        let vs = json_of(&cmd(&["glab", "api", &format!("projects/{pid}/merge_requests/{}/versions?per_page=100", py::str_(iid))]))?;
        Ok(vs.as_array().into_iter().flatten().filter_map(|v| v.get("head_commit_sha").filter(|s| py::truthy(s)).map(py::str_)).collect())
    }
}

fn comment(wt: &Value) -> String {
    wt.get("comment").filter(|c| py::truthy(c)).map(py::str_).unwrap_or_default()
}
fn branch_of(wt: &Value) -> String {
    let b = wt.get("branch").filter(|c| py::truthy(c)).map(py::str_).unwrap_or_default();
    b.strip_prefix("refs/heads/").map(String::from).unwrap_or(b)
}

/// MR/PR numbers the workspace itself names: Orca's link, then its comment or an mr-N branch.
fn mr_numbers(wt: &Value, branch: &str, repo: &Repo) -> Vec<Value> {
    let link = wt.get(if repo.host == "gitlab.com" { "linkedGitLabMR" } else { "linkedPR" }).cloned().unwrap_or(Value::Null);
    // newer Orca links a PR as {"number": 39, ...}
    let mut ns = vec![if link.is_object() { link.get("number").cloned().unwrap_or(Value::Null) } else { link }];
    let c = comment(wt);
    for m in [MR_REF.captures(&c), MR_BRANCH.captures(branch)].into_iter().flatten() {
        ns.push(json!(m[1].parse::<i64>().unwrap_or(0)));
    }
    let mut out: Vec<Value> = vec![];
    for n in ns {
        if py::truthy(&n) && !out.contains(&n) {
            out.push(n);
        }
    }
    out
}

/// -> (verdict, reason) with verdict in candidate / keep / skip.
fn evaluate(wt: &Value, repo: &Repo, busy: &[Value]) -> Res<(&'static str, String)> {
    let (path, branch) = (py::str_(k(wt, "path")?), branch_of(wt));
    let t = |key: &str| wt.get(key).is_some_and(py::truthy);
    if t("isMainWorktree") {
        return Ok(("keep", "main checkout".into()));
    }
    let c = comment(wt);
    if branch.is_empty() && !MR_REF.is_match(&c) && !t("linkedGitLabMR") && !t("linkedPR") {
        return Ok(("keep", "detached HEAD".into())); // e.g. cache trees; a review checkout names its MR
    }
    if t("isPinned") {
        return Ok(("keep", "pinned".into()));
    }
    if c.starts_with("Plan:") {
        return Ok(("keep", "a planner: the Plan view owns it".into()));
    }
    if !Path::new(&path).is_dir() {
        return Ok(("skip", "path missing on disk".into()));
    }
    if let Some(e) = &repo.error {
        return Ok(("skip", format!("repo: {e}")));
    }
    if !branch.is_empty() && branch == repo.default {
        return Ok(("keep", "default branch".into()));
    }

    let remote_name = if branch.is_empty() {
        String::new()
    } else {
        let r = git(&path, &["config", &format!("branch.{branch}.merge")], false)?;
        r.strip_prefix("refs/heads/").map(String::from).unwrap_or(r)
    };
    let pushed_as_self = !branch.is_empty() && remote_name == branch;
    let lookup = if !remote_name.is_empty() && remote_name != repo.default { &remote_name } else { &branch };
    let tip = git(&path, &["rev-parse", "HEAD"], true)?;

    let mrs = match repo.mrs(Some(lookup.as_str()).filter(|l| !l.is_empty()), &mr_numbers(wt, &branch, repo)) {
        Ok(m) => m,
        Err(e) => return Ok(("skip", format!("MR lookup failed: {e}"))),
    };

    let reason;
    if !mrs.is_empty() {
        let newest = &mrs[0]; // an open MR older than a merged/closed one for the same work is stale
        if newest.state == "open" {
            return Ok(("keep", format!("MR {} is open", newest.label)));
        }
        let mut r = format!("MR {} {}", newest.label, newest.state);
        let open: Vec<&str> = mrs.iter().filter(|m| m.state == "open").map(|m| m.label.as_str()).collect();
        if !open.is_empty() {
            r += &format!(" (older {} open, stale)", open.join(", "));
        }
        // Local commits must already be on the forge: in an MR head (squash merges delete the remote
        // branch), in an earlier pushed version of the MR (GitLab rebases before merging), or in a remote.
        let contains = |sha: &str| -> Res<bool> { Ok(!sha.is_empty() && (tip == sha || is_ancestor(&path, &tip, sha)?)) };
        let mut safe = false;
        for m in &mrs {
            if contains(m.sha.as_deref().unwrap_or(""))? {
                safe = true;
                break;
            }
        }
        if !safe {
            safe = git(&path, &["rev-list", "-1", &tip, "--not", "--remotes"], true)?.is_empty();
        }
        'outer: for m in &mrs {
            if safe {
                break;
            }
            for sha in repo.pushed_heads(&m.iid)? {
                if contains(&sha)? {
                    safe = true;
                    break 'outer;
                }
            }
        }
        if !safe {
            return Ok(("skip", format!("{r}, but local commits aren't in the MR head or any remote")));
        }
        reason = r;
    } else if branch.is_empty() {
        return Ok(("keep", "detached HEAD, its MR wasn't found".into()));
    } else {
        if !is_ancestor(&path, &tip, &repo.base)? {
            return Ok(("keep", "no MR/PR, branch not merged".into()));
        }
        let now = base::now_ms();
        let last = wt.get("lastActivityAt").filter(|v| py::truthy(v)).and_then(Value::as_f64).unwrap_or(now);
        let idle_h = (now - last) / 3.6e6;
        if !pushed_as_self && idle_h < FRESH_HOURS {
            return Ok(("keep", format!("no MR/PR, tip in {} but never pushed and idle only {idle_h:.0}h (fresh branch?)", repo.base)));
        }
        reason = format!("no MR/PR, branch merged into {}", repo.base);
    }

    if !git(&path, &["status", "--porcelain", "--untracked-files=normal"], true)?.is_empty() {
        return Ok(("skip", format!("{reason}, but has uncommitted/untracked changes")));
    }
    let ids = match wt.get("ids").filter(|v| py::truthy(v)) {
        Some(Value::Array(a)) => a.clone(),
        _ => vec![k(wt, "id")?.clone()],
    };
    if ids.iter().any(|i| busy.contains(i)) {
        return Ok(("skip", format!("{reason}, but a terminal was active in the last {ACTIVE_MINUTES}m")));
    }
    Ok(("candidate", reason))
}

/// os.path.normpath (posix, lexical).
fn normpath(p: &str) -> String {
    if p.is_empty() {
        return ".".into();
    }
    let lead = if p.starts_with("//") && !p.starts_with("///") { "//" } else if p.starts_with('/') { "/" } else { "" };
    let mut out: Vec<&str> = vec![];
    for c in p.split('/') {
        match c {
            "" | "." => {}
            ".." if out.last().is_some_and(|l| *l != "..") => {
                out.pop();
            }
            ".." if !lead.is_empty() => {}
            c => out.push(c),
        }
    }
    let s = format!("{lead}{}", out.join("/"));
    if s.is_empty() { ".".into() } else { s }
}

/// One entry per worktree path. Orca can list a worktree twice, one entry without its linked MR; merge
/// the twins so the richer facts win (linked MR, comment, pinned) and every id counts for terminal activity.
fn dedupe(wts: &[Value]) -> Res<Vec<Value>> {
    let mut groups: Vec<(String, Vec<Value>)> = vec![];
    for w in wts {
        let p = normpath(&py::str_(k(w, "path")?));
        match groups.iter_mut().find(|g| g.0 == p) {
            Some(g) => g.1.push(w.clone()),
            None => groups.push((p, vec![w.clone()])),
        }
    }
    let mut out = vec![];
    for (_, mut group) in groups {
        if group.len() == 1 {
            out.push(group.pop().unwrap());
            continue;
        }
        let linked = |w: &Value| w.get("linkedGitLabMR").is_some_and(py::truthy) || w.get("linkedPR").is_some_and(py::truthy);
        group.sort_by_key(|w| !linked(w));
        let mut w = group[0].as_object().cloned().unwrap_or_default();
        for key in ["linkedGitLabMR", "linkedPR", "comment", "branch", "displayName"] {
            let v = group.iter().find_map(|g| g.get(key).filter(|v| py::truthy(v))).or(w.get(key)).cloned().unwrap_or(Value::Null);
            w.insert(key.into(), v);
        }
        for key in ["isPinned", "isMainWorktree"] {
            w.insert(key.into(), json!(group.iter().any(|g| g.get(key).is_some_and(py::truthy))));
        }
        let mut last: Option<Value> = None; // max(g.get("lastActivityAt") or 0) or None
        for g in &group {
            let v = g.get("lastActivityAt").filter(|v| py::truthy(v)).cloned().unwrap_or(json!(0));
            if last.as_ref().is_none_or(|l| v.as_f64().unwrap_or(0.0) > l.as_f64().unwrap_or(0.0)) {
                last = Some(v);
            }
        }
        w.insert("lastActivityAt".into(), last.filter(|l| py::truthy(l)).unwrap_or(Value::Null));
        let mut ids = vec![];
        for g in &group {
            ids.push(k(g, "id")?.clone());
        }
        w.insert("ids".into(), json!(ids));
        out.push(Value::Object(w));
    }
    Ok(out)
}

fn arr(v: &Value, key: &str) -> Res<Vec<Value>> {
    match k(v, key)? {
        Value::Array(a) => Ok(a.clone()),
        _ => py::raise("TypeError", format!("{key} is not a list")),
    }
}

fn scan() -> Res<Vec<Value>> {
    let wts = dedupe(&arr(&orca(&["worktree", "list", "--limit", "10000"])?, "worktrees")?)?;
    let cutoff = base::now_ms() - ACTIVE_MINUTES * 60_000.0;
    let mut busy = vec![];
    for t in arr(&orca(&["terminal", "list"])?, "terminals")? {
        if t.get("lastOutputAt").and_then(Value::as_f64).unwrap_or(0.0) > cutoff {
            busy.push(k(&t, "worktreeId")?.clone());
        }
    }
    // Group by repo; worktree list gives the repo id, the main checkout gives its path.
    let mut roots: Vec<(Value, Value)> = vec![];
    let mut put = |id: Value, path: Value, overwrite: bool| match roots.iter_mut().find(|r| r.0 == id) {
        Some(r) if overwrite => r.1 = path,
        Some(_) => {}
        None => roots.push((id, path)),
    };
    for w in wts.iter().filter(|w| w.get("isMainWorktree").is_some_and(py::truthy)) {
        put(k(w, "repoId")?.clone(), k(w, "path")?.clone(), true);
    }
    for r in arr(&orca(&["repo", "list"])?, "repos")? {
        put(k(&r, "id")?.clone(), k(&r, "path")?.clone(), false);
    }
    let made = py::pmap(&roots, 8, |(_, p)| Repo::new(&py::str_(p)));
    let mut repos = vec![];
    for ((id, _), r) in roots.iter().zip(made) {
        repos.push((id, r?));
    }

    let one = |w: &Value| -> Res<Value> {
        let rid = k(w, "repoId")?;
        let (verdict, reason) = match repos.iter().find(|r| r.0 == rid) {
            None => ("skip", "unknown repo".to_string()),
            Some((_, repo)) => evaluate(w, repo, &busy).unwrap_or_else(|e| ("skip", format!("error: {e}"))),
        };
        let path = k(w, "path")?;
        let name = match w.get("displayName").filter(|v| py::truthy(v)) {
            Some(n) => n.clone(),
            None => json!(py::str_(path).split('/').filter(|c| !c.is_empty() && *c != ".").last().unwrap_or("")),
        };
        Ok(json!({"id": k(w, "id")?, "name": name, "path": path, "branch": branch_of(w), "verdict": verdict, "reason": reason}))
    };
    py::pmap(&wts, 8, one).into_iter().collect()
}

fn log(entry: Map<String, Value>) -> Res<()> {
    let f = STATE.join("log.jsonl");
    std::fs::create_dir_all(f.parent().unwrap())?;
    let mut o = json!({"ts": py::time() as i64});
    o.as_object_mut().unwrap().extend(entry);
    let mut fh = std::fs::OpenOptions::new().create(true).append(true).open(f)?;
    writeln!(fh, "{}", py::dumps(&o))?;
    Ok(())
}

const HELP: &str = "usage: cleanup [-h] [--json] {scan,precheck,apply}

positional arguments:
  {scan,precheck,apply}

options:
  -h, --help            show this help message and exit
  --json";

pub fn main(args: Vec<String>) -> Res<i32> {
    // argparse: one positional cmd (scan|precheck|apply), --json and its prefixes, -h
    let (mut cmd_, mut json_, mut bad) = (None, false, vec![]);
    for a in &args {
        if a == "-h" || (a.len() > 2 && "--help".starts_with(a.as_str())) {
            println!("{HELP}");
            return Ok(0);
        } else if a.len() > 2 && "--json".starts_with(a.as_str()) {
            json_ = true;
        } else if let Some(v) = a.strip_prefix("--j").filter(|_| a.contains('=') && "--json".starts_with(a.split('=').next().unwrap())) {
            let _ = v;
            py::argparse_exit("cleanup", &format!("argument --json: ignored explicit argument '{}'", a.split_once('=').unwrap().1));
        } else if a.starts_with('-') || cmd_.is_some() {
            bad.push(a.as_str());
        } else if ["scan", "precheck", "apply"].contains(&a.as_str()) {
            cmd_ = Some(a.as_str());
        } else {
            py::argparse_exit("cleanup", &format!("argument cmd: invalid choice: {} (choose from 'scan', 'precheck', 'apply')", py::repr_str(a)));
        }
    }
    let Some(cmd_) = cmd_ else { py::argparse_exit("cleanup", "the following arguments are required: cmd") };
    if !bad.is_empty() {
        py::argparse_exit("cleanup", &format!("unrecognized arguments: {}", bad.join(" ")));
    }

    let mut rows = match scan() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("scan failed: {e}");
            return Ok(if cmd_ == "precheck" { 1 } else { 2 });
        }
    };
    let n = rows.iter().filter(|r| r["verdict"] == "candidate").count();

    if cmd_ == "precheck" {
        println!("{n} workspace(s) to delete");
        return Ok(if n > 0 { 0 } else { 1 });
    }

    if cmd_ == "apply" {
        for r in rows.iter_mut().filter(|r| r["verdict"] == "candidate") {
            match orca(&["worktree", "rm", "--worktree", &format!("id:{}", py::str_(&r["id"]))]) {
                Ok(_) => r["removed"] = json!(true),
                Err(e) => {
                    r["removed"] = json!(false);
                    r["error"] = json!(py::tail(&e.to_string(), 300));
                }
            }
            let mut entry: Map<String, Value> = ["name", "path", "branch", "reason", "removed"].iter().map(|k| (k.to_string(), r[*k].clone())).collect();
            if r["removed"] == false {
                entry.insert("error".into(), r["error"].clone());
            }
            log(entry)?;
        }
    }

    if json_ {
        println!("{}", py::dumps_indent(&json!(rows)));
        return Ok(0);
    }
    for verdict in ["candidate", "skip", "keep"] {
        let mut group: Vec<&Value> = rows.iter().filter(|r| r["verdict"] == verdict).collect();
        if group.is_empty() {
            continue;
        }
        let title = match verdict {
            "candidate" if cmd_ == "apply" => "DELETED",
            "candidate" => "WOULD DELETE",
            "skip" => "SKIPPED (needs a human)",
            _ => "KEPT",
        };
        println!("\n## {title} ({})", group.len());
        group.sort_by(|a, b| py::str_(&a["path"]).cmp(&py::str_(&b["path"])));
        for r in group {
            let mark = if verdict != "candidate" || cmd_ != "apply" { "" } else if r["removed"] == true { "✓ " } else { "✗ " };
            let br = py::str_(&r["branch"]);
            let err = r.get("error").filter(|e| py::truthy(e)).map(|e| format!(" — {}", py::str_(e))).unwrap_or_default();
            println!("- {mark}{} [{}] — {}{err}", py::str_(&r["name"]), if br.is_empty() { "-" } else { &br }, py::str_(&r["reason"]));
        }
    }
    Ok(0)
}
