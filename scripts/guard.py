#!/usr/bin/env python3
"""The egress and side-effect gate for unattended agents: a Claude Code PreToolUse hook and a CLI wrapper.

Every tool call goes through check(). Local reads, edits and test loops pass straight through (no opinion: the agent's
own permission mode decides). A call that crosses a boundary (a push, a forge write, network egress, a delete outside
the worktree, a secret or env read, removing a workspace, a write that could switch the guard off) gets a risk tier
from deterministic rules, then System 1 (decide.py's bool) judges it against the workspace goal, the repo's rules and
the invariants below, within a time budget (config `guard.budget_ms`, default 200):

  deny    an invariant is broken (a secret on its way out, a force push to the default branch, rm -rf ~): refused, always
  high    always pauses for the user
  medium  allowed when System 1 is sure it's aligned (prob >= 0.9, a real provider, within budget); else pauses
  low     allowed unless System 1 is sure it isn't aligned

A pause is a question to the user, by mode (GREENLIGHT_GUARD_MODE in the hook's env, else `guard.mode`, else ask):
  ask   Claude Code's own prompt, for a session someone is watching
  ping  unattended (automations, dash.py's calls; also GREENLIGHT_UNATTENDED=1): a Greenlight menubar ping with
        `allow | deny` the hook waits on for `guard.timeout` seconds (default 240), denied when unanswered. An allow is
        remembered for that exact call in that session for an hour. The answer reaches the guard through the menubar
        chat (chat.py records it), which the app holds while a chat turn runs: so a chat turn never waits on a ping...
  chat  ...the menubar chat's turns are denied with a [guard:token] instead, and Chief asks in the chat; when the user
        answers allow (chat.py hands the question and the answer over in GREENLIGHT_REQUEST), the same call runs.

    guard.py hook < PreToolUse JSON     # Claude Code hook: prints the decision JSON (nothing: no opinion)
    guard.py check [--json] -- <cmd>    # what the hook would decide for a shell command (exit 0 allow/pass, 1 pause, 2 deny)
    guard.py run -- <argv...>           # the CLI wrapper: check, pause if it must, then exec the command
    guard.py settings [--mode ping]     # the hooks block for a settings.json (or claude --settings)
    guard.py log [-n N]                 # the last decisions, with how long each took

config.json `guard`: {"on": true, "mode": "auto"|"ask"|"ping", "timeout": 240 (maximum 240), "budget_ms": 200,
  "hosts": [extra hosts a GET may reach], "allow": [regexes a call auto-allows], "deny": [regexes it refuses]}.
Off (no `guard` key, or "on": false) the hook says nothing. Every boundary decision is logged to ~/.greenlight/guard.jsonl.
Stdlib only; never imports greenlight.py on the fast path (it reads the forge's config); decide.py is optional.

Known limits: this reads the command line, not what a program does. A script file the agent wrote and then runs,
or code it decodes at run time, is opaque here (Write/Edit content is scanned for a secret-plus-network combination,
and codex's own sandbox blocks the network). It is a gate on the obvious paths, not a sandbox.
"""
import ast
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
HOME = Path.home()

INVARIANTS = """\
- No secret (token, key, password, credential file, env var holding one) leaves this machine: not in a URL, a request body, a PR/MR/issue comment, a commit pushed, a gist.
- No unattended push to the default branch or a different repo; force pushes to the default branch are denied.
- Writes go to the workspace's own repo on its forge, never another repo or an arbitrary host.
- No unattended delete or overwrite outside the worktree and temp dirs; the guard, its config and the agent's settings are never edited.
- No workspace is removed without human approval."""

# Hosts a plain GET may reach without asking: the forges and package indexes agents fetch from.
HOSTS = {"github.com", "api.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com", "codeload.github.com",
         "gitlab.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "crates.io", "static.crates.io",
         "proxy.golang.org", "docs.python.org", "developer.apple.com", "docs.anthropic.com", "docs.claude.com"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
TIERS = ["pass", "low", "medium", "high", "deny"]

SECRET_RE = re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gh[osu]_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{20,}"
                       r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}"
                       r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_NAME_RE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIALS?|COOKIE)(?:_|$)|(?:AUTH|SESSION)$|^PAT$", re.I)
SECRET_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# Files whose contents are credentials. Matched against the resolved path.
SECRET_PATH_RE = re.compile(r"(?:^|/)(?:\.ssh/|\.aws/|\.gnupg/|\.kube/config|\.docker/config\.json|\.netrc|\.git-credentials|\.npmrc|\.pypirc"
                            r"|\.config/gh/hosts\.yml|\.config/glab-cli/|\.config/gcloud/|Library/Keychains/|\.password-store/"
                            r"|\.env(?:\.(?!(?:example|sample|template|dist)$)[\w.-]+)?$|[^/]*credentials[^/]*\.json$|id_(?:rsa|ed25519|ecdsa|dsa)[^/]*$|[^/]+\.(?:pem|p12|pfx|key)$)")
# Writing these would switch the guard off or run code outside the agent's turn: refused.
GUARD_PATH_RE = re.compile(r"(?:^|/)\.claude/(?:settings(?:\.local)?\.json|skills/greenlight/)|(?:^|/)\.greenlight/(?:config\.json|guard)"
                           r"|(?:^|/)\.codex/config\.toml$")
ESCAPE_PATH_RE = re.compile(r"(?:^|/)(?:\.(?:bash|zsh)(?:rc|_profile|env)|\.profile|\.gitconfig|\.ssh/|\.config/git/|\.git/hooks/"
                            r"|Library/LaunchAgents/|\.config/systemd/|\.config/autostart/|\.local/bin/)|^/(?:etc|usr|bin|sbin|opt|Library|System)/")

SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
INTERPS = {"python", "python3", "node", "ruby", "perl", "php", "deno", "bun", "osascript"}
WRAPPERS = {"sudo", "doas", "command", "exec", "nohup", "time", "nice", "ionice", "stdbuf", "caffeinate", "setsid", "builtin"}
NET_TOOLS = {"curl", "wget", "http", "https", "xh", "aria2c", "httpie", "websocat"}
RAW_NET = {"nc", "ncat", "netcat", "socat", "telnet", "ftp", "sftp", "scp", "ssh", "rsync", "tftp", "mosh"}
NET_CODE_RE = re.compile(r"urllib|requests\.|http\.client|httpx|aiohttp|socket\.|fetch\(|XMLHttpRequest|net\.connect|https?\.request"
                         r"|Net::HTTP|LWP::|curl|wget|smtplib|ftplib|websocket", re.I)


def config():
    """The guard's settings, or None when it's off. A config.json that's there but unreadable runs the guard on its
    defaults: a typo must not switch it off."""
    try:
        c = json.loads((STATE / "config.json").read_text())
        c = c.get("guard") if isinstance(c, dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        c = {}
    return enabled(c)


def enabled(c):
    """config.json's `guard` value as settings, or None when it's off."""
    if c is True:
        c = {}
    if not isinstance(c, dict) or not c.get("on", True):
        return None
    out = dict(c)
    for k, d in (("timeout", 240), ("budget_ms", 200)):
        try:
            out[k] = max(1, int(c.get(k, d)))
        except (TypeError, ValueError):
            out[k] = d
    out["timeout"] = min(out["timeout"], 240)  # the skill's installed hook timeout is 270 seconds
    for k in ("allow", "deny", "hosts"):
        out[k] = [x for x in (c.get(k) or []) if isinstance(x, str)] if isinstance(c.get(k), list) else []
    return out


def matches(pat, text):
    """A user regex; a broken one matches nothing (so a bad allow rule can't allow and a bad deny rule is logged)."""
    try:
        return re.search(pat, text) is not None
    except re.error:
        return False


# ---------------------------------------------------------------- the deterministic part

class Verdict:
    def __init__(self):
        self.tier, self.why, self.flags = "pass", [], set()

    def add(self, tier, why, *flags):
        if TIERS.index(tier) > TIERS.index(self.tier):
            self.tier = tier
        if why and why not in self.why:
            self.why.append(why)
        self.flags.update(flags)

    def merge(self, other):
        for w in other.why:
            if w not in self.why:
                self.why.append(w)
        if TIERS.index(other.tier) > TIERS.index(self.tier):
            self.tier = other.tier
        self.flags |= other.flags


class Ctx:
    """Where the call runs: the worktree, its branch and default branch, the remotes (read lazily, git only when needed)."""

    def __init__(self, cwd, unknown=False, source_root=None):
        self.cwd = Path(os.path.realpath(cwd or os.getcwd()))
        self.unknown = unknown  # after a `cd` this can't follow: relative paths could be anywhere
        self.source_root = source_root
        self._git = {}

    def git(self, *args):
        if args not in self._git:
            try:
                r = subprocess.run(["git", "-C", str(self.cwd), *args], capture_output=True, text=True, timeout=2)
                self._git[args] = r.stdout.strip() if r.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                self._git[args] = ""
        return self._git[args]

    @property
    def root(self):
        """The worktree, or None outside a git repo (then only the temp dirs count as inside)."""
        top = self.git("rev-parse", "--show-toplevel")
        return Path(os.path.realpath(top)) if top else None

    @property
    def branch(self):
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    @property
    def default(self):
        head = self.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return {head.split("/", 1)[-1]} if head else {"main", "master", "trunk", "develop", "production", "release"}

    @property
    def remotes(self):
        return set(self.git("remote").split())

    def path(self, p, follow=True):
        """p resolved against cwd, symlinks followed (follow=False keeps the last part: rm deletes a link, not its target)."""
        p = os.path.expandvars(os.path.expanduser(p))
        p = os.path.normpath(p if os.path.isabs(p) else self.cwd / p)
        if follow:
            return Path(os.path.realpath(p))
        head, tail = os.path.split(p)
        return Path(os.path.join(os.path.realpath(head), tail))

    def inside(self, p):
        """In the worktree (not its .git) or a temp dir that isn't another repo (a sibling worktree under /tmp)."""
        s, root = str(p), (self.source_root or self.root) and str(self.source_root or self.root)
        if root and s.startswith(root + "/"):
            return "/.git/" not in s[len(root):] + "/" and not s.endswith("/.git")
        if any(s == t or s.startswith(t + "/") for t in temp_dirs()):
            near = Path(s)
            while not near.exists() and near != near.parent:
                near = near.parent
            top = Ctx(near if near.is_dir() else near.parent).git("rev-parse", "--show-toplevel")
            return not top or (root is not None and os.path.realpath(top) == root)
        return False


def temp_dirs():
    t = os.environ.get("TMPDIR", "")
    return {os.path.realpath(d).rstrip("/") for d in (t, "/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders",
                                                      "/private/var/folders", str(HOME / ".cache")) if d}


def secret_values():
    """Values of env vars that look like secrets, long enough not to match by chance."""
    return {v for k, v in os.environ.items() if SECRET_NAME_RE.search(k) and len(v) >= 12}


def leaks(text):
    """Why `text` holds a secret, or None."""
    if not text:
        return None
    if m := SECRET_RE.search(text):
        return f"holds what looks like a credential ({m[0][:6]}…)"
    if any(v in text for v in secret_values()):
        return "holds the value of a secret env var"
    return None


def redact(text):
    text = SECRET_RE.sub("[credential redacted]", text)
    text = re.sub(r"(?:^|\s)(?:-u\s*|--user(?:=|\s+))([^\s:]+):([^\s]+)",
                  lambda m: m[0].replace(m[2], "[password redacted]"), text)
    for value in secret_values():
        text = text.replace(value, "[credential redacted]")
    return text


def scrub(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def check_upload_file(source, ctx, how):
    """Inspect a file an outbound command will send; an uninspectable file cannot be silently allowed."""
    v = Verdict()
    if source == "-":
        v.add("high", f"{how} sends stdin that the guard cannot inspect", "egress")
        return v
    p = ctx.path(source)
    if SECRET_PATH_RE.search(str(p)):
        v.add("deny", f"{how} sends a credential file ({source})", "secret", "egress")
        return v
    try:
        with p.open("rb") as fh:
            data = fh.read(5_000_001)
    except OSError:
        v.add("high", f"cannot inspect {how} file ({source})", "egress")
        return v
    if len(data) > 5_000_000:
        v.add("deny", f"{how} file is too large to inspect ({source})", "egress")
    elif leak := leaks(data.decode(errors="replace")):
        v.add("deny", f"{how} file {leak}", "secret", "egress")
    return v


def secret_vars(text):
    return [n for n in SECRET_VAR_RE.findall(text or "") if SECRET_NAME_RE.search(n)]


def host_of(url):
    if "://" in url:
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""
    m = re.match(r"(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]*@)?(\[[^\]]+\]|[^:/?#\s]+)", url, re.I)
    return (m[1] if m else "").lower()


def strip_heredocs(script):
    """(script without heredoc bodies, [(command line it feeds, body)])."""
    lines, out, docs, i = script.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tags = heredoc_tags(line)
        i += 1
        for quote, tag in tags:
            body = []
            while i < len(lines) and lines[i].strip() != tag:
                body.append(lines[i])
                i += 1
            i += 1
            docs.append((line, "\n".join(body), not quote))  # an unquoted tag's body expands $VAR and $(...)
    return "\n".join(out), docs


def heredoc_tags(line):
    """Heredoc operators outside quoted shell text and comments."""
    tags, quote, i = [], None, 0
    while i < len(line):
        c = line[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "#" and (i == 0 or line[i - 1] in " \t;&|("):
            break
        if c in "'\"":
            quote = c
            i += 1
            continue
        if line.startswith("<<", i) and not line.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1(?![\w<])", line[i:])
            if m:
                tags.append((m[1], m[2]))
                i += len(m[0])
                continue
        i += 1
    return tags


def expanding(script):
    """The script with what the shell never expands blanked out: single-quoted spans and comments."""
    out, i, dq = [], 0, False
    while i < len(script):
        c = script[i]
        if c == "\\" and i + 1 < len(script):
            out.append(script[i:i + 2])
            i += 2
            continue
        if c == '"':
            dq = not dq
        elif c == "'" and not dq:
            j = script.find("'", i + 1)
            j = len(script) if j < 0 else j
            out.append(" " * (j + 1 - i))
            i = j + 1
            continue
        elif c == "#" and not dq and (i == 0 or script[i - 1] in " \t\n;&|("):
            j = script.find("\n", i)
            j = len(script) if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def substitutions(script):
    """The bodies of $(...) and `...`, which run even inside double quotes (not single ones)."""
    script = expanding(script)
    subs, i = [], 0
    while (j := script.find("$(", i)) >= 0:
        depth, k = 1, j + 2
        while k < len(script) and depth:
            depth += {"(": 1, ")": -1}.get(script[k], 0)
            k += 1
        subs.append(script[j + 2:k - 1])
        i = j + 2
    subs += re.findall(r"`([^`]*)`", script)
    return subs


def commands(script):
    """[(argv, output redirects, input redirects)] for each simple command, or None when it can't be parsed."""
    script = re.sub(r"(?<![\w$-])\d+(?=>)", "", script)  # 2>/dev/null: the fd isn't an argument
    lex = shlex.shlex(script.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        toks = list(lex)
    except ValueError:
        return None
    out, argv, redir, inputs, want, piped = [], [], [], [], None, False
    for t in toks:
        if want:
            if not re.fullmatch(r"&\d*|\d+", t):  # 2>&1, >&2
                (redir if want == "write" else inputs if want == "read" else []).append(t)
                if want == "herestring":
                    inputs.append("<<<" + t)
            want = None
        elif t in (">", ">>", ">|", "&>", "&>>", ">&", "<&", "&>|"):
            want = "write" if t != "<&" else "skip"
        elif t and set(t) <= set(";&|()"):
            if argv or redir or inputs:
                out.append((argv, redir, inputs, piped))
            argv, redir, inputs, piped = [], [], [], t == "|"
        elif t in ("<", "<<", "<<<", "<<-"):
            want = "read" if t == "<" else "herestring" if t == "<<<" else "skip"
        else:
            argv.append(t)
    if argv or redir or inputs:
        out.append((argv, redir, inputs, piped))
    return out


def unwrap(argv):
    """The real command under sudo/env/timeout/xargs and leading VAR=value assignments."""
    v = list(argv)
    while v:
        b = os.path.basename(v[0])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", v[0]):
            v = v[1:]
        elif b in WRAPPERS:
            v = v[1:]
            while v and v[0].startswith("-"):
                takes_value = b == "sudo" and v[0] in ("-u", "-g", "-h", "-p", "--user", "--group", "--host", "--prompt")
                v = v[1:]
                if takes_value:
                    v = v[1:]
        elif b == "env":
            rest = v[1:]
            while rest and (rest[0].startswith("-") or "=" in rest[0]):
                takes_value = rest[0] in ("-u", "--unset")
                rest = rest[1:]
                if takes_value:
                    rest = rest[1:]
            if not rest:  # bare `env` dumps the environment
                return ["env"]
            v = rest
        elif b == "timeout":
            v = v[1:]
            while v and (v[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", v[0])):
                v = v[1:]
        elif b == "xargs":
            v = v[1:]
            while v and v[0].startswith("-"):
                v = v[2:] if v[0] in ("-I", "-n", "-P", "-L", "-d", "-s", "-E") else v[1:]
        else:
            break
    return v


def check_shell(script, ctx, depth=0):
    v = Verdict()
    if depth > 4:
        v.add("high", "nested too deep to read")
        return v
    if leak := leaks(script):
        v.add("medium", f"the command {leak}", "secret")
    body, docs = strip_heredocs(script)
    live = expanding(body) + "".join("\n" + d for _, d, expands in docs if expands)
    # a secret var sent in a forge auth header is how agents call the forge API: check_net judges that one
    auth_arg = re.compile(r"(?i)(?:-H|--header)(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;]+)")
    masked = auth_arg.sub(lambda m: "" if re.search(r"(?i)authorization|private-token|x-api-key|job-token", m[0]) else m[0], live)
    for n in secret_vars(masked):
        v.add("high", f"reads secret env var ${n}", "secret")
    for line, doc, expands in docs:
        head = commands(line) or []
        first = unwrap(head[0][0]) if head and head[0][0] else []
        name = os.path.basename(first[0]) if first else ""
        if name in SHELLS:
            v.merge(check_shell(doc, ctx, depth + 1))
        elif name.rstrip("0123456789.") in INTERPS:
            v.merge(check_code(doc, name, ctx, depth + 1))
        elif expands:
            for sub in substitutions(doc):
                v.merge(check_shell(sub, ctx, depth + 1))
    for sub in substitutions(body):
        v.merge(check_shell(sub, ctx, depth + 1))
    cmds = commands(body)
    if cmds is None:
        v.add("medium", "can't parse the command line")
        if NET_CODE_RE.search(body) or re.search(r"\b(?:rm|git\s+push|gh|glab)\b", body):
            v.add("high", "unparseable and names a network or destructive tool")
        return v
    parts = []
    for argv, redir, inputs, piped in cmds:
        if any(os.path.basename(x) == "env" for x in argv) and any(x in ("-S", "--split-string") or x.startswith("--split-string=") for x in argv):
            v.add("high", "env split-string runs a command the guard cannot safely parse")
        for r in redir:
            v.merge(check_write(ctx.path(r), ctx, "redirect", raw=r))
        for r in inputs:
            if r.startswith("<<<"):
                continue
            if SECRET_PATH_RE.search(str(ctx.path(r))):
                v.add("high", f"reads a credential file through input redirection ({r})", "secret")
        for item in argv:
            if item.startswith("GIT_SSH_COMMAND="):
                v.add("high", "command-local SSH command can run during git network operations", "egress")
                v.merge(check_shell(item.split("=", 1)[1], ctx, depth + 1))
            if item.startswith("GIT_CONFIG_"):
                v.add("high", "command-local Git configuration may change a push target", "egress")
            if item.startswith(("GH_REPO=", "GLAB_REPO=")) and not own_repo(item.split("=", 1)[1], ctx):
                v.add("high", "forge variable selects another repository", "egress")
            if item.startswith(("GH_HOST=", "GITLAB_HOST=")) and host_of(item.split("=", 1)[1]) != host_of(ctx.git("remote", "get-url", "origin")):
                v.add("high", "forge host differs from this workspace's origin", "egress")
        if piped and any(os.path.basename(item) == "xargs" for item in argv):
            v.add("high", "xargs runs a command with file paths from a pipe")
        argv = unwrap(argv)
        if argv:
            name = os.path.basename(argv[0])
            for r in inputs:
                if r.startswith("<<<") and name in SHELLS:
                    v.merge(check_shell(r[3:], ctx, depth + 1))
                elif r.startswith("<<<") and name.rstrip("0123456789.") in INTERPS:
                    v.merge(check_code(r[3:], name, ctx, depth + 1))
        if argv and argv[0] in ("if", "then", "fi", "for", "do", "done", "while", "until", "case", "esac"):
            v.add("high", "shell control syntax contains commands this parser cannot isolate")
            continue
        if argv and os.path.basename(argv[0]) in ("cd", "pushd", "popd"):
            to = argv[1] if len(argv) > 1 and argv[0] != "popd" else ("~" if argv[0] == "cd" else None)
            if to is None or to == "-" or "$" in to or "`" in to:
                ctx = Ctx(ctx.cwd, unknown=True, source_root=ctx.source_root or ctx.root)
            else:
                ctx = Ctx(ctx.path(to), unknown=ctx.unknown and not os.path.isabs(os.path.expanduser(to)),
                          source_root=ctx.source_root or ctx.root)
            continue
        if argv:
            part = check_argv(argv, ctx, depth, piped_from=piped)
            parts.append(part)
            v.merge(part)
    pure_separate_get = len(parts) > 1 and not any(piped for _, _, _, piped in cmds) and not substitutions(body) and not secret_vars(live) and all(
        "egress" not in part.flags or part.tier == "low" and "secret" not in part.flags for part in parts)
    if "secret" in v.flags and "egress" in v.flags and not pure_separate_get:
        v.add("deny", "reads a secret and sends data off the machine in one command")
    return v


def check_code(code, lang, ctx=None, depth=0):
    v = Verdict()
    net = NET_CODE_RE.search(code)
    if net:
        v.add("medium", f"{lang} code that reaches the network ({net[0]})", "egress")
    named = re.findall(r"(?:environ(?:\.get)?\s*[\[(]\s*|getenv\(\s*|process\.env\.|ENV\[\s*)['\"]?([A-Za-z_]\w*)", code)
    dump = re.search(r"os\.environ\b(?!\s*[\[.])|process\.env\b(?!\.)|dict\(os\.environ|environ\.(?:items|copy|values)\(", code)
    env_read = re.search(r"os\.environ\b|os\.getenv\s*\(|process\.env\b|ENV\s*\[", code)
    literal_secret_path = any(SECRET_PATH_RE.search(s) for s in re.findall(r"['\"]([^'\"]+)['\"]", code))
    assembled_secret_path = bool(re.search(r"\.aws.{0,100}credentials|\.ssh.{0,100}id_(?:rsa|ed25519|ecdsa)", code, re.S))
    if SECRET_PATH_RE.search(code) or literal_secret_path or assembled_secret_path or secret_vars(code) or dump or any(SECRET_NAME_RE.search(n) for n in named):
        v.add("medium" if not net else "deny", "code that reads secrets or the environment" + (" and sends data out" if net else ""), "secret")
    elif net and env_read:
        benign = {"PATH", "HOME", "PWD", "TMPDIR", "LANG", "USER", "SHELL"}
        v.add("high" if named and set(named) <= benign else "deny",
              "code reads an environment variable and reaches the network", "egress")
    if re.search(r"rmtree|os\.remove|unlink|rm -rf|fs\.rm", code):
        v.add("medium", f"{lang} code that deletes files")
    if re.search(r"child_process|execSync|spawnSync|\.exec\s*\(|\.spawn\s*\(", code):
        v.add("high", f"{lang} code launches a command the guard cannot fully inspect")
    if re.search(r"\.greenlight.{0,100}(?:config\.json|guard)|\.claude.{0,100}(?:settings(?:\.local)?\.json|skills.{0,100}greenlight)|\.codex.{0,100}config\.toml", code, re.S) and re.search(
            r"write_text|write_bytes|\.write\s*\(|open\s*\(|os\.remove|unlink|rmtree|fs\.write|fs\.rm|writeFile|rmSync", code):
        v.add("deny", f"{lang} code that can change the guard or agent permissions")
    if leak := leaks(code):
        v.add("deny" if net else "medium", f"the code {leak}", "secret")
    if lang.startswith("python") and ctx and depth <= 4:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            tree = None
        if tree:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                call = f"{func.value.id}.{func.attr}" if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else ""
                if isinstance(func, ast.Name) and func.id == "open" and len(node.args) > 1:
                    try:
                        write_mode = any(c in ast.literal_eval(node.args[1]) for c in "wax+")
                    except (ValueError, TypeError, SyntaxError):
                        write_mode = True
                    if write_mode:
                        try:
                            path = ast.literal_eval(node.args[0])
                        except (ValueError, TypeError, SyntaxError):
                            path = None
                        if isinstance(path, str):
                            v.merge(check_write(ctx.path(path), ctx, "inline Python write", raw=path))
                        else:
                            v.add("high", "inline Python writes to a path the guard cannot resolve")
                if isinstance(func, ast.Attribute) and func.attr in ("write_text", "write_bytes"):
                    base = func.value
                    try:
                        path = ast.literal_eval(base.args[0]) if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path" else None
                    except (ValueError, TypeError, SyntaxError, IndexError):
                        path = None
                    if isinstance(path, str):
                        v.merge(check_write(ctx.path(path), ctx, "inline Python write", raw=path))
                    else:
                        v.add("high", "inline Python writes to a path the guard cannot resolve")
                if call == "shutil.rmtree":
                    try:
                        target = ast.literal_eval(node.args[0])
                    except (ValueError, TypeError, SyntaxError):
                        target = "~" if "Path.home()" in ast.unparse(node.args[0]) else None
                    if isinstance(target, str):
                        v.merge(check_rm([target], ctx))
                    else:
                        v.add("high", "shutil.rmtree target cannot be resolved")
                    continue
                if call not in ("os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
                                "subprocess.check_output", "subprocess.Popen"):
                    continue
                try:
                    command = ast.literal_eval(node.args[0])
                except (ValueError, TypeError, SyntaxError):
                    v.add("high", f"{call} runs a command the guard cannot inspect")
                    continue
                if isinstance(command, str):
                    v.merge(check_shell(command, ctx, depth + 1))
                elif isinstance(command, (list, tuple)) and all(isinstance(x, str) for x in command):
                    v.merge(check_shell(shlex.join(command), ctx, depth + 1))
    return v


def check_argv(argv, ctx, depth, piped_from=None):
    v = Verdict()
    cmd, args = os.path.basename(argv[0]), argv[1:]
    if cmd.endswith("greenlight.py") or cmd in ("orca", "orca-ide") or (cmd.startswith("python") and any(a.endswith("greenlight.py") for a in args[:2])):
        rest = args[args.index("ade") + 1:] if "ade" in args else args
        if len(rest) >= 2 and rest[0] == "worktree" and rest[1] in ("rm", "remove", "delete"):
            v.add("high", "removes a workspace")
        if "terminal" in rest:
            for flag in ("--text", "--command"):
                payload = opt(args, flag)
                if payload:
                    v.merge(check_shell(payload, ctx, depth + 1))
        return v
    if cmd in SHELLS or cmd == "eval":
        if cmd == "eval":
            v.merge(check_shell(" ".join(args), ctx, depth + 1))
        elif (command_flag := next((i for i, a in enumerate(args) if a == "-c" or re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a)), None)) is not None and command_flag + 1 < len(args):
            v.merge(check_shell(args[command_flag + 1], ctx, depth + 1))
        elif piped_from:
            v.add("high", f"pipes into {cmd}: runs code this can't read")
        return v
    if cmd.rstrip("0123456789.") in INTERPS:
        if "-m" in args and args.index("-m") + 2 < len(args) and args[args.index("-m") + 1] in ("pip", "pip3", "pipx", "uv") and args[args.index("-m") + 2] in ("install", "add", "get"):
            v.add("high", f"{cmd} module installs packages that may run scripts with credentials")
        for flag in ("-c", "-e", "--eval", "-r"):
            if flag in args and args.index(flag) + 1 < len(args):
                v.merge(check_code(args[args.index(flag) + 1], cmd, ctx, depth + 1))
        for arg in args:
            if re.match(r"^-[cer].+", arg) and arg not in ("-c", "-e", "-r"):
                v.merge(check_code(arg[2:], cmd, ctx, depth + 1))
        if piped_from and not any(a.endswith((".py", ".js", ".rb", ".pl", ".ts")) for a in args) and "-m" not in args:
            v.add("high", f"pipes into {cmd}: runs code this can't read")
        if cmd in ("perl", "ruby") and any(re.fullmatch(r"-\w*i\w*|--in-place.*", a) for a in args):
            for a in args:
                if a.startswith("-") or a == opt(args, "-e"):
                    continue
                if GUARD_PATH_RE.search(str(ctx.path(a))):
                    v.merge(check_write(ctx.path(a), ctx, f"{cmd} in-place", raw=a))
        return v
    if cmd == "git":
        return check_git(args, ctx)
    if cmd in ("gh", "glab"):
        return check_forge(cmd, args, ctx)
    if cmd == "tar":
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
        extracting = "x" in flags or "--extract" in args
        creating = "c" in flags or "--create" in args
        dest = opt(args, "-C", "--directory") or "."
        if extracting:
            p = ctx.path(dest)
            if GUARD_PATH_RE.search(str(p / "config.json")) or GUARD_PATH_RE.search(str(p / "settings.json")):
                v.add("deny", f"tar extraction can overwrite guarded files in {dest}")
            else:
                v.merge(check_write(p, ctx, "tar extraction", raw=dest))
            if "P" in flags or "--absolute-names" in args:
                v.add("high", "tar extraction preserves absolute and parent paths")
            archive = opt(args, "-f", "--file") or next((a[2:] for a in args if a.startswith("-f") and len(a) > 2), None)
            archive = archive or next((args[i + 1] for i, a in enumerate(args)
                                       if re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", a) and i + 1 < len(args)), None)
            if archive:
                try:
                    with tarfile.open(ctx.path(archive), "r:*") as fh:
                        for i, member in enumerate(fh):
                            if i >= 1000:
                                v.add("high", "tar archive has too many entries to inspect")
                                break
                            target = ctx.path(os.path.join(dest, member.name))
                            v.merge(check_write(target, ctx, "tar extraction", raw=member.name))
                            if member.issym() or member.islnk():
                                v.add("high", "tar archive contains links that can redirect extraction")
                except (OSError, tarfile.TarError):
                    v.add("high", "cannot inspect tar extraction contents")
            else:
                v.add("high", "tar extraction archive cannot be identified")
        if creating:
            sources = [ctx.path(a) for a in args if not a.startswith("-") and a != opt(args, "-f", "--file")]
            for source in sources:
                if SECRET_PATH_RE.search(str(source)) or str(source).startswith(str(HOME / ".ssh")):
                    v.add("deny", "tar archive would package credentials", "secret")
                elif source.is_dir():
                    for count, (base, _, files) in enumerate(os.walk(source)):
                        if count >= 1000:
                            v.add("high", "tar input tree is too large to inspect")
                            break
                        if any(SECRET_PATH_RE.search(str(Path(base) / name)) for name in files):
                            v.add("deny", "tar archive would package a nested credential file", "secret")
                            break
        return v
    if cmd in ("unzip", "7z", "7za"):
        directory = opt(args, "-d", "--output") or "."
        target_dir = ctx.path(directory)
        if GUARD_PATH_RE.search(str(target_dir / "config.json")) or GUARD_PATH_RE.search(str(target_dir / "settings.json")):
            v.add("deny", f"{cmd} extraction can overwrite guarded files in {directory}")
        else:
            v.merge(check_write(target_dir, ctx, f"{cmd} extraction", raw=directory))
        if cmd != "unzip":
            v.add("high", f"{cmd} archive entries cannot be inspected")
            return v
        archive = next((a for a in args if not a.startswith("-") and a != directory), None)
        if archive:
            try:
                with zipfile.ZipFile(ctx.path(archive)) as fh:
                    for i, name in enumerate(fh.namelist()):
                        if i >= 1000:
                            v.add("high", "zip archive has too many entries to inspect")
                            break
                        v.merge(check_write(ctx.path(os.path.join(directory, name)), ctx, "unzip extraction", raw=name))
            except (OSError, zipfile.BadZipFile):
                v.add("high", "cannot inspect zip extraction contents")
        else:
            v.add("high", "zip extraction archive cannot be identified")
        return v
    if cmd in NET_TOOLS:
        return check_net(cmd, args, ctx)
    if cmd in RAW_NET:
        dest = next((a for a in args if not a.startswith("-")), "")
        if cmd == "rsync" and not any(":" in a for a in args if not a.startswith("-")):
            files = [a for a in args if not a.startswith("-")]
            if files:
                v.merge(check_write(ctx.path(files[-1]), ctx, "rsync", raw=files[-1]))
            return v
        if host_of(dest.split(":")[0]) in LOCAL_HOSTS:
            if cmd == "ssh" and len(args) > args.index(dest) + 1:
                v.merge(check_shell(" ".join(args[args.index(dest) + 1:]), Ctx(ctx.cwd, unknown=True, source_root=ctx.root), depth + 1))
            return v
        v.add("high", f"{cmd} opens a connection off the machine", "egress")
        if cmd in ("scp", "rsync") and args:
            target = args[-1]
            if ":" in target or "@" in target:
                for source in (a for a in args[:-1] if not a.startswith("-") and ":" not in a):
                    v.merge(check_upload_file(source, ctx, cmd))
        return v
    if cmd in ("rm", "rmdir", "unlink", "shred", "trash", "srm"):
        return check_rm(args, ctx)
    if cmd == "find" and ("-delete" in args or any(a in ("-exec", "-execdir", "-ok") for a in args)):
        roots = []
        for a in args:  # the starting points come before the first expression
            if a.startswith(("-", "(", ")", "!")):
                break
            roots.append(a)
        if "-delete" in args or re.search(r"\brm\b", " ".join(args)):
            for r in roots or ["."]:  # a root in the worktree only bounds what it deletes, inside it
                v.merge(check_rm(["-r", r], ctx))
        for flag in ("-exec", "-execdir", "-ok"):
            if flag in args:
                payload = args[args.index(flag) + 1:]
                payload = [a for a in payload if a not in ("{}", "+", ";")]
                if payload:
                    v.merge(check_argv(payload, ctx, depth + 1))
        return v
    if cmd in ("dd", "mkfs", "diskutil", "fdisk", "parted") or cmd.startswith("mkfs."):
        v.add("high", f"{cmd} writes disks")
        return v
    if cmd in ("tee", "cp", "mv", "install", "ln", "truncate", "chmod", "chown", "touch", "mkdir", "rsync"):
        files = [a for a in args if not a.startswith("-")]
        target_dir = (opt(args, "-t", "--target-directory") or next((a[2:] for a in args if a.startswith("-t") and len(a) > 2), None)) if cmd in ("cp", "mv", "install", "ln") else None
        if target_dir:
            sources = [a for a in files if a != target_dir]
            targets = [str(ctx.path(target_dir) / os.path.basename(src)) for src in sources]
        else:
            targets, sources = (files[-1:], files[:-1]) if cmd in ("cp", "mv", "install", "ln", "rsync") else (files, [])
            if targets and cmd in ("cp", "mv", "install", "ln", "rsync"):
                dest = targets[0]
                if dest.endswith("/") or ctx.path(dest).is_dir():
                    targets = [str(ctx.path(dest) / os.path.basename(src.rstrip("/"))) for src in sources]
        for t in targets:
            v.merge(check_write(ctx.path(t), ctx, cmd, raw=t))
        for src in sources:
            p = ctx.path(src, follow=cmd != "ln")
            if cmd == "mv":  # moving a file away deletes it where it was
                v.merge(check_rm([src], ctx))
            elif GUARD_PATH_RE.search(str(p)) and cmd == "ln":
                v.add("deny", f"links to {src}: a write through the link would change the guard")
            if SECRET_PATH_RE.search(str(p)):
                v.add("high", f"{cmd} copies a credential file", "secret")
        return v
    if cmd in ("sed", "gsed", "perl", "ruby") and any(re.fullmatch(r"-\w*i.*|--in-place.*", a) for a in args):
        for a in args[1:]:
            if not a.startswith("-") and (os.path.exists(ctx.path(a)) or "/" in a):
                v.merge(check_write(ctx.path(a), ctx, f"{cmd} -i", raw=a))
        return v
    if cmd == "set" and not args or cmd in ("env", "printenv", "export", "declare") and (not args or args in (["-p"], ["-x"])):
        v.add("high", f"`{cmd}` dumps the environment, secrets included", "secret")
        return v
    if cmd == "printenv" and any(SECRET_NAME_RE.search(a) for a in args):
        v.add("high", "reads a secret env var", "secret")
        return v
    if cmd == "security" and args and re.match(r"find-(generic|internet)-password|dump-keychain|export", args[0]):
        v.add("high", "reads the Keychain", "secret")
        return v
    if cmd in ("secret-tool", "op", "pass", "gopass", "vault", "bw") or (cmd in ("aws", "gcloud") and "get" in " ".join(args[:3])):
        v.add("high", f"{cmd} reads a secret store", "secret")
        return v
    if cmd in ("pip", "pip3", "npm", "pnpm", "yarn", "cargo", "go", "gem", "brew", "apt", "apt-get") and args[:1] and args[0] in (
            "install", "add", "i", "get"):
        v.add("high", f"{cmd} installs packages that may run scripts with the agent's credentials")
        return v
    if cmd in ("npx", "pnpx", "bunx", "uvx", "pipx") and args:
        v.add("high", f"{cmd} fetches and runs package code with credentials")
        return v
    # any other command: read its file arguments for credentials, and keep it off the guard's own files
    for a in args:
        if a.startswith("-"):
            continue
        p = ctx.path(a.split("=", 1)[-1] if a.startswith("--") else a)
        if SECRET_PATH_RE.search(str(p)) or p.is_dir() and p.name in (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store"):
            v.add("high", f"reads a credential file ({a})", "secret")
        elif GUARD_PATH_RE.search(str(p)) and cmd not in READERS:
            v.add("deny", f"{cmd} on {a}: it could change the guard or the agent's own permissions")
    return v


READERS = {"cat", "less", "more", "head", "tail", "grep", "rg", "ag", "jq", "wc", "diff", "cmp", "ls", "stat", "file", "md5sum",
           "sha256sum", "shasum", "bat", "view", "tree", "readlink", "realpath", "test", "["}


def check_git(args, ctx, depth=0):
    v = Verdict()
    if depth > 3:
        v.add("high", "Git alias chain is too deep to inspect")
        return v
    a = [part for arg in args for part in (["-c", arg[2:]] if arg.startswith("-c") and len(arg) > 2 else [arg])]
    source_root = ctx.source_root or ctx.root
    push_override = False
    while a and a[0].startswith("-"):  # git -C dir -c k=v ...
        if a[0] == "-C" and len(a) > 1:
            ctx = Ctx(ctx.path(a[1]), source_root=source_root)
            a = a[2:]
        elif a[0] in ("-c", "--git-dir", "--work-tree", "--namespace") and len(a) > 1:
            if a[0] in ("--git-dir", "--work-tree", "--namespace"):
                v.add("high", f"git {a[0]} changes the effective repository")
            if a[0] == "-c" and re.match(r"(?i)(core\.(sshCommand|hooksPath|fsmonitor|pager|editor)|credential\.|url\.|alias\.)", a[1]):
                v.add("high", f"git -c {a[1].split('=')[0]} runs or routes things outside the repo")
            if a[0] == "-c" and re.match(r"(?i)(remote\..*\.(?:push|pushurl|url|mirror)|remote\.pushDefault|branch\..*\.(?:remote|pushRemote|merge)|push\.default)=", a[1]):
                push_override = True
                v.add("high", f"git -c {a[1].split('=')[0]} changes the effective push target", "egress")
                if a[1].split("=", 1)[1].startswith("+"):
                    v.add("deny", "git -c force refspec can update an uninspected branch", "egress")
            a = a[2:]
        else:
            if a[0].startswith(("--git-dir=", "--work-tree=", "--namespace=")):
                v.add("high", "git option changes the effective repository")
            a = a[1:]
    if not a:
        return v
    sub, rest = a[0], a[1:]
    if alias := ctx.git("config", "--get", f"alias.{sub}"):
        if alias.startswith("!"):
            v.add("high", "Git shell alias may run arbitrary commands")
            v.merge(check_shell(alias[1:], ctx, depth + 1))
        else:
            try:
                v.merge(check_git(shlex.split(alias) + rest, ctx, depth + 1))
            except ValueError:
                v.add("high", "Git alias cannot be parsed")
        return v
    if sub == "push":
        if os.environ.get("GIT_SSH_COMMAND"):
            v.add("high", "inherited SSH command can run during git push", "egress")
            v.merge(check_shell(os.environ["GIT_SSH_COMMAND"], ctx, 1))
        if any(k.startswith("GIT_CONFIG_KEY_") and re.match(r"(?i)(remote\.|branch\.|push\.|url\.)", value)
               for k, value in os.environ.items()):
            v.add("high", "inherited Git configuration may change the push target", "egress")
        if push_override and ("--force" in rest or "-f" in rest or any(x.startswith("--force-with-lease") for x in rest)):
            v.add("deny", "force push with command-local routing could update the default branch", "egress")
        if ctx.unknown:
            v.add("high", "pushes after an unknown directory change", "egress")
        if source_root and ctx.root != source_root:
            v.add("high", "pushes from a different workspace", "egress")
        return check_push(rest, ctx, v)
    if sub == "remote" and rest[:1] and rest[0] in ("add", "set-url"):
        v.add("medium", f"git remote {rest[0]}: a new place to push to", "egress")
    elif sub == "config" and any(re.match(r"(?i)(core\.(sshCommand|hooksPath|fsmonitor)|credential\.|url\..*insteadof|remote\..*\.(push)?url|alias\.)", x) for x in rest):
        v.add("high", "git config that routes pushes or runs commands")
    elif sub == "config" and any(x in ("--global", "--system") for x in rest) and not any(x in ("--get", "--list", "-l", "--get-all") for x in rest):
        v.add("medium", "changes git config outside the repo")
    elif sub == "clone":
        url = next((x for x in rest if not x.startswith("-")), "")
        if host_of(url) not in HOSTS | LOCAL_HOSTS and re.match(r"[\w+.-]+://|[^/]+@[^/]+:", url):
            v.add("low", f"clones from {host_of(url)}", "egress")
    elif sub in ("fetch", "pull", "ls-remote") and any(re.match(r"[\w+.-]+://|[^/]+@[^/]+:", x) for x in rest):
        v.add("low", f"git {sub} reaches an explicit network URL", "egress")
    elif sub == "reset" and "--hard" in rest:
        v.add("medium", "git reset --hard drops uncommitted work")
    elif sub == "restore":
        v.add("medium", "git restore can discard uncommitted work")
    elif sub == "checkout" and "--" in rest:
        v.add("medium", "git checkout -- can discard uncommitted work")
    elif sub == "checkout" and any(x in ("-f", "--force") for x in rest) or sub == "switch" and any(x in ("-f", "--force", "--discard-changes") for x in rest):
        v.add("medium", f"git {sub} discards uncommitted work")
    elif sub in ("checkout", "switch") and any(x in ("-B", "-C", "--force-create") for x in rest):
        v.add("high", f"git {sub} can reset an existing branch tip")
    elif sub == "clean" and any(re.match(r"-\w*f", x) or x == "--force" for x in rest):
        v.add("medium", "git clean deletes untracked files")
    elif sub == "stash" and rest[:1] == ["clear"]:
        v.add("medium", "git stash clear discards every stash")
    elif sub == "stash" and rest[:1] == ["drop"]:
        v.add("high", "drops stashed work")
    elif sub == "branch" and any(x in ("-D", "--delete", "-d", "-M", "--force") for x in rest):
        v.add("high" if any(x in ("-D", "--delete", "-M", "--force") for x in rest) else "low", f"git {sub} changes branch references")
    elif sub == "worktree" and rest[:1] and rest[0] in ("remove", "prune"):
        v.add("high", "removes a git worktree")
    elif sub in ("filter-branch", "filter-repo") or sub == "update-ref" and ("-d" in rest or any("refs/remotes/" in x for x in rest)) or sub == "reflog" and "expire" in rest:
        v.add("medium", f"git {sub} rewrites history")
    elif sub == "send-email" or sub == "credential" or sub == "archive" and "--remote" in " ".join(rest):
        v.add("high", f"git {sub} reaches outside the repo", "egress")
    return v


def check_push(rest, ctx, v):
    pos = [x for x in rest if not x.startswith("-")]
    flags = [x for x in rest if x.startswith("-")]
    force = any(f in ("-f", "--force") or f.startswith("--force-with-lease") or f == "--force-if-includes" or re.fullmatch(r"-\w*f\w*", f)
                for f in flags) or any(p.startswith("+") for p in pos[1:])
    if force and not ctx.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD"):
        v.add("deny", "force push cannot prove the default branch without origin/HEAD", "egress")
    remote = pos[0] if pos else (ctx.git("config", "--get", f"branch.{ctx.branch}.pushRemote") or
                                 ctx.git("config", "--get", "remote.pushDefault") or
                                 ctx.git("config", "--get", f"branch.{ctx.branch}.remote") or "origin")
    push_default = ctx.git("config", "--get", "push.default") or "simple"
    if len(pos) < 2 and push_default not in ("simple", "current"):
        v.add("high", f"git push with push.default={push_default} may send other refs", "egress")
        if force:
            v.add("deny", "force push may update the default branch through push.default", "egress")
    if len(pos) < 2 and ctx.git("config", "--get-all", f"remote.{remote}.push"):
        v.add("high", f"git push uses configured refspecs for {remote}", "egress")
        if force:
            v.add("deny", "force push uses configured refspecs that may include the default branch", "egress")
    if remote in ctx.remotes:
        if ctx.git("config", "--bool", "--get", f"remote.{remote}.mirror") == "true":
            v.add("deny", "configured mirror push can force-update the default branch", "egress")
        destinations = ctx.git("remote", "get-url", "--push", "--all", remote).splitlines()
        project = workspace_project(ctx)
        origin_host = host_of(ctx.git("remote", "get-url", "origin"))
        if not destinations or not project or origin_host not in ("github.com", "gitlab.com") or any(
                repo_slug(dest) != project.lower() or host_of(dest) != origin_host for dest in destinations):
            v.add("high", f"pushes to {remote}, which points outside this workspace's repo", "egress")
    if remote not in ctx.remotes and remote != "origin" and ("/" in remote or ":" in remote or "@" in remote):
        v.add("high", f"pushes to {remote}, not one of the repo's remotes", "egress")
    if any(f in ("--mirror", "--all", "--tags", "--prune") for f in flags):
        v.add("high", "pushes every branch or tag, or deletes remote ones", "egress")
    if any(f in ("-o", "--push-option") or f.startswith(("-o", "--push-option=")) for f in flags):
        v.add("high", "git push option can change merge request behavior", "egress")
    if "--follow-tags" in flags or "--tags" in flags or ctx.git("config", "--bool", "--get", "push.followTags") == "true":
        v.add("high", "pushes tag objects that need inspection", "egress")
        v.merge(check_tag_objects(ctx))
    if "--mirror" in flags or (force and "--all" in flags):
        v.add("deny", "force-pushes all branches, including the default branch", "egress")
    if ":" in pos[1:]:
        v.add("deny" if force else "high", "matching refspec can update the default branch", "egress")
    if any(f in ("-d", "--delete") for f in flags) or any(p.startswith(":") and p != ":" for p in pos[1:]):
        v.add("high", "deletes a remote branch", "egress")
    refs = pos[1:] or [ctx.branch or "HEAD"]
    for r in refs:
        dst = r.lstrip("+").split(":")[-1] if ":" in r else r.lstrip("+")
        dst = ctx.branch if dst == "HEAD" else dst
        dst = re.sub(r"^refs/heads/", "", dst)
        if dst in ctx.default:
            if force:
                v.add("deny", f"force-pushes the default branch ({dst})", "egress")
            else:
                v.add("high", f"pushes straight to the default branch ({dst})", "egress")
        elif dst != ctx.branch and dst:
            v.add("deny" if force else "medium", f"{'force-pushes' if force else 'pushes'} {dst}, not this workspace's branch ({ctx.branch})", "egress")
        elif force:
            v.add("medium", f"force-pushes {dst}", "egress")
        else:
            v.add("low", f"pushes {dst}", "egress")
    if v.tier != "deny" and "--all" not in flags and "--mirror" not in flags:
        for ref in refs:
            source, _, target = ref.lstrip("+").partition(":")
            if source:
                v.merge(check_outgoing_commits(ctx, remote, source, target or source))
    if v.tier != "deny" and "--all" in flags:
        branches = ctx.git("for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        if len(branches) > 100:
            v.add("high", "too many branches to inspect for git push --all", "egress")
        else:
            for branch in branches:
                v.merge(check_outgoing_commits(ctx, remote, branch, branch))
    if v.tier != "deny" and "--tags" in flags:
        tags = ctx.git("for-each-ref", "--format=%(refname)", "refs/tags").splitlines()
        if len(tags) > 100:
            v.add("high", "too many tags to inspect for git push --tags", "egress")
        else:
            for tag in tags:
                v.merge(check_outgoing_commits(ctx, remote, tag, tag))
    if any("refs/tags/" in r or ctx.git("rev-parse", "--verify", f"refs/tags/{r}") for r in pos[1:]):
        v.add("high", "pushes an explicit tag ref", "egress")
        v.merge(check_tag_objects(ctx))
    return v


def check_tag_objects(ctx):
    v = Verdict()
    refs = ctx.git("for-each-ref", "--format=%(objectname) %(objecttype)", "refs/tags").splitlines()
    if len(refs) > 100:
        v.add("high", "too many tag objects to inspect", "egress")
        return v
    for ref in refs:
        oid, _, kind = ref.partition(" ")
        if kind != "tag":
            continue
        try:
            data = subprocess.run(["git", "-C", str(ctx.cwd), "cat-file", "-p", oid], capture_output=True, text=True,
                                  timeout=2, check=True).stdout
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            v.add("high", "cannot inspect an annotated tag", "egress")
            continue
        if leak := leaks(data):
            v.add("deny", f"pushes a tag that {leak}", "secret", "egress")
        try:
            target = subprocess.run(["git", "-C", str(ctx.cwd), "cat-file", "-p", f"{oid}^{{}}"], capture_output=True,
                                    timeout=2, check=True).stdout[:5_000_001]
            if len(target) > 5_000_000:
                v.add("high", "tag target is too large to inspect", "egress")
            elif leak := leaks(target.decode(errors="replace")):
                v.add("deny", f"pushes a tag target that {leak}", "secret", "egress")
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            v.add("high", "cannot inspect a tag target", "egress")
    return v


def check_outgoing_commits(ctx, remote, source, target):
    """Inspect reachable source objects without trusting mutable local remote-tracking refs."""
    v = Verdict()
    source = ctx.branch if source == "HEAD" else source
    revision = source
    try:
        count = len(ctx.git("rev-list", "--max-count=101", revision).splitlines())
        if count > 100:
            raise ValueError("too many commits")
        cmd = ["git", "-C", str(ctx.cwd)]
        objects = subprocess.run([*cmd, "rev-list", "--objects", revision], capture_output=True, text=True,
                                 timeout=3, check=True).stdout.splitlines()
        if len(objects) > 1000:
            raise ValueError("too many objects")
        for line in objects:
            if " " in line and SECRET_PATH_RE.search(line.split(" ", 1)[1]):
                v.add("deny", "pushes a credential file", "secret", "egress")
                return v
        ids = list(dict.fromkeys(line.split(" ", 1)[0] for line in objects))
        if not ids:
            return v
        infos = subprocess.run([*cmd, "cat-file", "--batch-check"], input="\n".join(ids) + "\n", capture_output=True,
                               text=True, timeout=3, check=True).stdout.splitlines()
        selected = [oid for oid, info in zip(ids, infos) if info.split()[1] in ("blob", "commit")]
        sizes = [int(info.split()[2]) for info in infos if info.split()[1] in ("blob", "commit")]
        if len(infos) != len(ids) or sum(sizes) > 5_000_000:
            raise ValueError("too much object data")
        raw = subprocess.run([*cmd, "cat-file", "--batch"], input=("\n".join(selected) + "\n").encode(),
                             capture_output=True, timeout=3, check=True).stdout if selected else b""
        offset = 0
        for oid, size in zip(selected, sizes):
            end = raw.index(b"\n", offset)
            header = raw[offset:end].decode()
            if not header.startswith(oid + " "):
                raise ValueError("unexpected git object")
            data = raw[end + 1:end + 1 + size]
            if len(data) != size:
                raise ValueError("incomplete git object")
            if leak := leaks(data.decode(errors="replace")):
                v.add("deny", f"pushes a commit that {leak}", "secret", "egress")
                return v
            offset = end + 1 + size + 1
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
        v.add("high", "cannot fully inspect the commits this push would send", "egress")
        return v
    return v


FORGE_READ = {"view", "list", "status", "diff", "checks", "search", "ls", "browse", "watch", "download", "show", "ci-status"}


def opt(args, *names):
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


def check_forge(cmd, args, ctx):
    v = Verdict()
    a = [x for x in args]
    if not a:
        return v
    group, verb = a[0], (a[1] if len(a) > 1 else "")
    if (ctx.source_root and ctx.root != ctx.source_root or ctx.unknown) and verb not in FORGE_READ:
        v.add("high", "forge command runs after a workspace change", "egress")
    origin_host = host_of(ctx.git("remote", "get-url", "origin"))
    host = os.environ.get("GH_HOST" if cmd == "gh" else "GITLAB_HOST", "")
    if host and host_of(host) != origin_host and verb not in FORGE_READ:
        v.add("high", f"{cmd} host differs from this workspace's origin", "egress")
    repo_env = os.environ.get("GH_REPO" if cmd == "gh" else "GLAB_REPO", "")
    if repo_env and not own_repo(repo_env, ctx) and verb not in FORGE_READ:
        v.add("high", f"{cmd} repository differs from this workspace's repo", "egress")
    selected_host = opt(a, "--hostname")
    if selected_host and host_of(selected_host) != origin_host and verb not in FORGE_READ:
        v.add("high", f"{cmd} --hostname differs from this workspace's origin", "egress")
    text = " ".join(a)
    body = " ".join(filter(None, [opt(a, "--body", "-b", "--message", "-m", "--description", "-d", "--title", "-t", "--notes")]))
    bfile = opt(a, "--body-file", "--notes-file", *(["-F"] if group in ("pr", "mr", "issue") else []),
                *(["-T", "--template"] if group in ("pr", "mr") and verb in ("create", "edit", "update") else []))
    bfile = bfile or next((x[2:] for x in a if (x.startswith("-F") and len(x) > 2 and group in ("pr", "mr", "issue")) or
                         (x.startswith("-T") and len(x) > 2 and group in ("pr", "mr"))), None)
    for source in filter(None, (bfile, opt(a, "--input"))):
        v.merge(check_upload_file(source, ctx, f"{cmd} upload"))
    for i, field in enumerate(a):
        value = (a[i + 1] if i + 1 < len(a) else "") if field in ("-F", "--field") else (
            field.split("=", 1)[1] if field.startswith("--field=") else field[2:] if field.startswith("-F") else "")
        if "@" in value:
            v.merge(check_upload_file(value.split("@", 1)[1], ctx, f"{cmd} field upload"))
    if leak := leaks(body) or leaks(text):
        v.add("deny", f"the text it posts {leak}", "secret", "egress")
    if secret_vars(text):
        v.add("deny", "posts a secret env var", "secret", "egress")
    other = opt(a, "--repo", "-R") or next((x[2:] for x in a if x.startswith("-R") and len(x) > 2), None)
    if other and not own_repo(other, ctx):
        if verb not in FORGE_READ:
            v.add("high", f"writes to {other}, not this workspace's repo", "egress")
    if verb not in FORGE_READ:
        for value in a[2:]:
            if value.startswith(("https://", "http://")):
                parsed = urlsplit(value)
                parts = parsed.path.strip("/").split("/")
                if parsed.hostname != host_of(ctx.git("remote", "get-url", "origin")) or len(parts) < 2 or not own_repo("/".join(parts[:2]), ctx):
                    v.add("high", f"{cmd} target URL is outside this workspace's repo", "egress")
    if group == "auth":
        if verb == "token" or "--show-token" in a or "-t" in a:
            v.add("high", f"{cmd} auth prints the token", "secret")
        elif verb not in ("status",):
            v.add("high", f"{cmd} auth {verb} changes who the agent is")
        return v
    if group == "api":
        method = (opt(a, "-X", "--method") or next((x[2:] for x in a if x.startswith("-X") and len(x) > 2), "")).upper()
        writes = method not in ("", "GET", "HEAD") or any(x in a or any(y.startswith(x + "=") for y in a) for x in ("-f", "-F", "--field", "--raw-field", "--input")) or any(x.startswith(("-f", "-F")) and len(x) > 2 for x in a)
        if writes:
            v.add("high" if method == "DELETE" else "medium", f"{cmd} api {method or 'POST'} writes to the forge", "egress")
            target = re.search(r"\brepos/([^/\s]+)/([^/\s?]+)", text)
            if target and not own_repo(f"{target[1]}/{target[2]}", ctx):
                v.add("high", f"{cmd} api writes to another repo ({target[1]}/{target[2]})", "egress")
            elif not target:
                v.add("high", f"{cmd} api write target is not proven to be this workspace's repo", "egress")
            if cmd == "glab" and re.search(r"\bprojects/[^/\s]+", text):
                v.add("high", "glab api project target cannot be verified against this workspace", "egress")
        return v
    if group in ("pr", "mr") and verb in ("edit", "ready", "update", "merge", "close", "reopen", "approve", "revoke", "rebase", "delete", "lock", "unlock", "comment", "note", "review", "reply"):
        goal = situation(ctx)[1]
        expected = re.search(r"[#!](\d+)", goal)
        target = a[2] if len(a) > 2 and not a[2].startswith("-") else ""
        if not expected or target != expected[1]:
            v.add("high", f"{cmd} {group} target is not verified as this workspace's PR/MR", "egress")
    if group in ("pr", "mr") and verb == "create" and any(x in a for x in ("--fill", "--fill-first", "--fill-verbose")):
        default = next((d for d in ctx.default if ctx.git("rev-parse", "--verify", f"refs/remotes/origin/{d}")), "main")
        source = opt(a, "--head") or ctx.branch
        if ":" in source or not ctx.git("rev-parse", "--verify", source):
            v.add("high", "cannot inspect the head branch used to fill the PR", "egress")
        else:
            v.merge(check_outgoing_commits(ctx, "origin", source, default))
    if group == "gist" and verb == "create" or group == "release" and verb in ("upload", "create"):
        for source in a[3:] if group == "release" and verb == "create" else a[2:]:
            if not source.startswith("-") and ("/" in source or Path(source).exists() or SECRET_PATH_RE.search(source)):
                v.merge(check_upload_file(source, ctx, f"{cmd} {group} upload"))
    if verb == "download":
        directory = opt(a, "--dir", "-D") or "."
        pattern = opt(a, "--pattern")
        if pattern and not any(c in pattern for c in "*?["):
            target = os.path.join(directory, pattern)
            v.merge(check_write(ctx.path(target), ctx, f"{cmd} download", raw=target))
        else:
            v.merge(check_write(ctx.path(directory), ctx, f"{cmd} download", raw=directory))
    if verb in FORGE_READ or group in ("help", "version", "--version", "completion"):
        return v
    if group in ("secret", "variable", "ssh-key", "gpg-key", "extension", "alias", "config") or group == "repo" and verb in (
            "delete", "archive", "rename", "edit", "create", "fork", "set-default", "unarchive", "deploy-key") or group == "gist" or group == "release":
        v.add("high", f"{cmd} {group} {verb} changes the repo or account, or publishes", "egress")
        return v
    low = {("pr", "create"), ("pr", "edit"), ("pr", "ready"), ("mr", "create"), ("mr", "update"), ("pr", "checkout"), ("mr", "checkout")}
    if (group, verb) in low:
        if verb in ("edit", "update") and any(x in a or any(y.startswith(x + "=") for y in a) for x in ("--body", "-b", "--body-file", "--base", "--title")):
            v.add("medium", f"{cmd} {group} {verb} changes published PR text or base", "egress")
        v.add("low", f"{cmd} {group} {verb} on this workspace's PR/MR", "egress")
    elif group in ("pr", "mr", "issue") and verb in ("comment", "note", "review", "reply", "create", "edit", "update"):
        v.add("medium", f"{cmd} {group} {verb} posts text to the forge", "egress")
    elif group in ("pr", "mr") and verb in ("merge", "close", "reopen", "approve", "revoke", "rebase", "delete", "lock", "unlock"):
        v.add("medium", f"{cmd} {group} {verb}", "egress")
    elif group in ("issue", "label", "run", "workflow", "ci", "milestone", "project", "cache", "schedule", "variable", "job", "pipeline"):
        v.add("medium", f"{cmd} {group} {verb} changes the forge", "egress")
    else:
        v.add("medium", f"{cmd} {group} {verb}", "egress")
    return v


def own_repo(repo, ctx):
    url = ctx.git("remote", "get-url", "origin")
    name, other = repo_slug(url), repo_slug(repo)
    return bool(name) and other == name and ("/" not in repo or "://" not in repo and "@" not in repo or host_of(url) == host_of(repo))


def repo_slug(repo):
    return re.sub(r"\.git$", "", re.sub(r"^(?:https?://|git@)[^/:]+[/:]", "", repo.lower())).rstrip("/")


def workspace_project(ctx):
    try:
        rows = json.loads((STATE / "last.json").read_text()).get("rows", [])
    except (OSError, ValueError):
        return ""
    return next((r.get("project", "") for r in rows if r.get("path") and ctx.root and
                 os.path.realpath(r["path"]) == str(ctx.root)), "")


def check_net(cmd, args, ctx):
    v = Verdict()
    if cmd == "curl" and args[:1] not in (["-q"], ["--disable"]):
        configs = [Path(os.environ.get("CURL_HOME") or HOME) / ".curlrc",
                   Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "curlrc", HOME / ".curlrc"]
        try:
            if any(p.is_file() and p.stat().st_size for p in configs):
                v.add("high", "curl loads a default config the guard cannot safely infer", "egress")
        except OSError:
            v.add("high", "cannot inspect curl's default config", "egress")
    urls = [a for a in args if re.match(r"(?i)[a-z][a-z0-9+.-]*://", a) or (not a.startswith("-") and re.match(r"[\w.-]+\.[a-z]{2,}(?:[:/]|$)", a))]
    hosts = {host_of(u) for u in urls}
    for url in urls:
        userinfo = re.match(r"(?i)[a-z][a-z0-9+.-]*://([^/@]+)@", url)
        if userinfo and ":" in userinfo[1]:
            v.add("deny", "URL contains a username and password", "secret", "egress")
    routed = any(a in ("--connect-to", "--proxy", "-x", "--resolve") or a.startswith(("--connect-to=", "--proxy=", "--resolve=", "-x"))
                 for a in args)
    if routed:
        v.add("high", f"{cmd} can route through a different network destination", "egress")
    if "--expand-url" in args:
        v.add("high", f"{cmd} expands a URL after the guard inspects it", "egress")
    if cmd == "curl" and any(a in ("--unix-socket", "--abstract-unix-socket") or a.startswith(("--unix-socket=", "--abstract-unix-socket=")) for a in args):
        v.add("high", "curl uses a local service socket with possible side effects", "egress")
    if cmd == "curl" and ("--location" in args or "--location-trusted" in args or
                           any(re.fullmatch(r"-[A-Za-z]*L[A-Za-z]*", a) for a in args)):
        v.add("high", "curl may follow a redirect to another host", "egress")
    if "--location-trusted" in args and any(re.search(r"(?i)authorization|x-api-key|private-token|job-token", a) for a in args):
        v.add("deny", f"{cmd} can forward authentication to a redirected host", "secret", "egress")
    user = opt(args, "-u", "--user") or next((a[2:] for a in args if a.startswith("-u") and len(a) > 2), None)
    if user:
        v.add("deny" if ":" in user else "high", f"{cmd} sends credentials from --user", "secret", "egress")
    cookie = opt(args, "-b", "--cookie") or next((a[2:] for a in args if a.startswith("-b") and len(a) > 2), None)
    if cookie:
        if "=" in cookie:
            v.add("high", f"{cmd} sends a literal cookie", "secret", "egress")
        else:
            v.merge(check_upload_file(cookie, ctx, f"{cmd} cookie"))
    for i, arg in enumerate(args):
        value = args[i + 1] if arg == "--variable" and i + 1 < len(args) else arg.split("=", 1)[1] if arg.startswith("--variable=") else ""
        if "@" in value:
            v.merge(check_upload_file(value.split("@", 1)[1], ctx, f"{cmd} variable"))
        value = args[i + 1] if arg == "--url-query" and i + 1 < len(args) else arg.split("=", 1)[1] if arg.startswith("--url-query=") else ""
        if "@" in value:
            v.merge(check_upload_file(value.split("@", 1)[1], ctx, f"{cmd} URL query"))
    output = opt(args, "-o", "-O" if cmd == "wget" else "-o", "--output", "--output-document")
    output = output or next((a[2:] for a in args if a.startswith(("-o", "-O" if cmd == "wget" else "-o")) and len(a) > 2), None)
    if cmd == "curl" and not output:
        output = next((args[i + 1] for i, a in enumerate(args) if re.fullmatch(r"-[A-Za-z]*o", a) and i + 1 < len(args)), None)
    if output:
        if cmd == "curl" and (directory := opt(args, "--output-dir")):
            output = os.path.join(directory, output)
        v.merge(check_write(ctx.path(output), ctx, f"{cmd} output", raw=output))
    if cmd == "curl":
        for flag in ("-D", "--dump-header", "-c", "--cookie-jar"):
            target = opt(args, flag) or next((a[len(flag):] for a in args if len(flag) == 2 and a.startswith(flag) and len(a) > 2), None)
            if target:
                v.merge(check_write(ctx.path(target), ctx, f"curl {flag} output", raw=target))
    if cmd == "wget" and not output:
        directory = opt(args, "-P", "--directory-prefix") or "."
        for url in urls:
            name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "index.html"
            target = os.path.join(directory, name)
            v.merge(check_write(ctx.path(target), ctx, "wget output", raw=target))
    if cmd == "curl" and ("--remote-name" in args or any(re.fullmatch(r"-[A-Za-z]*O[A-Za-z]*", a) for a in args)):
        directory = opt(args, "--output-dir") or "."
        for url in urls:
            name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if name:
                target = os.path.join(directory, name)
                v.merge(check_write(ctx.path(target), ctx, "curl remote-name output", raw=target))
    if cmd == "curl" and any(a in args for a in ("--netrc", "-n", "--netrc-optional")) or opt(args, "--netrc-file"):
        v.add("deny", f"{cmd} may send credentials from a netrc file", "secret", "egress")
    config_file = opt(args, "-K", "--config") or next((a[2:] for a in args if a.startswith("-K") and len(a) > 2), None)
    if config_file:
        v.add("high", f"{cmd} loads an external request config ({config_file})", "egress")
        try:
            with ctx.path(config_file).open("r", errors="replace") as fh:
                config_text = fh.read(1_000_001)
        except OSError:
            config_text = ""
        if len(config_text) > 1_000_000:
            v.add("deny", f"{cmd} request config is too large to inspect", "egress")
        if leak := leaks(config_text):
            v.add("deny", f"{cmd} request config {leak}", "secret", "egress")
        for source in re.findall(r"@([^\s\"']+)", config_text):
            v.merge(check_upload_file(source, ctx, f"{cmd} config upload"))
    if cmd == "wget":
        input_file = opt(args, "-i", "--input-file") or next((a[2:] for a in args if a.startswith("-i") and len(a) > 2), None)
        if input_file:
            v.merge(check_upload_file(input_file, ctx, "wget URL list"))
            v.add("high", "wget reads network destinations from a file", "egress")
        for flag in ("--post-file", "--body-file"):
            source = opt(args, flag)
            if source:
                v.merge(check_upload_file(source, ctx, f"{cmd} upload"))
    if hosts and hosts <= LOCAL_HOSTS and not routed:
        local_method = (opt(args, "-X", "--request", "--method") or "").upper()
        if local_method in ("POST", "PUT", "PATCH", "DELETE") or any(re.match(r"--data|-d|-[FT](?:$|.)|--form|--json|--upload-file|--post-", a) for a in args):
            v.add("high", f"{cmd} writes to a local service", "egress")
        return v
    data = [a for a in args if re.match(r"--data|-d|-[FT](?:$|.)|--form|--json|--upload-file|--post-|--body-|--method", a)]
    if cmd in ("http", "https", "xh", "httpie"):
        data += [a for a in args if re.match(r"^[A-Za-z_][\w.-]*[:=@].+", a)]
    method = (opt(args, "-X", "--request", "--method") or next((a[2:] for a in args if a.startswith("-X") and len(a) > 2), "")).upper()
    if cmd in ("http", "https", "xh", "httpie"):
        method = next((a.upper() for a in args if a.upper() in ("POST", "PUT", "PATCH", "DELETE")), method)
    full = " ".join(args)
    for i, a in enumerate(args):
        source = None
        if a.startswith("-H@") or a.startswith("--header=@"):
            source = a.split("@", 1)[1]
            v.merge(check_upload_file(source, ctx, f"{cmd} header"))
        if a in ("-F", "--form") and i + 1 < len(args):
            form = args[i + 1]
            if "=<" in form:
                v.merge(check_upload_file(form.split("=<", 1)[1].split(";", 1)[0], ctx, f"{cmd} form upload"))
        elif a.startswith(("-F", "--form=")) and "=<" in a:
            v.merge(check_upload_file(a.split("=<", 1)[1].split(";", 1)[0], ctx, f"{cmd} form upload"))
        if a in ("-d", "-F", "--data", "--data-binary", "--data-urlencode", "--form", "--json", "-T", "--upload-file") and i + 1 < len(args):
            source = args[i + 1]
        elif a.startswith(("--data=", "--data-binary=", "--data-urlencode=", "--form=", "--json=", "--upload-file=")):
            source = a.split("=", 1)[1]
        elif a.startswith(("-d@", "-F", "-T")) and len(a) > 2:
            source = a[2:]
        elif a.startswith("@"):
            source = a
        if source and ("@" in source or a in ("-T", "--upload-file") or a.startswith(("-T", "--upload-file="))):
            path = source.split("@", 1)[-1].split(";")[0] if "@" in source else source
            v.merge(check_upload_file(path, ctx, f"{cmd} upload"))
    if leak := leaks(full):
        v.add("deny", f"the request {leak}", "secret", "egress")
    auth_header = any(re.search(r"(?i)authorization|x-api-key|private-token|job-token", a) for a in args)
    header_vars = {n for a in args if re.search(r"(?i)authorization|x-api-key|private-token|job-token", a) for n in secret_vars(a)}
    if header_vars and any(urlsplit(url).scheme.lower() != "https" for url in urls):
        v.add("deny", "sends an authentication secret over a non-HTTPS URL", "secret", "egress")
    auth_hosts = {"github.com", "api.github.com", "gitlab.com", "api.gitlab.com"}
    allowed_header = {n for n in header_vars if
                      (hosts <= {"github.com", "api.github.com"} and n.upper().startswith(("GITHUB_", "GH_"))) or
                      (hosts <= {"gitlab.com", "api.gitlab.com"} and n.upper().startswith(("GITLAB_", "GLAB_", "CI_JOB_TOKEN")))}
    if secret_vars(full) and (set(secret_vars(full)) - allowed_header or not (auth_header and hosts and hosts <= auth_hosts)):
        v.add("deny", "sends a secret env var off the machine", "secret", "egress")
    if data or method in ("POST", "PUT", "PATCH", "DELETE"):
        v.add("high", f"{cmd} sends data to {', '.join(sorted(hosts)) or 'a host'}", "egress")
    elif hosts and hosts <= HOSTS | extra_hosts():
        long = any(len(u.split("?", 1)[1]) > 200 for u in urls if "?" in u)
        v.add("high" if long else "low", f"{cmd} GET {', '.join(sorted(hosts))}" + (" with a long query" if long else ""), "egress")
    else:
        v.add("medium", f"{cmd} reaches {', '.join(sorted(hosts)) or 'the network'}", "egress")
    return v


def extra_hosts():
    return set((config() or {}).get("hosts") or [])


def check_rm(args, ctx):
    v = Verdict()
    paths = [a for a in args if not a.startswith("-") or a == "-"]
    if "--" in args:
        paths = args[args.index("--") + 1:]
    for raw in paths:
        p = ctx.path(raw, follow=False)
        s = str(p)
        if "$(" in raw or "`" in raw:
            v.add("high", f"deletes a path from command substitution ({raw})")
            continue
        if "{" in raw and "}" in raw:
            v.add("high", f"deletes a path with brace expansion ({raw})")
            continue
        if re.search(r"\$\{[^}]*[:-][^}]*\}", raw):
            v.add("high", f"deletes a path with shell parameter expansion ({raw})")
            continue
        if s in temp_dirs():
            v.add("deny", f"deletes an entire temp root ({raw}) that can contain other workspaces")
            continue
        if any(c in raw for c in "*?["):
            anchor = raw.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].rstrip("/") or "."
            if fnmatch.fnmatch(".git", os.path.basename(raw)) or "/.git" in raw:
                v.add("high", f"deletes a glob that may match git data ({raw})")
            if not (ctx.root and ctx.path(anchor) == ctx.root or ctx.root and str(ctx.path(anchor)).startswith(str(ctx.root) + "/")):
                v.add("high", f"deletes a glob outside the worktree ({raw})")
        if ctx.unknown and not os.path.isabs(os.path.expanduser(raw)):
            v.add("high", f"deletes {raw} after an unknown directory change")
            continue
        if s in ("/", str(HOME), "/home", "/Users", "/root") or str(HOME).startswith(s + "/") or raw in ("~", "~/", "/*", "~/*"):
            v.add("deny", f"deletes {raw}: the home folder or above")
        elif re.search(r"\$\{?(?!HOME\b|PWD\b|TMPDIR\b)\w", raw):
            v.add("high", f"deletes {raw}: a path from a variable that may be empty")
        elif "$" in raw and "$" in s:
            v.add("high", f"deletes {raw}: an unresolved variable")
        elif ctx.root and (str(ctx.root).startswith(s + "/") or s == str(ctx.root)):
            v.add("high", f"deletes the worktree itself, or a folder above it ({raw})")
        elif GUARD_PATH_RE.search(s):
            v.add("deny", f"deletes the guard or its config ({raw})")
        elif s in (str(HOME / ".greenlight"), str(HOME / ".claude"), str(HOME / ".claude" / "skills" / "greenlight")):
            v.add("deny", f"deletes the guard or agent's settings directory ({raw})")
        elif s.endswith("/.git") or "/.git/" in s:
            v.add("high", f"deletes git's own data ({raw})")
        elif not ctx.inside(p):
            v.add("high", f"deletes {raw}, outside the worktree")
        elif p.is_dir() and not (ctx.root and str(p).startswith(str(ctx.root) + "/")):
            for count, (base, dirs, files) in enumerate(os.walk(p)):
                if ".git" in dirs or ".git" in files or count >= 1000:
                    v.add("high", f"deletes a temp directory that may contain another worktree ({raw})")
                    break
    return v


def check_write(p, ctx, how="write", raw=None):
    v = Verdict()
    s = str(p)
    if s in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or s.startswith("/dev/fd/"):
        return v
    if raw and re.search(r"\$\{[^}]*[:-][^}]*\}", raw):
        v.add("high", f"{how} uses shell parameter expansion ({raw})")
    if raw and "$" in raw and "$" in s:
        v.add("high", f"{how} uses an unresolved variable ({raw})")
    if ctx.unknown and raw and not os.path.isabs(os.path.expanduser(raw)):
        v.add("medium", f"{how} to {raw} after an unknown directory change")
    if GUARD_PATH_RE.search(s):
        v.add("deny", f"{how} to {s}: that would change the guard or the agent's own permissions")
    elif ESCAPE_PATH_RE.search(s):
        v.add("high", f"{how} to {s}: runs code outside the agent's turn")
    elif SECRET_PATH_RE.search(s):
        v.add("high", f"{how} to a credential file ({s})", "secret")
    elif not ctx.inside(p) and not re.search(r"/\.claude/projects/[^/]+/memory/", s):
        v.add("high", f"{how} to {s}, outside the worktree")
    return v


def check_tool(tool, inp, ctx):
    """The Verdict for one Claude Code tool call."""
    v = Verdict()
    inp = inp or {}
    if tool == "Bash":
        return check_shell(inp.get("command", ""), ctx)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = ctx.path(inp.get("file_path") or inp.get("notebook_path") or "")
        v.merge(check_write(p, ctx, tool))
        text = inp.get("content") or inp.get("new_string") or inp.get("new_source") or "".join(
            e.get("new_string", "") for e in inp.get("edits") or [])
        if NET_CODE_RE.search(text) and (SECRET_PATH_RE.search(text) or secret_vars(text) or leaks(text)):
            v.add("high", "writes code that reads secrets and reaches the network", "secret")
        return v
    if tool in ("Read", "Grep", "Glob", "LS"):
        p = inp.get("file_path") or inp.get("path") or ""
        if p and SECRET_PATH_RE.search(str(ctx.path(p))):
            v.add("high", f"reads a credential file ({p})", "secret")
        return v
    if tool == "WebFetch":
        url = inp.get("url", "")
        host = host_of(url)
        if leak := leaks(url):
            v.add("deny", f"the URL {leak}", "secret", "egress")
        elif "?" in url and len(url.split("?", 1)[1]) > 200:
            v.add("high", f"fetches {host} with a long query (data can ride in it)", "egress")
        elif host in HOSTS | extra_hosts() | LOCAL_HOSTS or host.endswith((".github.io", ".python.org", ".readthedocs.io")):
            v.add("low", f"fetches {host}", "egress")
        else:
            v.add("medium", f"fetches {host}", "egress")
        return v
    if tool == "WebSearch":
        if leak := leaks(json.dumps(inp)):
            v.add("deny", f"the search query {leak}", "secret", "egress")
        else:
            v.add("medium", "searches the web with the supplied query", "egress")
        return v
    if tool.startswith("mcp__"):
        name = tool.split("__")[-1].lower()
        if re.search(r"send|post|create|update|delete|comment|reply|forward|share|upload|write|publish|merge|push|trash|move|invite|cancel|invoke|execute|call|run", name):
            v.add("high" if re.search(r"execute|invoke|call|run", name) else "medium",
                  f"{tool} may change something outside this machine", "egress")
        if leak := leaks(json.dumps(inp)):
            v.add("deny", f"what it sends {leak}", "secret", "egress")
        return v
    return v


# ---------------------------------------------------------------- the goal, the rules, and System 1

def situation(ctx):
    """(workspace name, goal, repo rules) for the worktree the call runs in, from the last scan."""
    goal, name, rules = os.environ.get("GREENLIGHT_GOAL", ""), "", ""
    try:
        rows = json.loads((STATE / "last.json").read_text()).get("rows", [])
    except (OSError, ValueError):
        rows = []
    root = str(ctx.root)
    row = next((r for r in rows if r.get("path") and (root == r["path"] or root.startswith(r["path"].rstrip("/") + "/"))), None)
    if row:
        name, goal = row.get("name", ""), goal or row.get("goal") or row.get("comment") or ""
        repo = (row.get("project") or "").rsplit("/", 1)[-1]
        for f in (STATE / "lifecycles" / f"{repo}.md", HERE.parent / "lifecycles" / f"{repo}.md"):
            if repo and f.exists():
                rules = f.read_text().partition("\n## Rules")[2].strip()
                break
    return name, goal, rules


def local_bool(state, question, *, context=""):
    return {"value": False, "prob": 0.5, "provider": "local"}


def system_one(question, context, budget_ms):
    """decide.bool within budget_ms: (result, ms). A miss, an error or no decide.py is a low-confidence local answer."""
    t0, box = time.monotonic(), {}

    def go():
        try:
            import decide  # scripts/decide.py (system-one-decide); optional
            box["r"] = decide.bool(STATE, question, context=context)
        except Exception as e:  # noqa: BLE001 - any backend failure falls back
            box["r"] = local_bool(STATE, question, context=context) | {"error": f"{type(e).__name__}: {e}"[:200]}

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(budget_ms / 1000)
    ms = int((time.monotonic() - t0) * 1000)
    r = box.get("r") or {"value": False, "prob": 0.5, "provider": "timeout"}
    try:
        r = {"value": bool(r.get("value")), "prob": float(r.get("prob", 0.5)), "provider": str(r.get("provider") or "local"), **(
            {"error": r["error"]} if r.get("error") else {})}
        if not math.isfinite(r["prob"]) or not 0 <= r["prob"] <= 1 or r["value"] != (r["prob"] > 0.5):
            raise ValueError("inconsistent probability")
    except (AttributeError, TypeError, ValueError):
        r = {"value": False, "prob": 0.5, "provider": "local", "error": "bad answer"}
    return r, ms


def judge(v, action, ctx, conf):
    """The final call for a boundary action: allow | pause | deny, with the System 1 answer it rests on."""
    for pat in conf.get("deny") or []:
        if matches(pat, action):
            return "deny", f"matches your guard.deny rule /{pat}/", None, 0
    if v.tier == "deny":
        return "deny", "; ".join(v.why), None, 0
    for pat in conf.get("allow") or []:
        if matches(pat, action) and v.tier != "deny":
            return "allow", f"matches your guard.allow rule /{pat}/", None, 0
    name, goal, rules = situation(ctx)
    safe_reasons = [redact(reason) for reason in v.why]
    context = (f"Workspace: {name or (ctx.root.name if ctx.root else '(outside a repo)')}\nBranch: {ctx.branch}\nGoal: {goal or '(none known)'}\n"
               f"Repo rules:\n{rules or '(none)'}\nSecurity invariants:\n{INVARIANTS}\n"
               f"Risk the rules saw ({v.tier}): {'; '.join(safe_reasons)}\nAction:\n{action[:4000]}")
    q = "Is this action needed for the workspace's goal, and does it keep every security invariant?"
    s1, ms = system_one(q, context, int(conf.get("budget_ms", 200)))
    real = s1["provider"] not in ("local", "timeout") and "error" not in s1
    p = s1["prob"]
    if v.tier == "low":
        verdict = "pause" if real and p < 0.2 else "allow"
    elif v.tier == "medium":
        verdict = "allow" if real and p >= 0.9 else "pause"
    else:
        verdict = "pause"
    why = "; ".join(safe_reasons) + (f" (System 1 {s1['provider']}: {p:.2f} aligned)" if real else f" (System 1 unavailable: {s1['provider']})")
    return verdict, why, s1, ms


# ---------------------------------------------------------------- pausing for the user

def mode(conf):
    """How a pause reaches the user: ask (Claude Code's prompt), ping (the menubar app; the agent waits) or chat (the
    menubar chat's own turn: the app holds the user's answer until the turn ends, so the turn can't wait on a ping)."""
    env = os.environ.get("GREENLIGHT_GUARD_MODE", "")
    if env in ("ask", "ping", "chat"):
        return env
    if conf.get("mode") in ("ask", "ping", "chat"):
        return conf["mode"]
    return "ping" if os.environ.get("GREENLIGHT_UNATTENDED") == "1" else "ask"


ANSWER, ASKED = "\n(answer) ", "\n(asked) "  # chat.py's GREENLIGHT_REQUEST: the request, the chat's question, the user's answer
ALLOW_WORDS = {"allow", "yes", "y", "ok", "okay", "approve", "approved", "go ahead", "allow it"}


def consented(token):
    """In a chat turn: the user just answered "allow" to the chat's question that carried [guard:token]."""
    req = os.environ.get("GREENLIGHT_REQUEST", "")
    if ANSWER not in req:
        return False
    head, _, answer = req.rpartition(ANSWER)
    question = head.rpartition(ASKED)[2] if ASKED in head else ""
    return f"[guard:{token}]" in question and answer.strip().strip(".!").lower() in ALLOW_WORDS


def approvals(update=None):
    f = STATE / "guard" / "approved.json"
    if update is None:
        try:
            return {k: t for k, t in json.loads(f.read_text()).items() if t > time.time() - 3600}
        except (OSError, ValueError):
            return {}
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(update))
    os.replace(tmp, f)


def wait_for_user(summary, why, ctx, conf):
    """Ping the menubar app and wait for allow | deny. Unanswered by the timeout: deny."""
    import decisions  # stdlib, never imports greenlight.py
    timeout = min(int(conf.get("timeout", 240)), 240)
    name, goal, _ = situation(ctx)
    key = f"@guard-{int(time.time() * 1000):x}-{os.getpid():x}"
    ws = name or (ctx.root.name if ctx.root else ctx.cwd.name)
    text = (f"{ws}: an unattended agent wants to run\n{summary[:600]}\nWhy it asks: {why[:400]}\n"
            f"Goal: {goal[:200] or '(none known)'}\nDenied in {timeout // 60 or 1} min if unanswered. [{key}]\nOptions: allow | deny")
    try:
        import greenlight
        greenlight.ping(redact(text), key=key, ws=[name] if name else [], kind="guard")
    except Exception as e:  # noqa: BLE001 - no app, no ping: deny now
        return "deny", f"couldn't ask you ({type(e).__name__}: {e}); denied"
    end = time.time() + timeout
    while time.time() < end:
        for r in decisions.load():
            if r.get("key") != key:
                continue
            if r.get("answer") is not None:
                ans = r["answer"].strip().lower()
                return ("allow", "you allowed it") if ans in ("allow", "yes", "y", "ok", "approve") else ("deny", f"you said: {r['answer'][:200]}")
            if r.get("closed"):
                return "deny", f"the question closed ({r['closed']})"
        time.sleep(1)
    try:
        greenlight.resolve(key, "guard timed out: denied")
    except Exception:  # noqa: BLE001
        pass
    return "deny", f"no answer in {timeout}s; denied"


def log(ev):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "guard.jsonl").open("a") as fh:
            fh.write(json.dumps(scrub({"ts": int(time.time())} | ev)) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- entry points

def describe(tool, inp):
    if tool == "Bash":
        return inp.get("command", "")
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Read"):
        return f"{tool} {inp.get('file_path') or inp.get('notebook_path', '')}"
    if tool == "WebFetch":
        return f"WebFetch {inp.get('url', '')}"
    return f"{tool} {json.dumps(inp)[:1000]}"


def approval_files(tool, inp, ctx):
    """Hash readable file arguments so an approval cannot carry over to new payload bytes."""
    if tool != "Bash":
        return ""
    try:
        tokens = shlex.split(inp.get("command", ""))
    except ValueError:
        return "unparseable"
    found = []
    for token in tokens:
        candidate = token.split("@", 1)[1] if "@" in token and not token.startswith(("http:", "https:")) else token
        candidate = candidate.split("=", 1)[-1] if "=" in candidate else candidate
        candidate = candidate.split(";", 1)[0]
        if not candidate or candidate.startswith("-"):
            continue
        p = ctx.path(candidate)
        try:
            if p.is_file():
                with p.open("rb") as fh:
                    data = fh.read(5_000_001)
                found.append((str(p), hashlib.sha256(data).hexdigest() if len(data) <= 5_000_000 else "too-large"))
        except OSError:
            found.append((str(p), "unreadable"))
    return json.dumps(found, sort_keys=True)


def decide_call(tool, inp, cwd, session="", conf=None, interactive_ok=True, record=True, dry_run=False):
    """{decision: pass|allow|ask|deny, reason, tier, ...}. pass: no opinion."""
    t0 = time.monotonic()
    conf = conf if conf is not None else (config() or {})
    ctx = Ctx(cwd)
    action = redact(describe(tool, inp))
    try:
        v = check_tool(tool, inp, ctx)
    except Exception as e:  # noqa: BLE001 - a crash on a call that names a boundary tool can't wave it through
        v = Verdict()
        v.add("high" if re.search(r"\b(?:push|curl|wget|gh|glab|rm|ssh|scp|nc)\b|https?://", action) else "pass",
              f"guard error {type(e).__name__}: {e}")
    if v.tier == "pass" and not any(matches(p, action) for p in (conf.get("deny") or [])):
        return {"decision": "pass", "tier": "pass", "ms": int((time.monotonic() - t0) * 1000)}
    verdict, why, s1, s1_ms = judge(v, action, ctx, conf)
    why = redact(why)
    ev = {"tool": tool, "action": action[:500], "cwd": str(ctx.cwd), "tier": v.tier, "rules": [redact(reason) for reason in v.why], "s1": s1, "s1_ms": s1_ms,
          "session": session}
    routing_env = {k: os.environ.get(k, "") for k in ("GH_REPO", "GH_HOST", "GLAB_REPO", "GITLAB_HOST", "GIT_SSH_COMMAND")}
    routing_env.update({name: os.environ.get(name, "") for name in SECRET_VAR_RE.findall(inp.get("command", ""))} if tool == "Bash" else {})
    routing_env.update({k: value for k, value in os.environ.items() if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))})
    scope = f"{ctx.cwd}\0{ctx.root}\0{ctx.branch}\0{ctx.git('remote', 'get-url', 'origin')}\0{json.dumps(routing_env, sort_keys=True)}\0{v.tier}\0{approval_files(tool, inp, ctx)}"
    cacheable = tool != "Bash" or not re.search(r"\$|`|@[^\s]+|--(?:body-file|notes-file|input|post-file|upload-file|template)\b|\s-[FTK][^\s]*|\bgh\s+(?:gist|release)\s+(?:create|upload)\b", inp.get("command", ""))
    digest = hashlib.sha256(f"{session}\0{tool}\0{scope}\0{json.dumps(inp, sort_keys=True)}".encode()).hexdigest()[:24]
    if verdict == "pause" and cacheable and digest in approvals():
        verdict, why = "allow", "you allowed this exact call in this session within the hour"
    if verdict == "pause" and dry_run:
        verdict, why = "ask", f"{why}; dry run would ask for approval"
    if verdict == "pause":
        how = "ping" if not interactive_ok and mode(conf) == "ask" else mode(conf)
        token = hashlib.sha256(f"{tool}\0{scope}\0{json.dumps(inp, sort_keys=True)}".encode()).hexdigest()[:10]
        if how == "chat":
            if consented(token):
                verdict, why = "allow", f"{why}; you allowed it in the chat"
            else:
                verdict = "deny"
                why = (f"{why}. Paused for the user: ask them in this chat what you want to run and why, with [guard:{token}] "
                       f"in the question and the line `Options: allow | deny` last; if they answer allow, run the exact same call again")
        elif how == "ping":
            verdict, why2 = wait_for_user(action, why, ctx, conf)
            ev["asked"] = redact(why2)
            if verdict == "allow" and cacheable:
                approvals(approvals() | {digest: time.time()})
            why = f"{why}; {why2}"
        else:
            verdict = "ask"
    ms = int((time.monotonic() - t0) * 1000)
    if record:
        log(ev | {"decision": verdict, "ms": ms})
    return {"decision": verdict, "reason": f"Greenlight guard: {why}", "tier": v.tier, "ms": ms, "s1": s1}


def hook_main(stdin=sys.stdin, stdout=sys.stdout):
    conf = config()
    if conf is None:
        return 0
    try:
        ev = json.load(stdin)
    except ValueError:
        return 0
    r = decide_call(ev.get("tool_name", ""), ev.get("tool_input") or {}, ev.get("cwd"), ev.get("session_id", ""), conf)
    if r["decision"] == "pass":
        return 0
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": r["decision"],
                                      "permissionDecisionReason": r["reason"]}}, stdout)
    return 0


def settings(conf=None, how=None):
    """The hooks block that runs this guard before every tool call. how: ask | ping | chat, set in the hook's own env
    (so the agent can't change it); None leaves it to config and GREENLIGHT_UNATTENDED."""
    t = min(int((conf if conf is not None else config() or {}).get("timeout", 240)), 240) + 30
    env = f"GREENLIGHT_GUARD_MODE={how} " if how else ""
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"{env}python3 {shlex.quote(str(Path(__file__).resolve()))} hook", "timeout": t}]}]}}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "hook":
        return hook_main()
    if cmd == "settings":
        print(json.dumps(settings(how=argv[argv.index("--mode") + 1] if "--mode" in argv[:-1] else None)))
        return 0
    if cmd in ("check", "run"):
        rest = argv[2:]
        as_json = "--json" in rest[:rest.index("--")] if "--" in rest else "--json" in rest
        rest = rest[rest.index("--") + 1:] if "--" in rest else [a for a in rest if a != "--json"]
        if not rest:
            sys.exit(f"guard.py {cmd} -- <command>")
        line = rest[0] if cmd == "check" and len(rest) == 1 else shlex.join(rest)
        conf = config() if cmd == "run" else (config() or {})
        if cmd == "run" and conf is None:
            os.execvp(rest[0], rest)
        r = decide_call("Bash", {"command": line}, os.getcwd(), os.environ.get("GREENLIGHT_SESSION", "cli"), conf,
                        interactive_ok=cmd == "check", record=cmd == "run", dry_run=cmd == "check")
        if cmd == "check":
            print(json.dumps(r) if as_json else f"{r['decision']}: {r.get('reason', 'no boundary crossed')} ({r['ms']} ms)")
            return {"pass": 0, "allow": 0, "ask": 1, "deny": 2}[r["decision"]]
        if r["decision"] in ("pass", "allow"):
            os.execvp(rest[0], rest)
        print(r["reason"], file=sys.stderr)
        return 2
    if cmd == "log":
        n = int(argv[3]) if len(argv) > 3 and argv[2] == "-n" else 20
        f = STATE / "guard.jsonl"
        for e in (map(json.loads, f.read_text().splitlines()[-n:]) if f.exists() else []):
            s1 = e.get("s1") or {}
            print(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(e['ts']))} {e['decision']:5} {e['tier']:6} {e['ms']:>5}ms "
                  f"s1={s1.get('provider', '-')}:{s1.get('prob', '-')}/{e.get('s1_ms', 0)}ms  {e['action'][:100]}")
        return 0
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
