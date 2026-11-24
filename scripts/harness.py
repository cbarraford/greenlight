"""The coding-agent CLIs greenlight runs headless: automations, the menubar chat, the dashboard's LLM calls.

  argv(name, text, model=, effort=, sid=, system=, stream=, images=) -> (cmd, stdin text)
  images: files codex gets as images (--image); the others read them from the paths the message names
  route(model, default) -> (harness, model or None): which harness runs a model id

Safe by default: claude only accepts edits, codex runs in its workspace-write sandbox, opencode
auto-rejects whatever asks permission, gemini auto-approves edits only (`--approval-mode auto_edit`).
config.json `harnesses.<name>.bypass: true` gives that one full access (unattended greenlight work
needs it: the chat and automations run shell commands).
pi has no permission gate at all, so it runs only once you set its bypass.
`harnesses.<name>.bin` overrides where the binary is, `harnesses.<name>.model` the model it runs when none is asked for
(workspace agents through Orca too: greenlight.orca_agent).

A model id picks its harness everywhere greenlight takes one (chat, automations, dashboard pages, suggestions, System One):
  claude-*                      Claude Code
  gpt-*, codex-*, o<N>          Codex (gpt-oss-* excepted)
  gemini-*, gpt-oss-*           agy (Antigravity)
  <harness>:<model>             that harness, that model: agy:claude-opus-5-5-high, codex:gpt-6-luna, opencode:p/m
  <harness>                     that harness's own default model
`harness.py models` lists what each installed harness offers, as JSON.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from greenlight import CONFIG

# safe/bypass: permission args. system: has a system-prompt flag (else it rides in the first message).
H: dict[str, dict[str, Any]] = {
    "claude": {"safe": ["--permission-mode", "acceptEdits"], "bypass": ["--permission-mode", "bypassPermissions"], "system": True},
    "codex": {"safe": ["-c", "sandbox_mode=workspace-write"], "bypass": ["--dangerously-bypass-approvals-and-sandbox"], "system": False},
    "opencode": {"safe": [], "bypass": ["--auto"], "system": False},
    "pi": {"safe": None, "bypass": [], "system": True},
    # Gemini CLI: headless when stdin isn't a TTY (the text rides on stdin); no system-prompt or effort flag. Headless in a
    # folder it doesn't trust it refuses to start, so both modes pass --skip-trust (trust loads the folder's .gemini
    # settings; what the agent may do is still the approval mode).
    "gemini": {"safe": ["--approval-mode", "auto_edit", "--skip-trust"], "bypass": ["--approval-mode", "yolo", "--skip-trust"], "system": False},
    "agy": {"safe": ["--mode", "accept-edits"], "bypass": ["--dangerously-skip-permissions"], "system": False},
}
HAIKU = "claude-haiku-5-5"  # the cheap default for background calls when Claude runs them
EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")  # agy bakes effort into its model ids
ALIASES = {"antigravity": "agy"}  # ADE-specific agent identifiers -> greenlight's harness name


def canonical(name):
    """Map ADE-specific agent names (e.g. Orca's 'antigravity') to greenlight's harness name."""
    if not isinstance(name, str):
        return name
    low = name.strip().lower()
    return ALIASES.get(low, low) if low in ALIASES or low in H else name


def harness_of(model):
    """The harness a model id runs on, or None when the id doesn't say (see the module docstring)."""
    if not model:
        return None
    if model in H:
        return model
    head, sep, _ = model.partition(":")
    if sep and head in H:
        return head
    if model.startswith(("gemini-", "gpt-oss-")):
        return "agy"
    if model.startswith("claude-"):
        return "claude"
    if model.startswith(("gpt-", "codex-")) or re.fullmatch(r"o\d[\w.-]*", model):
        return "codex"
    return None


def route(model, default="claude"):
    """(harness, model id it gets or None for its default). The id picks the harness; an id that doesn't name one
    goes to `default` (config llm_harness for the dashboard's calls)."""
    h = harness_of(model)
    if h is None:
        return default, model or None
    if model == h:
        return h, None
    if model.startswith(h + ":"):
        return h, model[len(h) + 1:] or None
    return h, "gemini-4-neon-medium" if model == "gemini-4-neon" else model  # gemini-4-neon: an old saved choice


def conf(name):
    return (CONFIG.get("harnesses") or {}).get(name) or {}


def binary(name):
    # ponytail: systemd/launchd PATHs miss the per-user install dirs, so look there too
    return conf(name).get("bin") or shutil.which(name) or next(
        (p for p in (os.path.expanduser(f"~/.local/bin/{name}"), os.path.expanduser(f"~/.{name}/bin/{name}")) if os.path.exists(p)), name)


def argv(name, text, model=None, effort=None, sid=None, system=None, stream=False, images=(), tools=True):
    if name not in H:
        raise ValueError(f"unknown harness {name!r}: one of {', '.join(H)}")
    h = H[name]
    if tools:
        perms = h["bypass"] if conf(name).get("bypass") is True else h["safe"]
        if perms is None:
            raise ValueError(f"{name} has no permission sandbox: set harnesses.{name}.bypass true in config.json to use it")
    else:
        # Pure text/JSON generation (e.g. dashboard layout/suggestions): never enable bypass or edit tools.
        perms = {
            "claude": ["--tools", "", "--no-session-persistence"],
            "codex": ["-c", "sandbox_mode=read-only"],
            "opencode": [],
            "gemini": ["--approval-mode", "default", "--skip-trust"],
            "agy": [],
            "pi": None,
        }[name]
        if perms is None:
            raise ValueError(f"{name} has no toolless/sandboxed mode for untrusted prompts")
    if system and not h["system"] and not sid:
        text = system + "\n\n" + text
    opt = lambda flag, v: [flag, v] if v else []
    model = model or conf(name).get("model")  # no model asked for: this agent's own default from config.json
    b = binary(name)
    if name == "claude":
        cmd = [b, "-p", *perms, *(["--output-format", "stream-json", "--verbose", "--include-partial-messages"] if stream else []),
               *opt("--append-system-prompt", system), *opt("--model", model), *opt("--effort", effort), *opt("--resume", sid)]
    elif name == "codex":
        eff = codex_effort(model, effort)
        cmd = [b, "exec", *(["resume", sid] if sid else []), "--skip-git-repo-check", *perms, *(["--json"] if stream else []),
               *opt("-m", model), *opt("-c", eff and f"model_reasoning_effort={eff}"),
               *[f"--image={p}" for p in images or ()], "-"]  # --image=: its bare form takes every argument after it
    elif name == "opencode":
        cmd = [b, "run", *perms, *(["--format", "json"] if stream else []), *opt("-m", model), *opt("--variant", effort), *opt("-s", sid)]
    elif name == "gemini":
        cmd = [b, *perms, *(["--output-format", "stream-json"] if stream else []), *opt("-m", model), *opt("--resume", sid)]
    elif name == "agy":
        cmd = [b, *perms, "--input-format", "stream-json", "--output-format", "stream-json",
               *opt("--model", model), *opt("--effort", not EFFORT_SUFFIX.search(model or "") and effort), *opt("--conversation", sid)]
        text = json.dumps({"event": "user", "message": {"role": "user", "content": text}}) + "\n"
    else:
        cmd = [b, "-p", *perms, *(["--mode", "json"] if stream else []), *opt("--model", model),
               *opt("--thinking", {"max": "xhigh"}.get(effort, effort)), *opt("--session", sid), *opt("--append-system-prompt", system)]
    return cmd, text


def interactive(name, prompt):
    """The command line that starts `name` in a terminal with a first prompt (opencode's positional is a project dir;
    gemini's positional runs headless when stdin isn't a TTY, and agy takes -i/--prompt-interactive for an initial prompt)."""
    name = canonical(name)
    if name not in H:
        raise ValueError(f"unknown harness {name!r}: one of {', '.join(H)}")
    b = binary(name)
    model = conf(name).get("model")
    mflag = {"codex": "-m", "opencode": "-m", "gemini": "-m"}.get(name, "--model")
    base = [b, *([mflag, model] if model else [])]
    if not prompt:
        return base
    flag = {"opencode": ["--prompt"], "gemini": ["-i"], "agy": ["-i"]}.get(name)
    if flag:
        return [*base, *flag, prompt]
    return [*base, "--", prompt]


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _codex_cache():
    try:
        with open(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    ms = d.get("models") if isinstance(d, dict) else d
    return [m for m in ms or [] if isinstance(m, dict) and isinstance(m.get("slug"), str)]


def codex_effort(model, effort):
    """`effort` as `model` takes it: the nearest level at or below it among the levels Codex's model cache lists for
    that model (an unknown model, or no cache, takes it as asked)."""
    if effort not in EFFORTS:
        return effort
    levels = next(([lv.get("effort") for lv in m.get("supported_reasoning_levels") or [] if isinstance(lv, dict)]
                   for m in _codex_cache() if m["slug"] == model), [])
    known = [lv for lv in EFFORTS if lv in levels]
    if not known or effort in known:
        return effort
    below = [lv for lv in known if EFFORTS.index(lv) <= EFFORTS.index(effort)]
    return (below or known)[-1]


CLAUDE_MODELS = [("claude-opus-5-5", "Opus 5.5"), ("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-haiku-5-5", "Haiku 5.5"),
                 ("claude-fable-5-2", "Fable 5.2"), ("claude-fable-5-1", "Fable 5.1")]


def installed(name):
    b = binary(name)
    return os.path.isabs(b) and os.access(b, os.X_OK) or bool(shutil.which(b))


def _id(h, slug):
    return slug if harness_of(slug) == h else f"{h}:{slug}"


def _agy_models():
    """`agy models` (slug, tab, name), cached for a day: it asks the network."""
    from greenlight import STATE
    cache = STATE / "models-agy.json"
    try:
        with open(cache) as f:
            d = json.load(f)
        if time.time() - d["t"] < 86400:
            return d["models"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        out = subprocess.run([binary("agy"), "models"], capture_output=True, text=True, timeout=45).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ms = [line.split("\t", 1) for line in out.splitlines() if "\t" in line]
    ms = [[a.strip(), b.strip()] for a, b in ms if a.strip()]
    if ms:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as f:
            json.dump({"t": time.time(), "models": ms}, f)
    return ms


def models():
    """Every model the installed harnesses offer: [{id, name, harness}], ids as route() takes them."""
    out = []
    if installed("claude"):
        out += [{"id": i, "name": n, "harness": "claude"} for i, n in CLAUDE_MODELS]
    if installed("codex"):
        out += [{"id": _id("codex", m["slug"]), "name": m.get("display_name") or m["slug"], "harness": "codex"}
                for m in _codex_cache() if m.get("visibility", "list") == "list"]
        out.append({"id": "codex", "name": "Codex default", "harness": "codex"})
    if installed("agy"):
        out += [{"id": _id("agy", s), "name": n if s.startswith("gemini-") else f"{n} · agy", "harness": "agy"} for s, n in _agy_models()]
    for h, n in (("opencode", "OpenCode"), ("pi", "Pi"), ("gemini", "Gemini CLI")):
        if installed(h):
            out.append({"id": h, "name": n, "harness": h})
    return out


def answer(h, out):
    """A toolless run's stdout → its reply text ("" when it failed). agy prints stream-json; the others the reply."""
    if h != "agy":
        return out
    for line in reversed(out.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result = event.get("result") or {}
            return result.get("response", "") if result.get("status") == "SUCCESS" else ""
    return ""


def ask(prompt, model=None, effort=None, default="claude", timeout=180, cwd=None):
    """One toolless headless answer from whichever harness runs `model` (no model: `default` with its cheap default).
    "" when it fails: a failed `claude -p` prints its error on stdout, and that must not pass for an answer."""
    import tempfile
    h, model = route(model or (HAIKU if default == "claude" else default), default)
    cmd, text = argv(h, prompt, model=model, effort=effort, tools=False)
    with tempfile.TemporaryDirectory(prefix="greenlight-llm-") as empty:
        p = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=timeout, cwd=cwd or empty)
    return "" if p.returncode else answer(h, p.stdout)


if __name__ == "__main__":
    import diag
    diag.install("harness")
    if sys.argv[1:] == ["models"]:
        print(json.dumps(models()))
    elif sys.argv[1:2] == ["route"] and len(sys.argv) == 3:
        print(json.dumps(route(sys.argv[2])))
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
