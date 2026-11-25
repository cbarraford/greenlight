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
import subprocess
import sys
import time
from pathlib import Path

AGENTS = ("claude", "codex", "agy", "gemini", "opencode", "pi")
ALIASES = {"antigravity": "agy", "claude-code": "claude"}
TTL = 5.0
_CACHE: dict = {"at": 0.0, "val": None}


def agent_of(argv):
    """The agent an argv runs (its program, or the script a node/python/bun runs), else None."""
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


def _proc_linux():
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw if a]
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])  # after "(comm)": state, ppid
        out[int(d.name)] = {"ppid": ppid, "argv": argv}
    return out


def _cwd_linux(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _proc_mac():
    p = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,args="], capture_output=True, text=True, timeout=10)
    out = {}
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = {"ppid": int(parts[1]), "argv": (parts[2] if len(parts) > 2 else "").split()}
    return out


def _cwds_mac(pids):
    if not pids:
        return {}
    p = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
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
    """[{pid, agent, cwd}] for every agent process outside greenlight's tmux, topmost only (a claude's own node
    children aren't another agent). procs/roots/cwd_of: injected by tests."""
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
    top = [pid for pid in found if procs[pid]["ppid"] not in found and not ours(pid)]
    if cwd_of is None:
        cwds = _cwds_mac(top) if mac else {pid: _cwd_linux(pid) for pid in top}
        cwd_of = cwds.get
    return [{"pid": pid, "agent": found[pid], "cwd": c} for pid in sorted(top) if (c := cwd_of(pid))]


def agents(fresh=False):
    """scan(), cached for TTL seconds; [] when the process table can't be read."""
    if not fresh and _CACHE["val"] is not None and time.time() - _CACHE["at"] < TTL:
        return _CACHE["val"]
    try:
        val = scan()
    except (OSError, subprocess.SubprocessError, ValueError):
        val = []
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


def busy(path, found=None):
    """True when an agent outside greenlight's tmux runs in path (or below it)."""
    return bool(by_worktree([path], found).get(path.rstrip("/")))


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
