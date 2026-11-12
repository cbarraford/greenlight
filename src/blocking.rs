//! Port of scripts/blocking.py. What I'm blocking on GitLab, by a team's review rules. Off unless config.json
//! has a `blocking` roster:
//!
//!     "blocking": {"repos": {"acme/widgets": "Release-", "acme/tools": null},   # null: every open MR; a prefix: only MRs in that milestone
//!                  "people": ["me", "bob"], "teams": {"dev-team": ["me", "bob"]},  # teams: approval-rule name -> members
//!                  "lead": "me"}                                                   # who gets orphans (default: you)
//!
//!     blocking [--json] [--fresh]   # the open MRs I'm blocking now and why
//!
//! It walks every open, non-draft MR in REPOS (a repo with a prefix: only MRs in an active <prefix> milestone) and pings
//! whoever owns the next action. The checks run in order and the first that blocks ends the walk:
//!   conflict    the owner (first known assignee, else the author) must rebase
//!   pipeline    the newest pipeline isn't green: a failed job -> the owner; still running -> nobody yet
//!   discussion  each unresolved thread -> everyone in it plus the owner, minus whoever spoke last
//!   approval    approvals left -> rule-team members who haven't approved, reviewed, or commented since the
//!               last push; if that's nobody, the owner ("needs N more approvals")
//!   orphan      a milestone-repo MR that pinged nobody and isn't waiting on CI -> the lead dev (me)
//! Orphans on MRs I've already approved are dropped, and so is an
//! approval ask on an MR GitLab won't let me approve (my commits are in it).
//!
//! Cached in ~/.greenlight/blocks.json for CACHE seconds: adopt scans, dash and cleanup only read the file.
use crate::base::{self, gl, gl_all, pid, STATE};
use crate::py::{self, Res};
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::LazyLock;

struct Roster {
    lead: String,
    repos: Vec<(String, Value)>,
    people: BTreeSet<String>,
    teams: Map<String, Value>,
}

static R: LazyLock<Roster> = LazyLock::new(|| {
    let ro = base::cfg("blocking").as_object().cloned().unwrap_or_default();
    let get = |k: &str| ro.get(k).filter(|v| py::truthy(v));
    Roster {
        lead: get("lead").map(py::str_).unwrap_or_else(|| base::me().into()),
        repos: get("repos").and_then(Value::as_object).map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect()).unwrap_or_default(),
        people: get("people").map(strs).unwrap_or_default(),
        teams: get("teams").and_then(Value::as_object).cloned().unwrap_or_default(),
    }
});
fn file() -> PathBuf {
    STATE.join("blocks.json")
}
pub const CACHE: f64 = 300.0; // precheck and apply run back to back each automation tick: one scan serves both
const PENDING: [&str; 6] = ["created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"];

type Hit = (String, &'static str, String);

fn strs(v: &Value) -> BTreeSet<String> {
    v.as_array().into_iter().flatten().map(py::str_).collect()
}
/// v[k], KeyError when missing (check() errors are swallowed per MR, as Python's are).
pub fn k<'a>(v: &'a Value, key: &str) -> Res<&'a Value> {
    v.get(key).ok_or_else(|| py::key_err::<()>(key).unwrap_err())
}
fn user(v: &Value, key: &str) -> Res<String> {
    Ok(py::str_(k(k(v, key)?, "username")?))
}
/// v.get(key, []) iterated: missing is empty, anything but a list is an error.
pub fn list<'a>(v: &'a Value, key: &str) -> Res<&'a [Value]> {
    match v.get(key) {
        None => Ok(&[]),
        Some(Value::Array(a)) => Ok(a),
        Some(_) => py::raise("TypeError", format!("{key} is not a list")),
    }
}
fn ts(s: &Value) -> Res<chrono::DateTime<chrono::FixedOffset>> {
    let s = py::str_(s);
    Ok(chrono::DateTime::parse_from_rfc3339(&s.replace('Z', "+00:00"))?)
}

fn owner(mr: &Value) -> Res<String> {
    let author = user(mr, "author")?;
    for a in mr.get("assignees").filter(|v| py::truthy(v)).and_then(Value::as_array).into_iter().flatten() {
        let u = py::str_(k(a, "username")?);
        if R.people.contains(&u) {
            return Ok(u);
        }
    }
    Ok(author)
}

/// -> [(user, reason, why)] the rules would ping about this MR, orphan included.
fn check(proj: &str, mr: &Value) -> Res<Vec<Hit>> {
    let base = format!("projects/{}/merge_requests/{}", pid(proj), py::str_(k(mr, "iid")?));
    let own = owner(mr)?;
    let milestone = R.repos.iter().any(|(p, pre)| p == proj && py::truthy(pre));
    let (mut hits, mut mentioned, mut pending, mut note): (Vec<Hit>, bool, bool, Option<String>) = (vec![], false, false, None);

    let orphan = |mentioned: bool, pending: bool, note: &Option<String>| -> Res<Vec<Hit>> {
        if !milestone || mentioned || pending {
            return Ok(vec![]);
        }
        let ap = gl(&format!("{base}/approvals"))?;
        for a in list(&ap, "approved_by")? {
            if user(a, "user")? == base::me() {
                return Ok(vec![]);
            }
        }
        let why = format!("falling through the cracks ({})", note.as_deref().filter(|n| !n.is_empty()).unwrap_or("nobody to notify"));
        Ok(vec![(R.lead.clone(), "orphan", why)])
    };

    // ponytail: a review-bot-outage classifier is skipped: such a thread counts as an ordinary one here, and an
    // unknown-owner MR with one is orphaned where the original rules stay quiet. Add one if that shows up.
    if !R.people.contains(&own) {
        note = Some(format!("unknown author @{}", user(mr, "author")?));
        return orphan(mentioned, pending, &note);
    }
    if mr.get("has_conflicts").is_some_and(py::truthy) {
        return Ok(vec![(own, "conflict", format!("needs a rebase onto {}", py::str_(k(mr, "target_branch")?)))]);
    }
    let pipes = gl(&format!("{base}/pipelines?per_page=1"))?;
    if py::truthy(&pipes) {
        let p0 = pipes.get(0).ok_or_else(|| py::raise::<()>("IndexError", "no pipeline").unwrap_err())?;
        let status = k(p0, "status")?;
        if status != "success" {
            pending = PENDING.iter().any(|s| status == s);
            // a pipeline we can't read doesn't block, go on to threads
            if let Ok(jobs) = gl(&format!("projects/{}/pipelines/{}/jobs?per_page=100", pid(proj), py::str_(k(p0, "id")?))) {
                let mut failed: Option<&Value> = None; // the newest failed job: highest id, first on ties
                for j in jobs.as_array().into_iter().flatten() {
                    if k(j, "status")? == "failed" {
                        let id = k(j, "id")?.as_f64().unwrap_or(0.0);
                        if failed.is_none_or(|f| id > f["id"].as_f64().unwrap_or(0.0)) {
                            failed = Some(j);
                        }
                    }
                }
                if let Some(f) = failed {
                    return Ok(vec![(own, "pipeline", format!("CI failed: {}", py::str_(k(f, "stage")?)))]);
                }
                note = Some("pipeline".into());
                return orphan(mentioned, pending, &note);
            }
        }
    }
    let mut blocked = false;
    for d in gl_all(&format!("{base}/discussions"))? {
        let notes: Vec<&Value> = list(&d, "notes")?.iter().filter(|n| n.get("resolvable").is_some_and(py::truthy)).collect();
        if notes.is_empty() || notes.iter().all(|n| n.get("resolved").is_some_and(py::truthy)) {
            continue;
        }
        let last = user(notes[notes.len() - 1], "author")?;
        let mut who = BTreeSet::from([own.clone()]);
        for n in &notes {
            who.insert(user(n, "author")?);
        }
        who.remove(&last);
        if who.is_empty() {
            continue;
        }
        blocked = true;
        note = Some("discussion".into());
        hits.extend(who.into_iter().filter(|u| R.people.contains(u)).map(|u| (u, "discussion", format!("@{last} is waiting on you in a thread"))));
    }
    mentioned = !hits.is_empty();
    if !blocked {
        let ap = gl(&format!("{base}/approvals"))?;
        let left = ap.get("approvals_left").cloned().unwrap_or(Value::Null);
        if !py::truthy(&left) {
            return Ok(vec![]); // ready: the rules say "ship it" to the lead dev, which isn't a block
        }
        let mut approved = BTreeSet::new();
        for a in list(&ap, "approved_by")? {
            approved.insert(user(a, "user")?);
        }
        let mut reviewed = BTreeSet::new();
        // fail open: an error nags rather than mutes
        let revs = || -> Res<BTreeSet<String>> {
            let mut s = BTreeSet::new();
            for r in gl(&format!("{base}/reviewers"))?.as_array().into_iter().flatten() {
                if r.get("state").is_some_and(|x| x == "reviewed" || x == "requested_changes") {
                    s.insert(user(r, "user")?);
                }
            }
            Ok(s)
        };
        reviewed.extend(revs().unwrap_or_default());
        let since_push = || -> Res<BTreeSet<String>> {
            let c = gl(&format!("{base}/commits?per_page=1"))?;
            let c0 = c.get(0).ok_or_else(|| py::raise::<()>("IndexError", "no commits").unwrap_err())?;
            let since = ts(k(c0, "committed_date")?)?;
            let mut s = BTreeSet::new();
            for n in gl(&format!("{base}/notes?sort=desc&per_page=100"))?.as_array().into_iter().flatten() {
                if !n.get("system").is_some_and(py::truthy) && ts(k(n, "created_at")?)? > since {
                    s.insert(user(n, "author")?);
                }
            }
            Ok(s)
        };
        reviewed.extend(since_push().unwrap_or_default());
        let mut names = BTreeSet::new();
        for r in ap.get("approval_rules_left").filter(|v| py::truthy(v)).and_then(Value::as_array).into_iter().flatten() {
            names.extend(R.teams.get(&py::str_(k(r, "name")?)).map(strs).unwrap_or_default());
        }
        names.remove(&user(mr, "author")?);
        names.retain(|u| !approved.contains(u) && !reviewed.contains(u));
        if !ap.get("user_can_approve").map_or(true, py::truthy) {
            // glab is me: my commits are in it, so GitLab won't take my approval
            names.remove(base::me()); // can't approve it, so it's no block on me
        }
        hits = if names.is_empty() {
            let n = left.as_f64().unwrap_or(0.0);
            vec![(own, "approval", format!("needs {} more approval{}", py::str_(&left), if n > 1.0 { "s" } else { "" }))]
        } else {
            names.into_iter().map(|u| (u, "approval", "awaiting your review".to_string())).collect()
        };
        mentioned = true;
        note = Some("approval".into());
    }
    hits.extend(orphan(mentioned, pending, &note)?);
    Ok(hits)
}

fn open_mrs(proj: &str, prefix: &Value) -> Res<Vec<Value>> {
    let p = pid(proj);
    let mrs: Vec<Value> = if py::truthy(prefix) {
        let pre = py::str_(prefix);
        let mut mrs = Map::new(); // by iid, first seen wins
        for s in gl_all(&format!("projects/{p}/milestones?state=active"))? {
            if py::str_(k(&s, "title")?).starts_with(&pre) {
                for m in gl_all(&format!("projects/{p}/milestones/{}/merge_requests", py::str_(k(&s, "id")?)))? {
                    mrs.entry(py::repr(k(&m, "iid")?)).or_insert(m);
                }
            }
        }
        mrs.into_iter().map(|(_, m)| m).collect()
    } else {
        gl_all(&format!("projects/{p}/merge_requests?state=opened"))?
    };
    let pid_num = k(&gl(&format!("projects/{p}"))?, "id")?.clone();
    let mut out = vec![];
    for m in mrs {
        if k(&m, "state")? == "opened" && !m.get("draft").is_some_and(py::truthy) && k(&m, "project_id")? == &pid_num {
            out.push(m);
        }
    }
    Ok(out)
}

/// {"<project>!<iid>": {reason, why, n, url, title}} for every MR blocked on me. Errs when a repo can't be
/// listed (no answer is not "nothing blocked").
pub fn scan() -> Res<Value> {
    let mut jobs = vec![];
    for (proj, pre) in &R.repos {
        for m in open_mrs(proj, pre)? {
            jobs.push((proj.clone(), m));
        }
    }
    // ponytail: an MR whose check errored isn't orphaned; a GitLab blip shouldn't make a hex, so it's skipped until the next scan
    let all = py::pmap(&jobs, 8, |(proj, m)| check(proj, m).unwrap_or_default());
    let mut out = Map::new();
    for ((proj, m), hits) in jobs.iter().zip(all) {
        for (u, reason, why) in hits {
            if u != base::me() {
                continue;
            }
            let key = format!("{proj}!{}", py::str_(k(m, "iid")?));
            if !out.contains_key(&key) {
                let b = json!({"reason": reason, "why": why, "n": 0, "url": k(m, "web_url")?, "title": k(m, "title")?});
                out.insert(key.clone(), b);
            }
            let b = &mut out[&key];
            if b["reason"] == reason {
                // first reason wins; threads are counted
                b["n"] = json!(b["n"].as_i64().unwrap() + 1);
            }
        }
    }
    for b in out.values_mut() {
        if b["reason"] == "discussion" && b["n"].as_i64().unwrap() > 1 {
            b["why"] = json!(format!("{} threads are waiting on you", b["n"]));
        }
    }
    Ok(Value::Object(out))
}

/// The cached scan when younger than CACHE, else a new one; None when GitLab couldn't answer; {} when off.
pub fn blocks(fresh: bool) -> Res<Option<Value>> {
    if R.repos.is_empty() {
        return Ok(Some(json!({})));
    }
    let c = cached(CACHE)?;
    if c.is_some() && !fresh {
        return Ok(c);
    }
    let b = match scan() {
        Ok(b) => b,
        Err(e) => {
            eprintln!("blocking scan failed: {e}");
            return Ok(None);
        }
    };
    std::fs::create_dir_all(&*STATE)?;
    std::fs::write(file(), py::dumps_indent(&json!({"ts": py::time() as i64, "blocks": b})))?;
    Ok(Some(b))
}

/// cached()'s default max_age: 6h.
pub const MAX_AGE: f64 = 6.0 * 3600.0;

/// The last scan's blocks, or None when there's none younger than max_age (a stopped automation
/// mustn't pin "needs you" on a hex forever).
pub fn cached(max_age: f64) -> Res<Option<Value>> {
    if R.repos.is_empty() {
        return Ok(Some(json!({})));
    }
    let Some(c) = std::fs::read_to_string(file()).ok().and_then(|s| py::loads(&s).ok()) else { return Ok(None) };
    let Value::Object(c) = c else { return py::err(format!("{}: not a JSON object", file().display())) };
    let t = c.get("ts").map_or(Some(0.0), Value::as_f64).ok_or_else(|| format!("{}: ts is not a number", file().display()))?;
    Ok(if py::time() - t <= max_age { c.get("blocks").filter(|b| !b.is_null()).cloned() } else { None })
}

const HELP: &str = "usage: blocking [-h] [--json] [--fresh]

options:
  -h, --help  show this help message and exit
  --json
  --fresh     scan now even if the cache is young";

pub fn main(args: Vec<String>) -> Res<i32> {
    // argparse: --json, --fresh, -h, unambiguous prefixes of the long options
    let (mut json_, mut fresh, mut bad) = (false, false, vec![]);
    for a in &args {
        let (name, val) = a.split_once('=').map_or((a.as_str(), None), |(n, v)| (n, Some(v)));
        let hit: Vec<&str> = if a == "-h" {
            vec!["--help"]
        } else if name.len() > 2 && name.starts_with("--") {
            ["--help", "--json", "--fresh"].into_iter().filter(|o| o.starts_with(name)).collect()
        } else {
            vec![]
        };
        match hit.as_slice() {
            [o] if val.is_some() => py::argparse_exit("blocking", &format!("argument {o}: ignored explicit argument '{}'", val.unwrap())),
            ["--help"] => {
                println!("{HELP}");
                return Ok(0);
            }
            ["--json"] => json_ = true,
            ["--fresh"] => fresh = true,
            _ => bad.push(a.as_str()),
        }
    }
    if !bad.is_empty() {
        py::argparse_exit("blocking", &format!("unrecognized arguments: {}", bad.join(" ")));
    }
    let Some(b) = blocks(fresh)? else {
        py::sys_exit("blocking: GitLab couldn't answer, so this is no answer (not 'nothing blocked')")
    };
    if json_ {
        println!("{}", py::dumps_indent(&b));
        return Ok(0);
    }
    let order = ["approval", "discussion", "orphan", "conflict", "pipeline"]; // what I do first, then what's mine to fix
    let empty = Map::new();
    let mut rows: Vec<(usize, &String, &Value)> = vec![];
    for (key, v) in b.as_object().unwrap_or(&empty) {
        let r = py::str_(k(v, "reason")?);
        let Some(i) = order.iter().position(|o| *o == r) else {
            return py::err(format!("{}: unknown block reason {} on {key}", file().display(), py::repr_str(&r)));
        };
        rows.push((i, key, v));
    }
    rows.sort_by(|a, b| (a.0, a.1).cmp(&(b.0, b.1)));
    for (_, key, v) in rows {
        println!("- [{}] {key} {} — {}\n  {}", py::str_(&v["reason"]), py::head(&py::str_(&v["title"]), 70), py::str_(&v["why"]), py::str_(&v["url"]));
    }
    if b.as_object().is_none_or(Map::is_empty) {
        println!("nothing is blocked on me");
    }
    Ok(0)
}
