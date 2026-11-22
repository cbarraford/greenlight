#!/usr/bin/env python3
"""Greenlight's own automations: Orca-style scheduled agent runs, no Orca needed.

  auto.py list
  auto.py add <name> --schedule "<cron>" --prompt "<text>" [--cwd DIR] [--precheck CMD]
              [--provider claude|codex|opencode|pi|gemini] [--model M] [--effort E] [--timeout MIN]
  auto.py add <name> --schedule "<cron>" --orchestrator [--precheck CMD] [--timeout MIN]
                            # a greenlight run with no LLM session: orchestrate.py tick (adopt, scan, next.cmd,
                            # goal backfill, templated dispatch, cleanup; decide.py only at ambiguous forks)
  auto.py edit <name> --orchestrator | --agentic   # switch an automation between the two
  auto.py edit <name> [any add option] [--enabled | --disabled]
  auto.py rm <name>
  auto.py show <name>       # its JSON
  auto.py run <name>        # now, in the background, skipping the schedule (not the precheck)
  auto.py runs [name]       # last 20 runs
  auto.py tick              # the timer calls this every minute
  auto.py install           # writes and starts the timer: a systemd user timer, or a launchd agent on macOS

Automations live in ~/.greenlight/automations.json, runs in ~/.greenlight/auto/.
Schedules are 5-field cron in the machine's local time (*, */n, a-b, a,b; Sunday=0).
A precheck that exits non-zero means "nothing to do": the run is skipped.
The provider (default: config agents[0]) runs first; on a usage limit the same prompt goes to the next of config agents.
Model and effort apply to the provider only. Permissions follow harness.py: safe unless that harness has bypass set.
An --orchestrator automation ("kind": "orchestrator") runs orchestrate.py tick instead of an agent; its prompt is optional.
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, str(Path(__file__).parent))
import diag  # noqa: E402
import harness  # noqa: E402
from greenlight import CONFIG, LIMIT_RE, STATE, ping  # noqa: E402

FILE = STATE / "automations.json"
DIR = STATE / "auto"
OPTS = ("schedule", "prompt", "cwd", "precheck", "provider", "model", "effort", "timeout")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}\Z")


def valid_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid automation name: {name!r}")
    return name


def load():
    return json.loads(FILE.read_text()) if FILE.exists() else {}


def save(autos):
    STATE.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(autos, indent=2))
    tmp.replace(FILE)


def field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        if not part:
            raise ValueError(f"empty cron field part in {spec!r}")
        rng, slash, step_s = part.partition("/")
        step = int(step_s) if slash else 1
        if step < 1:
            raise ValueError(f"invalid cron step in {spec!r}")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            bits = rng.split("-")
            if len(bits) != 2:
                raise ValueError(f"invalid cron range in {spec!r}")
            a, b = int(bits[0]), int(bits[1])
        else:
            a = int(rng)
            b = hi if slash else a
        if not (lo <= a <= b <= hi):
            raise ValueError(f"cron value out of bounds in {spec!r}")
        out |= set(range(a, b + 1, step))
    return out


def due(cron, t):
    """Does a 5-field cron match struct_time t (to the minute)?"""
    m, h, dom, mon, dow = cron.split()
    dow_vals = field(dow, 0, 7)
    if 7 in dow_vals:
        dow_vals.add(0)
    ok_dom, ok_dow = t.tm_mday in field(dom, 1, 31), (t.tm_wday + 1) % 7 in dow_vals
    day = (ok_dom and ok_dow) if "*" in (dom, dow) else (ok_dom or ok_dow)  # cron: both restricted → either matches
    return t.tm_min in field(m, 0, 59) and t.tm_hour in field(h, 0, 23) and t.tm_mon in field(mon, 1, 12) and day


def alive(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return False
    f = DIR / f"{name}.pid"
    try:
        pid = int(f.read_text())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def provider(a):
    return a.get("provider") or CONFIG["agents"][0]


def agent(a, engine, log):
    """(text, exit code, tokens): the run's step titles, plain lines and reply, and its diag token fields."""
    own = engine == provider(a)  # a model name only means something to its own harness
    try:  # stream JSON, as chat does: the same parsers then give the reply and its tokens
        cmd, text = harness.argv(engine, a["prompt"], model=own and a.get("model"), effort=own and a.get("effort"), stream=True)
        env = {**os.environ, "GREENLIGHT_REQUEST": a["prompt"]}  # a read-only automation can't create workspaces (greenlight.read_only)
        p = subprocess.run(cmd, input=text, cwd=os.path.expanduser(a.get("cwd") or "~"), text=True, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=int(a.get("timeout") or 120) * 60)
        out, code = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or b"").decode(errors="replace") + "\n[timed out]", 124
    except (ValueError, OSError) as e:  # an unusable harness: unknown, pi without bypass, binary missing
        out, code = str(e), 127
    out, tokens = reply(engine, out)
    log.write(f"$ {engine}\n{out}\n[exit {code}]\n")
    return out, code, tokens


def reply(engine, raw):
    """A stream-JSON run's output → (readable text, token fields). Non-JSON lines (stderr: auth, limits) stay as they are.
    Never raises: a parser that chokes leaves the raw output and no tokens."""
    try:
        import chat  # here, not at the top: chat imports auto
        norm, lines, final = chat.Norm(), [], None
    except Exception:
        return raw, {}
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            d = None
        if not isinstance(d, dict):
            lines.append(line)
            continue
        try:
            for e in getattr(norm, engine)(d):
                if e.get("e") == "step":
                    lines.append(f"› {e.get('title', '')}")
                elif e.get("e") == "done" and final is None:  # the first, as chat.run(): codex's turn.failed repeats an error as a dict
                    final = e.get("text")
        except Exception:
            continue
    return "\n".join([*lines, final or norm.text or ""]).strip(), {k: v for k, v in chat.tokens(norm).items() if v is not None}


def orchestrator(a, log):
    """orchestrate.py tick, logged like an agent run."""
    nonce = uuid.uuid4().hex
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "orchestrate.py"), "tick"], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                         env={**os.environ, "GREENLIGHT_TICK_NONCE": nonce})
    try:
        out, _ = p.communicate(timeout=int(a.get("timeout") or 30) * 60)
        code = p.returncode
    except subprocess.TimeoutExpired:
        kill_process_tree(p.pid, nonce=nonce)
        try:
            tail, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired as drain:
            if p.poll() is None:
                p.kill()
            tail = drain.output or ""  # a surviving orphan must not hold the scheduler indefinitely
            if isinstance(tail, bytes):
                tail = tail.decode(errors="replace")
        out, code = (tail or "") + "\n[timed out]", 124
    log.write(f"$ orchestrate.py tick\n{out}\n[exit {code}]\n")
    return out, code


def process_start(pid):
    """Process birth identity for verifying a phase marker despite PID reuse."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        try:
            return subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                  text=True, timeout=2, check=True).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None


def kill_process_tree(root, nonce=None):
    """Stop child phase groups before the timed-out tick releases its lock."""
    if root != os.getpgrp():
        try:
            os.killpg(root, signal.SIGSTOP)  # freeze the tick before it can start another phase
        except ProcessLookupError:
            pass
    marker = STATE / f"orchestrator-phase-{root}.pid"
    try:
        record = json.loads(marker.read_text())
        phase = int(record["phase"])
    except (OSError, ValueError, KeyError, TypeError):
        record, phase = {}, None
    descendants = []
    try:
        listing = subprocess.run(["ps", "-e", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=5).stdout
        children, groups = {}, {}
        for line in listing.splitlines():
            pid, parent, group = map(int, line.split()[:3])
            children.setdefault(parent, []).append(pid)
            groups[pid] = group
        pending = [root]
        while pending:
            pid = pending.pop()
            descendants.append(pid)
            pending.extend(children.get(pid, []))
        targets = [groups[pid] for pid in reversed(descendants) if pid in groups]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        targets = [root]
    verified_birth = phase and record.get("root_start") and record.get("phase_start") and \
        process_start(root) == record["root_start"] and process_start(phase) == record["phase_start"]
    try:
        verified_nonce = bool(verified_birth and nonce and record.get("nonce") == nonce and os.getpgid(phase) == phase)
    except ProcessLookupError:
        verified_nonce = False
    if phase not in descendants and not verified_nonce:
        phase = None  # a stale marker cannot authorize killing a reused process group
    for group in dict.fromkeys([*([phase] if phase else []), *targets, root]):
        if group != os.getpgrp():
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    marker.unlink(missing_ok=True)


def execute(name):
    """One run, start to finish: precheck, agent (the next of config agents on a limit), log, record."""
    valid_name(name)
    a = load()[name]
    DIR.mkdir(parents=True, exist_ok=True)
    lock_file = DIR / f"{name}.lock"
    pid_file = DIR / f"{name}.pid"
    with lock_file.open("a+") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        pid_file.write_text(str(os.getpid()))
        try:
            start = time.time()
            logf = DIR / name / f"{time.strftime('%Y%m%d-%H%M%S')}.log"
            logf.parent.mkdir(exist_ok=True)
            rec = {"name": name, "ts": int(start), "log": str(logf)}
            with logf.open("w") as log:
                if a.get("precheck"):
                    # shell on purpose: prechecks are the owner's own commands (`a || b`), like Orca's
                    try:
                        p = subprocess.run(a["precheck"], shell=True, cwd=os.path.expanduser(a.get("cwd") or "~"),  # noqa: S602
                                           text=True, capture_output=True, timeout=300)
                        log.write(f"$ precheck: {a['precheck']}\n{p.stdout}{p.stderr}[exit {p.returncode}]\n")
                        if p.returncode:
                            rec |= {"status": "skipped", "summary": (p.stdout or p.stderr).strip()[-200:]}
                    except subprocess.TimeoutExpired:
                        log.write(f"$ precheck: {a['precheck']}\n[timed out]\n")
                        rec |= {"status": "failed", "summary": "precheck timed out"}
                if "status" not in rec and a.get("kind") == "orchestrator":
                    out, code = orchestrator(a, log)
                    rec |= {"status": "ok" if code == 0 else "failed", "engine": "orchestrator", "summary": out.strip()[-300:]}
                if "status" not in rec:
                    for engine in dict.fromkeys([provider(a), *CONFIG["agents"]]):
                        out, code, tokens = agent(a, engine, log)
                        if not (code and LIMIT_RE.search(out)):
                            break
                    rec |= {"status": "ok" if code == 0 else "failed", "engine": engine, "summary": out.strip()[-300:], **tokens}
                    if engine == provider(a) and a.get("model"):
                        rec["model"] = a["model"]
            rec["secs"] = int(time.time() - start)
            diag.event("automation", name_h=diag.h(name), status=rec["status"], engine=rec.get("engine"), model=rec.get("model"),
                       in_tokens=rec.get("in_tokens"), out_tokens=rec.get("out_tokens"), cost_cents=rec.get("cost_cents"), secs=time.time() - start,
                       msg_class=diag.classify(rec.get("summary", "")) if rec["status"] == "failed" else None)
            with (DIR / "runs.jsonl").open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            if rec["status"] == "failed":
                ping(f"Automation {name} failed: {rec['summary'][-150:]}\nLog: {logf}\nOptions: look into it | ignore", kind="automation-failed")
        finally:
            pid_file.unlink(missing_ok=True)


def spawn(name):
    valid_name(name)
    if alive(name):
        return print(f"{name}: still running, skipped")
    DIR.mkdir(parents=True, exist_ok=True)
    with (DIR / "errors.log").open("a") as err:  # a crash before the run log exists lands here
        subprocess.Popen([sys.executable, __file__, "exec", name], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=err)
    print(f"{name}: started")


def tick(now=None):
    t = time.localtime(now)
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        (DIR / "last_tick").touch()  # diag.py doctor: a timer that stopped firing shows as a stale file
    except OSError:
        pass  # a heartbeat never stops the tick
    # ponytail: a minute the timer misses (machine down) is skipped, not caught up; add a last-run check if that matters
    for name, a in load().items():
        try:
            if not NAME_RE.fullmatch(name):
                continue
            if a.get("enabled", True) and due(a["schedule"], t):
                spawn(name)
        except Exception as e:
            diag.event("automation", name_h=diag.h(name), status="failed", **diag.failure(e))


UNIT = """[Unit]
Description=Greenlight automations tick
[Service]
Type=oneshot
KillMode=process
ExecStart={exe} {path} tick
"""
TIMER = """[Unit]
Description=Run greenlight automations every minute
[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s
[Install]
WantedBy=timers.target
"""
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>greenlight-auto</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>{path}</string><string>tick</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{env_path}</string></dict>
  <key>StartInterval</key><integer>60</integer>
</dict></plist>
"""


def main():
    args = sys.argv[1:]
    cmd, name = (args or ["list"])[0], (args[1] if len(args) > 1 else None)
    opt = lambda k: args[args.index(f"--{k}") + 1] if f"--{k}" in args else None
    autos = load()
    if cmd == "list":
        for n, a in autos.items():
            orch = a.get("kind") == "orchestrator"
            print(f"{n:24} {'on ' if a.get('enabled', True) else 'off'}  {a['schedule']:15} {'tick' if orch else provider(a):8}"
                  f" {'running' if alive(n) else ''}\n    {'orchestrate.py tick' if orch else a['prompt'][:100]}")
    elif cmd in ("add", "edit"):
        try:
            valid_name(name)
        except ValueError as e:
            sys.exit(str(e))
        if (cmd == "add") == (name in autos):
            sys.exit(f"{name}: {'already exists' if cmd == 'add' else 'no such automation'}")
        a = autos.get(name, {"enabled": True}) | {k: opt(k) for k in OPTS if opt(k) is not None}
        if "--enabled" in args or "--disabled" in args:
            a["enabled"] = "--enabled" in args
        if "--orchestrator" in args:
            a["kind"] = "orchestrator"
        elif "--agentic" in args:
            a.pop("kind", None)
        if not a.get("schedule") or not (a.get("prompt") or a.get("kind") == "orchestrator"):
            sys.exit("--schedule and --prompt (or --orchestrator) are required")
        due(a["schedule"], time.localtime())  # fails loudly on a bad cron
        autos[name] = a
        save(autos)
        print(json.dumps({name: a}, indent=2))
    elif cmd == "show":
        print(json.dumps(autos[name]) if name in autos else sys.exit(f"{name}: no such automation"))
    elif cmd == "rm":
        autos.pop(name) if name in autos else sys.exit(f"{name}: no such automation")
        save(autos)
        print(f"removed {name}")
    elif cmd == "run":
        spawn(name) if name in autos else sys.exit(f"{name}: no such automation")
    elif cmd == "exec":
        execute(name)
    elif cmd == "runs":
        f = DIR / "runs.jsonl"
        rows = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        for r in [r for r in rows if not name or r["name"] == name][-20:]:
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {r['name']:24} {r['status']:8}"
                  f" {r.get('engine', ''):6} {r['secs']}s  {r['summary'][-80:]!r}")
    elif cmd == "tick":
        tick()
    elif cmd == "install" and sys.platform == "darwin":
        f = Path.home() / "Library/LaunchAgents/greenlight-auto.plist"
        f.parent.mkdir(parents=True, exist_ok=True)
        # launchd starts with a bare PATH: keep this shell's, so glab/gh/orca and the agents are found
        f.write_text(PLIST.format(exe=xml_escape(sys.executable), path=xml_escape(str(Path(__file__).resolve())),
                                  env_path=xml_escape(os.environ.get("PATH", ""))))
        subprocess.run(["launchctl", "unload", str(f)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(f)], check=True)
        print(f"greenlight-auto launch agent loaded ({f})")
    elif cmd == "install":
        def _sd_quote(s):
            s = str(s)
            if any(c in s for c in ("\n", "\r", "\0")):
                raise ValueError(f"control char in systemd ExecStart path: {s!r}")
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
        d = Path.home() / ".config/systemd/user"
        d.mkdir(parents=True, exist_ok=True)
        (d / "greenlight-auto.service").write_text(UNIT.format(exe=_sd_quote(sys.executable), path=_sd_quote(Path(__file__).resolve())))
        (d / "greenlight-auto.timer").write_text(TIMER)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "greenlight-auto.timer"], check=True)
        print("greenlight-auto.timer enabled")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    diag.install("auto")
    main()
