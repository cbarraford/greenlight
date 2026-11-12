//! Port of scripts/greenlight.py (lines 178-906; the top of the file is base.rs).
use crate::base::{self, cfg, me, orca, orca_s, pid, ref_, OWNERS, ROOT, STATE};
use crate::py::{self, truthy, PyErr, Res};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::cmp::Ordering;
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

// ---- Python-style access to decoded JSON, so a malformed reply fails with the scripts' error text ----

static NULL: Value = Value::Null;

fn tname(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}
/// v[k]
fn ix<'a>(v: &'a Value, k: &str) -> Res<&'a Value> {
    match v {
        Value::Object(m) => m.get(k).map_or_else(|| py::key_err(k), Ok),
        Value::String(_) => py::raise("TypeError", "string indices must be integers, not 'str'"),
        Value::Array(_) => py::raise("TypeError", "list indices must be integers or slices, not str"),
        _ => py::raise("TypeError", format!("'{}' object is not subscriptable", tname(v))),
    }
}
/// v[i] on a list (negative counts from the end)
fn at(v: &Value, i: isize) -> Res<&Value> {
    match v {
        Value::Array(a) => {
            let j = if i < 0 { a.len() as isize + i } else { i };
            usize::try_from(j).ok().and_then(|j| a.get(j)).map_or_else(|| py::raise("IndexError", "list index out of range"), Ok)
        }
        Value::Object(_) => py::raise("KeyError", i.to_string()),
        _ => py::raise("TypeError", format!("'{}' object is not subscriptable", tname(v))),
    }
}
/// v.get(k). ponytail: a non-dict reads as None here, where Python raises AttributeError.
fn g<'a>(v: &'a Value, k: &str) -> &'a Value {
    v.get(k).unwrap_or(&NULL)
}
/// v.get(k, default)
fn gd(v: &Value, k: &str, d: Value) -> Value {
    v.get(k).cloned().unwrap_or(d)
}
/// `a or b`
fn or<'a>(a: &'a Value, b: &'a Value) -> &'a Value {
    if truthy(a) { a } else { b }
}
/// iter(v): a list's items, a dict's keys, a str's chars.
fn it(v: &Value) -> Res<Vec<Value>> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(m) => Ok(m.keys().map(|k| json!(k)).collect()),
        Value::String(s) => Ok(s.chars().map(|c| json!(c.to_string())).collect()),
        _ => py::raise("TypeError", format!("'{}' object is not iterable", tname(v))),
    }
}
/// `u.get("login") or u["username"]`: the forge handle (GitHub users carry both).
fn login(u: &Value) -> Res<Value> {
    Ok(if truthy(g(u, "login")) { u["login"].clone() } else { ix(u, "username")?.clone() })
}
/// Sort key compare for `x or 0` keys (numbers, or ISO strings).
fn cmp_key(a: &Value, b: &Value) -> Ordering {
    match (a, b) {
        (Value::String(x), Value::String(y)) => x.cmp(y),
        _ => a.as_f64().unwrap_or(0.0).partial_cmp(&b.as_f64().unwrap_or(0.0)).unwrap_or(Ordering::Equal),
    }
}
/// An io error as Python's OSError str(): `[Errno 2] No such file or directory: '<path>'`.
fn oserr<T>(e: std::io::Error, p: &Path) -> Res<T> {
    let msg = e.to_string();
    let msg = msg.split(" (os error").next().unwrap_or(&msg);
    let kind = if e.kind() == std::io::ErrorKind::NotFound { "FileNotFoundError" } else { "OSError" };
    py::raise(kind, format!("[Errno {}] {msg}: {}", e.raw_os_error().unwrap_or(0), py::repr_str(&p.display().to_string())))
}
fn read(p: &Path) -> Res<String> {
    std::fs::read_to_string(p).or_else(|e| oserr(e, p))
}
/// glab api, naming the call when the reply isn't JSON (Python said only "Expecting value: line 1 column 1 (char 0)").
fn gl(path: &str) -> Res<Value> {
    base::gl(path).map_err(|e| match py::kind(&*e) {
        "JSONDecodeError" => PyErr { kind: "JSONDecodeError", msg: format!("glab api {path}: not JSON ({e})") }.into(),
        _ => e,
    })
}

// ---- lifecycles ----

/// ~/.greenlight/lifecycles/<repo>.md, else the bundled lifecycles/<repo>.md, else default.md.
fn lifecycle_file(project: &str) -> PathBuf {
    let name = format!("{}.md", project.rsplit('/').next().unwrap_or(""));
    [STATE.join("lifecycles").join(&name), ROOT.join("lifecycles").join(&name)]
        .into_iter()
        .find(|f| f.exists())
        .unwrap_or_else(|| ROOT.join("lifecycles").join("default.md"))
}

pub static BUILTIN: LazyLock<Value> = LazyLock::new(|| {
    json!({"mr_open": "An MR exists for the branch.", "ci_green": "The head pipeline succeeded (or there's no CI).",
           "no_conflicts": "No merge conflicts.", "review_bot": "The review bot is assignee and reviewer.",
           "milestone": "A release milestone is set.", "ready": "Not a draft.", "threads": "No unresolved threads.",
           "approved": "The required approvals are in.", "merge": "The forge reports it mergeable."})
});
static STAGE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^\s*\d+\.\s+`(\w+)`[ \t]*(?:—|-)?[ \t]*(.*)$").unwrap());
static DONE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s*Done:\s*`(signoff|label=[^`]+)`").unwrap());

/// The lifecycle as data: {stages: [{key, req, done}], options, rules, intro, file, source}.
/// A stage that isn't built in passes on `Done: `signoff`` (the user signs it off once per MR) or `Done: `label=x``.
pub fn lifecycle_doc(project: &str) -> Res<Value> {
    let f = lifecycle_file(project);
    let text = read(&f)?;
    let (head, rules) = text.split_once("\n## Rules").unwrap_or((&text, ""));
    let mut stages = vec![];
    for c in STAGE_RE.captures_iter(head) {
        let (key, rest) = (&c[1], &c[2]);
        let done = match DONE_RE.captures(rest) {
            Some(m) => m[1].to_string(),
            None if BUILTIN.get(key).is_some() => String::new(),
            None => "signoff".into(),
        };
        stages.push(json!({"key": key, "req": py::strip(&DONE_RE.replace_all(rest, "")), "done": done}));
    }
    let m = Regex::new(r"(?m)^Options:(.*)$").unwrap().captures(head);
    let first = STAGE_RE.find(head).map(|x| x.start()).or_else(|| m.as_ref().map(|m| m.get(0).unwrap().start()));
    let default = f.file_name() == Some("default.md".as_ref());
    let intro = if default && f.parent() == Some(ROOT.join("lifecycles").as_path()) {
        String::new()
    } else {
        let h = first.map_or(head, |i| &head[..i]);
        py::strip(&Regex::new(r"(?m)^Options:.*$|\A#.*$").unwrap().replace_all(h, "")).to_string()
    };
    let mut options = Map::new();
    if let Some(m) = &m {
        for c in Regex::new(r"`(\w+)=?([^`]*)`").unwrap().captures_iter(&m[1]) {
            options.insert(c[1].into(), json!(&c[2]));
        }
    }
    let source = if f.parent() == Some(STATE.join("lifecycles").as_path()) {
        "yours"
    } else if default {
        "default"
    } else {
        "bundled"
    };
    Ok(json!({"stages": stages, "options": options, "rules": py::strip(rules), "intro": intro,
              "file": f.display().to_string(), "source": source}))
}

fn one_line(t: &Value) -> String {
    let s = if truthy(t) { py::str_(t) } else { String::new() };
    py::split_ws(&s).join(" ")
}

/// The inverse of lifecycle_doc. Raises ValueError on a bad or duplicate key, or a label with a backtick.
pub fn lifecycle_md(repo: &str, doc: &Value) -> Res<String> {
    let stages = it(ix(doc, "stages")?)?;
    let keys = stages.iter().map(|s| ix(s, "key").cloned()).collect::<Res<Vec<_>>>()?;
    let word = Regex::new(r"^\w+$").unwrap();
    let dup = (1..keys.len()).any(|i| keys[..i].contains(&keys[i]));
    if keys.is_empty() || dup || !keys.iter().all(|k| k.as_str().is_some_and(|k| word.is_match(k))) {
        return py::raise("ValueError", format!("stage keys must be unique words: {}", py::repr(&Value::Array(keys))));
    }
    let mut out = vec![format!("# {repo} lifecycle"), String::new()];
    let intro = py::strip(py::gs(doc, "intro"));
    if !intro.is_empty() {
        out.extend([intro.to_string(), String::new()]);
    }
    if let Some(opts) = g(doc, "options").as_object().filter(|o| !o.is_empty()) {
        let o: Vec<String> = opts
            .iter()
            .map(|(k, v)| if truthy(v) { format!("`{k}={}`", py::str_(v)) } else { format!("`{k}`") })
            .collect();
        out.extend([format!("Options: {}", o.join(" ")), String::new()]);
    }
    let done_re = Regex::new(r"^(?:|signoff|label=[^`]+)$").unwrap();
    for (i, s) in stages.iter().enumerate() {
        let key = py::str_(&s["key"]);
        let done = if BUILTIN.get(&key).is_some() {
            String::new()
        } else {
            Some(one_line(g(s, "done"))).filter(|d| !d.is_empty()).unwrap_or_else(|| "signoff".into())
        };
        if !done_re.is_match(&done) {
            return py::raise("ValueError", format!("bad Done for {key}: {done}"));
        }
        let tail = if done.is_empty() { String::new() } else { format!(" Done: `{done}`") };
        out.push(format!("{}. `{key}` — {}{tail}", i + 1, one_line(g(s, "req"))));
    }
    let rules = py::strip(py::gs(doc, "rules"));
    if !rules.is_empty() {
        out.extend([String::new(), "## Rules".into(), String::new(), rules.to_string()]);
    }
    Ok(out.join("\n") + "\n")
}

/// (lifecycle file name, stage keys)
pub fn lifecycle(project: &str) -> Res<(String, Vec<String>)> {
    let doc = lifecycle_doc(project)?;
    let name = Path::new(py::gs(&doc, "file")).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    Ok((name, doc["stages"].as_array().into_iter().flatten().map(|s| py::str_(&s["key"])).collect()))
}

/// {flag: value} from the backticks on the lifecycle's `Options:` line: `solo` -> {"solo": ""}, `milestone=Release-`.
fn lifecycle_opts(project: &str) -> Res<Value> {
    Ok(lifecycle_doc(project)?["options"].take())
}

pub fn signoffs() -> Res<Value> {
    let f = STATE.join("signoffs.json");
    if f.exists() { py::loads(&std::fs::read_to_string(f)?) } else { Ok(json!({})) }
}

/// The active milestone named <prefix>x.y.z (prefix from `milestone=`, default Release-) due latest; None when ambiguous.
pub fn release_milestone(project: &str, forge: &str) -> Res<Value> {
    let opts = lifecycle_opts(project)?;
    let prefix = regex::escape(&py::str_(or(g(&opts, "milestone"), &json!("Release-"))));
    let ms = if forge == "github" {
        let mut ms = it(&base::gh(&["api", &format!("repos/{project}/milestones?state=open&per_page=100")])?)?;
        for m in &mut ms {
            let due = py::head(py::gs(m, "due_on"), 10).to_string();
            m["due_date"] = json!(due); // ponytail: IndexMut panics on a non-dict milestone; GitHub never sends one
        }
        ms
    } else {
        base::gl_all(&format!("projects/{}/milestones?state=active", pid(project)))?
    };
    let re = Regex::new(&format!(r"^(?:{prefix}\d+\.\d+\.\d+)$")).unwrap();
    let mut keep = vec![];
    for m in ms {
        if re.is_match(&py::str_(ix(&m, "title")?)) && truthy(g(&m, "due_date")) {
            keep.push(m);
        }
    }
    keep.sort_by(|a, b| cmp_key(&b["due_date"], &a["due_date"]));
    if keep.is_empty() || (keep.len() > 1 && keep[0]["due_date"] == keep[1]["due_date"]) {
        return Ok(Value::Null); // ambiguous: greenlight asks
    }
    Ok(keep.swap_remove(0))
}

// ---- MR / PR facts ----

pub fn mr_details(project: &str, iid: &Value, forge: &str) -> Res<Value> {
    if forge == "github" {
        return gh_pr(project, iid);
    }
    let base_ = format!("projects/{}/merge_requests/{}", pid(project), py::str_(iid));
    let mut mr = gl(&base_)?;
    if ix(&mr, "state")? != "opened" {
        return Ok(mr);
    }
    let disc = base::gl_all(&format!("{base_}/discussions"))?;
    let ap = gl(&format!("{base_}/approvals"))?;
    let approved_by = it(&gd(&ap, "approved_by", json!([])))?
        .iter()
        .map(|a| Ok(ix(ix(a, "user")?, "username")?.clone()))
        .collect::<Res<Vec<_>>>()?;
    let mut mine = vec![];
    for d in &disc {
        for n in it(ix(d, "notes")?)? {
            if ix(ix(&n, "author")?, "username")? == me() && truthy(g(g(&n, "position"), "head_sha")) {
                mine.push(n);
            }
        }
    }
    // ponytail: the head your newest diff comment saw; an approval or a plain comment carries no sha
    let mut newest: Option<(&Value, String)> = None;
    for n in &mine {
        let c = py::str_(ix(n, "created_at")?);
        if newest.as_ref().is_none_or(|(_, b)| c > *b) {
            newest = Some((n, c));
        }
    }
    let reviewed = match newest {
        Some((n, _)) => ix(ix(n, "position")?, "head_sha")?.clone(),
        None => Value::Null,
    };
    let pl = g(&mr, "head_pipeline").clone();
    let failed = if g(&pl, "status") == "failed" {
        // the Fix CI prompt's {failed_jobs}
        let jobs = base::gl_all(&format!("projects/{}/pipelines/{}/jobs?scope[]=failed", pid(project), py::str_(ix(&pl, "id")?)))?;
        let mut out = vec![];
        for j in jobs.iter().filter(|j| !truthy(g(j, "allow_failure"))) {
            out.push(json!({"name": ix(j, "name")?, "stage": ix(j, "stage")?, "url": ix(j, "web_url")?}));
        }
        Some(out)
    } else {
        None
    };
    let blocked = if g(&mr, "detailed_merge_status") == "merge_request_blocked" {
        // depends on other MRs: name the open ones
        let mut out = vec![];
        for b in it(&gl(&format!("{base_}/blocks"))?)? {
            let bm = ix(&b, "blocking_merge_request")?;
            if ix(bm, "state")? == "opened" {
                out.push(ix(bm, "iid")?.clone());
            }
        }
        Some(out)
    } else {
        None
    };
    let o = mr.as_object_mut().unwrap();
    o.insert("_discussions".into(), json!(disc));
    o.insert("_approved_by".into(), json!(approved_by));
    o.insert("_approvals_required".into(), or(g(&ap, "approvals_required"), &json!(0)).clone());
    o.insert("_approvals_left".into(), or(g(&ap, "approvals_left"), &json!(0)).clone());
    o.insert("_can_approve".into(), gd(&ap, "user_can_approve", json!(true))); // False when my commits are in it
    o.insert("_reviewed_sha".into(), reviewed);
    if let Some(f) = failed {
        o.insert("_failed_jobs".into(), json!(f));
    }
    if let Some(b) = blocked {
        o.insert("_blocked_by".into(), json!(b));
    }
    Ok(mr)
}

const GH_PR_QUERY: &str = r#"query($owner: String!, $name: String!, $n: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $n) {
      number url title body state isDraft updatedAt author { login avatarUrl }
      headRefName headRefOid baseRefName baseRef { branchProtectionRule { requiredApprovingReviewCount } } mergeable mergeStateStatus reviewDecision
      labels(first: 30) { nodes { name } }
      milestone { title }
      assignees(first: 20) { nodes { login avatarUrl } }
      reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
      latestOpinionatedReviews(first: 30) { nodes { state author { login } } }
      myReviews: reviews(last: 30) { nodes { author { login } commit { oid } } }
      reviewThreads(first: 100) { nodes { isResolved
        first: comments(first: 1) { nodes { author { login } body path line } } last: comments(last: 1) { nodes { author { login } } } } }
      commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { nodes { __typename
        ... on CheckRun { name status conclusion detailsUrl checkSuite { workflowRun { workflow { name } } } }
        ... on StatusContext { context state targetUrl } } } } } } }
    }
  }
}"#;

fn gh_state(s: &Value) -> Res<&'static str> {
    match s.as_str() {
        Some("OPEN") => Ok("opened"),
        Some("MERGED") => Ok("merged"),
        Some("CLOSED") => Ok("closed"),
        _ => py::raise("KeyError", py::repr(s)),
    }
}
fn gh_rollup(s: &Value) -> &'static str {
    match s.as_str() {
        Some("SUCCESS") => "success",
        Some("PENDING" | "EXPECTED") => "running",
        _ => "failed",
    }
}

fn gh_open_prs(project: &str) -> Res<Value> {
    base::gh(&["pr", "list", "-R", project, "--state", "open", "--limit", "500", "--json", "number,headRefName,closingIssuesReferences"])
}

/// A CheckRun / StatusContext -> GitLab job status.
fn gh_check_status(c: &Value) -> Res<&'static str> {
    if ix(c, "__typename")? == "StatusContext" {
        return Ok(match ix(c, "state")?.as_str() {
            Some("SUCCESS") => "success",
            Some("PENDING" | "EXPECTED") => "pending",
            _ => "failed",
        });
    }
    let status = ix(c, "status")?;
    if status != "COMPLETED" {
        return Ok(if status == "IN_PROGRESS" { "running" } else { "pending" });
    }
    Ok(match ix(c, "conclusion")?.as_str() {
        Some("SUCCESS" | "NEUTRAL") => "success",
        Some("SKIPPED") => "skipped",
        _ => "failed",
    })
}

/// Orca's linked MR/PR as its number: newer Orca stores linkedPR as {"number": 39, ...}, not 39.
fn linked_number(v: &Value) -> Value {
    if v.is_object() { g(v, "number").clone() } else { v.clone() }
}

/// `(x or [{}])[0]`
fn first_or_empty(x: &Value) -> Res<Value> {
    Ok(if truthy(x) { at(x, 0)?.clone() } else { json!({}) })
}

/// A GitHub PR in the GitLab MR shape mr_details returns. The gh user (the viewer) becomes ME,
/// so every author/approver test in check() and evaluate() reads the same for both forges.
fn gh_pr(project: &str, number: &Value) -> Res<Value> {
    let Some((owner, name)) = project.split_once('/') else {
        return py::raise("ValueError", "not enough values to unpack (expected 2, got 1)");
    };
    let cmd: Vec<String> = ["gh", "api", "graphql", "-F", &format!("owner={owner}"), "-F", &format!("name={name}"), "-F",
                            &format!("n={}", py::str_(number)), "-f", &format!("query={GH_PR_QUERY}")]
        .iter()
        .map(|s| s.to_string())
        .collect();
    let d = ix(&py::loads(&base::run(&cmd, 45.0, 3)?)?, "data")?.clone();
    let me_ = ix(ix(&d, "viewer")?, "login")?.clone();
    let repo = ix(&d, "repository")?;
    let p = ix(repo, "pullRequest")?;
    let user = |u: &Value| {
        let l = g(u, "login");
        let l_or_ghost = if truthy(l) { l.clone() } else { json!("ghost") };
        json!({"username": if *l == me_ { json!(me()) } else { l_or_ghost.clone() }, "login": l_or_ghost, "avatar_url": g(u, "avatarUrl")})
    };
    let roll = g(g(&first_or_empty(ix(ix(p, "commits")?, "nodes")?)?, "commit"), "statusCheckRollup").clone();
    let pipeline = if truthy(&roll) { json!(gh_rollup(ix(&roll, "state")?)) } else { roll.clone() };
    let rt = it(ix(ix(p, "reviewThreads")?, "nodes")?)?;
    let mut threads = vec![];
    for t in &rt {
        let f = first_or_empty(ix(ix(t, "first")?, "nodes")?)?;
        let first = json!({"resolvable": true, "resolved": ix(t, "isResolved")?, "author": user(g(&f, "author")),
                           "body": or(g(&f, "body"), &json!("")), "position": {"new_path": g(&f, "path"), "new_line": g(&f, "line")}});
        let last = first_or_empty(ix(ix(t, "last")?, "nodes")?)?;
        threads.push(json!({"notes": [first, {"author": user(g(&last, "author"))}]}));
    }
    let mut reviews = vec![];
    for r in it(ix(ix(p, "latestOpinionatedReviews")?, "nodes")?)? {
        reviews.push((ix(&r, "state")?.clone(), user(ix(&r, "author")?)["username"].clone()));
    }
    let mut requested = vec![];
    for r in it(ix(ix(p, "reviewRequests")?, "nodes")?)? {
        let rr = g(&r, "requestedReviewer");
        if truthy(rr) {
            requested.push(or(g(rr, "login"), g(rr, "name")).clone());
        }
    }
    let (mss, decision) = (ix(p, "mergeStateStatus")?, ix(p, "reviewDecision")?);
    let status = if ix(p, "state")? != "OPEN" {
        None
    } else if truthy(ix(p, "isDraft")?) {
        Some("draft_status")
    } else if ix(p, "mergeable")? == "CONFLICTING" || mss == "DIRTY" {
        Some("conflict")
    } else if mss == "BEHIND" {
        Some("need_rebase")
    } else if decision == "CHANGES_REQUESTED" {
        Some("requested_changes")
    } else if decision == "REVIEW_REQUIRED" {
        Some("not_approved")
    } else if pipeline == "running" {
        Some("ci_still_running")
    } else if mss == "CLEAN" || mss == "HAS_HOOKS" || mss == "UNSTABLE" {
        Some("mergeable") // UNSTABLE: only non-required checks failed (ci_green catches them)
    } else if mss == "BLOCKED" {
        Some(if pipeline == "failed" { "ci_must_pass" } else { "blocked_status" })
    } else {
        Some("checking") // UNKNOWN: GitHub computes mergeability lazily
    };
    let mut checks = vec![];
    let nodes = g(g(&roll, "contexts"), "nodes");
    for c in if nodes.is_null() { vec![] } else { it(nodes)? } {
        let stage = or(g(g(g(g(&c, "checkSuite"), "workflowRun"), "workflow"), "name"), &json!("checks")).clone();
        checks.push(json!({"name": or(g(&c, "name"), g(&c, "context")), "status": gh_check_status(&c)?,
                           "url": or(g(&c, "detailsUrl"), g(&c, "targetUrl")), "stage": stage}));
    }
    let mut revs: Vec<Value> = vec![];
    for n in requested.iter().chain(reviews.iter().map(|(_, u)| u)) {
        if !revs.contains(n) {
            revs.push(n.clone());
        }
    }
    let author = ix(p, "author")?;
    let my_reviews = g(g(p, "myReviews"), "nodes");
    let mut reviewed = Value::Null;
    for r in if truthy(my_reviews) { it(my_reviews)? } else { vec![] }.iter().rev() {
        if *g(g(r, "author"), "login") == me_ && truthy(g(r, "commit")) {
            reviewed = ix(ix(r, "commit")?, "oid")?.clone();
            break;
        }
    }
    let url = py::str_(ix(p, "url")?);
    let failed: Vec<Value> = checks.iter().filter(|c| c["status"] == "failed").cloned().collect();
    Ok(json!({
        "iid": ix(p, "number")?, "web_url": url, "title": ix(p, "title")?, "description": g(p, "body"), "state": gh_state(ix(p, "state")?)?,
        "draft": ix(p, "isDraft")?, "author": user(author), "has_conflicts": ix(p, "mergeable")? == "CONFLICTING",
        "assignees": it(ix(ix(p, "assignees")?, "nodes")?)?.iter().map(user).collect::<Vec<_>>(),
        "reviewers": revs.into_iter().map(|n| json!({"username": n})).collect::<Vec<_>>(),
        "milestone": ix(p, "milestone")?, "labels": it(ix(ix(p, "labels")?, "nodes")?)?.iter().map(|l| ix(l, "name").cloned()).collect::<Res<Vec<_>>>()?,
        "head_pipeline": if truthy(&pipeline) { json!({"status": pipeline, "web_url": format!("{url}/checks")}) } else { pipeline.clone() },
        "detailed_merge_status": status, "blocking_discussions_resolved": rt.iter().all(|t| truthy(&t["isResolved"])),
        "source_branch": ix(p, "headRefName")?, "target_branch": ix(p, "baseRefName")?, "sha": ix(p, "headRefOid")?, "updated_at": ix(p, "updatedAt")?,
        "_discussions": threads, "_approved_by": reviews.iter().filter(|(s, _)| s == "APPROVED").map(|(_, u)| u.clone()).collect::<Vec<_>>(),
        // ponytail: classic branch protection only (null unless you can read it); rulesets aren't counted
        "_approvals_required": or(g(g(g(p, "baseRef"), "branchProtectionRule"), "requiredApprovingReviewCount"), &json!(0)),
        "_approvals_left": (decision == "REVIEW_REQUIRED") as i64,
        "_can_approve": author.is_null() || *ix(author, "login")? != me_, // GitHub won't let you approve your own PR
        "_reviewed_sha": reviewed,
        "_default_branch": g(g(repo, "defaultBranchRef"), "name"),
        "_checks": checks, "_failed_jobs": failed,
        "_forge": "github",
    }))
}

/// Discussions with a resolvable, unresolved first note.
pub fn unresolved(mr: &Value) -> Res<Vec<Value>> {
    let mut out = vec![];
    for d in it(&gd(mr, "_discussions", json!([])))? {
        let notes = ix(&d, "notes")?;
        if truthy(notes) && truthy(g(at(notes, 0)?, "resolvable")) && !truthy(g(at(notes, 0)?, "resolved")) {
            out.push(d);
        }
    }
    Ok(out)
}

/// 'author, file:line: "first line"' for one discussion: a {threads} entry.
fn thread_line(d: &Value) -> Res<String> {
    let n = at(ix(d, "notes")?, 0)?;
    let pos = g(n, "position");
    let w = or(g(pos, "new_path"), g(pos, "old_path"));
    let w = if truthy(w) {
        let line = or(or(g(pos, "new_line"), g(pos, "old_line")), &json!("")).clone();
        json!(format!("{}:{}", py::str_(w), py::str_(&line)).trim_end_matches(':'))
    } else {
        w.clone()
    };
    let body = py::str_(or(g(n, "body"), &json!("")));
    let text = py::splitlines(&body).into_iter().map(py::strip).find(|l| !l.is_empty()).unwrap_or("");
    let text = if text.chars().count() > 160 { format!("{}…", py::head(text, 160)) } else { text.to_string() };
    let parts: Vec<String> = [login(ix(n, "author")?)?, w].iter().filter(|x| truthy(x)).map(py::str_).collect();
    Ok(parts.join(", ") + &if text.is_empty() { String::new() } else { format!(": \"{text}\"") })
}

/// What I wrote in the goal: the goal (and any old status line) minus the MR's own title, which
/// "Merge !N: <title>" goals copy in. A title like "hold inbounds on ..." is never an instruction.
/// If the title was edited since, the old copy stays in, so it can still ask me (never merge by mistake).
fn said(ws: &Value, mr: &Value) -> String {
    let title = py::strip(py::gs(mr, "title"));
    let mut texts: Vec<String> = vec![];
    for t in [py::gs(ws, "goal"), py::gs(ws, "comment")] {
        if !t.is_empty() && !texts.iter().any(|x| x == t) {
            texts.push(t.into());
        }
    }
    if !title.is_empty() {
        let re = Regex::new(&format!("(?i){}", regex::escape(title))).unwrap();
        texts = texts.iter().map(|t| re.replace_all(t, " ").into_owned()).collect();
    }
    texts.join("\n")
}

fn fre(re: &fancy_regex::Regex, s: &str) -> bool {
    re.is_match(s).unwrap_or(false)
}

type Hit = Option<(&'static str, String, Value)>;

/// -> None when the step passes, else (owner, action, cmd|None).
fn check(step: &str, mr: &Value, ws: &Value, milestone: &Value) -> Res<Hit> {
    let (iid, proj) = (py::str_(ix(mr, "iid")?), py::str_(ix(ws, "project")?));
    let github = g(ws, "forge") == "github";
    let sv = |xs: &[&str]| xs.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    let upd = if github { sv(&["gh", "pr", "edit", &iid, "-R", &proj]) } else { sv(&["glab", "mr", "update", &iid, "-R", &proj]) };
    let me_ = json!(me());
    let bot = json!(base::bot());
    let hit = |owner: &'static str, action: String, cmd: Option<Vec<String>>| Ok(Some((owner, action, json!(cmd))));
    match step {
        "mr_open" => return Ok(None),
        "ci_green" => {
            let st = g(g(mr, "head_pipeline"), "status");
            if st.is_null() || ["success", "manual", "skipped"].iter().any(|x| st == *x) {
                return Ok(None);
            }
            if ["running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"].iter().any(|x| st == *x) {
                return hit("ci", format!("pipeline {}", py::str_(st)), None);
            }
            let url = py::str_(ix(ix(mr, "head_pipeline")?, "web_url")?);
            return hit("agent", format!("Fix CI (pipeline {}: {url})", py::str_(st)), None);
        }
        "no_conflicts" => {
            return if truthy(ix(mr, "has_conflicts")?) { hit("agent", "Rebase Branch (merge conflicts)".into(), None) } else { Ok(None) };
        }
        "review_bot" => {
            let Some(b) = base::bot() else { return Ok(None) };
            let names = |k: &str| -> Res<Vec<Value>> { it(ix(mr, k)?)?.iter().map(|u| ix(u, "username").cloned()).collect() };
            let (a, r) = (names("assignees")?, names("reviewers")?);
            if a.contains(&bot) && r.contains(&bot) {
                return Ok(None);
            }
            let mut cmd = upd.clone();
            cmd.extend(if github { sv(&["--add-assignee", b, "--add-reviewer", b]) } else { sv(&["--assignee", &format!("+{b}"), "--reviewer", &format!("+{b}")]) });
            return hit("greenlight", format!("assign {b} as assignee + reviewer"), Some(cmd));
        }
        "milestone" => {
            if truthy(g(mr, "milestone")) {
                return Ok(None);
            }
            if !truthy(milestone) {
                return hit("me", "which milestone? latest active release is ambiguous".into(), None);
            }
            let title = py::str_(ix(milestone, "title")?);
            let mut cmd = upd.clone();
            cmd.extend(sv(&["--milestone", &title]));
            return hit("greenlight", format!("set milestone {title}"), Some(cmd));
        }
        "ready" => {
            if !truthy(ix(mr, "draft")?) {
                return Ok(None);
            }
            if fre(&base::KEEP_DRAFT_RE, &said(ws, mr)) {
                return hit("me", "goal says keep draft but everything else is green: take it out of draft?".into(), None);
            }
            let mut cmd = upd.clone();
            cmd.push("--ready".into());
            return hit("greenlight", "mark ready for review".into(), Some(if github { sv(&["gh", "pr", "ready", &iid, "-R", &proj]) } else { cmd }));
        }
        "threads" => {
            let ds = unresolved(mr)?;
            let mut todo = 0;
            for d in &ds {
                let u = ix(ix(at(ix(d, "notes")?, -1)?, "author")?, "username")?;
                if *u != me_ && *u != bot {
                    todo += 1;
                }
            }
            if !ds.is_empty() && todo == 0 {
                // we replied last on every one: the reviewer has to re-review
                return hit("reviewers", format!("{} threads answered, waiting on reviewer re-review", ds.len()), None);
            }
            if todo > 0 || g(mr, "blocking_discussions_resolved") == &Value::Bool(false) {
                // GitLab's flag catches what the first-note test misses
                let n = if todo > 0 { todo.to_string() } else { "GitLab says some".into() };
                return hit("agent", format!("Resolve MR Threads ({n} unresolved)"), None);
            }
            return Ok(None);
        }
        "approved" => {
            // GitLab's status names only its first blocker (a dependency MR hides not_approved): check the count too
            let st = g(mr, "detailed_merge_status");
            if st == "not_approved" || st == "requested_changes" || truthy(g(mr, "_approvals_left")) {
                return hit("reviewers", "waiting on approvals".into(), None);
            }
            return Ok(None);
        }
        "merge" => {
            let st = g(mr, "detailed_merge_status");
            let how = py::str_(cfg("merge_method"));
            let merge = if github {
                sv(&["gh", "pr", "merge", &iid, "-R", &proj, &format!("--{how}")])
            } else {
                let mut m = sv(&["glab", "mr", "merge", &iid, "-R", &proj, "--yes"]);
                if how != "merge" {
                    m.push(format!("--{how}"));
                }
                m
            };
            let mergeable = st == "mergeable";
            if mergeable && fre(&base::HOLD_RE, &said(ws, mr)) {
                return hit("me", "mergeable, but the goal says hold: merge now?".into(), Some(merge)); // yes runs it (the card's Merge button too)
            }
            let opts = lifecycle_opts(&proj)?;
            let (base_, default) = (g(mr, "target_branch"), g(mr, "_default_branch"));
            if mergeable && truthy(default) && base_ != default && opts.get("merge_any_base").is_none() {
                // a stacked PR: it lands with (or after) its base, never auto-merged into a side branch
                if truthy(g(mr, "_base_mr")) {
                    let r = ref_(py::gs(ws, "forge"), &mr["_base_mr"]);
                    return hit("dependency", format!("targets {}: waiting on {r} to merge first", py::str_(base_)), None);
                }
                return hit("me", format!("mergeable into {}, not {}: merge it by hand?", py::str_(base_), py::str_(default)), None);
            }
            let approved = it(&gd(mr, "_approved_by", json!([])))?;
            if mergeable && truthy(cfg("require_human_approval")) && approved.iter().all(|u| *u == me_ || *u == bot) {
                if opts.get("solo").is_some() {
                    // nobody else reviews this repo: I do, then greenlight runs the cmd
                    return hit("me", "mergeable, and nobody else reviews this repo: review it, then merge?".into(), Some(merge));
                }
                return hit("reviewers", "mergeable but no human approval yet (repo doesn't require one)".into(), None);
            }
            if mergeable {
                return hit("greenlight", "merge".into(), Some(merge));
            }
            if st == "need_rebase" {
                // user rule: rebase only on a real conflict, so ask rather than dispatch
                return hit("me", "GitLab needs a rebase to merge (no conflict): rebase it?".into(), None);
            }
            if ["ci_must_pass", "ci_still_running", "checking", "unchecked", "preparing"].iter().any(|x| st == *x) {
                return hit("ci", format!("merge status {}", py::str_(st)), None);
            }
            if st == "merge_request_blocked" && truthy(g(mr, "_blocked_by")) {
                let b: Vec<String> = it(&mr["_blocked_by"])?.iter().map(|i| format!("!{}", py::str_(i))).collect();
                return hit("dependency", format!("waiting on {} to merge first", b.join(", ")), None);
            }
            if st == "blocked_status" {
                return hit("me", "GitHub blocks the merge (branch protection): check the PR".into(), None);
            }
            return hit("me", format!("not mergeable: {}", py::str_(st)), None);
        }
        _ => {}
    }
    let doc = lifecycle_doc(&proj)?;
    let stage = doc["stages"].as_array().into_iter().flatten().find(|s| s["key"] == step).cloned().unwrap_or_default();
    let (name, req) = (step.replace('_', " "), py::str_(or(&stage["req"], &json!("done?"))));
    if let Some(label) = py::gs(&stage, "done").strip_prefix("label=") {
        let labels = it(&gd(mr, "labels", json!([])))?;
        return if labels.contains(&json!(label)) {
            Ok(None)
        } else {
            hit("agent", format!("{name}: {req} (add the label `{label}` when it's met)"), None)
        };
    }
    let so = signoffs()?;
    if it(&gd(&so, &format!("{proj}#{iid}"), json!([])))?.contains(&json!(step)) {
        return Ok(None);
    }
    let exe = std::env::current_exe()?.display().to_string();
    hit("me", format!("{name}: {req} Sign it off?"), Some(vec![exe, "signoff".into(), proj, iid, step.into()]))
}

/// -> {step, owner, action, cmd}.
fn evaluate(ws: &Value, mr: &Value, milestone: &Value) -> Res<Value> {
    let goal = py::str_(ix(ws, "goal")?);
    let finished = || -> Res<bool> { Ok(truthy(mr) && [json!("merged"), json!("closed")].contains(ix(mr, "state")?)) };
    if let Some(snooze) = base::REVISIT_RE.captures(&goal) {
        if snooze[1] > *py::strftime_local("%Y-%m-%d", py::time()) && !finished()? {
            return Ok(json!({"step": "parked", "owner": "parked", "action": format!("parked until {}", &snooze[1])}));
        }
    }
    if base::PLAN_RE.is_match(&goal) {
        return Ok(json!({"step": "plan", "owner": "parked", "action": "being planned in the Plan view", "hidden": true}));
    }
    if base::PARKED_RE.is_match(&goal) && !finished()? {
        return Ok(json!({"step": "parked", "owner": "parked", "action": "parked until you unpark it", "hidden": true}));
    }
    if !truthy(mr) {
        if py::str_(ix(ws, "branch")?).starts_with("auto-") && goal.is_empty() {
            return Ok(json!({"step": "automation", "owner": "me", "action": "automation run with no MR: delete?"}));
        }
        if goal.is_empty() {
            return Ok(json!({"step": "goal", "owner": "me", "action": "no MR and no goal: what's this workspace for?"}));
        }
        if base::AUTOMATION_RE.is_match(&goal) {
            return Ok(json!({"step": "freeform", "owner": "working", "action": "run by an Orca automation"}));
        }
        return Ok(json!({"step": "freeform", "owner": "agent", "action": format!("work toward goal: {goal}")}));
    }
    if finished()? {
        return Ok(json!({"step": "done", "owner": "done", "action": format!("MR {}: cleanup.py removes it", py::str_(&mr["state"]))}));
    }
    let me_ = json!(me());
    if *ix(ix(mr, "author")?, "username")? != me_ {
        if it(&gd(mr, "_approved_by", json!([])))?.contains(&me_) {
            return Ok(json!({"step": "done", "owner": "done", "action": "approved by me: remove workspace"}));
        }
        let mut mine = vec![];
        for d in unresolved(mr)? {
            if *ix(ix(at(ix(&d, "notes")?, 0)?, "author")?, "username")? == me_ {
                mine.push(d);
            }
        }
        if mine.is_empty() && !mr.get("_can_approve").is_none_or(truthy) {
            return Ok(json!({"step": "review", "owner": "reviewers", "action": "your commits are in it: you can't approve, waiting on others"}));
        }
        if mine.is_empty() {
            return Ok(json!({"step": "review", "owner": "me", "action": "review it"}));
        }
        for d in &mine {
            if *ix(ix(at(ix(d, "notes")?, -1)?, "author")?, "username")? != me_ {
                return Ok(json!({"step": "review", "owner": "me", "action": "author replied on your threads: re-review"}));
            }
        }
        return Ok(json!({"step": "review", "owner": "author", "action": format!("{} of your threads open, waiting on author", mine.len())}));
    }
    let (name, steps) = lifecycle(&py::str_(ix(ws, "project")?))?;
    for step in &steps {
        if let Some((owner, action, cmd)) = check(step, mr, ws, milestone)? {
            return Ok(json!({"step": step, "owner": owner, "action": action, "cmd": cmd, "lifecycle": name}));
        }
    }
    let forge = if g(ws, "forge") == "github" { "GitHub" } else { "GitLab" };
    Ok(json!({"step": "merge", "owner": "ci", "action": format!("all steps green, waiting on {forge}"), "lifecycle": name}))
}

/// Agent-owned work is only actionable if no agent is already on it, and the agent is alive.
fn agent_overlay(ws: &mut Value, terminals: &HashMap<String, Vec<Value>>) -> Res<()> {
    let state = ix(ix(ws, "agent")?, "state")?.clone();
    if state == "waiting" {
        let last = py::head(&py::str_(ix(&ws["agent"], "last")?), 200).to_string();
        ws["next"]["owner"] = json!("me");
        ws["next"]["action"] = json!(format!("agent is waiting on you: {last}"));
        return Ok(());
    }
    if *ix(ix(ws, "next")?, "owner")? != "agent" {
        return Ok(());
    }
    if state == "working" {
        ws["next"]["owner"] = json!("working");
        return Ok(());
    }
    let mut terms: Vec<Value> =
        terminals.get(&py::str_(ix(ws, "id")?)).into_iter().flatten().filter(|t| truthy(g(t, "agentIdentity"))).cloned().collect();
    terms.sort_by(|a, b| cmp_key(g(b, "lastOutputAt"), g(a, "lastOutputAt")));
    let Some(t0) = terms.first() else { return Ok(()) };
    let handle = ix(t0, "handle")?.clone();
    ws["agent"]["terminal"] = handle.clone();
    let h = py::str_(&handle);
    let read = orca_s(&["terminal", "read", "--terminal", &h, "--limit", "200"])
        .and_then(|v| Ok(ix(&v, "terminal")?.get("tail").cloned().unwrap_or(json!(""))));
    let Ok(tail) = read else { return Ok(()) };
    let tail = match &tail {
        Value::String(s) => s.clone(),
        _ => it(&tail)?.iter().map(py::str_).collect::<Vec<_>>().join("\n"),
    };
    let t = py::tail(&tail, 3000);
    let agents = cfg("agents").as_array().cloned().unwrap_or_default();
    if base::DEAD_RE.is_match(t) {
        ws["next"]["owner"] = json!("me");
        ws["next"]["action"] = json!(format!("agent logged out: /login in {h}, then greenlight re-dispatches"));
    } else if base::LIMIT_RE.is_match(t) && agents.len() > 1 {
        // ponytail: assumes the stuck agent is the first; walk the list if they mix
        let (first, next) = (py::str_(&agents[0]), agents[1].clone());
        let action = format!("{} [{first} out of tokens: dispatch with {}]", py::str_(&ws["next"]["action"]), py::str_(&next));
        ws["next"]["agent"] = next;
        ws["next"]["action"] = json!(action);
    }
    Ok(())
}

/// The hex/card facts of one MR, as of this scan.
fn mr_summary(r: &Value, mr: &Value) -> Res<Value> {
    let face = |u: &Value| -> Res<Value> { Ok(json!({"name": login(u)?, "avatar": g(u, "avatar_url")})) };
    let author = ix(mr, "author")?;
    let (me_, bot) = (json!(me()), json!(base::bot()));
    let un = unresolved(mr)?;
    let mut todo = vec![];
    for d in &un {
        let u = ix(ix(at(ix(d, "notes")?, -1)?, "author")?, "username")?;
        if *u != me_ && *u != bot {
            todo.push(json!(thread_line(d)?));
        }
    }
    todo.truncate(40);
    let mut total = 0;
    for d in it(&gd(mr, "_discussions", json!([])))? {
        let notes = ix(&d, "notes")?;
        if truthy(notes) && truthy(g(at(notes, 0)?, "resolvable")) {
            total += 1;
        }
    }
    let assignees = it(or(g(mr, "assignees"), &json!([])))?.iter().map(face).collect::<Res<Vec<_>>>()?;
    let approved = gd(mr, "_approved_by", json!([]));
    let failed: Vec<Value> = gd(mr, "_failed_jobs", json!([])).as_array().into_iter().flatten().take(40).cloned().collect();
    let dms = g(mr, "detailed_merge_status");
    Ok(json!({
        "iid": g(mr, "iid"), "web_url": g(mr, "web_url"), "title": g(mr, "title"), "state": g(mr, "state"), "draft": g(mr, "draft"),
        "ref": ref_(py::gs(r, "forge"), ix(mr, "iid")?), "forge": r["forge"],
        "author": login(author)?, "role": if *ix(author, "username")? == me_ { "author" } else { "review" },
        // Basement card byline: forge avatars, username on hover
        "by": face(author)?, "assignees": assignees,
        // hex signals (dashboard): as of this scan
        "pipeline": g(g(mr, "head_pipeline"), "status"), "conflicts": truthy(g(mr, "has_conflicts")),
        "threads": un.len(), "approvals": approved.as_array().map_or(0, Vec::len),
        "approvals_required": gd(mr, "_approvals_required", json!(0)), "mergeable": dms == "mergeable", "rebase": dms == "need_rebase",
        // hover card (dashboard)
        "source_branch": g(mr, "source_branch"), "target_branch": g(mr, "target_branch"), "labels": gd(mr, "labels", json!([])),
        "description": py::head(&py::str_(or(g(mr, "description"), &json!(""))), 2000), // Basement ⌘J matches on it; ponytail: first 2000 chars
        "milestone": g(g(mr, "milestone"), "title"), "approved_by": approved,
        "pipeline_url": g(g(mr, "head_pipeline"), "web_url"),
        "sha": g(mr, "sha"), "reviewed_sha": g(mr, "_reviewed_sha"), // the review prompt's {sha} and {last_review_sha}
        "todo_threads": todo,
        "failed_jobs": failed, // the Resolve threads / Fix CI prompts' {threads} / {failed_jobs}
        "threads_total": total,
    }))
}

/// An mkdir that fails like Python's (`[Errno 2] No such file or directory: '<path>'`).
fn mkdir(p: &Path) -> Res<()> {
    match std::fs::create_dir(p) {
        Err(e) if !(e.kind() == std::io::ErrorKind::AlreadyExists && p.is_dir()) => oserr(e, p),
        _ => Ok(()),
    }
}

static MR_NAME_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^mr-(\d+)(?:-.*)?$").unwrap());
static BANG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"!(\d+)").unwrap());
static GH_STATUS_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:Review|Merge) #(\d+)").unwrap());

pub fn scan(save: bool) -> Res<Value> {
    let wts = it(ix(&orca_s(&["worktree", "ps", "--limit", "10000"])?, "worktrees")?)?;
    let mut full = Map::new();
    for w in it(ix(&orca_s(&["worktree", "list", "--limit", "10000"])?, "worktrees")?)? {
        full.insert(py::str_(ix(&w, "id")?), w);
    }
    let mut terminals: HashMap<String, Vec<Value>> = HashMap::new();
    for t in it(ix(&orca_s(&["terminal", "list", "--limit", "10000"])?, "terminals")?)? {
        terminals.entry(py::str_(ix(&t, "worktreeId")?)).or_default().push(t);
    }

    let mut projects = HashMap::new();
    for w in full.values() {
        if let Some(fp) = base::forge_project(py::gs(w, "projectId")) {
            projects.insert(py::str_(ix(w, "repoId")?), fp);
        }
    }
    let (mut rows, mut skipped) = (vec![], 0);
    for w in &wts {
        let b = py::gs(w, "branch");
        let branch = b.strip_prefix("refs/heads/").unwrap_or(b);
        if truthy(g(w, "isMainWorktree")) || branch.is_empty() || !projects.contains_key(&py::str_(ix(w, "repoId")?)) {
            skipped += 1; // main checkouts, detached HEADs (cache trees), repos on neither GitLab nor GitHub
            continue;
        }
        let (forge, project) = projects[&py::str_(&w["repoId"])].clone();
        let mut agents = it(&gd(w, "agents", json!([])))?;
        agents.sort_by(|a, b| cmp_key(g(b, "updatedAt"), g(a, "updatedAt")));
        let ag = agents.first().cloned().unwrap_or(json!({}));
        let comment = py::gs(w, "comment");
        let lo = g(w, "lastOutputAt");
        let idle = if truthy(lo) { json!(py::round((base::now_ms() - lo.as_f64().unwrap_or(0.0)) / 3.6e6, 1)) } else { lo.clone() };
        let linked = g(w, if forge == "github" { "linkedPR" } else { "linkedGitLabMR" });
        rows.push(json!({
            "id": ix(w, "worktreeId")?, "name": ix(w, "displayName")?, "path": ix(w, "path")?, "branch": branch,
            "project": project, "forge": forge, "goal": if base::STATUS_LINE_RE.is_match(comment) { "" } else { py::strip(comment) },
            "comment": py::strip(comment),
            "pinned": g(w, "isPinned"), "linked_mr": linked_number(linked),
            "agent": {"state": gd(&ag, "state", json!("none")), "type": g(&ag, "agentType"),
                      "last": py::tail(py::gs(&ag, "lastAssistantMessage"), 400), "idle_h": idle},
        }));
    }

    // One open-MR/PR listing per project resolves most branches without per-branch lookups.
    let mut open_by_branch: HashMap<(String, String), Value> = HashMap::new();
    let mut closes: HashMap<(String, String), Value> = HashMap::new();
    let mut milestones: HashMap<String, Value> = HashMap::new();
    let mut pairs: Vec<(String, String)> = vec![];
    for r in &rows {
        let k = (py::str_(&r["forge"]), py::str_(&r["project"]));
        if !pairs.contains(&k) {
            pairs.push(k);
        }
    }
    for (forge, proj) in &pairs {
        if forge == "github" {
            for p in it(&gh_open_prs(proj)?)? {
                open_by_branch.insert((proj.clone(), py::str_(ix(&p, "headRefName")?)), ix(&p, "number")?.clone());
                for i in it(or(g(&p, "closingIssuesReferences"), &json!([])))? {
                    let n = ix(&p, "number")?.clone();
                    closes.entry((proj.clone(), py::str_(ix(&i, "number")?))).or_insert(n);
                }
            }
        } else {
            for m in base::gl_all(&format!("projects/{}/merge_requests?state=opened", pid(proj)))? {
                open_by_branch.insert((proj.clone(), py::str_(ix(&m, "source_branch")?)), ix(&m, "iid")?.clone());
            }
        }
        let ms = lifecycle(proj).and_then(|(_, st)| if st.iter().any(|s| s == "milestone") { release_milestone(proj, forge) } else { Ok(Value::Null) });
        milestones.insert(proj.clone(), ms.unwrap_or(Value::Null));
    }

    // The open MR/PR that closes issue N ("Merge MR for #N").
    let closing = |r: &Value, issue: &str| -> Res<Value> {
        if r["forge"] == "github" {
            return Ok(closes.get(&(py::str_(&r["project"]), issue.into())).cloned().unwrap_or_default());
        }
        let hits = match gl(&format!("projects/{}/issues/{issue}/closed_by", pid(py::gs(r, "project")))) {
            Err(e) if py::kind(&*e) == "RuntimeError" => return Ok(Value::Null), // no such issue (or not visible): fall back to the branch
            x => x?,
        };
        for m in it(&hits)? {
            if ix(&m, "state")? == "opened" {
                return Ok(ix(&m, "iid")?.clone());
            }
        }
        Ok(Value::Null)
    };
    // Newest MR/PR of any state whose source branch is this one.
    let by_branch = |r: &Value, branch: &str| -> Res<Value> {
        let project = py::gs(r, "project");
        if r["forge"] == "github" {
            let hits = base::gh(&["pr", "list", "-R", project, "--head", branch, "--state", "all", "--limit", "1", "--json", "number"])?;
            return Ok(if truthy(&hits) { ix(at(&hits, 0)?, "number")?.clone() } else { Value::Null });
        }
        let q = py::urlencode(&[("source_branch", branch), ("state", "all"), ("order_by", "created_at")]);
        let hits = gl(&format!("projects/{}/merge_requests?{q}", pid(project)))?;
        Ok(if truthy(&hits) { ix(at(&hits, 0)?, "iid")?.clone() } else { Value::Null })
    };
    let find_mr = |r: &Value| -> Res<Value> {
        let (branch, path, project) = (py::gs(r, "branch"), py::gs(r, "path"), py::gs(r, "project"));
        let mut upstream = branch.to_string();
        if Path::new(path).is_dir() {
            let cmd: Vec<String> = ["git", "-C", path, "config", &format!("branch.{branch}.merge")].iter().map(|s| s.to_string()).collect();
            let out = py::exec(&cmd, None, None, None, &[])?.out;
            let u = py::strip(&out);
            let u = u.strip_prefix("refs/heads/").unwrap_or(u);
            if !u.is_empty() {
                upstream = u.into();
            }
        }
        let gitlab = r["forge"] == "gitlab";
        let key = |b: &str| (project.to_string(), b.to_string());
        // review checkouts: mr-5191 (adopt names PR ones mr-N too)
        let named = MR_NAME_RE.captures(branch).and_then(|c| c[1].parse::<i64>().ok()).map_or(Value::Null, |n| json!(n));
        let mut iid = [
            r["linked_mr"].clone(),
            open_by_branch.get(&key(&upstream)).cloned().unwrap_or_default(),
            open_by_branch.get(&key(branch)).cloned().unwrap_or_default(),
            named,
        ]
        .into_iter()
        .find(truthy)
        .unwrap_or_default();
        if !truthy(&iid) {
            // old status line: "Thread !129 — dispatched …"
            let re: &Regex = if gitlab { &BANG_RE } else { &GH_STATUS_RE };
            if let Some(n) = re.captures(py::gs(r, "comment")).and_then(|m| m[1].parse::<i64>().ok()) {
                iid = json!(n);
            }
        }
        if !truthy(&iid) {
            // a sent plan: the MR/PR that closes its issue
            if let Some(n) = base::ISSUE_GOAL_RE.captures(py::gs(r, "goal")).and_then(|m| m[1].parse::<i64>().ok()) {
                iid = closing(r, &n.to_string())?;
            }
        }
        if !truthy(&iid) {
            iid = by_branch(r, &upstream)?;
        }
        if !truthy(&iid) {
            return Ok(Value::Null);
        }
        let mut mr = mr_details(project, &iid, py::gs(r, "forge"))?;
        if truthy(g(&mr, "_default_branch")) && g(&mr, "target_branch") != &mr["_default_branch"] {
            // stacked: the PR its base comes from
            let base_mr = open_by_branch.get(&key(&py::str_(ix(&mr, "target_branch")?))).cloned().unwrap_or_default();
            mr["_base_mr"] = base_mr;
        }
        Ok(mr)
    };

    let one = |r0: &Value| -> Value {
        let mut r = r0.clone();
        let res = (|| -> Res<()> {
            let mr = find_mr(&r)?;
            r["mr"] = if truthy(&mr) { mr_summary(&r, &mr)? } else { mr.clone() };
            r["next"] = evaluate(&r, &mr, milestones.get(py::gs(&r, "project")).unwrap_or(&NULL))?;
            agent_overlay(&mut r, &terminals)
        })();
        if let Err(e) = res {
            r["mr"] = Value::Null;
            r["next"] = json!({"step": "error", "owner": "me", "action": format!("greenlight couldn't evaluate: {e}")});
        }
        r
    };
    let mut rows = py::pmap(&rows, 8, one);
    let owner_ix = |r: &Value| OWNERS.iter().position(|o| r["next"]["owner"] == *o).unwrap_or(usize::MAX);
    rows.sort_by(|a, b| {
        (owner_ix(a), py::str_(&a["project"]), py::str_(&a["name"])).cmp(&(owner_ix(b), py::str_(&b["project"]), py::str_(&b["name"])))
    });

    mkdir(&STATE)?;
    let prev_file = STATE.join("last.json");
    let prev = if prev_file.exists() {
        py::loads(&read(&prev_file)?).map_err(|e| PyErr { kind: "JSONDecodeError", msg: format!("{}: not JSON ({e})", prev_file.display()) })?
    } else {
        json!({})
    };
    let mut prev_rows = Map::new();
    for p in it(&gd(&prev, "rows", json!([])))? {
        prev_rows.insert(py::str_(ix(&p, "id")?), p);
    }
    for r in &mut rows {
        if let Some(p) = prev_rows.get(&py::str_(&r["id"])).filter(|p| truthy(p)) {
            let (pn, n) = (ix(p, "next")?, &r["next"]);
            let (ps, po) = (ix(pn, "step")?, ix(pn, "owner")?);
            if (ps, po) != (&n["step"], &n["owner"]) {
                r["changed"] = json!(format!("{}/{} → {}/{}", py::str_(ps), py::str_(po), py::str_(&n["step"]), py::str_(&n["owner"])));
            }
        }
    }
    let ids: Vec<String> = rows.iter().map(|r| py::str_(&r["id"])).collect();
    let mut gone = vec![];
    for (i, p) in &prev_rows {
        if !ids.contains(i) {
            gone.push(ix(p, "name")?.clone());
        }
    }
    let result = json!({"ts": py::time() as i64, "prev_ts": g(&prev, "ts"), "skipped": skipped, "gone": gone, "rows": rows});
    if save {
        // an actionable precheck mustn't overwrite the snapshot the run's report diffs against
        keep(&result)?;
    }
    Ok(result)
}

pub fn keep(result: &Value) -> Res<()> {
    std::fs::write(STATE.join("last.json"), py::dumps(result))?;
    close_stale(result["rows"].as_array().map_or(&[][..], Vec::as_slice))
}


fn label(o: &str) -> &'static str {
    match o {
        "me" => "Needs you",
        "greenlight" => "Greenlight will do",
        "agent" => "Idle — needs an agent",
        "working" => "Agent working",
        "ci" => "Waiting on CI",
        "reviewers" => "Waiting on reviewers",
        "author" => "Waiting on author",
        "dependency" => "Waiting on another MR/PR",
        "parked" => "Parked (revisit later)",
        _ => "Done — cleanup",
    }
}

pub fn report(res: &Value) -> Res<String> {
    let e = |v: &Value| py::html_escape(&py::str_(v));
    let rows: Vec<&Value> = res["rows"].as_array().into_iter().flatten().filter(|r| !truthy(g(&r["next"], "hidden"))).collect();
    let counts: Vec<(&str, usize)> = OWNERS.iter().map(|o| (*o, rows.iter().filter(|r| r["next"]["owner"] == *o).count())).collect();
    let bar: String = counts
        .iter()
        .filter(|(_, n)| *n > 0)
        .map(|(o, n)| format!(r#"<span class="seg o-{o}" style="flex:{n}" title="{}: {n}"></span>"#, label(o)))
        .collect();
    let legend: String = counts.iter().filter(|(_, n)| *n > 0).map(|(o, n)| format!(r#"<li><i class="o-{o}"></i>{} <b>{n}</b></li>"#, label(o))).collect();
    let changed: Vec<&&Value> = rows.iter().filter(|r| truthy(g(r, "changed"))).collect();
    let gone = it(ix(res, "gone")?)?;
    let mut sections = vec![];
    if !changed.is_empty() || !gone.is_empty() {
        let mut items: String = changed.iter().map(|r| format!("<li><b>{}</b> {}</li>", e(&r["name"]), e(&r["changed"]))).collect();
        items += &gone.iter().map(|n| format!("<li><b>{}</b> removed</li>", e(n))).collect::<String>();
        sections.push(format!("<section><h2>Since last run</h2><ul class=changes>{items}</ul></section>"));
    }
    for o in OWNERS {
        let group: Vec<&&Value> = rows.iter().filter(|r| r["next"]["owner"] == o).collect();
        if group.is_empty() {
            continue;
        }
        let mut trs = String::new();
        for r in &group {
            let proj = py::str_(&r["project"]);
            let p = format!("{} · {}", proj.rsplit('/').next().unwrap_or(""), py::str_(&r["branch"]));
            let mr = if truthy(g(r, "mr")) {
                let m = &r["mr"];
                let rf = if truthy(g(m, "ref")) { py::str_(&m["ref"]) } else { format!("!{}", py::str_(ix(m, "iid")?)) };
                format!(r#"<a href="{}">{}</a><div class=sub>{}</div>"#, e(ix(m, "web_url")?), py::html_escape(&rf), e(ix(m, "role")?))
            } else {
                "—".into()
            };
            let goal = py::str_(&r["goal"]);
            let gl_ = if goal.is_empty() {
                r#"<div class="sub warn">no goal</div>"#.to_string()
            } else {
                format!("<div class=sub>goal: {}</div>", py::html_escape(py::head(&goal, 140)))
            };
            let idle = &r["agent"]["idle_h"];
            let ag = format!("{}<div class=sub>{}</div>", e(&r["agent"]["state"]), if idle.is_null() { "—".into() } else { format!("{}h idle", py::str_(idle)) });
            trs += &format!(
                "<tr><td>{}<div class=sub>{}</div></td><td>{mr}</td><td><code>{}</code></td><td>{}{gl_}</td><td>{ag}</td></tr>",
                e(&r["name"]),
                py::html_escape(&p),
                e(&r["next"]["step"]),
                e(&r["next"]["action"])
            );
        }
        sections.push(format!(
            r#"<section><h2><i class="o-{o}"></i>{} <span class=n>{}</span></h2><table><tr><th>Workspace</th><th>MR</th><th>Step</th><th>Next</th><th>Agent</th></tr>{trs}</table></section>"#,
            label(o),
            group.len()
        ));
    }
    let when = py::strftime_local("%Y-%m-%d %H:%M", ix(res, "ts")?.as_f64().unwrap_or(0.0));
    let css: String = OWNERS.iter().map(|o| format!(".o-{o}{{background:var(--{o})}}")).collect();
    Ok(format!(
        r#"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Greenlight Report</title><style>
:root{{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--card:#fff;
--me:#dc2626;--greenlight:#7c3aed;--agent:#ea580c;--working:#16a34a;--ci:#0891b2;--reviewers:#2563eb;--author:#64748b;--dependency:#94a3b8;--parked:#d6d3d1;--done:#a8a29e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c0a09;--fg:#f5f5f4;--mut:#a8a29e;--line:#292524;--card:#1c1917}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1100px;margin:auto}}h1{{margin:0 0 4px}}.meta{{color:var(--mut);margin-bottom:16px}}
.bar{{display:flex;height:18px;border-radius:6px;overflow:hidden;gap:2px}}.seg{{min-width:6px}}
ul.legend{{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:6px 16px;margin:10px 0 24px}}
i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}}
{css}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:16px;overflow-x:auto}}
h2{{font-size:16px;margin:4px 0 10px}}.n{{color:var(--mut);font-weight:400}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:500;border-top:0}}.sub{{color:var(--mut);font-size:12px}}.warn{{color:var(--me)}}
a{{color:var(--reviewers)}}code{{font-size:12px}}ul.changes{{margin:0;padding-left:18px}}
</style></head><body><main><h1>Greenlight</h1>
<div class=meta>{when} · {} workspaces tracked · {} skipped (main checkouts / detached / not on GitLab or GitHub)</div>
<div class=bar>{bar}</div><ul class=legend>{legend}</ul>{}</main></body></html>"#,
        rows.len(),
        py::str_(ix(res, "skipped")?),
        sections.concat()
    ))
}

pub fn history_file(name: &str) -> PathBuf {
    // one markdown file per workspace, named after the workspace (me/x -> me__x)
    STATE.join("history").join(name.replace('/', "__") + ".md")
}

pub fn note(name: &str, text: &str) -> Res<()> {
    let f = history_file(name);
    std::fs::create_dir_all(f.parent().unwrap())?;
    let mut fh = std::fs::OpenOptions::new().create(true).append(true).open(f)?;
    writeln!(fh, "- {} {text}", py::strftime_local("%Y-%m-%d %H:%M", py::time()))?;
    Ok(())
}


pub fn outbox(ev: Value) -> Res<()> {
    std::fs::create_dir_all(&*STATE)?;
    let mut o = json!({"ts": py::time() as i64});
    o.as_object_mut().unwrap().extend(ev.as_object().cloned().unwrap_or_default());
    let mut fh = std::fs::OpenOptions::new().create(true).append(true).open(STATE.join("outbox.jsonl"))?;
    writeln!(fh, "{}", py::dumps(&o))?;
    Ok(())
}

/// Open questions keyed by workspace name or id: {key: ts}. ~/.greenlight/pinged.json
pub fn pinged() -> Res<Map<String, Value>> {
    let f = STATE.join("pinged.json");
    if !f.exists() {
        return Ok(Map::new());
    }
    match py::loads(&std::fs::read_to_string(f)?)? {
        Value::Object(m) => Ok(m),
        _ => py::err("pinged.json is not an object"),
    }
}

pub fn pinged_set(m: Map<String, Value>) -> Res<()> {
    Ok(std::fs::write(STATE.join("pinged.json"), py::dumps(&Value::Object(m)))?)
}

pub const FLAP: f64 = 24.0 * 3600.0;

/// ws: the workspaces (names) it's about; a workspace key counts as one. The app lifts their hexes until it's answered.
pub fn ping(text: &str, key: Option<&str>, ws: Vec<Value>) -> Res<()> {
    let key = key.filter(|k| !k.is_empty());
    if let Some(k) = key.filter(|k| k.starts_with('@')) {
        if recent_keys()?.contains(k) {
            // puck blocks flap in and out of its list
            println!("skipped: {k} was pinged in the last {}h", FLAP as i64 / 3600);
            return Ok(());
        }
    }
    let mut all: Vec<Value> = match key.filter(|k| !k.starts_with('@')) {
        // an id key lifts that workspace by name
        Some(k) => vec![scanned_names()?.get(k).cloned().unwrap_or_else(|| json!(k))],
        None => vec![],
    };
    for w in ws {
        if !all.contains(&w) {
            all.push(w);
        }
    }
    let mut ev = json!({"text": text});
    if let Some(k) = key {
        ev["key"] = json!(k);
    }
    if !all.is_empty() {
        ev["ws"] = json!(all);
    }
    outbox(ev)?;
    if let Some(k) = key {
        let mut p = pinged()?;
        p.insert(k.into(), json!(py::time() as i64));
        pinged_set(p)?;
    }
    Ok(())
}

/// Keys pinged within FLAP, read from the outbox itself.
pub fn recent_keys() -> Res<std::collections::HashSet<String>> {
    let (f, cut) = (STATE.join("outbox.jsonl"), py::time() - FLAP);
    let mut out = std::collections::HashSet::new();
    if f.exists() {
        for l in py::splitlines(&std::fs::read_to_string(f)?) {
            let e = py::loads(l)?;
            if e.get("text").is_some() && e.get("key").is_some_and(py::truthy) {
                let ts = e.get("ts").ok_or_else(|| py::key_err::<()>("ts").unwrap_err())?;
                if ts.as_f64().unwrap_or(f64::NEG_INFINITY) > cut {
                    out.insert(py::str_(&e["key"]));
                }
            }
        }
    }
    Ok(out)
}

/// {name or id: name} for every workspace in the last scan (~/.greenlight/last.json).
pub fn scanned_names() -> Res<Map<String, Value>> {
    let f = STATE.join("last.json");
    let last = if f.exists() { py::loads(&std::fs::read_to_string(f)?)? } else { json!({}) };
    let rows = last.get("rows").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut m = Map::new();
    for r in &rows {
        m.insert(py::str_(&r["id"]), r["name"].clone());
    }
    for r in &rows {
        m.insert(py::str_(&r["name"]), r["name"].clone());
    }
    Ok(m)
}

/// greenlight.py ping [--key K] [--ws W]... <text>: --ws takes a workspace name or id; an unknown one sends nothing.
fn ping_cli(mut args: &[String]) -> Res<()> {
    let (mut key, mut ws) = (None, vec![]);
    while args.len() > 1 && (args[0] == "--key" || args[0] == "--ws") {
        if args[0] == "--key" {
            key = Some(args[1].as_str());
        } else {
            ws.push(args[1].as_str());
        }
        args = &args[2..];
    }
    let text = args.join(" ");
    if !regex::Regex::new(r"\nOptions: *\S.*\|").unwrap().is_match(&text) {
        // no buttons, only "Reply", in the app
        py::sys_exit("greenlight.py ping: end the text with a line `Options: first | second`; nothing sent");
    }
    let names = scanned_names()?;
    let unknown: Vec<&str> = ws.iter().copied().filter(|w| !names.contains_key(*w)).collect();
    if !unknown.is_empty() {
        py::sys_exit(&format!("greenlight.py ping: unknown workspace {} (not in the last scan); nothing sent", unknown.join(", ")));
    }
    ping(&text, key, ws.iter().map(|w| names[*w].clone()).collect())
}

pub fn resolve(key: &str, why: &str) -> Res<()> {
    outbox(json!({"resolve": key, "why": why}))?;
    let mut p = pinged()?;
    p.shift_remove(key);
    pinged_set(p)
}

/// Questions about a workspace close once it's gone or no longer waiting on me.
pub fn close_stale(rows: &[Value]) -> Res<()> {
    let mut by_key: Map<String, Value> = rows.iter().map(|r| (py::str_(&r["name"]), r.clone())).collect();
    by_key.extend(rows.iter().map(|r| (py::str_(&r["id"]), r.clone())));
    for key in pinged()?.keys() {
        if key.starts_with('@') {
            continue; // not a workspace: closed by hand with `greenlight.py resolve`
        }
        match by_key.get(key) {
            None => resolve(key, "workspace removed")?,
            Some(r) if r["next"]["owner"] != "me" => {
                let n = &r["next"];
                resolve(key, &format!("now {}: {}", py::str_(&n["owner"]), py::head(&py::str_(&n["action"]), 120)))?
            }
            _ => {}
        }
    }
    Ok(())
}

/// sys.argv[i] (argv[0] is the script), IndexError when it's missing like Python.
fn arg(args: &[String], i: usize) -> Res<&str> {
    match args.get(i - 1) {
        Some(a) => Ok(a),
        None => py::raise("IndexError", "list index out of range"),
    }
}

/// MRs with no workspace yet: adopt creates them, so a run should follow.
fn adopt_plan_creates(_rows: &[Value], _ts: &Value) -> Res<usize> {
    // TODO(wave2): crate::adopt::plan(rows, ts)["create"].len()
    py::err("adopt not ported yet")
}

pub fn main(args: Vec<String>) -> Res<i32> {
    let cmd = args.first().map(String::as_str).unwrap_or("scan");
    match cmd {
        "note" => note(arg(&args, 2)?, &args[2..].join(" "))?,
        "history" => {
            let f = history_file(arg(&args, 2)?);
            println!("{}", if f.exists() { std::fs::read_to_string(f)? } else { "(no history)".into() });
        }
        "signoff" => {
            let (mut so, k) = (signoffs()?, format!("{}#{}", arg(&args, 2)?, arg(&args, 3)?));
            let mut v: Vec<String> = so.get(&k).and_then(Value::as_array).into_iter().flatten().map(py::str_).collect();
            v.push(arg(&args, 4)?.into());
            v.sort();
            v.dedup();
            so.as_object_mut().ok_or("signoffs.json is not an object")?.insert(k, json!(v));
            std::fs::create_dir_all(&*STATE)?;
            std::fs::write(STATE.join("signoffs.json"), py::dumps_indent(&so))?;
        }
        "ping" => ping_cli(&args[1..])?, // notification + bell-inbox question on the Mac menubar app (it tails outbox.jsonl)
        "resolve" => {
            let (key, why) = (arg(&args, 2)?, args[2..].join(" "));
            resolve(key, if why.is_empty() { "no longer needed" } else { &why })?
        }
        "ade" => {
            let a: Vec<String> = args[1..].iter().filter(|a| *a != "--json").cloned().collect();
            println!("{}", py::dumps_indent(&orca(&a)?));
        }
        _ => {
            let res = scan(cmd != "precheck")?;
            let rows = res["rows"].as_array().map_or(&[][..], Vec::as_slice);
            if cmd == "precheck" {
                let mut n = rows.iter().filter(|r| r["next"]["owner"] == "greenlight" || r["next"]["owner"] == "agent").count();
                match adopt_plan_creates(rows, g(&res, "ts")) {
                    Ok(c) => n += c,
                    Err(e) => eprintln!("adopt plan failed: {e}"),
                }
                if n == 0 {
                    // no run follows, so this is the freshest snapshot the dashboard will get
                    keep(&res)?;
                }
                println!("{n} actionable workspace(s) or adoption(s)");
                return Ok(if n > 0 { 0 } else { 1 });
            }
            if cmd == "report" {
                let out = STATE.join("report.html");
                std::fs::write(&out, report(&res)?).or_else(|e| oserr(e, &out))?;
                println!("{}", out.display());
                return Ok(0);
            }
            if args.iter().any(|a| a == "--json") {
                println!("{}", py::dumps_indent(&res));
                return Ok(0);
            }
            for r in rows {
                let mr = if truthy(g(r, "mr")) {
                    let m = &r["mr"];
                    if truthy(g(m, "ref")) { py::str_(&m["ref"]) } else { format!("!{}", py::str_(ix(m, "iid")?)) }
                } else {
                    "-".into()
                };
                let n = &r["next"];
                println!(
                    "{} {} {} {} {}",
                    py::ljust(&py::str_(&n["owner"]), 10),
                    py::ljust(&py::str_(&n["step"]), 12),
                    py::ljust(&mr, 6),
                    py::ljust(py::head(&py::str_(&r["name"]), 40), 40),
                    py::head(&py::str_(&n["action"]), 90)
                );
            }
        }
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lifecycle_md_round_trips_the_bundled_files() {
        for name in ["default", "greenlight"] {
            let doc = lifecycle_doc(&format!("x/{name}")).unwrap();
            let md = lifecycle_md(name, &doc).unwrap();
            let keys: Vec<String> = STAGE_RE.captures_iter(&md).map(|c| c[1].to_string()).collect();
            let want: Vec<String> = doc["stages"].as_array().unwrap().iter().map(|s| py::str_(&s["key"])).collect();
            assert!(!want.is_empty() && keys == want, "{name}");
        }
        assert!(lifecycle_md("x", &json!({"stages": [{"key": "a"}, {"key": "a"}]})).is_err());
    }
}
