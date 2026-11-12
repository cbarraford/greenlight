//! Port of scripts/ade_t3.py: T3 Code backend: answers the `orca ...` calls greenlight makes, in Orca's JSON
//! shape (GREENLIGHT_ADE=t3).
//!
//! A workspace is a T3 project (one git worktree each); its threads are both its agents and its "terminals", so a
//! terminal handle is a thread id. Goals live in ~/.greenlight/goals.json (T3 has no project description). Needs the
//! T3 Code server running (the desktop app, or `t3 serve`).
use crate::ade_common::{self as c, default_agent, git_info, goals, repo_list, set_goal, worktree_add, worktree_remove};
use crate::py::{self, Obj, Res};
use serde_json::{json, Value};
use std::path::PathBuf;

fn home() -> PathBuf {
    match std::env::var("T3CODE_HOME") {
        Ok(h) if !h.is_empty() => PathBuf::from(h),
        _ => py::home().join(".t3"),
    }
}
/// greenlight's agent name -> T3 provider id, where they differ
fn provider(agent: &str) -> &str {
    if agent == "claude" { "claudeAgent" } else { agent }
}
/// T3 provider id -> greenlight's agent name
fn agent_of(t: &Value) -> Value {
    match t.get("providerInstanceId") {
        Some(Value::String(p)) if p == "claudeAgent" => json!("claude"),
        p => p.cloned().unwrap_or(Value::Null),
    }
}
fn working(t: &Value) -> bool {
    matches!(t["status"].as_str(), Some("running" | "starting"))
}

fn t3(args: &[&str], stdin: Option<&str>) -> Res<String> {
    let exe = py::which("t3").unwrap_or_else(|| home().join("bin").join("t3"));
    let mut cmd = vec![exe.display().to_string()];
    cmd.extend(args.iter().map(|a| a.to_string()));
    let p = py::exec(&cmd, stdin, Some(120.0), None, &[])?;
    if p.rc != 0 {
        let e = py::strip(if p.err.is_empty() { &p.out } else { &p.err });
        return py::err(format!("t3 {}: {}", args[..args.len().min(2)].join(" "), py::head(e, 300)));
    }
    Ok(p.out)
}

/// datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000, truncated; naive times are local.
fn ms(iso: &Value) -> Res<i64> {
    use chrono::{DateTime, Local, NaiveDate, NaiveDateTime, TimeZone, Timelike};
    if !py::truthy(iso) {
        return Ok(0);
    }
    let s = py::str_(iso).replace('Z', "+00:00");
    if let Ok(d) = DateTime::parse_from_rfc3339(&s) {
        return Ok((d.timestamp_micros() as f64 / 1e6 * 1000.0) as i64);
    }
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(&s, f).ok())
        .or_else(|| NaiveDate::parse_from_str(&s, "%Y-%m-%d").ok().and_then(|d| d.and_hms_opt(0, 0, 0)));
    match naive.and_then(|n| Local.from_local_datetime(&n).earliest().map(|d| (d, n.nanosecond() / 1000))) {
        Some((d, us)) => Ok(((d.timestamp() as f64 + us as f64 / 1e6) * 1000.0) as i64),
        None => py::raise("ValueError", format!("Invalid isoformat string: {}", py::repr_str(&s))),
    }
}

fn db_path() -> PathBuf {
    home().join("userdata").join("statev2.sqlite")
}

/// (project_id, title, workspace_root) of every live project.
fn projects() -> Res<Vec<(String, Value, String)>> {
    // ponytail: the CLI has no `project list`, so read T3's own state DB (read-only); swap in the CLI if it grows one
    use rusqlite::{types::ValueRef, Connection, OpenFlags};
    let path = db_path();
    let fail = |e: rusqlite::Error| -> Box<dyn std::error::Error + Send + Sync> {
        if path.exists() {
            format!("T3 Code state db {}: {e}", path.display()).into()
        } else {
            format!("no T3 Code state db at {} (is T3 Code installed? T3CODE_HOME overrides ~/.t3)", path.display()).into()
        }
    };
    let db = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(fail)?;
    let mut st = db.prepare("select project_id, title, workspace_root from projection_projects where deleted_at is null").map_err(fail)?;
    let text = |v: ValueRef| match v {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(i) => json!(i),
        ValueRef::Real(f) => json!(f),
        ValueRef::Text(t) | ValueRef::Blob(t) => json!(String::from_utf8_lossy(t)),
    };
    let rows = st
        .query_map([], |r| Ok((py::str_(&text(r.get_ref(0)?)), text(r.get_ref(1)?), py::str_(&text(r.get_ref(2)?)))))
        .map_err(fail)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(fail)?;
    Ok(rows)
}

fn threads(pid: &str) -> Res<Vec<Value>> {
    let v = py::loads(&t3(&["thread", "list", "--project", pid, "--json"], None)?)?;
    Ok(v.as_array().cloned().unwrap_or_default())
}

fn state(t: &Value) -> &'static str {
    if t.get("pendingRequestCount").is_some_and(py::truthy) || t["status"] == "waiting" {
        return "waiting";
    }
    if working(t) { "working" } else { "done" }
}

fn worktrees() -> Res<Vec<Value>> {
    let (g, mut out) = (goals(), vec![]);
    for (pid, title, root) in projects()? {
        let mut ts = threads(&pid)?;
        ts.sort_by(|a, b| py::gs(b, "updatedAt").cmp(py::gs(a, "updatedAt")));
        if let Some(t0) = ts.first_mut() {
            // the newest thread is the one greenlight acts on: fetch its pending requests and last reply
            let shown = py::loads(&t3(&["thread", "show", &py::str_(&t0["threadId"]), "--json", "--messages", "4"], None)?)?;
            let msgs = shown["messages"].as_array().map_or(&[][..], Vec::as_slice);
            let last = msgs.iter().rev().find(|m| m["role"] == "assistant").map_or(json!(""), |m| m["text"].clone());
            let mut th = shown["thread"].as_object().cloned().unwrap_or_default();
            th.insert("last".into(), last);
            *t0 = Value::Object(th);
        }
        let mut agents = vec![];
        for t in &ts {
            let at = ms(&t["updatedAt"])?;
            agents.push(json!({"state": state(t), "agentType": agent_of(t), "lastAssistantMessage": t.get("last").cloned().unwrap_or(json!("")),
                               "updatedAt": at, "stateStartedAt": at}));
        }
        let mut o = git_info(&root)?;
        let rest = json!({"id": pid, "worktreeId": pid, "displayName": title, "comment": g.get(&root).cloned().unwrap_or(json!("")),
            "isPinned": false, "linkedGitLabMR": null, "lastOutputAt": match ts.first() { Some(t) => json!(ms(&t["updatedAt"])?), None => Value::Null },
            "agents": agents});
        o.extend(rest.as_object().unwrap().clone());
        out.push(Value::Object(o));
    }
    Ok(out)
}

fn terminals() -> Res<Value> {
    let now = (py::time() * 1000.0) as i64;
    let mut out = vec![];
    for (pid, _, _) in projects()? {
        for t in threads(&pid)? {
            let last = if working(&t) { now } else { ms(&t["updatedAt"])? };
            out.push(json!({"handle": t["threadId"], "worktreeId": pid, "agentIdentity": agent_of(&t), "lastOutputAt": last}));
        }
    }
    Ok(Value::Array(out))
}

fn read(handle: &str, limit: &Value) -> Res<Value> {
    let shown = py::loads(&t3(&["thread", "show", handle, "--json", "--messages", "20"], None)?)?;
    let mut lines = vec![];
    for m in shown["messages"].as_array().map_or(&[][..], Vec::as_slice) {
        let s = format!("{} {}", if m["role"] == "user" { "❯" } else { "⏺" }, py::str_(&m["text"]));
        lines.extend(py::splitlines(&s).into_iter().map(String::from));
    }
    // lines[-int(limit):]: 0 (or a negative) counts from the front, as Python's slice does
    let n: i64 = match limit {
        Value::Bool(b) => *b as i64,
        Value::Number(n) => n.as_i64().unwrap_or(0),
        v => {
            let s = py::str_(v);
            match py::strip(&s).replace('_', "").parse() {
                Ok(n) => n,
                Err(_) => return py::err(format!("--limit must be a whole number, got {}", py::repr_str(&s))),
            }
        }
    };
    let len = lines.len() as i64;
    let start = if -n < 0 { (len - n).max(0) } else { (-n).min(len) };
    // ponytail: T3's composer isn't readable from the CLI, so an unsent draft there is invisible to greenlight
    Ok(json!({"terminal": {"handle": handle, "tail": lines[start as usize..], "draft": ""}}))
}

fn launch(pid: &str, agent: &str, prompt: &str) -> Res<Value> {
    let provider = provider(agent);
    let mf = home().join("userdata").join("model-manifest.json");
    let manifest = py::loads(&std::fs::read_to_string(&mf).map_err(|e| format!("{}: {e}", mf.display()))?)?;
    // ponytail: T3's first current model; add a knob if it's the wrong pick
    let Some(model) = manifest["manifest"]["currentModels"][provider].get(0) else {
        return py::err(format!("{} has no current model for provider {}", mf.display(), py::repr_str(provider)));
    };
    let out = t3(&["thread", "start", pid, "-", "--provider", provider, "--model", &py::str_(model), "--json"],
                 Some(if prompt.is_empty() { "Stand by for instructions." } else { prompt }))?;
    let r = py::loads(&out)?;
    Ok(json!({"terminal": {"handle": r["threadId"]}}))
}

fn create(f: &Obj) -> Res<Value> {
    let want = c::req(f, "repo")?;
    let want = want.strip_prefix("id:").unwrap_or(&want).to_string();
    let ws = worktrees()?;
    let repo = ws.iter().find(|w| py::gs(w, "repoId") == want || py::gs(w, "repo") == want).map_or(want.clone(), |w| py::gs(w, "repoId").into());
    let name = c::req(f, "name")?;
    let path = worktree_add(&repo, &name, c::opt(f, &["base-branch", "base"]).as_deref())?;
    let out = t3(&["project", "add", "--title", &name, &path], None)?;
    // "Added project <id> (<title>) at <path>."
    let Some(pid) = py::split_ws(&out).get(2).map(|s| s.to_string()) else {
        return py::err(format!("t3 project add: can't find the project id in its output {}", py::repr_str(py::strip(&out))));
    };
    if let Some(cm) = c::opt(f, &["comment"]) {
        set_goal(&path, &cm)?;
    }
    if let Some(agent) = c::opt(f, &["agent"]) {
        launch(&pid, &agent, &c::opt(f, &["prompt"]).unwrap_or_default())?;
    }
    Ok(json!({"worktree": {"id": pid, "path": path}}))
}

pub fn call(args: &[String]) -> Res<Value> {
    let (noun, verb, f) = c::parse(args)?;
    let wid = c::id_flag(&f, "worktree");
    match (noun, verb) {
        ("worktree", "ps" | "list") => Ok(json!({"worktrees": worktrees()?})),
        ("terminal", "list") => Ok(json!({"terminals": terminals()?})),
        ("terminal", "read") => read(&c::req(&f, "terminal")?, f.get("limit").unwrap_or(&json!(200))),
        ("terminal", "send") => {
            // a thread takes whole messages; a busy one queues it as its next turn
            t3(&["thread", "send", &c::req(&f, "terminal")?, "-"], Some(&py::str_(f.get("text").unwrap_or(&json!("")))))?;
            Ok(json!({}))
        }
        ("agent", "launch") => {
            let agent = c::opt(&f, &["agent"]).unwrap_or_else(default_agent);
            launch(&wid, &agent, &py::str_(f.get("prompt").unwrap_or(&json!(""))))
        }
        ("worktree", "set") => {
            // board status and MR links are Orca-only
            if f.contains_key("comment") {
                let Some((_, _, root)) = projects()?.into_iter().find(|(p, _, _)| *p == wid) else {
                    return py::err(format!("no worktree with id {}", py::repr_str(&wid)));
                };
                set_goal(&root, &c::comment(&f))?;
            }
            Ok(json!({}))
        }
        ("worktree", "rm") => {
            let all = worktrees()?;
            let ws = c::find(&all, &wid)?;
            worktree_remove(ws)?; // first: git refuses a dirty tree, and then the project stays
            t3(&["project", "remove", "--force", &wid], None)?; // --force: drop its threads too, as Orca drops terminals
            set_goal(py::gs(ws, "path"), "")?;
            Ok(json!({"removed": wid}))
        }
        ("worktree", "create") => create(&f),
        ("repo", "list") => Ok(repo_list(&worktrees()?)),
        _ => py::err(format!("orca {noun} {verb} has no t3 equivalent")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn t3_state_and_ms() {
        assert_eq!(state(&json!({"status": "running"})), "working");
        assert_eq!(state(&json!({"status": "running", "pendingRequestCount": 1})), "waiting");
        assert_eq!(state(&json!({"status": "completed"})), "done");
        assert_eq!(ms(&json!("2026-10-27T20:55:40.176Z")).unwrap(), 1793134540176);
        assert_eq!(ms(&json!("")).unwrap(), 0);
    }
}
