//! Port of scripts/auto.py: greenlight's own automations, Orca-style scheduled agent runs, no Orca needed.
use crate::base::{cfg, LIMIT_RE, STATE};
use crate::py::{self, kill, setsid, Res};
use crate::{greenlight, harness};
use serde_json::{json, Map, Value};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const DOC: &str = r#"Greenlight's own automations: Orca-style scheduled agent runs, no Orca needed.

  greenlight auto list
  greenlight auto add <name> --schedule "<cron>" --prompt "<text>" [--cwd DIR] [--precheck CMD]
              [--provider claude|codex|opencode|pi] [--model M] [--effort E] [--timeout MIN]
  greenlight auto edit <name> [any add option] [--enabled | --disabled]
  greenlight auto rm <name>
  greenlight auto show <name>       # its JSON
  greenlight auto run <name>        # now, in the background, skipping the schedule (not the precheck)
  greenlight auto runs [name]       # last 20 runs
  greenlight auto tick              # the timer calls this every minute
  greenlight auto install           # writes and starts the timer: a systemd user timer, or a launchd agent on macOS

Automations live in ~/.greenlight/automations.json, runs in ~/.greenlight/auto/.
Schedules are 5-field cron in the machine's local time (*, */n, a-b, a,b; Sunday=0).
A precheck that exits non-zero means "nothing to do": the run is skipped.
The provider (default: config agents[0]) runs first; on a usage limit the same prompt goes to the next of config agents.
Model and effort apply to the provider only. Permissions follow the harness rules (config `harnesses`): safe unless that harness has bypass set.
"#;

const OPTS: [&str; 8] = ["schedule", "prompt", "cwd", "precheck", "provider", "model", "effort", "timeout"];

fn file() -> PathBuf {
    STATE.join("automations.json")
}
fn dir() -> PathBuf {
    STATE.join("auto")
}

/// An io error as Python words it: `[Errno 2] No such file or directory: '<path>'`.
fn io_err(e: std::io::Error, path: &Path) -> Box<dyn std::error::Error + Send + Sync> {
    let kind = if e.kind() == std::io::ErrorKind::NotFound { "FileNotFoundError" } else { "OSError" };
    let s = e.to_string();
    let msg = s.split(" (os error").next().unwrap_or(&s);
    let code = e.raw_os_error().unwrap_or(0);
    Box::new(py::PyErr { kind, msg: format!("[Errno {code}] {msg}: {}", py::repr_str(&path.display().to_string())) })
}

fn load() -> Res<Map<String, Value>> {
    let f = file();
    if !f.exists() {
        return Ok(Map::new());
    }
    // Python: a JSONDecodeError traceback for every command; Rust names the file.
    match py::loads(&std::fs::read_to_string(&f)?).map_err(|e| format!("{}: {e}", f.display()))? {
        Value::Object(m) => Ok(m),
        _ => py::err(format!("{}: not a JSON object", f.display())),
    }
}

fn save(autos: &Map<String, Value>) -> Res<()> {
    if let Err(e) = std::fs::create_dir(&*STATE) {
        if e.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(io_err(e, &STATE));
        }
    }
    std::fs::write(file(), py::dumps_indent_n(&Value::Object(autos.clone()), 2))?;
    Ok(())
}

/// Python's int(s) for a cron token: surrounding whitespace, a sign, `_` between digits.
/// ponytail: non-ASCII digits (int("٣") == 3) are not accepted.
fn py_int(s: &str) -> Option<i64> {
    let t = py::strip(s);
    let (neg, d) = match t.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if d.is_empty() || d.starts_with('_') || d.ends_with('_') || d.contains("__") || !d.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return None;
    }
    let n: i64 = d.replace('_', "").parse().ok()?;
    Some(if neg { -n } else { n })
}

/// A cron field as Python ranges: (start, stop, step) with stop exclusive. Kept lazy: `0-1000000000` is fine.
fn field(spec: &str, lo: i64, hi: i64) -> Result<Vec<(i64, i64, i64)>, String> {
    let int = |s: &str| py_int(s).ok_or_else(|| format!("{} is not a number", py::repr_str(s)));
    let mut out = vec![];
    for part in spec.split(',') {
        let (rng, step) = part.split_once('/').unwrap_or((part, ""));
        let (a, b) = if rng == "*" {
            (lo, hi)
        } else {
            // (rng.split("-") * 2)[:2]: "5" is 5-5, "1-5-9" is 1-5
            let mut xs: Vec<&str> = rng.split('-').collect();
            xs.extend(xs.clone());
            (int(xs[0])?, int(xs[1])?)
        };
        let step = if step.is_empty() { 1 } else { int(step)? };
        if step == 0 {
            return Err(format!("step 0 in {}", py::repr_str(part)));
        }
        out.push((a, b + 1, step));
    }
    Ok(out)
}

fn has(f: &[(i64, i64, i64)], x: i64) -> bool {
    f.iter().any(|&(a, b, s)| if s > 0 { a <= x && x < b && (x - a) % s == 0 } else { b < x && x <= a && (a - x) % -s == 0 })
}

/// The struct_time fields due() reads; wday is Sunday=0.
struct Tm {
    min: i64,
    hour: i64,
    mday: i64,
    mon: i64,
    wday: i64,
}

fn localtime(ts: f64) -> Tm {
    use chrono::{Datelike, TimeZone, Timelike};
    let t = chrono::Local.timestamp_opt(ts.floor() as i64, 0).unwrap();
    Tm { min: t.minute() as i64, hour: t.hour() as i64, mday: t.day() as i64, mon: t.month() as i64, wday: t.weekday().num_days_from_sunday() as i64 }
}

/// Does a 5-field cron match t (to the minute)? Python's evaluation order is kept: the day fields parse first,
/// then minute, hour, month short-circuit, so a bad hour only fails when the minute matches.
fn due(cron: &str, t: &Tm) -> Res<bool> {
    let bad = |m: String| py::raise::<bool>("ValueError", format!("bad schedule {}: {m}", py::repr_str(cron)));
    let f = py::split_ws(cron);
    if f.len() != 5 {
        // Python: "not enough / too many values to unpack"
        return bad(format!("needs 5 fields (minute hour day month weekday), got {}", f.len()));
    }
    let (m, h, dom, mon, dow) = (f[0], f[1], f[2], f[3], f[4]);
    let ok_dom = match field(dom, 1, 31) {
        Ok(x) => has(&x, t.mday),
        Err(e) => return bad(e),
    };
    let ok_dow = match field(dow, 0, 7) {
        Ok(x) => has(&x, t.wday) || (t.wday == 0 && dow.contains('7')),
        Err(e) => return bad(e),
    };
    // cron: both restricted → either matches
    let day = if dom == "*" || dow == "*" { ok_dom && ok_dow } else { ok_dom || ok_dow };
    for (spec, lo, hi, x) in [(m, 0, 59, t.min), (h, 0, 23, t.hour), (mon, 1, 12, t.mon)] {
        match field(spec, lo, hi) {
            Ok(f) if has(&f, x) => {}
            Ok(_) => return Ok(false),
            Err(e) => return bad(e),
        }
    }
    Ok(day)
}

fn alive(name: &str) -> bool {
    let Ok(s) = std::fs::read_to_string(dir().join(format!("{name}.pid"))) else { return false };
    // ponytail: a pid beyond i32 is OverflowError in Python (a crash); here it reads as dead
    match py_int(&s).and_then(|p| i32::try_from(p).ok()) {
        Some(p) => unsafe { kill(p, 0) == 0 }, // pid 0 or negative signals a group: "alive", as in Python
        None => false,
    }
}

fn provider(a: &Value) -> Res<String> {
    if let Some(p) = py::get_or(a, "provider") {
        return Ok(py::str_(p));
    }
    match cfg("agents").get(0) {
        Some(x) => Ok(py::str_(x)),
        None => py::err("config.json agents is empty: set at least one agent, or give the automation a --provider"),
    }
}

/// int(x) on a JSON value, as `int(a.get("timeout") or 120)` sees it.
fn py_int_value(v: Option<&Value>) -> Res<i64> {
    match v.filter(|x| py::truthy(x)) {
        None => Ok(120),
        Some(Value::String(s)) => py_int(s).map_or_else(|| py::raise("ValueError", format!("invalid literal for int() with base 10: {}", py::repr_str(s))), Ok),
        Some(Value::Number(n)) => Ok(n.as_i64().unwrap_or_else(|| n.as_f64().unwrap_or(0.0).trunc() as i64)),
        Some(Value::Bool(_)) => Ok(1),
        Some(x) => py::raise("TypeError", format!("int() argument must be a string, a bytes-like object or a real number, not '{}'", if x.is_array() { "list" } else { "dict" })),
    }
}

/// subprocess.run(cmd, input=text, cwd=cwd, text=True, stdout=PIPE, stderr=STDOUT, timeout=secs):
/// (merged output, returncode, timed out). On a timeout only the direct child is killed and the output so far is kept.
fn run_merged(cmd: &[String], input: &str, cwd: &Path, secs: f64) -> Res<(String, i32, bool)> {
    let (mut r, w) = std::io::pipe()?;
    let mut child = {
        let mut c = Command::new(&cmd[0]);
        c.args(&cmd[1..]).current_dir(cwd).stdin(Stdio::piped()).stdout(w.try_clone()?).stderr(w);
        match c.spawn() {
            Ok(ch) => ch,
            Err(e) => return Err(io_err(e, if cwd.is_dir() { Path::new(&cmd[0]) } else { cwd })),
        }
    }; // the Command (and our copies of the write end) drop here, so EOF comes when the child's side closes
    let mut stdin = child.stdin.take().unwrap();
    let text = input.to_string();
    std::thread::spawn(move || {
        let _ = stdin.write_all(text.as_bytes());
    });
    let buf = Arc::new(Mutex::new(Vec::new()));
    let b2 = buf.clone();
    let reader = std::thread::spawn(move || {
        let mut chunk = [0u8; 8192];
        while let Ok(n) = r.read(&mut chunk) {
            if n == 0 {
                break;
            }
            b2.lock().unwrap().extend_from_slice(&chunk[..n]);
        }
    });
    let deadline = Instant::now() + Duration::from_secs_f64(secs.max(0.0));
    let status = loop {
        if let Some(s) = child.try_wait()? {
            break s;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            // ponytail: bytes.decode(errors="replace"), no newline translation, as TimeoutExpired.stdout is raw
            let out = String::from_utf8_lossy(&buf.lock().unwrap()).into_owned();
            return Ok((out, 124, true));
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    let _ = reader.join();
    use std::os::unix::process::ExitStatusExt;
    let rc = status.code().unwrap_or_else(|| -status.signal().unwrap_or(0));
    // ponytail: Python's strict UTF-8 decode would make invalid output a UnicodeDecodeError (exit 127); lossy here
    let out = String::from_utf8_lossy(&buf.lock().unwrap()).replace("\r\n", "\n").replace('\r', "\n");
    Ok((out, rc, false))
}

fn agent(a: &Value, engine: &str, log: &mut std::fs::File) -> Res<(String, i32)> {
    let own = engine == provider(a)?; // a model name only means something to its own harness
    let pick = |k: &str| if own { a.get(k).and_then(Value::as_str) } else { None };
    let res = (|| {
        let (cmd, text) = harness::argv(engine, py::gs(a, "prompt"), pick("model"), pick("effort"), None, None, false)?;
        let cwd = py::expanduser(py::get_or(a, "cwd").map_or("~".into(), py::str_).as_str());
        let secs = py_int_value(a.get("timeout"))? * 60;
        run_merged(&cmd, &text, &cwd, secs as f64)
    })();
    let (out, code) = match res {
        Ok((o, _, true)) => (o + "\n[timed out]", 124),
        Ok((o, rc, false)) => (o, rc),
        // an unusable harness: unknown, pi without bypass, binary missing
        Err(e) => (e.to_string(), 127),
    };
    write!(log, "$ {engine}\n{out}\n[exit {code}]\n")?;
    Ok((out, code))
}

/// Prechecks saved by the Python install (`/usr/bin/python3 …/scripts/greenlight.py precheck`) run
/// the binary instead: `<exe> greenlight precheck`, since the binary takes the old script name first.
fn migrate(pre: &str) -> String {
    static RE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| regex::Regex::new(r"\S*python3 +\S*scripts/(\w+)\.py\b").unwrap());
    RE.replace_all(pre, format!("{} $1", crate::base::exe()).as_str()).into_owned()
}

#[test]
fn migrate_precheck() {
    let e = crate::base::exe();
    let got = migrate("/usr/bin/python3 /h/.claude/skills/greenlight/scripts/greenlight.py precheck || python3 ~/x/scripts/cleanup.py precheck");
    assert_eq!(got, format!("{e} greenlight precheck || {e} cleanup precheck"));
}

/// One run, start to finish: precheck, agent (the next of config agents on a limit), log, record.
fn execute(name: Option<&str>) -> Res<()> {
    let autos = load()?;
    // Python: KeyError traceback
    let Some(a) = name.and_then(|n| autos.get(n)) else {
        return py::err(format!("{}: no such automation", name.unwrap_or("None")));
    };
    let name = name.unwrap();
    let d = dir();
    std::fs::create_dir_all(&d)?;
    let pidf = d.join(format!("{name}.pid"));
    std::fs::write(&pidf, std::process::id().to_string()).map_err(|e| io_err(e, &pidf))?;
    let start = py::time();
    let logf = d.join(name).join(format!("{}.log", py::strftime_local("%Y%m%d-%H%M%S", start)));
    let parent = logf.parent().unwrap();
    if let Err(e) = std::fs::create_dir(parent) {
        if e.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(io_err(e, parent));
        }
    }
    let mut rec = json!({"name": name, "ts": start as i64, "log": logf.display().to_string()});
    {
        let mut log = std::fs::File::create(&logf).map_err(|e| io_err(e, &logf))?;
        if let Some(pre) = py::get_or(a, "precheck") {
            let pre = migrate(&py::str_(pre));
            // shell on purpose: prechecks are the owner's own commands (`a || b`), like Orca's
            let cwd = py::expanduser(py::get_or(a, "cwd").map_or("~".into(), py::str_).as_str());
            // ponytail: a timeout or a missing cwd crashes the run (errors.log, pid file left), as in Python
            let p = py::exec(&["/bin/sh".into(), "-c".into(), pre.clone()], None, Some(300.0), Some(&cwd), &[])?;
            write!(log, "$ precheck: {pre}\n{}{}[exit {}]\n", p.out, p.err, p.rc)?;
            if p.rc != 0 {
                let s = py::strip(if p.out.is_empty() { &p.err } else { &p.out });
                rec["status"] = json!("skipped");
                rec["summary"] = json!(py::tail(s, 200));
            }
        }
        if rec.get("status").is_none() {
            let mut engines = vec![provider(a)?];
            for e in cfg("agents").as_array().into_iter().flatten().map(py::str_) {
                if !engines.contains(&e) {
                    engines.push(e);
                }
            }
            let (mut out, mut code, mut engine) = (String::new(), 0, String::new());
            for e in engines {
                (out, code) = agent(a, &e, &mut log)?;
                engine = e;
                if !(code != 0 && LIMIT_RE.is_match(&out)) {
                    break;
                }
            }
            rec["status"] = json!(if code == 0 { "ok" } else { "failed" });
            rec["engine"] = json!(engine);
            rec["summary"] = json!(py::tail(py::strip(&out), 300));
        }
    }
    rec["secs"] = json!((py::time() - start) as i64);
    let mut fh = std::fs::OpenOptions::new().create(true).append(true).open(d.join("runs.jsonl"))?;
    writeln!(fh, "{}", py::dumps(&rec))?;
    let _ = std::fs::remove_file(&pidf);
    if rec["status"] == "failed" {
        let summary = py::gs(&rec, "summary");
        greenlight::ping(&format!("Automation {name} failed: {}\nLog: {}\nOptions: look into it | ignore", py::tail(summary, 150), logf.display()), None, vec![])?;
    }
    Ok(())
}

fn spawn(name: &str) -> Res<()> {
    if alive(name) {
        println!("{name}: still running, skipped");
        return Ok(());
    }
    let d = dir();
    std::fs::create_dir_all(&d)?;
    // a crash before the run log exists lands here
    let err = std::fs::OpenOptions::new().create(true).append(true).open(d.join("errors.log"))?;
    use std::os::unix::process::CommandExt;
    // cutover: Python respawned `sys.executable auto.py exec <name>`; this binary respawns itself.
    let mut c = Command::new(std::env::current_exe()?);
    c.args(["auto", "exec", name]).stdout(Stdio::null()).stderr(err);
    unsafe {
        c.pre_exec(|| {
            setsid(); // start_new_session=True
            Ok(())
        });
    }
    c.spawn()?;
    println!("{name}: started");
    Ok(())
}

fn tick() -> Res<()> {
    let t = localtime(py::time());
    // ponytail: a minute the timer misses (machine down) is skipped, not caught up; add a last-run check if that matters
    for (name, a) in load()? {
        if a.get("enabled").map_or(true, py::truthy) && due(py::gs(&a, "schedule"), &t)? {
            spawn(&name)?;
        }
    }
    Ok(())
}

const UNIT: &str = r#"[Unit]
Description=Greenlight automations tick
[Service]
Type=oneshot
KillMode=process
Environment="PATH={env_path}"
ExecStart="{exe}" auto tick
"#;
const TIMER: &str = "[Unit]
Description=Run greenlight automations every minute
[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s
[Install]
WantedBy=timers.target
";
const PLIST: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>greenlight-auto</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>auto</string><string>tick</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{env_path}</string></dict>
  <key>StartInterval</key><integer>60</integer>
</dict></plist>
"#;


/// subprocess.run(cmd, check=check) with inherited stdio; `quiet` is capture_output=True.
fn sh(cmd: &[&str], check: bool, quiet: bool) -> Res<()> {
    let mut c = Command::new(cmd[0]);
    c.args(&cmd[1..]);
    if quiet {
        c.stdout(Stdio::null()).stderr(Stdio::null());
    }
    let st = c.status().map_err(|e| io_err(e, Path::new(cmd[0])))?;
    if check && !st.success() {
        let v: Vec<String> = cmd.iter().map(|s| s.to_string()).collect();
        return py::raise("CalledProcessError", format!("Command '{}' returned non-zero exit status {}.", py::repr_list(&v), st.code().unwrap_or(-1)));
    }
    Ok(())
}

fn install() -> Res<()> {
    let exe = crate::base::exe();
    // launchd/systemd start with a bare PATH: keep this shell's, so glab/gh/orca and the agents are found
    let env_path = std::env::var("PATH").unwrap_or_default();
    if cfg!(target_os = "macos") {
        let f = py::home().join("Library/LaunchAgents/greenlight-auto.plist");
        std::fs::create_dir_all(f.parent().unwrap())?;
        let x = |s: &str| s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
        std::fs::write(&f, PLIST.replace("{exe}", &x(&exe)).replace("{env_path}", &x(&env_path)))?;
        let fs = f.display().to_string();
        sh(&["launchctl", "unload", &fs], false, true)?;
        sh(&["launchctl", "load", "-w", &fs], true, false)?;
        println!("greenlight-auto launch agent loaded ({fs})");
    } else {
        let q = |s: &str| s.replace('\\', "\\\\").replace('"', "\\\"").replace('%', "%%");
        let d = py::home().join(".config/systemd/user");
        std::fs::create_dir_all(&d)?;
        std::fs::write(d.join("greenlight-auto.service"), UNIT.replace("{exe}", &q(&exe)).replace("{env_path}", &q(&env_path)))?;
        std::fs::write(d.join("greenlight-auto.timer"), TIMER)?;
        sh(&["systemctl", "--user", "daemon-reload"], true, false)?;
        sh(&["systemctl", "--user", "enable", "--now", "greenlight-auto.timer"], true, false)?;
        println!("greenlight-auto.timer enabled");
    }
    Ok(())
}

pub fn main(args: Vec<String>) -> Res<i32> {
    let cmd = args.first().map_or("list", String::as_str);
    let name = args.get(1).map(String::as_str);
    let shown = name.unwrap_or("None"); // Python prints None for a missing name
    let opt = |k: &str| -> Res<Option<String>> {
        match args.iter().position(|a| *a == format!("--{k}")) {
            None => Ok(None),
            Some(i) => match args.get(i + 1) {
                Some(v) => Ok(Some(v.clone())),
                None => py::raise("IndexError", "list index out of range"),
            },
        }
    };
    let mut autos = load()?;
    let known = name.is_some_and(|n| autos.contains_key(n)); // `None in autos` is False
    let missing = || py::sys_exit(&format!("{shown}: no such automation"));
    match cmd {
        "list" => {
            for (n, a) in &autos {
                let on = if a.get("enabled").map_or(true, py::truthy) { "on " } else { "off" };
                println!(
                    "{} {on}  {} {} {}\n    {}",
                    py::ljust(n, 24),
                    py::ljust(py::gs(a, "schedule"), 15),
                    py::ljust(&provider(a)?, 8),
                    if alive(n) { "running" } else { "" },
                    py::head(py::gs(a, "prompt"), 100)
                );
            }
        }
        "add" | "edit" => {
            if (cmd == "add") == known {
                py::sys_exit(&format!("{shown}: {}", if cmd == "add" { "already exists" } else { "no such automation" }));
            }
            // add with no name saves under "null", as json.dumps writes a None key
            let key = name.unwrap_or("null").to_string();
            let mut a = match autos.get(&key).filter(|_| known) {
                Some(Value::Object(m)) => m.clone(),
                _ => Map::from_iter([("enabled".to_string(), json!(true))]),
            };
            for k in OPTS {
                if let Some(v) = opt(k)? {
                    a.insert(k.into(), json!(v));
                }
            }
            if args.iter().any(|x| x == "--enabled" || x == "--disabled") {
                a.insert("enabled".into(), json!(args.iter().any(|x| x == "--enabled")));
            }
            let a = Value::Object(a);
            if py::get_or(&a, "schedule").is_none() || py::get_or(&a, "prompt").is_none() {
                py::sys_exit("--schedule and --prompt are required");
            }
            due(py::gs(&a, "schedule"), &localtime(py::time()))?; // fails loudly on a bad cron
            autos.insert(key.clone(), a.clone());
            save(&autos)?;
            println!("{}", py::dumps_indent_n(&json!({key: a}), 2));
        }
        "show" => {
            if !known {
                missing();
            }
            println!("{}", py::dumps(&autos[name.unwrap()]));
        }
        "rm" => {
            if !known {
                missing();
            }
            autos.shift_remove(name.unwrap());
            save(&autos)?;
            println!("removed {}", name.unwrap());
        }
        "run" => {
            if !known {
                missing();
            }
            spawn(name.unwrap())?;
        }
        "exec" => execute(name)?,
        "runs" => {
            let f = dir().join("runs.jsonl");
            let mut rows = vec![];
            if f.exists() {
                for (i, l) in py::splitlines(&std::fs::read_to_string(&f)?).into_iter().enumerate() {
                    // Python: a JSONDecodeError traceback (a blank line included)
                    rows.push(py::loads(l).map_err(|e| format!("{} line {}: {e}", f.display(), i + 1))?);
                }
            }
            let rows: Vec<&Value> = rows.iter().filter(|r| name.is_none_or(|n| n.is_empty() || r["name"] == n)).collect();
            for r in &rows[rows.len().saturating_sub(20)..] {
                println!(
                    "{} {} {} {} {}s  {}",
                    py::strftime_local("%m-%d %H:%M", r["ts"].as_f64().unwrap_or(0.0)),
                    py::ljust(py::gs(r, "name"), 24),
                    py::ljust(py::gs(r, "status"), 8),
                    py::ljust(py::gs(r, "engine"), 6),
                    py::str_(&r["secs"]),
                    py::repr_str(py::tail(py::gs(r, "summary"), 80))
                );
            }
        }
        "tick" => tick()?,
        "install" => install()?,
        _ => py::sys_exit(DOC),
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tm(min: i64, hour: i64, mday: i64, mon: i64, wday: i64) -> Tm {
        Tm { min, hour, mday, mon, wday }
    }
    #[test]
    fn cron_is_pythons() {
        let thu = tm(0, 12, 12, 11, 4);
        assert!(due("0 12 */1 * 1", &thu).unwrap()); // */1 counts as restricted: OR
        assert!(!due("5/10 * * * *", &thu).unwrap()); // a/n is the single value a
        assert!(due("0 +1_2 * * *", &thu).unwrap());
        assert!(due("0 12-12-30 * * *", &thu).unwrap());
        assert!(due("0 0 * * 5-7", &tm(0, 0, 15, 11, 0)).unwrap()); // "7" in dow matches Sunday
        assert!(due("0 0 * * */7", &tm(0, 0, 15, 11, 0)).unwrap());
        assert!(due("0 12-10/-1 * * *", &thu).unwrap()); // negative step counts down: {12, 11}
        assert!(!due("1 x * * *", &thu).unwrap()); // minute misses first: the bad hour is never parsed
        assert!(due("0 x * * *", &thu).is_err());
        assert!(due("0 -5 * * *", &thu).is_err());
        assert!(due("*/0 * * * *", &thu).is_err());
    }
}
