"""The coding-agent CLIs greenlight runs headless: automations, the menubar chat, the dashboard's LLM calls.

  argv(name, text, model=, effort=, sid=, system=, stream=, images=) -> (cmd, stdin text)
  images: files codex gets as images (--image); the others read them from the paths the message names

Safe by default: claude only accepts edits, codex runs in its workspace-write sandbox, opencode
auto-rejects whatever asks permission. config.json `harnesses.<name>.bypass: true` gives that one
full access (unattended greenlight work needs it: the chat and automations run shell commands).
pi has no permission gate at all, so it runs only once you set its bypass.
`harnesses.<name>.bin` overrides where the binary is.
"""
import os
import shutil

from greenlight import CONFIG

# safe/bypass: permission args. system: has a system-prompt flag (else it rides in the first message).
H = {
    "claude": {"safe": ["--permission-mode", "acceptEdits"], "bypass": ["--permission-mode", "bypassPermissions"], "system": True},
    "codex": {"safe": ["-c", "sandbox_mode=workspace-write"], "bypass": ["--dangerously-bypass-approvals-and-sandbox"], "system": False},
    "opencode": {"safe": [], "bypass": ["--auto"], "system": False},
    "pi": {"safe": None, "bypass": [], "system": True},
}


def conf(name):
    return (CONFIG.get("harnesses") or {}).get(name) or {}


def binary(name):
    # ponytail: systemd/launchd PATHs miss the per-user install dirs, so look there too
    return conf(name).get("bin") or shutil.which(name) or next(
        (p for p in (os.path.expanduser(f"~/.local/bin/{name}"), os.path.expanduser(f"~/.{name}/bin/{name}")) if os.path.exists(p)), name)


def argv(name, text, model=None, effort=None, sid=None, system=None, stream=False, images=()):
    if name not in H:
        raise ValueError(f"unknown harness {name!r}: one of {', '.join(H)}")
    h = H[name]
    perms = h["bypass"] if conf(name).get("bypass") else h["safe"]
    if perms is None:
        raise ValueError(f"{name} has no permission sandbox: set harnesses.{name}.bypass true in config.json to use it")
    if system and not h["system"] and not sid:
        text = system + "\n\n" + text
    opt = lambda flag, v: [flag, v] if v else []
    b = binary(name)
    if name == "claude":
        cmd = [b, "-p", *perms, *(["--output-format", "stream-json", "--verbose", "--include-partial-messages"] if stream else []),
               *opt("--append-system-prompt", system), *opt("--model", model), *opt("--effort", effort), *opt("--resume", sid)]
    elif name == "codex":
        eff = {"xhigh": "high", "max": "high"}.get(effort, effort)
        cmd = [b, "exec", *(["resume", sid] if sid else []), "--skip-git-repo-check", *perms, *(["--json"] if stream else []),
               *opt("-m", model), *opt("-c", eff and f"model_reasoning_effort={eff}"),
               *[f"--image={p}" for p in images or ()], "-"]  # --image=: its bare form takes every argument after it
    elif name == "opencode":
        cmd = [b, "run", *perms, *(["--format", "json"] if stream else []), *opt("-m", model), *opt("--variant", effort), *opt("-s", sid)]
    else:
        cmd = [b, "-p", *perms, *(["--mode", "json"] if stream else []), *opt("--model", model),
               *opt("--thinking", {"max": "xhigh"}.get(effort, effort)), *opt("--session", sid), *opt("--append-system-prompt", system)]
    return cmd, text


def interactive(name, prompt):
    """The command line that starts `name` in a terminal with a first prompt (opencode's positional is a project dir)."""
    return [name, *(["--prompt"] if name == "opencode" else []), prompt] if prompt else [name]
