//! Port of scripts/greenlight.py (lines 178-906; the top of the file is base.rs).
//! Foundation: note/history/signoff/ade only. The rest is being ported.
use crate::base::{orca, STATE};
use crate::py::{self, Res};
use serde_json::{json, Value};
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
        "ade" => {
            let a: Vec<String> = args[1..].iter().filter(|a| *a != "--json").cloned().collect();
            println!("{}", py::dumps_indent(&orca(&a)?));
        }
        _ => return py::err(format!("greenlight {cmd}: not ported yet")),
    }
    Ok(0)
}
