//! Port of scripts/ade_herdr.py: herdr backend: answers the `orca ...` calls greenlight makes, in Orca's JSON
//! shape (GREENLIGHT_ADE=herdr).
//!
//! herdr has workspaces, git worktrees, panes and agent detection of its own; goals live in ~/.greenlight/goals.json
//! (herdr has no workspace description). GREENLIGHT_HERDR_SESSION targets a named session instead of the default.
use crate::ade_common::{self as c, default_agent, draft, git_info, goals, plain, repo_list, set_goal};
use crate::base;
use crate::py::{self, arr, Obj, Res};
use serde_json::{json, Value};

/// herdr -> Orca agent state
fn state(s: &Value) -> &'static str {
    match s.as_str() {
        Some("working") => "working",
        Some("blocked") => "waiting",
        Some("idle" | "done") => "done",
        _ => "unknown",
    }
}

fn herdr_(args: &[&str], js: bool) -> Res<Value> {
    let mut cmd = vec!["herdr".to_string()];
    if let Ok(s) = std::env::var("GREENLIGHT_HERDR_SESSION") {
        if !s.is_empty() {
            cmd.extend(["--session".into(), s]);
        }
    }
    cmd.extend(args.iter().map(|a| a.to_string()));
    let p = py::exec(&cmd, None, Some(120.0), None, &[])?;
    if p.rc != 0 {
        let e = py::strip(if p.err.is_empty() { &p.out } else { &p.err });
        return py::err(format!("herdr {}: {}", args[..args.len().min(3)].join(" "), py::head(e, 300)));
    }
    if !js {
        return Ok(json!(p.out));
    }
    let v = py::loads(if p.out.is_empty() { "{}" } else { &p.out })?; // send-text prints nothing
    Ok(v.get("result").cloned().unwrap_or(json!({})))
}
fn herdr(args: &[&str]) -> Res<Value> {
    herdr_(args, true)
}

fn now() -> i64 {
    (py::time() * 1000.0) as i64
}

/// Live agents by pane id.
fn agents() -> Res<Obj> {
    Ok(arr(&herdr(&["agent", "list"])?["agents"]).iter().map(|a| (py::str_(&a["pane_id"]), a.clone())).collect())
}

fn terminals() -> Res<Value> {
    let (ags, now) = (agents()?, now());
    // ponytail: herdr keeps no output timestamps, so "working" stands in for "active just now"; a busy plain
    // shell looks idle to cleanup's 30m guard
    let panes = herdr(&["pane", "list"])?;
    Ok(Value::Array(arr(&panes["panes"]).iter().map(|p| {
        let ag = ags.get(&py::str_(&p["pane_id"]));
        let working = ag.is_some_and(|a| a.get("agent_status") == Some(&json!("working")));
        json!({"handle": p["pane_id"], "worktreeId": p["workspace_id"], "agentIdentity": ag.and_then(|a| a.get("agent")),
               "lastOutputAt": if working { json!(now) } else { Value::Null }})
    }).collect()))
}

fn worktrees() -> Res<Vec<Value>> {
    let (ags, g, now) = (agents()?.into_iter().map(|(_, a)| a).collect::<Vec<_>>(), goals(), now());
    let panes = herdr(&["pane", "list"])?;
    let mut cwd = Obj::new(); // first pane wins
    for p in arr(&panes["panes"]).iter().rev() {
        cwd.insert(py::str_(&p["workspace_id"]), p["cwd"].clone());
    }
    let mut out = vec![];
    for w in arr(&herdr(&["workspace", "list"])?["workspaces"]) {
        let co = w.get("worktree").filter(|x| py::truthy(x)).and_then(|x| x.get("checkout_path")).filter(|x| py::truthy(x));
        let path = py::str_(co.or(cwd.get(&py::str_(&w["workspace_id"]))).unwrap_or(&json!("")));
        let mine: Vec<&Value> = ags.iter().filter(|a| a["workspace_id"] == w["workspace_id"]).collect();
        let mut o = git_info(&path)?;
        let rest = json!({"id": w["workspace_id"], "worktreeId": w["workspace_id"], "displayName": w["label"],
            "comment": g.get(&path).cloned().unwrap_or(json!("")), "isPinned": false, "linkedGitLabMR": null,
            "lastOutputAt": if mine.iter().any(|a| a["agent_status"] == "working") { json!(now) } else { Value::Null },
            // ponytail: herdr gives no last reply or timestamps; dash shows the state without the agent's last words
            "agents": mine.iter().map(|a| json!({"state": state(&a["agent_status"]), "agentType": a["agent"], "lastAssistantMessage": "",
                                                 "updatedAt": 0, "stateStartedAt": null})).collect::<Vec<_>>()});
        o.extend(rest.as_object().unwrap().clone());
        out.push(Value::Object(o));
    }
    Ok(out)
}

fn read(handle: &str, limit: &str) -> Res<Value> {
    let out = herdr_(&["pane", "read", handle, "--source", "visible", "--lines", limit, "--format", "ansi"], false)?;
    let lines = py::splitlines(out.as_str().unwrap_or(""));
    let tail: Vec<String> = lines.iter().map(|x| plain(x, true)).collect();
    let typed: Vec<String> = lines.iter().map(|x| plain(x, false)).collect();
    Ok(json!({"terminal": {"handle": handle, "tail": tail, "draft": draft(&typed)}}))
}

fn send(handle: &str, text: &str, enter: bool) -> Res<()> {
    if enter && agents()?.contains_key(handle) {
        // agent prompt pastes (one message, newlines kept) and submits in one write
        herdr(&["agent", "prompt", handle, text])?;
    } else if enter {
        herdr(&["pane", "run", handle, text])?;
    } else {
        // a menu pick
        herdr(&["pane", "send-text", handle, text])?;
    }
    Ok(())
}

fn launch(ws_id: &str, agent: &str, prompt: &str, pane: Option<String>) -> Res<Value> {
    let pane = match pane.filter(|p| !p.is_empty()) {
        Some(p) => p,
        None => {
            let panes = herdr(&["pane", "list", "--workspace", ws_id])?;
            let Some(root) = arr(&panes["panes"]).first() else { return py::err(format!("herdr workspace {ws_id} has no panes")) };
            py::str_(&herdr(&["pane", "split", &py::str_(&root["pane_id"]), "--direction", "right", "--no-focus"])?["pane"]["pane_id"])
        }
    };
    let dashed = pane.replace(':', "-");
    // The brief rides in argv so it survives a startup block (Claude's folder-trust prompt is the user's to answer).
    // macOS caps a typed command line at ~1KB, and a newline would split it, so a long brief goes by file.
    let mut prompt = prompt.to_string();
    if !prompt.is_empty() && (prompt.contains('\n') || py::shlex_quote(&prompt).len() > 800) {
        let brief = base::STATE.join("briefs").join(format!("{dashed}-{}.md", py::time() as i64));
        std::fs::create_dir_all(brief.parent().unwrap())?;
        std::fs::write(&brief, &prompt)?;
        prompt = format!("Your brief is in {}. Read it and follow it.", brief.display());
    }
    let name = format!("{agent}-{dashed}");
    let mut args = vec!["agent", "start", &name, "--kind", agent, "--pane", &pane, "--timeout", "60000"];
    if !prompt.is_empty() {
        args.extend(["--", &prompt]);
    }
    for i in 0..15 {
        if let Err(e) = herdr(&args) {
            let msg = e.to_string();
            if py::kind(&*e) != "RuntimeError" {
                return Err(e);
            }
            if msg.contains("agent_pane_busy") && i < 14 {
                // a new pane's shell is still starting
                std::thread::sleep(std::time::Duration::from_secs(1));
                continue;
            }
            if !msg.contains("agent_not_ready") {
                // blocked at startup: it shows as waiting until the user answers
                return Err(e);
            }
        }
        break;
    }
    Ok(json!({"terminal": {"handle": pane}}))
}

fn create(f: &Obj) -> Res<Value> {
    let want = c::req(f, "repo")?;
    let want = want.strip_prefix("id:").unwrap_or(&want).to_string();
    let ws = worktrees()?;
    let src = ws.iter().find(|w| py::truthy(&w["isMainWorktree"]) && (py::gs(w, "repoId") == want || py::gs(w, "repo") == want))
        .map_or(want.clone(), |w| py::gs(w, "path").into());
    let name = c::req(f, "name")?;
    let base = c::opt(f, &["base-branch", "base"]); // Orca's flag is --base-branch
    let mut args = vec!["worktree", "create", "--cwd", &src, "--branch", &name, "--label", &name, "--no-focus"];
    if let Some(b) = &base {
        args.extend(["--base", b]);
    }
    let r = herdr(&args)?;
    let (wsid, path) = (r["workspace"]["workspace_id"].clone(), r["worktree"]["path"].clone());
    if let Some(cm) = c::opt(f, &["comment"]) {
        set_goal(&py::str_(&path), &cm)?;
    }
    if let Some(agent) = c::opt(f, &["agent"]) {
        launch(&py::str_(&wsid), &agent, &c::opt(f, &["prompt"]).unwrap_or_default(), Some(py::str_(&r["root_pane"]["pane_id"])))?;
    }
    Ok(json!({"worktree": {"id": wsid, "path": path}}))
}

pub fn call(args: &[String]) -> Res<Value> {
    let (noun, verb, f) = c::parse(args)?;
    let wid = c::id_flag(&f, "worktree");
    match (noun, verb) {
        ("worktree", "ps" | "list") => Ok(json!({"worktrees": worktrees()?})),
        ("terminal", "list") => Ok(json!({"terminals": terminals()?})),
        ("terminal", "read") => read(&c::req(&f, "terminal")?, &py::str_(f.get("limit").unwrap_or(&json!(200)))),
        ("terminal", "send") => {
            send(&c::req(&f, "terminal")?, &py::str_(f.get("text").unwrap_or(&json!(""))), f.contains_key("enter"))?;
            Ok(json!({}))
        }
        ("agent", "launch") => {
            let agent = c::opt(&f, &["agent"]).unwrap_or_else(default_agent);
            launch(&wid, &agent, &py::str_(f.get("prompt").unwrap_or(&json!(""))), None)
        }
        ("worktree", "set") => {
            // board status and MR links are Orca-only
            if f.contains_key("comment") {
                let all = worktrees()?;
                set_goal(py::gs(c::find(&all, &wid)?, "path"), &c::comment(&f))?;
            }
            Ok(json!({}))
        }
        ("worktree", "rm") => {
            let all = worktrees()?;
            let ws = c::find(&all, &wid)?;
            if py::truthy(&ws["isMainWorktree"]) {
                herdr(&["workspace", "close", &wid])?;
            } else {
                // never --force: herdr, like git, refuses a dirty tree
                herdr(&["worktree", "remove", "--workspace", &wid])?;
            }
            set_goal(py::gs(ws, "path"), "")?;
            Ok(json!({"removed": wid}))
        }
        ("worktree", "create") => create(&f),
        ("repo", "list") => Ok(repo_list(&worktrees()?)),
        _ => py::err(format!("orca {noun} {verb} has no herdr equivalent")),
    }
}
