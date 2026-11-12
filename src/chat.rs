//! Port of scripts/chat.py. Menubar chat backend: one greenlight session on the greenlight host, resumed per message.
//!
//! Every command streams JSON lines (events, see Norm) for the Greenlight app:
//!   chat send [--model M] [--effort E] [--name N] < message
//!                                      # M: a claude model, or a harness name (codex|opencode|pi) for its default model
//!   chat compact [--model M]          # claude only
//!   chat new                           # forget every harness's session (Clear)
//!   chat stop                          # kill the running turn
//!   chat bg                            # one JSON line: this chat's background shells + working Orca agents
//!   chat bgstop <task_id>              # kill one background shell
use crate::base::{orca_s, ROOT};
use crate::harness;
use crate::py::{self, kill, killpg, setsid, Res};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::LazyLock;

/// $GREENLIGHT_HOME or "~/.greenlight" (empty counts as unset), not yet expanduser'd.
fn gl_home() -> String {
    std::env::var("GREENLIGHT_HOME").ok().filter(|h| !h.is_empty()).unwrap_or_else(|| "~/.greenlight".into())
}
/// os.path.join(a, b) for strings.
fn join(a: &str, b: &str) -> String {
    if a.is_empty() || a.ends_with('/') { format!("{a}{b}") } else { format!("{a}/{b}") }
}
// CHAT_DIR: tests use a scratch session
static DIR: LazyLock<PathBuf> = LazyLock::new(|| {
    py::expanduser(&std::env::var("CHAT_DIR").ok().filter(|d| !d.is_empty()).unwrap_or_else(|| join(&gl_home(), "chat")))
});
fn pidfile() -> PathBuf {
    DIR.join("pid")
}
fn bgfile() -> PathBuf {
    DIR.join("bg.json") // the running turn's background shells
}
const CODEX_WINDOW: i64 = 272000; // ponytail: codex doesn't report its window; fixed guess for the meter
fn prompt() -> String {
    format!(
        "You are greenlight (the user also calls you greenlight), talking with the user through their Mac menubar app. \
For anything about workspaces, MRs, agents or automations, follow {} and its memory notes. \
Never do work yourself (code, debugging, reviews, investigations, writing): dispatch it to an agent in the \
appropriate Orca workspace, creating one with a goal if none fits, then report back what you dispatched and where. \
AskUserQuestion is unavailable here: ask in plain text, one question at a time. \
Replies render as markdown in a narrow popover: keep them short, no tables or headings, MR links as markdown links. \
When you ask a question with a few likely answers, end with one final line: `Options: first | second | third`. \
Each message runs in a process that exits when you reply, so background tasks, Monitor and ScheduleWakeup die \
at the end of the turn: never use run_in_background, Monitor or ScheduleWakeup here. Leave follow-up watching to \
the Greenlight automation, or record it with `greenlight.py note <workspace> \"...\"`. \
A <task-notification> is never the user's message; the text after `USER MESSAGE (reply to this):` is. Always answer it.",
        ROOT.join("SKILL.md").display()
    )
}
const WRAP: &str = "USER MESSAGE (reply to this): ";
const RETRY_PREFIX: &str = "Your previous reply ignored the user's message. Answer it now: ";
// empty, or the model answering only a notification
static IGNORED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^\s*(no response requested\.?)?\s*$").unwrap());

const SIGTERM: i32 = 15;
const ESRCH: i32 = 3;

/// os.kill / os.killpg: ProcessLookupError (ESRCH) is Ok(false); any other errno raises.
fn signal(group: bool, pid: i32) -> Res<bool> {
    if unsafe { if group { killpg(pid, SIGTERM) } else { kill(pid, SIGTERM) } } == 0 {
        return Ok(true);
    }
    let e = std::io::Error::last_os_error();
    if e.raw_os_error() == Some(ESRCH) {
        return Ok(false);
    }
    py::raise(if e.raw_os_error() == Some(1) { "PermissionError" } else { "OSError" }, oserr(&e))
}
/// str(OSError): "[Errno N] <strerror>".
fn oserr(e: &std::io::Error) -> String {
    let s = e.to_string();
    let msg = s.split(" (os error").next().unwrap_or(&s);
    format!("[Errno {}] {msg}", e.raw_os_error().unwrap_or(0))
}
/// int(open(f).read()) for a pid file: ValueError when it isn't a number.
fn read_pid(f: &PathBuf) -> Res<i32> {
    let s = std::fs::read_to_string(f)?;
    py::strip(&s).parse().or_else(|_| py::raise("ValueError", format!("invalid literal for int() with base 10: {}", py::repr_str(&s))))
}

fn emit(e: Value) {
    let mut o = std::io::stdout().lock();
    let _ = writeln!(o, "{}", py::dumps(&e));
    let _ = o.flush();
}

/// " ".join(str(s).split()), cut to n code points with an ellipsis.
fn short(s: &str, n: usize) -> String {
    let s = py::split_ws(s).join(" ");
    if s.chars().count() <= n { s } else { format!("{}…", py::head(&s, n - 1)) }
}
fn shortv(v: &Value, n: usize) -> String {
    short(&py::str_(v), n)
}
/// str.capitalize()
fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    c.next().map_or(String::new(), |f| f.to_uppercase().chain(c.flat_map(char::to_lowercase)).collect())
}
/// d.get(k, default): a present null stays null.
fn get(d: &Value, k: &str, default: Value) -> Value {
    d.get(k).cloned().unwrap_or(default)
}
/// d[k]: KeyError when missing.
fn req<'a>(d: &'a Value, k: &str) -> Res<&'a Value> {
    match d.get(k) {
        Some(v) => Ok(v),
        None => py::key_err(k),
    }
}
/// `a or b or ...` over keys, else default.
fn first_of(d: &Value, ks: &[&str], default: Value) -> Value {
    ks.iter().find_map(|k| py::get_or(d, k)).cloned().unwrap_or(default)
}
/// d.get(k, 0) as a token count. ponytail: token counts are ints; anything else counts as 0.
fn int(d: &Value, k: &str) -> i64 {
    d.get(k).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(0)
}
fn empty() -> Value {
    Value::Object(Map::new())
}
/// `d.get(k) or {}`
fn obj(d: &Value, k: &str) -> Value {
    py::get_or(d, k).cloned().unwrap_or_else(empty)
}
fn basename(p: &str) -> &str {
    p.rsplit('/').next().unwrap_or(p)
}

/// A tool call as the one-line step the activity list shows: (title, detail).
fn step_title(name: &str, inp: &Value) -> (String, String) {
    let p = py::str_(&first_of(inp, &["file_path", "path", "notebook_path"], json!("")));
    let s = |v: Value| py::str_(&v);
    match name {
        "Bash" => (s(first_of(inp, &["description"], json!("Ran a shell command"))), s(get(inp, "command", json!("")))),
        "Read" => (format!("Read {}", basename(&p)), p),
        "Edit" | "Write" | "NotebookEdit" => (format!("Edited {}", basename(&p)), p),
        "Skill" => ("Loaded skill".into(), s(get(inp, "skill", json!("")))),
        "Task" | "Agent" => ("Started an agent".into(), s(get(inp, "description", json!("")))),
        "Grep" | "Glob" => ("Searched files".into(), s(get(inp, "pattern", json!("")))),
        "WebFetch" | "WebSearch" => (
            if name == "WebSearch" { "Searched the web" } else { "Fetched a page" }.into(),
            s(py::get_or(inp, "query").cloned().unwrap_or_else(|| get(inp, "url", json!("")))),
        ),
        _ => {
            let first = inp.as_object().and_then(|m| m.values().find_map(Value::as_str)).unwrap_or("");
            (name.rsplit("__").next().unwrap_or(name).replace('_', " "), first.into())
        }
    }
}

/// Each harness's JSON lines (claude stream-json, codex/opencode/pi json) → the app's events.
struct Norm {
    msg_t: Option<f64>, // current API message start, for tok/s
    chars: usize,
    last_tps: f64,
    context: i64,
    window: Value,
    text: Value,             // last text block, the fallback reply
    cmds: Map<String, Value>, // Bash tool_use id → command, so a background shell can be found and stopped
    shells: Map<String, Value>, // task_id → {id, title, cmd, started}
    error: Option<Value>,
}

impl Norm {
    fn new() -> Norm {
        Norm { msg_t: None, chars: 0, last_tps: 0.0, context: 0, window: Value::Null, text: json!(""), cmds: Map::new(), shells: Map::new(), error: None }
    }

    fn usage(u: &Value) -> i64 {
        int(u, "input_tokens") + int(u, "cache_read_input_tokens") + int(u, "cache_creation_input_tokens")
    }

    fn bg(&self) -> Value {
        json!({"e": "bg", "shells": self.shells.values().cloned().collect::<Vec<_>>()})
    }

    fn claude(&mut self, d: &Value) -> Res<Vec<Value>> {
        let (t, sub, mut out) = (py::gs(d, "type"), d.get("subtype").and_then(Value::as_str), vec![]);
        let sys = t == "system";
        if sys && sub == Some("status") && py::get_or(d, "status").is_some() {
            out.push(json!({"e": "status", "text": capitalize(&py::str_(req(d, "status")?))}));
        } else if sys && sub == Some("task_started") && py::get_or(d, "is_backgrounded").is_some() {
            let id = req(d, "task_id")?.clone();
            let cmd = d.get("tool_use_id").and_then(|k| self.cmds.get(&py::str_(k))).cloned().unwrap_or(json!(""));
            let sh = json!({"id": id, "title": first_of(d, &["description"], json!("Background shell")), "cmd": cmd, "started": py::time()});
            self.shells.insert(py::str_(&id), sh);
            out.push(self.bg());
        } else if sys && sub == Some("background_tasks_changed") {
            let mut live = HashSet::new();
            for x in d.get("tasks").and_then(Value::as_array).into_iter().flatten() {
                live.insert(py::str_(req(x, "task_id")?));
            }
            self.shells.retain(|k, _| live.contains(k));
            out.push(self.bg());
        } else if sys && sub == Some("compact_boundary") {
            self.context = int(req(d, "compact_metadata")?, "post_tokens");
            out.push(json!({"e": "usage", "context": self.context}));
        } else if t == "stream_event" {
            let ev = req(d, "event")?;
            let et = py::str_(req(ev, "type")?);
            if et == "message_start" {
                self.msg_t = Some(py::time());
                self.chars = 0;
                let u = Self::usage(&get(req(ev, "message")?, "usage", empty()));
                if u != 0 {
                    self.context = u;
                }
                out.push(json!({"e": "usage", "context": self.context}));
            } else if et == "content_block_start" && req(req(ev, "content_block")?, "type")? == "text" {
                out.push(json!({"e": "text_reset"}));
            } else if et == "content_block_delta" {
                let dl = req(ev, "delta")?;
                let s = py::str_(&first_of(dl, &["text", "thinking", "partial_json"], json!("")));
                self.chars += s.chars().count();
                match dl.get("type").and_then(Value::as_str) {
                    Some("text_delta") => out.push(json!({"e": "text", "delta": s})),
                    Some("thinking_delta") => out.push(json!({"e": "status", "text": "Thinking"})),
                    _ => {}
                }
                if let Some(t0) = self.msg_t {
                    if py::time() - self.last_tps > 0.5 {
                        // live estimate, ~4 chars a token
                        self.last_tps = py::time();
                        out.push(json!({"e": "usage", "tps": py::round(self.chars as f64 / 4.0 / (py::time() - t0).max(0.2), 1)}));
                    }
                }
            } else if et == "message_delta" && self.msg_t.is_some() {
                let n = int(&get(ev, "usage", empty()), "output_tokens");
                out.push(json!({"e": "usage", "tps": py::round(n as f64 / (py::time() - self.msg_t.unwrap()).max(0.2), 1)}));
            }
        } else if t == "assistant" {
            for c in get(req(d, "message")?, "content", json!([])).as_array().into_iter().flatten() {
                match c.get("type").and_then(Value::as_str) {
                    Some("tool_use") => {
                        let id = req(c, "id")?;
                        let inp = get(c, "input", empty());
                        self.cmds.insert(py::str_(id), get(&inp, "command", json!("")));
                        let (title, detail) = step_title(&py::str_(req(c, "name")?), &inp);
                        out.push(json!({"e": "step", "id": id, "title": short(&title, 80), "detail": short(&detail, 160)}));
                    }
                    Some("text") => self.text = req(c, "text")?.clone(),
                    _ => {}
                }
            }
        } else if t == "user" && d.get("message").and_then(|m| m.get("content")).is_some_and(Value::is_array) {
            for c in d["message"]["content"].as_array().unwrap() {
                if c.get("type").and_then(Value::as_str) == Some("tool_result") {
                    let res = match c.get("content") {
                        Some(Value::Array(a)) => json!(a.iter().filter(|x| x.is_object()).map(|x| py::str_(&get(x, "text", json!("")))).collect::<Vec<_>>().join(" ")),
                        Some(v) => v.clone(),
                        None => Value::Null,
                    };
                    let res = if py::truthy(&res) { res } else { json!("") };
                    out.push(json!({"e": "step_done", "id": req(c, "tool_use_id")?, "ok": !c.get("is_error").is_some_and(py::truthy), "out": shortv(&res, 120)}));
                }
            }
        } else if t == "result" {
            let mu = py::get_or(d, "modelUsage").and_then(Value::as_object).and_then(|m| m.values().next().cloned()).unwrap_or_else(empty);
            if let Some(w) = py::get_or(&mu, "contextWindow") {
                self.window = w.clone();
            }
            out.push(json!({"e": "done", "ok": !d.get("is_error").is_some_and(py::truthy), "text": first_of(d, &["result"], self.text.clone()),
                            "context": self.context, "window": self.window, "ms": get(d, "duration_ms", Value::Null)}));
        }
        Ok(out)
    }

    fn codex(&mut self, d: &Value) -> Res<Vec<Value>> {
        let (t, it, mut out) = (py::gs(d, "type"), get(d, "item", empty()), vec![]);
        let cmd_exec = it.get("type").and_then(Value::as_str) == Some("command_execution");
        if t == "item.started" && cmd_exec {
            let c = py::str_(req(&it, "command")?);
            let cmd = c.splitn(2, " -lc ").last().unwrap_or("").trim_matches('\'');
            out.push(json!({"e": "step", "id": req(&it, "id")?, "title": "Ran a shell command", "detail": short(cmd, 160)}));
        } else if t == "item.completed" && cmd_exec {
            let ok = it.get("exit_code").is_some_and(|v| v.as_f64() == Some(0.0) || v == &json!(false));
            out.push(json!({"e": "step_done", "id": req(&it, "id")?, "ok": ok, "out": shortv(&get(&it, "aggregated_output", json!("")), 120)}));
        } else if t == "item.completed" && it.get("type").and_then(Value::as_str) == Some("agent_message") {
            self.text = req(&it, "text")?.clone();
            out.push(json!({"e": "text_reset"}));
            out.push(json!({"e": "text", "delta": self.text}));
        } else if t == "item.started" {
            out.push(json!({"e": "status", "text": capitalize(&py::str_(&get(&it, "type", json!("working"))).replace('_', " "))}));
        } else if t == "error" && py::str_(&get(d, "message", json!(""))).starts_with("Reconnecting") {
            // a retry, not the end
            out.push(json!({"e": "status", "text": shortv(req(d, "message")?, 80)}));
        } else if t == "turn.completed" {
            self.context = int(&get(d, "usage", empty()), "input_tokens");
            out.push(json!({"e": "done", "ok": true, "text": self.text, "context": self.context, "window": CODEX_WINDOW, "ms": null}));
        } else if t == "turn.failed" || t == "error" {
            let text = py::str_(&first_of(d, &["error"], get(d, "message", Value::Null)));
            out.push(json!({"e": "done", "ok": false, "text": text, "context": self.context, "window": CODEX_WINDOW, "ms": null}));
        }
        Ok(out)
    }

    /// `opencode run --format json`: whole parts, a tool only once it finished; no final event (run() adds the done).
    fn opencode(&mut self, d: &Value) -> Res<Vec<Value>> {
        let (t, pt, mut out) = (py::gs(d, "type"), obj(d, "part"), vec![]);
        if t == "tool_use" {
            let st = obj(&pt, "state");
            let (title, detail) = step_title(&capitalize(&py::str_(&get(&pt, "tool", json!("tool")))), &obj(&st, "input"));
            let id = get(&pt, "id", Value::Null);
            out.push(json!({"e": "step", "id": id, "title": shortv(&first_of(&st, &["title"], json!(title)), 80), "detail": short(&detail, 160)}));
            out.push(json!({"e": "step_done", "id": id, "ok": st.get("status").and_then(Value::as_str) == Some("completed"),
                            "out": shortv(&first_of(&st, &["output", "error"], json!("")), 120)}));
        } else if t == "text" {
            self.text = get(&pt, "text", json!(""));
            out.push(json!({"e": "text_reset"}));
            out.push(json!({"e": "text", "delta": self.text}));
        } else if t == "reasoning" {
            out.push(json!({"e": "status", "text": "Thinking"}));
        } else if t == "step_finish" {
            let tk = obj(&pt, "tokens");
            let cache = obj(&tk, "cache");
            let n = int(&tk, "input") + int(&cache, "read") + int(&cache, "write");
            if n != 0 {
                self.context = n;
            }
            out.push(json!({"e": "usage", "context": self.context}));
        } else if t == "error" {
            let e = obj(d, "error");
            let text = py::str_(&py::get_or(&obj(&e, "data"), "message").or_else(|| py::get_or(&e, "name")).cloned().unwrap_or(e.clone()));
            out.push(json!({"e": "done", "ok": false, "text": text, "context": self.context, "window": null, "ms": null}));
        }
        Ok(out)
    }

    /// `pi --mode json`: agent session events.
    fn pi(&mut self, d: &Value) -> Res<Vec<Value>> {
        let (t, mut out) = (py::gs(d, "type"), vec![]);
        let (m, ev) = (obj(d, "message"), obj(d, "assistantMessageEvent"));
        let et = ev.get("type").and_then(Value::as_str);
        if t == "message_update" && et == Some("text_start") {
            out.push(json!({"e": "text_reset"}));
        } else if t == "message_update" && et == Some("text_delta") {
            out.push(json!({"e": "text", "delta": get(&ev, "delta", json!(""))}));
        } else if t == "message_update" && et == Some("thinking_delta") {
            out.push(json!({"e": "status", "text": "Thinking"}));
        } else if t == "message_end" && m.get("role").and_then(Value::as_str) == Some("assistant") {
            let u = obj(&m, "usage");
            let n = int(&u, "input") + int(&u, "cacheRead") + int(&u, "cacheWrite");
            if n != 0 {
                self.context = n;
            }
            let txt: String = get(&m, "content", json!([]))
                .as_array()
                .into_iter()
                .flatten()
                .filter(|c| c.get("type").and_then(Value::as_str) == Some("text"))
                .map(|c| py::str_(&get(c, "text", json!(""))))
                .collect();
            if !txt.is_empty() {
                self.text = json!(txt);
            }
            if let Some(r @ ("error" | "aborted")) = m.get("stopReason").and_then(Value::as_str) {
                self.error = Some(first_of(&m, &["errorMessage"], json!(r)));
            }
            out.push(json!({"e": "usage", "context": self.context}));
        } else if t == "tool_execution_start" {
            let (title, detail) = step_title(&capitalize(&py::str_(&get(d, "toolName", json!("tool")))), &obj(d, "args"));
            out.push(json!({"e": "step", "id": get(d, "toolCallId", Value::Null), "title": short(&title, 80), "detail": short(&detail, 160)}));
        } else if t == "tool_execution_end" {
            let r = match d.get("result") {
                Some(r @ Value::Object(_)) => get(r, "content", json!([]))
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|c| c.is_object())
                    .map(|c| py::str_(&get(c, "text", json!(""))))
                    .collect::<Vec<_>>()
                    .join(" "),
                Some(r) if py::truthy(r) => py::str_(r),
                _ => String::new(),
            };
            out.push(json!({"e": "step_done", "id": get(d, "toolCallId", Value::Null), "ok": !d.get("isError").is_some_and(py::truthy), "out": short(&r, 120)}));
        } else if t == "agent_end" {
            let err = self.error.clone().filter(py::truthy);
            out.push(json!({"e": "done", "ok": err.is_none(), "text": err.unwrap_or(self.text.clone()), "context": self.context, "window": null, "ms": null}));
        }
        Ok(out)
    }
}

fn session(engine: &str, val: Option<&str>) -> Res<Option<String>> {
    let f = DIR.join(if engine == "claude" { "session".into() } else { format!("session_{engine}") });
    if let Some(v) = val {
        std::fs::write(&f, v)?;
    }
    Ok(if f.exists() { Some(py::strip(&std::fs::read_to_string(&f)?).to_string()) } else { None })
}

fn rm(f: &PathBuf) -> Res<()> {
    if f.exists() {
        std::fs::remove_file(f)?;
    }
    Ok(())
}

/// Run one turn, stream events. Returns (sid or None, error or None or "retry").
/// check: a done whose reply ignored the user (IGNORED) isn't emitted; the process is killed and "retry" returned.
fn run(cmd: &[String], engine: &str, text: &str, norm: &mut Norm, check: bool) -> Res<(Option<Value>, Option<String>)> {
    use std::os::unix::process::{CommandExt, ExitStatusExt};
    let mut c = Command::new(&cmd[0]);
    c.args(&cmd[1..]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).current_dir(&*DIR);
    // start_new_session: pgid == pid, so `stop` can killpg the whole turn; no controlling terminal
    unsafe {
        c.pre_exec(|| {
            setsid();
            Ok(())
        });
    }
    let mut p = match c.spawn() {
        Ok(p) => p,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return py::raise("FileNotFoundError", format!("[Errno 2] No such file or directory: {}", py::repr_str(&cmd[0])))
        }
        Err(e) => return py::raise("OSError", format!("{}: {}", oserr(&e), py::repr_str(&cmd[0]))),
    };
    let pid = p.id() as i32;
    // drained alongside stdout: a child writing >64 KB of stderr no longer deadlocks the turn (BUGS.md)
    let errt = p.stderr.take().map(|mut e| std::thread::spawn(move || {
        let mut b = vec![];
        let _ = e.read_to_end(&mut b);
        b
    }));
    std::fs::write(pidfile(), pid.to_string())?;
    {
        let mut w = p.stdin.take().unwrap();
        if let Err(e) = w.write_all(text.as_bytes()) {
            return py::raise("OSError", oserr(&e));
        }
    }
    let (mut sid, mut done) = (None, false);
    let mut rd = BufReader::new(p.stdout.take().unwrap());
    let mut buf = vec![];
    loop {
        buf.clear();
        if rd.read_until(b'\n', &mut buf)? == 0 {
            break;
        }
        let Ok(line) = std::str::from_utf8(&buf) else {
            return py::raise("ValueError", format!("{engine} wrote a line that is not UTF-8"));
        };
        let Ok(d) = py::loads(line) else { continue };
        if !d.is_object() {
            return py::raise("TypeError", format!("{engine} wrote a JSON line that is not an object: {}", py::head(py::strip(line), 160)));
        }
        let found = ["session_id", "thread_id", "sessionID"].iter().find_map(|k| py::get_or(&d, k)).cloned();
        let found = found.or_else(|| if py::gs(&d, "type") == "session" { py::get_or(&d, "id").cloned() } else { None });
        if found.is_some() {
            sid = found;
        }
        let evs = match engine {
            "claude" => norm.claude(&d)?,
            "codex" => norm.codex(&d)?,
            "opencode" => norm.opencode(&d)?,
            _ => norm.pi(&d)?,
        };
        let ignored = |e: &Value| {
            let t = e.get("text").filter(|t| py::truthy(t)).map_or(String::new(), py::str_);
            py::gs(e, "e") == "done" && py::truthy(&e["ok"]) && IGNORED.is_match(&t)
        };
        if check && evs.iter().any(ignored) {
            signal(true, pid)?;
            p.wait()?;
            rm(&pidfile())?;
            return Ok((sid, Some("retry".into())));
        }
        for e in evs {
            let kind = py::gs(&e, "e");
            if kind == "bg" {
                // not for the stream: `chat bg` serves it
                std::fs::write(bgfile(), py::dumps(&e["shells"]))?;
                continue;
            }
            if kind == "done" && done {
                continue; // opencode can report one failure twice
            }
            done |= kind == "done";
            emit(e);
        }
    }
    let st = p.wait()?;
    let rc = st.code().unwrap_or_else(|| -st.signal().unwrap_or(0));
    if !done && rc == 0 {
        // opencode has no final event: its clean exit is the done
        emit(json!({"e": "done", "ok": true, "text": norm.text, "context": norm.context, "window": norm.window, "ms": null}));
        done = true;
    }
    for f in [pidfile(), bgfile()] {
        rm(&f)?; // the process is gone, and its shells with it
    }
    if !done && rc != 0 {
        if rc == -15 || rc == 143 {
            return Ok((sid, Some("Stopped".into()))); // SIGTERM from `chat stop`
        }
        let err = errt.and_then(|t| t.join().ok()).unwrap_or_default();
        let err = String::from_utf8_lossy(&err).replace("\r\n", "\n").replace('\r', "\n");
        let err = py::strip(&err);
        return Ok((sid, Some(if err.is_empty() { format!("exit {rc}") } else { err.to_string() })));
    }
    Ok((sid, None))
}

static PASTED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"</?pasted_content[^>]*>").unwrap());
static GOAL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^/goal\s+").unwrap());

/// An Orca agent's one-line task: its title, else its prompt without the paste wrapper or a /goal prefix.
fn agent_title(a: &Value) -> String {
    let t = py::str_(&first_of(a, &["taskTitle", "displayName", "prompt"], json!("")));
    let t = PASTED.replace_all(&t, "");
    let t = GOAL.replace(py::strip(&t), "").into_owned();
    let t = short(t.split('\n').next().unwrap_or(""), 300); // the app truncates to fit its width
    if t.is_empty() { "Agent".into() } else { t }
}

fn load_shells() -> Res<Vec<Value>> {
    Ok(match py::loads(&std::fs::read_to_string(bgfile())?)? {
        Value::Array(a) => a,
        _ => vec![],
    })
}

/// This chat's live background shells and every working Orca agent, for the app's footer chip.
fn background() -> Res<Value> {
    let shells = if bgfile().exists() && pidfile().exists() { load_shells()? } else { vec![] };
    let mut agents = vec![];
    let mut scan = || -> Res<()> {
        let r = orca_s(&["worktree", "ps"])?;
        for w in req(&r, "worktrees")?.as_array().into_iter().flatten() {
            for a in get(w, "agents", json!([])).as_array().into_iter().flatten() {
                if a.get("state").and_then(Value::as_str) == Some("working") {
                    let started = py::get_or(a, "stateStartedAt").and_then(Value::as_f64).unwrap_or(0.0) / 1000.0;
                    agents.push(json!({"title": agent_title(a), "agent": get(a, "agentType", json!("agent")),
                                       "workspace": get(w, "displayName", json!("")), "started": started}));
                }
            }
        }
        Ok(())
    };
    if let Err(e) = scan() {
        // the ADE is down: show shells only
        if !["OSError", "FileNotFoundError", "PermissionError", "ValueError", "JSONDecodeError", "KeyError", "RuntimeError", "TimeoutExpired"].contains(&py::kind(&*e)) {
            return Err(e);
        }
    }
    let shells: Vec<Value> = shells
        .into_iter()
        .map(|s| Value::Object(s.as_object().cloned().unwrap_or_default().into_iter().filter(|(k, _)| k != "cmd").collect()))
        .collect();
    Ok(json!({"shells": shells, "agents": agents}))
}

/// str.split(None, 2)
fn split3(l: &str) -> Vec<&str> {
    let mut out = vec![];
    let mut rest = py::lstrip(l);
    while out.len() < 2 && !rest.is_empty() {
        let i = rest.find(py::is_space).unwrap_or(rest.len());
        out.push(&rest[..i]);
        rest = py::lstrip(&rest[i..]);
    }
    if !rest.is_empty() {
        out.push(rest);
    }
    out
}

/// Kill one background shell: the claude process's descendants whose command line holds the shell's command.
fn stop_shell(task_id: &str) -> Res<bool> {
    let shells = if bgfile().exists() { load_shells()? } else { vec![] };
    let mut cmd = String::new();
    for s in &shells {
        if req(s, "id")?.as_str() == Some(task_id) {
            cmd = py::str_(req(s, "cmd")?);
            break;
        }
    }
    // ponytail: longest quote-free chunk, quoting is rewritten on the way to the shell
    let mut key = "";
    for part in cmd.split(['\'', '"', '\\']) {
        if part.chars().count() > key.chars().count() {
            key = part;
        }
    }
    let key = py::strip(key);
    if key.is_empty() || !pidfile().exists() {
        return Ok(false);
    }
    let ps = py::exec(&crate::base::s(&["ps", "-eo", "pid=,ppid=,args="]), None, None, None, &[])?;
    let mut kids: BTreeMap<i64, Vec<(i64, String)>> = BTreeMap::new();
    for l in py::splitlines(&ps.out) {
        let r = split3(l);
        if r.len() == 3 {
            if let (Ok(pid), Ok(ppid)) = (r[0].parse(), r[1].parse()) {
                kids.entry(ppid).or_default().push((pid, r[2].to_string()));
            }
        }
    }
    let args: BTreeMap<i64, &str> = kids.values().flatten().map(|(p, a)| (*p, a.as_str())).collect();
    fn tree(pid: i64, kids: &BTreeMap<i64, Vec<(i64, String)>>) -> Vec<i64> {
        let mut v = vec![pid];
        for (k, _) in kids.get(&pid).into_iter().flatten() {
            v.extend(tree(*k, kids));
        }
        v
    }
    let root = read_pid(&pidfile())? as i64;
    let hits: Vec<i64> = tree(root, &kids)[1..].iter().copied().filter(|p| args.get(p).is_some_and(|a| a.contains(key))).collect();
    let all: HashSet<i64> = hits.iter().flat_map(|h| tree(*h, &kids)).collect();
    for p in all {
        signal(false, p as i32)?;
    }
    Ok(!hits.is_empty())
}

fn turn(text: &str, model: Option<&str>, effort: Option<&str>, compact: bool, name: Option<&str>) -> Res<()> {
    let (engine, model) = match model {
        Some(m) if harness::H.iter().any(|h| h.0 == m) => (m, None),
        _ => ("claude", model),
    };
    let mut prompt = prompt();
    if let Some(n) = name.filter(|n| !n.is_empty() && n.to_lowercase() != "greenlight") {
        prompt += &format!(" The user has named you {n} in the app; answer to that name.");
    }
    let rules = py::expanduser(&join(&gl_home(), "instructions.md"));
    if rules.exists() {
        // the user's standing instructions (Settings → Model), read every turn
        prompt += &format!("\n\nThe user's standing instructions, which override the defaults above:\n{}", py::strip(&std::fs::read_to_string(&rules)?));
    }
    let mut sid = session(engine, None)?;
    let text = if compact { text.to_string() } else { format!("{WRAP}{text}") };
    // a harness without a system-prompt flag gets the prompt in its first message
    let mk = |s: Option<&str>, t: &str| harness::argv(engine, t, model, if compact { None } else { effort }, s, Some(&prompt), true);
    emit(json!({"e": "start", "engine": engine, "model": model.filter(|m| !m.is_empty()).unwrap_or("default")}));
    let mut norm = Norm::new();
    let first = mk(sid.as_deref(), &text).and_then(|(cmd, t)| run(&cmd, engine, &t, &mut norm, !compact));
    let (mut new_sid, mut err) = match first {
        Ok(r) => r,
        Err(e) if ["ValueError", "OSError", "FileNotFoundError", "PermissionError"].contains(&py::kind(&*e)) => {
            // unknown harness, pi without bypass, binary missing
            emit(json!({"e": "done", "ok": false, "text": e.to_string(), "context": 0, "window": null, "ms": null}));
            return Ok(());
        }
        Err(e) => return Err(e),
    };
    if err.as_deref() == Some("retry") {
        // once only: the retry runs unchecked
        emit(json!({"e": "status", "text": "Reply ignored your message, asking again"}));
        norm.text = json!("");
        let s = new_sid.as_ref().map(py::str_).or(sid.clone());
        let (cmd, t) = mk(s.as_deref(), &format!("{RETRY_PREFIX}{text}"))?;
        (new_sid, err) = run(&cmd, engine, &t, &mut norm, false)?;
        sid = None; // don't fall into the session-lost retry below
    }
    if err.is_some() && sid.as_deref().is_some_and(|s| !s.is_empty()) && !compact && err.as_deref() != Some("Stopped") {
        // session gone or corrupt: start over rather than fail
        emit(json!({"e": "status", "text": "Session lost, starting a new one"}));
        let (cmd, t) = mk(None, &text)?;
        (new_sid, err) = run(&cmd, engine, &t, &mut norm, false)?;
    }
    if let Some(s) = &new_sid {
        session(engine, Some(&py::str_(s)))?;
    }
    if let Some(e) = err {
        emit(json!({"e": "done", "ok": false, "text": short(&e, 500), "context": norm.context, "window": norm.window, "ms": null}));
    }
    Ok(())
}

pub fn main(args: Vec<String>) -> Res<i32> {
    std::fs::create_dir_all(&*DIR)?;
    let cmd = args.first().map_or("send", String::as_str);
    let opt = |k: &str| -> Res<Option<String>> {
        match args.iter().position(|a| a == k) {
            None => Ok(None),
            Some(i) => match args.get(i + 1) {
                Some(v) => Ok(Some(v.clone())),
                None => py::raise("IndexError", "list index out of range"),
            },
        }
    };
    match cmd {
        "new" => {
            for f in std::iter::once("session".to_string()).chain(harness::H.iter().map(|h| format!("session_{}", h.0))) {
                rm(&DIR.join(f))?;
            }
        }
        "stop" => {
            if pidfile().exists() {
                match read_pid(&pidfile()) {
                    Ok(p) => {
                        signal(true, p)?;
                    }
                    Err(e) if py::kind(&*e) == "ValueError" => {}
                    Err(e) => return Err(e),
                }
            }
        }
        "bg" => println!("{}", py::dumps(&background()?)),
        "bgstop" => {
            let Some(id) = args.get(1) else { return py::raise("IndexError", "list index out of range") };
            println!("{}", py::dumps(&json!({"ok": stop_shell(id)?})));
        }
        "compact" => {
            if session("claude", None)?.is_none_or(|s| s.is_empty()) {
                emit(json!({"e": "done", "ok": true, "text": "Nothing to compact yet.", "context": 0, "window": null, "ms": null}));
            } else {
                turn("/compact", opt("--model")?.as_deref(), None, true, None)?;
            }
        }
        _ => {
            let mut s = String::new();
            std::io::stdin().read_to_string(&mut s)?;
            turn(py::strip(&s), opt("--model")?.as_deref(), opt("--effort")?.as_deref(), false, opt("--name")?.as_deref())?;
        }
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn helpers() {
        assert_eq!(short("a  b\n c", 160), "a b c");
        assert_eq!(short("abcdef", 4), "abc…");
        assert_eq!(capitalize("API retry"), "Api retry");
        assert_eq!(split3("  12  1 a  b  "), vec!["12", "1", "a  b  "]);
        assert!(IGNORED.is_match(" No response requested.\n"));
        assert!(!IGNORED.is_match("hi"));
    }
}
