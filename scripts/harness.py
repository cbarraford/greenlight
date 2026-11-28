"""The coding-agent CLIs greenlight runs headless: automations, the menubar chat, the dashboard's LLM calls.

  argv(name, text, model=, effort=, sid=, system=, stream=, images=) -> (cmd, stdin text)
  images: files codex gets as images (--image); the others read them from the paths the message names
  allow: permission rules the user approved for this one turn (claude's --allowedTools, e.g. "Bash(orca host list)")
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
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import ade_common
import bins
import diag
import greenlight
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
ALIASES = {"antigravity": "agy", "claude-code": "claude", "claude code": "claude", "gemini-cli": "gemini"}  # ADE-specific agent identifiers -> greenlight's harness name
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")  # fullmatch: ASCII-only safe session/conversation ID token (matches Swift & JS)


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
    # harnesses.<name>.bin wins over bins.<name>; systemd/launchd/ssh PATHs miss the per-user install dirs, so bins also
    # looks in ~/.local/bin and the like, then ~/.<name>/bin (codex's, opencode's own installers)
    alt = os.path.expanduser(f"~/.{name}/bin/{name}")
    return bins.find(name, override=conf(name).get("bin")) or (alt if bins.is_executable(alt) else name)


def permissions(name):
    """The permission args for a tool-using run of `name`, headless or interactive: its bypass ones when config.json sets
    harnesses.<name>.bypass to exactly true, else its safe ones (None: it has no sandbox)."""
    h = H[name]
    return h["bypass"] if conf(name).get("bypass") is True else h["safe"]


def argv(name, text, model=None, effort=None, sid=None, system=None, stream=False, images=(), tools=True, allow=()):
    if name not in H:
        raise ValueError(f"unknown harness {name!r}: one of {', '.join(H)}")
    h = H[name]
    if tools:
        perms = permissions(name)
        if perms is None:
            raise ValueError(f"{name} has no permission sandbox: set harnesses.{name}.bypass true in config.json to use it")
    else:
        # Pure text/JSON generation (e.g. dashboard layout/suggestions): never enable bypass or edit tools.
        perms = {
            "claude": ["--tools", "", "--no-session-persistence"],
            "codex": ["-c", "sandbox_mode=read-only"],
            "opencode": [],
            "gemini": ["--approval-mode", "default"],
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
               *opt("--append-system-prompt", system), *opt("--model", model), *opt("--effort", effort), *opt("--resume", sid),
               *(["--allowedTools", *allow] if allow and tools else [])]  # last: it takes every argument after it
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


def trust(name, cwd):
    """Args that trust cwd for this one session when name's bypass is on: agy's and codex's bypass flags don't skip their
    trust-folder dialogs, which stall an unattended launch in a new worktree. Neither override is saved to their config.
    Claude's dialog has no such flag (only -p skips it), so a new folder still asks once."""
    if not cwd or conf(name).get("bypass") is not True:
        return []
    path = os.path.abspath(os.path.expanduser(cwd))
    if name == "agy":
        return ["--settings", json.dumps({"trustedWorkspaces": [path]})]
    if name == "codex":  # a TOML inline table: the path has dots, so a dotted -c key can't name it; a JSON string is a TOML one
        return ["-c", "projects={" + json.dumps(path) + '={trust_level="trusted"}}']
    return []


def interactive(name, prompt, cwd=None):
    """The command line that starts `name` in a terminal (in cwd) with a first prompt (opencode's positional is a project dir;
    gemini's positional runs headless when stdin isn't a TTY, and agy takes -i/--prompt-interactive for an initial prompt).
    It carries the same permission args as a headless run, and trust(name, cwd)."""
    name = canonical(name)
    if name not in H:
        raise ValueError(f"unknown harness {name!r}: one of {', '.join(H)}")
    b = binary(name)
    model = conf(name).get("model")
    mflag = {"codex": "-m", "opencode": "-m", "gemini": "-m"}.get(name, "--model")
    perms = permissions(name) or []  # pi has no gate: it runs as it always has, with you watching its terminal
    base = [b, *perms, *trust(name, cwd), *([mflag, model] if model else [])]
    if not prompt:
        return base
    flag = {"opencode": ["--prompt"], "gemini": ["-i"], "agy": ["-i"]}.get(name)
    if flag:
        return [*base, *flag, prompt]
    return [*base, "--", prompt]


def resume_cmd(name, sid, binary_override=None):
    """The interactive command line that connects a terminal to a specific session ID for `name`."""
    name = canonical(name)
    if name not in H:
        raise ValueError(f"unknown harness {name!r}: one of {', '.join(H)}")
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
        raise ValueError(f"invalid session id {sid!r}")
    b = binary_override or binary(name)
    if name == "codex":
        return [b, "resume", sid]
    if name == "opencode":
        return [b, "-s", sid]
    if name == "pi":
        return [b, "--session", sid]
    return [b, "--resume", sid]


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
    return below[-1] if below else known[0]


CLAUDE_MODELS = [("claude-opus-5-5", "Opus 5.5"), ("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-haiku-5-5", "Haiku 5.5"),
                 ("claude-fable-5-2", "Fable 5.2"), ("claude-fable-5-1", "Fable 5.1")]


def installed(name):
    b = binary(name)
    return bins.is_executable(b) if os.sep in b else bool(shutil.which(b))


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
        out.append({"id": "agy", "name": "Antigravity default", "harness": "agy"})
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


LIMIT_COOLDOWN = 300  # default 5m cooldown after a runtime cap hit when no reset timestamp is known
MAX_COOLDOWN = 3600  # cap local runtime cooldown at 1h; usage.json tracks longer weekly windows live
USAGE_STALE = 900  # fallback freshness window (15m) when a usage window has no reset timestamp
EXTRA_LIMIT_RE = re.compile(
    r"rate.?limit(?:_error|\s+(?:exceeded|reached|hit))|(?:http(?:/[\d.]+)?|status|error|code)[:\s]+429\b|"
    r"\b429\s+too many requests|\btoo many requests\b|"
    r"hit your (?:5-?hour|weekly|daily|usage|rate)?\s*limit|overloaded_error|capacity exhausted",
    re.I,
)
AUTH_ERR_RE = re.compile(
    r"(?:OAuth access token has been revoked|Please run /login|not logged in|"
    r"Run `?(?:codex|claude|agy|gemini|opencode|pi) login`?|login expired|"
    r"API key (?:has expired|is invalid|not valid|expired)|Invalid API key|authentication_error)",
    re.I | re.M,
)
SPEC_RE = re.compile(r"[A-Za-z0-9_][\w.:/-]{0,79}(?:@[A-Za-z0-9_][\w.-]{0,39})?\Z")
_SHARED_WINDOWS = {"5h", "week", "day", "limit"}
_MODEL_POOLS = {"opus", "sonnet", "fable", "haiku"}


def is_limit_error(text: str | None) -> bool:
    """True when CLI output/stderr indicates a 5-hour, weekly, daily, rate, or quota cap."""
    if not text:
        return False
    return bool(ade_common.LIMIT_RE.search(text) or EXTRA_LIMIT_RE.search(text))


def is_unavailable_error(text: str | None) -> bool:
    """True when CLI output/stderr indicates the provider is logged out or has an expired/invalid credential."""
    if not text:
        return False
    return bool(ade_common.DEAD_RE.search(text) or AUTH_ERR_RE.search(text))


def fallback_reason(text: str | None, *, missing: bool = False) -> str | None:
    """Return a diag token explaining why we should fall back to the next provider, or None."""
    if missing:
        return "missing"
    if is_limit_error(text):
        return "rate_limit"
    if is_unavailable_error(text):
        return "auth"
    return None


def _limits_path() -> Path:
    return greenlight.STATE / "provider-limits.json"


@contextlib.contextmanager
def _limits_lock():
    path = _limits_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _load_limits() -> dict[str, dict[str, Any]]:
    try:
        with open(_limits_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(data, dict) else {}


def _save_limits(data: dict[str, dict[str, Any]]) -> None:
    path = _limits_path()
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _model_family(model: str | None) -> str:
    low = (model or "").lower()
    for needle, fam in (("claude", "claude"), ("gpt", "gpt"), ("flash-lite", "lite"), ("flash", "flash"), ("pro", "pro")):
        if needle in low:
            return fam
    return "gemini"


def _window_applies(name: str, label: str, note: str, model: str | None) -> bool:
    """Whether a usage.json window label caps (name, model)."""
    low = label.strip().lower()
    tokens = set(re.findall(r"[a-z0-9]+", low))
    if not low or name in ("codex", "opencode"):
        return True
    if name in ("claude", "pi"):
        if tokens & _SHARED_WINDOWS or low not in _MODEL_POOLS:
            return True
        return not model or low in model.lower()
    if name == "agy":
        fam = _model_family(model)
        if tokens & _SHARED_WINDOWS:
            mentioned = {f for f in ("claude", "gpt", "gemini") if f in f"{low} {note.lower()}"}
            if mentioned and model:
                return fam in mentioned or (fam not in ("claude", "gpt") and "gemini" in mentioned)
            return True
        if not model or low == "models":
            return True
        return low == fam or (low == "gemini" and fam not in ("claude", "gpt"))
    if name == "gemini":
        if not model or (tokens & _SHARED_WINDOWS) or low == "models":
            return True
        fam = _model_family(model)
        return low == fam or low == "gemini" or low in model.lower()
    return True


def _usage_entry(name: str) -> dict[str, Any] | None:
    if os.environ.get("GREENLIGHT_USAGE") == "off":
        return None
    try:
        with open(greenlight.STATE / "usage.json") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    providers = raw.get("p") if isinstance(raw, dict) else None
    entry = providers.get(name) if isinstance(providers, dict) else None
    return entry if isinstance(entry, dict) else None


def _capped_usage_reset(name: str, model: str | None, now: float) -> tuple[bool, float | None]:
    """Return (is_capped, reset_epoch_or_None) from cached usage.json (no network I/O)."""
    entry = _usage_entry(name)
    if not entry:
        return False, None
    windows = entry.get("windows")
    if not isinstance(windows, list):
        return False, None
    note = str(entry.get("note") or "")
    at = entry.get("at")
    fresh = isinstance(at, (int, float)) and not isinstance(at, bool) and (now - float(at)) < USAGE_STALE
    best_reset: float | None = None
    capped = False
    for win in windows:
        if not isinstance(win, dict):
            continue
        pct = win.get("pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or pct < 99.5:
            continue
        if not _window_applies(name, str(win.get("label") or ""), note, model):
            continue
        resets = win.get("resets")
        if isinstance(resets, (int, float)) and not isinstance(resets, bool):
            if resets > now:
                capped = True
                best_reset = resets if best_reset is None else max(best_reset, float(resets))
        elif fresh:
            capped = True
    return capped, best_reset


def is_capped(name: str, model: str | None = None, now: float | None = None) -> str | None:
    """Return 'cooldown' or 'usage_cap' if provider `name` is currently known to be capped, else None."""
    t = time.time() if now is None else now
    rec = _load_limits().get(name)
    if isinstance(rec, dict):
        until = rec.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until > t:
            return "cooldown"
    capped, _ = _capped_usage_reset(name, model, t)
    return "usage_cap" if capped else None


def record_limit(name: str, text: str = "", model: str | None = None, now: float | None = None) -> float:
    """Record a runtime rate/usage limit cooldown for provider `name` and return its expiry epoch."""
    t = time.time() if now is None else now
    _, reset = _capped_usage_reset(name, model, t)
    until = min(reset, t + MAX_COOLDOWN) if reset and reset > t else t + LIMIT_COOLDOWN
    with contextlib.suppress(OSError), _limits_lock():
        limits = _load_limits()
        limits[name] = {"until": int(until), "at": int(t), "reason": "rate_limit"}
        _save_limits(limits)
    return until


def clear_limit(name: str) -> None:
    """Clear any active runtime cooldown for provider `name` after a successful call."""
    with contextlib.suppress(OSError), _limits_lock():
        limits = _load_limits()
        if name in limits:
            limits.pop(name, None)
            _save_limits(limits)


def limits_status(now: float | None = None) -> dict[str, dict[str, Any]]:
    """Current cap/cooldown status per harness (for `harness.py limits`)."""
    t = time.time() if now is None else now
    stored = _load_limits()
    out: dict[str, dict[str, Any]] = {}
    for name in H:
        reason = is_capped(name, now=t)
        rec = stored.get(name) if isinstance(stored.get(name), dict) else None
        raw_until = rec.get("until") if rec else None
        until = int(raw_until) if isinstance(raw_until, (int, float)) and not isinstance(raw_until, bool) and raw_until > t else None
        _, usage_reset = _capped_usage_reset(name, None, t)
        if usage_reset and (until is None or usage_reset > until):
            until = int(usage_reset)
        out[name] = {"capped": reason is not None, "reason": reason, "until": until}
    return out


def parse_fallback_spec(spec: Any) -> tuple[str, str | None] | None:
    """Parse a fallback spec ('model', 'model@effort', {'model': m, 'effort': e}, or (m, e)) -> (model, effort)."""
    if isinstance(spec, str):
        s = spec.strip()
        if not s:
            return None
        model, sep, eff = s.partition("@")
        model = model.strip()
        return (model, eff.strip() or None) if model else None
    if isinstance(spec, dict):
        m = spec.get("model")
        if not isinstance(m, str) or not m.strip():
            return None
        e = spec.get("effort")
        return m.strip(), (e.strip() or None) if isinstance(e, str) else None
    if isinstance(spec, (tuple, list)) and len(spec) == 2 and isinstance(spec[0], str) and spec[0].strip():
        e = spec[1]
        return spec[0].strip(), (e.strip() or None) if isinstance(e, str) else None
    return None


def parse_fallback_flags(args: Sequence[str], value_flags: Collection[str] = ()) -> list[str] | None:
    """Extract repeatable `--fallback SPEC` / `--no-fallback` from argv without misreading another option's value.
    Raises ValueError on an invalid SPEC."""
    no_fb = False
    fbs: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--no-fallback":
            no_fb = True
            i += 1
        elif tok == "--fallback" and i + 1 < len(args):
            val = args[i + 1]
            if not SPEC_RE.fullmatch(val):
                raise ValueError(f"bad --fallback: {val!r}")
            fbs.append(val)
            i += 2
        elif tok in value_flags and i + 1 < len(args):
            i += 2
        else:
            i += 1
    if no_fb:
        return []
    return fbs or None


def _parse_config_group(raw: Any) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    if isinstance(raw, list):
        out.extend(parsed for item in raw if (parsed := parse_fallback_spec(item)))
    elif isinstance(raw, dict):
        order = list(dict.fromkeys([*(CONFIG.get("agents") or ()), *raw.keys()]))
        for key in order:
            if key not in raw:
                continue
            canon = canonical(key)
            if canon not in H:
                continue
            if parsed := parse_fallback_spec(raw[key]):
                m, e = parsed
                out.append((m if harness_of(m) == canon else f"{canon}:{m}", e))
    return out


def _config_fallbacks(slot: str | None = None) -> list[tuple[str, str | None]]:
    cfg = CONFIG.get("model_fallbacks")
    if not cfg:
        return []
    if isinstance(cfg, list):
        return _parse_config_group(cfg)
    if isinstance(cfg, dict):
        if slot and slot in cfg:
            return _parse_config_group(cfg[slot])
        if "default" in cfg:
            return _parse_config_group(cfg["default"])
        if any(canonical(k) in H for k in cfg):
            return _parse_config_group(cfg)
    return []


def default_model_for(name: str, slot: str | None = None) -> tuple[str | None, str | None]:
    """Default (model, effort) for harness `name` when falling back automatically."""
    c = conf(name)
    m = c.get("model")
    e = c.get("effort")
    if not m and name == "claude" and slot in ("page", "suggest", "naming", "decide"):
        m = HAIKU
    return (m if isinstance(m, str) and m else None), (e if isinstance(e, str) and e else None)


def _route_compat(model: str | None, default: str) -> tuple[str, str | None]:
    return route(model) if default == "claude" else route(model, default)


def fallback_chain(
    model: str | None,
    effort: str | None = None,
    fallbacks: Sequence[Any] | None = None,
    default: str = "claude",
    slot: str | None = None,
    allowed: Collection[str] | None = None,
    exclude: Collection[str] = (),
) -> list[tuple[str, str | None, str | None]]:
    """Ordered [(harness, model_id, effort)] starting with the primary, then at most one fallback per other provider.
    When `fallbacks` is explicitly supplied (including `[]`), only those providers are used after primary;
    when `fallbacks` is None, config `model_fallbacks` and remaining installed `agents`/`harnesses` auto-fill."""
    h0, m0 = _route_compat(model or (HAIKU if default == "claude" else default), default)
    chain: list[tuple[str, str | None, str | None]] = [] if h0 in exclude else [(h0, m0, effort)]
    seen = {h0, *exclude}
    explicit = [p for item in fallbacks if (p := parse_fallback_spec(item))] if fallbacks is not None else _config_fallbacks(slot)
    for fb_m, fb_e in explicit:
        fh, fm = _route_compat(fb_m, default)
        if fh in seen or (allowed is not None and fh not in allowed) or not installed(fh):
            continue
        if fh == "pi" and conf("pi").get("bypass") is not True:
            continue
        seen.add(fh)
        chain.append((fh, fm, fb_e if fb_e is not None else effort))
    if fallbacks is not None:
        return chain
    configured = dict.fromkeys([*(CONFIG.get("agents") or ("claude", "codex")), *(CONFIG.get("harnesses") or {}), default])
    for raw_h in configured:
        fh = canonical(raw_h)
        if fh not in H or fh in seen or (allowed is not None and fh not in allowed) or not installed(fh):
            continue
        if fh == "pi" and conf("pi").get("bypass") is not True:
            continue
        dm, de = default_model_for(fh, slot)
        seen.add(fh)
        chain.append((fh, dm, de if de is not None else effort))
    return chain


def order_by_availability(
    chain: Sequence[tuple[str, str | None, str | None]],
    now: float | None = None,
) -> tuple[list[tuple[str, str | None, str | None]], dict[str, str]]:
    """Put uncapped providers first (preserving order) and append capped ones last so we never falsely block."""
    ready: list[tuple[str, str | None, str | None]] = []
    capped: list[tuple[str, str | None, str | None]] = []
    reasons: dict[str, str] = {}
    for entry in chain:
        why = is_capped(entry[0], entry[1], now=now)
        if why:
            capped.append(entry)
            reasons[entry[0]] = why
        else:
            ready.append(entry)
    return ready + capped, reasons


def _emit_fallback(from_h: str, to_h: str, to_model: str | None, reason: str) -> None:
    tok_model = (to_model or "default").replace("/", ":")
    diag.event("model_fallback", harness=from_h, to=to_h, model=tok_model if diag.token(tok_model) else "custom", reason=reason)


_TOOLLESS_ISOLATE_CWD = frozenset({"opencode", "gemini", "agy"})


def ask(prompt, model=None, effort=None, default="claude", timeout=180, cwd=None, fallbacks=None, slot=None, exclude=()):
    """One toolless headless answer from whichever harness runs `model` (no model: `default` with its cheap default).
    Proactively skips providers known to be at their 5h/weekly cap or in cooldown, and falls back across configured
    providers on usage/rate limits, auth errors, or missing binaries. "" when it fails."""
    chain = fallback_chain(model, effort, fallbacks=fallbacks, default=default, slot=slot, exclude=exclude)
    if not chain:
        return ""
    ordered, skipped = order_by_availability(chain)
    if ordered[0][0] != chain[0][0]:
        _emit_fallback(chain[0][0], ordered[0][0], ordered[0][1], skipped.get(chain[0][0], "usage_cap"))
    with tempfile.TemporaryDirectory(prefix="greenlight-llm-") as empty:
        for idx, (h, m, eff) in enumerate(ordered):
            has_next = idx + 1 < len(ordered)
            try:
                cmd, text = argv(h, prompt, model=m, effort=eff, tools=False)
            except ValueError:
                if has_next:
                    _emit_fallback(h, ordered[idx + 1][0], ordered[idx + 1][1], "unavailable")
                    continue
                raise
            # Harnesses without a native tool-disable flag always run in an isolated empty tempdir.
            run_cwd = empty if h in _TOOLLESS_ISOLATE_CWD else (cwd or empty)
            try:
                p = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=timeout, cwd=run_cwd)
            except FileNotFoundError:
                if has_next:
                    _emit_fallback(h, ordered[idx + 1][0], ordered[idx + 1][1], "missing")
                    continue
                raise
            if p.returncode == 0:
                ans = answer(h, p.stdout)
                if ans:
                    clear_limit(h)
                    return ans
            combined = f"{p.stderr}\n{p.stdout}"
            why = fallback_reason(combined)
            if why == "rate_limit":
                record_limit(h, combined, model=m)
            if why and has_next:
                _emit_fallback(h, ordered[idx + 1][0], ordered[idx + 1][1], why)
                continue
            return ""
    return ""


if __name__ == "__main__":
    diag.install("harness")
    if sys.argv[1:] == ["models"]:
        print(json.dumps(models()))
    elif sys.argv[1:2] == ["route"] and len(sys.argv) == 3:
        print(json.dumps(route(sys.argv[2])))
    elif sys.argv[1:] == ["limits"]:
        print(json.dumps(limits_status()))
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
