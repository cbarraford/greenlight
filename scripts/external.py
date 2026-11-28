"""Coding agents running outside Greenlight's own tmux server, by the worktree they run in (#195 passive coexistence).

While someone still uses another ADE (Orca, T3, cmux, herdr, Rex) or a plain terminal (Ghostty, iTerm, VS Code, …)
alongside the native engine, greenlight must not dispatch into, or delete, a worktree where an agent of theirs is
live. No per-ADE driver: an agent is a process (claude, codex, agy, gemini, opencode, pi) whose working directory is
inside the worktree, and it is ours when it descends from a pane of `tmux -L greenlight`.

    python3 external.py [--json] [--all]   every agent outside Greenlight's panes (--all: its own too, with `pane`)

Linux reads /proc; macOS asks `ps` and `lsof`. Read-only, stdlib only, cached for a few seconds (a scan asks often).
A scan that fails fails closed (cleanup: strict; native rows: by_worktree_safe). Known gap: an app that runs its
agents inside one server process whose cwd isn't the worktree (the Codex desktop app's app-server) isn't seen.
"""
import functools
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import bins

AGENTS = ("claude", "codex", "agy", "gemini", "opencode", "pi")
ALIASES = {"claude-code": "claude"}  # not "antigravity": that's the editor, open for days in a folder; its CLI is agy
TTL = 5.0
FAIL_BACKOFF = 5.0  # after a failed scan, don't retry for this long (orchestrate sets TTL 0 but keeps this)
_CACHE: dict = {"at": 0.0, "val": None, "failed": 0.0}
_LOCK = threading.Lock()  # one scan at a time: cleanup's worker threads share it


PACKAGES = {"claude": ("/@anthropic-ai/claude-code/", "/@anthropic-ai/claude-agent-sdk/"),  # the SDK: T3 Code's Claude
            "codex": ("/@openai/codex/",), "gemini": ("/@google/gemini-cli/",)}
# servers that never edit a folder: `claude mcp serve` too (`claude mcp add` is a one-shot config command). Not
# `codex app-server`: T3 and the VS Code extension run one per session in the worktree (an editor's idle one is held
# until the 6 h hold hands it to the user)
SERVERS = {"mcp-server", "mcp"}  # not `serve`: an ADE may run `opencode serve` per worktree, and it runs the sessions there
APP_RE = re.compile(r"\.app/Contents/(?:MacOS|Frameworks)/")  # an app's own executable or helpers (an agent CLI it ships in Resources counts)
LAUNCHERS = ("node", "bun", "deno", "python", "python3", "env")
LAUNCHER_ARGS = {"run", "-r", "--require", "--import", "--loader"}  # `bun run x.js`, `node --require hook.js x.js`: not the script


def agent_of(argv):
    """The agent an argv runs (its program, or the script a node/python/bun runs), else None. A desktop app named
    like one (Claude.app, Codex.app, Antigravity and its helpers) isn't: the CLI agents it starts are counted."""
    if not argv or any(a.startswith("--type=") for a in argv):
        return None
    if APP_RE.search(argv[0]):  # an app; but one running an agent's package as node (ELECTRON_RUN_AS_NODE) runs that agent
        return next((k for a in argv[1:3] for k, pkgs in PACKAGES.items() if any(p in a for p in pkgs)), None)
    skip = False
    for i, a in enumerate(argv[:8]):
        if skip:
            skip = False
            continue  # the value of a launcher's --require
        if i and a in LAUNCHER_ARGS:
            skip = a.startswith("-")
            continue
        if a.startswith("-") or ("=" in a and not a.startswith("/")):
            continue  # a flag, or env's VAR=value
        b = os.path.basename(a).lower().removesuffix(".js").removesuffix(".mjs")
        b = ALIASES.get(b, b)
        kind = b if b in AGENTS else next((k for k, pkgs in PACKAGES.items() if any(p in a for p in pkgs)), None)
        if kind:
            rest = argv[i + 1:]
            words = [w for w in rest[:4] if not w.startswith("-")]
            if words[:1] and words[0] in SERVERS or words[:2] == ["mcp", "serve"]:
                return None  # a server that never edits a folder
            listen = next((rest[j + 1] if w == "--listen" and j + 1 < len(rest) else w.partition("=")[2]
                           for j, w in enumerate(rest) if w.startswith("--listen")), "stdio://")
            if words[:1] == ["app-server"] and ("daemon" in words or not listen.startswith("stdio")):
                return None  # codex's machine-wide app-server daemon, not a session's (those talk over stdio)
            return kind
        if b not in LAUNCHERS:
            return None  # some other program, or a launcher's script that isn't an agent's ("vim codex.md" isn't one): an argument naming an agent ("vim codex.md") isn't one
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
            f = stat[stat.rindex(")") + 2:].split()  # after "(comm)": state, ppid, …, starttime (ticks since boot) at 19
            ppid = int(f[1])
        except (OSError, ValueError, IndexError):
            continue  # exited mid-read: one odd entry never blanks the table
        argv = [a.decode(errors="replace") for a in raw if a]
        out[int(d.name)] = {"ppid": ppid, "argv": argv, "start": boot + int(f[19]) // hz if len(f) > 19 and f[19].isdigit() else None}
    return out


def _cwd_linux(pid):
    """The agent's cwd; None when it exited, or when it's another user's (their /proc cwd is closed to us: a known
    limit). Our own agent's cwd we can't read is a failed scan, never "nothing here"."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except FileNotFoundError:
        return None
    except OSError:
        try:
            mine = os.stat(f"/proc/{pid}").st_uid == os.getuid()
        except OSError:
            return None  # gone
        if mine:
            raise
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
    p = subprocess.run(["ps", "-axww", "-o", fields], capture_output=True, text=True, errors="replace", timeout=10)
    if p.returncode:
        raise OSError(f"ps exited {p.returncode}")
    return p.stdout.splitlines()


def _proc_mac():
    """ps has no NUL-split argv: the program is `comm` (its whole path, spaces and all: ~/Library/Application Support/…),
    the arguments are what follows it in `args`."""
    args = {int(p[0]): p[1] if len(p) > 1 else "" for line in _ps("pid=,args=") if (p := line.strip().split(None, 1)) and p[0].isdigit()}
    out, now = {}, int(time.time())
    me = str(os.getuid())
    for line in _ps("pid=,ppid=,etime=,uid=,comm="):
        parts = line.split(None, 4)
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
            pid, comm, up = int(parts[0]), parts[4] if len(parts) > 4 else "", etime_secs(parts[2])
            rest = args.get(pid, "")
            rest = rest[len(comm):] if comm and rest.startswith(comm) else " ".join(rest.split()[1:])
            out[pid] = {"ppid": int(parts[1]), "argv": [comm, *rest.split()] if comm else rest.split(),
                        "start": now - up if up is not None else None, "other": parts[3] != me}
    return out


def _cwds_mac(pids):
    if not pids:
        return {}
    lsof = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"  # -b: never block on a hung mount
    p = subprocess.run([lsof, "-b", "-w", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))],
                       capture_output=True, text=True, errors="replace", timeout=10)
    out, cur = {}, None
    for line in p.stdout.splitlines():
        if line[:1] == "p" and line[1:].isdigit():
            cur = int(line[1:])
            out[cur] = ""  # listed, but maybe no path (its folder was deleted): seen, not lost
        elif line[:1] == "n" and cur is not None:
            out[cur] = line[1:]
    return out


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # exited mid-scan: nothing to find
    except OSError:
        return True
    return True


def _tmux_roots():
    """{pane pid: its folder} for every pane on greenlight's tmux server: what descends from one, in that folder, is ours."""
    sock = os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"
    try:
        p = subprocess.run([bins.find("tmux") or "tmux", "-L", sock, "list-panes", "-a", "-F", "#{pane_pid}\t#{pane_current_path}"],
                           capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return {}  # no tmux: nothing of ours runs
    if p.returncode and not re.search(r"no server running|error connecting|No such file", p.stderr):
        raise OSError(f"tmux list-panes exited {p.returncode}")  # can't tell ours from theirs: the scan failed
    return {int(k): v for k, _, v in (x.partition("\t") for x in p.stdout.splitlines()) if k.isdigit()}


def scan(procs=None, roots=None, cwd_of=None):
    """[{pid, agent, cwd, start[, pane]}] for every agent process (start: when it began, epoch s). One under a Greenlight
    pane carries that pane's folder as `pane`: by_worktree, the one way callers read this, keeps it only when it works in
    another worktree than its pane's. An
    agent another agent started counts on its own only in another folder (a claude's own children aren't another
    agent; a codex it runs in another worktree is). procs/roots/cwd_of: injected by tests."""
    mac = platform.system() == "Darwin"
    if procs is None:
        procs = _proc_mac() if mac else _proc_linux()
    roots = _tmux_roots() if roots is None else roots
    roots = roots if isinstance(roots, dict) else dict.fromkeys(roots)  # tests: a set, no folders

    def root_of(pid):
        seen = set()
        while pid and pid not in seen and pid in procs:
            if pid in roots:
                return pid
            seen.add(pid)
            pid = procs[pid]["ppid"]
        return pid if pid in roots else None

    # another user's agents: their cwd is closed to us (lsof, /proc), a known limit; they still count as parents
    found = {pid: a for pid, p in procs.items() if not p.get("other") and (a := agent_of(p["argv"]))}
    if cwd_of is None:
        cwds = _cwds_mac(list(found)) if mac else {pid: _cwd_linux(pid) for pid in found}
        cwd_of = cwds.get

    # Under a Greenlight pane, an agent of any kind (Greenlight launches opencode, gemini and pi too) carries its pane's
    # folder as `pane`: by_worktree drops it only when it works in the pane's own worktree; one working in another
    # (`cd ../B && codex`, a nested worktree) counts there. (A pane's opencode, gemini or pi has no pane state in
    # ade_basement.agent_type: the native engine's own limit, not passive coexistence's.)
    rooted = {pid: r for pid in found if (r := root_of(pid)) is not None}
    pane = {pid: roots[r] for pid, r in rooted.items() if roots[r]}
    every = dict(found)  # for the child-of-its-parent check below, before a pane of unknown folder drops its own
    found = {pid: k for pid, k in found.items() if not (pid in rooted and not roots[rooted[pid]])}  # a pane of unknown folder: ours
    top = {pid for pid in pane if procs[pid]["ppid"] == rooted[pid]}  # the pane's own agent: in the pane's folder
    if mac and (lost := [p for p in found if p not in top and cwd_of(p) is None and _alive(p)]):
        raise OSError(f"no cwd for {len(lost)} live agent(s)")  # lsof said nothing about a live agent that may be elsewhere
    out = []
    for pid in sorted(found):
        c, parent = cwd_of(pid), procs[pid]["ppid"]
        if c and not (parent in every and cwd_of(parent) == c):  # a child in its parent's folder is the parent's own
            out.append({"pid": pid, "agent": found[pid], "cwd": c, "start": procs[pid].get("start"),
                        **({"pane": pane[pid]} if pid in pane else {})})

    return out


def _diag_failed(e):
    """A failed scan holds every worktree: say why in the trail (diag.py errors), once per failure."""
    try:
        import diag
        diag.event("external", op="scan", err=type(e).__name__, msg_class=diag.classify(str(e)))
    except Exception:  # noqa: BLE001 - the trail must never break the scan's fallback
        pass


class Unknown(RuntimeError):
    """The process table couldn't be read: whether an agent runs anywhere is unknown."""


def agents(fresh=False, strict=False):
    """scan(), cached for TTL seconds. When the process table can't be read: [] (listing and dispatch carry on), or,
    strict, Unknown (cleanup: a deletion must not go ahead on a scan that failed)."""
    with _LOCK:
        now = time.time()
        if not fresh and _CACHE["val"] is not None and now - _CACHE["at"] < TTL:
            return _CACHE["val"]
        if now - _CACHE["failed"] < FAIL_BACKOFF:  # it just failed: don't pile on another 10 s ps/lsof per caller
            if strict:
                raise Unknown("recent scan failed")
            return []
        try:
            val = scan()
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            _CACHE.update(at=0.0, val=None, failed=time.time())  # never cached as "none"
            _diag_failed(e)
            if strict:
                raise Unknown(f"{type(e).__name__}") from e
            return []
        _CACHE.update(at=time.time(), val=val, failed=0.0)
        return val


def by_worktree_safe(paths):
    """by_worktree, failing closed: when the process table can't be read, every path counts as having an agent of
    unknown kind (a dispatch waits a tick rather than land in a live session)."""
    try:
        return by_worktree(paths, agents(strict=True))
    except Unknown:
        return {p.rstrip("/"): [{"pid": None, "agent": None, "cwd": p, "start": None}] for p in paths if p}


@functools.lru_cache(maxsize=4096)
def real(p):
    return os.path.realpath(p)  # cleanup asks once per candidate, over every worktree: resolve each path once


def by_worktree(paths, found=None):
    """{worktree path: [agent]} for the agents whose cwd is inside one of paths (the deepest one wins: a nested
    worktree owns its own agents)."""
    given = {real(p).rstrip("/") or "/": p.rstrip("/") for p in paths if p}  # an ADE's path may go through a symlink
    deep = sorted(given, key=len, reverse=True)
    out: dict = {}
    owner_of = lambda c: next((w for w in deep if c == w or c.startswith(w + "/")), None)  # noqa: E731
    for a in agents() if found is None else found:
        owner = owner_of(real(a["cwd"]))
        if owner and not (a.get("pane") and owner_of(real(a["pane"])) == owner):  # ours, in its own pane's worktree
            out.setdefault(given[owner], []).append(a)
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import diag
    diag.install("external")
    def own(r):  # in its pane's folder: Greenlight's own (by_worktree decides it exactly, against the worktree list)
        return "pane" in r and (r["cwd"] == r["pane"] or r["cwd"].startswith(r["pane"].rstrip("/") + "/"))
    rows = [r for r in agents(fresh=True) if "--all" in sys.argv or not own(r)]  # --all: Greenlight's own pane agents too
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            print(f"{r['pid']:>7}  {r['agent']:9} {r['cwd']}")
