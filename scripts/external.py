"""Coding agents running outside Greenlight's own tmux server, by the worktree they run in (#195 passive coexistence).

While someone still uses another ADE (Orca, T3, cmux, herdr, Rex) or a plain terminal (Ghostty, iTerm, VS Code, …)
alongside the native engine, greenlight must not dispatch into, or delete, a worktree where an agent of theirs is
live. No per-ADE driver: an agent is a process (claude, codex, agy, gemini, opencode, pi) whose working directory is
inside the worktree, and it is ours when it descends from a pane of `tmux -L greenlight`.

    python3 external.py [--json]     every external agent: worktree-relative facts only (pid, agent, cwd)

Linux reads /proc; macOS asks `ps` and `lsof`. Read-only, stdlib only, cached for a few seconds (a scan asks often).
"""
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

AGENTS = ("claude", "codex", "agy", "gemini", "opencode", "pi")
ALIASES = {"claude-code": "claude"}  # not "antigravity": that's the editor, open for days in a folder; its CLI is agy
TTL = 5.0
_CACHE: dict = {"at": 0.0, "val": None}


APP_RE = re.compile(r"\.app/Contents/|^--type=")  # a desktop app or an Electron helper, not a CLI agent


def agent_of(argv):
    """The agent an argv runs (its program, or the script a node/python/bun runs), else None. A desktop app named
    like one (Claude.app, Codex.app, Antigravity and its helpers) isn't: the CLI agents it starts are counted."""
    if any(APP_RE.search(a) for a in argv):
        return None
    for a in argv[:3]:
        if a.startswith("-"):
            continue
        b = os.path.basename(a).lower().removesuffix(".js").removesuffix(".mjs")
        b = ALIASES.get(b, b)
        if b in AGENTS:
            return b
        if "/@anthropic-ai/claude-code/" in a:
            return "claude"
        if "/@openai/codex/" in a:
            return "codex"
        if "/@google/gemini-cli/" in a:
            return "gemini"
        if b not in ("node", "bun", "deno", "python", "python3", "env"):
            return None  # some other program: an argument naming an agent ("vim codex.md") isn't one
    return None


def _boot_linux():
    with open("/proc/stat") as f:
        return next((int(x.split()[1]) for x in f if x.startswith("btime ")), 0)


def _proc_linux():
    out, boot, hz = {}, _boot_linux(), os.sysconf("SC_CLK_TCK")
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw if a]
        f = stat[stat.rindex(")") + 2:].split()  # after "(comm)": state, ppid, …, starttime (ticks since boot) at 19
        out[int(d.name)] = {"ppid": int(f[1]), "argv": argv, "start": boot + int(f[19]) // hz if len(f) > 19 else None}
    return out


def _cwd_linux(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def etime_secs(t):
    """ps's etime ([[dd-]hh:]mm:ss) in seconds, else None."""
    days, _, rest = t.rpartition("-")
    try:
        parts = [int(x) for x in rest.split(":")]
        return (int(days) if days else 0) * 86400 + sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
    except ValueError:
        return None


def _ps(fields):
    p = subprocess.run(["ps", "-axww", "-o", fields], capture_output=True, text=True, timeout=10)
    if p.returncode:
        raise OSError(f"ps exited {p.returncode}")
    return p.stdout.splitlines()


def _proc_mac():
    """ps has no NUL-split argv: the program is `comm` (its whole path, spaces and all: ~/Library/Application Support/…),
    the arguments are what follows it in `args`."""
    args = {int(p[0]): p[1] if len(p) > 1 else "" for line in _ps("pid=,args=") if (p := line.strip().split(None, 1)) and p[0].isdigit()}
    out, now = {}, int(time.time())
    for line in _ps("pid=,ppid=,etime=,comm="):
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            pid, comm, up = int(parts[0]), parts[3] if len(parts) > 3 else "", etime_secs(parts[2])
            rest = args.get(pid, "")
            rest = rest[len(comm):] if comm and rest.startswith(comm) else " ".join(rest.split()[1:])
            out[pid] = {"ppid": int(parts[1]), "argv": [comm, *rest.split()] if comm else rest.split(),
                        "start": now - up if up is not None else None}
    return out


def _cwds_mac(pids):
    if not pids:
        return {}
    lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"  # -b: never block on a hung mount
    p = subprocess.run([lsof, "-b", "-w", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                       capture_output=True, text=True, timeout=10)
    out, cur = {}, None
    for line in p.stdout.splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            cur = int(line[1:])
        elif line[:1] == "n" and cur is not None:
            out[cur] = line[1:]
    return out


def _tmux_roots():
    """The pids of every pane on greenlight's tmux server: what descends from them is ours."""
    sock = os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"
    try:
        p = subprocess.run(["tmux", "-L", sock, "list-panes", "-a", "-F", "#{pane_pid}"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(x) for x in p.stdout.split() if x.isdigit()}


def scan(procs=None, roots=None, cwd_of=None):
    """[{pid, agent, cwd, start}] for every agent process outside greenlight's tmux (start: when it began, epoch s). An
    agent another agent started counts on its own only in another folder (a claude's own children aren't another
    agent; a codex it runs in another worktree is). procs/roots/cwd_of: injected by tests."""
    mac = platform.system() == "Darwin"
    if procs is None:
        procs = _proc_mac() if mac else _proc_linux()
    roots = _tmux_roots() if roots is None else roots

    def ours(pid):
        seen = set()
        while pid and pid not in seen and pid in procs:
            if pid in roots:
                return True
            seen.add(pid)
            pid = procs[pid]["ppid"]
        return pid in roots

    found = {pid: a for pid, p in procs.items() if (a := agent_of(p["argv"]))}
    outside = [pid for pid in found if not ours(pid)]
    if cwd_of is None:
        cwds = _cwds_mac(outside) if mac else {pid: _cwd_linux(pid) for pid in outside}
        cwd_of = cwds.get
    out = []
    for pid in sorted(outside):
        c, parent = cwd_of(pid), procs[pid]["ppid"]
        if c and not (parent in found and cwd_of(parent) == c):
            out.append({"pid": pid, "agent": found[pid], "cwd": c, "start": procs[pid].get("start")})
    return out


class Unknown(RuntimeError):
    """The process table couldn't be read: whether an agent runs anywhere is unknown."""


def agents(fresh=False, strict=False):
    """scan(), cached for TTL seconds. When the process table can't be read: [] (listing and dispatch carry on), or,
    strict, Unknown (cleanup: a deletion must not go ahead on a scan that failed)."""
    if not fresh and _CACHE["val"] is not None and time.time() - _CACHE["at"] < TTL:
        return _CACHE["val"]
    try:
        val = scan()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        _CACHE.update(at=0.0, val=None)
        if strict:
            raise Unknown(f"{type(e).__name__}") from e
        return []
    _CACHE.update(at=time.time(), val=val)
    return val


def by_worktree(paths, found=None):
    """{worktree path: [agent]} for the agents whose cwd is inside one of paths (the deepest one wins: a nested
    worktree owns its own agents)."""
    deep = sorted({p.rstrip("/") for p in paths if p}, key=len, reverse=True)
    out: dict = {}
    for a in agents() if found is None else found:
        owner = next((w for w in deep if a["cwd"] == w or a["cwd"].startswith(w + "/")), None)
        if owner:
            out.setdefault(owner, []).append(a)
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import diag
    diag.install("external")
    rows = agents(fresh=True)
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            print(f"{r['pid']:>7}  {r['agent']:9} {r['cwd']}")
