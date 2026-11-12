//! The Python behaviors the scripts' output depends on, so the port stays byte-compatible:
//! json.dumps (both styles, ensure_ascii), float repr/round, str methods, shlex/html/url quoting,
//! subprocess.run with a timeout, the wall clock (GREENLIGHT_NOW), and the three ways a script dies.
use serde_json::{Map, Value};
use std::fmt::Write as _;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub type Res<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub type Obj = Map<String, Value>;

/// A Python exception: `kind` is its class name (callers that catch one kind check it), Display is str(e).
#[derive(Debug, Clone)]
pub struct PyErr {
    pub kind: &'static str,
    pub msg: String,
}
impl std::fmt::Display for PyErr {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str(&self.msg)
    }
}
impl std::error::Error for PyErr {}

/// `raise <kind>(msg)`
pub fn raise<T>(kind: &'static str, msg: impl Into<String>) -> Res<T> {
    Err(Box::new(PyErr { kind, msg: msg.into() }))
}
/// RuntimeError(msg): the scripts' usual "this failed" error.
pub fn err<T>(msg: impl Into<String>) -> Res<T> {
    raise("RuntimeError", msg)
}
/// The exception's class name ("OSError" for anything that isn't a PyErr: io errors and the like).
pub fn kind(e: &(dyn std::error::Error + Send + Sync + 'static)) -> &'static str {
    e.downcast_ref::<PyErr>().map_or("OSError", |p| p.kind)
}
/// KeyError's str() quotes the key: `'x'`.
pub fn key_err<T>(k: &str) -> Res<T> {
    raise("KeyError", repr_str(k))
}

// ---- How a script dies. Use these three, nothing else. ----

/// Uncaught exception (traceback): `Error: <str(e)>` on stderr, rc 1. main() does this for any Err.
pub fn die_traceback(e: &dyn std::fmt::Display) -> ! {
    eprintln!("Error: {e}");
    std::process::exit(1)
}
/// `sys.exit("msg")`: the bare message on stderr, rc 1.
pub fn sys_exit(msg: &str) -> ! {
    eprintln!("{msg}");
    std::process::exit(1)
}
/// argparse's `parser.error(msg)`: `<prog>: error: <msg>` on stderr, rc 2 (prog = "adopt", "blocking", ...).
/// ponytail: no usage block; the harness strips Python's.
pub fn argparse_exit(prog: &str, msg: &str) -> ! {
    eprintln!("{prog}: error: {msg}");
    std::process::exit(2)
}

// ---- JSON ----

pub fn loads(s: &str) -> Res<Value> {
    serde_json::from_str(s).map_err(|e| PyErr { kind: "JSONDecodeError", msg: e.to_string() }.into())
}
/// json.dumps(v): `, ` and `: ` separators, ensure_ascii.
pub fn dumps(v: &Value) -> String {
    let mut o = String::new();
    write_json(v, None, 0, &mut o);
    o
}
/// json.dumps(v, indent=1)
pub fn dumps_indent(v: &Value) -> String {
    let mut o = String::new();
    write_json(v, Some(1), 0, &mut o);
    o
}
/// json.dumps(v, indent=n) for any n.
pub fn dumps_indent_n(v: &Value, n: usize) -> String {
    let mut o = String::new();
    write_json(v, Some(n), 0, &mut o);
    o
}

fn write_json(v: &Value, ind: Option<usize>, lvl: usize, o: &mut String) {
    let nl = |o: &mut String, l: usize| {
        if let Some(n) = ind {
            o.push('\n');
            o.extend(std::iter::repeat(' ').take(n * l));
        }
    };
    let sep = if ind.is_some() { "," } else { ", " };
    match v {
        Value::Null => o.push_str("null"),
        Value::Bool(b) => o.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => o.push_str(&num_repr(n)),
        Value::String(s) => esc(s, o),
        Value::Array(a) if a.is_empty() => o.push_str("[]"),
        Value::Object(m) if m.is_empty() => o.push_str("{}"),
        Value::Array(a) => {
            o.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    o.push_str(sep);
                }
                nl(o, lvl + 1);
                write_json(x, ind, lvl + 1, o);
            }
            nl(o, lvl);
            o.push(']');
        }
        Value::Object(m) => {
            o.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    o.push_str(sep);
                }
                nl(o, lvl + 1);
                esc(k, o);
                o.push_str(": ");
                write_json(x, ind, lvl + 1, o);
            }
            nl(o, lvl);
            o.push('}');
        }
    }
}

fn esc(s: &str, o: &mut String) {
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            '\u{8}' => o.push_str("\\b"),
            '\u{c}' => o.push_str("\\f"),
            ' '..='~' => o.push(c),
            _ => {
                let mut b = [0u16; 2];
                for u in c.encode_utf16(&mut b) {
                    let _ = write!(o, "\\u{u:04x}");
                }
            }
        }
    }
    o.push('"');
}

fn num_repr(n: &serde_json::Number) -> String {
    match n.as_f64() {
        Some(f) if n.is_f64() => float_json(f),
        _ => n.to_string(),
    }
}
fn float_json(f: f64) -> String {
    if f.is_nan() {
        "NaN".into()
    } else if f.is_infinite() {
        if f > 0.0 { "Infinity" } else { "-Infinity" }.into()
    } else {
        float_repr(f)
    }
}

/// repr(float) / str(float): shortest round-trip digits, exponent form outside 1e-4 <= |x| < 1e16.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    let s = format!("{x:e}"); // shortest round-trip: "-1.5e-5", "1e16"
    let (mant, exp) = s.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let neg = mant.starts_with('-');
    let digits: String = mant.trim_start_matches('-').replace('.', "");
    let n = digits.len() as i32;
    let body = if (-4..16).contains(&exp) {
        if exp >= n - 1 {
            format!("{digits}{}.0", "0".repeat((exp - (n - 1)) as usize))
        } else if exp >= 0 {
            format!("{}.{}", &digits[..=exp as usize], &digits[exp as usize + 1..])
        } else {
            format!("0.{}{digits}", "0".repeat((-exp - 1) as usize))
        }
    } else {
        let m = if n == 1 { digits.clone() } else { format!("{}.{}", &digits[..1], &digits[1..]) };
        format!("{m}e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs())
    };
    if neg { format!("-{body}") } else { body }
}

/// round(x, nd) for floats: the exact binary value rounded half-even, as CPython does.
pub fn round(x: f64, nd: usize) -> f64 {
    format!("{x:.nd$}").parse().unwrap()
}

/// str(v) for a JSON value as Python would print the decoded object.
pub fn str_(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        _ => repr(v),
    }
}
/// repr(v): None/True/False, 'str' quoting, Python float repr, {'k': v}.
pub fn repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(n) => match n.as_f64() {
            Some(f) if n.is_f64() => float_repr(f),
            _ => n.to_string(),
        },
        Value::String(s) => repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => {
            format!("{{{}}}", m.iter().map(|(k, x)| format!("{}: {}", repr_str(k), repr(x))).collect::<Vec<_>>().join(", "))
        }
    }
}
pub fn repr_str(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut o = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if c == q => {
                o.push('\\');
                o.push(c)
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                let _ = write!(o, "\\x{:02x}", c as u32);
            }
            c => o.push(c),
        }
    }
    o.push(q);
    o
}
/// repr(list of str), e.g. for TimeoutExpired's message.
pub fn repr_list(xs: &[String]) -> String {
    format!("[{}]", xs.iter().map(|s| repr_str(s)).collect::<Vec<_>>().join(", "))
}

/// Python truthiness of a JSON value.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}
/// `d.get(k) or default` style access: the value if truthy, else None.
pub fn get_or<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.get(k).filter(|x| truthy(x))
}
/// The str at `k`, or "" (missing, null, or not a string).
pub fn gs<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

// ---- str methods ----

/// str.isspace() per char (Rust's is_whitespace misses \x1c-\x1f).
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
/// str.strip()
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}
pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}
pub fn lstrip(s: &str) -> &str {
    s.trim_start_matches(is_space)
}
/// str.split() with no separator.
pub fn split_ws(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|x| !x.is_empty()).collect()
}
/// str.splitlines(): every Unicode line boundary, no trailing empty line.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut out = vec![];
    let (mut start, mut it) = (0, s.char_indices().peekable());
    while let Some((i, c)) = it.next() {
        if matches!(c, '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            out.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' && it.peek().map(|x| x.1) == Some('\n') {
                it.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}
/// s[:n] by code point.
pub fn head(s: &str, n: usize) -> &str {
    s.char_indices().nth(n).map_or(s, |(i, _)| &s[..i])
}
/// s[-n:] by code point.
pub fn tail(s: &str, n: usize) -> &str {
    let len = s.chars().count();
    if len <= n {
        s
    } else {
        s.char_indices().nth(len - n).map_or(s, |(i, _)| &s[i..])
    }
}
/// f"{s:w}" (left-justified, padded by code point).
pub fn ljust(s: &str, w: usize) -> String {
    let n = s.chars().count();
    if n >= w { s.to_string() } else { format!("{s}{}", " ".repeat(w - n)) }
}

// ---- quoting ----

/// shlex.quote
pub fn shlex_quote(s: &str) -> String {
    if s.is_empty() {
        return "''".into();
    }
    if s.chars().all(|c| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c)) {
        return s.into();
    }
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}
/// shlex.join
pub fn shlex_join<S: AsRef<str>>(xs: &[S]) -> String {
    xs.iter().map(|s| shlex_quote(s.as_ref())).collect::<Vec<_>>().join(" ")
}
/// html.escape(s) (quote=True)
pub fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}
/// urllib.parse.quote(s, safe=safe)
pub fn quote(s: &str, safe: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) || safe.as_bytes().contains(&b) {
            o.push(b as char);
        } else {
            let _ = write!(o, "%{b:02X}");
        }
    }
    o
}
/// urllib.parse.quote_plus(s): safe="", space -> "+"
pub fn quote_plus(s: &str) -> String {
    s.split(' ').map(|p| quote(p, "")).collect::<Vec<_>>().join("+")
}
/// urllib.parse.urlencode(pairs)
pub fn urlencode(pairs: &[(&str, &str)]) -> String {
    pairs.iter().map(|(k, v)| format!("{}={}", quote_plus(k), quote_plus(v))).collect::<Vec<_>>().join("&")
}

// ---- clock ----

fn clock_offset() -> f64 {
    // Harness only: GREENLIGHT_NOW shifts the wall clock so "now" starts there and ticks on (as pyclock/ does to Python).
    static OFF: OnceLock<f64> = OnceLock::new();
    *OFF.get_or_init(|| match std::env::var("GREENLIGHT_NOW").ok().and_then(|s| s.parse::<f64>().ok()) {
        Some(n) => n - real_time(),
        None => 0.0,
    })
}
fn real_time() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}
/// time.time()
pub fn time() -> f64 {
    real_time() + clock_offset()
}
/// time.strftime(fmt, time.localtime(ts)): local time per TZ.
pub fn strftime_local(fmt: &str, ts: f64) -> String {
    use chrono::TimeZone;
    chrono::Local.timestamp_opt(ts.floor() as i64, 0).unwrap().format(fmt).to_string()
}
/// time.strftime(fmt, time.gmtime(ts))
pub fn strftime_utc(fmt: &str, ts: f64) -> String {
    chrono::DateTime::from_timestamp(ts.floor() as i64, 0).unwrap().format(fmt).to_string()
}

// ---- processes ----

pub struct Out {
    pub rc: i32,
    pub out: String,
    pub err: String,
}

/// subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=timeout, cwd=cwd, env=os.environ+env).
/// Errors as Python raises them: FileNotFoundError (missing binary), TimeoutExpired (child killed).
/// stdin None inherits ours, as Python does without `input=`. Output gets universal newlines.
pub fn exec(cmd: &[String], stdin: Option<&str>, timeout: Option<f64>, cwd: Option<&Path>, env: &[(&str, &str)]) -> Res<Out> {
    let (rc, out, err) = exec_bytes(cmd, stdin.map(str::as_bytes), timeout, cwd, env)?;
    let text = |b: Vec<u8>| String::from_utf8_lossy(&b).replace("\r\n", "\n").replace('\r', "\n");
    Ok(Out { rc, out: text(out), err: text(err) })
}

/// exec without decoding.
pub fn exec_bytes(cmd: &[String], stdin: Option<&[u8]>, timeout: Option<f64>, cwd: Option<&Path>, env: &[(&str, &str)]) -> Res<(i32, Vec<u8>, Vec<u8>)> {
    let mut c = Command::new(&cmd[0]);
    c.args(&cmd[1..]).stdout(Stdio::piped()).stderr(Stdio::piped());
    c.stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::inherit() });
    if let Some(d) = cwd {
        c.current_dir(d);
    }
    for (k, v) in env {
        c.env(k, v);
    }
    let mut child = match c.spawn() {
        Ok(ch) => ch,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let what = if cwd.is_some_and(|d| !d.is_dir()) { cwd.unwrap().display().to_string() } else { cmd[0].clone() };
            return raise("FileNotFoundError", format!("[Errno 2] No such file or directory: {}", repr_str(&what)));
        }
        Err(e) => return Err(e.into()),
    };
    let feeder = stdin.map(|b| {
        let (mut w, b) = (child.stdin.take().unwrap(), b.to_vec());
        std::thread::spawn(move || {
            let _ = w.write_all(&b);
        })
    });
    let reader = |r: Option<Box<dyn Read + Send>>| {
        std::thread::spawn(move || {
            let mut v = vec![];
            if let Some(mut r) = r {
                let _ = r.read_to_end(&mut v);
            }
            v
        })
    };
    let o = reader(child.stdout.take().map(|x| Box::new(x) as _));
    let e = reader(child.stderr.take().map(|x| Box::new(x) as _));
    let deadline = timeout.map(|t| Instant::now() + Duration::from_secs_f64(t.max(0.0)));
    let status = loop {
        if let Some(s) = child.try_wait()? {
            break s;
        }
        if deadline.is_some_and(|d| Instant::now() >= d) {
            let _ = child.kill();
            let _ = child.wait();
            let t = timeout.unwrap();
            let t = if t.fract() == 0.0 { format!("{}", t as i64) } else { float_repr(t) };
            return raise("TimeoutExpired", format!("Command '{}' timed out after {t} seconds", repr_list(cmd)));
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    if let Some(f) = feeder {
        let _ = f.join();
    }
    use std::os::unix::process::ExitStatusExt;
    let rc = status.code().unwrap_or_else(|| -status.signal().unwrap_or(0));
    Ok((rc, o.join().unwrap(), e.join().unwrap()))
}

/// shutil.which(name)
pub fn which(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let ok = |p: &Path| p.is_file() && p.metadata().is_ok_and(|m| m.permissions().mode() & 0o111 != 0);
    if name.contains('/') {
        let p = PathBuf::from(name);
        return ok(&p).then_some(p);
    }
    std::env::var("PATH").ok()?.split(':').map(|d| Path::new(if d.is_empty() { "." } else { d }).join(name)).find(|p| ok(p))
}

/// Path.home()
pub fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default())
}
/// os.path.expanduser
pub fn expanduser(s: &str) -> PathBuf {
    match s.strip_prefix('~') {
        Some(r) if r.is_empty() || r.starts_with('/') => PathBuf::from(format!("{}{r}", home().display())),
        _ => PathBuf::from(s),
    }
}

/// ThreadPoolExecutor(n).map(f, items): results in input order.
pub fn pmap<T: Sync, R: Send>(items: &[T], n: usize, f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;
    let next = AtomicUsize::new(0);
    let out: Mutex<Vec<Option<R>>> = Mutex::new((0..items.len()).map(|_| None).collect());
    std::thread::scope(|s| {
        for _ in 0..n.min(items.len()) {
            s.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::SeqCst);
                if i >= items.len() {
                    break;
                }
                let r = f(&items[i]);
                out.lock().unwrap()[i] = Some(r);
            });
        }
    });
    out.into_inner().unwrap().into_iter().map(Option::unwrap).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn json_matches_python() {
        // Expected strings are Python 3's json.dumps output for the same values.
        let v = json!({"a": [1, 2.0, -0.0, 1e16, 1.5e-5, 0.1, 123456789012345678u64], "é→😀": "\u{7f}\u{1}\"\\\n", "e": [], "o": {}});
        assert_eq!(dumps(&v), r#"{"a": [1, 2.0, -0.0, 1e+16, 1.5e-05, 0.1, 123456789012345678], "\u00e9\u2192\ud83d\ude00": "\u007f\u0001\"\\\n", "e": [], "o": {}}"#);
        assert_eq!(dumps_indent(&json!({"a": [1, {"b": null}], "c": []})), "{\n \"a\": [\n  1,\n  {\n   \"b\": null\n  }\n ],\n \"c\": []\n}");
        assert_eq!(float_repr(0.1 + 0.2), "0.30000000000000004");
        assert_eq!(float_repr(1234.5), "1234.5");
        assert_eq!(float_repr(1e-4), "0.0001");
        assert_eq!(float_repr(123e20), "1.23e+22");
    }
    #[test]
    fn round_is_python() {
        assert_eq!(round(0.25, 1), 0.2);
        assert_eq!(round(0.35, 1), 0.3);
        assert_eq!(round(2.675, 2), 2.67);
        assert_eq!(round(0.05, 1), 0.1);
        assert_eq!(float_repr(round(-0.04, 1)), "-0.0");
    }
    #[test]
    fn strs() {
        assert_eq!(splitlines("a\r\nb\rc\x0bd\n\ne\n"), vec!["a", "b", "c", "d", "", "e"]);
        assert_eq!(strip("\x1c a \u{a0}"), "a");
        assert_eq!(head("héllo", 2), "hé");
        assert_eq!(tail("héllo", 4), "éllo");
        assert_eq!(shlex_join(&["a b", "it's", "x/y@1", "é"]), r#"'a b' 'it'"'"'s' x/y@1 'é'"#);
        assert_eq!(quote("acme/w x~", ""), "acme%2Fw%20x~");
        assert_eq!(urlencode(&[("source_branch", "f/a b")]), "source_branch=f%2Fa+b");
        assert_eq!(repr(&json!({"a": [null, true, "it's"]})), r#"{'a': [None, True, "it's"]}"#);
    }
}
