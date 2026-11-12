//! The coding-agent CLIs greenlight runs headless (harness.py): automations, the menubar chat, the dashboard's LLM calls.
//! Safe by default; config.json `harnesses.<name>.bypass: true` gives that one full access, `.bin` overrides the binary.
use crate::base::cfg;
use crate::py::{self, Res};
use serde_json::Value;

/// (name, safe permission args (None: no sandbox, needs bypass), bypass args, has a system-prompt flag)
pub const H: [(&str, Option<&[&str]>, &[&str], bool); 4] = [
    ("claude", Some(&["--permission-mode", "acceptEdits"]), &["--permission-mode", "bypassPermissions"], true),
    ("codex", Some(&["-c", "sandbox_mode=workspace-write"]), &["--dangerously-bypass-approvals-and-sandbox"], false),
    ("opencode", Some(&[]), &["--auto"], false),
    ("pi", None, &[], true),
];

pub fn conf(name: &str) -> Value {
    py::get_or(cfg("harnesses"), name).filter(|v| v.is_object()).cloned().unwrap_or(Value::Object(Default::default()))
}

pub fn binary(name: &str) -> String {
    // ponytail: systemd/launchd PATHs miss the per-user install dirs, so look there too
    if let Some(b) = py::get_or(&conf(name), "bin") {
        return py::str_(b);
    }
    if let Some(p) = py::which(name) {
        return p.display().to_string();
    }
    [format!("~/.local/bin/{name}"), format!("~/.{name}/bin/{name}")]
        .iter()
        .map(|p| py::expanduser(p))
        .find(|p| p.exists())
        .map_or(name.to_string(), |p| p.display().to_string())
}

/// (cmd, stdin text) that runs `name` headless on `text`. Empty strings count as unset, as in Python.
pub fn argv(name: &str, text: &str, model: Option<&str>, effort: Option<&str>, sid: Option<&str>, system: Option<&str>, stream: bool) -> Res<(Vec<String>, String)> {
    let Some(&(_, safe, bypass, has_system)) = H.iter().find(|h| h.0 == name) else {
        return py::raise("ValueError", format!("unknown harness {}: one of claude, codex, opencode, pi", py::repr_str(name)));
    };
    let some = |v: Option<&str>| v.filter(|s| !s.is_empty()).map(str::to_string);
    let (model, effort, sid, system) = (some(model), some(effort), some(sid), some(system));
    let perms = if py::get_or(&conf(name), "bypass").is_some() { Some(bypass) } else { safe };
    let Some(perms) = perms else {
        return py::raise("ValueError", format!("{name} has no permission sandbox: set harnesses.{name}.bypass true in config.json to use it"));
    };
    let mut text = text.to_string();
    if let (Some(sys), false, None) = (&system, has_system, &sid) {
        text = format!("{sys}\n\n{text}");
    }
    let opt = |flag: &str, v: &Option<String>| v.as_ref().map_or(vec![], |v| vec![flag.to_string(), v.clone()]);
    let st = |xs: &[&str]| if stream { xs.iter().map(|s| s.to_string()).collect() } else { vec![] };
    let mut cmd = vec![binary(name)];
    let perms: Vec<String> = perms.iter().map(|s| s.to_string()).collect();
    match name {
        "claude" => {
            cmd.extend(["-p".to_string()]);
            cmd.extend(perms);
            cmd.extend(st(&["--output-format", "stream-json", "--verbose", "--include-partial-messages"]));
            cmd.extend(opt("--append-system-prompt", &system));
            cmd.extend(opt("--model", &model));
            cmd.extend(opt("--effort", &effort));
            cmd.extend(opt("--resume", &sid));
        }
        "codex" => {
            let eff = effort.map(|e| if e == "xhigh" || e == "max" { "high".to_string() } else { e });
            cmd.push("exec".into());
            if let Some(s) = &sid {
                cmd.extend(["resume".into(), s.clone()]);
            }
            cmd.push("--skip-git-repo-check".into());
            cmd.extend(perms);
            cmd.extend(st(&["--json"]));
            cmd.extend(opt("-m", &model));
            cmd.extend(opt("-c", &eff.map(|e| format!("model_reasoning_effort={e}"))));
            cmd.push("-".into());
        }
        "opencode" => {
            cmd.push("run".into());
            cmd.extend(perms);
            cmd.extend(st(&["--format", "json"]));
            cmd.extend(opt("-m", &model));
            cmd.extend(opt("--variant", &effort));
            cmd.extend(opt("-s", &sid));
        }
        _ => {
            cmd.push("-p".into());
            cmd.extend(perms);
            cmd.extend(st(&["--mode", "json"]));
            cmd.extend(opt("--model", &model));
            cmd.extend(opt("--thinking", &effort.map(|e| if e == "max" { "xhigh".to_string() } else { e })));
            cmd.extend(opt("--session", &sid));
            cmd.extend(opt("--append-system-prompt", &system));
        }
    }
    Ok((cmd, text))
}

/// The command line that starts `name` in a terminal with a first prompt (opencode's positional is a project dir).
pub fn interactive(name: &str, prompt: &str) -> Vec<String> {
    let mut v = vec![name.to_string()];
    if !prompt.is_empty() {
        if name == "opencode" {
            v.push("--prompt".into());
        }
        v.push(prompt.into());
    }
    v
}
