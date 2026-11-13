#!/usr/bin/env python3
"""Dev copies: greenlight-<workspace>.app on your Mac for every worktree of this repo, kept live as the worktree changes.

  dev.py install <host> <repo>   on the Mac, once: two launch agents (this script's `run`, and one ssh tunnel to <host>).
                                 <host>: an ssh host, or "local". <repo>: a checkout of this repo there (any worktree of it)
  dev.py log [--wait [secs]]     in a worktree: what the Mac made of its app sources as they are now (built, live, or the
                                 compiler's errors). --wait: until it has an answer for them (default 180s). Exit 0 ok, 1 failed, 2 no answer
  dev.py status | uninstall      on the Mac

A page (dashboard.html, terminal.html, their scripts) reloads in the running copy within a second of a save. A change to
the Swift or build.sh rebuilds the copy (unoptimized, ~15s) and relaunches it if it was open. A copy opens by itself once,
the first time its branch changes the app; after that it's yours to open and quit. Its Python is the worktree's own
(greenlightCode), its state the real one (greenlightHome), its settings a copy of Greenlight's from its first build.
A branch from before dev copies (its build.sh only knows Greenlight.app) gets none until it merges main.

Nothing a copy does opens a network connection in its name: its ssh rides the tunnel's socket. So a firewall (Little
Snitch) sees one process, launchd's /usr/bin/ssh, whatever the workspace and however often it's rebuilt.

How: `dev.py watch` runs on the host through that tunnel and streams each worktree's app sources as they change (JSON
lines). `dev.py run` mirrors them under ~/.greenlight-dev, builds with the worktree's own build.sh, and writes the result
into the worktree's git dir for `dev.py log`. It replaces itself when the repo's dev.py changes (main's, or <repo>'s
while that worktree lasts). Stdlib only, and Python 3.9: the Mac runs it on the Command Line Tools' python3.
"""
import base64, hashlib, json, os, plistlib, re, shlex, shutil, signal, subprocess, sys, threading, time, traceback
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")  # a workspace: it names an app, a folder and a defaults domain
SRC = ("app", "brand/icons")  # what build.sh reads
MARK = "GREENLIGHT_DEV"  # a build.sh that knows this builds a copy; an older one would build over Greenlight.app
REPORT = "greenlight-dev.json"  # in the worktree's git dir: {sig, ok, what, secs, at, errors}
sha1 = lambda b: hashlib.sha1(b).hexdigest()
sig = lambda files: sha1("".join(f"{r}\0{h}\n" for r, h in sorted(files.items())).encode())[:16]  # of a {path: sha1}


def git(tree, *a):
    r = subprocess.run(["git", "-C", tree, *a], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


class Tree:
    """A folder's app sources, hashed once per change (by size and mtime)."""
    def __init__(self, path):
        self.path, self.stat, self.sha, self.gitdir = str(path), {}, {}, None

    def scan(self):
        """{relative path: sha1}."""
        seen = {}
        for top in SRC:
            for d, dirs, names in os.walk(os.path.join(self.path, top)):
                dirs[:] = [x for x in dirs if not x.startswith(".") and x != "__pycache__"]
                for n in names:
                    p = os.path.join(d, n)
                    rel = os.path.relpath(p, self.path)
                    try:
                        st = os.stat(p)
                        if n.startswith(".") or not os.path.isfile(p) or st.st_size > 64 << 20:
                            continue
                        if self.stat.get(rel) != (st.st_size, st.st_mtime_ns):
                            self.sha[rel], self.stat[rel] = sha1(Path(p).read_bytes()), (st.st_size, st.st_mtime_ns)
                        seen[rel] = self.sha[rel]
                    except OSError:
                        continue  # saved over while we looked: the next pass has it
        self.stat = {r: self.stat[r] for r in seen}
        return seen


# ---- on the host: the worktrees and their sources

def worktrees(repo):
    """(the main checkout, [the other worktrees' paths], main's HEAD), or None when git fails: never read that as "no worktrees"."""
    r = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True)
    blocks = [dict(l.partition(" ")[::2] for l in b.splitlines()) for b in r.stdout.strip().split("\n\n")]
    if r.returncode or not blocks or "worktree" not in blocks[0]:
        return None
    return blocks[0]["worktree"], [b["worktree"] for b in blocks[1:] if "worktree" in b and "bare" not in b and "prunable" not in b
                                   and os.path.isdir(b["worktree"])], blocks[0].get("HEAD", "")


def changed(tree, main_head):
    """Has this branch changed the app since it left main? Uncommitted edits count."""
    base = git(tree, "merge-base", "HEAD", main_head) if main_head else ""
    return bool(base) and subprocess.run(["git", "-C", tree, "diff", "--quiet", base, "--", *SRC]).returncode == 1


def watch(repo, tool="", have=None, out=sys.stdout, period=0.1, passes=None):
    """Streams events as JSON lines until the reader goes away:
      trees {names}                                    the worktrees there are, first and whenever that changes
      ws    {name, path, git, sig, ok, ui, put, rm}    a worktree's sources: once at the start, then on each change that has
                                                       sat still for a pass. put {path: base64}, rm [path]: against what the reader has
      tool  {path, sha}                                the dev.py the reader should be running
      tick                                             every 5s, so a dead connection ends us"""
    def emit(**ev):
        out.write(json.dumps(ev) + "\n")
        out.flush()
    trees, sent, prev, told = {}, dict(have or {}), {}, set()
    names = toolsha = None
    main_head, listed, ticked, n = "", -1e9, time.monotonic(), 0
    while passes is None or n < passes:
        n += 1
        now = time.monotonic()
        if now - listed >= 2 or passes is not None:
            wt, listed = worktrees(repo), now
            if wt:
                main, paths, main_head = wt
                cur = {os.path.basename(p): p for p in paths if NAME.match(os.path.basename(p))}
                trees = {k: trees[k] if k in trees and trees[k].path == p else Tree(p) for k, p in cur.items()}
                if sorted(cur) != names:
                    names = sorted(cur)
                    emit(ev="trees", names=names)
                f = next((f for f in (os.path.join(d, "scripts", "dev.py") for d in (tool, main) if d) if os.path.isfile(f)), None)
                sha = sha1(Path(f).read_bytes()) if f else None
                if sha and sha != toolsha:
                    emit(ev="tool", path=f, sha=sha)
                toolsha = sha
        for k, t in trees.items():
            files = t.scan()
            s, was = sig(files), sent.get(k, {})
            if (s != sig(was) or k not in told) and s == prev.get(k):
                put = {}
                for r in files:
                    if files[r] != was.get(r):
                        try:
                            put[r] = base64.b64encode(Path(t.path, r).read_bytes()).decode()
                        except OSError:
                            pass  # gone again: the next pass says so
                t.gitdir = t.gitdir or git(t.path, "rev-parse", "--absolute-git-dir")
                try:
                    ok = MARK in Path(t.path, "app", "build.sh").read_text()
                except OSError:
                    ok = False
                emit(ev="ws", name=k, path=t.path, git=t.gitdir, sig=s, ok=ok, ui=changed(t.path, main_head), put=put, rm=sorted(set(was) - set(files)))
                sent[k] = files
                told.add(k)
            prev[k] = s
        if now - ticked >= 5:
            ticked = now
            emit(ev="tick")
        if passes is None or n < passes:
            time.sleep(period)


def log(wait):
    """The Mac's answer for this worktree's sources as they are now."""
    top, gd = git(".", "rev-parse", "--show-toplevel"), git(".", "rev-parse", "--absolute-git-dir")
    if not top:
        print("Not in a git worktree.")
        return 2
    t, end = Tree(top), time.time() + wait
    while True:
        s = sig(t.scan())
        try:
            r = json.loads(Path(gd, REPORT).read_text())
        except (OSError, ValueError):
            r = {}
        if r.get("sig") == s or time.time() >= end:
            break
        time.sleep(0.5)
    if r.get("sig") != s:
        print("No answer from the Mac for these sources" + (f" (its last, {time.strftime('%H:%M:%S', time.localtime(r['at']))}, was for older ones)" if r else "")
              + ". Is it awake, with dev copies installed (scripts/dev.py install)?")
        return 2
    at = time.strftime("%H:%M:%S", time.localtime(r.get("at", 0)))
    if r.get("ok"):
        print({"built": f"ok: built in {r.get('secs', 0)}s", "live": "ok: nothing to build, the pages are live"}.get(r.get("what"), "ok") + f" ({at})")
        return 0
    print(f"FAILED ({at}): " + (r.get("errors") or r.get("what") or ""))
    return 1


# ---- on the Mac

HOME = Path(os.environ.get("GREENLIGHT_DEV_HOME", "~/.greenlight-dev")).expanduser()
ME = HOME / "dev.py"
APPS, AGENTS, SUPPORT = (Path(p).expanduser() for p in ("~/Applications", "~/Library/LaunchAgents", "~/Library/Application Support"))
LABEL, TUNNEL, PROD = "dev.greenlight.dev", "dev.greenlight.dev.tunnel", "dev.greenlight.app"
LOCK = threading.Lock()  # the mirrors: written by the watch's thread, snapshotted by the builder's
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


def say(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def conf():
    return json.loads((HOME / "conf.json").read_text())


def local(c):
    return c["host"] in ("", "local")


def sock(c):
    return HOME / "ssh" / (c["host"] + ".sock")


def on_host(c, cmd):
    """The argv for a shell command where the worktrees are. Over ssh it rides the tunnel's socket: no connection of our own."""
    if local(c):
        return ["/bin/sh", "-c", cmd]
    return ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ControlPath=none", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-o", f"ProxyCommand=/usr/bin/nc -U '{sock(c)}'", c["host"], cmd]


class Copy:
    """One workspace's copy: ~/Applications/greenlight-<name>.app and its folder here (src: the mirror, web: its live pages)."""
    def __init__(self, name):
        assert NAME.match(name), name
        self.name, self.title = name, "greenlight-" + name
        self.dir, self.app = HOME / "ws" / name, APPS / (self.title + ".app")
        self.src, self.web = self.dir / "src", self.dir / "web"

    def bundle_id(self):
        try:
            return plistlib.loads((self.app / "Contents/Info.plist").read_bytes())["CFBundleIdentifier"]
        except (OSError, KeyError, ValueError):
            return ""

    def pids(self):
        ps = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [int(l.split(None, 1)[0]) for l in ps.splitlines() if l.split(None, 1)[1:] and l.split(None, 1)[1].startswith(str(self.app) + "/Contents/MacOS/")]

    def stop(self):
        pids = self.pids()
        for s in (signal.SIGTERM, signal.SIGKILL):
            for p in pids:
                try:
                    os.kill(p, s)
                except OSError:
                    pass
            end = time.time() + 5
            while self.pids() and time.time() < end:
                time.sleep(0.1)
            if not self.pids():
                break
        return bool(pids)

    def front(self):
        asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
        return bool(asn and self.bundle_id()) and f'"{self.bundle_id()}"' in subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "bundleid", asn], capture_output=True, text=True).stdout

    def open(self, front=False):
        """-g: behind what you're doing. -n: a relaunch can come before LaunchServices has noticed the old one is gone."""
        for _ in range(3):
            r = subprocess.run(["/usr/bin/open", "-n"] + ([] if front else ["-g"]) + [str(self.app)], capture_output=True, text=True)
            end = time.time() + 5
            while not self.pids() and time.time() < end:
                time.sleep(0.1)
            if self.pids():
                return
            say(f"{self.title}: open didn't start it (exit {r.returncode}) {r.stderr.strip()}")


def write(p, data):
    """Whole or not at all: a copy's page watcher never loads half a file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def mirror(k, ev):
    """Applies a ws event to the copy's mirror."""
    for rel in list(ev.get("put", {})) + list(ev.get("rm", [])):
        if os.path.isabs(rel) or ".." in Path(rel).parts or not any(rel.startswith(t + "/") for t in SRC):
            raise ValueError(f"not an app source: {rel!r}")
    for rel, b in ev.get("put", {}).items():
        write(k.src / rel, base64.b64decode(b))
    for rel in ev.get("rm", []):
        (k.src / rel).unlink(missing_ok=True)


def live(k):
    """The names of the copy's live pages: what its bundle ships, icons aside."""
    res = k.app / "Contents/Resources"
    return sorted(p.name for p in res.iterdir() if p.suffix != ".icns") if res.is_dir() else []


def lay_web(k):
    """The live pages as the mirror has them now, flat like the bundle's Resources. The copy reloads what changes here."""
    names = live(k)
    byname = {p.name: p for p in (k.src / "app").rglob("*") if p.is_file() and p.name in names}
    for n, p in byname.items():
        data = p.read_bytes()
        if not (k.web / n).is_file() or (k.web / n).read_bytes() != data:
            write(k.web / n, data)


def build_key(k, root):
    """What a build bakes in: the sources under root, minus the pages the copy loads live."""
    return sig({r: h for r, h in Tree(root).scan().items() if os.path.basename(r) not in live(k)})


def seed(c, k, ev):
    """A new copy starts as Greenlight is now: its settings and projects, on the same host, running the worktree's Python.
    Not its terminal layout: closing a tab kills that tab's tmux session, and those are Greenlight's."""
    bid = k.bundle_id()
    exp = subprocess.run(["/usr/bin/defaults", "export", PROD, "-"], capture_output=True)
    if exp.returncode == 0 and exp.stdout:
        subprocess.run(["/usr/bin/defaults", "import", bid, "-"], input=exp.stdout, capture_output=True)
    for key, v in (("host", "local" if local(c) else c["host"]), ("greenlightCode", ev["path"])):
        subprocess.run(["/usr/bin/defaults", "write", bid, key, v], capture_output=True)
    for f in ("projects.json", "transcript.json", "questions.json"):
        if (SUPPORT / "Greenlight" / f).is_file():
            write(SUPPORT / k.title / f, (SUPPORT / "Greenlight" / f).read_bytes())
    (k.dir / "id").write_text(bid)


def drop(k):
    """The workspace is gone: so are its copy, its settings and its folders."""
    k.stop()
    bid = (k.dir / "id").read_text().strip() if (k.dir / "id").is_file() else k.bundle_id()
    if k.app.is_dir():
        subprocess.run([LSREGISTER, "-u", str(k.app)], capture_output=True)
        shutil.rmtree(k.app, ignore_errors=True)
    if re.match(re.escape(PROD) + r"\.[A-Za-z0-9-]+$", bid):  # a copy's id, never Greenlight's own
        subprocess.run(["/usr/bin/defaults", "delete", bid], capture_output=True)
        for d in (f"Preferences/{bid}.plist", f"WebKit/{bid}", f"Caches/{bid}", f"HTTPStorages/{bid}", f"Saved Application State/{bid}.savedState"):
            f = SUPPORT.parent / d
            shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
    shutil.rmtree(SUPPORT / k.title, ignore_errors=True)
    shutil.rmtree(k.dir, ignore_errors=True)
    say(f"{k.title}: removed")


def errors(out):
    """The compiler's errors (path:line:col: error: …), else the end of what it printed."""
    e = [re.sub(r"^(?=[\w.-]+\.swift:\d)", "app/", l) for l in out.splitlines() if "error:" in l and not l.lstrip().startswith(("|", "`"))]  # build.sh compiles in app/
    return "\n".join(dict.fromkeys(e)) if e else "\n".join(out.splitlines()[-30:])


def report(c, ev, **r):
    """Into the worktree's git dir (never its status), for `dev.py log` there."""
    if not ev.get("git"):
        return
    r.update(sig=ev["sig"], at=int(time.time()))
    subprocess.run(on_host(c, "cat > " + shlex.quote(ev["git"] + "/" + REPORT)), input=json.dumps(r).encode(), capture_output=True, timeout=60)


def banner(title, text):
    """Best effort (Script Editor's banner, no permission asked)."""
    subprocess.run(["/usr/bin/osascript", "-e", f"display notification {json.dumps(text[:200], ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"], capture_output=True)


def make(c, k, ev):
    """Builds the copy if what a build bakes in has moved, relaunches it if it was open, and answers the worktree."""
    snap = k.dir / "build"  # the build reads a snapshot: the mirror keeps moving under a 15s compile
    with LOCK:
        shutil.rmtree(snap, ignore_errors=True)
        shutil.copytree(k.src, snap)
    read = lambda f: (k.dir / f).read_text() if (k.dir / f).is_file() else ""
    key, failed = build_key(k, snap), read("failed").partition("\n")
    if key == read("built") and k.app.is_dir():
        (k.dir / "failed").unlink(missing_ok=True)
        report(c, ev, ok=True, what="live")
    elif key == failed[0]:  # the same sources that didn't compile: a page saved meanwhile is live all the same
        return report(c, ev, ok=False, what="build", errors=failed[2])
    else:
        t = time.time()
        r = subprocess.run(["/bin/bash", str(snap / "app/build.sh")], env={**os.environ, "GREENLIGHT_APP": k.title, "GREENLIGHT_DEV": str(k.dir)},
                           capture_output=True, text=True, timeout=900)
        secs = round(time.time() - t)
        if r.returncode:
            err = errors(r.stdout + r.stderr)
            say(f"{k.title}: build failed\n{err}")
            if not failed[0]:  # one banner per broken stretch, not per save
                banner(k.title, "Build failed: " + err.splitlines()[0] if err else "Build failed")
            (k.dir / "failed").write_text(key + "\n" + err)
            return report(c, ev, ok=False, what="build", secs=secs, errors=err)
        (k.dir / "failed").unlink(missing_ok=True)
        if not (k.dir / "id").is_file():
            seed(c, k, ev)
        with LOCK:
            lay_web(k)
        (k.dir / "built").write_text(build_key(k, snap))  # again: the first build is what says which pages are live
        front = k.front()
        if k.stop():
            k.open(front)
        say(f"{k.title}: built in {secs}s")
        report(c, ev, ok=True, what="built", secs=secs)
    if ev.get("ui") and not (k.dir / "opened").exists():  # once: the first time its branch has changed the app
        (k.dir / "opened").touch()
        if not k.pids():
            k.open()


def fetch_tool(c, ev):
    """The repo's dev.py over ours, if it's a different one that at least parses. True: restart into it."""
    if not ME.is_file() or sha1(ME.read_bytes()) == ev["sha"]:
        return False
    r = subprocess.run(on_host(c, "cat " + shlex.quote(ev["path"])), capture_output=True, timeout=60)
    if r.returncode or sha1(r.stdout) != ev["sha"]:
        return False
    try:
        compile(r.stdout, "dev.py", "exec")
    except (SyntaxError, ValueError) as e:
        say(f"not taking the repo's dev.py: {e}")
        return False
    write(ME, r.stdout)
    return True


def run():
    """The launch agent: follow the host's watch, forever."""
    c = conf()
    (HOME / "ws").mkdir(parents=True, exist_ok=True)
    if (HOME / "log").is_file() and (HOME / "log").stat().st_size > 4 << 20:
        os.truncate(HOME / "log", 0)
    pending, cv, busy, skipped = {}, threading.Condition(), threading.Lock(), set()

    def builder():
        while True:
            with cv:
                while not pending:
                    cv.wait()
                name, ev = pending.popitem()
            with busy:
                try:
                    make(c, Copy(name), ev)
                except Exception:
                    say(f"greenlight-{name}:\n{traceback.format_exc()}")
    threading.Thread(target=builder, daemon=True).start()

    def handle(ev):
        if ev["ev"] == "trees":
            for d in (HOME / "ws").iterdir():
                if d.name not in ev["names"] and NAME.match(d.name):
                    with busy:
                        pending.pop(d.name, None)
                        drop(Copy(d.name))
        elif ev["ev"] == "ws" and NAME.match(ev["name"]):
            if not ev["ok"]:
                if ev["name"] not in skipped:
                    say(f"greenlight-{ev['name']}: no copy, its app/build.sh is from before dev copies (merge main)")
                skipped.add(ev["name"])
                return report(c, ev, ok=False, what="No dev copy: this branch's app/build.sh is from before them. Merge main.")
            k = Copy(ev["name"])
            with LOCK:
                mirror(k, ev)
                lay_web(k)  # the pages are live from here, ahead of any build
            with cv:
                pending[ev["name"]] = ev
                cv.notify()
        return ev["ev"] == "tool" and fetch_tool(c, ev)

    down = False
    while True:
        have = {d.name: Tree(d / "src").scan() for d in (HOME / "ws").iterdir() if (d / "src").is_dir()}
        cmd = " ".join(["python3", "-", "watch", shlex.quote(c["repo"]), shlex.quote(c.get("tool", "")), base64.b64encode(json.dumps(have).encode()).decode()])
        with open(ME, "rb") as me:
            p = subprocess.Popen(on_host(c, cmd), stdin=me, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        restart = False
        for line in p.stdout:
            if down:
                say("connected")
            down = False
            try:
                restart = handle(json.loads(line))
            except Exception:
                say(traceback.format_exc())
            if restart:
                break
        p.kill()
        p.wait()
        if restart:
            with busy:  # not mid-build
                say("new dev.py: restarting")
                os.execv(sys.executable, [sys.executable, str(ME), "run"])
        if not down:
            say(f"no watch on {c['host']}: retrying")
        down = True
        time.sleep(3)


AGENT = {"RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5, "ProcessType": "Interactive"}  # Interactive: a build at full speed, not a background job's


def plists(c):
    """{label: plist}. The tunnel is launchd's own /usr/bin/ssh, a socket here forwarded to the host's sshd: the one process
    of all this that opens a connection, so the one a firewall asks about, once."""
    out = {LABEL: dict(AGENT, Label=LABEL, ProgramArguments=["/usr/bin/python3", str(ME), "run"], StandardOutPath=str(HOME / "log"), StandardErrorPath=str(HOME / "log"))}
    if not local(c):
        out[TUNNEL] = dict(AGENT, Label=TUNNEL, StandardErrorPath=str(HOME / "tunnel.log"), ProgramArguments=[
            "/usr/bin/ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "StreamLocalBindUnlink=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-L", f"{sock(c)}:{c['sshd']}", c["host"]])
    return out


def unload(label):
    subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    (AGENTS / (label + ".plist")).unlink(missing_ok=True)


def install(host, repo):
    if sys.platform != "darwin":
        sys.exit("dev.py install runs on the Mac the copies are for.")
    if not Path("/Library/Developer/CommandLineTools/usr/bin/swiftc").exists():
        sys.exit("No Command Line Tools swiftc: xcode-select --install")
    if not re.match(r"^[\w.-]+$", host) or not re.match(r"^[~\w./@+-]+$", repo):
        sys.exit("usage: dev.py install <ssh host|local> <repo path there>")
    c = {"host": host}
    direct = (lambda cmd: ["/bin/sh", "-c", cmd]) if local(c) else (lambda cmd: ["/usr/bin/ssh", "-o", "BatchMode=yes", host, cmd])  # this once, from your terminal
    r = subprocess.run(direct(f"cd {repo} && git rev-parse --show-toplevel && git worktree list --porcelain | head -1 && cat scripts/dev.py"), capture_output=True)
    lines = r.stdout.split(b"\n", 2)
    if r.returncode or len(lines) < 3 or not lines[1].startswith(b"worktree "):
        sys.exit(f"No checkout with scripts/dev.py at {host}:{repo}\n{r.stderr.decode(errors='replace')}")
    c.update(tool=lines[0].decode(), repo=lines[1].decode()[9:])
    if not local(c):
        port = re.search(r"^port (\d+)$", subprocess.run(["/usr/bin/ssh", "-G", host], capture_output=True, text=True).stdout, re.M)
        c["sshd"] = "localhost:" + (port[1] if port else "22")  # the host's own sshd, as the far end of the tunnel sees it
    if len(str(sock(c))) > 100:
        sys.exit(f"{sock(c)} is too long a path for a socket: give the host a shorter name in ~/.ssh/config.")
    for d in (HOME / "ssh", HOME / "ws", AGENTS, APPS):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    write(ME, lines[2])
    (HOME / "conf.json").write_text(json.dumps(c, indent=1))
    for label in (LABEL, TUNNEL):
        unload(label)
    for label, pl in plists(c).items():
        f = AGENTS / (label + ".plist")
        f.write_bytes(plistlib.dumps(pl))
        for _ in range(10):  # a bootout can still be settling
            if subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(f)], capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        else:
            sys.exit(f"launchctl wouldn't load {f}")
    print(f"Dev copies on: greenlight-<workspace>.app for each worktree of {host}:{c['repo']}, in {APPS}. Log: {HOME}/log")


def uninstall():
    for label in (LABEL, TUNNEL):
        unload(label)
    for d in (HOME / "ws").iterdir() if (HOME / "ws").is_dir() else []:
        if NAME.match(d.name):
            drop(Copy(d.name))
    shutil.rmtree(HOME, ignore_errors=True)
    print("Dev copies off: the launch agents, the copies and their settings are gone.")


def status():
    c = conf()
    print(f"host {c['host']}  repo {c['repo']}  tool {c.get('tool', '')}")
    for label in plists(c):
        on = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
        print(f"{label}: " + (re.search(r"state = (\w+)", on.stdout) or [0, "not loaded"])[1])
    if not local(c):
        print("tunnel: " + ("up" if subprocess.run(on_host(c, "true"), capture_output=True, timeout=30).returncode == 0 else "down (see tunnel.log)"))
    for d in sorted((HOME / "ws").iterdir()):
        k = Copy(d.name)
        print(f"{k.title}: " + ("no build yet" if not k.app.is_dir() else "build failed (dev.py log, in its worktree)" if (d / "failed").exists() else "running" if k.pids() else "built"))


def main(a):
    if a[:1] == ["watch"] and len(a) >= 2:
        try:
            watch(a[1], a[2] if len(a) > 2 else "", json.loads(base64.b64decode(a[3])) if len(a) > 3 else None)
        except (BrokenPipeError, KeyboardInterrupt):
            os._exit(0)  # the reader left
    elif a[:1] == ["log"]:
        sys.exit(log(float(a[2]) if a[1:2] == ["--wait"] and len(a) > 2 else 180 if a[1:2] == ["--wait"] else 0))
    elif a[:1] == ["install"] and len(a) == 3:
        install(a[1], a[2])
    elif a in (["run"], ["status"], ["uninstall"]):
        {"run": run, "status": status, "uninstall": uninstall}[a[0]]()
    else:
        sys.exit(__doc__.split("\n\nA page")[0])


if __name__ == "__main__":
    main(sys.argv[1:])
