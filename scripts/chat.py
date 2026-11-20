#!/usr/bin/env python3
"""Menubar chat backend: one greenlight session on the greenlight host, resumed per message.

Every command streams JSON lines (events, see Norm) for the Greenlight app:
  chat.py send [--model M] [--effort E] [--name N] < message
                                     # M: a claude model, or a harness name (codex|opencode|pi) for its default model
  chat.py compact [--model M]       # claude only
  chat.py new                        # forget every harness's session (Clear)
  chat.py stop                       # kill the running turn
  chat.py bg                         # one JSON line: this chat's background shells + working Orca agents
  chat.py bgstop <task_id>           # kill one background shell
"""
import json, os, re, signal, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402

DIR = os.path.expanduser(os.environ.get("CHAT_DIR") or os.path.join(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight", "chat"))  # CHAT_DIR: tests use a scratch session
PIDFILE = os.path.join(DIR, "pid")
BGFILE = os.path.join(DIR, "bg.json")  # the running turn's background shells
CODEX_WINDOW = 272000  # ponytail: codex doesn't report its window; fixed guess for the meter
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PROMPT = (
    "You are greenlight (the user also calls you greenlight), talking with the user through their Mac menubar app. "
    "For anything about workspaces, MRs, agents or automations, follow " + os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md") + " and its memory notes. "
    "Never do work yourself (code, debugging, reviews, investigations, writing): dispatch it to an agent in the "
    "appropriate Orca workspace, creating one with a goal if none fits, then report back what you dispatched and where. "
    "AskUserQuestion is unavailable here: ask in plain text, one question at a time. "
    "Begin every reply with a line of exactly three emoji that capture your emotional state right then (say `😌🔍☕`), "
    "honestly: how the work, the board and the conversation feel to you. Voice mode doesn't speak them. "
    "Replies render as markdown in a narrow popover: keep them short, no tables or headings, MR links as markdown links. "
    "When you ask a question with a few likely answers, end with one final line: `Options: first | second | third`. "
    "When the user asks you to reset, clear the context or start over, say a short ok and add a line `[chat: reset]`: the app clears "
    "the conversation after your reply. "
    "After everything else (even the Options line), end every reply with `[hexes: name, name]`: the workspaces the reply is about, "
    "by their exact board names, or `[hexes:]` when none. The app hides the line and lights those workspaces' hexes on the dashboard in "
    "every window until your next reply: it's how you highlight hexes when asked, and the only way. When they ask you to highlight, "
    "show, light up or point at workspaces (\"show me…\", \"which ones…\"), that is a yes: light them, never ask whether to. "
    "Each message runs in a process that exits when you reply, so background tasks, Monitor and ScheduleWakeup die "
    "at the end of the turn: never use run_in_background, Monitor or ScheduleWakeup here. Leave follow-up watching to "
    "the Greenlight automation, or record it with `greenlight.py note <workspace> \"...\"`. "
    "A <task-notification> is never the user's message; the text after `USER MESSAGE (reply to this):` is. Always answer it. "
    "Messages may start with a BOARD: every unfinished workspace with its lifecycle step (step i/n), who it waits on, and when "
    "it's on the user what to do. It's greenlight's latest scan; trust it over memory and say where a workspace is in its lifecycle "
    "when it matters. Asked what to work on (or checked in on), pick 1–3 from it: blocks on the user first (others wait on them), "
    "then the user's moves closest to merge, then anything whose agent is idle; say why and the one next action for each. "
    "Refresh the workspaces whenever you see fit (the board looks stale, or you just dispatched, merged, created or cleaned one up): "
    "`python3 " + SCRIPTS + "/greenlight.py scan >/dev/null; python3 " + SCRIPTS + "/dash.py board` rescans every workspace, "
    "updates the user's dashboard and prints the fresh board. "
    "To see the user's GitHub and GitLab work beyond the workspaces (asked what to pick up, what's waiting on them, about an issue "
    "or MR, or when the board is thin): `python3 " + SCRIPTS + "/dash.py forge` lists the open MRs/PRs they're asked to review, "
    "their own, and the issues assigned to them, each with its status (draft, conflicts, CI, approvals, threads), how stale it is, "
    "and the workspace already on it; `dash.py forge <owner/repo>` lists everything open in one repo. Suggest from the statuses: "
    "reviews others wait on first, then their MRs one step from merge (fix CI, a rebase, conflicts, threads), then issues with no "
    "MR yet; say why and the next action, and offer to start a workspace for one that has none."
)


def board_note():
    """The BOARD line ahead of the user's message, or "" when the session already has this one (or there's no scan)."""
    try:
        import dash
        b = dash.board()
    except Exception:  # ponytail: no scan yet, or it's mid-write: the chat goes on without the board
        return ""
    if not b and os.environ.get("GREENLIGHT_ONLY"):
        b = "(no workspaces in this window)"  # not nothing: the session would go on with the last window's
    f = os.path.join(DIR, "board")  # the board the session last saw: an unchanged one isn't sent again
    if not b or (os.path.exists(f) and open(f).read() == b):
        return ""
    open(f, "w").write(b)
    scope = ", only the workspaces in the window the user is in" if os.environ.get("GREENLIGHT_ONLY") else ""
    return f"BOARD (greenlight's latest scan, {time.strftime('%H:%M')}{scope}):\n{b}\n\n"
WRAP = "USER MESSAGE (reply to this): "
RETRY_PREFIX = "Your previous reply ignored the user's message. Answer it now: "
IGNORED = re.compile(r"^\s*(no response requested\.?)?\s*$", re.I)  # empty, or the model answering only a notification


def emit(**e):
    print(json.dumps(e), flush=True)


def short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def step_title(name, inp):
    """A tool call as the one-line step the activity list shows: (title, detail)."""
    p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
    if name == "Bash":
        return inp.get("description") or "Ran a shell command", inp.get("command", "")
    if name == "Read":
        return f"Read {os.path.basename(p)}", p
    if name in ("Edit", "Write", "NotebookEdit"):
        return f"Edited {os.path.basename(p)}", p
    if name == "Skill":
        return "Loaded skill", inp.get("skill", "")
    if name in ("Task", "Agent"):
        return "Started an agent", inp.get("description", "")
    if name in ("Grep", "Glob"):
        return "Searched files", inp.get("pattern", "")
    if name in ("WebFetch", "WebSearch"):
        return "Searched the web" if name == "WebSearch" else "Fetched a page", inp.get("query") or inp.get("url", "")
    first = next((v for v in inp.values() if isinstance(v, str)), "")
    return name.split("__")[-1].replace("_", " "), first


def call(name, inp):
    """A tool call the way Claude Code prints it: Bash(git status), Read(scripts/chat.py)."""
    a = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("query") \
        or inp.get("url") or inp.get("skill") or inp.get("description") or next((v for v in inp.values() if isinstance(v, str)), "")
    return f"{name.split('__')[-1]}({short(a, 100)})"


class Norm:
    """Each harness's JSON lines (claude stream-json, codex/opencode/pi json) → the app's events."""

    def __init__(self):
        self.msg_t = None  # current API message start, for tok/s
        self.chars = 0
        self.out, self.msg_out = 0, 0  # output tokens: finished messages, the current one
        self.last_tps = 0.0
        self.context = 0
        self.window = None
        self.text = ""  # last text block, the fallback reply
        self.cmds = {}  # Bash tool_use id → command, so a background shell can be found and stopped
        self.shells = {}  # task_id → {id, title, cmd, started}

    def usage(self, u):
        return u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)

    def claude(self, d):
        t, out = d.get("type"), []
        if t == "system" and d.get("subtype") == "status" and d.get("status"):
            out.append({"e": "status", "text": d["status"].capitalize()})
        elif t == "system" and d.get("subtype") == "task_started" and d.get("is_backgrounded"):
            self.shells[d["task_id"]] = {"id": d["task_id"], "title": d.get("description") or "Background shell",
                                         "cmd": self.cmds.get(d.get("tool_use_id"), ""), "started": time.time()}
            out.append({"e": "bg", "shells": list(self.shells.values())})
        elif t == "system" and d.get("subtype") == "background_tasks_changed":
            live = {x["task_id"] for x in d.get("tasks", [])}
            self.shells = {k: v for k, v in self.shells.items() if k in live}
            out.append({"e": "bg", "shells": list(self.shells.values())})
        elif t == "system" and d.get("subtype") == "compact_boundary":
            self.context = d["compact_metadata"].get("post_tokens", 0)
            out.append({"e": "usage", "context": self.context})
        elif t == "stream_event":
            ev = d["event"]
            if ev["type"] == "message_start":
                self.msg_t, self.chars = time.time(), 0
                self.out, self.msg_out = self.out + self.msg_out, 0
                self.context = self.usage(ev["message"].get("usage", {})) or self.context
                out.append({"e": "usage", "context": self.context})
            elif ev["type"] == "content_block_start" and ev["content_block"]["type"] == "text":
                out.append({"e": "text_reset"})
            elif ev["type"] == "content_block_delta":
                dl = ev["delta"]
                s = dl.get("text") or dl.get("thinking") or dl.get("partial_json") or ""
                self.chars += len(s)
                if dl.get("type") == "text_delta":
                    out.append({"e": "text", "delta": s})
                elif dl.get("type") == "thinking_delta":
                    out.append({"e": "status", "text": "Thinking"})
                if self.msg_t and time.time() - self.last_tps > 0.5:  # live estimate, ~4 chars a token
                    self.last_tps = time.time()
                    out.append({"e": "usage", "tps": round(self.chars / 4 / max(time.time() - self.msg_t, 0.2), 1),
                                "out": self.out + max(self.msg_out, self.chars // 4)})
            elif ev["type"] == "message_delta" and self.msg_t:
                n = self.msg_out = ev.get("usage", {}).get("output_tokens", 0)
                out.append({"e": "usage", "tps": round(n / max(time.time() - self.msg_t, 0.2), 1), "out": self.out + n})
        elif t == "assistant":
            for c in d["message"].get("content", []):
                if c.get("type") == "tool_use":
                    self.cmds[c["id"]] = c.get("input", {}).get("command", "")
                    title, detail = step_title(c["name"], c.get("input", {}))
                    out.append({"e": "step", "id": c["id"], "title": short(title, 80), "detail": short(detail), "call": call(c["name"], c.get("input", {}))})
                elif c.get("type") == "text":
                    self.text = c["text"]
        elif t == "user" and isinstance(d.get("message", {}).get("content"), list):
            for c in d["message"]["content"]:
                if c.get("type") == "tool_result":
                    res = c.get("content")
                    if isinstance(res, list):
                        res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                    out.append({"e": "step_done", "id": c["tool_use_id"], "ok": not c.get("is_error"), "out": short(res or "", 120)})
        elif t == "result":
            mu = next(iter((d.get("modelUsage") or {}).values()), {})
            self.window = mu.get("contextWindow") or self.window
            out.append({"e": "done", "ok": not d.get("is_error"), "text": d.get("result") or self.text,
                        "context": self.context, "window": self.window, "ms": d.get("duration_ms")})
        return out

    def codex(self, d):
        t, it, out = d.get("type"), d.get("item", {}), []
        if t == "item.started" and it.get("type") == "command_execution":
            cmd = it["command"].split(" -lc ", 1)[-1].strip("'")
            out.append({"e": "step", "id": it["id"], "title": "Ran a shell command", "detail": short(cmd), "call": f"Bash({short(cmd, 100)})"})
        elif t == "item.completed" and it.get("type") == "command_execution":
            out.append({"e": "step_done", "id": it["id"], "ok": it.get("exit_code") == 0, "out": short(it.get("aggregated_output", ""), 120)})
        elif t == "item.completed" and it.get("type") == "agent_message":
            self.text = it["text"]
            out += [{"e": "text_reset"}, {"e": "text", "delta": it["text"]}]
        elif t == "item.started":
            out.append({"e": "status", "text": it.get("type", "working").replace("_", " ").capitalize()})
        elif t == "error" and str(d.get("message", "")).startswith("Reconnecting"):  # a retry, not the end
            out.append({"e": "status", "text": short(d["message"], 80)})
        elif t == "turn.completed":
            self.context = d.get("usage", {}).get("input_tokens", 0)
            out.append({"e": "done", "ok": True, "text": self.text, "context": self.context, "window": CODEX_WINDOW, "ms": None})
        elif t in ("turn.failed", "error"):
            out.append({"e": "done", "ok": False, "text": str(d.get("error") or d.get("message")), "context": self.context, "window": CODEX_WINDOW, "ms": None})
        return out

    def opencode(self, d):
        """`opencode run --format json`: whole parts, a tool only once it finished; no final event (run() adds the done)."""
        t, pt, out = d.get("type"), d.get("part") or {}, []
        if t == "tool_use":
            st = pt.get("state") or {}
            title, detail = step_title(pt.get("tool", "tool").capitalize(), st.get("input") or {})
            out += [{"e": "step", "id": pt.get("id"), "title": short(st.get("title") or title, 80), "detail": short(detail)},
                    {"e": "step_done", "id": pt.get("id"), "ok": st.get("status") == "completed", "out": short(str(st.get("output") or st.get("error") or ""), 120)}]
        elif t == "text":
            self.text = pt.get("text", "")
            out += [{"e": "text_reset"}, {"e": "text", "delta": self.text}]
        elif t == "reasoning":
            out.append({"e": "status", "text": "Thinking"})
        elif t == "step_finish":
            tk = pt.get("tokens") or {}
            self.context = tk.get("input", 0) + (tk.get("cache") or {}).get("read", 0) + (tk.get("cache") or {}).get("write", 0) or self.context
            out.append({"e": "usage", "context": self.context})
        elif t == "error":
            e = d.get("error") or {}
            out.append({"e": "done", "ok": False, "text": str((e.get("data") or {}).get("message") or e.get("name") or e),
                        "context": self.context, "window": None, "ms": None})
        return out

    def pi(self, d):
        """`pi --mode json`: agent session events."""
        t, out = d.get("type"), []
        m, ev = d.get("message") or {}, d.get("assistantMessageEvent") or {}
        if t == "message_update" and ev.get("type") == "text_start":
            out.append({"e": "text_reset"})
        elif t == "message_update" and ev.get("type") == "text_delta":
            out.append({"e": "text", "delta": ev.get("delta", "")})
        elif t == "message_update" and ev.get("type") == "thinking_delta":
            out.append({"e": "status", "text": "Thinking"})
        elif t == "message_end" and m.get("role") == "assistant":
            u = m.get("usage") or {}
            self.context = u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0) or self.context
            self.text = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text") or self.text
            if m.get("stopReason") in ("error", "aborted"):
                self.error = m.get("errorMessage") or m["stopReason"]
            out.append({"e": "usage", "context": self.context})
        elif t == "tool_execution_start":
            title, detail = step_title(d.get("toolName", "tool").capitalize(), d.get("args") or {})
            out.append({"e": "step", "id": d.get("toolCallId"), "title": short(title, 80), "detail": short(detail)})
        elif t == "tool_execution_end":
            r = d.get("result")
            r = " ".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict)) if isinstance(r, dict) else str(r or "")
            out.append({"e": "step_done", "id": d.get("toolCallId"), "ok": not d.get("isError"), "out": short(r, 120)})
        elif t == "agent_end":
            err = getattr(self, "error", None)
            out.append({"e": "done", "ok": not err, "text": err or self.text, "context": self.context, "window": None, "ms": None})
        return out


def session(engine, val=None):
    f = os.path.join(DIR, "session" if engine == "claude" else f"session_{engine}")
    if val is not None:
        open(f, "w").write(val)
    return open(f).read().strip() if os.path.exists(f) else None


def run(cmd, engine, text, norm, check=False):
    """Run one turn, stream events. Returns (sid or None, error or None or "retry").
    check: a done whose reply ignored the user (IGNORED) isn't emitted; the process is killed and "retry" returned."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=DIR, start_new_session=True)
    open(PIDFILE, "w").write(str(p.pid))
    p.stdin.write(text)
    p.stdin.close()
    sid, done = None, False
    for line in p.stdout:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        sid = d.get("session_id") or d.get("thread_id") or d.get("sessionID") or (d.get("id") if d.get("type") == "session" else None) or sid
        evs = getattr(norm, engine)(d)
        if check and any(e["e"] == "done" and e["ok"] and IGNORED.match(e["text"] or "") for e in evs):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            p.wait()
            if os.path.exists(PIDFILE):
                os.remove(PIDFILE)
            return sid, "retry"
        for e in evs:
            if e["e"] == "bg":  # not for the stream: `chat.py bg` serves it
                json.dump(e["shells"], open(BGFILE, "w"))
                continue
            if e["e"] == "done" and done:  # opencode can report one failure twice
                continue
            if e["e"] == "done" and sid:  # saved before the app hears done: a Clear right after must not be undone by a late save
                session(engine, sid)
            done |= e["e"] == "done"
            emit(**e)
    p.wait()
    if not done and not p.returncode:  # opencode has no final event: its clean exit is the done
        if sid:
            session(engine, sid)
        emit(e="done", ok=True, text=norm.text, context=norm.context, window=norm.window, ms=None)
        done = True
    for f in (PIDFILE, BGFILE):  # the process is gone, and its shells with it
        if os.path.exists(f):
            os.remove(f)
    if not done and p.returncode:
        if p.returncode in (-15, 143):  # SIGTERM from `chat.py stop`
            return sid, "Stopped"
        return sid, p.stderr.read().strip() or f"exit {p.returncode}"
    return (None if done else sid), None  # done: its session is saved already


def agent_title(a):
    """An Orca agent's one-line task: its title, else its prompt without the paste wrapper or a /goal prefix."""
    t = a.get("taskTitle") or a.get("displayName") or a.get("prompt") or ""
    t = re.sub(r"</?pasted_content[^>]*>", "", t).strip()
    t = re.sub(r"^/goal\s+", "", t)
    return short(t.split("\n")[0], 300) or "Agent"  # the app truncates to fit its width


def background():
    """This chat's live background shells and every working Orca agent, for the app's footer chip."""
    shells = json.load(open(BGFILE)) if os.path.exists(BGFILE) and os.path.exists(PIDFILE) else []
    agents = []
    try:
        import greenlight
        for w in greenlight.orca("worktree", "ps")["worktrees"]:
            for a in w.get("agents", []):
                if a.get("state") == "working":
                    agents.append({"title": agent_title(a), "agent": a.get("agentType", "agent"), "workspace": w.get("displayName", ""),
                                   "started": (a.get("stateStartedAt") or 0) / 1000})
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        pass  # the ADE is down: show shells only
    return {"shells": [{k: v for k, v in s.items() if k != "cmd"} for s in shells], "agents": agents}


def stop_shell(task_id):
    """Kill one background shell: the claude process's descendants whose command line holds the shell's command."""
    shells = json.load(open(BGFILE)) if os.path.exists(BGFILE) else []
    cmd = next((s["cmd"] for s in shells if s["id"] == task_id), "")
    key = max(re.split(r"['\"\\]", cmd), key=len).strip()  # ponytail: longest quote-free chunk, quoting is rewritten on the way to the shell
    if not key or not os.path.exists(PIDFILE):
        return False
    rows = [l.split(None, 2) for l in subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines()]
    kids = {}
    for r in rows:
        if len(r) == 3:
            kids.setdefault(int(r[1]), []).append((int(r[0]), r[2]))
    args = {pid: a for v in kids.values() for pid, a in v}
    def tree(pid):
        return [pid] + [x for k, _ in kids.get(pid, []) for x in tree(k)]
    hits = [p for p in tree(int(open(PIDFILE).read()))[1:] if key in args.get(p, "")]
    for p in {x for h in hits for x in tree(h)}:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return bool(hits)


def turn(text, model, effort, compact=False, name=None):
    engine, model = (model, None) if model in harness.H else ("claude", model)
    prompt = PROMPT + (f" The user has named you {name} in the app; answer to that name." if name and name.lower() not in ("greenlight", "greenlight") else "")
    rules = os.path.expanduser(os.path.join(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight", "instructions.md"))
    if os.path.exists(rules):  # the user's standing instructions (Settings → Model), read every turn
        prompt += "\n\nThe user's standing instructions, which override the defaults above:\n" + open(rules).read().strip()
    sid = session(engine)
    if not compact:
        if not sid and os.path.exists(os.path.join(DIR, "board")):
            os.remove(os.path.join(DIR, "board"))  # a new session hasn't seen any board
        text = board_note() + WRAP + text
    # a harness without a system-prompt flag gets the prompt in its first message
    mk = lambda s, t: harness.argv(engine, t, model=model, effort=None if compact else effort, sid=s, system=prompt, stream=True)
    emit(e="start", engine=engine, model=model or "default")
    norm = Norm()
    try:
        cmd, t = mk(sid, text)
        new_sid, err = run(cmd, engine, t, norm, check=not compact)
    except (ValueError, OSError) as e:  # unknown harness, pi without bypass, binary missing
        return emit(e="done", ok=False, text=str(e), context=0, window=None, ms=None)
    if err == "retry":  # once only: the retry runs unchecked
        emit(e="status", text="Reply ignored your message, asking again")
        norm.text = ""
        cmd, t = mk(new_sid or sid, RETRY_PREFIX + text)
        new_sid, err = run(cmd, engine, t, norm)
        sid = None  # don't fall into the session-lost retry below
    if err and sid and not compact and err != "Stopped":  # session gone or corrupt: start over rather than fail
        emit(e="status", text="Session lost, starting a new one")
        cmd, t = mk(None, text)
        new_sid, err = run(cmd, engine, t, norm)
    if new_sid:
        session(engine, new_sid)
    if err:
        emit(e="done", ok=False, text=short(err, 500), context=norm.context, window=norm.window, ms=None)


def main():
    os.makedirs(DIR, exist_ok=True)
    args = sys.argv[1:]
    cmd = args[0] if args else "send"
    opt = lambda k: args[args.index(k) + 1] if k in args else None
    if cmd == "new":
        for f in ["session", "board"] + [f"session_{h}" for h in harness.H]:
            if os.path.exists(os.path.join(DIR, f)):
                os.remove(os.path.join(DIR, f))
    elif cmd == "stop":
        if os.path.exists(PIDFILE):
            try:
                os.killpg(int(open(PIDFILE).read()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
    elif cmd == "bg":
        print(json.dumps(background()))
    elif cmd == "bgstop":
        print(json.dumps({"ok": stop_shell(args[1])}))
    elif cmd == "compact":
        if not session("claude"):
            emit(e="done", ok=True, text="Nothing to compact yet.", context=0, window=None, ms=None)
        else:
            turn("/compact", opt("--model"), None, compact=True)
    else:
        if opt("--only") is not None:  # the window the user is in: its workspaces only, for the BOARD and Chief's own `dash.py board`
            os.environ["GREENLIGHT_ONLY"] = opt("--only")
        turn(sys.stdin.read().strip(), opt("--model"), opt("--effort"), name=opt("--name"))


if __name__ == "__main__":
    main()
