//! Port of scripts/dash.py: live data for the Greenlight left pane (app/dashboard.html).
//!
//! Pucks join ~/.greenlight/last.json with a live `orca worktree ps`. Page layouts come from Claude Haiku
//! picking widgets out of CATALOG; the page renders only known widgets. See DOC for the commands.
use crate::base::{self, orca_s, s, AUTOMATION_RE, HOLD_RE, LIMIT_RE, PARKED_RE, ROOT, STATE};
use crate::greenlight;
use crate::py::{self, g, gd, gs, ix, or, str_, truthy, Obj, Res, NULL};
use crate::{blocking, harness};
use regex::Regex;
use serde_json::{json, Value};
use std::cmp::Ordering;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

const DOC: &str = r#"dash: live data for the Greenlight left pane (app/dashboard.html).

    greenlight dash snapshot          one JSON line: every workspace as a puck
    greenlight dash stream [secs]     a snapshot every `secs` (default 10); records state changes as history
    greenlight dash page <ws> [--fresh] [--model M] [--effort E]  one JSON line: facts + an AI-designed layout for that workspace's page
    greenlight dash suggest <ws> [--model M] [--effort E]  one JSON line: the next prompt to give an agent there (the console drawer, the
                                                            hover card's button); cached per state, and snapshots carry it as `suggestion`
    greenlight dash run <ws> < prompt                       launch an agent in that workspace with the prompt on stdin
    greenlight dash transcript <ws> [--before CURSOR]       one JSON line: the agents' conversation there, newest page first (the console)
    greenlight dash trees                                   one JSON line: every worktree of every installed ADE (orca, cmux, herdr, t3), for the terminal's sidebar
    greenlight dash tree info|rm|goal|cfg <path> [--force]  the sidebar's workspace menu: git facts; remove the worktree (refuses a dirty,
                                                            unpushed or pinned one without --force); set the goal (stdin); write the main
                                                            checkout's .greenlight.json (stdin)
    greenlight dash ask <ws> [--model M] [--effort E]       one JSON line: the waiting agent's question and the answers to offer
    greenlight dash answer <ws> < text                      answer the waiting agent (a menu choice number, or a reply + Enter)
    greenlight dash park <ws> on|off                        add or drop "(parked)" on that workspace's goal (the left dock)
    greenlight dash task repos -                            one JSON line: the repos a task can go to (New task)
    greenlight dash task new <repo> < {text, files}         start a workspace on that repo with an agent working the request; files are data URLs

Pucks join ~/.greenlight/last.json (lifecycle, MR, next step from greenlight scan, refreshed by every greenlight run; the interval is in Greenlight Settings → Dashboard)
with a live `orca worktree ps` (agent state, seconds old). Never calls GitLab for the overview.
Page layouts come from Claude Haiku picking widgets out of CATALOG; the page renders only known
widgets, so a bad layout degrades to fewer cards, never to executed output.
"#;

fn re(p: &str) -> Regex {
    Regex::new(p).unwrap()
}
// re.match's `$` also takes a trailing newline
static NAME_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[\w.@+/-]{1,200}\n?\z"));
static HIST_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^- (\d{4}-\d\d-\d\d \d\d:\d\d) (.*)"));
static VAR_RE: LazyLock<Regex> = LazyLock::new(|| re(r"\{(\w+)\}"));
static SAFE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"[^\w.@+-]"));
static MENU_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^\s*(?:❯\s*)?(\d)\.\s+(\S.*)$"));
static OBJ_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?s)\{.*\}"));
static LIST_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?s)\[.*\]"));
static CMD_RE: LazyLock<Regex> = LazyLock::new(|| re(r"<command-name>(.*?)</command-name>"));
static ARGS_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?s)<command-args>(.*?)</command-args>"));
static FILE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^data:[\w.+/-]*(?:;[\w=.-]+)*;base64,([A-Za-z0-9+/=\s]*)$"));
static FNAME_RE: LazyLock<Regex> = LazyLock::new(|| re(r"[^\w.+-]+"));
static SLUG_RE: LazyLock<Regex> = LazyLock::new(|| re(r"[^a-z0-9]+"));
static REPO_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[\w.-]{1,100}\z"));
static NOTE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[\w-]{1,100}\z"));
static DESC_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?m)^description:\s*(.+)$"));
static OPT_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[\w.-]{1,40}\z"));
static BEFORE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^[\w.-]{1,200}:\d{1,9}\z"));
// ponytail: `{1,300}` over Unicode \w is too big for the regex crate; main checks the length
static PATH_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^/[\w./@+-]+\z"));

static EMPTY_ARR: Value = Value::Array(Vec::new());
static EMPTY_STR: Value = Value::String(String::new());

fn dash() -> PathBuf {
    STATE.join("dash")
}

// Puck states, most urgent first. Size in the overview follows this order.
const STATES: [&str; 7] = ["needs", "crashed", "raised", "working", "queued", "quiet", "done"];

// widget: (props, when to use). P = also fits inside a puck.
const CATALOG: [(&str, &str, &str); 20] = [
    ("text", r#"{"text": str}"#, "P one short line of what is happening"),
    ("stat", r#"{"value": str, "label": str}"#, "P one big number or value"),
    ("bar", r#"{"done": num, "total": num, "label": str}"#, "P done out of total"),
    ("stack", r#"{"parts": [{"label": str, "value": num, "tone": tone}]}"#, "P parts of a whole"),
    ("grid", r#"{"cells": [tone], "cols": int}"#, "P many small pass/fail results"),
    ("steps", r#"{"steps": [str], "current": int}"#, "P a sequence with a current step (0-based)"),
    ("meter", r#"{"value": 0-100, "label": str}"#, "P a level against a target"),
    ("spark", r#"{"points": [num], "label": str}"#, "P a trend over time"),
    ("kv", r#"{"rows": [[str, str]]}"#, "labelled facts"),
    ("list", r#"{"items": [{"text": str, "detail": str, "tone": tone}]}"#, "a short list of things"),
    ("ranked", r#"{"items": [{"label": str, "value": 0-100}]}"#, "options or hypotheses ranked by weight"),
    ("timeline", r#"{"items": [{"ts": "YYYY-MM-DD HH:MM", "text": str, "tone": tone}]}"#, "dated events, newest first"),
    ("files", r#"{"files": [{"path": str, "add": int, "del": int}]}"#, "files changed"),
    ("log", r#"{"lines": [str]}"#, "raw output or transcript lines"),
    ("checklist", r#"{"items": [{"text": str, "done": bool}]}"#, "things to do with done state"),
    ("links", r#"{"links": [{"label": str, "url": str}]}"#, "where to go: MR, pipeline, docs"),
    ("callout", r#"{"text": str, "tone": tone}"#, "the one thing that matters most right now"),
    ("heatmap", r#"{"rows": [{"label": str, "cells": [tone]}]}"#, "results per item over runs"),
    ("table", r#"{"columns": [str], "rows": [[str]]}"#, "small comparisons"),
    ("note", r#"{"text": str}"#, "a paragraph of explanation"),
];
fn catalog(k: &str) -> Option<&'static (&'static str, &'static str, &'static str)> {
    CATALOG.iter().find(|c| c.0 == k)
}
const TONES: &str = "ok | warn | bad | info | idle";
// The page's only generated part: up to 3 equal tiles plus one wide panel. Everything else on it is fixed markup.
const TILES: [&str; 7] = ["stat", "meter", "bar", "spark", "grid", "stack", "note"];
const WIDE: [&str; 2] = ["files", "list"];
const PAGE_V: i64 = 2; // bump when the layout shape changes so cached pages are redesigned

/// The block on me for this row's open MR (blocking.py's last scan), or None.
fn block_of(r: &Value, blk: &Value) -> Value {
    let mr = g(r, "mr");
    if g(mr, "state") != "opened" {
        return Value::Null;
    }
    let b = g(blk, &format!("{}!{}", str_(g(r, "project")), str_(g(mr, "iid"))));
    if truthy(b) { b.clone() } else { Value::Null }
}

const COMMON: [&str; 6] = ["mr", "url", "title", "branch", "target", "sha"];
// card order is MOVES
fn kinds(kind: &str) -> Option<&'static [&'static str]> {
    match kind {
        "review" => Some(&["last_review_sha"]),
        "threads" => Some(&["threads"]),
        "ci" => Some(&["failed_jobs", "pipeline_url"]),
        "conflicts" => Some(&[]),
        _ => None,
    }
}

fn own_prompt(repo: &str, kind: &str) -> PathBuf {
    STATE.join("prompts").join(repo).join(format!("{kind}.md")) // yours, private to this host
}

/// (template, yours): ~/.greenlight/prompts/<repo>/<kind>.md, else the bundled prompts/<kind>.md. {default} in yours pulls that in.
fn prompt_of(repo: &str, kind: &str) -> Res<(String, bool)> {
    let d = py::strip(&read(&ROOT.join("prompts").join(format!("{kind}.md")))?).to_string();
    let f = own_prompt(repo, kind);
    Ok(if f.exists() { (py::strip(&read(&f)?).replace("{default}", &d), true) } else { (d, false) })
}

// Settings › Repositories › Auto-run: kind → (when, [(gate label, what the unattended run may do)]). Merge has no gate: greenlight merges.
type Gates = &'static [(&'static str, &'static str)];
const AUTO: [(&str, &str, Gates); 5] = [
    ("ci", "The pipeline failed on your MR", &[("Stop before push", "Stop before pushing and wait for me."),
        ("Push the fix", "You have my OK to push once the fix is proven; don't retry jobs.")]),
    ("conflicts", "Your MR has merge conflicts", &[("Stop before push", "Stop before pushing and wait for me."),
        ("Force-push", "OK to git push --force-with-lease once resolved and tests pass; if both sides can't hold, stop and ask.")]),
    ("threads", "Your MR has unresolved threads", &[("Stop before push", "Stop before pushing and wait for me."),
        ("Push, draft replies", "Push the fixes, but don't post or resolve anything; leave the replies here."),
        ("Push, post & resolve", "Push, post your replies, and resolve only the threads you fixed.")]),
    ("review", "Your review is the next move", &[("Draft only", "Don't post anything; leave the findings here."),
        ("Post comments", "Post the findings as review comments; never approve.")]),
    ("merge", "greenlight says it can merge now", &[]),
];
const AUTO_HEAD: &str = "This run was started automatically by Greenlight; nobody is watching. ";
fn gates(kind: &str) -> Option<Gates> {
    AUTO.iter().find(|a| a.0 == kind).map(|a| a.2)
}
/// isinstance(g, int) and 0 <= g < max(1, len(gates)): the gate index, bools included as Python counts them.
fn gate(v: &Value, gs_: Gates) -> Option<usize> {
    let i = match v {
        Value::Bool(b) => *b as i64,
        Value::Number(n) => n.as_i64()?,
        _ => return None,
    };
    (0..gs_.len().max(1) as i64).contains(&i).then_some(i as usize)
}

/// ~/.greenlight/autorun.json: {repo: {kind: gate index}}; a kind that's there is on.
fn autorun() -> Res<Value> {
    let f = STATE.join("autorun.json");
    if f.exists() { load(&f) } else { Ok(json!({})) }
}

/// 'Squash', 'Rebase' or 'Merge': how greenlight's merge cmd lands it.
fn merge_how(cmd: &Value) -> &'static str {
    py::arr(cmd).iter().find_map(|f| match f.as_str() {
        Some("--squash") => Some("Squash"),
        Some("--rebase") => Some("Rebase"),
        _ => None,
    }).unwrap_or("Merge")
}

/// `xs or []`, iterated.
fn list(v: &Value) -> Res<Vec<Value>> {
    py::it(or(v, &EMPTY_ARR))
}

/// Both hover cards' buttons for this row's open MR, most urgent first: conflicts › ci › threads (your MR's fixes) ›
/// review (your review is the next move) › merge (greenlight can merge now; only when nothing else applies). None → the card
/// shows greenlight's generated next step instead. Each prompt's {vars} filled in.
fn moves(r: &Value, blk: &Value) -> Res<Vec<Value>> {
    let (mr, nxt) = (g(r, "mr"), g(r, "next"));
    if g(mr, "state") != "opened" {
        return Ok(vec![]);
    }
    let mine = g(mr, "role") == "author";
    let cmd = g(nxt, "cmd");
    let signoff = match cmd {
        Value::Array(a) => a.iter().any(|x| x == "signoff"),
        Value::String(c) => c.contains("signoff"),
        _ => false,
    };
    let review = g(nxt, "owner") == "me" && (g(nxt, "step") == "review" || signoff || gs(nxt, "action").contains("review it"));
    let on = |kind: &str| match kind {
        "conflicts" => mine && truthy(g(mr, "conflicts")),
        "ci" => mine && g(mr, "pipeline") == "failed",
        "threads" => mine && truthy(g(mr, "todo_threads")),
        _ => review || g(&block_of(r, blk), "reason") == "approval",
    };
    let repo = str_(ix(r, "project")?).rsplit('/').next().unwrap().to_string();
    let goal = gs(r, "goal");
    let held = HOLD_RE.is_match(goal).unwrap_or(false) || PARKED_RE.is_match(goal) || AUTOMATION_RE.is_match(goal);
    let on_auto = if held { json!({}) } else { g(&autorun()?, &repo).clone() };
    let lines = |xs: Vec<String>| if xs.is_empty() { Value::Null } else { json!(xs.iter().map(|x| format!("- {x}")).collect::<Vec<_>>().join("\n")) };
    let mut vals = Obj::new();
    let branch = or(g(mr, "source_branch"), g(r, "branch"));
    for (k, v) in COMMON.iter().zip([g(mr, "ref"), g(mr, "web_url"), g(mr, "title"), branch, g(mr, "target_branch"), g(mr, "sha")]) {
        vals.insert(k.to_string(), v.clone());
    }
    vals.insert("last_review_sha".into(), g(mr, "reviewed_sha").clone());
    vals.insert("threads".into(), lines(list(g(mr, "todo_threads"))?.iter().map(str_).collect()));
    vals.insert("pipeline_url".into(), g(mr, "pipeline_url").clone());
    let jobs = list(g(mr, "failed_jobs"))?;
    let failed = jobs.iter().map(|j| Ok(format!("{} ({}): {}", str_(ix(j, "name")?), str_(ix(j, "stage")?), str_(ix(j, "url")?)))).collect::<Res<Vec<_>>>()?;
    vals.insert("failed_jobs".into(), lines(failed));
    let mut out = vec![];
    for kind in ["conflicts", "ci", "threads", "review"] {
        if !on(kind) {
            continue;
        }
        let (text, own) = prompt_of(&repo, kind)?;
        let ok: Vec<&str> = COMMON.iter().chain(kinds(kind).unwrap()).copied().collect();
        // one pass: a title can't fill in
        let text = VAR_RE.replace_all(&text, |m: &regex::Captures| {
            if !ok.contains(&&m[1]) {
                return m[0].to_string();
            }
            let v = &vals[&m[1]];
            if truthy(v) { str_(v) } else { "none".into() }
        });
        let names = jobs.iter().map(|j| ix(j, "name").cloned()).collect::<Res<Vec<_>>>()?;
        out.push(json!({"kind": kind, "prompt": text, "own": own, "repo": repo, "n": list(g(mr, "todo_threads"))?.len(), "jobs": names}));
    }
    let head = py::arr(cmd).first();
    if out.is_empty() && g(nxt, "step") == "merge" && head.is_some_and(|c| c == "gh" || c == "glab") {
        out.push(json!({"kind": "merge", "how": merge_how(cmd), "target": g(mr, "target_branch")}));
    }
    for m in out.iter_mut() {
        // auto: what the terminal runs unattended (merge: True), once per head commit
        let kind = gs(m, "kind").to_string();
        let gs_ = gates(&kind).unwrap();
        if let Some(i) = gate(g(&on_auto, &kind), gs_) {
            m["sha"] = g(mr, "sha").clone();
            m["auto"] = if kind == "merge" { json!(true) } else { json!(format!("{AUTO_HEAD}{}\n\n{}", gs_[i].1, gs(m, "prompt"))) };
        }
    }
    Ok(out)
}

/// The card's Merge: greenlight's own merge cmd for this workspace's MR, pinned to the head greenlight saw, so a push since
/// can't land unseen. The forge still checks everything else; its refusal comes back verbatim.
fn tree_merge(path: &str) -> Res<Value> {
    let f = STATE.join("last.json");
    let mut r = json!({});
    for x in py::it(ix(&load(&f)?, "rows")?)? {
        if ix(&x, "path")? == path {
            r = x; // dict: the last row for a path wins
        }
    }
    let no = || Ok(json!({"error": "greenlight doesn't say this can merge now"}));
    let m = if truthy(&r) { moves(&r, &NULL)?.into_iter().find(|x| x["kind"] == "merge") } else { None };
    let Some(m) = m else { return no() };
    if !truthy(g(ix(&r, "mr")?, "sha")) {
        return no();
    }
    let mut cmd: Vec<String> = py::it(ix(ix(&r, "next")?, "cmd")?)?.iter().map(str_).collect();
    let gh = cmd.first().map(String::as_str) == Some("gh");
    cmd.extend([if gh { "--match-head-commit" } else { "--sha" }.to_string(), str_(&r["mr"]["sha"])]);
    base::run(&cmd, 90.0, 1)?;
    // ponytail: mark it merged in the snapshot so the card stops offering it; greenlight's next scan rewrites the row anyway
    r["mr"]["state"] = json!("merged");
    r["next"] = json!({"step": "done", "owner": "done", "action": "merged from Greenlight"});
    let mut snap = load(&f)?;
    let rows = py::it(ix(&snap, "rows")?)?.into_iter().map(|x| Ok(if ix(&x, "path")? == path { r.clone() } else { x })).collect::<Res<Vec<_>>>()?;
    snap["rows"] = Value::Array(rows);
    write(&f, &py::dumps(&snap))?;
    Ok(json!({"ok": true, "ref": g(&r["mr"], "ref"), "target": g(&r["mr"], "target_branch"), "how": m["how"]}))
}

/// (state, why) from greenlight's next step plus the live agent and what I'm blocking.
fn state_of(r: &Value, blk: &Value) -> Res<(&'static str, Value)> {
    let (nxt, ag) = (ix(r, "next")?, ix(r, "agent")?);
    let ast = ix(ag, "state")?;
    if ast == "waiting" {
        return Ok(("needs", json!("agent is waiting on your answer")));
    }
    if ix(nxt, "step")? == "error" || gs(nxt, "action").contains("logged out") {
        return Ok(("crashed", gd(nxt, "action", json!(""))));
    }
    let b = block_of(r, blk);
    if truthy(&b) {
        return Ok(("needs", json!(format!("You're blocking: {}", str_(ix(&b, "why")?)))));
    }
    let owner = ix(nxt, "owner")?;
    if ast == "working" || owner == "working" || owner == "ci" {
        return Ok(("working", json!(if ast == "working" { "agent working" } else { "CI running" })));
    }
    Ok(match owner.as_str() {
        Some("me") => ("raised", json!("your move")),
        Some("greenlight" | "agent") => ("queued", json!("greenlight dispatches next")),
        Some("done") => ("done", json!("goal reached")),
        _ => ("quiet", json!(format!("waiting on {}", str_(owner)))),
    })
}

fn progress(r: &Value) -> Res<(Value, Vec<String>, Value)> {
    let steps = if truthy(g(r, "project")) { greenlight::lifecycle(&str_(&r["project"]))?.1 } else { vec![] };
    let nxt = ix(r, "next")?;
    let step = ix(nxt, "step")?;
    if ix(nxt, "owner")? == "done" || step == "done" {
        let n = steps.len();
        return Ok((json!(1.0), steps, json!(n)));
    }
    if let Some(i) = step.as_str().and_then(|x| steps.iter().position(|s| s == x)) {
        let n = steps.len();
        return Ok((json!(i as f64 / n as f64), steps, json!(i)));
    }
    Ok((Value::Null, steps, Value::Null))
}

/// {worktreeId: (worktree, its newest agent or {})} in Orca's order.
type Live = Vec<(Value, Value, Value)>;
fn live_agents() -> Res<Live> {
    let mut out: Live = vec![];
    for w in py::it(ix(&orca_s(&["worktree", "ps", "--limit", "10000"])?, "worktrees")?)? {
        let ags = py::it(&gd(&w, "agents", json!([])))?;
        // sorted(reverse=True)[0]: the first of the newest
        let mut ag: Option<&Value> = None;
        for a in &ags {
            if ag.is_none_or(|b| py::cmp_key(g(a, "updatedAt"), g(b, "updatedAt")) == Ordering::Greater) {
                ag = Some(a);
            }
        }
        let ag = ag.cloned().unwrap_or_else(|| json!({}));
        let wid = ix(&w, "worktreeId")?.clone();
        match out.iter_mut().find(|x| x.0 == wid) {
            Some(x) => (x.1, x.2) = (w, ag),
            None => out.push((wid, w, ag)),
        }
    }
    Ok(out)
}
/// The live worktree whose displayName is `name`.
fn by_name<'a>(live: &'a Live, name: &str) -> Res<Option<&'a (Value, Value, Value)>> {
    for x in live {
        if ix(&x.1, "displayName")? == name {
            return Ok(Some(x));
        }
    }
    Ok(None)
}

/// x[:n] (or x[-n:] with from_end) on a str or list. Anything else used to crash with Python's slice internals.
fn cut(v: &Value, n: usize, from_end: bool, what: &str) -> Res<Value> {
    match v {
        Value::String(x) => Ok(json!(if from_end { py::tail(x, n) } else { py::head(x, n) })),
        Value::Array(a) => Ok(Value::Array(if from_end { a[a.len().saturating_sub(n)..].to_vec() } else { a[..n.min(a.len())].to_vec() })),
        _ => py::raise("TypeError", format!("{what} is a {}, not text", py::tname(v))),
    }
}
fn obj_mut<'a>(v: &'a mut Value, what: &str) -> Res<&'a mut Obj> {
    let t = py::tname(v);
    match v.as_object_mut() {
        Some(o) => Ok(o),
        None => py::raise("TypeError", format!("{what} is a {t}, not an object")),
    }
}
fn num(v: &Value) -> Res<f64> {
    v.as_f64().map_or_else(|| py::raise("TypeError", format!("unsupported operand type(s) for -: 'float' and '{}'", py::tname(v))), Ok)
}

fn snapshot(full: bool) -> Res<Value> {
    // full: the agent's whole last message (pages); the 5s stream keeps the tail
    let snap = load(&STATE.join("last.json"))?;
    let live = live_agents()?;
    let now = py::time() * 1000.0;
    let find = |id: &Value| live.iter().find(|x| &x.0 == id);
    let mut rows = vec![];
    for r in py::it(ix(&snap, "rows")?)? {
        if find(ix(&r, "id")?).is_some() {
            rows.push(r); // drop workspaces deleted since the last scan
        }
    }
    let known: Vec<Value> = rows.iter().map(|r| r["id"].clone()).collect();
    for (wid, w, _) in &live {
        // workspaces on neither GitLab nor GitHub, with an agent or a goal (greenlight.py skips them)
        if known.contains(wid) || truthy(g(w, "isMainWorktree")) || truthy(g(w, "isArchived")) || !truthy(g(w, "branch"))
            || !(truthy(g(w, "agents")) || truthy(g(w, "comment")))
        {
            continue;
        }
        rows.push(json!({"id": wid, "name": ix(w, "displayName")?, "path": ix(w, "path")?, "project": or(g(w, "repo"), &EMPTY_STR),
                         "goal": py::strip(gs(w, "comment")), "mr": null,
                         "next": {"step": "freeform", "owner": "parked", "action": "no lifecycle: goal only"},
                         "agent": {"state": "none", "type": null, "last": ""}}));
    }
    let blk = blocking::cached(blocking::MAX_AGE)?.unwrap_or(Value::Null);
    let mut pucks = vec![];
    for mut r in rows {
        let (_, w, ag) = find(&r["id"]).unwrap();
        let cur = ix(&r, "agent")?.clone();
        if truthy(ag) {
            let state = gd(ag, "state", ix(&cur, "state")?.clone());
            let ty = if truthy(g(ag, "agentType")) { ag["agentType"].clone() } else { ix(&cur, "type")?.clone() };
            let last = if truthy(g(ag, "lastAssistantMessage")) { &ag["lastAssistantMessage"] } else { ix(&cur, "last")? };
            let last = cut(last, if full { 20000 } else { 400 }, true, "the agent's last message")?;
            let task = cut(or(g(ag, "taskTitle"), or(g(ag, "prompt"), &EMPTY_STR)), 160, false, "orca worktree ps: the agent's taskTitle")?;
            let tool = or(g(ag, "toolName"), &EMPTY_STR).clone();
            let input = cut(or(g(ag, "toolInput"), &EMPTY_STR), 160, false, "orca worktree ps: the agent's toolInput")?;
            let o = obj_mut(r.get_mut("agent").unwrap(), "last.json: a row's agent")?;
            for (k, v) in [("state", state), ("type", ty), ("last", last), ("task", task), ("tool", tool), ("input", input), ("since", g(ag, "stateStartedAt").clone())] {
                o.insert(k.into(), v);
            }
        }
        let lo = g(w, "lastOutputAt");
        let idle = if truthy(lo) { json!(py::round((now - num(lo)?) / 3.6e6, 1)) } else { lo.clone() };
        obj_mut(r.get_mut("agent").unwrap(), "last.json: a row's agent")?.insert("idle_h".into(), idle);
        let (state, why) = state_of(&r, &blk)?;
        let (prog, steps, idx) = progress(&r)?;
        let mr = g(&r, "mr");
        let name = str_(ix(&r, "name")?);
        let page = cached_page(&name);
        let mrp = if truthy(mr) {
            let ks = ["iid", "ref", "forge", "web_url", "title", "role", "draft", "author", "state", "pipeline", "conflicts", "threads", "approvals",
                      "approvals_required", "mergeable", "source_branch", "target_branch", "labels", "milestone", "approved_by", "pipeline_url", "threads_total"];
            Value::Object(ks.iter().map(|k| (k.to_string(), g(mr, k).clone())).collect())
        } else {
            json!({})
        };
        let mut p = json!({
            "name": r["name"], "path": ix(&r, "path")?, "project": ix(&r, "project")?, "goal": ix(&r, "goal")?, "state": state, "why": why,
            "progress": prog, "steps": steps, "step_index": idx, "step": r["next"]["step"],
            // the live goal, not last.json's: a park from the dock shows up before the next scan
            "owner": ix(&r["next"], "owner")?,
            "hidden": PARKED_RE.is_match(gs(w, "comment")) && !matches!(g(mr, "state").as_str(), Some("merged" | "closed")),
            "action": gd(&r["next"], "action", json!("")), "changed": g(&r, "changed"),
            "agent": r["agent"], "mr": mrp,
            "branch": gs(w, "branch").strip_prefix("refs/heads/").unwrap_or(gs(w, "branch")),
            "puck": g(g(&page, "layout"), "puck"),
            "blocking": g(&block_of(&r, &blk), "reason"), // the reason I'm blocking its MR, if I am
        });
        p["moves"] = json!(moves(&r, &blk)?); // the hex card's smart button, the same as the Basement card's
        let sg_ = sig(&p)?;
        p["sig"] = json!(sg_);
        // its page layout is current; else the app queues a build
        p["built"] = json!(g(&page, "sig").as_str() == Some(&sg_) && g(&page, "v").as_f64() == Some(PAGE_V as f64));
        // the hover card's button, written ahead (the app runs `suggest` for a puck whose state moved); None until then
        let sg = cached_suggest(&name);
        p["suggestion"] = if g(&sg, "sig").as_str() == Some(&sg_) && sg.get("label").is_some() && truthy(g(&sg, "prompt")) {
            json!({"label": sg["label"], "prompt": sg["prompt"]})
        } else {
            Value::Null
        };
        pucks.push((STATES.iter().position(|x| *x == state).unwrap(), name, p));
    }
    pucks.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    let pucks: Vec<Value> = pucks.into_iter().map(|x| x.2).collect();
    // every other worktree (default branches first): the console's picker reaches all of them, the map stays goals-only
    let have: Vec<&Value> = pucks.iter().map(|p| &p["path"]).collect();
    let mut trees = vec![];
    for (_, w, ag) in &live {
        if have.contains(&ix(w, "path")?) || truthy(g(w, "isArchived")) {
            continue;
        }
        let main = truthy(g(w, "isMainWorktree"));
        trees.push((main, str_(or(g(w, "repo"), &EMPTY_STR)), str_(ix(w, "displayName")?),
                    json!({"name": w["displayName"], "path": w["path"], "project": or(g(w, "repo"), &EMPTY_STR), "main": main,
                           "branch": gs(w, "branch").strip_prefix("refs/heads/").unwrap_or(gs(w, "branch")),
                           "agent": or(g(ag, "state"), &json!("none"))})));
    }
    trees.sort_by(|a, b| (!a.0, &a.1, &a.2).cmp(&(!b.0, &b.1, &b.2)));
    let trees: Vec<Value> = trees.into_iter().map(|x| x.3).collect();
    Ok(json!({"ts": py::time() as i64, "scanned": ix(&snap, "ts")?, "pucks": pucks, "trees": trees}))
}

const ADES: [&str; 4] = ["orca", "cmux", "herdr", "t3"];

/// One ADE's CLI: Orca's own, else the ade_<name> adapter.
fn call(ade: &str, args: &[&str]) -> Res<Value> {
    let a = s(args);
    match ade {
        "orca" => base::orca_cli(&a, 120.0),
        "cmux" => crate::ade_cmux::call(&a),
        "herdr" => crate::ade_herdr::call(&a),
        _ => crate::ade_t3::call(&a),
    }
}
fn ps(ade: &str) -> Res<Vec<Value>> {
    py::it(ix(&call(ade, &["worktree", "ps", "--limit", "10000"])?, "worktrees")?)
}

/// Every installed ADE's worktrees merged, first ADE wins a shared path. An ADE whose CLI is missing or
/// app isn't running raises, and is skipped.
fn trees() -> Res<Vec<Value>> {
    let lastf = STATE.join("last.json");
    // greenlight's last scan: the MR, CI, threads and "needs you" for the workspaces it tracks (no API calls here)
    let read_scan = || -> Res<(Vec<(Value, Value)>, Value)> {
        let mut scan: Vec<(Value, Value)> = vec![];
        for r in py::it(ix(&load(&lastf)?, "rows")?)? {
            let k = ix(&r, "path")?.clone();
            match scan.iter_mut().find(|x| x.0 == k) {
                Some(x) => x.1 = r,
                None => scan.push((k, r)),
            }
        }
        Ok((scan, blocking::cached(blocking::MAX_AGE)?.unwrap_or(Value::Null)))
    };
    let (scan, blk) = match read_scan() {
        Ok(x) => x,
        Err(e) if matches!(py::kind(&*e), "OSError" | "FileNotFoundError" | "JSONDecodeError" | "ValueError" | "KeyError") => (vec![], Value::Null),
        Err(e) => return Err(e),
    };
    let (mut out, mut seen) = (vec![], vec![]);
    for ade in ADES {
        let Ok(ws) = ps(ade) else { continue };
        for w in ws {
            if truthy(g(&w, "isArchived")) || !truthy(g(&w, "path")) || seen.contains(&w["path"]) {
                continue;
            }
            seen.push(w["path"].clone());
            let path = str_(&w["path"]);
            let agents = if truthy(g(&w, "agents")) { py::it(&w["agents"])? } else { vec![json!({})] };
            let mut ag = &agents[0]; // max(): the first of the newest
            for a in &agents {
                if py::cmp_key(g(a, "updatedAt"), g(ag, "updatedAt")) == Ordering::Greater {
                    ag = a;
                }
            }
            let main = truthy(g(&w, "isMainWorktree"));
            let name = if truthy(g(&w, "displayName")) { w["displayName"].clone() } else { json!(file_name(&path)) };
            let mut row = json!({"ade": ade, "project": or(g(&w, "repo"), &EMPTY_STR), "name": name, "path": w["path"],
                                 "branch": gs(&w, "branch").strip_prefix("refs/heads/").unwrap_or(gs(&w, "branch")),
                                 "main": main, "agent": or(g(ag, "state"), &json!("none")), "goal": or(g(&w, "comment"), &EMPTY_STR),
                                 "pinned": truthy(g(&w, "isPinned")), "active": g(&w, "lastOutputAt"), "since": g(ag, "stateStartedAt"),
                                 "created": if main { Value::Null } else { created(&path) }});
            if main {
                if let Some(c) = repo_cfg(&path).filter(truthy) {
                    row["cfg"] = c;
                }
            }
            let Some(mut r) = scan.iter().find(|x| x.0 == w["path"]).map(|x| x.1.clone()).filter(truthy) else {
                out.push(row);
                continue;
            };
            row["puck"] = ix(&r, "name")?.clone(); // greenlight's name for it: `suggest` and the dashboard know it by this
            // a row the scan wrote without these used to crash with KeyError 'agent' / 'next'
            for k in ["agent", "next"] {
                if r.get(k).is_none() {
                    return py::err(format!("{}: the row for {path} has no {k}", lastf.display()));
                }
            }
            let mut a = obj_mut(&mut r["agent"].clone(), "last.json: a row's agent")?.clone();
            a.insert("state".into(), row["agent"].clone()); // the live agent, not the scan's
            r["agent"] = Value::Object(a);
            let (st, why) = state_of(&r, &blk)?;
            (row["state"], row["why"]) = (json!(st), why);
            let mv = moves(&r, &blk)?;
            if !mv.is_empty() {
                let auto = mv.iter().any(|m| m.get("auto").is_some());
                row["moves"] = json!(mv);
                if auto {
                    // auto-run: has greenlight's scan caught up with what's pushed? (the tab's "needs you")
                    // ponytail: no timeout, as in Python
                    let h = py::exec(&s(&["git", "-C", &path, "rev-parse", "@{u}"]), None, None, None, &[])?;
                    row["pushed"] = if h.rc == 0 { json!(py::strip(&h.out)) } else { Value::Null };
                }
            }
            let mr = g(&r, "mr");
            if truthy(mr) {
                let ks = ["ref", "web_url", "title", "state", "draft", "pipeline", "conflicts", "rebase", "threads", "mergeable", "by", "assignees",
                          "target_branch", "labels", "milestone", "description", "sha"];
                row["mr"] = Value::Object(ks.iter().map(|k| (k.to_string(), g(mr, k).clone())).collect());
            }
            out.push(row);
        }
    }
    let key = |x: &Value| (str_(&x["project"]).to_lowercase(), !truthy(&x["main"]), str_(&x["name"]).to_lowercase());
    out.sort_by_key(key);
    Ok(out)
}

/// Path(p).name
fn file_name(p: &str) -> String {
    Path::new(p).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}
/// st_mtime as Python computes it.
fn mtime(m: &std::fs::Metadata) -> f64 {
    use std::os::unix::fs::MetadataExt;
    m.mtime() as f64 + m.mtime_nsec() as f64 * 1e-9
}

/// ms: a linked worktree's `.git` is a one-line file `git worktree add` writes and nothing rewrites.
fn created(path: &str) -> Value {
    // ponytail: `git worktree repair/move` rewrites it too; the branch's first reflog entry if that misleads
    std::fs::metadata(Path::new(path).join(".git")).map_or(Value::Null, |m| json!((mtime(&m) * 1000.0) as i64))
}

/// (ade, worktree) for a path, re-listed now so a stale id from the page can't name another tree.
fn tree(path: &str) -> Res<(&'static str, Value)> {
    for ade in ADES {
        let Ok(ws) = ps(ade) else { continue };
        if let Some(w) = ws.into_iter().find(|w| g(w, "path") == path && !truthy(g(w, "isArchived"))) {
            return Ok((ade, w));
        }
    }
    py::raise("LookupError", format!("no workspace at {path}"))
}
/// "id:<worktreeId or id>"
fn wid(w: &Value) -> Res<String> {
    let id = g(w, "worktreeId");
    Ok(format!("id:{}", str_(if truthy(id) { id } else { ix(w, "id")? })))
}

/// The git facts the sidebar's menu shows and Delete checks: uncommitted files, commits no remote has, last commit.
fn tree_info(path: &str) -> Res<Value> {
    let git = |a: &[&str]| {
        let mut c = s(&["git", "-C", path]);
        c.extend(s(a));
        py::exec(&c, None, Some(20.0), None, &[])
    };
    let st = git(&["status", "--porcelain", "--untracked-files=normal"])?;
    if st.rc != 0 {
        let e = py::strip(&st.err);
        return Ok(json!({"error": py::head(if e.is_empty() { "not a git checkout" } else { e }, 200)}));
    }
    let last = py::strip(&git(&["log", "-1", "--format=%ct"])?.out).to_string();
    let unpushed = py::split_ws(&git(&["rev-list", "HEAD", "--not", "--remotes"])?.out).len();
    let commit = match last.parse::<i64>() {
        Ok(n) if last.bytes().all(|c| c.is_ascii_digit()) => json!(n * 1000),
        _ => Value::Null,
    };
    Ok(json!({"dirty": py::splitlines(&st.out).len(), "unpushed": unpushed, "commit": commit}))
}

/// Remove the worktree (never its branch). Refuses the main checkout always, and, unless forced, a pinned or dirty
/// tree or one with commits no remote has.
fn tree_rm(path: &str, force: bool) -> Res<Value> {
    let (ade, w) = tree(path)?;
    if truthy(g(&w, "isMainWorktree")) {
        return Ok(json!({"error": "the repo's main checkout is never removed"}));
    }
    let info = tree_info(path)?;
    let mut risks = vec![];
    if truthy(g(&w, "isPinned")) {
        risks.push("it's pinned".to_string());
    }
    if let Some(e) = info.get("error") {
        risks.push(str_(e));
    }
    let n = |k: &str| g(&info, k).as_i64().unwrap_or(0);
    let plural = |n: i64| if n != 1 { "s" } else { "" };
    if n("dirty") != 0 {
        risks.push(format!("{} uncommitted file{}", n("dirty"), plural(n("dirty"))));
    }
    if n("unpushed") != 0 {
        risks.push(format!("{} commit{} no remote has (the local branch keeps them)", n("unpushed"), plural(n("unpushed"))));
    }
    if !risks.is_empty() && !force {
        return Ok(json!({"risks": risks}));
    }
    // ponytail: only Orca honours --force; the git-backed ADEs still refuse a dirty tree, and that error reaches the page
    let id = wid(&w)?;
    let mut args = vec!["worktree", "rm", "--worktree", &id];
    if force {
        args.push("--force");
    }
    call(ade, &args)?;
    Ok(json!({"ok": true}))
}

fn tree_goal(path: &str, goal: &str) -> Res<Value> {
    let (ade, w) = tree(path)?;
    let goal = py::split_ws(goal).join(" ");
    call(ade, &["worktree", "set", "--worktree", &wid(&w)?, "--comment", py::head(&goal, 500)])?;
    Ok(json!({"ok": true}))
}

const CFG: &str = ".greenlight.json"; // in a repo's main checkout: {"name", "look", "host", "hosts"}, its project in the terminal (README)

/// The checkout's .greenlight.json; None when it's missing, over 8 KB or not a JSON object.
fn repo_cfg(path: &str) -> Option<Value> {
    let f = Path::new(path).join(CFG);
    if std::fs::metadata(&f).ok()?.len() > 8192 {
        return None;
    }
    py::loads(&std::fs::read_to_string(&f).ok()?).ok().filter(Value::is_object)
}

/// Save to repo: write the main checkout's .greenlight.json (you commit it).
fn tree_cfg(path: &str, text: &str) -> Res<Value> {
    let (_, w) = tree(path)?;
    if !truthy(g(&w, "isMainWorktree")) {
        return Ok(json!({"error": "only a repo's main checkout holds .greenlight.json"}));
    }
    let Some(o) = py::loads(text).ok().filter(Value::is_object) else { return Ok(json!({"error": "not a JSON object"})) };
    write(&Path::new(path).join(CFG), &(py::dumps_indent_n(&o, 2) + "\n"))?;
    Ok(json!({"ok": true, "cfg": o}))
}

fn safe(name: &str) -> String {
    SAFE_RE.replace_all(name, "_").into_owned()
}

// ---- files, as pathlib does them (errors read as Python's OSError text) ----

fn read(p: &Path) -> Res<String> {
    std::fs::read_to_string(p).or_else(|e| py::oserr(e, p))
}
/// open(errors="replace").read()
fn read_lossy(p: &Path) -> Res<String> {
    std::fs::read(p).map(|b| String::from_utf8_lossy(&b).into_owned()).or_else(|e| py::oserr(e, p))
}
fn load(p: &Path) -> Res<Value> {
    py::loads(&read(p)?)
}
fn write(p: &Path, text: &str) -> Res<()> {
    std::fs::write(p, text).or_else(|e| py::oserr(e, p))
}
fn mkdirs(p: &Path) -> Res<()> {
    std::fs::create_dir_all(p).or_else(|e| py::oserr(e, p))
}
fn append(p: &Path, text: &str) -> Res<()> {
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(p).or_else(|e| py::oserr(e, p))?;
    f.write_all(text.as_bytes()).or_else(|e| py::oserr(e, p))
}
/// unlink(missing_ok=True)
fn unlink(p: &Path) -> Res<()> {
    match std::fs::remove_file(p) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => py::oserr(e, p),
        _ => Ok(()),
    }
}
/// dir.glob(pattern) for one level: the entries `keep` accepts, in directory order (hidden ones too).
fn glob1(dir: &Path, keep: impl Fn(&str) -> bool) -> Vec<PathBuf> {
    let Ok(rd) = std::fs::read_dir(dir) else { return vec![] };
    rd.flatten().filter(|e| keep(&e.file_name().to_string_lossy())).map(|e| e.path()).collect()
}
fn stem(p: &Path) -> String {
    p.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Append a history line per puck whose (state, step, owner) moved since the last tick.
fn record(pucks: &[Value]) -> Res<()> {
    let d = dash();
    mkdirs(&d)?;
    let f = d.join("state.json");
    let prev = if f.exists() { load(&f)? } else { json!({}) };
    let mut cur = Obj::new();
    for p in pucks {
        let sig = json!([p["state"], p["step"], p["owner"]]);
        let name = str_(&p["name"]);
        cur.insert(name.clone(), sig.clone());
        let was = g(&prev, &name);
        if !was.is_null() && *was != sig {
            let e = json!({"ts": py::time() as i64, "from": was, "to": sig, "action": cut(&p["action"], 200, false, "a puck's action")?});
            append(&d.join(format!("{}.jsonl", safe(&name))), &(py::dumps(&e) + "\n"))?;
        }
    }
    write(&f, &py::dumps(&Value::Object(cur)))
}

fn history(name: &str) -> Res<Vec<Value>> {
    let hf = greenlight::history_file(name);
    let mut all = vec![];
    if hf.exists() {
        for line in py::splitlines(&read(&hf)?) {
            if let Some(m) = HIST_RE.captures(line) {
                all.push(json!({"ts": &m[1], "text": &m[2]}));
            }
        }
    }
    let jf = dash().join(format!("{}.jsonl", safe(name)));
    if jf.exists() {
        for line in py::splitlines(&read(&jf)?) {
            let e = py::loads(line)?;
            let ts = py::strftime_local("%Y-%m-%d %H:%M", num(ix(&e, "ts")?)?);
            let (fr, to) = (ix(&e, "from")?, ix(&e, "to")?);
            let text = format!("{} → {} ({}/{}): {}", str_(py::at(fr, 0)?), str_(py::at(to, 0)?), str_(py::at(to, 1)?), str_(py::at(to, 2)?), str_(ix(&e, "action")?));
            all.push(json!({"ts": ts, "text": text, "state": py::at(to, 0)?}));
        }
    }
    all.sort_by(|a, b| py::cmp_key(&b["ts"], &a["ts"])); // newest first, stable
    all.truncate(60);
    Ok(all)
}

fn git_facts(path: &str) -> Res<Value> {
    let Ok(o) = py::exec(&s(&["git", "-C", path, "log", "-15", "--format=@%h\t%cI\t%s", "--numstat"]), None, Some(10.0), None, &[]) else {
        return Ok(json!({}));
    };
    let (mut commits, mut files) = (vec![], Vec::<(String, i64, i64)>::new());
    let int = |x: &str| if !x.is_empty() && x.bytes().all(|c| c.is_ascii_digit()) { x.parse().unwrap_or(0) } else { 0 };
    for line in py::splitlines(&o.out) {
        if let Some(rest) = line.strip_prefix('@') {
            let p: Vec<&str> = rest.splitn(3, '\t').collect();
            if p.len() < 3 {
                return py::raise("ValueError", format!("not enough values to unpack (expected 3, got {})", p.len()));
            }
            commits.push(json!({"sha": p[0], "ts": py::head(p[1], 16).replace('T', " "), "subject": p[2]}));
        } else if line.matches('\t').count() == 2 {
            let p: Vec<&str> = line.split('\t').collect();
            let i = match files.iter().position(|f| f.0 == p[2]) {
                Some(i) => i,
                None => {
                    files.push((p[2].to_string(), 0, 0));
                    files.len() - 1
                }
            };
            files[i].1 += int(p[0]);
            files[i].2 += int(p[1]);
        }
    }
    files.sort_by_key(|f| -(f.1 + f.2));
    let files: Vec<Value> = files.iter().take(12).map(|f| json!({"path": f.0, "add": f.1, "del": f.2})).collect();
    Ok(json!({"commits": commits, "files": files}))
}

fn mr_facts(project: &str, iid: &Value, forge: &str) -> Value {
    let facts = || -> Res<Value> {
        let mr = greenlight::mr_details(project, iid, forge)?;
        let job = |c: &Value| Ok(json!({"name": ix(c, "name")?, "stage": ix(c, "stage")?, "status": ix(c, "status")?}));
        // GitHub: the head commit's checks
        let mut jobs = py::it(&gd(&mr, "_checks", json!([])))?.iter().map(job).collect::<Res<Vec<_>>>()?;
        let pl = g(&mr, "head_pipeline");
        if truthy(pl) && truthy(g(pl, "id")) {
            let path = format!("projects/{}/pipelines/{}/jobs?scope[]=failed&scope[]=success&scope[]=running&scope[]=pending", base::pid(project), str_(&pl["id"]));
            jobs = base::gl_all(&path)?.iter().map(job).collect::<Res<Vec<_>>>()?;
        }
        jobs.truncate(60);
        Ok(json!({"pipeline": g(pl, "status"), "pipeline_url": g(pl, "web_url"), "jobs": jobs, "unresolved": greenlight::unresolved(&mr)?.len(),
                  "approved_by": gd(&mr, "_approved_by", json!([])), "conflicts": g(&mr, "has_conflicts"), "milestone": g(g(&mr, "milestone"), "title"),
                  "updated": g(&mr, "updated_at"), "labels": gd(&mr, "labels", json!([]))}))
    };
    facts().unwrap_or_else(|e| json!({"error": py::head(&e.to_string(), 200)}))
}

fn facts(name: &str) -> Res<Value> {
    let snap = snapshot(true)?;
    let Some(p) = py::arr(&snap["pucks"]).iter().find(|p| p["name"] == name).cloned() else {
        py::sys_exit(&py::dumps(&json!({"error": format!("no workspace {name}")})))
    };
    let mut f = json!({"puck": p, "history": history(name)?, "git": git_facts(&str_(&p["path"]))?});
    let mr = &p["mr"];
    if truthy(mr) && truthy(g(mr, "iid")) && g(mr, "state") == "opened" {
        let forge = if truthy(g(mr, "forge")) { str_(&mr["forge"]) } else { "gitlab".into() };
        f["mr_live"] = mr_facts(&str_(&p["project"]), &mr["iid"], &forge);
    }
    Ok(f)
}

fn design(f: &Value, model: &str, effort: &str) -> Res<Value> {
    let pick = |ks: &[&str]| {
        ks.iter().map(|k| {
            let (_, props, desc) = catalog(k).unwrap();
            format!("- {k} {props}: {}", desc.strip_prefix("P ").unwrap_or(desc))
        }).collect::<Vec<_>>().join("\n")
    };
    let pucks: Vec<&str> = CATALOG.iter().filter(|c| c.2.starts_with("P ")).map(|c| c.0).collect();
    let prompt = [
        "You pick the Insights for one workspace's page in a developer's Mac app. The page already shows, as fixed parts: ",
        "the MR title and goal, the current step and next action, all lifecycle steps, the agent and its last message, ",
        "pipeline status, conflicts, unresolved threads, approvals, milestone, labels, links, history and commits. ",
        "NEVER repeat any of those. Insights add what they don't say: e.g. CI jobs as a grid, where the diff is as a stack, ",
        "commit cadence as a spark, one non-obvious finding as a note. Tone is one of ", TONES, ".\n",
        "Tile widgets:\n", &pick(&TILES), "\nWide panel widgets:\n", &pick(&WIDE), "\n",
        "Puck widgets (fit inside a small hex on the overview):\n", &pick(&pucks), "\n\n",
        r#"Return ONLY JSON, no prose: {"puck": {"widget": w, "props": {...}}, "#,
        r#""tiles": [{"widget": w, "title": 2-4 words, "props": {...}}], "wide": {"widget": w, "title": 2-4 words, "props": {...}} or null}."#, "\n",
        "0 to 3 tiles, most useful first. Fewer is better than filler. Build every value from the facts; never invent data.\n\nFacts:\n",
        py::head(&py::dumps(f), 30000),
    ].concat();
    let reply = ask(&prompt, model, effort)?;
    let layout = match OBJ_RE.find(&reply) {
        // a reply that isn't JSON used to fail with the decoder's bare message
        Some(m) => py::loads(m.as_str()).or_else(|e| py::raise("ValueError", format!("the designer's reply isn't JSON ({e}): {}", py::head(m.as_str(), 80))))?,
        None => json!({}),
    };
    validate(&layout)
}

/// One headless answer from config llm_harness. The model names here are Claude's, so another harness uses its default.
fn ask(prompt: &str, model: &str, effort: &str) -> Res<String> {
    let h = str_(base::cfg("llm_harness"));
    let (cmd, text) = harness::argv(&h, prompt, (h == "claude").then_some(model), Some(effort), None, None, false)?;
    Ok(py::exec(&cmd, Some(&text), Some(180.0), None, &[])?.out)
}

/// The console drawer's ghost prompt: greenlight's next step, written as a task for an agent in that workspace.
fn suggest(name: &str, model: &str, effort: &str) -> Res<Value> {
    let snap = snapshot(false)?;
    let Some(p) = py::arr(&snap["pucks"]).iter().find(|p| p["name"] == name) else {
        return Ok(json!({"name": name, "error": format!("no workspace {name}")}));
    };
    let (cache, sg) = (dash().join("suggest").join(format!("{}.json", safe(name))), p["sig"].clone());
    let cached = cached_suggest(name);
    if *g(&cached, "sig") == sg && cached.get("label").is_some() {
        return Ok(cached); // older cached suggestions have no button label
    }
    let rules: String = py::it(base::cfg("suggest_rules"))?.iter().map(|r| format!("{}. ", str_(r).trim_end_matches('.'))).collect();
    let prompt = [
        "You are ", &str_(base::cfg("name")), ", a developer's agent manager. Write the ONE prompt the developer should send to a fresh coding agent ",
        "working in this workspace so it does the next thing toward the workspace goal. Ground it on greenlight's next step ",
        "(puck.step, puck.owner, puck.action) and the lifecycle; be concrete (MR number, failing job, file, thread) using only the facts. ",
        "If the next move is the developer's own (a review, an approval, an answer), the prompt prepares it for them ",
        "(e.g. summarize the diff and open threads, draft a reply) and must not approve, comment or push. ",
        "Rules: don't suggest merging (greenlight merges). ", &rules,
        "Reply with a 1 to 4 word button label for the step on the first line (imperative, e.g. Fix failing lint job), ",
        "then the prompt on the next line: 1 to 3 imperative sentences, no quotes, no preamble.\n\nFacts:\n",
        py::head(&py::dumps(&facts(name)?), 30000),
    ].concat();
    let (label, text) = split_label(&ask(&prompt, model, effort)?);
    let out = json!({"name": name, "sig": sg, "prompt": py::head(&text, 1000), "label": label});
    if !text.is_empty() {
        mkdirs(cache.parent().unwrap())?;
        write(&cache, &py::dumps(&out))?;
    }
    Ok(out)
}

fn cached_suggest(name: &str) -> Value {
    load(&dash().join("suggest").join(format!("{}.json", safe(name)))).unwrap_or_else(|_| json!({}))
}

/// suggest's reply: a 1-4 word label line, then the prompt. A one-line reply is all prompt, no label.
fn split_label(text: &str) -> (String, String) {
    let t = py::strip(text);
    let (head, rest) = t.split_once('\n').unwrap_or((t, ""));
    if py::strip(rest).is_empty() {
        return (String::new(), py::strip(head).trim_matches('"').to_string());
    }
    let label = py::split_ws(py::strip(head).trim_matches(|c| c == '"' || c == '*')).into_iter().take(4).collect::<Vec<_>>().join(" ");
    (label.trim_end_matches('.').to_string(), py::strip(rest).trim_matches('"').to_string())
}

fn park(name: &str, on: bool) -> Res<Value> {
    let live = live_agents()?;
    let Some((_, w, _)) = by_name(&live, name)? else { return Ok(json!({"error": format!("no workspace {name}")})) };
    let goal = py::split_ws(&PARKED_RE.replace_all(gs(w, "comment"), "")).join(" ");
    let goal = if on { py::strip(&format!("{goal} (parked)")).to_string() } else { goal };
    orca_s(&["worktree", "set", "--worktree", &format!("id:{}", str_(ix(w, "worktreeId")?)), "--comment", &goal])?;
    greenlight::note(name, &format!("{} from the dashboard dock", if on { "parked" } else { "unparked" }))?;
    Ok(json!({"ok": true, "goal": goal}))
}

/// Launch an agent in the workspace with `prompt`. Refuses while its agent is working or waiting on an answer.
fn run(name: &str, prompt: &str) -> Res<Value> {
    let live = live_agents()?;
    let Some((_, w, ag)) = by_name(&live, name)? else { return Ok(json!({"error": format!("no workspace {name}")})) };
    if g(ag, "state") == "working" {
        return Ok(json!({"error": "its agent is still working; wait for it to finish"}));
    }
    if g(ag, "state") == "waiting" {
        return Ok(json!({"error": "its agent is waiting on an answer; answer it in its workspace or the chat"}));
    }
    let wid = ix(w, "worktreeId")?;
    let mut row = json!({});
    for r in py::it(ix(&load(&STATE.join("last.json"))?, "rows")?)? {
        if ix(&r, "id")? == wid {
            row = r;
            break;
        }
    }
    let agent = g(g(&row, "next"), "agent");
    let agent = if truthy(agent) { agent.clone() } else { py::at(base::cfg("agents"), 0)?.clone() };
    // ponytail: always a new agent tab; reusing the idle one needs greenlight's empty-input-box check first
    let mut order: Vec<Value> = vec![];
    for a in std::iter::once(agent).chain(py::it(base::cfg("agents"))?) {
        if !order.contains(&a) {
            order.push(a);
        }
    }
    let mut used = String::new();
    for (i, a) in order.iter().enumerate() {
        used = str_(a);
        match orca_s(&["agent", "launch", "--agent", &used, "--worktree", &format!("id:{}", str_(wid)), "--prompt", prompt]) {
            Ok(_) => break,
            Err(e) if py::kind(&*e) == "RuntimeError" => {
                if i == order.len() - 1 || !LIMIT_RE.is_match(&e.to_string()) {
                    return Ok(json!({"error": py::head(&e.to_string(), 300)}));
                }
            }
            Err(e) => return Err(e),
        }
    }
    let mr = g(&row, "mr");
    let mr = if truthy(mr) { format!("{} ", if truthy(g(mr, "ref")) { str_(&mr["ref"]) } else { format!("!{}", str_(ix(mr, "iid")?)) }) } else { String::new() };
    greenlight::note(name, &format!("{mr}console → launched {used}: {}", py::head(&py::split_ws(prompt).join(" "), 300)))?;
    Ok(json!({"ok": true, "agent": used}))
}

/// The workspace's waiting agent and its terminal.
struct Term {
    w: Value,
    ag: Value,
    h: String,
    rd: Value,
}
/// (worktree, agent, terminal handle, terminal read) for the workspace's waiting agent, or an error string.
fn waiting_term(name: &str) -> Res<Result<Term, String>> {
    let live = live_agents()?;
    let Some((_, w, ag)) = by_name(&live, name)? else { return Ok(Err(format!("no workspace {name}"))) };
    if g(ag, "state") != "waiting" {
        return Ok(Err("its agent isn't waiting on you any more".into()));
    }
    // ponytail: newest agent terminal in the tree; match on agent identity if a tree ever runs two at once
    let wid = ix(w, "worktreeId")?;
    let mut newest: Option<Value> = None; // sorted(...)[-1]: the last of the newest
    for t in py::it(ix(&orca_s(&["terminal", "list", "--limit", "10000"])?, "terminals")?)? {
        if ix(&t, "worktreeId")? == wid && truthy(g(&t, "agentIdentity"))
            && newest.as_ref().is_none_or(|b| py::cmp_key(g(&t, "lastOutputAt"), g(b, "lastOutputAt")) != Ordering::Less)
        {
            newest = Some(t);
        }
    }
    let Some(t) = newest else { return Ok(Err("no agent terminal found there".into())) };
    let h = str_(ix(&t, "handle")?);
    let rd = ix(&orca_s(&["terminal", "read", "--terminal", &h, "--limit", "60"])?, "terminal")?.clone();
    Ok(Ok(Term { w: w.clone(), ag: ag.clone(), h, rd }))
}

/// The terminal's last 30 lines, rstripped.
fn tail_lines(tail: &Value) -> Res<Vec<String>> {
    let all: Vec<String> = py::it(tail)?.iter().map(|x| py::rstrip(&str_(x)).to_string()).collect();
    Ok(all[all.len().saturating_sub(30)..].to_vec())
}

/// A Claude question menu on screen: [(digit, label)], or [] when the agent waits at its normal prompt.
fn menu(tail: &Value) -> Res<Vec<(String, String)>> {
    let lines = tail_lines(tail)?;
    if lines.iter().any(|x| py::strip(x) == "❯") {
        return Ok(vec![]); // the empty input box: a plain end-of-turn question
    }
    let opts: Vec<_> = lines.iter().filter_map(|x| MENU_RE.captures(x).map(|m| (m[1].to_string(), py::strip(&m[2]).to_string()))).collect();
    Ok(if opts.len() >= 2 { opts } else { vec![] })
}

/// The on-screen question above a menu and each option's description: (question, {digit: detail}).
fn picker(tail: &Value) -> Res<(String, Obj)> {
    let (mut q, mut details, mut cur): (Vec<String>, Obj, Option<String>) = (vec![], Obj::new(), None);
    for x in tail_lines(tail)? {
        let ls = py::lstrip(&x);
        if ls.starts_with('☐') {
            (q, details, cur) = (vec![], Obj::new(), None);
        } else if let Some(m) = MENU_RE.captures(&x) {
            cur = Some(m[1].to_string());
        } else if x.starts_with('│') && cur.is_none() {
            q.push(py::strip(&x['│'.len_utf8()..]).to_string());
        } else if cur.is_some() && x.chars().next().is_some_and(py::is_space) && !py::strip(&x).is_empty() {
            let c = cur.clone().unwrap();
            let d = format!("{} {}", details.get(&c).and_then(Value::as_str).unwrap_or(""), py::strip(&x));
            details.insert(c, json!(py::strip(&d)));
        } else if (py::strip(&x).is_empty() || ls.starts_with('─') || ls.starts_with("Enter to select")) && ls.starts_with('─') {
            cur = None;
        }
    }
    Ok((q.join(" "), details))
}

fn ask_user(name: &str, model: &str, effort: &str) -> Res<Value> {
    let t = match waiting_term(name)? {
        Ok(t) => t,
        Err(e) => return Ok(json!({"name": name, "error": e})),
    };
    let q = py::strip(gs(&t.ag, "lastAssistantMessage")).to_string();
    let tail = or(g(&t.rd, "tail"), &EMPTY_ARR);
    let opts = menu(tail)?;
    if !opts.is_empty() {
        // "Type something." needs a second, typed step in the picker; that one is answered in the workspace
        let (pq, det) = picker(tail)?;
        let options: Vec<Value> = opts.iter().filter(|(_, l)| l != "Type something.").map(|(k, l)| {
            json!({"key": k, "label": py::head(l, 200), "detail": py::head(det.get(k).and_then(Value::as_str).unwrap_or(""), 400)})
        }).collect();
        return Ok(json!({"name": name, "question": py::tail(if pq.is_empty() { &q } else { &pq }, 4000), "kind": "menu",
                         "questions": pending_questions(gs(&t.w, "path"))?, "options": options}));
    }
    let cache = dash().join("ask").join(format!("{}.json", safe(name)));
    let key = sha1_hex(q.as_bytes())[..12].to_string();
    if let Ok(c) = load(&cache) {
        if g(&c, "key") == key.as_str() {
            return Ok(ix(&c, "out")?.clone());
        }
    }
    let mut out = json!({"name": name, "question": py::tail(&q, 4000), "kind": "prompt", "options": []});
    let mut suggest = || -> Res<()> {
        let raw = ask(&format!("A coding agent ended its turn with this message to the developer. If it asks the developer to choose or confirm \
something, list up to 3 short replies (each under 12 words, in the developer's voice) that answer it. \
If it asks nothing answerable that way, return []. Return ONLY a JSON array of strings.\n\n{}", py::tail(&q, 6000)), model, effort)?;
        let opts = match LIST_RE.find(&raw) {
            Some(m) => py::it(&py::loads(m.as_str())?)?,
            None => vec![],
        };
        let opts: Vec<Value> = opts.iter().map(str_).filter(|o| !py::strip(o).is_empty()).take(3).map(|o| json!({"key": "", "label": py::head(&o, 200)})).collect();
        out["options"] = json!(opts);
        mkdirs(cache.parent().unwrap())?;
        write(&cache, &py::dumps(&json!({"key": key, "out": out})))
    };
    let _ = suggest(); // no suggestions is fine: the free-text reply still works
    Ok(out)
}

/// The questions of an AskUserQuestion call still waiting on its result in the newest Claude transcript, else None.
fn pending_questions(path: &str) -> Res<Value> {
    let fs: Vec<PathBuf> = if path.is_empty() { vec![] } else { transcript_files(path)?.into_iter().filter(|x| x.1 == "claude").map(|x| x.0).collect() };
    let Some(f) = fs.first() else { return Ok(Value::Null) };
    let mut pend = Value::Null;
    // ponytail: rescans the whole transcript per ask; tail-read if screenshot-heavy ones get slow
    for line in read_lossy(f)?.lines() {
        let Ok(d) = py::loads(line) else { continue };
        for b in py::arr(g(g(&d, "message"), "content")) {
            if !b.is_object() {
                continue;
            }
            if g(b, "type") == "tool_use" && g(b, "name") == "AskUserQuestion" {
                pend = b.clone();
            } else if g(b, "type") == "tool_result" && truthy(&pend) && g(b, "tool_use_id") == g(&pend, "id") {
                pend = Value::Null;
            }
        }
    }
    let qs = g(g(&pend, "input"), "questions");
    Ok(if py::arr(qs).is_empty() { Value::Null } else { qs.clone() })
}

/// Answer the waiting agent. Never types over text the user left unsent in that terminal.
fn answer(name: &str, text: &str) -> Res<Value> {
    let t = match waiting_term(name)? {
        Ok(t) => t,
        Err(e) => return Ok(json!({"error": e})),
    };
    if !py::strip(gs(&t.rd, "draft")).is_empty() {
        return Ok(json!({"error": "there's unsent text in that agent's input box; answer it in its workspace"}));
    }
    let opts = menu(or(g(&t.rd, "tail"), &EMPTY_ARR))?;
    let text = py::split_ws(text).join(" "); // one line: a newline would submit half the answer
    let h = t.h.as_str();
    if !opts.is_empty() && opts.iter().any(|(k, _)| *k == text) {
        orca_s(&["terminal", "send", "--terminal", h, "--text", &text])?;
    } else {
        if !opts.is_empty() {
            // Esc closes the picker, then the answer goes in as a plain message
            orca_s(&["terminal", "send", "--terminal", h, "--text", "\x1b"])?;
            let mut closed = false;
            for _ in 0..20 {
                // ponytail: polls ~8s for the picker to close
                std::thread::sleep(std::time::Duration::from_millis(400));
                let rd = ix(&orca_s(&["terminal", "read", "--terminal", h, "--limit", "60"])?, "terminal")?.clone();
                if menu(or(g(&rd, "tail"), &EMPTY_ARR))?.is_empty() {
                    closed = true;
                    break;
                }
            }
            if !closed {
                return Ok(json!({"error": "the agent's menu didn't close; answer it in its workspace"}));
            }
        }
        orca_s(&["terminal", "send", "--terminal", h, "--text", &text, "--enter"])?;
    }
    greenlight::note(name, &format!("you answered the agent: {}", py::head(&py::split_ws(&text).join(" "), 300)))?;
    Ok(json!({"ok": true}))
}

/// Keep only allowed widgets with dict props, at most 3 tiles and 1 wide panel. The page never runs model output.
fn validate(layout: &Value) -> Res<Value> {
    let ok = |w: &Value, allowed: &[&str]| w.is_object() && g(w, "widget").as_str().is_some_and(|x| allowed.contains(&x)) && g(w, "props").is_object();
    let card = |w: &Value| json!({"widget": w["widget"], "title": py::head(&str_(or(g(w, "title"), &EMPTY_STR)), 40), "props": w["props"]});
    let tiles: Vec<Value> = list(g(layout, "tiles"))?.iter().filter(|t| ok(t, &TILES)).take(3).map(card).collect();
    let (wide, puck) = (g(layout, "wide"), g(layout, "puck"));
    let all: Vec<&str> = CATALOG.iter().map(|c| c.0).collect();
    let puck = if ok(puck, &all) && catalog(gs(puck, "widget")).unwrap().2.starts_with("P ") {
        json!({"widget": puck["widget"], "props": puck["props"]})
    } else {
        Value::Null
    };
    Ok(json!({"puck": puck, "tiles": tiles, "wide": if ok(wide, &WIDE) { card(wide) } else { Value::Null }}))
}

fn sig(p: &Value) -> Res<String> {
    let v = json!([ix(p, "state")?, ix(p, "step")?, ix(p, "owner")?, ix(ix(p, "agent")?, "state")?, g(ix(p, "mr")?, "state")]);
    Ok(sha1_hex(py::dumps(&v).as_bytes())[..12].to_string())
}

fn cached_page(name: &str) -> Value {
    load(&dash().join("pages").join(format!("{}.json", safe(name)))).unwrap_or_else(|_| json!({}))
}

fn page(name: &str, fresh: bool, model: &str, effort: &str) -> Res<Value> {
    let f = facts(name)?;
    let cache = dash().join("pages").join(format!("{}.json", safe(name)));
    // a corrupt cache used to crash with the decoder's bare message
    let mut cached = if cache.exists() {
        py::loads(&read(&cache)?).or_else(|e| py::raise("ValueError", format!("{}: not JSON ({e})", cache.display())))?
    } else {
        json!({})
    };
    let sg = sig(&f["puck"])?;
    if fresh || g(&cached, "sig").as_str() != Some(&sg) || g(&cached, "v").as_f64() != Some(PAGE_V as f64) {
        let designed = py::time() as i64;
        let res = design(&f, model, effort).and_then(|layout| {
            cached = json!({"v": PAGE_V, "sig": sg, "designed": designed, "layout": layout});
            mkdirs(cache.parent().unwrap())?;
            write(&cache, &py::dumps(&cached))
        });
        if let Err(e) = res {
            // designer down / out of tokens: keep the old layout, say why
            obj_mut(&mut cached, &format!("{}", cache.display()))?.insert("error".into(), json!(format!("designer failed: {}", py::head(&e.to_string(), 200))));
        }
    }
    let mut out = json!({"name": name, "facts": f});
    merge(&mut out, cached)?;
    Ok(out)
}

/// {**a, **b}: b's keys over a's, a's order kept.
fn merge(a: &mut Value, b: Value) -> Res<()> {
    let t = py::tname(&b);
    let Value::Object(b) = b else { return py::raise("TypeError", format!("'{t}' object is not a mapping")) };
    a.as_object_mut().unwrap().extend(b);
    Ok(())
}

fn claude_entries(f: &Path) -> Res<Vec<(String, String)>> {
    let mut out = vec![];
    for line in read_lossy(f)?.lines() {
        let Ok(d) = py::loads(line) else { continue };
        let ty = g(&d, "type");
        if !(ty == "user" || ty == "assistant") || truthy(g(&d, "isSidechain")) || truthy(g(&d, "isMeta")) {
            continue;
        }
        let ct = g(g(&d, "message"), "content");
        let blocks = if ct.is_string() { vec![json!({"type": "text", "text": ct})] } else { list(ct)? };
        for b in blocks {
            if g(&b, "type") == "text" {
                let mut t = str_(or(g(&b, "text"), &EMPTY_STR));
                if let Some(c) = CMD_RE.captures(&t) {
                    let args = ARGS_RE.captures(&t).map_or(" ".to_string(), |a| a[1].to_string());
                    t = format!("{} {args}", &c[1]);
                } else if py::lstrip(&t).starts_with('<') {
                    continue; // caveats, reminders, local command output
                }
                out.push((if ty == "user" { "user" } else { "assistant" }.to_string(), py::strip(&t).to_string()));
            } else if g(&b, "type") == "tool_use" {
                let i = g(&b, "input");
                let arg = ["command", "file_path", "pattern", "description", "url", "prompt"].iter().map(|k| g(i, k)).find(|v| truthy(v)).unwrap_or(&EMPTY_STR);
                out.push(("tool".into(), format!("{} {}", str_(g(&b, "name")), py::split_ws(&str_(arg)).join(" "))));
            }
        }
    }
    Ok(out)
}

fn codex_entries(f: &Path) -> Res<Vec<(String, String)>> {
    let mut out = vec![];
    for line in read_lossy(f)?.lines() {
        let Ok(d) = py::loads(line) else { continue };
        let p = g(&d, "payload");
        let ty = g(p, "type");
        if ty == "message" && (g(p, "role") == "user" || g(p, "role") == "assistant") {
            for b in list(g(p, "content"))? {
                let t = str_(or(g(&b, "text"), &EMPTY_STR));
                if !py::strip(&t).is_empty() && !py::lstrip(&t).starts_with('<') {
                    out.push((str_(&p["role"]), py::strip(&t).to_string()));
                }
            }
        } else if ty == "function_call" || ty == "custom_tool_call" || ty == "local_shell_call" {
            let name = if truthy(g(p, "name")) { str_(&p["name"]) } else { "shell".into() };
            let arg = or(g(p, "arguments"), or(g(p, "input"), &EMPTY_STR));
            out.push(("tool".into(), format!("{name} {}", py::split_ws(&str_(arg)).join(" "))));
        }
    }
    Ok(out)
}

/// Every agent session that ran in `path`, newest first: Claude Code transcripts and Codex rollouts.
fn transcript_files(path: &str) -> Res<Vec<(PathBuf, &'static str)>> {
    let proj: String = path.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    let mut fs: Vec<(PathBuf, &str)> = glob1(&py::home().join(".claude/projects").join(proj), |n| n.ends_with(".jsonl")).into_iter().map(|f| (f, "claude")).collect();
    let cutoff = py::time() - 30.0 * 86400.0;
    // ponytail: reads each recent rollout's first line; index by cwd if this gets slow
    let dirs = |ds: Vec<PathBuf>| ds.iter().flat_map(|d| glob1(d, |_| true)).filter(|d| d.is_dir()).collect::<Vec<_>>();
    let days = dirs(dirs(dirs(vec![py::home().join(".codex/sessions")])));
    for f in days.iter().flat_map(|d| glob1(d, |n| n.starts_with("rollout-") && n.ends_with(".jsonl"))) {
        let ok = || -> Option<bool> {
            if mtime(&std::fs::metadata(&f).ok()?) <= cutoff {
                return Some(false);
            }
            let b = std::fs::read(&f).ok()?;
            let first = std::str::from_utf8(&b[..b.iter().position(|&c| c == b'\n').map_or(b.len(), |i| i + 1)]).ok()?;
            Some(g(py::loads(first).ok()?.get("payload")?, "cwd") == path)
        };
        if ok() == Some(true) {
            fs.push((f, "codex"));
        }
    }
    let mut keyed = fs.into_iter().map(|x| Ok((mtime(&std::fs::metadata(&x.0).or_else(|e| py::oserr(e, &x.0))?), x))).collect::<Res<Vec<_>>>()?;
    keyed.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(Ordering::Equal));
    Ok(keyed.into_iter().take(40).map(|x| x.1).collect())
}

/// A page of the conversation in the workspace, oldest first, ending just before `before` ("<file>:<index>").
/// Older sessions continue where the newest one starts; `more` is the cursor for the page before this one.
fn transcript(name: &str, before: Option<&str>) -> Res<Value> {
    let limit = 60;
    let live = live_agents()?;
    let Some((_, w, _)) = by_name(&live, name)? else { return Ok(json!({"error": format!("no workspace {name}")})) };
    let files = transcript_files(&str_(ix(w, "path")?))?;
    let (mut f, mut i): (usize, Option<usize>) = (0, None);
    if let Some(b) = before.filter(|b| !b.is_empty()) {
        let (st, idx) = b.rsplit_once(':').unwrap();
        f = files.iter().position(|x| stem(&x.0) == st).unwrap_or(files.len());
        i = Some(idx.parse().unwrap());
    }
    let mut page: Vec<Value> = vec![];
    while f < files.len() && page.len() < limit {
        let (path, kind) = &files[f];
        let es = if *kind == "claude" { claude_entries(path)? } else { codex_entries(path)? };
        let n = i.map_or(es.len(), |i| i.min(es.len()));
        let take = n.saturating_sub(limit - page.len());
        let st = stem(path);
        let chunk = es[take..n].iter().enumerate().map(|(k, (r, t))| json!({"id": format!("{st}:{}", k + take), "role": r, "text": py::head(t, 8000)}));
        page.splice(0..0, chunk);
        if take == 0 {
            use std::os::unix::fs::MetadataExt;
            let ct = std::fs::metadata(path).or_else(|e| py::oserr(e, path))?.ctime() as f64;
            page.insert(0, json!({"id": format!("{st}:start"), "role": "sep", "text": format!("{kind} session · {}", py::strftime_local("%b %-d %H:%M", ct))}));
            (f, i) = (f + 1, None);
        } else {
            i = Some(take);
            break;
        }
    }
    // 999999999: from that session's end
    let more = if f < files.len() { json!(format!("{}:{}", stem(&files[f].0), i.unwrap_or(999999999))) } else { Value::Null };
    Ok(json!({"entries": page, "more": more, "before": before}))
}

// ---------- New task: a request (+ files) becomes a workspace whose agent starts on it at once ----------
fn tasks() -> PathBuf {
    STATE.join("tasks")
}
const FILES_CAP: usize = 15 << 20; // raw bytes, all files together; stdin is capped at 20 MB of base64

/// Orca repos with a GitHub/GitLab remote: where a task's MR can live.
fn repos() -> Res<Vec<Value>> {
    let mut out = vec![];
    for r in py::it(ix(&orca_s(&["repo", "list"])?, "repos")?)? {
        let key = str_(or(g(g(&r, "gitRemoteIdentity"), "canonicalKey"), &EMPTY_STR));
        let (host, path) = key.split_once('/').unwrap_or((&key, ""));
        if (host == "gitlab.com" || host == "github.com") && !path.is_empty() {
            out.push(json!({"id": ix(&r, "id")?, "name": ix(&r, "displayName")?, "host": host.split('.').next().unwrap(), "path": path}));
        }
    }
    Ok(out)
}

/// A task's title from what was typed: its first non-empty line, cut to its first sentence, or else at a word with …, to fit cap.
fn task_title(text: &str, cap: usize) -> String {
    let line = py::splitlines(text).into_iter().find(|l| !py::strip(l).is_empty()).map(|l| py::split_ws(l).join(" ")).unwrap_or_default();
    let c: Vec<char> = line.chars().collect();
    if c.len() <= cap {
        return line;
    }
    // re.search(r"[.!?](?=\s|$)", line[:cap + 1])
    let s = &c[..cap + 1];
    if let Some(k) = (0..s.len()).find(|&k| ".!?".contains(s[k]) && (k + 1 == s.len() || py::is_space(s[k + 1]))) {
        if k < cap {
            return c[..=k].iter().collect();
        }
    }
    let cut = c[..cap].iter().rposition(|&x| x == ' '); // at most cap - 1 chars before it: room for the …
    let head: String = match cut {
        Some(i) if i > 0 => py::rstrip(&c[..i].iter().collect::<String>()).to_string(),
        _ => c[..cap - 1].iter().collect(),
    };
    head + "…"
}

const REQUEST_CAP: usize = 8000; // of the request quoted in the brief: Orca passes it as --prompt argv

fn slug(title: &str) -> String {
    let s = SLUG_RE.replace_all(&title.to_lowercase(), "-").into_owned();
    let s = py::head(s.trim_matches('-'), 40).trim_end_matches('-').to_string();
    if s.is_empty() { "untitled".into() } else { s }
}

fn free(base: &str, taken: &[String]) -> Res<String> {
    let names = (1..100).map(|i| if i == 1 { base.to_string() } else { format!("{base}-{i}") });
    names.into_iter().find(|n| !taken.contains(n)).map_or_else(|| py::raise("StopIteration", ""), Ok)
}

/// [(safe basename, bytes)] from [{name, data: data URL}], or an error string. Names are the user's; the bytes stay opaque.
fn files_of(files: &Value) -> Res<Result<Vec<(String, Vec<u8>)>, String>> {
    let (mut out, mut seen, mut total) = (vec![], Vec::<String>::new(), 0);
    for f in list(files)? {
        let data = str_(or(g(&f, "data"), &EMPTY_STR));
        let Some(m) = FILE_RE.captures(&data) else {
            return Ok(Err(format!("{} didn't come through", str_(or(g(&f, "name"), &json!("a file"))))));
        };
        let raw = b64decode(&m[1])?;
        total += raw.len();
        if total > FILES_CAP {
            return Ok(Err("attachments are capped at 15 MB together".into()));
        }
        let base = file_name(&str_(or(g(&f, "name"), &EMPTY_STR)));
        let name = FNAME_RE.replace_all(&base, "-");
        let name = py::head(name.trim_start_matches(['.', '-']), 80);
        let name = if name.is_empty() { "file".to_string() } else { name.to_string() };
        let name = if seen.contains(&name) { format!("{}-{name}", out.len() + 1) } else { name };
        seen.push(name.clone());
        out.push((name, raw));
    }
    Ok(Ok(out))
}

/// base64.b64decode(s) (validate=False): characters outside the alphabet are skipped, binascii's errors.
fn b64decode(s: &str) -> Res<Vec<u8>> {
    if !s.is_ascii() {
        return py::raise("ValueError", "string argument should contain only ASCII characters");
    }
    let (mut out, mut quad, mut left, mut pads) = (vec![], 0, 0u32, 0);
    for c in s.bytes() {
        if c == b'=' {
            if quad >= 2 {
                pads += 1;
                if quad + pads >= 4 {
                    return Ok(out); // a pad sequence ends the input
                }
            }
            continue;
        }
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => continue,
        } as u32;
        pads = 0;
        match quad {
            0 => left = v,
            1 => {
                out.push(((left << 2) | (v >> 4)) as u8);
                left = v & 0x0f
            }
            2 => {
                out.push(((left << 4) | (v >> 2)) as u8);
                left = v & 0x03
            }
            _ => out.push(((left << 6) | v) as u8),
        }
        quad = (quad + 1) % 4;
    }
    match quad {
        0 => Ok(out),
        1 => py::raise("Error", format!("Invalid base64-encoded string: number of data characters ({}) cannot be 1 more than a multiple of 4", out.len() / 3 * 4 + 1)),
        _ => py::raise("Error", "Incorrect padding"),
    }
}

/// hashlib.sha1(data).hexdigest()
fn sha1_hex(data: &[u8]) -> String {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let mut m = data.to_vec();
    m.push(0x80);
    while m.len() % 64 != 56 {
        m.push(0);
    }
    m.extend((data.len() as u64 * 8).to_be_bytes());
    for chunk in m.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes(chunk[4 * i..4 * i + 4].try_into().unwrap());
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = h;
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let t = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*wi);
            (e, d, c, b, a) = (d, c, b.rotate_left(30), a, t);
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e]) {
            *x = x.wrapping_add(y);
        }
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

/// Start a task: a workspace whose goal is the title (greenlight's freeform step) and whose agent gets the whole request and the files.
fn task_new(repo_name: &str, payload: &Value) -> Res<Value> {
    let Some(repo) = repos()?.into_iter().find(|r| r["name"] == repo_name) else {
        return Ok(json!({"error": format!("no repo {repo_name} with a GitHub/GitLab remote")}));
    };
    if !payload.is_object() {
        return py::raise("AttributeError", format!("'{}' object has no attribute 'get'", py::tname(payload)));
    }
    let request = py::strip(&str_(or(g(payload, "text"), &EMPTY_STR))).to_string();
    let title = task_title(&request, 120);
    if title.is_empty() {
        return Ok(json!({"error": "describe the task first"}));
    }
    let files = match files_of(g(payload, "files"))? {
        Ok(f) => f,
        Err(e) => return Ok(json!({"error": e})),
    };
    let mut taken = live_agents()?.iter().map(|x| ix(&x.1, "displayName").map(str_)).collect::<Res<Vec<_>>>()?;
    taken.extend(glob1(&tasks(), |_| true).iter().map(|d| file_name(&d.to_string_lossy())));
    let name = free(&slug(&title), &taken)?;
    let d = tasks().join(&name);
    mkdirs(&d.join("files"))?;
    write(&d.join("request.md"), &(request.clone() + "\n"))?;
    for (f, raw) in &files {
        let p = d.join("files").join(f);
        std::fs::write(&p, raw).or_else(|e| py::oserr(e, &p))?;
    }
    let dd = d.display();
    let quoted = if request.chars().count() <= REQUEST_CAP { request.clone() } else { format!("{}\n[… cut here: the rest is in {dd}/request.md]", py::head(&request, REQUEST_CAP)) };
    let mut brief = format!("Your task in {}: {title}. My whole request is below (verbatim in {dd}/request.md).", str_(&repo["path"]));
    if !files.is_empty() {
        let names: Vec<&str> = files.iter().map(|f| f.0.as_str()).collect();
        brief += &format!(" I attached {} file{}, in {dd}/files: {}. Read them first.", files.len(), if files.len() > 1 { "s" } else { "" }, names.join(", "));
    }
    brief += &format!(" Implement it in this workspace with tests, then commit, push and open a draft MR (a draft PR on GitHub). If something is unclear, ask me with the \
AskUserQuestion tool (2-4 options, your recommendation first) rather than guess.\n\nMy request:\n\n{quoted}");
    let agents = py::it(base::cfg("agents"))?;
    let (mut res, mut used) = (None, String::new());
    for a in &agents {
        used = str_(a);
        match orca_s(&["worktree", "create", "--repo", &format!("id:{}", str_(&repo["id"])), "--name", &name, "--setup", "skip",
                       "--comment", &title, "--no-parent", "--agent", &used, "--prompt", &brief]) {
            Ok(v) => {
                res = Some(v);
                break;
            }
            Err(e) if py::kind(&*e) == "RuntimeError" => {
                if Some(a) == agents.last() || !LIMIT_RE.is_match(&e.to_string()) {
                    return Ok(json!({"error": format!("couldn't start the workspace: {}", py::head(&e.to_string(), 300))}));
                }
            }
            Err(e) => return Err(e),
        }
    }
    // no agents at all used to crash with "cannot access local variable 'res'"
    let Some(res) = res else { return py::err("no agents configured (config.json agents is empty)") };
    let wid = g(g(&res, "worktree"), "id");
    if truthy(wid) {
        // keep Orca from prefixing the name; cosmetic, so a failure here doesn't fail the task
        if let Err(e) = orca_s(&["worktree", "set", "--worktree", &format!("id:{}", str_(wid)), "--display-name", &name]) {
            if py::kind(&*e) != "RuntimeError" {
                return Err(e);
            }
        }
    }
    greenlight::note(&name, &format!("task started ({used}, {} file{}): {title}", files.len(), if files.len() != 1 { "s" } else { "" }))?;
    Ok(json!({"ok": true, "name": name, "repo": repo["name"]}))
}

fn task(op: &str, arg: &str, stdin: &str) -> Res<Value> {
    match op {
        "repos" => Ok(json!({"repos": repos()?.iter().map(|r| json!({"name": r["name"], "host": r["host"], "path": r["path"]})).collect::<Vec<_>>()})),
        // a body that isn't JSON used to fail with the decoder's bare message
        "new" => task_new(arg, &py::loads(if stdin.is_empty() { "{}" } else { stdin }).or_else(|e| py::raise("ValueError", format!("stdin is not JSON ({e})")))?),
        _ => settings(op, arg, stdin),
    }
}

/// The app's Settings: a repo's lifecycle, greenlight's standing instructions, and greenlight's memory notes.
fn settings(op: &str, arg: &str, stdin: &str) -> Res<Value> {
    let bad_repo = |r: &str| !REPO_RE.is_match(r) || r.starts_with('.');
    if op.starts_with("prompt-") {
        // the hover card's prompts: arg <repo>/<kind>; yours in ~/.greenlight/prompts/<repo>/<kind>.md
        let (repo, kind) = arg.split_once('/').unwrap_or((arg, ""));
        let Some(vars) = kinds(kind).filter(|_| !bad_repo(repo)) else { return Ok(json!({"error": "bad repo or kind"})) };
        let f = own_prompt(repo, kind);
        if op == "prompt-set" && !py::strip(stdin).is_empty() {
            mkdirs(f.parent().unwrap())?;
            write(&f, &(py::strip(stdin).to_string() + "\n"))?;
        } else if op == "prompt-set" {
            unlink(&f)?;
        } else if op != "prompt-get" {
            return Ok(json!({"error": format!("no op {op}")}));
        }
        let text = if f.exists() { read(&f)? } else { String::new() };
        let default = py::strip(&read(&ROOT.join("prompts").join(format!("{kind}.md")))?).to_string();
        let vars: Vec<&str> = COMMON.iter().chain(vars).copied().collect();
        return Ok(json!({"kind": kind, "text": text, "default": default, "own": f.exists(), "vars": vars}));
    }
    if op == "autorun-get" || op == "autorun-set" {
        // arg <repo>; set's stdin {kind: gate index}, a kind left out is off
        if bad_repo(arg) {
            return Ok(json!({"error": "bad repo"}));
        }
        let mut a = autorun()?;
        if op == "autorun-set" {
            let on = py::loads(if stdin.is_empty() { "{}" } else { stdin })?;
            let t = py::tname(&on);
            let Value::Object(on) = on else { return py::raise("AttributeError", format!("'{t}' object has no attribute 'items'")) };
            let keep: Obj = on.into_iter().filter(|(k, v)| gates(k).is_some_and(|gs_| gate(v, gs_).is_some())).collect();
            obj_mut(&mut a, "autorun.json")?.insert(arg.into(), Value::Object(keep));
            write(&STATE.join("autorun.json"), &py::dumps_indent(&a))?;
        }
        let kinds: Vec<Value> = AUTO.iter().map(|(k, w, gs_)| json!({"kind": k, "when": w, "gates": gs_.iter().map(|x| x.0).collect::<Vec<_>>()})).collect();
        return Ok(json!({"on": gd(&a, arg, json!({})), "kinds": kinds}));
    }
    if op.starts_with("lifecycle-") {
        if bad_repo(arg) {
            return Ok(json!({"error": "bad repo"}));
        }
        let f = STATE.join("lifecycles").join(format!("{arg}.md"));
        if op == "lifecycle-set" {
            let md = greenlight::lifecycle_md(arg, &py::loads(stdin)?)?;
            mkdirs(f.parent().unwrap())?;
            write(&f, &md)?;
        } else if op == "lifecycle-reset" {
            unlink(&f)?;
        } else if op != "lifecycle-get" {
            return Ok(json!({"error": format!("no op {op}")}));
        }
        let mut out = greenlight::lifecycle_doc(arg)?;
        merge(&mut out, json!({"builtins": *greenlight::BUILTIN}))?;
        return Ok(out);
    }
    if op == "instructions-get" || op == "instructions-set" {
        let f = STATE.join("instructions.md");
        if op == "instructions-set" {
            if py::strip(stdin).is_empty() { unlink(&f)? } else { write(&f, &(py::strip(stdin).to_string() + "\n"))? }
        }
        return Ok(json!({"text": if f.exists() { read(&f)? } else { String::new() }}));
    }
    if op.starts_with("memory-") {
        let memory = ROOT.join("memory");
        let index = memory.join("MEMORY.md");
        let text = if index.exists() { read(&index)? } else { String::new() };
        let lines = py::splitlines(&text);
        if op != "memory-list" {
            if !NOTE_RE.is_match(arg) || arg == "MEMORY" {
                return Ok(json!({"error": "bad note name"}));
            }
            let (f, link) = (memory.join(format!("{arg}.md")), format!("]({arg}.md)"));
            let mut keep: Vec<String> = lines.iter().filter(|l| !l.contains(&link)).map(|l| l.to_string()).collect();
            if op == "memory-set" {
                mkdirs(&memory)?;
                write(&f, &(py::rstrip(stdin).to_string() + "\n"))?;
                let desc = DESC_RE.captures(stdin).map_or(arg.to_string(), |d| py::strip(&d[1]).to_string());
                keep.push(format!("- [{arg}]({arg}.md) — {desc}"));
            } else if op == "memory-delete" {
                unlink(&f)?;
            } else {
                return Ok(json!({"error": format!("no op {op}")}));
            }
            write(&index, &(keep.join("\n") + "\n"))?;
        }
        let mut fs = glob1(&memory, |n| n.ends_with(".md"));
        fs.sort();
        let notes = fs.iter().filter(|f| f.file_name().is_some_and(|n| n != "MEMORY.md")).map(|f| Ok(json!({"name": stem(f), "text": read(f)?}))).collect::<Res<Vec<_>>>()?;
        return Ok(json!({"notes": notes}));
    }
    Ok(json!({"error": format!("no task op {op}")}))
}

/// sys.stdin.read()
fn stdin() -> String {
    let mut b = vec![];
    let _ = std::io::stdin().read_to_end(&mut b);
    String::from_utf8_lossy(&b).into_owned()
}

/// float(x)
fn pyfloat(x: &str) -> Res<f64> {
    py::strip(x).parse().or_else(|_| py::raise("ValueError", format!("could not convert string to float: {}", py::repr_str(x))))
}

pub fn main(a: Vec<String>) -> Res<i32> {
    let name_ok = |i: usize| a.get(i).is_some_and(|x| NAME_RE.is_match(x));
    // --model/--effort: the value after the flag (a flag in last place is a name, not an option)
    let opts = || {
        let mut o = ["claude-haiku-5-5".to_string(), "low".to_string()];
        for (k, v) in ["--model", "--effort"].iter().zip(o.iter_mut()) {
            if a[..a.len() - 1].iter().any(|x| x == k) {
                *v = a[a.iter().position(|x| x == k).unwrap() + 1].clone();
            }
        }
        if !o.iter().all(|v| OPT_RE.is_match(v)) {
            py::sys_exit("bad --model/--effort")
        }
        o
    };
    let print = |v: &Value| println!("{}", py::dumps(v));
    let with = |head: Value, r: Value| -> Res<Value> {
        let mut o = head;
        merge(&mut o, r)?;
        Ok(o)
    };
    let err_of = |e: Box<dyn std::error::Error + Send + Sync>, n: usize| json!(py::head(&e.to_string(), n));
    match a.first().map(String::as_str) {
        Some("snapshot") => print(&snapshot(false)?),
        Some("stream") => {
            let every = match a.get(1) {
                Some(x) => pyfloat(x)?,
                None => 10.0,
            };
            loop {
                let tick = || -> Res<Value> {
                    let s = snapshot(false)?;
                    record(py::arr(&s["pucks"]))?;
                    Ok(s)
                };
                match tick() {
                    Ok(s) => print(&s),
                    Err(e) => print(&json!({"error": err_of(e, 300)})),
                }
                if every < 0.0 {
                    return py::raise("ValueError", "sleep length must be non-negative");
                }
                if !every.is_finite() {
                    return py::raise("ValueError", format!("bad stream interval {}: not a finite number of seconds", a[1]));
                }
                std::thread::sleep(std::time::Duration::from_secs_f64(every));
            }
        }
        Some("page") if name_ok(1) => {
            let [m, e] = opts();
            print(&page(&a[1], a.iter().any(|x| x == "--fresh"), &m, &e)?)
        }
        Some("suggest") if name_ok(1) => {
            let [m, e] = opts();
            print(&suggest(&a[1], &m, &e).unwrap_or_else(|e| json!({"name": a[1], "error": err_of(e, 200)})))
        }
        Some("run") if name_ok(1) => {
            let prompt = py::head(py::strip(&stdin()), 8000).to_string();
            let r = if prompt.is_empty() { json!({"error": "empty prompt"}) } else { run(&a[1], &prompt)? };
            print(&with(json!({"name": a[1]}), r)?)
        }
        Some(c @ ("ask" | "answer")) if name_ok(1) => {
            let [m, e] = opts();
            let r = || -> Res<Value> {
                if c == "ask" {
                    return ask_user(&a[1], &m, &e);
                }
                let text = py::head(py::strip(&stdin()), 4000).to_string();
                with(json!({"name": a[1]}), if text.is_empty() { json!({"error": "empty answer"}) } else { answer(&a[1], &text)? })
            };
            print(&r().unwrap_or_else(|e| json!({"name": a[1], "error": err_of(e, 200)})))
        }
        Some("park") if a.len() == 3 && name_ok(1) && (a[2] == "on" || a[2] == "off") => {
            let r = park(&a[1], a[2] == "on").and_then(|r| with(json!({"name": a[1]}), r));
            print(&r.unwrap_or_else(|e| json!({"name": a[1], "error": err_of(e, 200)})))
        }
        Some("task") if a.len() == 3 && name_ok(2) => {
            let r = || with(json!({"op": a[1], "arg": a[2]}), task(&a[1], &a[2], py::head(py::strip(&stdin()), 20 << 20))?);
            print(&r().unwrap_or_else(|e| json!({"op": a[1], "arg": a[2], "error": err_of(e, 300)})))
        }
        Some("transcript") if name_ok(1) => {
            let before = a[..a.len() - 1].iter().position(|x| x == "--before").map(|i| a[i + 1].as_str());
            if before.is_some_and(|b| !b.is_empty() && !BEFORE_RE.is_match(b)) {
                py::sys_exit("bad --before")
            }
            print(&with(json!({"name": a[1]}), transcript(&a[1], before)?)?)
        }
        Some("trees") => print(&json!({"trees": trees()?})),
        Some("tree") if a.len() >= 3 && ["info", "rm", "goal", "cfg", "merge"].contains(&a[1].as_str()) && PATH_RE.is_match(&a[2]) && a[2].chars().count() <= 301 && !a[2].contains("..") => {
            let (op, path) = (a[1].as_str(), a[2].as_str());
            let r = match op {
                "info" => tree_info(path),
                "merge" => tree_merge(path),
                "rm" => tree_rm(path, a[3..].iter().any(|x| x == "--force")),
                "goal" => tree_goal(path, py::head(&stdin(), 2000)),
                _ => tree_cfg(path, py::head(&stdin(), 8193)),
            };
            let r = r.unwrap_or_else(|e| json!({"error": err_of(e, 300)}));
            print(&with(json!({"op": op, "path": path}), r)?)
        }
        _ => py::sys_exit(DOC),
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hand_rolled_codecs() {
        assert_eq!(sha1_hex(b"abc"), "a9993e364706816aba3e25717850c26c9cd0d89d");
        assert_eq!(sha1_hex(&[b'a'; 64]), "0098ba824b5c16427bd7a1122a5a442a25ec644d");
        assert_eq!(b64decode("aGVs bG8=").unwrap(), b"hello");
        assert_eq!(b64decode("aGVsbG8").unwrap_err().to_string(), "Incorrect padding");
        assert!(b64decode("a").unwrap_err().to_string().contains("(1) cannot be 1 more"));
        assert_eq!(task_title(&format!("{} end", "x".repeat(130)), 120), format!("{}…", "x".repeat(119)));
    }
}
