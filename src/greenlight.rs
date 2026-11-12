//! Port of scripts/greenlight.py (lines 178-906; the top of the file is base.rs).
//! Foundation: note/history/signoff/ade and the ping family. The rest is being ported.
use crate::base::{orca, STATE};
use crate::py::{self, Res};
use serde_json::{json, Map, Value};
use std::io::Write;
use std::path::PathBuf;

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

pub fn signoffs() -> Res<Value> {
    let f = STATE.join("signoffs.json");
    if f.exists() { py::loads(&std::fs::read_to_string(f)?) } else { Ok(json!({})) }
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
        _ => return py::err(format!("greenlight {cmd}: not ported yet")),
    }
    Ok(0)
}
