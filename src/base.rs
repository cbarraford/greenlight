//! The top of greenlight.py that every script imports: paths, config, ME, the regexes, run(), and the
//! Orca / GitLab / GitHub CLI wrappers. Loaded once by init() before any subcommand runs, as Python's
//! import does (bad config.json fails every command; ME may hit the network).
use crate::py::{self, Res};
use fancy_regex::Regex as FRegex;
use regex::Regex;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{LazyLock, OnceLock};

/// The skill dir: lifecycles/, prompts/, examples/. $GREENLIGHT_ROOT, else the first ancestor of the
/// binary that has lifecycles/ (target/release/greenlight inside a checkout).
pub static ROOT: LazyLock<PathBuf> = LazyLock::new(|| {
    if let Some(r) = std::env::var_os("GREENLIGHT_ROOT").filter(|r| !r.is_empty()) {
        return PathBuf::from(r);
    }
    let exe = std::env::current_exe().unwrap_or_default();
    exe.ancestors().find(|d| d.join("lifecycles").is_dir()).map(PathBuf::from).unwrap_or_else(|| exe.parent().unwrap_or(&exe).into())
});
/// $GREENLIGHT_HOME, else ~/.greenlight (empty env counts as unset).
pub static STATE: LazyLock<PathBuf> = LazyLock::new(|| match std::env::var("GREENLIGHT_HOME") {
    Ok(h) if !h.is_empty() => PathBuf::from(h),
    _ => py::home().join(".greenlight"),
});

/// Only what differs between users. config.json's keys replace these whole (no deep merge).
pub fn defaults() -> Value {
    json!({
        "me": null, "review_bot": null, "agents": ["claude", "codex"], "harnesses": {}, "llm_harness": "claude",
        "merge_method": "merge", "require_human_approval": true, "adopt": {"cap": 10, "roles": ["author", "reviewer"]},
        "blocking": null, "suggest_rules": [], "name": "greenlight"
    })
}

static CONFIG_: OnceLock<Value> = OnceLock::new();
static ME_: OnceLock<String> = OnceLock::new();
static NOW_MS_: OnceLock<f64> = OnceLock::new();

/// greenlight.CONFIG
pub fn config() -> &'static Value {
    CONFIG_.get().expect("base::init not called")
}
/// CONFIG[k] (null when absent)
pub fn cfg(k: &str) -> &'static Value {
    config().get(k).unwrap_or(&Value::Null)
}
/// greenlight.BOT
pub fn bot() -> Option<&'static str> {
    cfg("review_bot").as_str()
}
/// greenlight.ME ("" when nothing knows who you are)
pub fn me() -> &'static str {
    ME_.get().map(String::as_str).unwrap_or("")
}
/// greenlight.NOW_MS: time.time()*1000, frozen at startup.
pub fn now_ms() -> f64 {
    *NOW_MS_.get_or_init(|| py::time() * 1000.0)
}

pub fn init() -> Res<()> {
    let mut c = defaults();
    match std::fs::read_to_string(STATE.join("config.json")) {
        Ok(s) => match py::loads(&s).map_err(|e| format!("{}: {e}", STATE.join("config.json").display()))? {
            Value::Object(m) => c.as_object_mut().unwrap().extend(m),
            other => {
                let t = match other {
                    Value::Null => "NoneType",
                    Value::Bool(_) => "bool",
                    Value::Number(n) if n.is_f64() => "float",
                    Value::Number(_) => "int",
                    Value::String(_) => "str",
                    _ => "list",
                };
                return py::raise("TypeError", format!("'{t}' object is not a mapping"));
            }
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    CONFIG_.set(c).ok();
    now_ms();
    let me = match std::env::var("GREENLIGHT_ME") {
        Ok(m) if !m.is_empty() => m,
        _ => match cfg("me") {
            v if py::truthy(v) => py::str_(v),
            _ => whoami(),
        },
    };
    ME_.set(me).ok();
    Ok(())
}

/// Your forge username: me.json, else glab's user, else gh's (cached). "" when neither answers.
fn whoami() -> String {
    let f = STATE.join("me.json");
    if let Some(m) = std::fs::read_to_string(&f).ok().and_then(|s| py::loads(&s).ok()).and_then(|v| v.get("me").cloned()) {
        return if m.is_null() { String::new() } else { py::str_(&m) };
    }
    for (cmd, key) in [(["glab", "api", "user"], "username"), (["gh", "api", "user"], "login")] {
        let cmd: Vec<String> = cmd.iter().map(|s| s.to_string()).collect();
        let Some(me) = run(&cmd, 20.0, 1).ok().and_then(|s| py::loads(&s).ok()).and_then(|v| v.get(key).cloned()) else { continue };
        let _ = std::fs::create_dir_all(&*STATE);
        let _ = std::fs::write(&f, py::dumps(&json!({"me": me})));
        return py::str_(&me);
    }
    String::new()
}

fn re(p: &str) -> Regex {
    Regex::new(p).unwrap()
}
pub static DEAD_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\b401\b|OAuth access token has been revoked|Please run /login|login expired|API key (?:has expired|is invalid)"));
pub static LIMIT_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(r"(?i)usage limit|limit reached|5-hour limit|weekly limit|out of (?:extra )?usage|rate limit exceeded|exceeded your current quota|insufficient_quota|429 Too Many Requests")
});
/// Instructions I add to a goal. A word joined to its neighbour by - or _ ("security-hold") is part of a name, not an instruction.
pub static KEEP_DRAFT_RE: LazyLock<FRegex> = LazyLock::new(|| FRegex::new(r"(?i)(?<![\w-])(stay|keep|remain)s? (?:a |as )?draft(?![\w-])").unwrap());
pub static HOLD_RE: LazyLock<FRegex> = LazyLock::new(|| FRegex::new(r"(?i)(?<![\w-])(?:hold|do ?n[o'’]t merge)(?![\w-])").unwrap());
pub static AUTOMATION_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\(automation\)"));
pub static REVISIT_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)revisit (\d{4}-\d\d-\d\d)"));
pub static PARKED_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\(parked\)"));
pub static ISSUE_GOAL_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^Merge MR for #(\d+)"));
pub static PLAN_RE: LazyLock<Regex> = LazyLock::new(|| re(r"^Plan:"));
pub static STATUS_LINE_RE: LazyLock<Regex> = LazyLock::new(|| re(r"dispatched \d{4}-\d\d-\d\d"));
pub static TRANSIENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(r"(?i)\b50[0234]\b|bad gateway|service unavailable|gateway time-?out|timed? ?out|connection (?:reset|refused)|unexpected EOF|TLS handshake")
});
/// Order matters in the report: who needs to act, most urgent first.
pub const OWNERS: [&str; 10] = ["me", "greenlight", "agent", "working", "ci", "reviewers", "author", "dependency", "parked", "done"];

pub fn s(xs: &[&str]) -> Vec<String> {
    xs.iter().map(|x| x.to_string()).collect()
}

/// stdout of cmd. tries > 1: retry timeouts and transient failures (GitLab blips) with short backoff.
/// Failure: RuntimeError "<cmd joined>: <stderr or stdout, stripped, first 300 chars>".
pub fn run(cmd: &[String], timeout: f64, tries: u32) -> Res<String> {
    for i in 0..tries {
        let last = i == tries - 1;
        match py::exec(cmd, None, Some(timeout), None, &[]) {
            Err(e) if py::kind(&*e) == "TimeoutExpired" && !last => {}
            Err(e) => return Err(e),
            Ok(o) if o.rc == 0 => return Ok(o.out),
            Ok(o) => {
                let err = py::strip(if o.err.is_empty() { &o.out } else { &o.err });
                if last || !TRANSIENT_RE.is_match(err) {
                    return py::err(format!("{}: {}", cmd.join(" "), py::head(err, 300)));
                }
            }
        }
        std::thread::sleep(std::time::Duration::from_secs(2 * 2u64.pow(i)));
    }
    unreachable!()
}

/// An Orca CLI call. Any other GREENLIGHT_ADE answers the same calls from another ADE, in Orca's shape.
pub fn orca(args: &[String]) -> Res<Value> {
    match std::env::var("GREENLIGHT_ADE").ok().as_deref() {
        None | Some("orca") => orca_cli(args, 120.0),
        Some("cmux") => crate::ade_cmux::call(args),
        Some("herdr") => crate::ade_herdr::call(args),
        Some("t3") => crate::ade_t3::call(args),
        Some(x) => py::raise("ModuleNotFoundError", format!("No module named 'ade_{x}'")),
    }
}
/// orca(*args) with &str args.
pub fn orca_s(args: &[&str]) -> Res<Value> {
    orca(&s(args))
}

/// The Orca CLI itself, whatever GREENLIGHT_ADE is.
pub fn orca_cli(args: &[String], timeout: f64) -> Res<Value> {
    // Bare `orca` on Linux outside an Orca terminal is the GNOME screen reader.
    let shim = py::home().join(".config/orca/linux-orca-cli-shim/orca"); // ssh / cron shells have no orca-ide on PATH
    let exe = match std::env::var("ORCA_CLI_COMMAND") {
        Ok(e) if !e.is_empty() => e,
        _ => py::which("orca-ide")
            .map(|p| p.display().to_string())
            .or_else(|| shim.exists().then(|| shim.display().to_string()))
            .unwrap_or_else(|| "orca".into()),
    };
    let mut cmd = vec![exe];
    cmd.extend(args.iter().cloned());
    if let Ok(e) = std::env::var("ORCA_ENVIRONMENT") {
        if !e.is_empty() {
            cmd.extend(["--environment".into(), e]);
        }
    }
    cmd.push("--json".into());
    let out = py::loads(&run(&cmd, timeout, 1)?)?;
    if !out.get("ok").is_some_and(py::truthy) {
        return py::err(format!("orca {}: {}", args.join(" "), py::str_(out.get("error").unwrap_or(&Value::Null))));
    }
    match out.get("result") {
        Some(r) => Ok(r.clone()),
        None => py::key_err("result"),
    }
}

pub fn gl(path: &str) -> Res<Value> {
    py::loads(&run(&s(&["glab", "api", path]), 45.0, 3)?) // worst case ~2.4 min
}

pub fn gl_all(path: &str) -> Res<Vec<Value>> {
    // ponytail: manual paging; `glab api --paginate` concatenates arrays inconsistently across versions
    let (mut out, mut page) = (vec![], 1);
    loop {
        let batch = gl(&format!("{path}{}per_page=100&page={page}", if path.contains('?') { '&' } else { '?' }))?;
        let n = match batch {
            Value::Array(a) => {
                let n = a.len();
                out.extend(a);
                n
            }
            // Python's `out += dict` adds its keys (inventory core §7.1); kept for parity.
            Value::Object(m) => {
                let n = m.len();
                out.extend(m.keys().map(|k| Value::String(k.clone())));
                n
            }
            other => return py::raise("TypeError", format!("'{}' object is not iterable", py::repr(&other))),
        };
        if n < 100 {
            return Ok(out);
        }
        page += 1;
    }
}

pub fn gh(args: &[&str]) -> Res<Value> {
    let mut cmd = vec!["gh".to_string()];
    cmd.extend(args.iter().map(|a| a.to_string()));
    py::loads(&run(&cmd, 45.0, 3)?)
}

/// urllib.parse.quote(project, safe="")
pub fn pid(project: &str) -> String {
    py::quote(project, "")
}

/// Orca projectId -> (forge, "owner/repo"), or None for repos greenlight can't track.
pub fn forge_project(project_id: &str) -> Option<(String, String)> {
    let (kind, rest) = project_id.split_once(':').unwrap_or((project_id, ""));
    if kind == "github" && !rest.is_empty() {
        return Some(("github".into(), rest.into()));
    }
    let (host, path) = rest.split_once('/').unwrap_or((rest, ""));
    if kind == "git" && (host == "gitlab.com" || host == "github.com") && !path.is_empty() {
        return Some((host.split('.').next().unwrap().into(), path.into()));
    }
    None
}

pub fn ref_(forge: &str, n: &Value) -> String {
    format!("{}{}", if forge == "github" { "#" } else { "!" }, py::str_(n))
}
