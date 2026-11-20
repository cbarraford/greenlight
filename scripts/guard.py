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

config.json `guard`: {"on": true, "mode": "auto"|"ask"|"ping", "timeout": 240, "budget_ms": 200,
  "hosts": [extra hosts a GET may reach], "allow": [regexes a call auto-allows], "deny": [regexes it refuses]}.
Off (no `guard` key, or "on": false) the hook says nothing. Every boundary decision is logged to ~/.greenlight/guard.jsonl.
Stdlib only; never imports greenlight.py on the fast path (it reads the forge's config); decide.py is optional.

Known limits: this reads the command line, not what a program does. A script file the agent wrote and then runs,
or code it decodes at run time, is opaque here (Write/Edit content is scanned for a secret-plus-network combination,
and codex's own sandbox blocks the network). It is a gate on the obvious paths, not a sandbox.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
HOME = Path.home()

INVARIANTS = """\
- No secret (token, key, password, credential file, env var holding one) leaves this machine: not in a URL, a request body, a PR/MR/issue comment, a commit pushed, a gist.
- Nothing is pushed to the default branch, and no branch but the workspace's own is force-pushed.
- Writes go to the workspace's own repo on its forge, never another repo or an arbitrary host.
- Nothing outside the worktree and the temp dirs is deleted or overwritten; the guard, its config and the agent's settings are never edited.
- No other workspace is removed; this one only when its goal is done."""

# Hosts a plain GET may reach without asking: the forges and package indexes agents fetch from.
HOSTS = {"github.com", "api.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com", "codeload.github.com",
         "gitlab.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "crates.io", "static.crates.io",
         "proxy.golang.org", "docs.python.org", "developer.apple.com", "docs.anthropic.com", "docs.claude.com"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
TIERS = ["pass", "low", "medium", "high", "deny"]

SECRET_RE = re.compile(r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gh[osu]_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{20,}"
                       r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}"
                       r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
SECRET_NAME_RE = re.compile(r"(?:KEY|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIALS?|AUTH|COOKIE|SESSION)(?:_|$)|^PAT$", re.I)
SECRET_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# Files whose contents are credentials. Matched against the resolved path.
SECRET_PATH_RE = re.compile(r"(?:^|/)(?:\.ssh/|\.aws/|\.gnupg/|\.kube/config|\.docker/config\.json|\.netrc|\.git-credentials|\.npmrc|\.pypirc"
                            r"|\.config/gh/hosts\.yml|\.config/glab-cli/|\.config/gcloud/|Library/Keychains/|\.password-store/"
                            r"|\.env(?:\.[\w.-]+)?$|[^/]*credentials[^/]*\.json$|id_(?:rsa|ed25519|ecdsa|dsa)[^/]*$|[^/]+\.(?:pem|p12|pfx|key)$)")
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
    try:
        c = json.loads((STATE / "config.json").read_text()).get("guard")
    except (FileNotFoundError, ValueError):
        c = None
    if c is True:
        c = {}
    return c if isinstance(c, dict) and c.get("on", True) else None


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

    def __init__(self, cwd):
        self.cwd = Path(cwd or os.getcwd())
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
        top = self.git("rev-parse", "--show-toplevel")
        return Path(top) if top else self.cwd

    @property
    def branch(self):
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    @property
    def default(self):
        head = self.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return {head.split("/", 1)[-1]} if head else {"main", "master"}

    @property
    def remotes(self):
        return set(self.git("remote").split())

    def path(self, p):
        p = os.path.expandvars(os.path.expanduser(p))
        return Path(os.path.normpath(p if os.path.isabs(p) else self.cwd / p))

    def inside(self, p):
        """In the worktree (not its .git) or a temp dir."""
        s = str(p)
        tmps = [os.environ.get("TMPDIR", ""), "/tmp", "/private/tmp", "/var/folders", str(HOME / ".cache")]
        if any(t and (s == t.rstrip("/") or s.startswith(t.rstrip("/") + "/")) for t in tmps):
            return True
        root = str(self.root)
        return s.startswith(root + "/") and "/.git/" not in s[len(root):] + "/" and not s.endswith("/.git")


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


def secret_vars(text):
    return [n for n in SECRET_VAR_RE.findall(text or "") if SECRET_NAME_RE.search(n)]


def host_of(url):
    m = re.match(r"(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]*@)?(\[[^\]]+\]|[^:/?#\s]+)", url, re.I)
    return (m[1] if m else "").lower()


def strip_heredocs(script):
    """(script without heredoc bodies, [(command line it feeds, body)])."""
    lines, out, docs, i = script.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tags = re.findall(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1", line)
        i += 1
        for _, tag in tags:
            body = []
            while i < len(lines) and lines[i].strip() != tag:
                body.append(lines[i])
                i += 1
            i += 1
            docs.append((line, "\n".join(body)))
    return "\n".join(out), docs


def substitutions(script):
    """The bodies of $(...) and `...`, which run even inside double quotes."""
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
    """[(argv, redirect targets)] for each simple command, or None when it can't be parsed."""
    script = re.sub(r"(?<![\w$-])\d+(?=>)", "", script)  # 2>/dev/null: the fd isn't an argument
    lex = shlex.shlex(script.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        toks = list(lex)
    except ValueError:
        return None
    out, argv, redir, want = [], [], [], False
    for t in toks:
        if want:
            if not re.fullmatch(r"&\d*|\d+", t):  # 2>&1, >&2
                redir.append(t)
            want = False
        elif t in (">", ">>", ">|", "&>", "&>>"):
            want = True
        elif t and set(t) <= set(";&|()"):
            if argv:
                out.append((argv, redir))
            argv, redir = [], []
        elif t in ("<", "<<", "<<<", "<<-"):
            want = False
        else:
            argv.append(t)
    if argv:
        out.append((argv, redir))
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
                v = v[1:]
        elif b == "env":
            rest = v[1:]
            while rest and (rest[0].startswith("-") or "=" in rest[0]):
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
    for n in secret_vars(script):
        v.add("high", f"reads secret env var ${n}", "secret")
    body, docs = strip_heredocs(script)
    for line, doc in docs:
        head = commands(line) or []
        first = unwrap(head[0][0]) if head and head[0][0] else []
        name = os.path.basename(first[0]) if first else ""
        if name in SHELLS:
            v.merge(check_shell(doc, ctx, depth + 1))
        elif name.rstrip("0123456789.") in INTERPS:
            v.merge(check_code(doc, name))
    for sub in substitutions(body):
        v.merge(check_shell(sub, ctx, depth + 1))
    cmds = commands(body)
    if cmds is None:
        v.add("medium", "can't parse the command line")
        if NET_CODE_RE.search(body) or re.search(r"\b(?:rm|git\s+push|gh|glab)\b", body):
            v.add("high", "unparseable and names a network or destructive tool")
        return v
    for i, (argv, redir) in enumerate(cmds):
        for r in redir:
            v.merge(check_write(ctx.path(r), ctx, "redirect"))
        argv = unwrap(argv)
        if argv:
            v.merge(check_argv(argv, ctx, depth, piped_from=i > 0 and cmds[i - 1]))
    if "secret" in v.flags and "egress" in v.flags:
        v.add("deny", "reads a secret and sends data off the machine in one command")
    return v


def check_code(code, lang):
    v = Verdict()
    net = NET_CODE_RE.search(code)
    if net:
        v.add("medium", f"{lang} code that reaches the network ({net[0]})", "egress")
    if SECRET_PATH_RE.search(code) or secret_vars(code) or re.search(r"os\.environ\b(?!\.get\(\s*['\"](?:HOME|PATH|PWD|USER)\b)|process\.env\b(?!\.(?:HOME|PATH|PWD))", code):
        v.add("medium" if not net else "deny", "code that reads secrets or the environment" + (" and sends data out" if net else ""), "secret")
    if re.search(r"rmtree|os\.remove|unlink|rm -rf|fs\.rm", code):
        v.add("medium", f"{lang} code that deletes files")
    if leak := leaks(code):
        v.add("deny" if net else "medium", f"the code {leak}", "secret")
    return v


def check_argv(argv, ctx, depth, piped_from=None):
    v = Verdict()
    cmd, args = os.path.basename(argv[0]), argv[1:]
    if cmd.endswith("greenlight.py") or cmd in ("orca", "orca-ide") or (cmd.startswith("python") and any(a.endswith("greenlight.py") for a in args[:2])):
        rest = args[args.index("ade") + 1:] if "ade" in args else args
        if len(rest) >= 2 and rest[0] == "worktree" and rest[1] in ("rm", "remove", "delete"):
            v.add("medium", "removes a workspace")
        return v
    if cmd in SHELLS or cmd == "eval":
        if cmd == "eval":
            v.merge(check_shell(" ".join(args), ctx, depth + 1))
        elif "-c" in args and args.index("-c") + 1 < len(args):
            v.merge(check_shell(args[args.index("-c") + 1], ctx, depth + 1))
        elif piped_from:
            v.add("high", f"pipes into {cmd}: runs code this can't read")
        return v
    if cmd.rstrip("0123456789.") in INTERPS:
        for flag in ("-c", "-e", "--eval", "-r"):
            if flag in args and args.index(flag) + 1 < len(args):
                v.merge(check_code(args[args.index(flag) + 1], cmd))
        if piped_from and not any(a.endswith((".py", ".js", ".rb", ".pl", ".ts")) for a in args) and "-m" not in args:
            v.add("high", f"pipes into {cmd}: runs code this can't read")
        return v
    if cmd == "git":
        return check_git(args, ctx)
    if cmd in ("gh", "glab"):
        return check_forge(cmd, args, ctx)
    if cmd in NET_TOOLS:
        return check_net(cmd, args, ctx)
    if cmd in RAW_NET:
        dest = next((a for a in args if not a.startswith("-")), "")
        if cmd == "rsync" and not any(":" in a for a in args if not a.startswith("-")):
            return v  # a local copy
        if host_of(dest.split(":")[0]) in LOCAL_HOSTS:
            return v
        v.add("high", f"{cmd} opens a connection off the machine", "egress")
        return v
    if cmd in ("rm", "rmdir", "unlink", "shred", "trash", "srm"):
        return check_rm(args, ctx)
    if cmd == "find" and ("-delete" in args or any(a in ("-exec", "-execdir", "-ok") for a in args)):
        roots = [a for a in args if not a.startswith(("-", "(", ")", "!"))][:1] or ["."]
        if "-delete" in args or re.search(r"\brm\b", " ".join(args)):
            return check_rm(["-r", *roots], ctx)
    if cmd in ("dd", "mkfs", "diskutil", "fdisk", "parted") or cmd.startswith("mkfs."):
        v.add("high", f"{cmd} writes disks")
        return v
    if cmd in ("tee", "cp", "mv", "install", "ln", "truncate", "chmod", "chown"):
        targets = [a for a in args if not a.startswith("-")]
        if cmd in ("cp", "mv", "install", "ln"):
            targets = targets[-1:]
        for t in targets:
            v.merge(check_write(ctx.path(t), ctx, cmd))
        if cmd in ("cp", "mv") and any(SECRET_PATH_RE.search(str(ctx.path(a))) for a in args[:-1] if not a.startswith("-")):
            v.add("high", f"{cmd} copies a credential file", "secret")
        return v
    if cmd in ("env", "printenv", "export", "set", "declare") and (not args or args in (["-p"], ["-x"])):
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
            "install", "add", "i", "get") and len([a for a in args[1:] if not a.startswith("-")]) > 0:
        v.add("low", f"{cmd} installs packages from the network")
        return v
    if cmd in ("npx", "pnpx", "bunx", "uvx", "pipx") and args:
        v.add("low", f"{cmd} fetches and runs a package")
        return v
    # any other command: read its file arguments for credentials
    for a in args:
        if not a.startswith("-") and SECRET_PATH_RE.search(str(ctx.path(a))):
            v.add("high", f"reads a credential file ({a})", "secret")
    return v


def check_git(args, ctx):
    v = Verdict()
    a = list(args)
    while a and a[0].startswith("-"):  # git -C dir -c k=v ...
        if a[0] == "-C" and len(a) > 1:
            ctx = Ctx(ctx.path(a[1]))
            a = a[2:]
        elif a[0] in ("-c", "--git-dir", "--work-tree", "--namespace") and len(a) > 1:
            if a[0] == "-c" and re.match(r"(?i)(core\.(sshCommand|hooksPath|fsmonitor|pager|editor)|credential\.|url\.|alias\.)", a[1]):
                v.add("high", f"git -c {a[1].split('=')[0]} runs or routes things outside the repo")
            a = a[2:]
        else:
            a = a[1:]
    if not a:
        return v
    sub, rest = a[0], a[1:]
    if sub == "push":
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
    elif sub == "reset" and "--hard" in rest:
        v.add("medium", "git reset --hard drops uncommitted work")
    elif sub == "clean" and any(re.match(r"-\w*f", x) or x == "--force" for x in rest):
        v.add("medium", "git clean deletes untracked files")
    elif sub == "branch" and any(x in ("-D", "--delete", "-d") for x in rest) or sub == "stash" and rest[:1] and rest[0] in ("drop", "clear"):
        v.add("low", f"git {sub} deletes work")
    elif sub == "worktree" and rest[:1] and rest[0] in ("remove", "prune"):
        v.add("medium", "removes a git worktree")
    elif sub in ("filter-branch", "filter-repo") or sub == "update-ref" and "-d" in rest or sub == "reflog" and "expire" in rest:
        v.add("medium", f"git {sub} rewrites history")
    elif sub == "send-email" or sub == "credential" or sub == "archive" and "--remote" in " ".join(rest):
        v.add("high", f"git {sub} reaches outside the repo", "egress")
    return v


def check_push(rest, ctx, v):
    pos = [x for x in rest if not x.startswith("-")]
    flags = [x for x in rest if x.startswith("-")]
    force = any(f in ("-f", "--force") or f.startswith("--force-with-lease") or f == "--force-if-includes" or re.fullmatch(r"-\w*f\w*", f)
                for f in flags) or any(p.startswith("+") for p in pos[1:])
    remote = pos[0] if pos else "origin"
    if remote not in ctx.remotes and remote != "origin" and ("/" in remote or ":" in remote or "@" in remote):
        v.add("high", f"pushes to {remote}, not one of the repo's remotes", "egress")
    if any(f in ("--mirror", "--all", "--tags", "--prune") for f in flags):
        v.add("high", "pushes every branch or tag, or deletes remote ones", "egress")
    if any(f in ("-d", "--delete") for f in flags) or any(p.startswith(":") for p in pos[1:]):
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
            v.add("medium", f"pushes {dst}, not this workspace's branch ({ctx.branch})", "egress")
        elif force:
            v.add("medium", f"force-pushes {dst}", "egress")
        else:
            v.add("low", f"pushes {dst}", "egress")
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
    text = " ".join(a)
    body = " ".join(filter(None, [opt(a, "--body", "-b", "--message", "-m", "--description", "-d", "--title", "-t", "--notes")]))
    bfile = opt(a, "--body-file", "-F", "--notes-file")
    if bfile and bfile != "-":
        p = ctx.path(bfile)
        if SECRET_PATH_RE.search(str(p)):
            v.add("deny", f"posts a credential file ({bfile}) to {cmd}", "secret", "egress")
        try:
            body += "\n" + p.read_text(errors="replace")[:200000]
        except OSError:
            pass
    if leak := leaks(body) or leaks(text):
        v.add("deny", f"the text it posts {leak}", "secret", "egress")
    if secret_vars(text):
        v.add("deny", "posts a secret env var", "secret", "egress")
    other = opt(a, "--repo", "-R")
    if other and not own_repo(other, ctx):
        if verb not in FORGE_READ:
            v.add("high", f"writes to {other}, not this workspace's repo", "egress")
    if group == "auth":
        if verb == "token" or "--show-token" in a or "-t" in a:
            v.add("high", f"{cmd} auth prints the token", "secret")
        elif verb not in ("status",):
            v.add("high", f"{cmd} auth {verb} changes who the agent is")
        return v
    if group == "api":
        method = (opt(a, "-X", "--method") or "").upper()
        writes = method not in ("", "GET", "HEAD") or any(x in a or any(y.startswith(x + "=") for y in a) for x in ("-f", "-F", "--field", "--raw-field", "--input"))
        if writes and method != "GET":
            v.add("high" if method == "DELETE" else "medium", f"{cmd} api {method or 'POST'} writes to the forge", "egress")
        return v
    if verb in FORGE_READ or group in ("help", "version", "--version", "completion"):
        return v
    if group in ("secret", "variable", "ssh-key", "gpg-key", "extension", "alias", "config") or group == "repo" and verb in (
            "delete", "archive", "rename", "edit", "create", "fork", "set-default", "unarchive", "deploy-key") or group == "gist" or group == "release":
        v.add("high", f"{cmd} {group} {verb} changes the repo or account, or publishes", "egress")
        return v
    low = {("pr", "create"), ("pr", "edit"), ("pr", "ready"), ("mr", "create"), ("mr", "update"), ("pr", "checkout"), ("mr", "checkout")}
    if (group, verb) in low:
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
    name = re.sub(r"\.git$", "", re.sub(r"^(?:https?://|git@)[^/:]+[/:]", "", url)).lower()
    return bool(name) and re.sub(r"^(?:https?://)?[^/]+\.[a-z]+/", "", repo.lower()).rstrip("/") == name


def check_net(cmd, args, ctx):
    v = Verdict()
    urls = [a for a in args if re.match(r"(?i)[a-z][a-z0-9+.-]*://", a) or (not a.startswith("-") and re.match(r"[\w.-]+\.[a-z]{2,}(?:[:/]|$)", a))]
    hosts = {host_of(u) for u in urls}
    if hosts and hosts <= LOCAL_HOSTS:
        return v
    data = [a for a in args if re.match(r"--data|-d$|-F$|--form|--json|-T$|--upload-file|--post-|--body-|--method", a)]
    method = (opt(args, "-X", "--request", "--method") or "").upper()
    full = " ".join(args)
    for i, a in enumerate(args):
        if a.startswith("@") or (i and args[i - 1] in ("-d", "-F", "--data", "--data-binary", "--data-raw", "--form", "-T", "--upload-file")
                                 and ("@" in a or args[i - 1] in ("-T", "--upload-file"))):
            path = a.split("@", 1)[-1].split(";")[0] if "@" in a else a
            if path != "-" and SECRET_PATH_RE.search(str(ctx.path(path))):
                v.add("deny", f"uploads a credential file ({path})", "secret", "egress")
    if leak := leaks(full):
        v.add("deny", f"the request {leak}", "secret", "egress")
    if secret_vars(full) and not any(re.search(r"(?i)authorization|x-api-key|private-token", a) for a in args):
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
        p = ctx.path(raw)
        s = str(p)
        if s in ("/", str(HOME), "/home", "/Users", "/root") or str(HOME).startswith(s + "/") or raw in ("~", "~/", "/*", "~/*"):
            v.add("deny", f"deletes {raw}: the home folder or above")
        elif re.search(r"\$\{?(?!HOME\b|PWD\b|TMPDIR\b)\w", raw):
            v.add("medium", f"deletes {raw}: a path from a variable that may be empty")
        elif str(ctx.root).startswith(s + "/") or s == str(ctx.root):
            v.add("high", f"deletes the worktree itself, or a folder above it ({raw})")
        elif GUARD_PATH_RE.search(s):
            v.add("deny", f"deletes the guard or its config ({raw})")
        elif s.endswith("/.git") or "/.git/" in s:
            v.add("high", f"deletes git's own data ({raw})")
        elif not ctx.inside(p):
            v.add("high", f"deletes {raw}, outside the worktree")
    return v


def check_write(p, ctx, how="write"):
    v = Verdict()
    s = str(p)
    if s in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or s.startswith("/dev/fd/"):
        return v
    if GUARD_PATH_RE.search(s):
        v.add("deny", f"{how} to {s}: that would change the guard or the agent's own permissions")
    elif ESCAPE_PATH_RE.search(s):
        v.add("high", f"{how} to {s}: runs code outside the agent's turn")
    elif SECRET_PATH_RE.search(s):
        v.add("high", f"{how} to a credential file ({s})", "secret")
    elif not ctx.inside(p) and not re.search(r"/\.claude/projects/[^/]+/memory/|/\.greenlight/(?!config\.json)", s):
        v.add("medium", f"{how} to {s}, outside the worktree")
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
    if tool.startswith("mcp__"):
        name = tool.split("__")[-1].lower()
        if re.search(r"send|post|create|update|delete|comment|reply|forward|share|upload|write|publish|merge|push|trash|move|invite|cancel|invoke", name):
            v.add("medium", f"{tool} changes something outside this machine", "egress")
            blob = json.dumps(inp)
            if leak := leaks(blob):
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
    except (AttributeError, TypeError, ValueError):
        r = {"value": False, "prob": 0.5, "provider": "local", "error": "bad answer"}
    return r, ms


def judge(v, action, ctx, conf):
    """The final call for a boundary action: allow | pause | deny, with the System 1 answer it rests on."""
    for pat in conf.get("deny") or []:
        if re.search(pat, action):
            return "deny", f"matches your guard.deny rule /{pat}/", None, 0
    if v.tier == "deny":
        return "deny", "; ".join(v.why), None, 0
    for pat in conf.get("allow") or []:
        if re.search(pat, action) and v.tier != "deny":
            return "allow", f"matches your guard.allow rule /{pat}/", None, 0
    name, goal, rules = situation(ctx)
    context = (f"Workspace: {name or ctx.root.name}\nBranch: {ctx.branch}\nGoal: {goal or '(none known)'}\n"
               f"Repo rules:\n{rules or '(none)'}\nSecurity invariants:\n{INVARIANTS}\n"
               f"Risk the rules saw ({v.tier}): {'; '.join(v.why)}\nAction:\n{action[:4000]}")
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
    why = "; ".join(v.why) + (f" (System 1 {s1['provider']}: {p:.2f} aligned)" if real else f" (System 1 unavailable: {s1['provider']})")
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
    timeout = int(conf.get("timeout", 240))
    name, goal, _ = situation(ctx)
    key = f"@guard-{int(time.time() * 1000):x}-{os.getpid():x}"
    ws = name or ctx.root.name
    text = (f"{ws}: an unattended agent wants to run\n{summary[:600]}\nWhy it asks: {why[:400]}\n"
            f"Goal: {goal[:200] or '(none known)'}\nDenied in {timeout // 60 or 1} min if unanswered. [{key}]\nOptions: allow | deny")
    try:
        import greenlight
        greenlight.ping(text, key=key, ws=[name] if name else [], kind="guard")
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
            fh.write(json.dumps({"ts": int(time.time())} | ev) + "\n")
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


def decide_call(tool, inp, cwd, session="", conf=None, interactive_ok=True, record=True):
    """{decision: pass|allow|ask|deny, reason, tier, ...}. pass: no opinion."""
    t0 = time.monotonic()
    conf = conf if conf is not None else (config() or {})
    ctx = Ctx(cwd)
    action = describe(tool, inp)
    try:
        v = check_tool(tool, inp, ctx)
    except Exception as e:  # noqa: BLE001 - a crash on a call that names a boundary tool can't wave it through
        v = Verdict()
        v.add("high" if re.search(r"\b(?:push|curl|wget|gh|glab|rm|ssh|scp|nc)\b|https?://", action) else "pass",
              f"guard error {type(e).__name__}: {e}")
    if v.tier == "pass" and not any(re.search(p, action) for p in (conf.get("deny") or [])):
        return {"decision": "pass", "tier": "pass", "ms": int((time.monotonic() - t0) * 1000)}
    verdict, why, s1, s1_ms = judge(v, action, ctx, conf)
    ev = {"tool": tool, "action": action[:500], "cwd": str(ctx.cwd), "tier": v.tier, "rules": v.why, "s1": s1, "s1_ms": s1_ms,
          "session": session}
    digest = hashlib.sha256(f"{session}\0{tool}\0{json.dumps(inp, sort_keys=True)}".encode()).hexdigest()[:24]
    if verdict == "pause" and digest in approvals():
        verdict, why = "allow", "you allowed this exact call in this session within the hour"
    if verdict == "pause":
        how = "ping" if not interactive_ok and mode(conf) == "ask" else mode(conf)
        token = hashlib.sha256(f"{tool}\0{json.dumps(inp, sort_keys=True)}".encode()).hexdigest()[:10]
        if how == "chat":
            if consented(token):
                verdict, why = "allow", f"{why}; you allowed it in the chat"
            else:
                verdict = "deny"
                why = (f"{why}. Paused for the user: ask them in this chat what you want to run and why, with [guard:{token}] "
                       f"in the question and the line `Options: allow | deny` last; if they answer allow, run the exact same call again")
        elif how == "ping":
            verdict, why2 = wait_for_user(action, why, ctx, conf)
            ev["asked"] = why2
            if verdict == "allow":
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
    t = int((conf if conf is not None else config() or {}).get("timeout", 240)) + 30
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
                        interactive_ok=cmd == "check", record=cmd == "run")
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
