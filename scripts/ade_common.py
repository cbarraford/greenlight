"""Helpers the non-Orca backends share: git facts Orca reports itself, orca flag parsing, what an agent's terminal shows
(agent_state: one System One choice over its last lines, for any harness; draft: text typed but not sent), and goal
storage for ADEs with no comment/description field."""
import json
import os
import re
import subprocess
from pathlib import Path

GOALS = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight") / "goals.json"  # {workspace path: goal}
MENU_ITEM_RE = re.compile(r"[❯›●▸]\s*\d\.")  # a picker's highlighted row: Claude ❯, Codex ›, Gemini ●, agy ▸
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\r")
DIM_RE = re.compile(r"\x1b\[2m.*?(?=\x1b\[(?:0|22)?m|$)")  # dim text: Claude's prompt suggestion, not a draft


def default_agent():
    import greenlight  # here, not at the top: greenlight imports the adapters
    return greenlight.CONFIG["agents"][0]


def plain(line, ghost=True):
    return ANSI_RE.sub("", line if ghost else DIM_RE.sub("", line))


def git(path, *args):
    p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)
    return p.stdout.strip() if p.returncode == 0 else ""


def project_id(remote):
    # git@gitlab.com:acme/widgets.git | https://gitlab.com/acme/widgets -> git:gitlab.com/acme/widgets
    m = re.search(r"(?:@|://)([^/:]+)[:/](.+?)(?:\.git)?/?$", remote)
    return f"git:{m[1]}/{m[2]}" if m else None


def git_info(path):
    """The Orca worktree fields that come from git."""
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir") if path else ""
    repo = str(Path(common).parent) if common.endswith("/.git") else common
    return {"path": path, "branch": git(path, "symbolic-ref", "-q", "HEAD") if common else "",
            "isMainWorktree": bool(common) and git(path, "rev-parse", "--path-format=absolute", "--git-dir") == common,
            "repoId": repo, "repo": Path(repo).name if repo else "",
            "projectId": common and project_id(git(path, "remote", "get-url", "origin"))}


def worktree_add(repo, name, base=None):
    if not name or name.startswith("-") or (base and base.startswith("-")):
        raise RuntimeError("git worktree add: invalid branch or base ref")
    path = str(Path(repo).parent / f"{Path(repo).name}-{name.replace('/', '-')}")
    p = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", name, "--", path, *([base] if base else [])],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"git worktree add: {p.stderr.strip()[:300]}")
    return path


def worktree_remove(ws, force=False):
    if not ws["isMainWorktree"]:  # git refuses a dirty tree without --force: only the Basement passes it, after the page showed the risks
        p = subprocess.run(["git", "-C", ws["repoId"], "worktree", "remove", *(["--force"] if force else []), "--", ws["path"]], capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"git worktree remove {ws['path']}: {p.stderr.strip()[:300]}")


VALUE_FLAGS = frozenset({
    "worktree", "repo", "name", "base-branch", "base", "comment", "terminal",
    "text", "agent", "prompt", "limit", "pr", "gitlab-mr", "setup", "status", "display-name",
})


def flags(args):
    out, i = {}, 0
    while i < len(args):
        if args[i].startswith("--"):
            k = args[i][2:]
            val = i + 1 < len(args) and (k in VALUE_FLAGS or not args[i + 1].startswith("--"))
            out[k] = args[i + 1] if val else True
            i += 2 if val else 1
        else:
            i += 1
    return out


BOX_RE = re.compile(r"^[│┃|]\s?|\s?[│┃|]$")  # an input box's side borders (Gemini, opencode)
PLACEHOLDER_RE = re.compile(r'^(?:Type your message(?: or @path/to/file)?|Ask Codex to do anything|Ask anything|Send a message|Try "[^"]+")$', re.I)  # exact known placeholders only


def draft(lines):
    """Text in the agent's input box the user hasn't sent: Claude's ❯ box, Codex's ›, Gemini's │ > … │ (menu rows aren't).
    A plain-text read can't tell a dim placeholder from typed text: the known placeholders read as empty."""
    box = []
    for i, x in enumerate(lines):
        t = x.strip()
        inner = BOX_RE.sub("", t).strip() if BOX_RE.search(t) else None
        if t and t[:1] in "❯›":
            box.append((i, t))
        elif inner is not None and inner.startswith(">"):
            box.append((i, inner))
    if not box or MENU_ITEM_RE.match(box[-1][1]) or re.match(r"^>\s*\d\.", box[-1][1]):
        return ""
    parts = [box[-1][1][1:].strip()]
    for x in lines[box[-1][0] + 1:]:
        t = x.strip()
        if t.lower() == "? for shortcuts":
            continue  # Codex's passive footer is below its editor
        if not t or re.fullmatch(r"[│┃|\s─━═\-–—╭╮╰╯]+", t):
            continue
        inner = BOX_RE.sub("", t).strip() if BOX_RE.search(t) else ""
        if (x[:1].isspace() or t[:1] in "│┃|") and (inner or t):
            # Without box metadata this may be output or typed text; protect the possible draft.
            parts.append(inner or t)
        else:
            return ""  # newer agent output means the old input was sent
    text = "\n".join(p for p in parts if p)
    return "" if PLACEHOLDER_RE.match(text) else text


# ---- what an agent's terminal shows, for any harness ----

STATES = {  # the classifier's options: name → when it applies (System One reads these)
    "working": "The agent is busy right now: a spinner or timer, 'esc to interrupt'/'esc to cancel', a tool or command running.",
    "waiting_for_user": "The agent stopped to ask the user something: a permission or approval prompt, a numbered menu, a yes/no, "
                        "or a question at the end of its reply that it needs answered before it can go on.",
    "finished": "The agent finished its turn and sits at an empty prompt with nothing asked: done, or idle.",
    "stuck_looping": "The agent repeats itself: the same error, command or message over and over without progress.",
    "rate_limited": "The agent hit a usage, rate or quota limit and can't continue until it resets (5-hour/weekly limit, 429, quota).",
    "logged_out": "The coding agent itself can't authenticate: logged out, expired or revoked token or API key, its own 401, asked to /login or sign in. A command or test seeing HTTP 401 is not agent logout.",
    "unsent_draft": "Text is typed into the agent's input box but not sent: the turn is over and the draft waits on Enter.",
}
QUESTION = ("This is the bottom of a coding agent's terminal (Claude Code, Codex, Antigravity/agy, opencode, pi or Gemini CLI). "
            "What state is the agent in now? Judge by the bottom of the screen: older lines are history.")
DEAD_RE = re.compile(r"^\s*\W{0,3}\s*(?:(?:API Error:\s*401\s+)?OAuth access token has been revoked|Please run /login|"
                     r"(?:Not logged in\.\s*)?Run `?(?:codex|claude|agy|gemini|opencode|pi) login`?|login expired|"
                     r"API key (?:has expired|is invalid|not valid|expired)|Invalid API key|authentication_error)", re.I)
LIMIT_RE = re.compile(r"^\s*\W{0,3}\s*(?:(?:API )?Error:?\s*)?(?:(?:You've hit your )?usage limit|limit reached|5-hour limit|weekly limit|out of (?:extra )?usage"
                      r"|exceeded your current quota|insufficient_quota"
                      r"|(?:You have )?exhausted your daily quota|(?:Your )?quota will reset after)", re.I | re.M)  # a bare RESOURCE_EXHAUSTED is any gRPC error
BUSY_RE = re.compile(r"esc to interrupt|esc interrupt|\(esc to cancel\b|ctrl\+c to (?:interrupt|stop|cancel)|"
                     r"^\W{0,3}\S[^(]{0,40}…\s*\(\d+[smh]\b|^[⠀-⣿]\s|\bWorking\s*\(\d+s\b", re.I | re.M)  # spinners and timers
ASK_RE = re.compile(r"^\s*(?:[❯›●▸]\s*\d\.\s+\S|Do you want to|Allow (?:execution|command|this)|"
                    r"Waiting for (?:your )?(?:approval|confirmation|input))|(?:\(y/n\)|\[Y/n\]|\[y/N\])\s*$", re.I | re.M)
HEURISTIC_P = 0.75  # a pattern hit: above the 0.7 that greenlight acts on, as the regexes it replaces were acted on


def screen_state(lines):
    """The deterministic reading, for when no System One model is set up: patterns every harness's TUI shares."""
    ls = [plain(str(x)).rstrip() for x in lines][-60:]
    nonblank = [x for x in ls if x.strip()]
    at = lambda rx: max((i for i, x in enumerate(nonblank) if rx.search(x)), default=-1)
    busy = at(BUSY_RE)
    def empty_prompt(x):
        t = x.strip()
        if t in ("┃", "│", "|"):
            return True
        if t[:1] in "❯›>" and (not t[1:].strip() or PLACEHOLDER_RE.match(t[1:].strip())):
            return True
        inner = BOX_RE.sub("", t).strip() if BOX_RE.search(t) else ""
        return bool(inner.startswith(">") and (not inner[1:].strip() or PLACEHOLDER_RE.match(inner[1:].strip())))

    # A loop is current only if the repeated output is the latest substantive output.
    footer = lambda x: x.strip().lower() == "? for shortcuts"
    output = [x.strip() for x in nonblank if not empty_prompt(x) and not footer(x)
              and not re.search(r"\S+://\S+", x)
              and not re.fullmatch(r"[│┃|\s─━═\-–—╭╮╰╯]+", x.strip())]
    repeated = [width for width in range(1, min(4, len(output) // 5) + 1)
                if output[-width * 5:] == output[-width:] * 5 and len("".join(output[-width:])) >= 20]

    border = lambda x: bool(re.fullmatch(r"[│┃|\s─━═\-–—╭╮╰╯]+", x.strip()))
    passive = lambda x: empty_prompt(x) or border(x) or footer(x)
    prompt = max((i for i, x in enumerate(nonblank) if empty_prompt(x)), default=-1)
    idle = prompt >= 0 and prompt >= len(nonblank) - 3 and all(border(x) or footer(x) for x in nonblank[prompt + 1:])
    recovered = at(re.compile(r"login successful|successfully logged in|logged in as|authentication successful|"
                              r"successfully authenticated|retry succeeded|retried and completed|"
                              r"(?:task|request) (?:successfully )?(?:completed|succeeded)|tests pass(?:ed)?|"
                              r"token accepted|changes complete|finished (?:the )?task|tests are green|"
                              r"resumed after (?:reset|limit)", re.I))
    dead, limited = at(DEAD_RE), at(LIMIT_RE)
    retry = at(re.compile(r"retried with refreshed credentials", re.I))
    wrote = at(re.compile(r"\bwrote\s+\S", re.I))
    done = at(re.compile(r"^\s*Done\.?\s*$", re.I))
    if idle and dead >= 0 and dead < retry < wrote < done:
        recovered = max(recovered, done)  # a successful write and completion after the credential retry
    diagnostic = lambda x: bool(re.search(r"Traceback|Error|Exception|^\s*(?:File |at |Caused by|detail)|"
                                          r"\b(?:failed|denied|unauthorized|quota)\b", x, re.I))
    error_live = lambda i: i >= 0 and (i >= len(nonblank) - 8 or all(diagnostic(x) for x in nonblank[i + 1:] if not passive(x)))
    ask = at(ASK_RE)
    for i, line in enumerate(nonblank[:-2]):
        if (re.match(r"^\s*(?:Choose|Select|Pick)\b[^:]*:\s*$", line, re.I)
                and re.match(r"^\s*1\.\s+\S", nonblank[i + 1])
                and re.match(r"^\s*2\.\s+\S", nonblank[i + 2])
                and all(passive(x) for x in nonblank[i + 3:])):
            ask = max(ask, i)
    if idle:
        latest = next(((i, x.strip()) for i, x in reversed(list(enumerate(nonblank[:prompt])))
                       if not border(x) and not empty_prompt(x) and not footer(x)), None)
        if latest and re.match(r"^(?:(?:Which|What|Who|When|Where|Why|How|Should|Would|Could|Can|Do|Does|Did|Is|Are|Have|Has)|(?:Please\s+)?(?:Choose|Select|Specify|Confirm|Provide))\b.*\?\s*$", latest[1], re.I):
            ask = max(ask, latest[0])
    answered = at(re.compile(r"User selected|Command allowed|Approval granted|\bDone\.|Task completed", re.I))
    typed = draft(ls)
    typed_at = max((i for i, x in enumerate(nonblank) if typed and
                    (x.strip()[:1] in "❯›" or (BOX_RE.search(x.strip()) and BOX_RE.sub("", x.strip()).strip().startswith(">")))), default=-1)
    menu_tail = lambda x: bool(passive(x) or re.match(r"^\s*\d+\.\s+\S", x) or re.fullmatch(r"Esc to cancel", x.strip(), re.I))
    ask_live = ask >= 0 and ask > answered and ask >= len(nonblank) - 15 and all(menu_tail(x) for x in nonblank[ask + 1:])
    if busy >= 0 and all(passive(x) for x in nonblank[busy + 1:]) and busy > max(dead, limited):
        label = "working"
    elif error_live(dead) and dead > max(recovered, ask, typed_at, limited):
        label = "logged_out"
    elif error_live(limited) and limited > max(recovered, ask, typed_at, dead):
        label = "rate_limited"
    elif typed_at > ask:
        label = "unsent_draft"
    elif ask_live:
        label = "waiting_for_user"
    elif any(not idle or any(re.search(r"\b(?:error|fail|retry|exception|traceback)\b|^\$ ", x, re.I)
                             for x in output[-width:]) for width in repeated):
        label = "stuck_looping"
    else:
        label = "finished"
    p = HEURISTIC_P if label != "finished" else 0.5
    rest = round((1 - p) / (len(STATES) - 1), 4)
    return {"state": label, "prob": p, "probs": {k: (p if k == label else rest) for k in STATES}, "provider": "local"}


def agent_state(lines, agent=""):
    """The agent's state from its terminal's last ~60 lines, any harness: one System One choice over STATES
    ({state, prob, probs, provider}; `draft` too when it's unsent_draft). With no model set up (provider local), the patterns decide."""
    import decide  # here, not at the top: decide's haiku path imports harness → greenlight → the adapters → us
    ls = [plain(str(x)).rstrip() for x in lines]
    while ls and not ls[-1].strip():
        ls.pop()
    ls = ls[-60:]
    h = screen_state(ls)
    if ls and h["state"] not in ("unsent_draft", "waiting_for_user", "working", "logged_out", "rate_limited", "stuck_looping"):  # observable gates cannot be released by a model
        r = decide.choice("\n".join(ls), QUESTION, STATES, context=f"The agent is {agent}." if agent else "")
        if r["provider"] != "local" and r["confidence"] >= 0.7:
            h = {"state": r["choice"], "prob": r["confidence"], "probs": r["probs"], "provider": r["provider"]}
    if h["state"] == "unsent_draft":
        h["draft"] = draft(ls)
    return h


def goals():
    try:
        return json.loads(GOALS.read_text())
    except (OSError, ValueError):
        return {}


def set_goal(path, goal):
    g = goals()
    g.pop(path, None) if not goal else g.update({path: goal})
    GOALS.parent.mkdir(parents=True, exist_ok=True)
    tmp = GOALS.with_name(f".{GOALS.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(g, indent=1))
    os.replace(tmp, GOALS)


def repo_list(worktrees):
    """Orca's `repo list` shape: one entry per repo, named by its folder, keyed by its remote (dash.py's New task)."""
    out = {}
    for w in worktrees:
        if w["repoId"] and w["repoId"] not in out:
            out[w["repoId"]] = {"id": w["repoId"], "path": w["repoId"], "displayName": Path(w["repoId"]).name,
                                "gitRemoteIdentity": {"canonicalKey": (w.get("projectId") or "").removeprefix("git:")}}
    return {"repos": list(out.values())}
