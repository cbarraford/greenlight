//! Port of scripts/ade_cmux.py: cmux backend: answers the `orca ...` calls greenlight makes, in Orca's JSON
//! shape (GREENLIGHT_ADE=cmux).
//!
//! cmux has workspaces and terminals but no worktrees, repos or agent records, so those come from git
//! and from cmux's agent hook stores (~/.cmuxterm/*-hook-sessions.json). The goal lives in the
//! workspace description, as it lives in the Orca comment. Needs cmux's socket in Automation mode
//! (automation.socketControlMode in ~/.config/cmux/cmux.json).
use crate::ade_common::{self as c, default_agent, draft, git_info, repo_list, worktree_add, worktree_remove};
use crate::harness;
use crate::py::{self, Obj, Res};
use serde_json::{json, Value};

/// cmux lifecycle -> Orca agent state
fn state(lifecycle: &Value) -> &'static str {
    match lifecycle.as_str() {
        Some("running") => "working",
        Some("needsInput") => "waiting",
        Some("idle") => "done",
        _ => "unknown",
    }
}

fn cmux(args: &[&str], js: bool) -> Res<Value> {
    let mut cmd = crate::base::s(&["cmux", "--id-format", "uuids"]);
    if js {
        cmd.push("--json".into());
    }
    cmd.extend(args.iter().map(|a| a.to_string()));
    let p = py::exec(&cmd, None, Some(60.0), None, &[("CMUX_QUIET", "1")])?;
    if p.rc != 0 {
        let e = py::strip(if p.err.is_empty() { &p.out } else { &p.err });
        return py::err(format!("cmux {}: {}", args.join(" "), py::head(e, 300)));
    }
    if js {
        py::loads(&p.out)
    } else {
        Ok(json!(p.out))
    }
}

/// Live agent sessions by surface id.
fn sessions() -> Obj {
    let mut out = Obj::new();
    // <agent>-hook-sessions.json, written by `cmux hooks setup <agent>`; glob order is directory order, as in Python
    let Ok(dir) = std::fs::read_dir(py::home().join(".cmuxterm")) else { return out };
    for e in dir.flatten() {
        if !e.file_name().to_string_lossy().ends_with("-hook-sessions.json") {
            continue;
        }
        let Some(d) = std::fs::read_to_string(e.path()).ok().and_then(|s| py::loads(&s).ok()) else { continue };
        let live: Vec<&Value> = d.get("activeSessionsBySurface").and_then(Value::as_object).into_iter().flatten().map(|(_, v)| &v["sessionId"]).collect();
        for s in d.get("sessions").and_then(Value::as_object).into_iter().flatten().map(|(_, s)| s) {
            if live.contains(&s.get("sessionId").unwrap_or(&Value::Null)) {
                out.insert(py::str_(&s["surfaceId"]), s.clone());
            }
        }
    }
    out
}

fn launcher(a: &Value) -> Option<&Value> {
    a.get("launchCommand").filter(|l| py::truthy(l)).and_then(|l| l.get("launcher")).filter(|l| py::truthy(l))
}

fn terminals() -> Res<Value> {
    let agents = sessions();
    let mut out = vec![];
    let tree = cmux(&["tree", "--all"], true)?;
    for win in arr(&tree["windows"]) {
        for ws in arr(&win["workspaces"]) {
            for pane in arr(&ws["panes"]) {
                for s in arr(&pane["surfaces"]) {
                    if s["type"] == "terminal" {
                        let ag = agents.get(&py::str_(&s["id"]));
                        let ident = match ag {
                            Some(a) => launcher(a).cloned().unwrap_or(json!("claude")),
                            None => Value::Null,
                        };
                        // ponytail: no hook, no timestamp, so cleanup's "active in the last 30m" guard
                        // can't see a busy plain shell; diff read-screen snapshots if that bites
                        let last = match ag.and_then(|a| a.get("updatedAt")) {
                            Some(u) if py::truthy(u) => c::x1000(u),
                            Some(u) => u.clone(),
                            None => Value::Null,
                        };
                        out.push(json!({"handle": s["id"], "worktreeId": ws["id"], "title": s.get("title"),
                                        "agentIdentity": ident, "lastOutputAt": last}));
                    }
                }
            }
        }
    }
    Ok(Value::Array(out))
}

fn arr(v: &Value) -> &[Value] {
    v.as_array().map_or(&[], Vec::as_slice)
}

fn num(v: Option<&Value>) -> f64 {
    v.filter(|x| py::truthy(x)).and_then(Value::as_f64).unwrap_or(0.0)
}

fn worktrees() -> Res<Vec<Value>> {
    let agents = sessions();
    let mut out = vec![];
    for win in arr(&cmux(&["list-windows"], true)?) {
        let list = cmux(&["workspace", "list", "--window", &py::str_(&win["id"])], true)?;
        for w in arr(&list["workspaces"]) {
            let path = w.get("current_directory").filter(|p| py::truthy(p)).map(py::str_).unwrap_or_default();
            let mut ags: Vec<&Value> = agents.values().filter(|a| a.get("workspaceId").unwrap_or(&Value::Null) == &w["id"]).collect();
            ags.sort_by(|a, b| num(b.get("updatedAt")).total_cmp(&num(a.get("updatedAt"))));
            let last = match ags.first() {
                Some(a) => match a.get("updatedAt") {
                    Some(u) => c::x1000(u),
                    None => return py::key_err("updatedAt"),
                },
                None => Value::Null,
            };
            let mut o = git_info(&path)?;
            let comment = w.get("description").filter(|d| py::truthy(d)).cloned().unwrap_or(json!(""));
            let rest = json!({"id": w["id"], "worktreeId": w["id"], "displayName": w["title"], "comment": comment,
                "isPinned": w.get("pinned"), "linkedGitLabMR": null, "lastOutputAt": last,
                "agents": ags.iter().map(|a| {
                    let at = c::x1000(a.get("updatedAt").unwrap_or(&json!(0)));
                    json!({"state": state(a.get("agentLifecycle").unwrap_or(&Value::Null)),
                           "agentType": launcher(a).cloned().unwrap_or(json!("claude")),
                           "lastAssistantMessage": a.get("lastBody").filter(|b| py::truthy(b)).cloned().unwrap_or(json!("")),
                           "updatedAt": at, "stateStartedAt": at})
                }).collect::<Vec<_>>()});
            o.extend(rest.as_object().unwrap().clone());
            out.push(Value::Object(o));
        }
    }
    Ok(out)
}

fn read(handle: &str, limit: &str) -> Res<Value> {
    // ponytail: read-screen is plain text, so Claude's dim prompt suggestion reads as a draft and greenlight won't
    // type over it; herdr reads ANSI and drops it. Fix if cmux grows an ANSI read
    let screen = cmux(&["read-screen", "--surface", handle, "--lines", limit], true)?;
    let text = screen.get("text").map_or_else(|| py::key_err("text"), Ok)?;
    let lines = py::splitlines(&py::str_(text)).into_iter().map(String::from).collect::<Vec<_>>();
    Ok(json!({"terminal": {"handle": handle, "tail": lines, "draft": draft(&lines)}}))
}

fn send(handle: &Value, text: &Value, enter: bool) -> Res<()> {
    if enter {
        // terminal.paste keeps a multi-line brief one message and submits it; typed newlines would each submit
        cmux(&["rpc", "terminal.paste", &py::dumps(&json!({"surface_id": handle, "text": text}))], true)?;
    } else {
        // ponytail: typed, for a menu pick; terminal.paste always submits
        cmux(&["send", "--surface", &py::str_(handle), "--", &py::str_(text)], false)?;
    }
    Ok(())
}

fn launch(ws_id: &str, agent: &str, prompt: &str) -> Res<Value> {
    let s = cmux(&["new-pane", "--type", "terminal", "--workspace", ws_id, "--focus", "false"], true)?["surface_id"].clone();
    std::thread::sleep(std::time::Duration::from_secs(2)); // ponytail: fixed wait for the shell prompt; poll read-screen if 2s ever proves short
    send(&s, &json!(py::shlex_join(&harness::interactive(agent, prompt))), true)?;
    Ok(json!({"terminal": {"handle": s}}))
}

fn create(f: &Obj) -> Res<Value> {
    let want = c::req(f, "repo")?;
    let want = want.strip_prefix("id:").unwrap_or(&want).to_string();
    let ws = worktrees()?;
    let repo = ws.iter().find(|w| py::gs(w, "repoId") == want || py::gs(w, "repo") == want).map_or(want.clone(), |w| py::gs(w, "repoId").into());
    let name = c::req(f, "name")?;
    let path = worktree_add(&repo, &name, c::opt(f, &["base-branch", "base"]).as_deref())?;
    let mut args = vec!["workspace", "create", "--name", &name, "--cwd", &path, "--focus", "false"];
    let comment = c::opt(f, &["comment"]);
    if let Some(cm) = &comment {
        args.extend(["--description", cm]);
    }
    let w = cmux(&args, true)?;
    let ws_id = w.get("workspace_id").filter(|x| py::truthy(x)).or(w.get("id")).cloned().unwrap_or(Value::Null);
    if let Some(agent) = c::opt(f, &["agent"]) {
        launch(&py::str_(&ws_id), &agent, &c::opt(f, &["prompt"]).unwrap_or_default())?;
    }
    Ok(json!({"worktree": {"id": ws_id, "path": path}}))
}

pub fn call(args: &[String]) -> Res<Value> {
    let (noun, verb, f) = c::parse(args)?;
    let wid = c::id_flag(&f, "worktree");
    match (noun, verb) {
        ("worktree", "ps" | "list") => Ok(json!({"worktrees": worktrees()?})),
        ("terminal", "list") => Ok(json!({"terminals": terminals()?})),
        ("terminal", "read") => read(&c::req(&f, "terminal")?, &py::str_(f.get("limit").unwrap_or(&json!(200)))),
        ("terminal", "send") => {
            let t = f.get("terminal").cloned().map_or_else(|| py::err("missing --terminal"), Ok)?;
            send(&t, f.get("text").unwrap_or(&json!("")), f.contains_key("enter"))?;
            Ok(json!({}))
        }
        ("agent", "launch") => {
            let agent = c::opt(&f, &["agent"]).unwrap_or_else(default_agent);
            launch(&wid, &agent, &py::str_(f.get("prompt").unwrap_or(&json!(""))))
        }
        ("worktree", "set") => {
            // board status and MR links are Orca-only; the goal is the description
            if f.contains_key("comment") {
                let cm = c::comment(&f);
                let mut a = vec!["workspace-action", "--workspace", &wid, "--action"];
                if cm.is_empty() {
                    a.push("clear-description");
                } else {
                    a.extend(["set-description", "--description", &cm]);
                }
                cmux(&a, false)?;
            }
            Ok(json!({}))
        }
        ("worktree", "rm") => {
            let all = worktrees()?;
            let ws = c::find(&all, &wid)?;
            worktree_remove(ws)?;
            cmux(&["workspace", "close", &py::str_(&ws["id"])], false)?;
            Ok(json!({"removed": ws["id"]}))
        }
        ("worktree", "create") => create(&f),
        ("repo", "list") => Ok(repo_list(&worktrees()?)),
        _ => py::err(format!("orca {noun} {verb} has no cmux equivalent")),
    }
}
