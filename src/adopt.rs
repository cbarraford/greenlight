//! Port of scripts/adopt.py. Give every open MR/PR of mine, one I'm a requested reviewer on, or (config
//! `blocking`) one I'm blocking (see blocking.rs), a workspace (so it gets a hex).
//!
//!     adopt scan [--json]            # candidates and why each is skipped; changes nothing
//!     adopt precheck                 # exit 0 = at least one workspace to create, 1 = none
//!     adopt apply [--json] [--cap N] # create them, write tombstones, ping what needs me
//!     adopt dismiss <project>!<iid>... # tombstone them: never adopted while open
//!
//! A candidate is an open MR across all of GitLab with me as author or reviewer, plus every MR
//! blocking.rs says is blocked on me, for any reason (re-read from GitLab, kept only while it's open). If
//! that scan fails, adopt runs on the author/reviewer queries alone and keeps any tombstone it can't recheck.
//! Guards, in order: covered (a workspace in ~/.greenlight/last.json already has it, by MR or by source branch)
//! → tombstoned (its workspace was deleted while the MR was open; a review re-request, or a block on me
//! after the MR changed, clears it) → someone else's MR I've approved (or can't approve) and am not blocking → someone else's
//! draft → no repo in the ADE for its project (pinged once per project) → branch checked out in another worktree
//! (pinged) → at most CAP creates per run.
//!
//! Authored MRs check out their source branch tracking origin; review MRs get `mr-<N>` from the MR head
//! ref. Goal "Merge !N: <title>" / "Review !N: <title>", no agent, `--setup skip`: the lifecycle decides
//! what happens next. greenlight stays read-only; this file owns ~/.greenlight/dismissed.json and adopt.json.
//!
//! Needs `glab` (GitLab repos) and/or `gh` (GitHub repos) authenticated; blocking is GitLab-only.
use crate::base::{self, cfg, me, pid, ref_, s, STATE};
use crate::blocking;
use crate::greenlight;
use crate::py::{self, g, gd, gs, ix, str_, truthy, Res};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, OnceLock};

const ROLES: [(&str, &str); 2] = [("author", "author_username"), ("reviewer", "reviewer_username")];
fn dismissed_file() -> PathBuf {
    STATE.join("dismissed.json") // {"<project>!<iid>": {"ts", "why"}}: deleted while the MR was open
}
fn state_file() -> PathBuf {
    STATE.join("adopt.json") // {"seen": {key: {id, name}}, "created": {key: ts}}
}
fn log_file() -> PathBuf {
    STATE.join("log.jsonl")
}

/// CONFIG["adopt"].get("cap", 10): the default --cap.
pub fn cap() -> Value {
    cfg("adopt").get("cap").cloned().unwrap_or(json!(10))
}

fn git(path: &str, args: &[&str], check: bool) -> Res<String> {
    let mut cmd = s(&["git", "-C", path]);
    cmd.extend(s(args));
    let p = py::exec(&cmd, None, Some(180.0), None, &[])?;
    if check && p.rc != 0 {
        let e = py::strip(if p.err.is_empty() { &p.out } else { &p.err });
        return py::err(format!("git {}: {}", args.join(" "), py::head(e, 300)));
    }
    Ok(py::strip(&p.out).into())
}

fn load(f: &Path, default: Value) -> Value {
    let text = std::fs::read(f).ok().and_then(|b| String::from_utf8(b).ok());
    text.and_then(|t| py::loads(&t).ok()).unwrap_or(default)
}

fn save(f: &Path, data: &Value) -> Res<()> {
    std::fs::create_dir_all(f.parent().unwrap())?;
    std::fs::write(f, py::dumps_indent(data)).or_else(|e| py::oserr(e, f))
}

/// A state file's dict (missing → {}); Python died with AttributeError on anything else.
fn dict(v: &Value, what: &str) -> Res<Map<String, Value>> {
    match v {
        Value::Object(m) => Ok(m.clone()),
        Value::Null => Ok(Map::new()),
        _ => py::err(format!("{what} is not a JSON object")),
    }
}

fn orca_ade() -> bool {
    std::env::var("GREENLIGHT_ADE").map_or(true, |v| v == "orca")
}

// --- project mapping ---------------------------------------------------------------------------

static REMOTE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+)$").unwrap());

/// git@host:a/b.git | https://user@host/a/b | ssh://git@host:22/a/b | host/a/b -> (host, "a/b").
fn parse_remote(url: &str) -> Option<(String, String)> {
    let u = py::strip(url);
    let u = u.strip_suffix('/').unwrap_or(u);
    let u = u.strip_suffix(".git").unwrap_or(u);
    REMOTE.captures(u).map(|m| (m[1].to_string(), m[2].to_string()))
}

/// {project path: {"id", "path", "forge"}} for every gitlab.com/github.com repo the ADE knows, worktrees or not.
/// ponytail: keyed by path alone, so the same owner/repo on both forges collides (first one wins)
fn repo_map() -> Res<Map<String, Value>> {
    let mut out = Map::new();
    let res = base::orca_s(&["repo", "list"])?;
    for r in py::it(ix(&res, "repos")?)? {
        let mut key = g(g(&r, "gitRemoteIdentity"), "canonicalKey").clone();
        if !truthy(&key) && truthy(g(&r, "path")) {
            // cmux/herdr/T3 shims, or an Orca repo without the field
            key = json!(git(&str_(&r["path"]), &["remote", "get-url", "origin"], false)?);
        }
        let Some((host, proj)) = parse_remote(key.as_str().unwrap_or("")) else { continue };
        if host == "gitlab.com" || host == "github.com" {
            let v = json!({"id": ix(&r, "id")?, "path": ix(&r, "path")?, "forge": host.split('.').next().unwrap()});
            out.entry(proj).or_insert(v);
        }
    }
    Ok(out)
}

/// {branch: worktree path} for every worktree of the repo at path.
fn checked_out(path: &str) -> Res<Map<String, Value>> {
    let (mut out, mut wt) = (Map::new(), Value::Null);
    for line in py::splitlines(&git(path, &["worktree", "list", "--porcelain"], false)?) {
        if let Some(p) = line.strip_prefix("worktree ") {
            wt = json!(p);
        } else if let Some(b) = line.strip_prefix("branch ") {
            out.insert(b.strip_prefix("refs/heads/").unwrap_or(b).into(), wt.clone());
        }
    }
    Ok(out)
}

// --- candidates ------------------------------------------------------------------------------

fn project_of(m: &Value) -> Res<String> {
    let full = g(g(m, "references"), "full").as_str().unwrap_or("");
    if let Some((p, _)) = full.rsplit_once('!') {
        return Ok(p.into());
    }
    let url = str_(ix(m, "web_url")?);
    match url.split_once("gitlab.com/") {
        Some((_, rest)) => Ok(rest.split("/-/").next().unwrap().into()),
        None => py::err(format!("MR {} has no project: no references.full, and its web_url {url:?} isn't on gitlab.com", str_(g(m, "iid")))),
    }
}

fn cand(m: &Value) -> Res<Value> {
    let proj = project_of(m)?;
    let author = ix(ix(m, "author")?, "username")?;
    let mine = author.as_str() == Some(me());
    let updated = if truthy(g(m, "updated_at")) { m["updated_at"].clone() } else { json!("") };
    Ok(json!({"key": format!("{proj}!{}", str_(ix(m, "iid")?)), "project": proj, "project_id": ix(m, "project_id")?, "iid": m["iid"],
        "forge": "gitlab", "title": ix(m, "title")?, "source_branch": ix(m, "source_branch")?, "draft": truthy(g(m, "draft")),
        "role": if mine { "author" } else { "review" }, "author": author, "updated_at": updated, "sha": g(m, "sha"),
        "web_url": ix(m, "web_url")?}))
}

const GH_SEARCH: &str = "query($q: String!) { search(type: ISSUE, first: 100, query: $q) { nodes { ... on PullRequest {
  number title isDraft headRefName headRefOid updatedAt url author { login } repository { nameWithOwner } } } } }";
const GH_ROLES: [(&str, &str); 2] = [("author", "author:@me"), ("reviewer", "review-requested:@me")];

/// ME may be the GitLab name. Cached once it answers (functools.cache).
fn gh_login() -> Res<Value> {
    static LOGIN: OnceLock<Value> = OnceLock::new();
    if let Some(v) = LOGIN.get() {
        return Ok(v.clone());
    }
    let v = ix(&base::gh(&["api", "user"])?, "login")?.clone();
    Ok(LOGIN.get_or_init(|| v).clone())
}

/// ROLES[role] / GH_ROLES[role]; Python died with KeyError 'x' on a role it doesn't know.
fn role_q(table: &[(&'static str, &'static str)], role: &str) -> Res<&'static str> {
    match table.iter().find(|(r, _)| *r == role) {
        Some((_, q)) => Ok(q),
        None => py::err(format!("config.json: adopt.roles has {}: each must be \"author\" or \"reviewer\"", py::repr_str(role))),
    }
}

/// Open PRs I wrote or was asked to review, in cand()'s shape. The key keeps `!` like GitLab's (adopt.json,
/// dismissed.json and greenlight's rows all use <project>!<n>).
fn gh_cands(role: &str) -> Res<Vec<Value>> {
    let q = format!("q=is:pr is:open archived:false {}", role_q(&GH_ROLES, role)?);
    let d = base::gh(&["api", "graphql", "-f", &q, "-f", &format!("query={GH_SEARCH}")])?;
    let mut out = vec![];
    for p in py::it(ix(ix(ix(&d, "data")?, "search")?, "nodes")?)? {
        if !truthy(&p) {
            continue;
        }
        let proj = ix(ix(&p, "repository")?, "nameWithOwner")?;
        out.push(json!({"key": format!("{}!{}", str_(proj), str_(ix(&p, "number")?)), "project": proj, "project_id": null,
            "iid": p["number"], "forge": "github", "title": ix(&p, "title")?, "source_branch": ix(&p, "headRefName")?,
            "draft": ix(&p, "isDraft")?, "role": if role == "author" { "author" } else { "review" },
            "author": g(ix(&p, "author")?, "login"), "updated_at": ix(&p, "updatedAt")?, "sha": ix(&p, "headRefOid")?,
            "web_url": ix(&p, "url")?}));
    }
    Ok(out)
}

/// {GitHub's current owner/name: the ADE's} for GitHub repos renamed since they were cloned. Search reports a PR
/// under the new name, the ADE's remote (so greenlight's rows) keeps the old one, and GitHub redirects the old to the new.
fn gh_renamed(repos: &Map<String, Value>) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for (proj, r) in repos {
        if r["forge"] != "github" {
            continue;
        }
        // gone or no access: its PRs stay unmatched, as before
        let Ok(name) = base::gh(&["api", &format!("repos/{proj}")]).and_then(|d| ix(&d, "full_name").cloned()) else { continue };
        let name = str_(&name);
        if name.to_lowercase() != proj.to_lowercase() {
            out.insert(name, proj.clone());
        }
    }
    out
}

/// -> (open MRs/PRs I wrote, was asked to review, or am blocking, deduped, newest activity first; whether the
/// blocking scan answered). Each MR I'm blocking carries c["block"] = its reason. Only the forges the ADE has
/// repos on are asked, so a GitHub-only setup never needs glab (and vice versa).
fn candidates(forges: &HashSet<String>) -> Res<(Vec<Value>, bool)> {
    let mut seen = Map::new();
    let roles = match cfg("adopt").get("roles") {
        Some(r) => py::it(r)?,
        None => ROLES.iter().map(|(r, _)| json!(r)).collect(),
    };
    for r in roles {
        let r = str_(&r);
        let mut cs = vec![];
        if forges.contains("gitlab") {
            let q = format!("merge_requests?scope=all&state=opened&{}={}", role_q(&ROLES, &r)?, me());
            for m in base::gl_all(&q)? {
                cs.push(cand(&m)?);
            }
        }
        if forges.contains("github") {
            cs.extend(gh_cands(&r)?);
        }
        for c in cs {
            seen.entry(str_(&c["key"])).or_insert(c);
        }
    }
    let blocks = if forges.contains("gitlab") { blocking::blocks(false)? } else { Some(json!({})) };
    if let Some(Value::Object(b)) = &blocks {
        let mut keys: Vec<&String> = b.keys().collect();
        keys.sort();
        for key in keys {
            if !seen.contains_key(key) {
                let Some((proj, iid)) = key.rsplit_once('!') else {
                    return py::err(format!("{}: blocked MR key {} isn't <project>!<iid>", blocking_file(), py::repr_str(key)));
                };
                // gone or no access since the scan: skip it
                let Ok(m) = greenlight::gl(&format!("projects/{}/merge_requests/{iid}", pid(proj))) else { continue };
                if g(&m, "state").as_str() != Some("opened") {
                    continue; // the scan is cached for minutes: it may have merged since
                }
                seen.insert(key.clone(), cand(&m)?);
            }
            seen[key.as_str()]["block"] = ix(&b[key.as_str()], "reason")?.clone();
        }
    }
    let mut out: Vec<Value> = seen.into_iter().map(|(_, c)| c).collect();
    out.sort_by(|a, b| py::cmp_key(&b["updated_at"], &a["updated_at"])); // stable, like sorted(reverse=True)
    Ok((out, blocks.is_some()))
}

fn blocking_file() -> String {
    STATE.join("blocks.json").display().to_string()
}

fn iso_ts(s: &Value) -> Res<f64> {
    if !truthy(s) {
        return Ok(0.0);
    }
    let h = py::head(s.as_str().unwrap_or(""), 19);
    match chrono::NaiveDateTime::parse_from_str(h, "%Y-%m-%dT%H:%M:%S") {
        Ok(t) => Ok(t.and_utc().timestamp() as f64),
        Err(_) => py::raise("ValueError", format!("time data {} does not match format '%Y-%m-%dT%H:%M:%S'", py::repr_str(h))),
    }
}

/// Why someone else's MR needs no review workspace from me (approved, or GitLab won't let me), else None.
fn review_done(c: &Value) -> Res<Option<&'static str>> {
    if c["forge"] == "github" {
        // GitHub never blocks approving a PR with your commits in it
        let d = base::gh(&["pr", "view", &str_(&c["iid"]), "-R", &str_(&c["project"]), "--json", "latestReviews"])?;
        for r in py::it(ix(&d, "latestReviews")?)? {
            if *ix(ix(&r, "author")?, "login")? == gh_login()? && ix(&r, "state")? == "APPROVED" {
                return Ok(Some("approved by me"));
            }
        }
        return Ok(None);
    }
    let ap = greenlight::gl(&format!("projects/{}/merge_requests/{}/approvals", str_(&c["project_id"]), str_(&c["iid"])))?;
    for a in py::it(&gd(&ap, "approved_by", json!([])))? {
        if ix(ix(&a, "user")?, "username")?.as_str() == Some(me()) {
            return Ok(Some("approved by me"));
        }
    }
    if !truthy(&gd(&ap, "user_can_approve", json!(true))) {
        return Ok(Some("I can't approve it (my commits are in it)"));
    }
    Ok(None)
}

/// A tombstone clears when the MR changed after it and either I'm blocking it or (a review) I'm back to
/// unreviewed.
fn rerequested(c: &Value, tomb: &Value) -> Res<bool> {
    let ts = ix(tomb, "ts")?.as_f64().ok_or_else(|| format!("{}: {}'s ts is not a number", dismissed_file().display(), str_(&c["key"])))?;
    if iso_ts(&c["updated_at"])? <= ts {
        return Ok(false);
    }
    if truthy(g(c, "block")) {
        return Ok(true);
    }
    if c["role"] != "review" {
        return Ok(false);
    }
    if c["forge"] == "github" {
        let d = base::gh(&["pr", "view", &str_(&c["iid"]), "-R", &str_(&c["project"]), "--json", "reviewRequests"])?;
        for r in py::it(ix(&d, "reviewRequests")?)? {
            if *g(&r, "login") == gh_login()? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    let revs = greenlight::gl(&format!("projects/{}/merge_requests/{}/reviewers", str_(&c["project_id"]), str_(&c["iid"])))?;
    for r in py::it(&revs)? {
        if ix(ix(&r, "user")?, "username")?.as_str() == Some(me()) && g(&r, "state") == "unreviewed" {
            return Ok(true);
        }
    }
    Ok(false)
}

// --- what's already covered ------------------------------------------------------------------

static SCHED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\*/([0-9]+) ").unwrap());

/// The greenlight automation's period (`*/N` minutes), default 30 min.
fn interval_s() -> f64 {
    let a = load(&STATE.join("automations.json"), json!({}));
    let sched = g(g(&a, "greenlight"), "schedule").as_str().unwrap_or("");
    SCHED.captures(sched).and_then(|m| m[1].parse::<f64>().ok()).map_or(1800.0, |n| n * 60.0)
}

/// -> (rows, ts) from last.json, or a fresh scan when that's older than one automation interval.
fn current_rows() -> Res<(Vec<Value>, Value)> {
    let f = STATE.join("last.json");
    let last = load(&f, json!({}));
    if !g(&last, "rows").is_null() {
        let ts = gd(&last, "ts", json!(0)).as_f64().ok_or_else(|| format!("{}: ts is not a number", f.display()))?;
        if py::time() - ts <= interval_s() {
            return Ok((py::it(&last["rows"])?, ix(&last, "ts")?.clone()));
        }
    }
    let res = greenlight::scan(false)?;
    Ok((py::it(ix(&res, "rows")?)?, ix(&res, "ts")?.clone()))
}

/// {"<project>!<iid>": {id, name}} for rows tied to an open MR.
fn row_keys(rows: &[Value]) -> Res<Map<String, Value>> {
    let mut out = Map::new();
    for r in rows {
        let mr = g(r, "mr");
        let iid = if truthy(g(mr, "iid")) { g(mr, "iid") } else { g(r, "linked_mr") };
        if truthy(iid) && mr.get("state").is_none_or(|s| s == "opened") {
            out.insert(format!("{}!{}", str_(ix(r, "project")?), str_(iid)), json!({"id": ix(r, "id")?, "name": ix(r, "name")?}));
        }
    }
    Ok(out)
}

fn removed_by_cleanup(name: &Value) -> Res<bool> {
    let f = log_file();
    if !f.exists() {
        return Ok(false);
    }
    let text = std::fs::read_to_string(&f).or_else(|e| py::oserr(e, &f))?;
    for line in py::splitlines(&text) {
        let Ok(e) = py::loads(line) else { continue };
        if g(&e, "name") == name && truthy(g(&e, "removed")) {
            return Ok(true);
        }
    }
    Ok(false)
}

// --- the plan --------------------------------------------------------------------------------

/// What plan() decided. `create` indexes into `items` (Python shares the dicts, so apply's results show
/// in the printed items).
pub struct Plan {
    pub items: Vec<Value>,
    pub create: Vec<usize>,
    pub dismissed: Map<String, Value>,
    pub seen: Map<String, Value>,
    pub pings: Vec<(String, String)>,
}

/// Decide everything, change nothing. rows: (rows, their scan ts), or None to read last.json / scan.
pub fn plan(rows: Option<(&[Value], &Value)>, cap: &Value) -> Res<Plan> {
    let now = py::time();
    let (rows, rows_ts) = match rows {
        Some((r, t)) => (r.to_vec(), t.clone()),
        None => current_rows()?,
    };
    // ponytail: a truthy non-number ts reads as now (Python failed later comparing it)
    let rows_ts = if truthy(&rows_ts) { rows_ts.as_f64().unwrap_or(now) } else { now };
    let st = load(&state_file(), json!({}));
    let st = dict(&st, "adopt.json")?;
    let empty = Value::Null;
    let seen_before = dict(st.get("seen").unwrap_or(&empty), "adopt.json seen")?;
    let created = dict(st.get("created").unwrap_or(&empty), "adopt.json created")?;
    let cur = row_keys(&rows)?;
    let ids = rows.iter().map(|r| ix(r, "id").cloned()).collect::<Res<Vec<_>>>()?;
    let covered_branch = rows.iter().map(|r| Ok((ix(r, "project")?.clone(), ix(r, "branch")?.clone()))).collect::<Res<Vec<_>>>()?;
    let repos = repo_map()?;
    let mut busy_cache: HashMap<String, Map<String, Value>> = HashMap::new();
    let (mut items, mut create) = (vec![], vec![]);
    let forges: HashSet<String> = repos.values().map(|r| str_(&r["forge"])).collect();
    let (mut cands, blocks_ok) = candidates(&forges)?;
    if cands.iter().any(|c| c["forge"] == "github" && !repos.contains_key(gs(c, "project"))) {
        let renamed = gh_renamed(&repos);
        for c in &mut cands {
            // back to the ADE's name, the one greenlight's rows use, so a renamed repo's PR still reads as covered
            if let Some(p) = renamed.get(gs(c, "project")).filter(|_| c["forge"] == "github").cloned() {
                c["key"] = json!(format!("{p}!{}", str_(&c["iid"])));
                c["project"] = json!(p);
            }
        }
    }
    // ponytail: GitHub PRs only in repos the ADE has. Old PRs on forks scatter across GitHub, and a no-repo ping each is noise
    cands.retain(|c| c["forge"] != "github" || repos.contains_key(gs(c, "project")));
    let open_keys: HashSet<String> = cands.iter().map(|c| str_(&c["key"])).collect();

    // Tombstones: drop ones whose MR closed; add workspaces that vanished while their MR stayed open.
    // Without the blocking scan, an MR missing from open_keys may be a blocked-only one that's still open: keep
    // its tombstone and its seen entry until a run that can tell.
    let mut dismissed: Map<String, Value> =
        dict(&load(&dismissed_file(), json!({})), "dismissed.json")?.into_iter().filter(|(k, _)| open_keys.contains(k) || !blocks_ok).collect();
    for (k, v) in &seen_before {
        if open_keys.contains(k) && !cur.contains_key(k) && !ids.contains(ix(v, "id")?) && !dismissed.contains_key(k) {
            let why = if removed_by_cleanup(ix(v, "name")?)? { "cleanup" } else { "manual" };
            dismissed.insert(k.clone(), json!({"ts": now as i64, "why": why}));
        }
    }

    for mut c in cands {
        let k = str_(&c["key"]);
        let (mut verdict, reason) = ("skip", String::new());
        let reason = if cur.contains_key(&k) || covered_branch.contains(&(c["project"].clone(), c["source_branch"].clone())) {
            "covered: a workspace already has it".to_string()
        } else if created.get(&k).and_then(Value::as_f64).unwrap_or(0.0) > rows_ts {
            "covered: created by an earlier adopt run, not scanned yet".to_string()
        } else if dismissed.contains_key(&k) && !rerequested(&c, &dismissed[&k])? {
            format!("tombstoned ({}): its workspace was removed", str_(ix(&dismissed[&k], "why")?))
        } else {
            if dismissed.contains_key(&k) {
                c["cleared"] = json!(true); // re-requested: the tombstone goes on apply
            }
            let review = c["role"] == "review";
            let done = if review && !truthy(g(&c, "block")) { review_done(&c)? } else { None };
            if let Some(d) = done {
                d.to_string()
            } else if review && truthy(&c["draft"]) {
                "someone else's draft".to_string()
            } else if !repos.contains_key(gs(&c, "project")) {
                verdict = "no-repo";
                format!("no repo in the ADE for {}", str_(&c["project"]))
            } else {
                let repo = repos[gs(&c, "project")].clone();
                let path = str_(&repo["path"]);
                c["repo"] = repo;
                if !busy_cache.contains_key(&path) {
                    busy_cache.insert(path.clone(), checked_out(&path)?);
                }
                let busy = &busy_cache[&path];
                let mrb = review.then(|| format!("mr-{}", str_(&c["iid"])));
                let hit = [c["source_branch"].as_str().map(String::from), mrb].into_iter().flatten().find(|b| !b.is_empty() && busy.contains_key(b));
                if let Some(h) = hit {
                    verdict = "busy";
                    format!("{h} is checked out in {}", str_(&busy[&h]))
                } else if create.len() as f64 >= cap.as_f64().ok_or_else(|| format!("--cap {} is not a number", py::repr(cap)))? {
                    verdict = "deferred";
                    format!("over the {}-per-run cap: next run", str_(cap))
                } else {
                    verdict = "create";
                    let block = if truthy(g(&c, "block")) { format!(" (I'm blocking it: {})", str_(&c["block"])) } else { String::new() };
                    c["name"] = json!(ws_name(&c));
                    create.push(items.len());
                    format!("{} MR with no workspace{block}", str_(&c["role"]))
                }
            }
        } + &reason;
        c["verdict"] = json!(verdict);
        c["reason"] = json!(reason);
        items.push(c);
    }

    let mut pings = vec![];
    let mut no_repo: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for i in items.iter().filter(|i| i["verdict"] == "no-repo") {
        no_repo.entry(str_(&i["project"])).or_default().push(i);
    }
    for (proj, group) in &no_repo {
        let mrs: Vec<String> = group.iter().map(|i| ref_(gs(i, "forge"), &i["iid"])).collect();
        pings.push((
            format!("@no-repo:{proj}"),
            format!(
                "adopt: {proj} has no repo in the ADE, so {} open MR(s) get no workspace: {}. Add the repo to the ADE?\n{}\nOptions: add the repo | ignore its MRs",
                group.len(),
                mrs.join(" "),
                str_(&group[0]["web_url"])
            ),
        ));
    }
    let busy: Vec<String> = items.iter().filter(|i| i["verdict"] == "busy").map(|i| format!("{} ({})", str_(&i["key"]), str_(&i["reason"]))).collect();
    if !busy.is_empty() {
        pings.push((
            "@adopt-busy".into(),
            format!("adopt: can't create a workspace, the branch is checked out elsewhere: {}\nOptions: free the branches | leave them", busy.join("; ")),
        ));
    }
    dismissed.retain(|k, _| !items.iter().any(|i| i["key"] == k.as_str() && truthy(g(i, "cleared"))));
    let seen = if blocks_ok {
        cur
    } else {
        let mut s: Map<String, Value> = seen_before.into_iter().filter(|(k, _)| !open_keys.contains(k)).collect();
        s.extend(cur);
        s
    };
    Ok(Plan { items, create, dismissed, seen, pings })
}

// --- creating --------------------------------------------------------------------------------

fn ws_name(c: &Value) -> String {
    if c["role"] == "author" { str_(&c["source_branch"]) } else { format!("mr-{}", str_(&c["iid"])) }
}

fn create_args(c: &Value, base_: &str) -> Vec<String> {
    let name = ws_name(c);
    let goal = format!("{} {}: {}", if c["role"] == "author" { "Merge" } else { "Review" }, ref_(gs(c, "forge"), &c["iid"]), str_(&c["title"]));
    let id = str_(&c["repo"]["id"]);
    let repo = if orca_ade() { format!("id:{id}") } else { id };
    let mut a = s(&["worktree", "create", "--name", &name, "--repo", &repo, "--base-branch", base_]);
    if orca_ade() {
        a.extend([if c["forge"] == "github" { "--pr" } else { "--gitlab-mr" }.to_string(), str_(&c["iid"])]);
    }
    a.extend(s(&["--comment", &goal, "--setup", "skip"]));
    a
}

/// `git merge-base --is-ancestor a HEAD`, uncaptured and untimed like Python's bare subprocess.run.
fn is_ancestor(path: &str, a: &str) -> Res<bool> {
    std::io::stdout().flush()?;
    match std::process::Command::new("git").args(["-C", path, "merge-base", "--is-ancestor", a, "HEAD"]).status() {
        Ok(st) => Ok(st.code() == Some(0)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => py::raise("FileNotFoundError", "[Errno 2] No such file or directory: 'git'"),
        Err(e) => Err(e.into()),
    }
}

/// Put the new worktree on local branch `want` (Orca may pick `<name>-2`), tracking upstream.
fn ensure_branch(path: &str, want: &str, upstream: Option<&str>) -> Res<String> {
    let cur = git(path, &["symbolic-ref", "--short", "-q", "HEAD"], false)?;
    let mut want = want.to_string();
    if cur != want {
        let exists = git(path, &["rev-parse", "--verify", "-q", &format!("refs/heads/{want}")], false)?;
        if exists.is_empty() {
            git(path, &["switch", "-c", &want], true)?;
        } else if is_ancestor(path, &want)? {
            git(path, &["switch", "-C", &want], true)?; // want is behind HEAD: moving it loses nothing
        } else {
            want = cur.clone(); // a diverged local branch of that name: leave it alone, keep Orca's
        }
        if !cur.is_empty() && cur != want {
            git(path, &["branch", "-D", &cur], false)?; // same commit as HEAD
        }
    }
    if let Some(u) = upstream {
        git(path, &["branch", &format!("--set-upstream-to=origin/{u}"), &want], true)?;
    }
    Ok(want)
}

/// -> (path, branch) of the new workspace.
fn create(c: &Value) -> Res<(Value, Value)> {
    let path = str_(ix(&c["repo"], "path")?);
    let iid = str_(&c["iid"]);
    let (base_, want, upstream);
    if c["role"] == "author" {
        let sb = str_(&c["source_branch"]);
        git(&path, &["fetch", "origin", &sb], true)?;
        (base_, want, upstream) = (format!("origin/{sb}"), sb.clone(), Some(sb));
    } else {
        (want, upstream) = (format!("mr-{iid}"), None);
        let head = if c["forge"] == "github" { format!("refs/pull/{iid}/head") } else { format!("refs/merge-requests/{iid}/head") };
        if orca_ade() {
            // Orca bases the worktree on the local branch, then names its own (fixed below)
            git(&path, &["fetch", "origin", &format!("+{head}:{want}")], true)?;
            base_ = want.clone();
        } else {
            // the shims run `git worktree add -b mr-N`, so mr-N mustn't exist yet: base on the commit
            git(&path, &["fetch", "origin", &head], true)?;
            base_ = str_(&c["sha"]);
        }
    }
    let res = base::orca(&create_args(c, &base_))?;
    let wt = if truthy(g(&res, "worktree")) { &res["worktree"] } else { &res };
    let p = g(wt, "path").clone();
    let branch = if truthy(&p) { json!(ensure_branch(&str_(&p), &want, upstream.as_deref())?) } else { Value::Null };
    Ok((p, branch))
}

fn log(entry: Value) -> Res<()> {
    let f = log_file();
    std::fs::create_dir_all(f.parent().unwrap())?;
    let mut o = json!({"ts": py::time() as i64});
    o.as_object_mut().unwrap().extend(entry.as_object().cloned().unwrap_or_default());
    let mut fh = std::fs::OpenOptions::new().create(true).append(true).open(&f).or_else(|e| py::oserr(e, &f))?;
    writeln!(fh, "{}", py::dumps(&o))?;
    Ok(())
}

fn apply(p: &mut Plan) -> Res<()> {
    let now = py::time() as i64;
    save(&dismissed_file(), &Value::Object(p.dismissed.clone()))?;
    let st = dict(&load(&state_file(), json!({})), "adopt.json")?;
    let mut created: Map<String, Value> =
        dict(st.get("created").unwrap_or(&Value::Null), "adopt.json created")?.into_iter().filter(|(k, _)| !p.seen.contains_key(k)).collect();
    for &i in &p.create {
        let c = &mut p.items[i];
        let r = ref_(gs(c, "forge"), &c["iid"]);
        let res = (|| -> Res<()> {
            let (path, branch) = create(c)?;
            c["created"] = json!(true);
            c["path"] = path;
            c["branch"] = branch;
            created.insert(str_(&c["key"]), json!(now));
            log(json!({"ws": c["name"], "mr": r, "did": "adopt", "project": c["project"], "role": c["role"]}))?;
            greenlight::note(gs(c, "name"), &format!("adopted {r} ({}): no workspace existed", str_(&c["role"])))
        })();
        if let Err(e) = res {
            c["created"] = json!(false);
            c["error"] = json!(py::tail(&e.to_string(), 300));
            log(json!({"ws": c["name"], "mr": r, "did": "adopt failed", "error": c["error"]}))?;
        }
    }
    save(&state_file(), &json!({"seen": p.seen, "created": created}))?;
    for (key, text) in &p.pings {
        // ponytail: Python redirects stdout to stderr around greenlight.ping so its "skipped" note can't pollute
        // --json; here the flap guard is checked first and the note goes straight to stderr
        if key.starts_with('@') && greenlight::recent_keys()?.contains(key) {
            eprintln!("skipped: {key} was pinged in the last {}h", greenlight::FLAP as i64 / 3600);
        } else {
            greenlight::ping(text, Some(key), vec![])?;
        }
    }
    Ok(())
}

const HELP: &str = "usage: adopt [-h] [--json] [--cap CAP] {scan,precheck,apply}

positional arguments:
  {scan,precheck,apply}

options:
  -h, --help            show this help message and exit
  --json
  --cap CAP";

/// argparse's test for a value that looks like a negative number (taken as --cap's value, not an option).
fn negnum(a: &str) -> bool {
    let d = |s: &str| s.chars().all(|c| c.is_ascii_digit());
    a.strip_prefix('-').is_some_and(|r| !r.is_empty() && (d(r) || r.split_once('.').is_some_and(|(x, y)| d(x) && !y.is_empty() && d(y))))
}

pub fn main(args: Vec<String>) -> Res<i32> {
    if args.first().map(String::as_str) == Some("dismiss") {
        // adopt dismiss <project>!<iid>...: never adopt these (the no-repo ping's "ignore")
        let f = dismissed_file();
        let mut d = dict(&load(&f, json!({})), "dismissed.json")?;
        let t = py::time() as i64;
        for k in &args[1..] {
            d.insert(k.clone(), json!({"ts": t, "why": "ignored"}));
        }
        save(&f, &Value::Object(d))?;
        return Ok(0);
    }
    // argparse: one positional cmd, --json, --cap N, -h, unambiguous prefixes of the long options; errors in argv order
    let (mut cmd, mut json_, mut cap_, mut bad) = (None, false, cap(), vec![]);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let (name, val) = a.split_once('=').map_or((a.as_str(), None), |(n, v)| (n, Some(v)));
        let hit: Vec<&str> = if a == "-h" {
            vec!["--help"]
        } else if name.len() > 2 && name.starts_with("--") {
            ["--help", "--json", "--cap"].into_iter().filter(|o| o.starts_with(name)).collect()
        } else {
            vec![]
        };
        match hit.as_slice() {
            ["--help" | "--json"] if val.is_some() => py::argparse_exit("adopt", &format!("argument {}: ignored explicit argument '{}'", hit[0], val.unwrap())),
            ["--help"] => {
                println!("{HELP}");
                return Ok(0);
            }
            ["--json"] => json_ = true,
            ["--cap"] => {
                let v = match val {
                    Some(v) => v.to_string(),
                    None => match it.as_slice().first() {
                        Some(n) if !n.starts_with('-') || negnum(n) => it.next().unwrap().clone(),
                        _ => py::argparse_exit("adopt", "argument --cap: expected one argument"),
                    },
                };
                match v.trim().replace('_', "").parse::<i64>() {
                    Ok(n) => cap_ = json!(n),
                    Err(_) => py::argparse_exit("adopt", &format!("argument --cap: invalid int value: {}", py::repr_str(&v))),
                }
            }
            _ if a.starts_with('-') && a.len() > 1 && !negnum(a) => bad.push(a.as_str()),
            _ if cmd.is_some() => bad.push(a.as_str()),
            _ if ["scan", "precheck", "apply"].contains(&a.as_str()) => cmd = Some(a.as_str()),
            _ => py::argparse_exit("adopt", &format!("argument cmd: invalid choice: {} (choose from 'scan', 'precheck', 'apply')", py::repr_str(a))),
        }
    }
    let Some(cmd) = cmd else { py::argparse_exit("adopt", "the following arguments are required: cmd") };
    if !bad.is_empty() {
        py::argparse_exit("adopt", &format!("unrecognized arguments: {}", bad.join(" ")));
    }

    let mut p = match plan(None, &cap_) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("adopt plan failed: {e}");
            return Ok(if cmd == "precheck" { 1 } else { 2 });
        }
    };
    if cmd == "precheck" {
        println!("{} workspace(s) to adopt", p.create.len());
        return Ok(if p.create.is_empty() { 1 } else { 0 });
    }
    if cmd == "apply" {
        apply(&mut p)?;
    }
    if json_ {
        let out: Vec<Value> = p.items.iter().map(|i| Value::Object(i.as_object().unwrap().iter().filter(|(k, _)| *k != "repo").map(|(k, v)| (k.clone(), v.clone())).collect())).collect();
        println!("{}", py::dumps_indent(&json!(out)));
        return Ok(0);
    }
    let titles = [
        ("create", if cmd == "apply" { "CREATED" } else { "WOULD CREATE" }),
        ("deferred", "DEFERRED (cap)"),
        ("busy", "BRANCH BUSY (pinged)"),
        ("no-repo", "NO ADE REPO (pinged)"),
        ("skip", "SKIPPED"),
    ];
    for (verdict, title) in titles {
        let group: Vec<&Value> = p.items.iter().filter(|i| i["verdict"] == verdict).collect();
        if group.is_empty() {
            continue;
        }
        println!("\n## {title} ({})", group.len());
        for i in group {
            let mark = if verdict != "create" || cmd != "apply" { "" } else if truthy(g(i, "created")) { "✓ " } else { "✗ " };
            let block = if truthy(g(i, "block")) { format!("/{}", str_(&i["block"])) } else { String::new() };
            let err = if truthy(g(i, "error")) { format!(" — {}", str_(&i["error"])) } else { String::new() };
            println!("- {mark}{} [{}{block}] {} — {}{err}", str_(&i["key"]), str_(&i["role"]), py::head(&str_(&i["title"]), 60), str_(&i["reason"]));
        }
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parsing() {
        let p = |u| parse_remote(u).map(|(h, p)| format!("{h} {p}"));
        assert_eq!(p("git@gitlab.com:acme/widgets.git").unwrap(), "gitlab.com acme/widgets");
        assert_eq!(p("https://user@github.com/o/r/").unwrap(), "github.com o/r");
        assert_eq!(p("ssh://git@gitlab.com:22/a/b/c").unwrap(), "gitlab.com a/b/c");
        assert_eq!(p("nonsense"), None);
        assert!(negnum("-1") && negnum("-2.5") && !negnum("-x") && !negnum("-") && !negnum("--cap"));
    }
}
