//! Port of scripts/ade_common.py: helpers the non-Orca backends share: git facts Orca reports itself,
//! orca flag parsing, Claude draft detection, and goal storage for ADEs with no comment/description field.
use crate::base;
use crate::py::{self, Obj, Res};
use fancy_regex::Regex as FRegex;
use regex::Regex;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

/// {workspace path: goal}
pub fn goals_path() -> PathBuf {
    base::STATE.join("goals.json")
}
static MENU_ITEM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^❯\s*\d\.").unwrap());
static ANSI_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\r").unwrap());
// dim text: Claude's prompt suggestion, not a draft
static DIM_RE: LazyLock<FRegex> = LazyLock::new(|| FRegex::new(r"\x1b\[2m.*?(?=\x1b\[(?:0|22)?m|$)").unwrap());
static PROJECT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?:@|://)([^/:]+)[:/](.+?)(?:\.git)?/?$").unwrap());

pub fn default_agent() -> String {
    py::str_(base::cfg("agents").get(0).unwrap_or(&Value::Null))
}

pub fn plain(line: &str, ghost: bool) -> String {
    if ghost {
        ANSI_RE.replace_all(line, "").into_owned()
    } else {
        ANSI_RE.replace_all(&DIM_RE.replace_all(line, ""), "").into_owned()
    }
}

pub fn git(path: &str, args: &[&str]) -> Res<String> {
    let mut cmd = vec!["git".to_string(), "-C".into(), path.into()];
    cmd.extend(args.iter().map(|a| a.to_string()));
    let p = py::exec(&cmd, None, Some(30.0), None, &[])?;
    Ok(if p.rc == 0 { py::strip(&p.out).to_string() } else { String::new() })
}

pub fn project_id(remote: &str) -> Option<String> {
    // git@gitlab.com:acme/widgets.git | https://gitlab.com/acme/widgets -> git:gitlab.com/acme/widgets
    PROJECT_RE.captures(remote).map(|m| format!("git:{}/{}", &m[1], &m[2]))
}

/// Path(p).name
pub fn name(p: &str) -> String {
    Path::new(p).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// The Orca worktree fields that come from git.
pub fn git_info(path: &str) -> Res<Obj> {
    let common = if path.is_empty() { String::new() } else { git(path, &["rev-parse", "--path-format=absolute", "--git-common-dir"])? };
    let repo = match common.strip_suffix("/.git") {
        Some(_) => Path::new(&common).parent().map(|p| p.display().to_string()).unwrap_or_default(),
        None => common.clone(),
    };
    let branch = if common.is_empty() { String::new() } else { git(path, &["symbolic-ref", "-q", "HEAD"])? };
    let main = !common.is_empty() && git(path, &["rev-parse", "--path-format=absolute", "--git-dir"])? == common;
    let project = if common.is_empty() { json!("") } else { json!(project_id(&git(path, &["remote", "get-url", "origin"])?)) };
    let v = json!({"path": path, "branch": branch, "isMainWorktree": main, "repoId": repo, "repo": name(&repo), "projectId": project});
    Ok(v.as_object().unwrap().clone())
}

pub fn worktree_add(repo: &str, name_: &str, base: Option<&str>) -> Res<String> {
    let r = Path::new(repo);
    let path = r.parent().unwrap_or(Path::new("")).join(format!("{}-{}", name(repo), name_.replace('/', "-"))).display().to_string();
    let mut cmd = base::s(&["git", "-C", repo, "worktree", "add", "-b", name_, &path]);
    cmd.extend(base.map(String::from));
    let p = py::exec(&cmd, None, None, None, &[])?;
    if p.rc != 0 {
        return py::err(format!("git worktree add: {}", py::head(py::strip(&p.err), 300)));
    }
    Ok(path)
}

pub fn worktree_remove(ws: &Value) -> Res<()> {
    if !py::truthy(&ws["isMainWorktree"]) {
        // git refuses a dirty tree without --force, which we never pass
        let p = py::exec(&base::s(&["git", "-C", py::gs(ws, "repoId"), "worktree", "remove", py::gs(ws, "path")]), None, None, None, &[])?;
        if p.rc != 0 {
            return py::err(format!("git worktree remove {}: {}", py::gs(ws, "path"), py::head(py::strip(&p.err), 300)));
        }
    }
    Ok(())
}

/// `--name value` pairs; a bare flag (or one followed by another `--flag`) is true. Positionals are ignored.
pub fn flags(args: &[String]) -> Obj {
    let (mut out, mut i) = (Obj::new(), 0);
    while i < args.len() {
        if let Some(k) = args[i].strip_prefix("--") {
            let val = i + 1 < args.len() && !args[i + 1].starts_with("--");
            out.insert(k.into(), if val { json!(args[i + 1]) } else { json!(true) });
            i += if val { 2 } else { 1 };
        } else {
            i += 1;
        }
    }
    out
}

/// `noun, verb, f = *args[:2], flags(args[2:])`
pub fn parse(args: &[String]) -> Res<(&str, &str, Obj)> {
    if args.len() < 2 {
        return py::err(format!("ade: need a noun and a verb (e.g. worktree list), got {}", py::repr_list(args)));
    }
    Ok((&args[0], &args[1], flags(&args[2..])))
}

/// str(f.get(k, "")).removeprefix("id:")
pub fn id_flag(f: &Obj, k: &str) -> String {
    let v = py::str_(f.get(k).unwrap_or(&json!("")));
    v.strip_prefix("id:").unwrap_or(&v).to_string()
}
/// f[k] as a str; a missing flag is a clean error instead of Python's KeyError.
pub fn req(f: &Obj, k: &str) -> Res<String> {
    match f.get(k) {
        Some(v) => Ok(py::str_(v)),
        None => py::err(format!("missing --{k}")),
    }
}
/// f.get(k) or f.get(k2) ..., as a str: the first truthy one.
pub fn opt(f: &Obj, ks: &[&str]) -> Option<String> {
    ks.iter().find_map(|k| f.get(*k).filter(|v| py::truthy(v)).map(py::str_))
}
/// `f["comment"] if isinstance(f["comment"], str) else ""`
pub fn comment(f: &Obj) -> String {
    f.get("comment").and_then(Value::as_str).unwrap_or("").to_string()
}

pub fn draft<S: AsRef<str>>(lines: &[S]) -> String {
    // Claude's input box is the last `❯` line; text after it is a draft the user hasn't sent (menu items aren't).
    // ponytail: Claude-only; a codex draft reads as empty until its prompt glyph is added here
    let last = lines.iter().map(|x| py::strip(x.as_ref())).filter(|x| x.starts_with('❯')).last();
    match last {
        Some(b) if !MENU_ITEM_RE.is_match(b) => py::strip(&b['❯'.len_utf8()..]).to_string(),
        _ => String::new(),
    }
}

pub fn goals() -> Obj {
    std::fs::read_to_string(goals_path()).ok().and_then(|s| py::loads(&s).ok()).and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

pub fn set_goal(path: &str, goal: &str) -> Res<()> {
    let mut g = goals();
    if goal.is_empty() {
        g.shift_remove(path);
    } else {
        g.insert(path.into(), json!(goal));
    }
    let f = goals_path();
    std::fs::create_dir_all(f.parent().unwrap())?;
    std::fs::write(f, py::dumps_indent(&Value::Object(g)))?;
    Ok(())
}

/// Orca's `repo list` shape: one entry per repo, named by its folder, keyed by its remote (dash.py's New task).
pub fn repo_list(worktrees: &[Value]) -> Value {
    let mut out = Obj::new();
    for w in worktrees {
        let id = py::gs(w, "repoId");
        if !id.is_empty() && !out.contains_key(id) {
            let key = py::gs(w, "projectId");
            out.insert(id.into(), json!({"id": id, "path": id, "displayName": name(id),
                "gitRemoteIdentity": {"canonicalKey": key.strip_prefix("git:").unwrap_or(key)}}));
        }
    }
    json!({"repos": out.into_iter().map(|(_, v)| v).collect::<Vec<_>>()})
}

/// The `next(w for w in worktrees() if w["id"] == wid)` lookup; an unknown id is a clean error, not StopIteration.
pub fn find<'a>(ws: &'a [Value], wid: &str) -> Res<&'a Value> {
    ws.iter().find(|w| w["id"] == json!(wid)).map_or_else(|| py::err(format!("no worktree with id {}", py::repr_str(wid))), Ok)
}

/// `x * 1000` on a JSON number, keeping int-vs-float as Python does.
pub fn x1000(v: &Value) -> Value {
    match v.as_i64() {
        Some(n) if !v.is_f64() => json!(n * 1000),
        _ => json!(v.as_f64().unwrap_or(0.0) * 1000.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parsing() {
        assert_eq!(project_id("git@gitlab.com:acme/widgets.git").unwrap(), "git:gitlab.com/acme/widgets");
        assert_eq!(project_id("https://gitlab.com/acme/ops/relay").unwrap(), "git:gitlab.com/acme/ops/relay");
        let f = flags(&base::s(&["--terminal", "t1", "--text", "hi", "--enter", "--wait-submit", "10"]));
        assert_eq!(Value::Object(f), json!({"terminal": "t1", "text": "hi", "enter": true, "wait-submit": "10"}));
        assert_eq!(draft(&["───", "❯ ", "───"]), "");
        assert_eq!(draft(&["───", "❯ half typed", "───"]), "half typed");
        assert_eq!(draft(&["Which one?", "❯ 1. Keep it", "  2. Port it"]), "");
        let ghost = "\x1b[1m❯\x1b[0m\u{a0}\x1b[0m\x1b[2mlist both words\x1b[0m\r";
        assert_eq!(plain(ghost, true), "❯\u{a0}list both words");
        assert_eq!(draft(&[plain(ghost, false)]), "");
        assert_eq!(draft(&[plain("\x1b[1m❯\x1b[0m half \x1b[32mtyped\x1b[0m", false)]), "half typed");
    }
}
