"""The coding-agent CLIs greenlight runs headless: automations, the menubar chat, the dashboard's LLM calls.

  argv(name, text, model=, effort=, sid=, system=, stream=, images=) -> (cmd, stdin text)
  images: files codex gets as images (--image); the others read them from the paths the message names
  route(model, default) -> (harness, model or None): which harness runs a model id

Safe by default: claude only accepts edits, codex runs in its workspace-write sandbox, opencode
auto-rejects whatever asks permission, gemini auto-approves edits only (`--approval-mode auto_edit`).
config.json `harnesses.<name>.bypass: true` gives that one full access (unattended greenlight work
needs it: the chat and automations run shell commands).
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
    # Gemini CLI: headless when stdin isn't a TTY (the text rides on stdin); no system-prompt or effort flag. In a folder
    # it doesn't trust it drops any approval mode back to "default", so bypass also trusts the folder (--skip-trust).
    "gemini": {"safe": ["--approval-mode", "auto_edit"], "bypass": ["--approval-mode", "yolo", "--skip-trust"], "system": False},
}
# Gemini models the app offers by name. Gemini CLI passes any -m id to the API as is; gemini-4-neon isn't in its own
# model list yet (google-gemini/gemini-cli#30385), so the account must have API access to it.
GEMINI_MODELS = {"gemini-4-neon": "Gemini 4 Neon"}


def route(model, default="claude"):
    """The harness that runs `model` and the model id it gets: a harness name runs its default model, gemini-* runs
    on gemini, anything else (a Claude model, or nothing) on `default`, which gets the model only when it's claude."""
    if model in H:
        return model, None
    if model and model.startswith("gemini-"):
        return "gemini", model
    return default, model if default == "claude" else None


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
    elif name == "gemini":
        cmd = [b, *perms, *(["--output-format", "stream-json"] if stream else []), *opt("-m", model), *opt("--resume", sid)]
    else:
        cmd = [b, "-p", *perms, *(["--mode", "json"] if stream else []), *opt("--model", model),
               *opt("--thinking", {"max": "xhigh"}.get(effort, effort)), *opt("--session", sid), *opt("--append-system-prompt", system)]
    return cmd, text


def interactive(name, prompt):
    """The command line that starts `name` in a terminal with a first prompt (opencode's positional is a project dir;
    gemini's positional runs headless when stdin isn't a TTY, -i keeps it interactive)."""
    return [name, *({"opencode": ["--prompt"], "gemini": ["-i"]}.get(name, [])), prompt] if prompt else [name]
