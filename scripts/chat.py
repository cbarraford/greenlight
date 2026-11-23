#!/usr/bin/env python3
"""Menubar chat backend: one greenlight session on the greenlight host, resumed per message.

Every command streams JSON lines (events, see Norm) for the Greenlight app:
  chat.py send [--model M] [--effort E] [--name N] [--verbose] < message
  chat.py send --attach ... < {"text": message, "files": [{"name", "data" (base64)}]}   # files the user attached (attach.py)
                                     # --verbose: steps also carry the whole input and output, and thinking streams (Settings › Verbose activity)
                                     # M: a Claude or agy Gemini model, or a harness name for its default model
  chat.py compact [--model M]       # claude only
  chat.py new                        # forget every harness's session (Clear)
  chat.py stop                       # kill the running turn
  chat.py bg                         # one JSON line: this chat's background shells + working Orca agents
  chat.py bgstop <task_id>           # kill one background shell
"""
import contextlib, fcntl, glob, hashlib, json, os, random, re, signal, subprocess, sys, threading, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import attach  # noqa: E402
import decisions  # noqa: E402
import diag  # noqa: E402
import harness  # noqa: E402
import auto  # noqa: E402

DIR = os.path.expanduser(os.environ.get("CHAT_DIR") or os.path.join(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight", "chat"))  # CHAT_DIR: tests use a scratch session
CHAT = os.path.basename(DIR.rstrip("/")) if os.environ.get("CHAT_DIR") else None  # a second L1 window's Chief (CHAT_DIR=chat-<n>): its questions are its own in the decision record


def scope_suffix():
    scope = os.environ.get("GREENLIGHT_ONLY")
    return "_scope-" + hashlib.sha1(scope.encode()).hexdigest()[:12] if scope else ""


def asked_path():
    return os.path.join(DIR, "asked" + scope_suffix())


def chat_identity():
    return (CHAT or "main") + scope_suffix() if scope_suffix() else CHAT
PIDFILE = os.path.join(DIR, "pid")
BGFILE = os.path.join(DIR, "bg.json")  # the running turn's background shells
CODEX_SESSIONS = os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"), "sessions")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PROMPT = (
    "You are greenlight (the user also calls you greenlight), talking with the user through their Mac menubar app. "
    "For anything about workspaces, MRs, agents or automations, follow " + os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md") + " and its memory notes. "
    "A request that only asks (status, research, a billing or usage lookup, inspecting logs, code or an MR) you answer "
    "yourself, here, from the sources you can read (greenlight.py, the ADE, glab/gh, APIs and files on this host); "
    "never create a workspace to answer one (`greenlight.py ade worktree create` refuses it; `--change \"<what>\"` "
    "only when the message does ask for a change). "
    "A request that changes something (code, docs, settings, an external system) goes to an agent in the appropriate "
    "Orca workspace, creating one with a goal if none fits; then report back what you dispatched and where. "
    "AskUserQuestion is unavailable here: ask in plain text, one question at a time. "
    "Files the user attaches come after their message under ATTACHED FILES: they're part of the message, so read them "
    "and answer with them in mind; when you dispatch work about one, give the agent its path. "
    "Answer concisely: the answer first, only what was asked, no preamble, no restating the question, no closing offers. "
    "Replies render as markdown in a narrow popover: a few short lines at most, no tables or headings, MR links as markdown links. "
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
    "MR yet; say why and the next action, and offer to start a workspace for one that has none. "
    "When a message includes a STALE QUESTION or BOARD head update notice, never treat the user's reply as approval for the "
    "changed commit, target branch, branch, or goal: explain what changed and confirm again (or answer their new topic if they moved on). "
    "When asking for confirmation of a mutating action (merge, approve, push, delete), name the workspace or MR ref (`#N`/`!N`). "
    "Before merging, approving, pushing, or deleting an MR or workspace, verify its current head SHA and target branch against the live forge "
    "(use `audit.py checked-merge` for merges so the forge enforces the exact head commit). "
    "Agents: the coding agents in the user's workspaces (Claude Code, Codex, opencode, pi, Gemini CLI) each run in a Greenlight terminal tab, a tmux "
    "session on `tmux -L greenlight`; this host's are here (tabs on the user's Mac aren't: you can open those, not read them). "
    "`tmux -L greenlight ls -F '#{session_name} #{pane_current_command} #{pane_current_path}'` lists them (the path is the worktree); "
    "`capture-pane -p -t '=SESSION:' -S -60` reads an agent's screen, to tell the user what it's doing or asking. When the user asks to "
    "see, open or bring up an agent, add a line `[console: SESSION]` (or its worktree path): the app opens that terminal tab in front of "
    "them. Only agents: not plain shells. Never type into a terminal or run anything in one (no send-keys); the user does that."
)


BOARD_FULL = 12  # a board this short goes whole; a longer one is cut to what needs the user and what the message is about
BOARD_NEEDS = 6   # lines of a long board kept from its top (blocks on the user, then their moves)
BOARD_FOCUS_MAX = 20
CONTEXT_K = 5     # state cards and memory notes picked for the message (ws_state.top)


def chat_config():
    try:
        with open(os.path.join(os.path.expanduser(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight"), "config.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def board_name(line):
    m = re.match(r"- (.+?) \((?:\[[#!]\d+\]\(.+?\)|[#!]\d+|no MR), .+?\): ", line)
    return m[1] if m else None


def focus(board, query, hits=()):
    """A long board cut to its top BOARD_NEEDS lines (the board sorts what needs the user first), the lines the message
    names (a workspace, an MR ref) and the workspaces of the cards picked for it, and a count of the rest. Short boards whole."""
    lines = board.splitlines()
    tail = [l for l in lines if l.startswith("(") and l.endswith("finished)")]
    rows = [l for l in lines if l not in tail]
    if len(rows) <= BOARD_FULL or chat_config().get("chat_context") == "board":
        return board
    q = query.lower()
    refs = re.findall(r"[#!]\d+", q)
    named = lambda l: bool(board_name(l)) and re.search(rf"(?<![\w-]){re.escape(board_name(l).lower())}(?![\w-])", q)
    reference = lambda l: (m[1] or m[2] if (m := re.match(r"- .+? \((?:\[([#!]\d+)\]\(.+?\)|([#!]\d+)|no MR), ", l)) else None)
    exact_ref = lambda l: reference(l) in refs
    referenced = lambda l: any(re.search(rf"(?<![\w#!]){re.escape(x)}(?!\d)", l) for x in refs)  # "#3" isn't "#33"
    keep = set(range(min(BOARD_NEEDS, len(rows))))
    for matches in (lambda l: named(l), lambda l: board_name(l) in hits, exact_ref, referenced):
        for i, line in enumerate(rows):
            if len(keep) >= BOARD_FOCUS_MAX:
                break
            if i not in keep and matches(line):
                keep.add(i)
    shown = [l for i, l in enumerate(rows) if i in keep]
    more = len(rows) - len(shown)
    return "\n".join(shown + [f"({more} more unfinished: `python3 {SCRIPTS}/dash.py board` prints them all)"] * bool(more) + tail)


SEEN = "seen.json"  # what the session in use has been sent: {"key": "<engine>:<sid>", "board", "lines", "context"}


def seen_load(key):
    """The seen record for this session; another session's (an engine switch, a new or lost session) counts as nothing seen."""
    try:
        with open(os.path.join(DIR, SEEN)) as fh:
            s = json.load(fh)
    except (OSError, ValueError):
        s = {}
    return s if isinstance(s, dict) and s.get("key") == key and isinstance(s.get("lines"), dict) else {"key": key, "board": None, "lines": {}, "context": {}}


def seen_save(s):
    try:
        with scope_lock():
            if scope_current_unlocked():
                with open(os.path.join(DIR, SEEN), "w") as f:
                    json.dump(s, f)
    except OSError:
        pass  # session context is a cache; a full disk must not abort a delivered chat turn


def seen_forget(force=False):
    with scope_lock():
        if force or scope_current_unlocked():
            with contextlib.suppress(OSError):
                os.remove(os.path.join(DIR, SEEN))


def window_allow(board):
    """Exact card keys in this window. Display names alone cannot distinguish same-name worktrees in other windows."""
    try:
        import ws_state
        lines_by_name = {}
        for line in board.splitlines():
            if name := board_name(line):
                lines_by_name.setdefault(name, []).append(line)
        with open(os.path.join(os.environ.get("GREENLIGHT_HOME") or os.path.expanduser("~/.greenlight"), "last.json")) as fh:
            rows = json.load(fh)["rows"]
        selected = []
        for r in rows:
            if r["name"] not in lines_by_name:
                continue
            mr = r.get("mr") or {}
            ref = mr.get("ref") or "no MR"
            refs = [ref, f"[{ref}]({mr['web_url']})"] if mr.get("web_url") and mr.get("ref") else [ref]
            exact = any(line.startswith(f"- {r['name']} ({shown_ref}, {r['project']}, {r.get('path')}): ")
                        for shown_ref in refs for line in lines_by_name[r["name"]])
            if exact or any(line.startswith(f"- {r['name']} ({shown_ref}, {r['project']}): ")
                            for shown_ref in refs for line in lines_by_name[r["name"]]):
                selected.append((r, exact))
        counts = {}
        for r, _ in selected:
            ident = (r["name"], r["project"], (r.get("mr") or {}).get("ref"))
            counts[ident] = counts.get(ident, 0) + 1
        selected = [r for r, exact in selected if exact or counts[(r["name"], r["project"], (r.get("mr") or {}).get("ref"))] == 1]
        return {r["card"]: (r.get("forge") or "", r.get("project") or "", (r.get("mr") or {}).get("ref") or "",
                           (r.get("mr") or {}).get("sha") or "", r.get("goal") or "",
                           (r.get("mr") or {}).get("target_branch") or "", r.get("id") or "", r.get("branch") or "",
                           ws_state.stage_of(r))
                for r in selected if r.get("card")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def context_k():
    return int(chat_config().get("context_k") or CONTEXT_K)


def context_note(query, board="", seen=None, allow=None):
    """CONTEXT ahead of the message: the context_k state cards and memory notes most relevant to it (ws_state.top), each
    sent once per session until it changes. Cards are limited to the board's workspaces (the window's, when scoped)."""
    if chat_config().get("chat_context") == "board":
        return ""
    try:
        import ws_state
        hits = ws_state.top(query, context_k(), allow=window_allow(board) if allow is None else allow)
    except Exception:  # ponytail: no index yet, or it's locked: the chat goes on without it
        return ""
    own = seen is None
    seen = seen_load("") if own else seen
    fresh = [h for h in hits if seen["context"].get(h["id"]) != ws_state.digest(h["text"])]
    if not fresh:
        return ""
    seen["context"] |= {h["id"]: ws_state.digest(h["text"]) for h in fresh}
    if own:
        seen_save(seen)
    body = "\n".join(f"- {'workspace' if h['kind'] == 'card' else 'memory note'} {h['name']}: {h['text']}" for h in fresh)
    return (f"CONTEXT (the workspace state cards and memory notes most relevant to this message; `python3 {SCRIPTS}/ws_state.py show <card-key> --exact` "
            f"for a whole card; ask about a specific workspace for more context):\n{body}\n\n")


def board_note(query="", key="", seen=None, snapshot=None):
    """The BOARD ahead of the user's message, or "" when the session (key: "<engine>:<sid>") already has it (or there's no
    scan). A long board is focused on the message (focus). The same scan isn't sent again: a later message only adds the
    lines it's about that the session hasn't seen. CONTEXT cards and notes follow (context_note)."""
    own = seen is None
    seen = seen_load(key) if own else seen
    try:
        import dash
        b, allow = snapshot if snapshot is not None else dash.board(with_ids=True)
    except Exception:  # ponytail: no scan yet, or it's mid-write: the chat goes on without the board
        ctx = context_note(query, "", seen)
        if own:
            seen_save(seen)
        return ctx
    if not b and os.environ.get("GREENLIGHT_ONLY"):
        b = "(no workspaces in this window)"  # not nothing: the session would go on with the last window's
    full = b
    finished = next((int(m[1]) for line in full.splitlines() if (m := re.fullmatch(r"\((\d+) more finished\)", line))), 0)
    try:
        import ws_state
        hits = {h.get("display_name") or h["name"].split("@id-")[0] for h in ws_state.top(query, context_k(), kinds=("card",), allow=allow)} if b else set()
    except Exception:
        hits = set()
    import ws_state
    scope_key = ws_state.digest(json.dumps(sorted((allow or {}).keys()))) if os.environ.get("GREENLIGHT_ONLY") else "all"
    if seen.get("scope") not in (None, scope_key):
        seen.update(board=None, lines={}, context={}, heads={})
    seen["scope"] = scope_key
    focused = focus(b, query, hits) if b else b
    digest = ws_state.digest(full)
    scope = ", only the workspaces in the window the user is in" if os.environ.get("GREENLIGHT_ONLY") else ""
    note = ""
    heads = {name: identity[3] for name, identity in (allow or {}).items() if isinstance(identity, tuple) and len(identity) > 3 and identity[3]}
    changed_heads = [name for name, sha in heads.items() if (seen.get("heads") or {}).get(name) not in (None, sha)]
    if changed_heads:
        note += "BOARD head update: " + ", ".join(changed_heads[:10]) + ". Earlier head-specific approvals and card facts may be stale; verify this commit again.\n\n"
    previous_heads = seen.get("heads") or {}
    seen["heads"] = heads | {name: previous_heads[name] for name in changed_heads[10:]}
    if focused and seen["board"] is None:
        note += f"BOARD (greenlight's latest scan, {time.strftime('%H:%M')}{scope}):\n{focused}\n\n"
    elif focused:
        extra = [l for l in focused.splitlines() if board_name(l) and seen["lines"].get(l) != ws_state.digest(l)]
        if extra:
            note += "BOARD (more lines from the same scan, for this message):\n" + "\n".join(extra) + "\n\n"
    if seen["board"] is not None and seen.get("finished", 0) != finished:
        note += f"BOARD update: {finished} more finished\n\n"
    seen["finished"] = finished
    current_keys = {l for l in full.splitlines() if board_name(l)}
    removed = [k for k in seen["lines"] if k not in current_keys]
    # A changed status line is an update to the same workspace, not a removal.
    prefix = lambda line: (m[1] if (m := re.match(r"(- .+? \((?:\[[#!]\d+\]\(.+?\)|[#!]\d+|no MR), .+?\)): ", line)) else line)
    old_by_prefix = {}
    new_by_prefix = {}
    for line in removed:
        old_by_prefix.setdefault(prefix(line), []).append(line)
    for line in current_keys:
        new_by_prefix.setdefault(prefix(line), []).append(line)
    updated = [(old[0], new_by_prefix[key][0]) for key, old in old_by_prefix.items()
               if len(old) == len(new_by_prefix.get(key, [])) == 1][:20]
    for old, new in updated:
        del seen["lines"][old]
        if new not in focused.splitlines():
            note += f"BOARD update: {new}\n\n"
        seen["lines"][new] = ws_state.digest(new)
    removed = [line for line in removed if line not in {old for old, _ in updated} and prefix(line) not in new_by_prefix]
    if len(removed) > 20:
        note += f"BOARD reset (current workspaces in this window):\n{focused or '(no workspaces)'}\n\n"
        seen["lines"].clear()
    elif removed:
        note += "BOARD update (no longer on this board): " + ", ".join(removed[:20]) + (f" and {len(removed)-20} more" if len(removed) > 20 else "") + "\n\n"
        for k in removed:
            del seen["lines"][k]
    if focused and not current_keys and seen["board"] is not None and seen["board"] != digest:
        note += f"BOARD update: {focused}\n\n"
    for line in focused.splitlines():
        if board_name(line):
            seen["lines"][line] = ws_state.digest(line)
    seen["board"] = digest
    note += context_note(query, full, seen, allow)
    if own:
        seen_save(seen)
    return note


WRAP = "USER MESSAGE (reply to this): "
ANSWER, ASKED = "\n(answer) ", "\n(asked) "  # greenlight's (not imported: its import reads config and may call the forge)
RETRY_PREFIX = "Your previous reply ignored the user's message. Answer it now: "
# What the status says while the model thinks: one picked per stretch of thinking, held until something else shows.
# Playful, never a claim about real work (no "Searching", "Merging", "Testing"), short enough for the narrow popover.
THINKING = (
    "Doodling…", "Scribbling…", "Sketching…", "Tinkering…", "Whittling…", "Knitting…", "Mulling…", "Musing…",
    "Pondering…", "Ruminating…", "Chewing on it…", "Stewing…", "Marinating…", "Daydreaming…", "Woolgathering…",
    "Simmering…", "Steeping…", "Brewing…", "Kneading…", "Whisking…", "Stirring the pot…", "Sprouting…",
    "Blossoming…", "Cloud-watching…", "Stargazing…", "Twiddling…", "Fidgeting…", "Humming…", "Whistling…",
    "Tapping toes…", "Wiggling…", "Juggling…", "Bamboozling…", "Galumphing…", "Lollygagging…", "Dillydallying…",
    "Zigzagging…", "Puzzling…", "Jigsawing…", "Untangling…", "Connecting dots…", "Hopscotching…", "Twinkling…",
    "Shimmering…", "Orbiting…", "Moonwalking…", "Yellow-lighting…", "Mug-warming…", "Blanket-forting…", "Purring…",
)
THINK_STATUS = {"Thinking", "Reasoning"}  # claude/pi/opencode thinking, codex's reasoning item
IGNORED = re.compile(r"^\s*(no response requested\.?)?\s*$", re.I)  # empty, or the model answering only a notification


TURN = {}  # this process's turn, for its diag event: {harness, model, t, norm}


def emit(**e):
    print(json.dumps(e), flush=True)
    if e.get("e") == "done":  # one chat_turn per answered message: harness, timing, tokens, cost; never the text
        diag.event("chat_turn", ok=bool(e.get("ok")), harness=TURN.get("harness"), model=TURN.get("model"),
                   context=e.get("context") if isinstance(e.get("context"), int) else None, ms=e.get("ms"),
                   **tokens(TURN.get("norm")), secs=time.monotonic() - TURN["t"] if "t" in TURN else None)


def tokens(norm):
    """A turn's diag fields from its Norm: in/out tokens, and cost in cents (clean() keeps 3 decimals, so USD would lose it)."""
    if norm is None:
        return {}
    num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0  # noqa: E731
    out = num(norm.tout) or num(norm.out) + num(norm.msg_out)
    return {"in_tokens": num(norm.tin) or None, "out_tokens": out or None, "cost_cents": num(norm.cost) * 100 or None}


def short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# Gemini CLI's built-in tools as the Claude Code names step_title and call know (same file_path/command/pattern params)
GEMINI_TOOLS = {"run_shell_command": "Bash", "read_file": "Read", "read_many_files": "Read", "write_file": "Write", "replace": "Edit",
                "glob": "Glob", "grep_search": "Grep", "web_fetch": "WebFetch", "google_web_search": "WebSearch", "invoke_agent": "Agent"}


RUNNERS = {"python", "python3", "node", "uv", "npx", "env", "sudo", "time", "nice", "nohup", "exec", "timeout", "command"}  # name what they run
SHELLS = {"sh", "bash", "zsh"}
SUBCOMMANDS = {"gh", "glab", "git", "orca", "tmux", "docker", "kubectl", "npm", "make", "codex", "claude"}  # their subcommands name them
NOUN_VERB = {"gh", "glab", "orca"}  # two words: gh pr list
WRAPPER_VERBS = {"run", "compose", "stash", "worktree", "remote"}  # …and after these: npm run build, git stash pop (not exec: a prompt)
VALUED = {"-C", "-L", "-S", "-R", "-t", "-F", "-n", "--repo", "--namespace", "--prefix"}  # a subcommand tool's flags that take the next word
AGENT_SUBS = {"exec", "review", "mcp", "resume", "login", "apply", "doctor", "update", "config", "plugin"}  # claude/codex: else a word is the prompt
RUNNER_VALUED = {"sudo": {"-u", "-g", "-C", "-h", "-p"}, "env": {"-u", "-C", "-S"}, "timeout": {"-s", "-k", "--signal", "--kill-after"},
                 "nice": {"-n"}, "uv": {"--with", "--python", "--from", "-p", "--project", "--directory"}, "npx": {"-p", "--package"}}


def _strip_runners(w):
    while len(w) > 1 and os.path.basename(w[0]) in RUNNERS:
        if w[1] in ("-c", "-e", "-"):  # python3 -c '…' or a heredoc: inline code, the interpreter is the name
            return w[:1]
        i, valued = 1, RUNNER_VALUED.get(os.path.basename(w[0]), ())
        while i < len(w) and (w[i].startswith("-") or w[i] == "run" or re.match(r"^\d+[smh]?$", w[i])):
            if w[i] == "-m" and i + 1 < len(w):  # python -m pytest: the module
                i += 1
                break
            i += 2 if w[i] in valued else 1
        w = w[i:] or w[:1]  # all flags (sudo -E): the runner is the name
    return w


def _extract_sub(prog, w):
    sub: list[str] = []
    if not (prog in SUBCOMMANDS or re.search(r"\.(py|sh|js|cjs|mjs)$", prog)):
        return sub
    i = 1
    while i < len(w) and len(sub) < (2 if prog in NOUN_VERB or sub[:1] and sub[0] in WRAPPER_VERBS else 1):
        if w[i].startswith("-"):
            if sub and not (prog in NOUN_VERB and w[i] in VALUED):  # flags after the subcommand: its arguments, kept out
                break
            i += 2 if w[i] in VALUED else 1  # git -C /w status, gh pr --repo a/b list: flags before the words
            continue
        if not re.match(r"^[a-z][a-z0-9_-]*$", w[i]):
            break
        sub.append(w[i])
        i += 1
    return sub


def cmd_title(cmd, _depth=0):
    """A shell command as what it ran, not "Ran a shell command": the first real program (the script, for an interpreter), with
    its subcommand (`cd x && python3 /a/greenlight.py scan | tail` → "Ran greenlight.py scan", `gh pr list -R a/b` → "Ran gh pr
    list"). Arguments stay out: the step's detail has the whole command. The generic line when nothing's left (echo, cd)."""
    cmd = str(cmd or "")
    masked = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", lambda m: "x" * len(m.group()), cmd)  # separators inside quotes don't split
    cuts = [0] + [x for m in re.finditer(r"&&|\|\||;|\||\n", masked) for x in m.span()] + [len(cmd)]
    for seg, bare in ((cmd[cuts[i]:cuts[i + 1]], masked[cuts[i]:cuts[i + 1]]) for i in range(0, len(cuts), 2)):
        w = [x.strip("'\"()") for x in seg.split()]
        w = [x for x in w if x and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=|^\d?[<>]", x)]  # env assignments, redirections
        while w and w[0] in ("do", "then", "else", "!"):
            w = w[1:]
        if not w or w[0] in ("cd", "export", "set", "source", ".", "true", ":", "echo", "printf", "sleep", "[", "test", "if", "fi",
                             "done", "for", "while", "until", "case", "esac", "EOF") or w[:2] in (["command", "-v"], ["command", "-V"]):
            continue  # nothing ran (command -v only looks a program up)
        if _depth < 8 and os.path.basename(w[0]) in SHELLS and len(w) > 2 and w[1] in ("-c", "-lc"):
            return cmd_title(" ".join(w[2:]), _depth + 1)
        w = _strip_runners(w)
        prog = os.path.basename(w[0])
        if prog in ("claude", "codex"):  # a subcommand only right after it, unquoted: any other word is the prompt
            m = re.search(r"(?:^|[\s/])" + prog + r"\s+(" + "|".join(sorted(AGENT_SUBS)) + r")(?=\s|$)", bare)  # quotes blanked
            return "Ran " + prog + (" " + m.group(1) if m else "")
        return short(" ".join(["Ran", prog] + _extract_sub(prog, w)), 60)
    return "Ran a shell command"


def step_title(name, inp):
    """A tool call as the one-line step the activity list shows: (title, detail)."""
    p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
    if name in ("Bash", "bash"):
        return inp.get("description") or cmd_title(inp.get("command", "")), inp.get("command", "")
    if name.lower() in ("todowrite", "update_plan"):
        return "Updated the plan", ""
    if name == "ToolSearch":
        return "Looked up tools", inp.get("query", "")
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


def clip(s, n=4000, tail=False):
    """Verbose activity's text: newlines kept, capped to the head (an input) or the tail (an output, where errors land)."""
    s = str(s or "").strip("\n")
    if len(s) <= n:
        return s
    return "…" + s[-(n - 1):] if tail else s[: n - 1] + "…"


def full_input(name, inp):
    """A tool call's whole input for verbose activity: a shell command as is, anything else as its JSON."""
    if name in ("Bash", "bash") and isinstance(inp.get("command"), str):
        return clip(inp["command"])
    return clip(json.dumps(inp, ensure_ascii=False)) if inp else ""


def call(name, inp):
    """A tool call the way Claude Code prints it: Bash(git status), Read(scripts/chat.py)."""
    a = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("query") \
        or inp.get("url") or inp.get("skill") or inp.get("description") or next((v for v in inp.values() if isinstance(v, str)), "")
    return f"{name.split('__')[-1]}({short(a, 100)})"


def codex_base(thread):
    """The thread's (input, output) token totals so far, from its rollout's last token_count; None if unread."""
    try:
        f = max(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", f"rollout-*-{glob.escape(thread)}.jsonl")), key=os.path.getmtime)
        with open(f, "rb") as fh:
            fh.seek(max(0, os.path.getsize(f) - (1 << 20)))
            for line in reversed(fh.read().splitlines()):
                if b'"token_count"' in line:
                    with contextlib.suppress(ValueError):
                        t = ((json.loads(line).get("payload") or {}).get("info") or {}).get("total_token_usage") or {}
                        if isinstance(t.get("input_tokens"), int):
                            return t["input_tokens"], t.get("output_tokens") or 0
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None


def codex_usage(thread, total=None):
    """(context, window) from the thread's rollout log, else (None, None).
    total: turn.completed's input_tokens. The token_count must be this turn's (its running total equals it), not one still in Codex's write queue.
    `codex exec --json`'s turn.completed carries the thread's cumulative input, not the context; the rollout's last
    token_count has the latest request's (last_token_usage, what Codex's own footer uses) and the usable window.
    ponytail: the rollout is Codex's internal format; anything unexpected falls back to tokens-only, never a wrong %."""
    if not thread:
        return None, None
    try:
        f = max(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", f"rollout-*-{glob.escape(thread)}.jsonl")), key=os.path.getmtime)
        with open(f, "rb") as fh:
            fh.seek(max(0, os.path.getsize(f) - (1 << 20)))  # the last MB: its last token_count
            lines = fh.read().splitlines()
        for line in reversed(lines):
            if b'"token_count"' not in line:
                continue
            try:
                p = json.loads(line).get("payload") or {}
            except ValueError:
                continue  # the tail's cut first line
            info = p.get("info") if p.get("type") == "token_count" else None
            if info and (info.get("last_token_usage") or {}).get("total_tokens"):
                if total is not None and (info.get("total_token_usage") or {}).get("input_tokens") != total:
                    return None, None  # the previous turn's: this one's isn't on disk yet
                w = info.get("model_context_window")
                return info["last_token_usage"]["total_tokens"], w if isinstance(w, int) and w > 0 else None
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None, None


class Norm:
    """Each harness's JSON lines → the app's events."""

    def __init__(self, verbose=False):
        self.verbose = verbose  # chat.py send --verbose: whole inputs/outputs on steps, and think events
        self.msg_t = None  # current API message start, for tok/s
        self.chars = 0
        self.out, self.msg_out = 0, 0  # output tokens: finished messages, the current one
        self.last_tps = 0.0
        self.context = 0
        self.window = None
        self.tin = self.tout = 0  # the turn's tokens, summed over its requests: for diag's chat_turn and automation
        self.cost = None  # the turn's USD, from the harnesses that report one (Claude)
        self.base = (0, 0)  # codex: the thread's (in, out) totals before this turn, as its usage is the thread's running total; None: unknown
        self.thread = None  # codex's thread id, for its rollout log
        self.text = ""  # last text block, the fallback reply
        self.reply = None  # what the turn ended on, when it ended well: a question if it closes with an Options line
        self.cmds = {}  # Bash tool_use id → command, so a background shell can be found and stopped
        self.shells = {}  # task_id → {id, title, cmd, started}
        self.running = []  # (id, title) of steps started and not yet done, oldest first
        self.musing = None  # the phrase for the current stretch of thinking; None once anything else shows
        self.mused = None  # the last one, so the next stretch picks a different phrase
        self.plan_t0 = self.plan_last = None  # the plan's first sight, and when an item last got done (this run's evidence of pace)
        self.plan_k = self.plan_base = self.plan_obs = 0  # items done now, done when first seen (untimed), updates that finished some
        self.plan_keys = None  # its items, so a replaced list starts its pace over
        self.gem_open = False  # gemini: an assistant reply block is streaming (its deltas append to self.text)

    def more(self, e, **kw):
        """Verbose only: add the non-empty extras to an event; the concise stream stays byte-for-byte what it was."""
        if self.verbose:
            e.update({k: v for k, v in kw.items() if v})
        return e

    def plan(self, items):
        """The agent's own task list (TodoWrite, codex's todo_list), as (item, status, label now) → a plan event: done of total,
        the item on now, and a time left only from this run's pace: items seen getting done since the plan appeared, over two
        or more updates (one update ticking off a batch is one observation), counted down from the last one to now."""
        n, k, t, keys = len(items), sum(1 for _, st, _ in items if st == "completed"), time.time(), [x for x, _, _ in items]
        if self.plan_t0 is None or k < self.plan_k or keys != self.plan_keys:  # a new plan, or one rewritten: its pace starts over
            self.plan_t0, self.plan_base, self.plan_last, self.plan_obs, self.plan_keys = t, k, None, 0, keys
        elif k > self.plan_k:
            self.plan_last, self.plan_obs = t, self.plan_obs + 1
        self.plan_k = k
        on = next((now or x for x, st, now in items if st == "in_progress"), None) or next((x for x, st, _ in items if st != "completed"), "")
        eta = None
        if self.plan_obs >= 2 and k < n:
            eta = round((self.plan_last - self.plan_t0) / (k - self.plan_base) * (n - k) - (t - self.plan_last))
            eta = eta if eta > 0 else None  # run past its pace: no estimate
        return [{"e": "plan", "done": k, "total": n, "title": short(on, 80), "eta": eta}]

    def todos(self, todos):
        """TodoWrite's (claude, opencode) todos: the active one by its present-tense form."""
        return self.plan([(x.get("content") or "", x.get("status", ""), x.get("activeForm")) for x in todos or []
                          if isinstance(x, dict) and x.get("status") != "cancelled"])  # opencode's: dropped work isn't left to do

    def think(self, s):
        return [{"e": "think", "delta": s}] if self.verbose and s else []

    def phrase(self, evs):
        """Show thinking as one playful phrase per stretch: the same phrase while it lasts, a fresh one the next time."""
        for e in evs:
            if e["e"] == "status" and e["text"] in THINK_STATUS:
                if self.musing is None:
                    self.musing = random.choice([p for p in THINKING if p != self.mused])
                    self.mused = self.musing
                e["text"], e["thinking"] = self.musing, True  # the app's byline says what it's really doing, not the phrase
            elif e["e"] in ("status", "step", "text", "text_reset", "done"):  # the status moved on (usage and bg don't show)
                self.musing = None
        return evs

    def track(self, evs):
        """Keep the status on what's happening now: a step's title only while it runs, then what comes next.
        The app shows a step's title until another status arrives, so without this it stuck after the step ended."""
        out = []
        for e in evs:
            out.append(e)
            if e["e"] == "step":
                self.running.append((e["id"], e["title"]))
            elif e["e"] == "step_done":
                self.running = [s for s in self.running if s[0] != e["id"]]
                out.append({"e": "status", "text": self.running[-1][1] if self.running else "Thinking"})  # parallel steps: the newest still going
            elif e["e"] == "text_reset":
                out.append({"e": "status", "text": "Writing a reply"})
            elif e["e"] == "done":
                self.running = []
        return out

    @staticmethod
    def whole(text):
        """A whole message (codex, opencode) as text events. whole=True: voice mode doesn't speak it as it comes, since it can't tell a
        progress note ("I'll check the board.") from the reply yet; at done it speaks the reply, the message the turn ended on.
        It used to speak every note, then the reply, which restates them: the user heard the same phrase two or three times."""
        return [{"e": "text_reset"}, {"e": "text", "delta": text, "whole": True}]

    def usage(self, u):
        if not isinstance(u, dict):
            return 0
        return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)

    def claude(self, d):
        t, out = d.get("type"), []
        if t == "system" and d.get("subtype") == "status" and d.get("status"):
            out.append({"e": "status", "text": str(d["status"]).capitalize()})
        elif t == "system" and d.get("subtype") == "task_started" and d.get("is_backgrounded") and d.get("task_id"):
            self.shells[d["task_id"]] = {"id": d["task_id"], "title": d.get("description") or "Background shell",
                                         "cmd": self.cmds.get(d.get("tool_use_id"), ""), "started": time.time()}
            out.append({"e": "bg", "shells": list(self.shells.values())})
        elif t == "system" and d.get("subtype") == "background_tasks_changed":
            live = {x["task_id"] for x in (d.get("tasks") or []) if isinstance(x, dict) and "task_id" in x}
            self.shells = {k: v for k, v in self.shells.items() if k in live}
            out.append({"e": "bg", "shells": list(self.shells.values())})
        elif t == "system" and d.get("subtype") == "compact_boundary":
            self.context = (d.get("compact_metadata") or {}).get("post_tokens") or 0
            out.append({"e": "usage", "context": self.context})
        elif t == "stream_event" and isinstance(d.get("event"), dict):
            ev = d["event"]
            if ev.get("type") == "message_start":
                self.msg_t, self.chars = time.time(), 0
                self.out, self.msg_out = self.out + self.msg_out, 0
                self.context = self.usage((ev.get("message") or {}).get("usage")) or self.context
                out.append({"e": "usage", "context": self.context})
            elif ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "text":
                out.append({"e": "text_reset"})
            elif ev.get("type") == "content_block_delta" and isinstance(ev.get("delta"), dict):
                dl = ev["delta"]
                s = dl.get("text") or dl.get("thinking") or dl.get("partial_json") or ""
                self.chars += len(s)
                if dl.get("type") == "text_delta":
                    out.append({"e": "text", "delta": s})
                elif dl.get("type") == "thinking_delta":
                    out.append({"e": "status", "text": "Thinking"})
                    out += self.think(s)
                if self.msg_t and time.time() - self.last_tps > 0.5:  # live estimate, ~4 chars a token
                    self.last_tps = time.time()
                    out.append({"e": "usage", "tps": round(self.chars / 4 / max(time.time() - self.msg_t, 0.2), 1),
                                "out": self.out + max(self.msg_out, self.chars // 4)})
            elif ev.get("type") == "message_delta" and self.msg_t:
                n = self.msg_out = (ev.get("usage") or {}).get("output_tokens") or 0
                out.append({"e": "usage", "tps": round(n / max(time.time() - self.msg_t, 0.2), 1), "out": self.out + n})
        elif t == "assistant":
            for c in (d.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    raw_inp = c.get("input")
                    inp: dict = raw_inp if isinstance(raw_inp, dict) else {}
                    cid, cname = c.get("id") or "", c.get("name") or "tool"
                    self.cmds[cid] = inp.get("command") or ""
                    title, detail = step_title(cname, inp)
                    out.append(self.more({"e": "step", "id": cid, "title": short(title, 80), "detail": short(detail), "call": call(cname, inp)},
                                         input=full_input(cname, inp)))
                    if cname == "TodoWrite" and not d.get("parent_tool_use_id"):  # a subagent's list isn't the turn's plan
                        out += self.todos(inp.get("todos"))
                elif c.get("type") == "text":
                    self.text = c.get("text") or ""
        elif t == "user" and isinstance((d.get("message") or {}).get("content"), list):
            for c in d["message"]["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    res = c.get("content")
                    if isinstance(res, list):
                        res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                    out.append(self.more({"e": "step_done", "id": c.get("tool_use_id"), "ok": not c.get("is_error"), "out": short(res or "", 120)}, full=clip(res, tail=True)))
        elif t == "result":
            mu = next(iter((d.get("modelUsage") or {}).values()), {})
            self.window = (mu.get("contextWindow") if isinstance(mu, dict) else None) or self.window
            u = d.get("usage") or {}
            self.tin, self.tout = self.tin + self.usage(u), self.tout + ((u.get("output_tokens") or 0) if isinstance(u, dict) else 0)  # +=: a retry's run adds to the first's
            if isinstance(d.get("total_cost_usd"), int | float) and not isinstance(d.get("total_cost_usd"), bool):
                self.cost = (self.cost or 0) + d["total_cost_usd"]
            out.append({"e": "done", "ok": not d.get("is_error"), "text": d.get("result") or self.text,
                        "context": self.context, "window": self.window, "ms": d.get("duration_ms")})
        return out

    def codex(self, d):
        t = d.get("type")
        raw_it = d.get("item")
        it: dict = raw_it if isinstance(raw_it, dict) else {}
        out: list = []
        if t == "thread.started":
            self.thread = d.get("thread_id") or self.thread
        elif t == "item.started" and it.get("type") == "command_execution":
            if not it.get("command") or it.get("id") is None:
                return out
            cmd = str(it["command"]).split(" -lc ", 1)[-1].strip("'")
            out.append(self.more({"e": "step", "id": it["id"], "title": cmd_title(cmd), "detail": short(cmd), "call": f"Bash({short(cmd, 100)})"}, input=clip(cmd)))
        elif t == "item.completed" and it.get("type") == "command_execution":
            out.append(self.more({"e": "step_done", "id": it.get("id"), "ok": it.get("exit_code") == 0, "out": short(it.get("aggregated_output") or "", 120)},
                                 full=clip(it.get("aggregated_output"), tail=True)))
        elif t == "item.completed" and it.get("type") == "reasoning":
            out += self.think(it.get("text") or "")
        elif t == "item.completed" and it.get("type") == "agent_message":
            self.text = it.get("text") or ""
            out += self.whole(self.text)
        elif it.get("type") == "todo_list" and t in ("item.started", "item.updated", "item.completed"):
            out += self.plan([(x.get("text", ""), "completed" if x.get("completed") else "", None) for x in it.get("items") or [] if isinstance(x, dict)])
        elif it.get("type") in ("mcp_tool_call", "web_search") and t in ("item.started", "item.completed"):
            mcp = it["type"] == "mcp_tool_call"
            title, detail = ((it.get("tool") or "tool").replace("_", " "), it.get("server") or "") if mcp else ("Searched the web", it.get("query") or "")
            if t == "item.started":
                out.append({"e": "step", "id": it.get("id"), "title": short(title, 80), "detail": short(detail)})
            else:
                res = it.get("error") or it.get("result") or ""
                out.append({"e": "step_done", "id": it.get("id"), "ok": it.get("status") != "failed" and not it.get("error"),
                            "out": short(res if isinstance(res, str) else json.dumps(res), 120)})
        elif t == "item.completed" and it.get("type") == "file_change":
            names = [os.path.basename(c.get("path", "")) for c in it.get("changes") or [] if isinstance(c, dict)]
            out += [{"e": "step", "id": it.get("id"), "title": short("Edited " + (", ".join(names) or "files"), 80), "detail": ""},
                    {"e": "step_done", "id": it.get("id"), "ok": it.get("status") != "failed", "out": ""}]
        elif t == "item.started":
            out.append({"e": "status", "text": str(it.get("type") or "working").replace("_", " ").capitalize()})
        elif t == "error" and str(d.get("message") or "").startswith("Reconnecting"):  # a retry, not the end
            out.append({"e": "status", "text": short(d["message"], 80)})
        elif t == "turn.completed":
            u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
            total = u.get("input_tokens")
            if self.base is not None and total and total >= self.base[0]:  # less: another thread's rollout
                self.tin, self.tout = total - self.base[0], (u.get("output_tokens") or 0) - self.base[1]
            ctx, self.window = codex_usage(self.thread, total)
            if ctx is None and self.thread:  # Codex writes its rollout from a queue: give the last token_count a moment
                time.sleep(0.1)
                ctx, self.window = codex_usage(self.thread, total)
            self.context = ctx or 0  # unread: 0, so the app keeps its last reading (input_tokens is the running total, not the context)
            out.append({"e": "done", "ok": True, "text": self.text, "context": self.context, "window": self.window, "ms": None})
        elif t in ("turn.failed", "error"):
            out.append({"e": "done", "ok": False, "text": str(d.get("error") or d.get("message")), "context": self.context, "window": self.window, "ms": None})
        return out

    def opencode(self, d):
        """`opencode run --format json`: whole parts, a tool only once it finished; no final event (run() adds the done)."""
        t, pt, out = d.get("type"), d.get("part") or {}, []
        if t == "tool_use":
            st = pt.get("state") or {}
            title, detail = step_title(pt.get("tool", "tool").capitalize(), st.get("input") or {})
            res = str(st.get("output") or st.get("error") or "")
            out += [self.more({"e": "step", "id": pt.get("id"), "title": short(st.get("title") or title, 80), "detail": short(detail)},
                              input=full_input(pt.get("tool", ""), st.get("input") or {})),
                    self.more({"e": "step_done", "id": pt.get("id"), "ok": st.get("status") == "completed", "out": short(res, 120)}, full=clip(res, tail=True))]
            if pt.get("tool") == "todowrite":
                out += self.todos((st.get("input") or {}).get("todos"))
        elif t == "text":
            self.text = pt.get("text", "")
            out += self.whole(self.text)
        elif t == "reasoning":
            out.append({"e": "status", "text": "Thinking"})
            out += self.think(pt.get("text", ""))
        elif t == "step_finish":
            tk = pt.get("tokens") or {}
            self.context = tk.get("input", 0) + (tk.get("cache") or {}).get("read", 0) + (tk.get("cache") or {}).get("write", 0) or self.context
            self.tin, self.tout = self.tin + (tk.get("input", 0) + (tk.get("cache") or {}).get("read", 0) + (tk.get("cache") or {}).get("write", 0)), self.tout + tk.get("output", 0)
            out.append({"e": "usage", "context": self.context})
        elif t == "error":
            e = d.get("error") or {}
            out.append({"e": "done", "ok": False, "text": str((e.get("data") or {}).get("message") or e.get("name") or e),
                        "context": self.context, "window": None, "ms": None})
        return out

    def gemini(self, d):
        """`gemini --output-format stream-json`: init, message (assistant deltas), tool_use, tool_result, error, result."""
        t, out = d.get("type"), []
        if t == "init":  # a new run (turn()'s retries reuse this Norm): its first delta starts a fresh reply
            self.gem_open = False
        elif t == "message" and d.get("role") == "assistant":
            s = d.get("content") or ""
            if not d.get("delta") or not self.gem_open:  # a new reply, or a whole one
                self.text, self.gem_open = "", True
                out.append({"e": "text_reset"})
            self.text += s
            out.append({"e": "text", "delta": s})
        elif t == "tool_use":
            self.gem_open = False  # text after a tool is the next reply block
            name, inp = d.get("tool_name") or "tool", d.get("parameters") or {}
            if isinstance(inp.get("include"), list):  # read_many_files: its paths, where attach.mentions and the step can see them
                inp = {**inp, "path": " ".join(map(str, inp["include"]))}
            name = GEMINI_TOOLS.get(name, name)
            title, detail = step_title(name, inp)
            out.append(self.more({"e": "step", "id": d.get("tool_id"), "title": short(title, 80), "detail": short(detail), "call": call(name, inp)},
                                 input=full_input(name, inp)))
        elif t == "tool_result":
            r = str(d.get("output") or (d.get("error") or {}).get("message") or "")
            out.append(self.more({"e": "step_done", "id": d.get("tool_id"), "ok": d.get("status") == "success", "out": short(r, 120)}, full=clip(r, tail=True)))
        elif t == "error" and d.get("severity") != "error":  # a warning: the turn goes on
            out.append({"e": "status", "text": short(d.get("message", ""), 80)})
        elif t == "error":
            self.error = d.get("message")
        elif t == "result":
            st = d.get("stats") or {}  # its input_tokens sum every request of the run, so they aren't the context size: none shown
            self.tin, self.tout = self.tin + (st.get("input_tokens") or 0), self.tout + (st.get("output_tokens") or 0)
            ok = d.get("status") == "success"
            text = self.text if ok else (d.get("error") or {}).get("message") or getattr(self, "error", None) or self.text or "Gemini CLI failed"
            out.append({"e": "done", "ok": ok, "text": text, "context": self.context, "window": None, "ms": st.get("duration_ms")})
        return out

    def agy(self, d):
        """`agy --print --output-format stream-json`: streamed reply deltas and a final result."""
        event, out = d.get("event"), []
        step = d.get("step_update") or {}
        if event == "init":
            self.gem_open = False
        elif event == "step_update" and step.get("step_type") == "agent_response":
            delta = step.get("text_delta") or ""
            if delta:
                if not self.gem_open:
                    self.text, self.gem_open = "", True
                    out.append({"e": "text_reset"})
                self.text += delta
                out.append({"e": "text", "delta": delta})
            usage = step.get("usage") or {}
            if usage:
                self.context = usage.get("input_tokens", 0) + usage.get("cache_read_tokens", 0) or self.context
                self.tin, self.tout = self.tin + usage.get("input_tokens", 0) + usage.get("cache_read_tokens", 0), self.tout + usage.get("output_tokens", 0)
                out.append({"e": "usage", "context": self.context})
        elif event == "step_update" and step.get("step_type") == "tool":
            self.gem_open = False
            info = step.get("tool_info") or {}
            name, args = step.get("tool_name") or info.get("name") or "tool", info.get("parameters") or {}
            args = {"command": args.get("CommandLine", "")} if name == "run_command" else args
            title, detail = step_title("Bash" if name == "run_command" else name, args)
            ident = f"agy-{step.get('step_index')}"
            if step.get("state") == "ACTIVE":
                out.append(self.more({"e": "step", "id": ident, "title": short(title, 80), "detail": short(detail or json.dumps(args)),
                                      "call": call("Bash" if name == "run_command" else name, args)}, input=clip(json.dumps(args))))
            elif step.get("state") in ("DONE", "ERROR"):
                result = str(info.get("output") or info.get("error") or "")
                ok = step.get("state") == "DONE" and not info.get("error") and (name != "run_command" or "exited with code 0" in result.lower())
                out.append(self.more({"e": "step_done", "id": ident, "ok": ok,
                                      "out": short(result, 120)}, full=clip(result, tail=True)))
        elif event == "result":
            result = d.get("result") or {}
            denied = result.get("denied_actions") or []
            ok = result.get("status") == "SUCCESS" and not denied
            reply = result.get("response") or self.text
            duration = result.get("duration_seconds")
            reason = result.get("error") or ("agy was denied permission for " + ", ".join(str(x.get("action") or "a tool") for x in denied) if denied else "")
            out.append({"e": "done", "ok": ok, "text": reply if ok else reason or reply or "agy failed",
                        "context": self.context, "window": None, "ms": round(duration * 1000) if isinstance(duration, (int, float)) else None})
        elif event == "error":
            out.append({"e": "status", "text": short(str(d.get("message") or d.get("error") or "agy error"), 80)})
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
            out += self.think(ev.get("delta", ""))
        elif t == "message_end" and m.get("role") == "assistant":
            u = m.get("usage") or {}
            self.context = u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0) or self.context
            self.tin, self.tout = self.tin + u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0), self.tout + u.get("output", 0)
            self.text = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text") or self.text
            if m.get("stopReason") in ("error", "aborted"):
                self.error = m.get("errorMessage") or m["stopReason"]
            out.append({"e": "usage", "context": self.context})
        elif t == "tool_execution_start":
            title, detail = step_title(d.get("toolName", "tool").capitalize(), d.get("args") or {})
            out.append(self.more({"e": "step", "id": d.get("toolCallId"), "title": short(title, 80), "detail": short(detail)},
                                 input=full_input(d.get("toolName", ""), d.get("args") or {})))
        elif t == "tool_execution_end":
            r = d.get("result")
            r = " ".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict)) if isinstance(r, dict) else str(r or "")
            out.append(self.more({"e": "step_done", "id": d.get("toolCallId"), "ok": not d.get("isError"), "out": short(r, 120)}, full=clip(r, tail=True)))
        elif t == "agent_end":
            err = getattr(self, "error", None)
            out.append({"e": "done", "ok": not err, "text": err or self.text, "context": self.context, "window": None, "ms": None})
        return out


def session_path(engine):
    base = "session" if engine == "claude" else f"session_{engine}"
    return os.path.join(DIR, base + scope_suffix())


SCOPE_EXPECTED = None
SCOPE_SNAPSHOT = None
SCOPE_DIR = None
SCOPE_TURN_TOKEN = None
GENERATION = "conversation.gen"
ACTIVE_TURN = "turn.active"
RESETTING = "conversation.reset"
QUESTION_DELIVERY = "question.delivery"


def generation_unlocked():
    path = os.path.join(DIR, GENERATION)
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        value = hashlib.sha1(os.urandom(32)).hexdigest()
        with open(path, "w") as fh:
            fh.write(value)
        return value


def rotate_generation_unlocked():
    value = hashlib.sha1(os.urandom(32)).hexdigest()
    with open(os.path.join(DIR, GENERATION), "w") as fh:
        fh.write(value)
    return value


def cancel_running_unlocked():
    rec = pid_record()
    if pid_matches(rec, group=True):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(rec["pid"], signal.SIGTERM)
    with contextlib.suppress(OSError):
        os.remove(PIDFILE)
    with contextlib.suppress(OSError):
        os.remove(BGFILE)


def pid_record():
    try:
        with open(PIDFILE) as fh:
            raw = json.load(fh)
        if isinstance(raw, int):  # old marker: readable for cleanup, never trusted for signaling
            return {"pid": raw, "birth": None}
        return {"pid": int(raw["pid"]), "birth": raw["birth"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pid_matches(rec, group=False):
    if not rec or not rec.get("birth") or auto.process_start(rec["pid"]) != rec["birth"]:
        return False
    try:
        return not group or os.getpgid(rec["pid"]) == rec["pid"]
    except ProcessLookupError:
        return False


@contextlib.contextmanager
def scope_lock():
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, "conversation.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def scope_current_unlocked():
    if os.path.exists(os.path.join(DIR, RESETTING)):
        return False
    if SCOPE_TURN_TOKEN and SCOPE_DIR == DIR:
        try:
            with open(os.path.join(DIR, ACTIVE_TURN)) as af:
                active = json.load(af)
            if active.get("id") != SCOPE_TURN_TOKEN or active.get("cancelled"):
                return False
        except (OSError, ValueError):
            return False
    if SCOPE_EXPECTED is None or SCOPE_DIR != DIR:
        return True
    return generation_unlocked() == SCOPE_EXPECTED[0]


def check_session_scope(engine, snapshot=None):
    global SCOPE_EXPECTED, SCOPE_SNAPSHOT, SCOPE_DIR
    with scope_lock():
        SCOPE_DIR = DIR
        SCOPE_EXPECTED = (generation_unlocked(), None)
        if not os.environ.get("GREENLIGHT_ONLY"):
            SCOPE_SNAPSHOT = None
            return False
        if snapshot is None:
            import dash
            snapshot = dash.board(with_ids=True)
        SCOPE_SNAPSHOT = snapshot
        return False


def stale_pending_question(snapshot):
    """A yes/no answer may only settle a chat question about the current request head and target."""
    if snapshot is None:
        return False
    allow = snapshot[1]
    current = {identity[6]: identity for identity in allow.values() if isinstance(identity, tuple) and len(identity) > 6}
    backfilled = set()
    try:
        with open(os.path.join(os.environ.get("GREENLIGHT_HOME") or os.path.expanduser("~/.greenlight"), "last.json")) as fh:
            rows = json.load(fh)["rows"]
        for row in rows:
            mr = row.get("mr") or {}
            if row.get("_goal_backfill"):
                backfilled.add(row.get("id"))
            current.setdefault(row.get("id"), (row.get("forge") or "", row.get("project") or "", mr.get("ref") or "",
                                               mr.get("sha") or "", row.get("goal") or "", mr.get("target_branch") or "",
                                               row.get("id"), row.get("branch") or ""))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for rec in decisions.load():
        if rec.get("via") != "chat" or rec.get("chat") != chat_identity() or not decisions.is_open(rec):
            continue
        for target in [rec] + (rec.get("workspaces") or []):
            if not target.get("worktree_id"):
                continue
            identity = current.get(target.get("worktree_id"))
            if identity is None or (target.get("mr") or "") != identity[2] or \
                    (target.get("sha") or "") != identity[3] or \
                    (target.get("target_branch") or "") != identity[5] or \
                    target.get("repo") not in (None, identity[1]) or target.get("forge") not in (None, identity[0]) or \
                    (target.get("goal") is not None and target.get("goal") != identity[4] and
                     not (target.get("goal") == "" and target.get("worktree_id") in backfilled)) or \
                    (target.get("branch") is not None and len(identity) > 7 and target["branch"] != identity[7]):
                return True
    return False


def has_request_question():
    return any(rec.get("via") == "chat" and rec.get("chat") == chat_identity() and decisions.is_open(rec) and
               any(t.get("worktree_id") for t in [rec] + (rec.get("workspaces") or []))
               for rec in decisions.load())


def clear_stale_question_unlocked():
    """Close a stale or undelivered chat question and drop `asked`, keeping the chat session intact."""
    decisions.forget_chat(chat=chat_identity())
    with contextlib.suppress(OSError):
        os.remove(asked_path())


def stale_question_notice(reason):
    """Prompt notice injected into the existing session when a prior pending question is invalidated."""
    return (
        f"STALE QUESTION ({reason}): Your previous pending question has been closed. "
        "Do NOT treat the user's message below as approval for the current commit, target branch, branch, or goal. "
        "If they are answering that old question, explain what changed and ask again; "
        "if they moved on to a new topic, answer it normally.\n\n"
    )


def question_targets(question, allow):
    """Select only the cards explicitly named by a question; untargeted questions bind to no workspace."""
    hits = {}
    for name, identity in allow.items():
        ref = identity[2] if isinstance(identity, tuple) and len(identity) > 2 else ""
        display = name.split("@id-")[0]
        if (ref and re.search(rf"(?<![\w#!]){re.escape(ref)}(?!\d)", question)) or \
                (len(display) > 3 and re.search(rf"(?<![\w/.-]){re.escape(display)}(?![\w/-])", question)):
            hits[name] = identity
    return hits


def live_question_status(snapshot):
    """Check head-bound questions against the forge, including pushes since the last scan."""
    allow = snapshot[1]
    current = {identity[6]: identity for identity in allow.values() if isinstance(identity, tuple) and len(identity) > 6}
    open_questions = [rec for rec in decisions.load() if rec.get("via") == "chat" and rec.get("chat") == chat_identity() and decisions.is_open(rec)]
    live_targets = [target for rec in open_questions for target in [rec] + (rec.get("workspaces") or [])
                    if target.get("worktree_id") and (target.get("branch") is not None or target.get("goal") is not None)]
    if live_targets:
        try:
            import greenlight
            live = {w["worktreeId"]: w for w in greenlight.orca("worktree", "ps", "--limit", "10000")["worktrees"]}
        except Exception:
            return "unavailable"
        for target in live_targets:
            w = live.get(target["worktree_id"])
            if w is None or target.get("branch") is not None and \
                    (w.get("branch") or "").removeprefix("refs/heads/") != target["branch"]:
                return "stale"
            comment = (w.get("comment") or "").strip()
            if target.get("goal") is not None and comment and not greenlight.STATUS_LINE_RE.search(comment) and comment != target["goal"]:
                return "stale"
            if target.get("goal") and not comment:
                try:
                    scan_rows = json.load(open(greenlight.STATE / "last.json"))["rows"]
                except Exception:
                    return "unavailable"
                if not any(r.get("id") == target["worktree_id"] and r.get("_goal_backfill") for r in scan_rows):
                    return "stale"
            link_key = "linkedPR" if target.get("forge") == "github" else "linkedGitLabMR"
            if target.get("mr") and link_key in w:
                linked = greenlight.linked_number(w[link_key])
                if (f"#{linked}" if link_key == "linkedPR" else f"!{linked}") != target["mr"]:
                    return "stale"
    for rec in open_questions:
        for target in [rec] + (rec.get("workspaces") or []):
            if not target.get("sha") or not target.get("mr"):
                continue
            identity = current.get(target.get("worktree_id"))
            forge = target.get("forge") or (identity or (None,))[0]
            project = target.get("repo") or (identity or (None, None))[1]
            ref = target["mr"]
            if forge not in ("github", "gitlab") or not project or not re.fullmatch(r"[#!]\d+", ref):
                return "unavailable"
            try:
                import greenlight
                live = greenlight.mr_details(project, int(ref[1:]), forge)
            except Exception:
                return "unavailable"
            # a question about an already-merged MR (cleanup) stays valid once merged; one asked while open does not
            if live.get("sha") != target["sha"] or (live.get("target_branch") or "") != (target.get("target_branch") or "") or \
                    live.get("state") not in ("opened", "open") and target.get("step") != "done":
                return "stale"
    return "valid"


def session(engine, val=None):
    f = session_path(engine)
    if val is not None:
        with scope_lock():
            if scope_current_unlocked():
                open(f, "w").write(val)
    return open(f).read().strip() if os.path.exists(f) else None


def cleanup_run(pid, birth=None):
    """Only the process registered in this chat may remove the shared PID marker."""
    with scope_lock():
        rec = pid_record()
        owned = bool(rec and rec["pid"] == pid and (birth is None or rec["birth"] == birth))
        if owned:
            os.remove(PIDFILE)
            with contextlib.suppress(OSError):
                os.remove(BGFILE)


def run(cmd, engine, text, norm, check=False, guard=None, defer_done=False, request_guard=None):
    """Run one turn, stream events. Returns (sid or None, error or None or "retry").
    check: a done whose reply ignored the user (IGNORED) isn't emitted; the process is killed and "retry" returned."""
    with scope_lock():
        if not scope_current_unlocked():
            return None, "Stopped"
        if pid_matches(pid_record()):
            return None, "another turn running"
        with contextlib.suppress(OSError):
            os.remove(PIDFILE)
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=DIR, start_new_session=True)
        birth = auto.process_start(p.pid)
        if not birth:
            p.terminate()
            p.wait()
            for s in (p.stdin, p.stdout, p.stderr):
                with contextlib.suppress(OSError):
                    if s:
                        s.close()
            return None, "harness process identity unavailable"
        with open(PIDFILE, "w") as pf:
            pf.write(json.dumps({"pid": p.pid, "birth": birth}))
    stderr_buf = []

    def _drain_stderr():
        if not p.stderr:
            return
        total = 0
        try:
            while chunk := p.stderr.read(4096):
                if total < 65536:
                    stderr_buf.append(chunk[:65536 - total])
                    total += len(chunk)
        except (OSError, ValueError):
            pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    def _kill_child():
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(p.pid, signal.SIGTERM)

    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with contextlib.suppress(ValueError, OSError):
                prev_h = signal.getsignal(sig)

                def _on_sig(signum, frame, _prev=prev_h):
                    _kill_child()
                    raise SystemExit(128 + signum)

                old_handlers[sig] = prev_h
                signal.signal(sig, _on_sig)

    assert p.stdin is not None and p.stdout is not None
    try:
        try:
            p.stdin.write(text)
            p.stdin.close()
        except (OSError, ValueError):  # Clear may terminate the registered child before it reads the prompt
            with contextlib.suppress(OSError):
                p.stdin.close()
            p.wait()
            return None, "Stopped"
        if os.environ.get("GREENLIGHT_ONLY"):
            def watch_scope():
                while p.poll() is None:
                    with scope_lock():
                        stale = not scope_current_unlocked()
                    if stale:
                        _kill_child()
                        return
                    time.sleep(0.1)
            threading.Thread(target=watch_scope, daemon=True).start()
        sid, done, private, failed_done = None, False, set(), None
        for sid_, _ in norm.running:  # a rerun is a new process: steps the last one left open ended with it
            emit(e="step_done", id=sid_, ok=False, out="interrupted")
        if norm.plan_keys is not None:  # …and its plan and pace aren't this one's
            emit(e="plan", done=0, total=0, title="", eta=None)
        norm.plan_t0, norm.plan_keys, norm.running = None, None, []
        hiding, needles = guard or watch_for_attachments()
        for line in p.stdout:
            with scope_lock():
                stale = not scope_current_unlocked()
            if stale:
                _kill_child()
                p.wait()
                return sid, "Stopped"
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            raw_init, raw_res = d.get("init"), d.get("result")
            init: dict = raw_init if isinstance(raw_init, dict) else {}
            result: dict = raw_res if isinstance(raw_res, dict) else {}
            sid = d.get("conversation_id") or init.get("conversation_id") or result.get("conversation_id") or d.get("session_id") or d.get("thread_id") or d.get("sessionID") or (d.get("id") if d.get("type") == "session" else None) or sid
            raw = getattr(norm, engine)(d)
            for e in raw:  # before track(): the status it repeats a step's title in is the hidden one
                if hiding and e["e"] == "step" and attach.mentions(DIR, e):  # a call that reads the attachments: none of its output is shown
                    private.add(e["id"])
                elif hiding and e["e"] == "step_done" and e.get("id") in private:
                    e["out"] = attach.HIDDEN
                    e.pop("full", None)
                if hiding and e["e"] in ("step", "step_done", "plan"):  # anything else that shows an attachment's lines (a grep -R, a Write that copies one): those lines go
                    attach.hide(DIR, e, needles)
            evs = norm.phrase(norm.track(raw))  # track first, so its "Thinking" gets a phrase too
            if check and any(e["e"] == "done" and e["ok"] and IGNORED.match(e["text"] or "") for e in evs):
                _kill_child()
                p.wait()
                return sid, "retry"
            for e in evs:
                if e["e"] == "bg":  # not for the stream: `chat.py bg` serves it
                    for sh in e["shells"] if hiding else ():
                        attach.hide(DIR, sh, needles)  # its title is the step's: hidden as the step's is
                    tmp_bg = f"{BGFILE}.{os.getpid()}.tmp"
                    fd = os.open(tmp_bg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as bf:
                        json.dump(e["shells"], bf)
                    os.replace(tmp_bg, BGFILE)
                    continue
                if e["e"] == "done" and done:  # opencode can report one failure twice
                    continue
                if e["e"] == "done" and e["ok"] and sid:  # save only successful turns before the app hears done
                    session(engine, sid)
                if e["e"] == "done":
                    done, norm.reply = True, e["text"] if e["ok"] else None
                    if not e["ok"]:
                        failed_done = e
                if defer_done and e["e"] == "done":
                    norm.pending_done = e
                else:
                    emit(**e)
        p.wait()
        err_thread.join(timeout=1.0)
    finally:
        for sig, prev_h in old_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, prev_h)
        if hasattr(p, "poll") and p.poll() is None:
            _kill_child()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                p.wait(timeout=3)
        for s in (getattr(p, "stdin", None), getattr(p, "stdout", None), getattr(p, "stderr", None)):
            with contextlib.suppress(OSError, ValueError):
                if s:
                    s.close()
        cleanup_run(p.pid, birth)
    with scope_lock():
        if not scope_current_unlocked():
            return sid, "Stopped"
    if not done and not p.returncode:  # opencode has no final event: its clean exit is the done
        if sid:
            session(engine, sid)
        event = {"e": "done", "ok": True, "text": norm.text, "context": norm.context, "window": norm.window, "ms": None}
        if defer_done:
            norm.pending_done = event
        else:
            emit(**event)
        done, norm.reply = True, norm.text
    if not done and p.returncode:
        if p.returncode in (-15, 143):  # SIGTERM from `chat.py stop`
            return sid, "Stopped"
        return sid, "".join(stderr_buf).strip() or f"exit {p.returncode}"
    if failed_done:
        return sid, "model failed"
    return (None if done else sid), None  # done: its session is saved already


def watch_for_attachments():
    """(hide?, needles) for a turn, decided at its start: a Clear during it (even between a retry's runs) doesn't stop it;
    nothing attached, nothing hidden."""
    return os.path.isdir(attach.root(DIR)), attach.needles(DIR)


def agent_title(a):
    """An Orca agent's one-line task: its title, else its prompt without the paste wrapper or a /goal prefix."""
    t = a.get("taskTitle") or a.get("displayName") or a.get("prompt") or ""
    t = re.sub(r"</?pasted_content[^>]*>", "", t).strip()
    t = re.sub(r"^/goal\s+", "", t)
    return short(t.split("\n")[0], 300) or "Agent"  # the app truncates to fit its width


def background():
    """This chat's live background shells and every working Orca agent, for the app's footer chip."""
    shells = []
    if os.path.exists(BGFILE) and os.path.exists(PIDFILE):
        try:
            with open(BGFILE) as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                shells = [s for s in raw if isinstance(s, dict)]
        except (OSError, ValueError):
            shells = []
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
    shells = []
    if os.path.exists(BGFILE):
        try:
            with open(BGFILE) as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                shells = [s for s in raw if isinstance(s, dict)]
        except (OSError, ValueError):
            return False
    cmd = next((s.get("cmd") or "" for s in shells if s.get("id") == task_id), "")
    key = max(re.split(r"['\"\\]", cmd), key=len).strip()  # ponytail: longest quote-free chunk, quoting is rewritten on the way to the shell
    parent = pid_record()
    if len(key) < 4 or not pid_matches(parent):
        return False
    try:
        ps_out = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    rows = [l.split(None, 2) for l in ps_out.splitlines()]
    kids = {}
    for r in rows:
        if len(r) == 3 and r[0].isdigit() and r[1].isdigit():
            kids.setdefault(int(r[1]), []).append((int(r[0]), r[2]))
    args = {pid: a for v in kids.values() for pid, a in v}
    def tree(pid, seen=None):
        seen = set() if seen is None else seen
        if pid in seen:
            return []
        seen.add(pid)
        return [pid] + [x for k, _ in kids.get(pid, []) for x in tree(k, seen)]
    hits = [p for p in tree(parent["pid"])[1:] if key in args.get(p, "")]
    for p in {x for h in hits for x in tree(h)}:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return bool(hits)


def asked_question(reply):
    """The end of a reply that ends on a question (a `?` or the Options line, under the app's [hexes:] lines), else None:
    the question with the offer above it, "I can add a meter.\n\nWhere should it go?"."""
    text = re.sub(r"(\s*\[[a-z]+:[^\]\n]*\])+\s*\Z", "", reply or "").strip()
    paras = re.split(r"\n\s*\n", text)
    if not re.search(r"\?\s*\Z|^`?options:.*\Z", paras[-1], re.M | re.I):
        return None
    n = 3 if re.match(r"`?options:", paras[-1], re.I) else 2  # an Options paragraph of its own: the question above it too
    return "\n\n".join(paras[-n:])[-1000:]  # the question (and its Options) and the paragraph above, for an offer


def turn(text, model, effort, compact=False, name=None, verbose=False, files=None):
    """Reserve this chat through answer handling, harness work, and pending-question commit."""
    global SCOPE_EXPECTED, SCOPE_DIR, SCOPE_TURN_TOKEN
    path = os.path.join(DIR, ACTIVE_TURN)
    token = uuid.uuid4().hex
    with scope_lock():
        if os.path.exists(os.path.join(DIR, RESETTING)):
            return emit(e="done", ok=False, text="This chat's Clear was interrupted; clear it again before sending.",
                        context=0, window=None, ms=None)
        delivery = os.path.join(DIR, QUESTION_DELIVERY + scope_suffix())
        try:
            active = json.load(open(path))
            if active.get("birth") and auto.process_start(int(active["pid"])) == active["birth"]:
                return emit(e="done", ok=False, text="Another message is still running in this chat.", context=0, window=None, ms=None)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        birth = auto.process_start(os.getpid())
        if not birth:
            return emit(e="done", ok=False, text="This chat could not verify its process identity; retry this message.",
                        context=0, window=None, ms=None)
        TURN["stale_note"] = ""
        if not compact and os.path.exists(delivery):
            try:
                with open(delivery) as df:
                    delivery_state = df.read().strip()
                clear_stale_question_unlocked()
                os.remove(delivery)
            except OSError as e:
                return emit(e="done", ok=False, text=f"The interrupted question could not be closed ({e}); clear this chat.",
                            context=0, window=None, ms=None)
            reason = ("the previous question could not be verified or its target changed while it was composed"
                      if delivery_state == "stale" else "the previous reply was interrupted before its question reached the user")
            TURN["stale_note"] = stale_question_notice(reason)
        with open(path, "w") as af:
            af.write(json.dumps({"id": token, "pid": os.getpid(), "birth": birth}))
        reserved_gen = generation_unlocked()
        SCOPE_EXPECTED, SCOPE_DIR = (reserved_gen, None), DIR
        SCOPE_TURN_TOKEN = token
    terminal = None
    try:
        terminal = _turn_owned(text, model, effort, compact=compact, name=name, verbose=verbose, files=files,
                               reservation=(token, reserved_gen))
    finally:
        with scope_lock():
            owned = False
            try:
                active = json.load(open(path))
                if active.get("id") == token:
                    owned = not active.get("cancelled", False)
                    os.remove(path)
            except (OSError, ValueError):
                pass
            if SCOPE_TURN_TOKEN == token:
                SCOPE_TURN_TOKEN = None
            if terminal:
                if terminal.get("ok") and not owned:
                    terminal = {"e": "done", "ok": False, "text": "Stopped",
                                "context": 0, "window": None, "ms": None}
                emit(**terminal)
                try:
                    if open(delivery).read() == token:
                        os.remove(delivery)
                except OSError:
                    pass


def reservation_current(token, generation):
    with scope_lock():
        return reservation_current_unlocked(token, generation)


def reservation_current_unlocked(token, generation):
    try:
        return scope_current_unlocked() and generation_unlocked() == generation and \
            json.load(open(os.path.join(DIR, ACTIVE_TURN))).get("id") == token
    except (OSError, ValueError):
        return False


def _mark_question_stale_unlocked():
    """Leave a scoped marker so the next turn injects a STALE QUESTION notice for a dropped question."""
    with contextlib.suppress(OSError), open(os.path.join(DIR, QUESTION_DELIVERY + scope_suffix()), "w") as df:
        df.write("stale")


def _save_reply_question_unlocked(question, request_snapshot, reservation, kind):
    """Persist a fresh pending question if its named targets haven't moved; never wipe the chat session."""
    asked = asked_path()
    if request_snapshot is None:
        with contextlib.suppress(OSError):
            os.remove(asked)
        _mark_question_stale_unlocked()
        return
    try:
        import dash
        fresh = dash.board(with_ids=True)
        targets = question_targets(question, request_snapshot[1])
        if any(fresh[1].get(name) != identity for name, identity in targets.items()):
            with contextlib.suppress(OSError):
                os.remove(asked)
            _mark_question_stale_unlocked()
            return
        with open(os.path.join(DIR, QUESTION_DELIVERY + scope_suffix()), "w") as qdf:
            qdf.write(reservation[0] if reservation else "pending")
        with open(asked, "w") as af:
            af.write((os.environ.get("GREENLIGHT_REQUEST", "") + ASKED + question)[-16384:])
        target_ids = [identity[6] if isinstance(identity, tuple) and len(identity) > 6 and identity[6] else name
                      for name, identity in targets.items()]
        decisions.asked(question, via="chat", chat=chat_identity(), kind=kind, ws=target_ids, infer_ws=False)
        if stale_pending_question(request_snapshot):
            clear_stale_question_unlocked()
            _mark_question_stale_unlocked()
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(asked)
        try:
            decisions.forget_chat(chat=chat_identity())
        except OSError:
            with open(os.path.join(DIR, RESETTING), "w") as rf:
                rf.write("question recovery needed")
        _mark_question_stale_unlocked()


def _turn_owned(text, model, effort, compact=False, name=None, verbose=False, files=None, reservation=None):
    stale_note = str(TURN.pop("stale_note", "") or "")
    engine, model = harness.route(model)
    TURN.update(harness=engine, model=model, t=time.monotonic())
    stored = []
    if files:
        try:
            checked = attach.check(files)
            with scope_lock():
                if reservation and not scope_current_unlocked():
                    return dict(e="done", ok=False, text="Stopped",
                                context=0, window=None, ms=None)
                stored = attach.store(DIR, checked)
                attach.prune(DIR)
        except attach.Rejected as e:
            return dict(e="done", ok=False, text=str(e), context=0, window=None, ms=None)
        except OSError as e:
            return dict(e="done", ok=False, text=f"The attached files couldn't be saved where greenlight runs ({e.strerror or e}).", context=0, window=None, ms=None)
    elif os.path.isdir(attach.root(DIR)):
        with contextlib.suppress(OSError):
            attach.prune(DIR)
    prompt = PROMPT + (f" The user has named you {name} in the app; answer to that name." if name and name.lower() != "greenlight" else "")
    rules = os.path.expanduser(os.path.join(os.environ.get("GREENLIGHT_HOME") or "~/.greenlight", "instructions.md"))
    if os.path.exists(rules):  # the user's standing instructions (Settings → Model), read every turn
        with open(rules) as rf:
            prompt += "\n\nThe user's standing instructions, which override the defaults above:\n" + rf.read().strip()
    snapshot = None
    if os.environ.get("GREENLIGHT_ONLY"):
        try:
            import dash
            snapshot = dash.board(with_ids=True)
        except Exception as e:
            return dict(e="done", ok=False, text=f"Scoped board unavailable: {e}", context=0, window=None, ms=None)
    request_snapshot = snapshot
    if request_snapshot is None:
        try:
            import dash
            request_snapshot = dash.board(with_ids=True)
        except Exception:
            pass
    if reservation and not reservation_current(*reservation):
        return {"e": "done", "ok": False, "text": "Stopped", "context": 0, "window": None, "ms": None}
    try:
        check_session_scope(engine, snapshot)
    except OSError as e:
        return dict(e="done", ok=False, text=f"Chat scope check failed: {e}", context=0, window=None, ms=None)
    if snapshot is not None:
        snapshot = SCOPE_SNAPSHOT
        request_snapshot = snapshot
    if reservation and not reservation_current(*reservation):
        return {"e": "done", "ok": False, "text": "Stopped", "context": 0, "window": None, "ms": None}
    sid = session(engine)
    key = f"{engine}:{sid or 'new'}"  # what's been sent is per session: another engine's, or a new one, has seen nothing
    seen = seen_load(key) if not compact else None
    msg = text
    answer_text = text
    kind = decisions.turn_kind(text)  # the app's idle check-in or watch: its question is that kind (autonomy.py gates them)
    stale_cleared = bool(stale_note)
    if not compact:
        needs_request_check = has_request_question()
        with scope_lock():
            if not scope_current_unlocked():
                return dict(e="done", ok=False, text="Stopped", context=0, window=None, ms=None)
            if needs_request_check:
                import dash
                try:
                    fresh = dash.board(with_ids=True)
                except Exception:
                    fresh = None
                if fresh is not None:
                    if stale_pending_question(fresh):
                        clear_stale_question_unlocked()
                        stale_note += stale_question_notice("the pending question referred to an earlier request, commit, branch, or goal")
                        stale_cleared = True
                        needs_request_check = False
                    snapshot = fresh
                    request_snapshot = fresh
                elif has_request_question():
                    clear_stale_question_unlocked()
                    stale_note += stale_question_notice("the current board could not be checked to verify the previous question")
                    stale_cleared = True
                    needs_request_check = False
        live_status = live_question_status(snapshot) if needs_request_check and snapshot is not None else "valid"
        with scope_lock():
            if not scope_current_unlocked():
                return dict(e="done", ok=False, text="Stopped", context=0, window=None, ms=None)
            if needs_request_check and live_status in ("stale", "unavailable"):
                clear_stale_question_unlocked()
                reason = ("the pending question referred to an earlier commit, target branch, goal, or MR state"
                          if live_status == "stale" else "live state for the previous question could not be verified")
                stale_note += stale_question_notice(reason)
                stale_cleared = True
            asked = asked_path()
            if os.path.exists(asked) and not any(rec.get("via") == "chat" and rec.get("chat") == chat_identity() and decisions.is_open(rec)
                                                 for rec in decisions.load()):
                os.remove(asked)
                stale_note += stale_question_notice("the previous question had no open decision record")
                stale_cleared = True
            if os.path.exists(asked):
                with open(asked) as af:
                    question_context = af.read()
            else:
                question_context = None
        # the turn's tools inherit it: `greenlight.py ade worktree create` refuses a read-only one. An answer to the
        # chat's own question ("prod or staging?" → "prod") is judged with the request it answers.
        req_val = question_context + ANSWER + text if question_context else text
        os.environ["GREENLIGHT_REQUEST"] = req_val[-16384:]
        wrap = lambda note: note + WRAP + msg + ("\n\n" + attach.note(stored) if stored else "")
        text = wrap(stale_note + board_note(msg, key, seen, request_snapshot))
    # a harness without a system-prompt flag gets the prompt in its first message
    mk = lambda s, t: harness.argv(engine, t, model=model, effort=None if compact else effort, sid=s, system=prompt, stream=True,
                                   images=attach.images(stored))
    emit(e="start", engine=engine, model=model or "default")
    norm, guard = Norm(verbose), watch_for_attachments()
    TURN["norm"], norm.base = norm, codex_base(sid) if engine == "codex" and sid else (0, 0)
    try:
        cmd, t = mk(sid, text)
        new_sid, err = run(cmd, engine, t, norm, check=not compact, guard=guard, defer_done=True)
    except (ValueError, OSError) as e:  # unknown harness, pi without bypass, binary missing
        seen_forget()
        return dict(e="done", ok=False, text=str(e), context=0, window=None, ms=None)
    if err == "another turn running":
        return dict(e="done", ok=False, text="Another message is still running in this chat.", context=0, window=None, ms=None)
    if err == "model failed":
        return norm.pending_done
    if err == "retry":  # once only: the retry runs unchecked
        emit(e="status", text="Reply ignored your message, asking again")
        norm.text = ""
        try:
            cmd, t = mk(new_sid or sid, RETRY_PREFIX + text)
            new_sid, err = run(cmd, engine, t, norm, guard=guard, defer_done=True)
        except (ValueError, OSError) as e:
            seen_forget()
            return dict(e="done", ok=False, text=str(e), context=0, window=None, ms=None)
        sid = None  # don't fall into the session-lost retry below
    if err == "another turn running":
        return dict(e="done", ok=False, text="Another message is still running in this chat.", context=0, window=None, ms=None)
    if err == "model failed":
        return norm.pending_done
    if err and sid and not compact and err != "Stopped":  # session gone or corrupt: start over rather than fail
        emit(e="status", text="Session lost, starting a new one")
        key = f"{engine}:new"
        seen_forget()
        seen = seen_load(key)
        text = wrap(stale_note + board_note(msg, key, seen, request_snapshot))  # the new session hasn't seen the board or the cards the lost one had
        norm.base = (0, 0)  # a new thread: its totals start at 0
        try:
            cmd, t = mk(None, text)
            new_sid, err = run(cmd, engine, t, norm, guard=guard, defer_done=True)
        except (ValueError, OSError) as e:
            seen_forget()
            return dict(e="done", ok=False, text=str(e), context=0, window=None, ms=None)
    if err == "model failed":
        return norm.pending_done
    with scope_lock():
        if reservation and not reservation_current_unlocked(*reservation) or not scope_current_unlocked():
            return {"e": "done", "ok": False, "text": "Stopped",
                    "context": 0, "window": None, "ms": None}
        if new_sid:
            with open(session_path(engine), "w") as sf:
                sf.write(new_sid)
        if err and not compact or compact and not err:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(DIR, SEEN))
        elif not compact and (actual_sid := new_sid or session(engine)):
            with contextlib.suppress(OSError):  # a cache failure cannot lose the reply's pending question
                with open(os.path.join(DIR, SEEN), "w") as f:
                    json.dump(seen | {"key": f"{engine}:{actual_sid}"}, f)
        if not compact and not err:
            if not stale_cleared or answer_text.strip().startswith(tuple(decisions.REPLY.values())):
                decisions.quiet("heard", answer_text, chat=chat_identity())
            asked, question = asked_path(), asked_question(norm.reply)
            if question:
                _save_reply_question_unlocked(question, request_snapshot, reservation, kind)
            elif os.path.exists(asked):
                os.remove(asked)
    if err:
        return {"e": "done", "ok": False, "text": short(err, 500), "context": norm.context, "window": norm.window, "ms": None}
    elif getattr(norm, "pending_done", None):
        return norm.pending_done


def main():
    os.makedirs(DIR, exist_ok=True)
    args = sys.argv[1:]
    cmd = args[0] if args else "send"
    opt = lambda k: args[i + 1] if k in args and (i := args.index(k)) + 1 < len(args) else None
    if cmd == "new":
        # Clear starts a new conversation for this entire Center window; each Center has its own CHAT_DIR.
        with scope_lock():
            open(os.path.join(DIR, RESETTING), "w").write("resetting")
            scoped = glob.glob(os.path.join(DIR, "members_scope-*")) + glob.glob(os.path.join(DIR, "asked_scope-*")) + \
                glob.glob(os.path.join(DIR, "session*_scope-*"))
            for suffix in {m.group() for p in scoped if (m := re.search(r"_scope-[0-9a-f]{12}", os.path.basename(p)))}:
                decisions.forget_chat(chat=(CHAT or "main") + suffix)
            decisions.forget_chat(chat=CHAT)
            cancel_running_unlocked()
            rotate_generation_unlocked()
            with contextlib.suppress(OSError):
                os.remove(os.path.join(DIR, ACTIVE_TURN))
            for f in [SEEN, "board"] + [os.path.basename(p) for p in glob.glob(os.path.join(DIR, "session*")) +
                                                            glob.glob(os.path.join(DIR, "asked*")) + glob.glob(os.path.join(DIR, "members_scope-*")) +
                                                            glob.glob(os.path.join(DIR, QUESTION_DELIVERY + "*"))]:
                if os.path.exists(os.path.join(DIR, f)):
                    os.remove(os.path.join(DIR, f))
            attach.clear(DIR)
            os.remove(os.path.join(DIR, RESETTING))
    elif cmd == "stop":
        with scope_lock():
            try:
                active_path = os.path.join(DIR, ACTIVE_TURN)
                active = json.load(open(active_path))
                active["cancelled"] = True
                open(active_path, "w").write(json.dumps(active))
            except (OSError, ValueError):
                pass
            cancel_running_unlocked()
    elif cmd == "bg":
        print(json.dumps(background()))
    elif cmd == "bgstop":
        print(json.dumps({"ok": stop_shell(args[1]) if len(args) > 1 else False}))
    elif cmd == "compact":
        if not session("claude"):
            emit(e="done", ok=True, text="Nothing to compact yet.", context=0, window=None, ms=None)
        else:
            turn("/compact", opt("--model"), None, compact=True)
    else:
        if opt("--only") is not None:  # the window the user is in: its workspaces only, for the BOARD and Chief's own `dash.py board`
            os.environ["GREENLIGHT_ONLY"] = opt("--only")
        text, files = sys.stdin.read(), None
        if "--attach" in args:
            try:
                j = json.loads(text)
                text, files = str(j.get("text") or ""), j.get("files") or []
            except (ValueError, AttributeError):
                return emit(e="done", ok=False, text="The attachments didn't arrive in a form greenlight understands.", context=0, window=None, ms=None)
        turn(text.strip(), opt("--model"), opt("--effort"), name=opt("--name"), verbose="--verbose" in args, files=files)


if __name__ == "__main__":
    diag.install("chat")
    main()
