#!/usr/bin/env python3
"""greenlight: work out the next lifecycle step, and who it's waiting on, for every Worktrunk workspace.

    greenlight.py scan [--json]   facts + next step per workspace; snapshot saved to ~/.greenlight/last.json. Each row also
                                  carries `life`: its lifecycle stage and sub-stage, who holds it, and the suggested move (lifestage.py)
    greenlight.py report          scan, then write ~/.greenlight/report.html (needs-you first)
    greenlight.py precheck        automation gate: exit 0 when something is actionable (or cleanup.py has a workspace
                                  to delete), 1 when nothing is, 2 when the scan itself failed (its cause on stdout)
    greenlight.py note <ws> <text>  append a dated line to ~/.greenlight/history/<ws>.md
    greenlight.py history <ws>      print that workspace's history
    greenlight.py ping [--key <ws>|@k] [--kind <kind>] [--ws <ws>]... [--proactive] <text>  question for the Greenlight menubar app
                                       (outbox.jsonl); --ws names a workspace it's about (its hex is lifted until answered);
                                       --kind names the sort of question for the decision record (decisions.py). A known kind gets
                                       autonomy.py's proposal: preselected in the app, or (opted-in, sure, inert) answered for
                                       you: it prints `auto: <option>`. --proactive (or kind check-in/watch): sent only when
                                       autonomy.py's anti-slop gate passes, else it prints `held: ...` and sends nothing
    greenlight.py resolve <key> <why>       close that question in the app
    greenlight.py import [detect|preview|apply] [--from orca|t3|cmux|herdr|folder] [--path DIR]  another ADE's repos and
                                 goals into the native engine, read-only on its side (importers.py; preview changes nothing)
    greenlight.py repos [list|add|rm <path>...]  the native engine's repos (repos.json): every worktree of each is a workspace
    greenlight.py ade <orca-shaped args>    a compatibility JSON call answered by the native backend (ade.py);
                                            Worktrunk creates and removes the worktrees

Read-only: it never changes anything. The greenlight skill acts on `next.cmd` / `next.owner`.
Deleting finished workspaces is cleanup.py's job (next to this file); creating workspaces for MRs
that have none is adopt.py's.

Stdlib only. Needs `glab` (gitlab.com repos) and `gh` (github.com repos) authenticated. A GitHub PR is
normalised into the GitLab MR shape (gh_pr), so the lifecycle steps, report and dashboard treat both alike. ORCA_ENVIRONMENT=<name> targets a paired runtime
(handy from a laptop; git upstream lookups are skipped when worktree paths aren't local).

State and config live in $GREENLIGHT_HOME (default ~/.greenlight). config.json there overrides DEFAULTS below.
"""

import fcntl
import html
import json
import contextlib
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import uuid
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ade  # noqa: E402
import ade_common  # noqa: E402
import diag  # noqa: E402
import decide  # noqa: E402
import autonomy  # noqa: E402
import decisions  # noqa: E402
import lifestage  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the skill dir: lifecycles/, prompts/, examples/, memory/
STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
# Only what differs between users. config.json's keys replace these whole (no deep merge).
DEFAULTS = {
    "me": None,                       # your GitLab/GitHub username; None = ask glab (else gh) once, cached in me.json
    "review_bot": None,               # a bot the `review_bot` step assigns as assignee + reviewer; None skips the step
    "agents": ["claude", "codex"],    # dispatch order: on a usage limit greenlight moves to the next one (claude|codex|opencode|pi|gemini|agy)
    "bins": {},                       # CLI paths when they're off the PATH greenlight runs with, e.g. {"wt": "~/bin/wt"}; see bins.py
    "harnesses": {},                  # per agent: {"bypass": true} for full access, {"bin": path}, {"model": id} its default model; see harness.py
    "model_fallbacks": {},            # per slot (chat/page/suggest/auto/decide) or per harness fallback model+effort specs; see harness.py
    "llm_harness": "claude",          # runs the dashboard's own LLM calls (design, suggestions)
    "merge_method": "merge",          # merge | squash | rebase
    "require_human_approval": True,   # without automerge a human signs off: a reviewer where the forge requires approvals, else you ("merge it?");
                                      # False: a mergeable MR with no other human's approval merges anyway
    "adopt": {"cap": 10, "roles": ["author", "reviewer"]},  # adopt.py: creates per run, which of your MRs get a workspace
    "blocking": None,                 # blocking.py roster ({repos, people, teams, lead}); None turns it off
    "suggest_rules": [],              # extra rules for the dashboard's suggested prompts
    "name": "greenlight",                  # what the dashboard's LLM prompts call it
}


def config():
    try:
        return {**DEFAULTS, **json.loads((STATE / "config.json").read_text())}
    except FileNotFoundError:
        return dict(DEFAULTS)


CONFIG = config()
BOT = CONFIG["review_bot"]
NOW_MS = time.time() * 1000
DEAD_RE, LIMIT_RE = ade_common.DEAD_RE, ade_common.LIMIT_RE  # an agent logged out / out of usage, in any harness's words
# Instructions I add to a goal. A word joined to its neighbour by - or _ ("security-hold") is part of a name, not an instruction.
KEEP_DRAFT_RE = re.compile(r"(?<![\w-])(stay|keep|remain)s? (?:a |as )?draft(?![\w-])", re.I)
HOLD_RE = re.compile(r"(?<![\w-])(?:hold|do ?n[o'’]t merge)(?![\w-])", re.I)
AUTOMATION_RE = re.compile(r"\(automation\)", re.I)  # free-form goal driven by an Orca automation: don't dispatch
REVISIT_RE = re.compile(r"revisit (\d{4}-\d\d-\d\d)", re.I)  # "(revisit 2026-11-06)" parks it until then
PARKED_RE = re.compile(r"\(parked\)", re.I)  # parked with no end date, and hidden from the dashboard and report
# The same instructions in my own words, asked of System One (decide.py). A model can only add to what the patterns
# above find, never take a hold or a draft away; the bar is low for the safe direction (hold, stay draft), high for
# parking (it hides the workspace) and for a date.
INTENTS = {
    "is_held": ("Does the goal tell the bot to hold off merging this PR/MR: hold, don't merge, wait for something "
                "or someone before merging, or ask first?", 0.6),
    "stay_draft": ("Does the goal say to keep the PR/MR as a draft, not mark it ready for review?", 0.6),
    "is_parked": ("Does the goal say to park this work: set it aside, pause it, put it on ice, don't work on it for now?", 0.85),
    "revisit": ("Does the goal say to leave this work alone until a particular later date?", 0.7),
}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ISSUE_GOAL_RE = re.compile(r"^Merge MR for #(\d+)")  # dash.py plan_send: the MR/PR that closes issue N
PLAN_RE = re.compile(r"^Plan:")  # a planner workspace (dash.py plan): the Plan view's, never driven by greenlight
# Old /workspaces status lines ("Rebase !42 — dispatched 2026-01-13") aren't goals.
STATUS_LINE_RE = re.compile(r"dispatched \d{4}-\d\d-\d\d")
TRANSIENT_RE = re.compile(r"\b50[0234]\b|bad gateway|service unavailable|gateway time-?out|timed? ?out|"
                          r"connection (?:reset|refused)|unexpected EOF|TLS handshake", re.I)
# Order matters in the report: who needs to act, most urgent first.
OWNERS = ["me", "greenlight", "agent", "working", "ci", "reviewers", "author", "dependency", "parked", "done"]


def run(cmd, timeout=120, tries=1):
    # tries > 1: retry timeouts and transient failures (GitLab blips) with short backoff
    sub = cmd[1] if len(cmd) > 1 and re.fullmatch(r"[a-z][a-z-]{0,20}", cmd[1]) else None  # "api", "mr": never an argument
    with diag.span("cmd", tool=Path(cmd[0]).name, sub=sub) as d:
        return _run(cmd, timeout, tries, d)


def _cmd_error(msg, tool="", msg_class=None):
    """RuntimeError carrying the tool name and stderr class so callers never re-parse argv (keeps type name RuntimeError)."""
    exc = RuntimeError(msg)
    exc._gl_tool = tool  # type: ignore[attr-defined]
    exc._gl_class = msg_class  # type: ignore[attr-defined]
    return exc


def _run(cmd, timeout, tries, d):
    for i in range(tries):
        d["tries"] = i + 1
        d.pop("rc", None), d.pop("msg_class", None)  # an earlier try's failure isn't this one's
        last = i == tries - 1
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if last:
                raise
        else:
            if p.returncode == 0:
                return p.stdout
            err = (p.stderr or p.stdout).strip()
            cls = diag.classify(err)
            d.update(rc=p.returncode, msg_class=cls)  # the class of stderr, not the message (it has argv)
            if last or not TRANSIENT_RE.search(err):
                raise _cmd_error(f"{' '.join(cmd)}: {err[:2000]}", tool=Path(cmd[0]).name, msg_class=cls)  # orca's JSON error puts its cause last
        time.sleep(2 * 2 ** i)


def whoami():
    """Your forge username: me.json, else glab's user, else gh's (cached). "" when neither answers."""
    f = STATE / "me.json"
    try:
        return json.loads(f.read_text())["me"]
    except (OSError, ValueError, KeyError):
        pass
    for cmd, key in ((["glab", "api", "user"], "username"), (["gh", "api", "user"], "login")):
        try:
            me = json.loads(run(cmd, timeout=20))[key]
        except Exception:  # noqa: BLE001 - not installed or not logged in: try the next
            continue
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"me": me}))
        return me
    return ""


ME = os.environ.get("GREENLIGHT_ME") or CONFIG["me"] or whoami()


def orca(*args, timeout=120):
    """An Orca-shaped compatibility call answered by Greenlight's native backend."""
    return ade.call(*args)


class PlacementRefused(RuntimeError):
    """place_here said no: `ade` prints it and exits 3, like a read-only refusal."""


def primary_agent(agents):
    """The live session to show for a workspace. A completed review may be newer than its still-running parent. An
    agent outside Greenlight (external.py) shows only when none of its own is active there."""
    return max(agents or [{}], key=lambda a: (a.get("state") in ("working", "waiting"), not a.get("external"),
                                             a.get("updatedAt") or a.get("stateStartedAt") or 0))


def base_branches(wts):
    """{repoId: branch} of each repo's main checkout: a second worktree on that branch is a copy of the base, not a workspace."""
    return {w.get("repoId"): w.get("branch") for w in wts if w.get("isMainWorktree") and w.get("branch")}


def on_base(w, bases):
    """A non-main worktree checked out on its repo's base branch (a Claude subagent's checkout of main, say): no MR can come
    from it and there's nothing to rebase, so it gets no lifecycle."""
    return not w.get("isMainWorktree") and bool(w.get("branch")) and bases.get(w.get("repoId")) == w["branch"]


ORCA_AGENTS = {"agy": "antigravity"}  # greenlight's harness names that Orca knows by another id
FROM_ORCA_AGENTS = {v: k for k, v in ORCA_AGENTS.items()}


_KNOWN_AGENTS = ("claude", "codex", "agy", "opencode", "pi", "gemini")


def canonical_agent(name):
    """Normalize ADE-specific agent ids (e.g. Orca's 'antigravity') to greenlight's harness name."""
    if not isinstance(name, str):
        return name
    low = name.strip().lower()
    return FROM_ORCA_AGENTS.get(low, low if low in _KNOWN_AGENTS else name)


def orca_agent(args):
    """`--agent <name>` as Orca knows it, plus that agent's `harnesses.<name>.model` unless a --model was given."""
    args = list(args)
    if "--agent" not in args[:-1]:
        return args
    i = args.index("--agent") + 1
    name = canonical_agent(args[i])
    args[i] = ORCA_AGENTS.get(name, name)
    model = ((CONFIG.get("harnesses") or {}).get(name) or {}).get("model")
    return args + ["--model", model] if model and "--model" not in args else args


def orca_cli(*args, timeout=120):
    """The Orca CLI itself, whatever GREENLIGHT_ADE is (dash.py trees asks every ADE)."""
    args = orca_agent(args)
    # Bare `orca` on Linux outside an Orca terminal is the GNOME screen reader.
    shim = Path.home() / ".config/orca/linux-orca-cli-shim/orca"  # ssh / cron shells have no orca-ide on PATH
    exe = os.environ.get("ORCA_CLI_COMMAND") or shutil.which("orca-ide") or (shim.exists() and str(shim)) or "orca"
    env = ["--environment", os.environ["ORCA_ENVIRONMENT"]] if os.environ.get("ORCA_ENVIRONMENT") else []
    out = json.loads(run([exe, *args, *env, "--json"], timeout=timeout))
    if not out.get("ok"):
        err = str(out.get("error") or "")
        raise _cmd_error(f"orca {' '.join(args)}: {err}", tool="orca", msg_class=diag.classify(err))
    return out["result"]


# A request that only asks (status, research, billing lookups, inspections) is answered where it's asked, never
# by a new workspace; a workspace is for a change. GREENLIGHT_REQUEST is the user's message (chat.py) or the
# automation's prompt (auto.py); `ade worktree create` refuses while it's read-only. Wrongly refusing a change is
# the worse mistake, so only plain questions and lookups count: every clause has to open with one ("Who's on call?
# Page them." asks for a change), and anything else (a request, unclear) is a change.
LOOKUP = (r"how|what|whats|which|who|whose|when|where|why|is|are|was|were|did|has|does|status|check|look up|lookup|"
          r"tell me|list|summari[sz]e|explain|compare|research|investigate|inspect")
ASKS_RE = re.compile(rf"^\W*(?:(?:can|could|would|will) (?:you|we) |please )?(?:please )?({LOOKUP})\b", re.I)
CLAUSE_RE = re.compile(r"[?.;:,\n]+|!(?=\s|$)|\b(?:(?:and )?then|otherwise|if so|if not|but)\b", re.I)  # clauses, not "and"
COURTESY_RE = re.compile(r"\W*(please|thanks?|thank you)?\W*", re.I)  # "…, please?" is no clause
CHANGES = (r"fix|change|add|implement|update|delete|remove|create|make|write|rename|merge|deploy|ship|set ?up|set|"
           r"refactor|build|rebase|push|commit|edit|install|configure|enable|disable|cancel|send|post|reply|respond|"
           r"comment|open (?:a|an|the|it|up|new)|close|move|bump|upgrade|downgrade|migrate|patch|revert|label|assign|approve|review|restart|"
           r"kill|stop|start|run|launch|dispatch|retry|re-?run|draft|publish|resolve|address|tag|release|save|record|steward|"
           r"archive|clean|turn (on|off)|hold|park|rotate|revoke|pay|buy|order|schedule|book|undo|apply|land|split|"
           r"squash|rewrite|document|test|grant|give|invite|share|allow|deny|block|unblock|take over|pick up|handle|"
           r"spin up|try|continue|proceed|finish|resume|do|go ahead|keep|use|switch|replace|reset|clear|mark|ping|"
           r"notify|e-?mail|message|get|port|hook|wire|file|sort|triage|translate|retrigger|kick off|page|auto-?merge|"
           r"what about|how about|what if|why not|check off|tick off|list (?:it|us|our \w+|my \w+) on|let|put|renew|roll|nudge|"
           r"reassign|cherry-pick|sign[ -]?off|check in|check out|generate|is it possible to|in(?:to)? (?:a|the|our|my|its|this) (?:[\w-]+ )?(?:docs?|readme|wiki|changelog|description|report|notes?)|"
           r"workspace|worktree|agent")  # never complete: a missed verb is why --change exists
CHANGE_RE = re.compile(rf"\b({CHANGES})\b", re.I)  # base forms only: "is !42 merged?" asks about a state
DET = r"the|a|an|it|its|them|this|that|these|those|my|our|your|their|his|her|me|us|him|all|every"
AND_DO_RE = re.compile(rf"\band (?:then )?(?!(?:{DET})\b)(\w+) (?:(?:{DET})\b|(?-i:[A-Z])|[\s$€£]|(?:prs?|mrs?|issues?|tickets?)\b)", re.I)  # "and refund the customer / Alice / !42"
NAME_RE = re.compile(r"(?!\d+[.)]\s)[\w.!#/-]*\d[\w.!#/-]*|\w+(?:-\w+){2,}")  # names, not verbs: "fix-001", "gpt-live-24h-cost" (but "re-run" runs)
ANSWER, ASKED = "\n(answer) ", "\n(asked) "  # chat.py: request, (asked) the chat's question, (answer) the user's answer, …
OFFER_VERB = rf"\b(?!(?:use|do|get|keep|try|set(?! ?up))\b)({CHANGES})\b"  # not "should I use prod?"
S = r"(?:[^.?!\n]|[.!](?=\S))"  # within one sentence ("greenlight.py" doesn't end it)
OFFER_RE = re.compile(  # the chat's question offered a change, not an answer that mentions one ("since the deploy")
    rf"\b(?:i|me|we|us)\b{S}*?{OFFER_VERB}{S}*\?|\bwant (?:a|an|me to|us to|it|this|that|one)\b{S}*\?"  # "can I add a meter?"
    rf"|\b(?:i can|i could|i'll|i will|let me|happy to)\s+(?:\S+\s+){{0,2}}?{OFFER_VERB}"  # "I can add a meter."
    rf"|^[ \t]*(?:[-*•]|\d+[.)])\s+(?:\*\*|__)?{OFFER_VERB}"  # "1. **Add a cost meter**: $2/mo", not a breakdown row:
    rf"(?!(?:[ \t]+(?!(?:{DET})\b)\w+)?(?:\*\*|__)?[ \t]*:)(?!(?:[ \t]+(?!(?:{DET})\b)\w+){{0,2}}(?:\*\*|__)?[ \t]*[$€£])"  # "- Agent runs: $3", "- Test runs cost $3"
    rf"|^`?options:[^\n]*{OFFER_VERB}|{OFFER_VERB}[^\n]*\n`?options:", re.I | re.M)  # "Add a meter?\nOptions: …"
AGREE_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|please do|go for it|sounds good)\b", re.I)  # in an answer: "sure" agrees to a change
READ_ONLY_REFUSAL = ("refused: this request only asks a question (status, research, a billing lookup, an inspection), so "
                     "answer it here from the sources you can read (greenlight.py, ade worktree ps / terminal read, "
                     "glab / gh, the APIs and files on this host) instead of creating a workspace. Workspaces are for "
                     "requests that change something (code, docs, settings, an external system). Only if the request "
                     "(the user's message, or the automation's prompt) does ask for a change, run the same command with "
                     "--change \"<what it changes>\".")


def read_only(text):
    """True when the request is plainly questions or lookups, no question the chat asked about it offered a change,
    and no answer to one asks for a change."""
    req, *turns = re.split("(" + "|".join(map(re.escape, (ASKED, ANSWER))) + ")", NAME_RE.sub(" ", text or ""))
    asked = [t for k, t in zip(turns[0::2], turns[1::2]) if k == ASKED]
    answers = [t for k, t in zip(turns[0::2], turns[1::2]) if k == ANSWER]
    clauses = [c for c in CLAUSE_RE.split(req) if c and not COURTESY_RE.fullmatch(c)]
    proposed = not asked or any(CHANGE_RE.search(q) or not ASKS_RE.search(re.sub(r"^.*?\b(?:me|us) to (?:also )?", "", q, flags=re.S))
                                for q in asked)  # "yes" agrees to a change, unless only a lookup was offered ("would you like me to check staging?")
    def answer_changes(a):  # "prod" narrows the question; "sure" to a proposed change, or a sentence that doesn't ask, is a request
        return CHANGE_RE.search(a) or proposed and AGREE_RE.search(a) or (len(a.split()) > 3 and not ASKS_RE.match(a))
    if any(not ASKS_RE.match(req[m.start(1):]) for m in AND_DO_RE.finditer(req)):  # "and tell me why" still asks
        return False
    return (bool(clauses) and all(ASKS_RE.match(c) for c in clauses) and not CHANGE_RE.search(req)
            and not any(OFFER_RE.search(q) for q in asked)  # "want me to add a meter?", or Options offering one
            and not any(answer_changes(a) for a in answers))


def create_refused(args):
    """READ_ONLY_REFUSAL when args create a workspace for a read-only request, else None. --change overrides."""
    a = [x for x in args if not x.startswith("-")][:2]
    if a != ["worktree", "create"] or any(x.startswith("--change") for x in args):
        return None
    return READ_ONLY_REFUSAL if read_only(os.environ.get("GREENLIGHT_REQUEST", "")) else None


def here():
    """This machine's short name, as Orca's `host list` prints it (devbox, not devbox.lan)."""
    return socket.gethostname().split(".")[0].lower()


def same_host(a, b):
    return bool(a) and bool(b) and str(a).split(".")[0].lower() == str(b).split(".")[0].lower()


ELSEWHERE = ("refused: Orca here answers from {there}, not this machine ({me}), so the workspace and its agent would "
             "run there. Chief's workspaces run where chief does. Pass --host <name> (orca host list) or set "
             "ORCA_ENVIRONMENT only when the user asked for another machine.")
REPO_ELSEWHERE = ("refused: {what} is checked out on {there}, not this machine ({me}), so the workspace and its agent "
                  "would run there.{alt} Only when the user asked for that machine, use --project <id> --host <name>.")
UNVERIFIED = ("refused: Orca didn't answer `project setups`, so whether {what} is on this machine can't be checked "
              "while another host is paired. Try again in a moment.")


def flag(args, name):
    """The value of --name v or --name=v in args, else None."""
    for i, x in enumerate(args):
        if x.startswith(f"--{name}="):
            return x.split("=", 1)[1]
        if x == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return None


def repo_setups(sel, setups):
    """The Orca project setups an `--repo` selector (id:, name:, path:, or bare) can mean."""
    kind, _, val = sel.partition(":") if sel.split(":", 1)[0] in ("id", "name", "path") else ("", "", sel)
    keys = {"id": ("repoId", "id"), "name": ("displayName",), "path": ("path",)}.get(kind, ("repoId", "displayName", "path"))
    return [s for s in setups if any(s.get(k) == val for k in keys)]


def project_setups(tries=2):
    """Orca's project setups (each checkout's id, repoId, displayName, path, hostId, projectId), always Orca's whatever
    GREENLIGHT_ADE is (only Orca has hosts); None when Orca didn't answer, after a second try."""
    for _ in range(tries):
        try:
            return [s for s in orca_cli("project", "setups", timeout=20).get("setups") or [] if isinstance(s, dict)]
        except Exception:  # noqa: BLE001 - a blip, or an Orca without them: the caller decides
            pass
    diag.event("placement", op="setups", err="unverified")
    return None


def is_local(s):
    return (s.get("hostId") or "local") == "local"


def local_first(repos):
    """repos (`orca repo list` rows, which carry no host) with this machine's checkouts first, so a pick by name or by
    project (dash.py's New task, adopt.py) lands here when it can (#160). Unchanged under other ADEs (no hosts), and
    when Orca can't say."""
    if not ade.is_orca():
        return list(repos)
    here_ = {(s.get("repoId"), s.get("path")) for s in project_setups(tries=1) or [] if is_local(s)}
    return sorted(repos, key=lambda r: (r.get("id"), r.get("path")) not in here_) if here_ else list(repos)


def place_repo(args, hosts):
    """(args, None) when a --repo or --project-host-setup names a checkout on this machine (or one Orca doesn't know:
    Orca refuses those), else (None, why not), naming this machine's checkout of the same project when there is one.
    A --project-host-setup whose id also names one here gets --host local (setup ids repeat across hosts)."""
    setups = project_setups()
    sel, hs = flag(args, "repo"), flag(args, "project-host-setup")
    what = f"repo {sel}" if sel else f"project setup {hs}"
    if setups is None:
        return None, UNVERIFIED.format(what=what)
    hit = repo_setups(sel, setups) if sel else [s for s in setups if s.get("id") == hs]
    if not hit:
        return list(args), None
    if any(is_local(s) for s in hit):
        return list(args) + (["--host", "local"] if hs and not sel else []), None
    mine = next((s for s in setups if is_local(s) and s.get("projectId") and s.get("projectId") == hit[0].get("projectId")), None)
    names = {h.get("id"): h.get("machineName") or h.get("name") for h in hosts if isinstance(h, dict)}
    alt = f" This machine's checkout of it is --repo id:{mine['repoId']}." if mine else ""
    return None, REPO_ELSEWHERE.format(what=what, there=names.get(hit[0].get("hostId")) or hit[0].get("hostId"), me=here(), alt=alt)


def place_here(args, picked=False):
    """(args, None) that create an Orca workspace on this machine, or (None, why not). An explicit --host or
    ORCA_ENVIRONMENT is the user's choice and stands. Otherwise: a runtime whose `local` is another machine (an Orca
    terminal relayed from elsewhere) is refused; so is a --repo or --project-host-setup only on another paired host,
    unless the user picked that checkout themselves (picked: the sidebar's New workspace on its row); and --project
    gets --host local (#160)."""
    a = [x for x in args if not x.startswith("-")][:2]
    if a == ["worktree", "create"] and flag(args, "repo") is not None and flag(args, "host") == "local":
        args = drop_flag(args, "host")  # Orca refuses --repo with --host ("choose either"); --repo creates here anyway
    if a != ["worktree", "create"] or os.environ.get("ORCA_ENVIRONMENT") or flag(args, "host") is not None:
        return list(args), None
    try:
        hosts = orca_cli("host", "list", timeout=20).get("hosts") or []
    except Exception:  # noqa: BLE001 - an Orca with no `host list` can't be checked: create as before
        hosts = []
    local = next((h for h in hosts if isinstance(h, dict) and h.get("id") == "local"), {})
    if local.get("machineName") and not same_host(local["machineName"], here()):
        return None, ELSEWHERE.format(there=local["machineName"], me=here())
    if not picked and len(hosts) > 1 and (flag(args, "repo") or flag(args, "project-host-setup")):
        args, why = place_repo(args, hosts)
        if why:
            return None, why
    return list(args) + (["--host", "local"] if flag(args, "project") is not None else []), None


def drop_flag(args, name):
    """args minus --name v and --name=v."""
    out, skip = [], False
    for x in args:
        if skip:
            skip = False
        elif x == f"--{name}":
            skip = True
        elif not x.startswith(f"--{name}="):
            out.append(x)
    return out


def without_change(args):
    """args minus --change <why>, which is greenlight's own and not the ADE's."""
    return drop_flag(args, "change")


def gl(path):
    return json.loads(run(["glab", "api", path], timeout=45, tries=3))  # worst case ~2.4 min


def gl_all(path):
    # ponytail: manual paging; `glab api --paginate` concatenates arrays inconsistently across versions
    out, page = [], 1
    while True:
        batch = gl(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def pid(project):
    return urllib.parse.quote(project, safe="")


def forge_project(project_id):
    """Orca projectId -> (forge, "owner/repo"), or None for repos greenlight can't track."""
    kind, _, rest = (project_id or "").partition(":")
    if kind == "github" and rest:  # "github:octo/greenlight"
        return "github", rest
    host, _, path = rest.partition("/")
    if kind == "git" and host in ("gitlab.com", "github.com") and path:  # "git:gitlab.com/acme/widgets"
        return host.split(".")[0], path
    return None


def ref(forge, n):
    return f"#{n}" if forge == "github" else f"!{n}"


def lifecycle_file(project):
    """~/.greenlight/lifecycles/<org>__<repo>.md or <repo>.md, else bundled, else default.md."""
    parts = [p for p in str(project or "").strip("/").split("/") if p]
    names = ([f"{'__'.join(parts)}.md"] if len(parts) > 1 else []) + ([f"{parts[-1]}.md"] if parts else [])
    candidates = [d / n for d in (STATE / "lifecycles", ROOT / "lifecycles") for n in names]
    return next((f for f in candidates if f.exists()), ROOT / "lifecycles" / "default.md")


BUILTIN = {"mr_open": "An MR exists for the branch.", "ci_green": "The head pipeline succeeded (or there's no CI).",
           "no_conflicts": "No merge conflicts.", "verified": "A fresh verifier passed it end to end on the current head commit.",
           "review_bot": "The review bot is assignee and reviewer.",
           "milestone": "A release milestone is set.", "ready": "Not a draft.", "threads": "No unresolved threads.",
           "approved": "The required approvals are in.", "merge": "The forge reports it mergeable."}
STAGE_RE = re.compile(r"^\s*\d+\.\s+`(\w+)`[ \t]*(?:—|-)?[ \t]*(.*)$", re.M)
DONE_RE = re.compile(r"\s*Done:\s*`(signoff|(?:label|check)=[^`]+)`")
RUN_RE = re.compile(r"\s*Run:\s*`([^`]+)`")  # a signoff step's command: the card's button runs it on the host, then signs it off
OWNER_RE = re.compile(r"\s*Owner:\s*`(agent|me|reviewers|ci)`")
PHASE_RE = re.compile(r"\s*Phase:\s*`(define|build|verify|review|land)`")
# a lifecycle's standing rules and its diff-triggered checklist, either order; "## Rules (all agents)" or "## Rules:" too, as the old partition took them
SECTION_RE = re.compile(r"^##[ \t]+(Rules|Checklist)\b[^\n]*$", re.M)


def lifecycle_doc(project):
    """The lifecycle as data: {stages: [{key, req, done, run, owner?, phase?}], options, rules, checklist, intro, file, source}.
    A custom stage passes on `Done: `signoff``, `Done: `label=x``, or `Done: `check=x``, and may set `Owner:` / `Phase:`."""
    f = lifecycle_file(project)
    parts = SECTION_RE.split(f.read_text())
    head, sections = parts[0], dict[str, str]()
    for i in range(1, len(parts) - 1, 2):  # a section given twice keeps both
        k = parts[i].lower()
        sections[k] = "\n\n".join(x for x in (sections.get(k, ""), parts[i + 1].strip()) if x)
    stages = []
    for key, rest in STAGE_RE.findall(head):
        m, run, own, ph = DONE_RE.search(rest), RUN_RE.search(rest), OWNER_RE.search(rest), PHASE_RE.search(rest)
        req = PHASE_RE.sub("", OWNER_RE.sub("", RUN_RE.sub("", DONE_RE.sub("", rest)))).strip()
        stage = {"key": key, "req": req, "done": m[1] if m else ("" if key in BUILTIN else "signoff"), "run": run[1] if run else ""}
        if own and key not in BUILTIN:
            stage["owner"] = own[1]
        if ph and key not in BUILTIN:
            stage["phase"] = ph[1]
        stages.append(stage)
    m = re.search(r"^Options:(.*)$", head, re.M)
    first = STAGE_RE.search(head) or m
    intro = "" if f.name == "default.md" and f.parent == ROOT / "lifecycles" else \
        re.sub(r"^Options:.*$|\A#.*$", "", head[:first.start()] if first else head, flags=re.M).strip()
    return {"stages": stages, "options": dict(re.findall(r"`(\w+)=?([^`]*)`", m[1])) if m else {},
            "rules": sections.get("rules", ""), "checklist": sections.get("checklist", ""), "intro": intro, "file": str(f),
            "source": "yours" if f.parent == STATE / "lifecycles" else "default" if f.name == "default.md" else "bundled"}


def _format_stage(i, s):
    one = lambda t: " ".join(str(t or "").split())
    done = "" if s["key"] in BUILTIN else one(s.get("done")) or "signoff"
    if not re.fullmatch(r"|signoff|(?:label|check)=[^`\s][^`]*", done):
        raise ValueError(f"bad Done for {s['key']}: {done}")
    run = one(s.get("run")) if done == "signoff" else ""
    if "`" in run:
        raise ValueError(f"a Run command can't hold a backtick: {s['key']}")
    own = "" if s["key"] in BUILTIN else one(s.get("owner"))
    if own not in ("", "agent", "me", "reviewers", "ci"):
        raise ValueError(f"bad Owner for {s['key']}: {own}")
    phase = "" if s["key"] in BUILTIN else one(s.get("phase"))
    if phase not in ("", "define", "build", "verify", "review", "land"):
        raise ValueError(f"bad Phase for {s['key']}: {phase}")
    return (f"{i}. `{s['key']}` — {one(s.get('req'))}" + (f" Done: `{done}`" if done else "")
            + (f" Run: `{run}`" if run else "") + (f" Owner: `{own}`" if own else "") + (f" Phase: `{phase}`" if phase else ""))


def _format_options(opts):
    parts = []
    for k, v in opts.items():
        ks, vs = str(k), str(v or "")
        if not re.fullmatch(r"\w+", ks):
            raise ValueError(f"bad option key: {ks}")
        if any(c in vs for c in "`\n\r"):
            raise ValueError(f"option {ks} can't hold backticks or newlines")
        parts.append(f"`{ks}={vs}`" if vs else f"`{ks}`")
    return "Options: " + " ".join(parts)


def lifecycle_md(repo, doc):
    """The inverse of lifecycle_doc. Raises ValueError on a bad or duplicate key, or invalid stage/option metadata."""
    stages = doc["stages"]
    keys = [s["key"] for s in stages]
    if not keys or len(set(keys)) != len(keys) or not all(re.fullmatch(r"\w+", k) for k in keys):
        raise ValueError(f"stage keys must be unique words: {keys}")
    if late := [k for k in split_merge(keys)[1] if k in BUILTIN]:
        raise ValueError(f"only your own steps can come after merge: {late}")
    by_key = {s["key"]: s for s in stages}
    if late_chk := [k for k in split_merge(keys)[1] if str(by_key[k].get("done") or "").startswith("check=")]:
        raise ValueError(f"CI check stages can't come after merge: {late_chk}")
    if "mr_open" in keys:
        if early_mr := [k for k in keys[:keys.index("mr_open")] if k not in BUILTIN and str(by_key[k].get("done") or "").startswith(("label=", "check="))]:
            raise ValueError(f"only sign-off stages can come before mr_open: {early_mr}")
    out = [f"# {repo} lifecycle", ""] + ([doc["intro"].strip(), ""] if doc.get("intro", "").strip() else [])
    opts = doc.get("options") or {}
    if opts:
        out += [_format_options(opts), ""]
    out += [_format_stage(i, s) for i, s in enumerate(stages, 1)]
    if doc.get("rules", "").strip():
        out += ["", "## Rules", "", doc["rules"].strip()]
    if doc.get("checklist", "").strip():
        out += ["", "## Checklist", "", doc["checklist"].strip()]
    return "\n".join(out) + "\n"


def lifecycle(project):
    return Path(lifecycle_doc(project)["file"]).name, [s["key"] for s in lifecycle_doc(project)["stages"]]


def split_merge(steps):
    """(up to and including `merge`, after it). Steps after `merge` run once the MR has merged."""
    i = steps.index("merge") + 1 if "merge" in steps else len(steps)
    return steps[:i], steps[i:]


def post_merge(project):
    return split_merge(lifecycle(project)[1])[1]


def lifecycle_opts(project):
    """{flag: value} from the backticks on the lifecycle's `Options:` line: `solo` -> {"solo": ""}, `milestone=Release-`."""
    return lifecycle_doc(project)["options"]


def opt_val(opts, key, default=""):
    """Read an option value from lifecycle_opts() (tolerating mocked set objects in tests)."""
    return opts.get(key, default) if isinstance(opts, dict) else default


def repo_bot(project):
    """Review bot for `project`: per-repo `review_bot=` option (`none` disables), else global config `review_bot`."""
    opts = lifecycle_opts(project)
    if "review_bot" in opts:
        val = opt_val(opts, "review_bot").strip()
        return None if not val or val.lower() == "none" else val
    return BOT


def repo_merge_method(project, opts=None):
    """Merge method for `project`: per-repo `merge_method=` option, else global config `merge_method`."""
    val = opt_val(lifecycle_opts(project) if opts is None else opts, "merge_method").strip().lower()
    return val if val in ("merge", "squash", "rebase") else CONFIG["merge_method"]


def repo_human_approval(opts):
    """Whether `project` requires another human's approval: per-repo option overrides global config."""
    ha = opt_val(opts, "human_approval").strip().lower()
    if ha in ("true", "false"):
        return ha == "true"
    return bool(CONFIG["require_human_approval"]) and "no_human_approval" not in opts


HOST_MODES = ("server", "mac", "auto")
_MAC_SKIP_DIRS = frozenset({".git", "node_modules", "vendor", "target", ".build", "DerivedData", "Pods", ".venv", "__pycache__", "dist", "build"})
_MAC_SWIFT_IMPORTS = re.compile(r"^\s*import\s+(?:AppKit|SwiftUI|UIKit|Cocoa|XCTest|XCUIApplication)\b", re.M)
_MAC_BUILD_RE = re.compile(r"\b(?:xcodebuild|swiftc|xcrun\s+simctl)\b|\.app/Contents/MacOS\b")
_MAC_PKG_RE = re.compile(r"\.(?:macOS|iOS|tvOS|watchOS|visionOS)\s*\(")


def _read_head(path, limit=8192):
    try:
        with path.open("r", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _scan_mac_tree(root):
    """Walk up to 2 levels under `root` for Xcode / macOS GUI test markers."""
    for dirpath, dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        for d in dirs:
            if d.endswith((".xcodeproj", ".xcworkspace")):
                return "Xcode project"
            if d.endswith("UITests"):
                return "GUI tests"
        for f in files:
            if f.endswith(".xctestplan"):
                return "Xcode test plan"
            if f in ("Podfile", "project.yml", "Project.swift"):
                return "Xcode project"
            if (f.startswith("test_uitest") and f.endswith(".py")) or f in ("uitest.py", "vm_test.sh"):
                return "GUI tests"
        if depth >= 2:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d not in _MAC_SKIP_DIRS and not d.startswith(".")]
    return None


def _scan_mac_sources(root):
    """Check Package.swift, app build scripts, and Apple UI Swift imports."""
    pkg = root / "Package.swift"
    if pkg.is_file() and _MAC_PKG_RE.search(_read_head(pkg)):
        return "Apple Swift package"
    for rel in ("app/build.sh", "Makefile"):
        if (p := root / rel).is_file() and _MAC_BUILD_RE.search(_read_head(p)):
            return "macOS app build"
    checked = 0
    for sub in ("app/Sources", "Sources", "macos", "macOS", "ios", "iOS", "."):
        d = root / sub
        if not d.is_dir():
            continue
        for sf in sorted(d.glob("*.swift")):
            if _MAC_SWIFT_IMPORTS.search(_read_head(sf, 4096)):
                return "macOS/iOS app"
            checked += 1
            if checked >= 12:
                return None
    return None


_MAC_REASON_CACHE: dict[str, tuple[float, str | None]] = {}
_MAC_REASON_TTL = 300.0


def needs_mac_reason(path):
    """Why a repo checkout needs the Mac (Xcode, macOS/iOS app, GUI tests), or None when it runs fine on Linux."""
    if not path:
        return None
    key = str(path)
    now = time.monotonic()
    if (hit := _MAC_REASON_CACHE.get(key)) and now - hit[0] < _MAC_REASON_TTL:
        return hit[1]
    try:
        root = Path(path)
        if not root.is_dir():
            return None
        reason = _scan_mac_tree(root) or _scan_mac_sources(root)
    except OSError:
        return None
    _MAC_REASON_CACHE[key] = (now, reason)
    return reason


def needs_mac(path):
    """True when the checkout at `path` shows Xcode, local Mac/iOS app, or macOS GUI test markers."""
    return bool(needs_mac_reason(path))


def repo_host_mode(project, opts=None):
    """Per-repo host preference (`server` default, `mac`, or `auto`) from its lifecycle `Options:` line."""
    val = opt_val(lifecycle_opts(project) if opts is None else opts, "host").strip().lower()
    return val if val in HOST_MODES else "server"


def resolve_repo_host(project, path=None, opts=None, check_mac=True):
    """Resolved host preference for `project`: {mode, target ('server'|'mac'), needs_mac, reason}.
    Pass `check_mac=False` on hot paths when `needs_mac` is only needed for `mode == 'auto'`."""
    mode = repo_host_mode(project, opts)
    reason = needs_mac_reason(path) if (path and (check_mac or mode == "auto")) else None
    mac = bool(reason)
    target = "mac" if (mode == "mac" or (mode == "auto" and mac)) else "server"
    return {"mode": mode, "target": target, "needs_mac": mac, "reason": reason or ""}


def signoffs():
    f = STATE / "signoffs.json"
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except (OSError, ValueError):
        return {}


VERIFY_MAX_ROUNDS = 3  # verifier fails this many times in a row on an MR: it goes to the user


def verifications():
    """SHA-pinned verification receipts per MR: {"project#iid": {sha, verdict, round, ts, summary, evidence, defects, unproven}}."""
    f = STATE / "verify.json"
    try:
        vs = json.loads(f.read_text()) if f.exists() else {}
    except (OSError, ValueError):
        return {}
    return vs if isinstance(vs, dict) else {}  # torn or foreign: none, rather than a scan that dies on every MR


def verify_phase(verdict, stale, rnd):
    """Where the builder <-> verifier loop stands: pass, verify (a fresh verifier's turn), fix (the builder's) or escalated (the user's)."""
    if verdict == "pass":
        return "verify" if stale else "pass"
    if verdict == "uncertain":
        return "verify" if stale and rnd < VERIFY_MAX_ROUNDS else "escalated"
    if verdict == "fail":
        return "escalated" if rnd >= VERIFY_MAX_ROUNDS else "verify" if stale else "fix"
    return "verify"


def verify_status(project, iid, head_sha):
    """The MR's verification at head_sha ({..receipt, stale, phase, max_rounds}), or None if it was never verified.
    A receipt for an older commit is stale: a pass no longer counts, and a fail's fix has landed for the verifier to check."""
    rec = verifications().get(f"{project}#{iid}")
    if not rec:
        return None
    sha, rnd = rec.get("sha"), int(rec.get("round") or 0)
    stale = bool(head_sha and sha and not (head_sha.startswith(sha) or sha.startswith(head_sha)))  # a short sha names its commit too
    return {**rec, "stale": stale, "phase": verify_phase(rec.get("verdict"), stale, rnd), "max_rounds": VERIFY_MAX_ROUNDS}


def verify_html(project, iid, rec):
    """A standalone HTML evidence page for one verification run (its verdict, summary, defects and what it couldn't prove); its path."""
    e = html.escape
    out = STATE / "verify" / f"{re.sub(r'[^\w.-]+', '_', f'{project}-{iid}')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    v = rec.get("verdict") or "unknown"
    col = {"pass": "#16a34a", "fail": "#dc2626", "uncertain": "#ea580c"}.get(v, "#64748b")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("ts") or time.time()))
    lst = lambda title, xs: f"<section><h2>{title}</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in xs)}</ul></section>" if xs else ""
    out.write_text(f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Verification: {e(project)} #{e(str(iid))}</title><style>
:root{{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c0a09;--fg:#f5f5f4;--mut:#a8a29e;--line:#292524;--card:#1c1917}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:820px;margin:auto}}h1{{margin:0 0 6px;font-size:20px}}.meta{{color:var(--mut);margin-bottom:16px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;color:#fff;background:{col};margin-right:8px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:12px}}
h2{{font-size:15px;margin:0 0 8px}}code{{font-size:12px}}ul{{margin:0;padding-left:18px}}
</style></head><body><main>
<h1><span class=badge>{e(v.upper())}</span>{e(project)} #{e(str(iid))}</h1>
<div class=meta>{when} · sha <code>{e((rec.get('sha') or 'none')[:12])}</code> · round {int(rec.get('round') or 0)}/{VERIFY_MAX_ROUNDS}</div>
<section><h2>Summary</h2><p>{e(rec.get('summary') or '(no summary)')}</p></section>
{lst("Defects", rec.get("defects"))}{lst("Could not prove", rec.get("unproven"))}
</main></body></html>""")
    return str(out)


def verify_record(project, iid, verdict, sha="", summary="", evidence="", defects=(), unproven=()):
    """Record a verifier's verdict on project#iid at sha (reset: forget it). A fail or uncertain after one counts a round; a pass
    starts over; a verdict on the commit the last one checked replaces it in the same round. Without --evidence, an HTML page is written for it. Returns the stored record (None on reset)."""
    if verdict not in ("pass", "fail", "uncertain", "reset"):
        raise ValueError(f"bad verdict {verdict!r}: expected pass, fail, uncertain or reset")
    key, f = f"{project}#{iid}", STATE / "verify.json"
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "verify.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        vs = verifications()
        if f.exists() and not vs and f.read_text().strip() not in ("", "{}"):  # never write over receipts it can't read
            raise ValueError(f"{f} is unreadable: fix or move it, then record again")
        prev, rec = vs.pop(key, None) or {}, None
        if verdict != "reset":
            last = int(prev.get("round") or 0) if prev.get("verdict") in ("fail", "uncertain") else 0
            rnd = 0 if verdict == "pass" else last if last and prev.get("sha") == (sha or "") else last + 1  # the same commit again: the same round
            rec = {"sha": sha or "", "verdict": verdict, "round": rnd, "ts": int(time.time()), "summary": summary.strip(),
                   "evidence": evidence.strip(), "defects": [d.strip() for d in defects if d and d.strip()],
                   "unproven": [u.strip() for u in unproven if u and u.strip()]}
            rec["evidence"] = rec["evidence"] or verify_html(project, iid, rec)
            vs[key] = rec
        tmp = f.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(vs, indent=1))
        os.replace(tmp, f)
    return rec


def verify_cli(args):
    """greenlight.py verify <project> <iid> <pass|fail|uncertain|reset> [--sha S] [--summary T] [--evidence P] [--defect D]... [--unproven U]..."""
    import argparse
    ap = argparse.ArgumentParser(prog="greenlight.py verify", description="Record a verifier's verdict on an MR's head commit.")
    ap.add_argument("project")
    ap.add_argument("iid")
    ap.add_argument("verdict", choices=("pass", "fail", "uncertain", "reset"))
    ap.add_argument("--sha", default="", help="the commit it verified (the MR's head)")
    ap.add_argument("--summary", default="", help="the verdict in one line")
    ap.add_argument("--evidence", default="", help="a report of the run (path or URL); default: an HTML page written for it")
    ap.add_argument("--defect", action="append", default=[], help="a defect found (repeatable)")
    ap.add_argument("--unproven", action="append", default=[], help="something it could not prove (repeatable)")
    a = ap.parse_args(args)
    if a.verdict != "reset" and not re.fullmatch(r"[0-9a-f]{7,40}", a.sha):
        ap.error("--sha needs the commit it checked (7-40 hex digits): a verdict is pinned to it")
    rec = verify_record(a.project, a.iid, a.verdict, sha=a.sha, summary=a.summary, evidence=a.evidence, defects=a.defect, unproven=a.unproven)
    print(json.dumps(rec or {"reset": f"{a.project}#{a.iid}"}, indent=1))


def release_milestone(project, forge="gitlab"):
    """The active milestone named <prefix>x.y.z (prefix from `milestone=`, default Release-) due latest."""
    prefix = re.escape(opt_val(lifecycle_opts(project), "milestone") or "Release-")
    if forge == "github":
        ms = [{**m, "due_date": (m.get("due_on") or "")[:10]} for m in gh("api", f"repos/{project}/milestones?state=open&per_page=100")]
    else:
        ms = gl_all(f"projects/{pid(project)}/milestones?state=active")
    ms = [m for m in ms if re.fullmatch(prefix + r"\d+\.\d+\.\d+", m["title"]) and m.get("due_date")]
    ms.sort(key=lambda m: m["due_date"], reverse=True)
    if not ms or (len(ms) > 1 and ms[0]["due_date"] == ms[1]["due_date"]):
        return None  # ambiguous: greenlight asks
    return ms[0]


def mr_details(project, iid, forge="gitlab"):
    if forge == "github":
        return gh_pr(project, iid)
    base = f"projects/{pid(project)}/merge_requests/{iid}"
    mr = gl(base)
    if mr["state"] == "opened":
        mr["_discussions"] = gl_all(f"{base}/discussions")
        ap = gl(f"{base}/approvals")
        mr["_approved_by"] = [a["user"]["username"] for a in ap.get("approved_by", [])]
        mr["_approvals_required"] = ap.get("approvals_required") or 0
        mr["_approvals_left"] = ap.get("approvals_left") or 0
        mr["_can_approve"] = ap.get("user_can_approve", True)  # False when my commits are in it
        mine = [n for d in mr["_discussions"] for n in d["notes"] if n["author"]["username"] == ME and (n.get("position") or {}).get("head_sha")]
        # ponytail: the head your newest diff comment saw; an approval or a plain comment carries no sha
        mr["_reviewed_sha"] = max(mine, key=lambda n: n["created_at"])["position"]["head_sha"] if mine else None
        pl = mr.get("head_pipeline") or {}
        if pl.get("status") == "failed":  # the Fix CI prompt's {failed_jobs}
            mr["_failed_jobs"] = [{"name": j["name"], "stage": j["stage"], "url": j["web_url"]}
                                  for j in gl_all(f"projects/{pid(project)}/pipelines/{pl['id']}/jobs?scope[]=failed") if not j.get("allow_failure")]
        if pl.get("id") and any(s["done"].startswith("check=") for s in lifecycle_doc(project)["stages"]):
            mr["_checks"] = [{"name": j["name"], "stage": j.get("stage") or "", "status": j["status"]}
                             for j in gl_all(f"projects/{pid(project)}/pipelines/{pl['id']}/jobs")]
        if mr.get("detailed_merge_status") == "merge_request_blocked":  # depends on other MRs: name the open ones
            mr["_blocked_by"] = [b["blocking_merge_request"]["iid"] for b in gl(f"{base}/blocks")
                                 if b["blocking_merge_request"]["state"] == "opened"]
    return mr


GH_PR_QUERY = """query($owner: String!, $name: String!, $n: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $n) {
      number url title body state isDraft updatedAt author { login avatarUrl }
      headRefName headRefOid baseRefName baseRef { branchProtectionRule { requiredApprovingReviewCount } } mergeable mergeStateStatus reviewDecision
      labels(first: 30) { nodes { name } }
      milestone { title }
      assignees(first: 20) { nodes { login avatarUrl } }
      reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
      latestOpinionatedReviews(first: 30) { nodes { state author { login } } }
      myReviews: reviews(last: 30) { nodes { author { login } commit { oid } } }
      reviewThreads(first: 100) { nodes { isResolved
        first: comments(first: 1) { nodes { author { login } body path line } } last: comments(last: 1) { nodes { author { login } } } } }
      commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { nodes { __typename
        ... on CheckRun { name status conclusion detailsUrl checkSuite { workflowRun { workflow { name } } } }
        ... on StatusContext { context state targetUrl } } } } } } }
    }
  }
}"""
GH_STATE = {"OPEN": "opened", "MERGED": "merged", "CLOSED": "closed"}
GH_ROLLUP = {"SUCCESS": "success", "PENDING": "running", "EXPECTED": "running", "FAILURE": "failed", "ERROR": "failed"}


def gh(*args):
    return json.loads(run(["gh", *args], timeout=45, tries=3))


def gh_open_prs(project):
    return gh("pr", "list", "-R", project, "--state", "open", "--limit", "500",
              "--json", "number,headRefName,closingIssuesReferences")


def gh_check_status(c):
    """A CheckRun / StatusContext -> GitLab job status."""
    if c["__typename"] == "StatusContext":
        return {"SUCCESS": "success", "PENDING": "pending", "EXPECTED": "pending"}.get(c["state"], "failed")
    if c["status"] != "COMPLETED":
        return "running" if c["status"] == "IN_PROGRESS" else "pending"
    return {"SUCCESS": "success", "NEUTRAL": "success", "SKIPPED": "skipped"}.get(c["conclusion"], "failed")


def linked_number(v):
    """Orca's linked MR/PR as its number: newer Orca stores linkedPR as {"number": 39, ...}, not 39."""
    return v.get("number") if isinstance(v, dict) else v


APPROVAL_RULES_TTL = 3600  # a repo's required-approval count, cached per repo and base branch
APPROVAL_RULES_MISS_TTL = 300  # GitHub couldn't say (no rulesets on the server, no access): ask again in 5 minutes


_APPROVAL_RULES_LOCK = threading.Lock()


def _load_approval_rules(f):
    try:
        return json.loads(f.read_text()) if f.exists() else {}
    except (OSError, ValueError):
        return {}


def _save_approval_rules(f, key, entry):
    """mr_details runs on 8 threads: re-read and merge under a lock (no entries lost within one process; two processes
    can still race, which only costs a refetch), then replace atomically (a reader never sees half a file)."""
    with _APPROVAL_RULES_LOCK:
        cache = _load_approval_rules(f) | {key: entry}
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(cache))
            os.replace(tmp, f)
        except OSError:
            pass


def gh_required_approvals(project, branch):
    """Approving reviews GitHub's rulesets require on `branch` (classic protection comes in the PR query):
    an int, cached an hour per repo and branch in approval_rules.json, or None (cached 5 minutes) when GitHub couldn't say."""
    f, key = STATE / "approval_rules.json", f"{project}@{branch}"
    hit = _load_approval_rules(f).get(key) or {}
    age = time.time() - (hit.get("at") or 0)
    if isinstance(hit.get("n"), int) and age < APPROVAL_RULES_TTL:
        return hit["n"]
    if "n" in hit and hit["n"] is None and age < APPROVAL_RULES_MISS_TTL:
        return None
    try:
        pages = gh("api", "--paginate", "--slurp", f"repos/{project}/rules/branches/{urllib.parse.quote(branch, safe='')}?per_page=100")
        rules = [r for pg in pages for r in pg] if isinstance(pages, list) and all(isinstance(pg, list) for pg in pages) else None
    except Exception:  # noqa: BLE001 - unknown, not zero: the PR's reviewDecision still says when one is required
        rules = None
    n = None if not isinstance(rules, list) else max(
        [(r.get("parameters") or {}).get("required_approving_review_count") or 0
         for r in rules if isinstance(r, dict) and r.get("type") == "pull_request"] or [0])
    _save_approval_rules(f, key, {"n": n, "at": int(time.time())})
    return n


def approvals_needed(mr):
    """Whether the forge needs any approval on this MR/PR before it can merge: GitLab's count, or on GitHub the
    branch protection / rulesets count and reviewDecision. False means approvals are no blocker and nothing says otherwise."""
    return bool(mr.get("_approvals_required") or mr.get("_approvals_left")
                or mr.get("detailed_merge_status") in ("not_approved", "requested_changes"))


def gh_pr(project, number):
    """A GitHub PR in the GitLab MR shape mr_details returns. The gh user (the viewer) becomes ME,
    so every author/approver test in check() and evaluate() reads the same for both forges."""
    owner, name = project.split("/", 1)
    d = json.loads(run(["gh", "api", "graphql", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={number}",
                        "-f", f"query={GH_PR_QUERY}"], timeout=45, tries=3))["data"]
    me, repo, p = d["viewer"]["login"], d["repository"], d["repository"]["pullRequest"]
    user = lambda u: {"username": ME if (u or {}).get("login") == me else (u or {}).get("login") or "ghost",
                      "login": (u or {}).get("login") or "ghost", "avatar_url": (u or {}).get("avatarUrl")}
    roll = ((p["commits"]["nodes"] or [{}])[0].get("commit") or {}).get("statusCheckRollup")
    pipeline = roll and GH_ROLLUP.get(roll["state"], "failed")
    threads = [{"notes": [{"resolvable": True, "resolved": t["isResolved"], "author": user(f.get("author")), "body": f.get("body") or "",
                           "position": {"new_path": f.get("path"), "new_line": f.get("line")}},
                          {"author": user((t["last"]["nodes"] or [{}])[0].get("author"))}]}
               for t in p["reviewThreads"]["nodes"] for f in [(t["first"]["nodes"] or [{}])[0]]]
    reviews = [(r["state"], user(r["author"])["username"]) for r in p["latestOpinionatedReviews"]["nodes"]]
    requested = [r["requestedReviewer"].get("login") or r["requestedReviewer"].get("name")
                 for r in p["reviewRequests"]["nodes"] if r.get("requestedReviewer")]
    mss, decision = p["mergeStateStatus"], p["reviewDecision"]
    if p["state"] != "OPEN":
        status = None
    elif p["isDraft"]:
        status = "draft_status"
    elif p["mergeable"] == "CONFLICTING" or mss == "DIRTY":
        status = "conflict"
    elif mss == "BEHIND":
        status = "need_rebase"
    elif decision == "CHANGES_REQUESTED":
        status = "requested_changes"
    elif decision == "REVIEW_REQUIRED":
        status = "not_approved"
    elif pipeline == "running":
        status = "ci_still_running"
    elif mss in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):  # UNSTABLE: only non-required checks failed (ci_green catches them)
        status = "mergeable"
    elif mss == "BLOCKED":
        status = "ci_must_pass" if pipeline == "failed" else "blocked_status"
    else:  # UNKNOWN: GitHub computes mergeability lazily
        status = "checking"
    checks = [{"name": c.get("name") or c.get("context"), "status": gh_check_status(c), "url": c.get("detailsUrl") or c.get("targetUrl"),
               "stage": ((((c.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}).get("name") or "checks")}
              for c in (roll or {}).get("contexts", {}).get("nodes", [])]
    return {
        "iid": p["number"], "web_url": p["url"], "title": p["title"], "description": p.get("body"), "state": GH_STATE[p["state"]], "draft": p["isDraft"],
        "author": user(p["author"]), "has_conflicts": p["mergeable"] == "CONFLICTING",
        "assignees": [user(a) for a in p["assignees"]["nodes"]],
        "reviewers": [{"username": n} for n in dict.fromkeys(requested + [u for _, u in reviews])],
        "milestone": p["milestone"], "labels": [l["name"] for l in p["labels"]["nodes"]],
        "head_pipeline": pipeline and {"status": pipeline, "web_url": p["url"] + "/checks"},
        "detailed_merge_status": status, "blocking_discussions_resolved": all(t["isResolved"] for t in p["reviewThreads"]["nodes"]),
        "source_branch": p["headRefName"], "target_branch": p["baseRefName"], "sha": p["headRefOid"], "updated_at": p["updatedAt"],
        "_discussions": threads, "_approved_by": [u for s, u in reviews if s == "APPROVED"],
        # classic branch protection (null unless you can read it) or the base branch's rulesets, whichever asks more
        "_approvals_required": max((((p.get("baseRef") or {}).get("branchProtectionRule") or {}).get("requiredApprovingReviewCount") or 0),
                                   (gh_required_approvals(project, p["baseRefName"]) or 0) if p["state"] == "OPEN" else 0,
                                   int(decision == "REVIEW_REQUIRED")),  # display only: the rail reads "0 of 1", not "none required", on an unreadable count
        "_approvals_left": int(decision == "REVIEW_REQUIRED"),
        "_can_approve": p["author"] is None or p["author"]["login"] != me,  # GitHub won't let you approve your own PR
        "_reviewed_sha": next((r["commit"]["oid"] for r in reversed((p.get("myReviews") or {}).get("nodes") or [])
                               if (r.get("author") or {}).get("login") == me and r.get("commit")), None),
        "_default_branch": (repo["defaultBranchRef"] or {}).get("name"),
        "_checks": checks, "_failed_jobs": [c for c in checks if c["status"] == "failed"],
        "_forge": "github",
    }


def unresolved(mr):
    """Discussions with a resolvable, unresolved first note."""
    return [d for d in mr.get("_discussions", [])
            if d["notes"] and d["notes"][0].get("resolvable") and not d["notes"][0].get("resolved")]


def awaiting_me(d):
    """An unresolved thread whose last word isn't mine: whoever spoke last, a person or the review bot,
    waits on a fix or a reply. Only my note (a reply, or GitLab's note for my push to the line) hands it back."""
    return d["notes"][-1]["author"]["username"] != ME


def thread_line(d):
    """'author, file:line: "first line"' for one discussion: a {threads} entry."""
    n = d["notes"][0]
    pos = n.get("position") or {}
    where = pos.get("new_path") or pos.get("old_path")
    where = where and f"{where}:{pos.get('new_line') or pos.get('old_line') or ''}".rstrip(":")
    text = next((l.strip() for l in (n.get("body") or "").splitlines() if l.strip()), "")
    text = text[:160] + "…" * (len(text) > 160)
    return ", ".join(filter(None, [n["author"].get("login") or n["author"]["username"], where])) + (f': "{text}"' if text else "")


def said(ws, mr):
    """What I wrote in the goal: the goal (and any old status line) minus the MR's own title, which
    "Merge !N: <title>" goals copy in. A title like "hold inbounds on ..." is never an instruction.
    If the title was edited since, the old copy stays in, so it can still ask me (never merge by mistake)."""
    title = (mr.get("title") or "").strip()
    texts = dict.fromkeys(t for t in ((ws.get("comment", ""),) if ws.get("_goal_backfill") else
                                      (ws.get("goal", ""), ws.get("comment", ""))) if t)
    # An edited title can absorb an explicit hold from an older goal. Keep the
    # hold in that ambiguous case; a false hold is safer than an automatic merge.
    if title and not re.search(r"(?:\(|[—–-]\s*)\s*(?:hold|do ?n[o'’]t merge)\b", title, re.I):
        header = re.compile(rf"^((?:Merge|Review)\s+[#!]\d+:\s*){re.escape(title)}(?=[.!?]?(?:$|\n)|\s+\((?!until\b)|\s+[—–-]\s+)", re.I)
        texts = [header.sub(r"\1", t, count=1) for t in texts]
    return "\n".join(texts)


def when(text, today=None):
    """The first date mentioned in a goal, as YYYY-MM-DD. None when it cannot be resolved."""
    import calendar
    import datetime as dt
    t, today = text.lower(), today or dt.date.today()
    found = []

    def add(pattern, parse):
        for m in re.finditer(pattern, t):
            try:
                d = parse(m)
                found.append((m.start(), d.isoformat() if d else None))
            except ValueError:
                found.append((m.start(), None))

    add(r"\b(\d{4})-(\d\d)-(\d\d)\b", lambda m: dt.date(int(m[1]), int(m[2]), int(m[3])))
    mon = "|".join(MONTHS)
    absolute = rf"(?:\d{{4}}-\d\d-\d\d|(?:{mon})[a-z]*\.? \d{{1,2}}(?:st|nd|rd|th)?(?:,? \d{{4}})?|\d{{1,2}}(?:st|nd|rd|th)? (?:{mon})[a-z]*(?:,? \d{{4}})?)"
    def next_after(m):
        base = when(m[2], today)
        if not base:
            return None
        d = dt.date.fromisoformat(base)
        return d + dt.timedelta(days=(DAYS.index(m[1]) - d.weekday()) % 7 or 7)
    add(rf"\bnext ({'|'.join(DAYS)}) after (?:the )?({absolute})", next_after)
    def month_day(m):
        a, b = (m[1], m[2]) if m[1][:3] in MONTHS else (m[2], m[1])
        years = [int(m[3])] if m[3] else range(today.year, today.year + 9)
        for year in years:
            try:
                d = dt.date(year, MONTHS.index(a[:3]) + 1, int(b))
            except ValueError:
                continue
            if m[3] or d >= today:
                return d
        return None
    add(rf"\b({mon})[a-z]*\.? (\d{{1,2}})(?:st|nd|rd|th)?\b(?:,? (\d{{4}}))?", month_day)
    add(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? ({mon})[a-z]*\b(?:,? (\d{{4}}))?", month_day)
    add(r"\btomorrow\b", lambda m: today + dt.timedelta(days=1))
    def offset(m):
        n = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m[1]) or int(m[1])
        if m[2] == "month":
            year, month = today.year + (today.month - 1 + n) // 12, (today.month - 1 + n) % 12 + 1
            return dt.date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
        return today + dt.timedelta(days=n * {"day": 1, "week": 7}[m[2]])
    add(r"\bin (\d{1,3}|a|one|two|three) (day|week|month)s?\b", offset)
    add(r"\bnext week\b", lambda m: today + dt.timedelta(days=7 - today.weekday()))
    def weekday(m):
        ahead = (DAYS.index(m[1]) - today.weekday()) % 7 or 7
        return today + dt.timedelta(days=ahead)
    add(rf"\b(?:next|on|until|till|after) ({'|'.join(DAYS)})\b", weekday)
    chosen = min(found, key=lambda item: item[0], default=(len(t), None))[1]
    if chosen and re.match(r"^\s*(?:until\s+)?after\s+", t):
        return (dt.date.fromisoformat(chosen) + dt.timedelta(days=1)).isoformat()
    return chosen


def goal_clauses(text):
    """Split separate instructions, keeping month abbreviations such as `Dec. 3` together."""
    return re.split(r"[;\n]|(?<=[!?])\s+|(?<=\.)\s+(?!\d)|"
                    r",\s+(?=(?:and|but|come back|revisit|return to|resume|pick (?:this )?up)\b)|"
                    r"\band\s+(?=(?:revisit|check|review)\b)", text, flags=re.I)


def work_resumes_on(text):
    """A date that explicitly ends a work pause, rather than a deadline or a status-check date."""
    mon = "|".join(MONTHS)
    date_head = rf"(?:\d{{4}}-\d\d-\d\d|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{mon})|(?:{mon})[a-z]*\.?\s+\d|tomorrow|next\s+\w+|in\s+(?:\d+|a|one|two|three)\s+(?:day|week|month)|{'|'.join(DAYS)})"
    parts = goal_clauses(text)
    conditional_pause = False
    for part in parts:
        if m := re.search(r"\b(?:pause|park|hold|leave|set (?:this |it |the work )?aside|(?:do not|don['’]t) work on (?:this|it|the work))\b[^.;\n]*?\buntil\b", part, re.I):
            if re.search(r"\bmerg(?:e|ing)\b", m.group(), re.I):
                continue  # a merge hold is not a pause of the work
            tail = part[m.end():].strip()
            if re.match(rf"^(?:(?:on|after)\s+)?{date_head}", tail, re.I) and (d := when("until " + tail)):
                return d
            conditional_pause = True  # an approval/CI condition is not replaced by a later status date
        if m := re.search(r"\b(?:resume(?: work)?|restart(?: work)?|come back(?: to (?:it|this|work))?|return to (?:it|this|work)|revisit(?: (?:it|this|work))?)\b", part, re.I):
            status_check = re.search(r"\b(?:to (?:check|review)(?: (?:its|the|our))? (?:status|progress)|for (?:a )?status (?:check|update)|status (?:check|update))\b", part, re.I)
            if conditional_pause or status_check:
                continue
            tail = part[m.end():].strip()
            if re.match(rf"^(?:on|by|after)\s+{date_head}|^{date_head}", tail, re.I) and (d := when(tail)):
                return d
    return None


def intents(ws, mr=None):
    """What my goal tells greenlight: {is_held, stay_draft, is_parked, revisit_date (YYYY-MM-DD or None)}, read from
    said(): the patterns first, then System One for the same thing in other words. Once per scan row."""
    if "_intents" in ws:
        return ws["_intents"]
    text = said(ws, mr or {})
    out = {"is_held": bool(HOLD_RE.search(text)), "stay_draft": bool(KEEP_DRAFT_RE.search(text)),
           "is_parked": bool(PARKED_RE.search(text)), "revisit_date": (m := REVISIT_RE.search(text)) and m[1]}
    model_on = decide.configured() != "local"
    prior = ws.get("_revisit") or {}
    if model_on and not out["revisit_date"] and prior.get("text") == text:
        out["revisit_date"] = prior.get("date")
    if not model_on:
        ws.pop("_revisit", None)
    if text.strip() and model_on:
        asked = {k: {"type": "bool", "question": q, "context": "The goal a user wrote for a coding workspace; a PR/MR title in it is a name, not an instruction."}
                 for k, (q, _) in INTENTS.items() if not out.get("revisit_date" if k == "revisit" else k)}
        got = decide.batch(text, asked) if asked else {}
        for k, a in got.items():
            if a["provider"] != "local" and a["prob"] >= INTENTS[k][1]:
                if k == "revisit":
                    parts = goal_clauses(text)
                    cues = [re.search(r"\b(?:revisit|come back|return to|resume|pick (?:this )?up|until)\b", part, re.I) for part in parts]
                    dated = []
                    for part, cue in zip(parts, cues):
                        if not cue:
                            continue
                        suffix = part[cue.end():]
                        d = when(suffix) or when(part[cue.start():])
                        if not d and not re.search(r"\b(?:later|after|in|next|on|until|by)\b", suffix, re.I):
                            d = when(part[:cue.start()])
                        dated.append(d)
                    out["revisit_date"] = next((d for d in dated if d), None) or (when(text) if not any(cues) and len(parts) == 1 else None)
                else:
                    out[k] = True
    if model_on and not out["revisit_date"]:
        out["revisit_date"] = work_resumes_on(text)  # an explicit dated work pause must not become indefinite on a weak model answer
    resumes_work = (out["revisit_date"] and work_resumes_on(text) == out["revisit_date"])
    if prior.get("text") == text and prior.get("date") == out["revisit_date"]:
        resumes_work = prior.get("resumes_work", resumes_work)
    if resumes_work and not PARKED_RE.search(text) and not re.search(r"\bindefinitely\b|until (?:I|you) unpark", text, re.I):
        out["is_parked"] = False  # a dated work pause ends on its date; a dated status check does not unpark work
    ws["_intents"] = out
    if out["revisit_date"]:
        ws["_revisit"] = {"text": text, "date": out["revisit_date"], "resumes_work": bool(resumes_work)}
    return out


def _check_custom(step, mr, ws):
    """Evaluate a repo's own custom stage: `label=x`, `check=name`, or `signoff`, respecting optional `Owner:`."""
    proj = ws["project"]
    stage = next(s for s in lifecycle_doc(proj)["stages"] if s["key"] == step)
    name, req = step.replace("_", " "), stage["req"] or "done?"
    done, own = stage["done"], stage.get("owner") or ""
    if done.startswith("label="):
        label = done[6:]
        return None if mr and label in mr.get("labels", []) else (own or "agent", f"{name}: {req} (add the label `{label}` when it's met)", None)
    if done.startswith("check="):
        want = done[6:]
        hits = [c for c in (mr.get("_checks") or []) if c.get("name") == want] if mr else []
        if any(c.get("status") in ("success", "skipped") for c in hits):
            return None
        if any(c.get("status") in ("running", "pending", "queued", "in_progress", "created") for c in hits):
            return "ci", f"{name}: check `{want}` running", None
        if not hits and mr and (mr.get("head_pipeline") or {}).get("status") in ("running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"):
            return "ci", f"{name}: waiting for check `{want}`", None
        return own or "agent", f"{name}: {req} (check `{want}` has not passed)", None
    ws_branch, mr_branch = ws.get("branch") or "", (mr or {}).get("source_branch") or ""
    ref_id = str(mr["iid"]) if mr else f"@{ws_branch or ws.get('name') or 'branch'}"
    so = signoffs()
    keys = ([f"{proj}#{mr['iid']}"] if mr else []) + [f"{proj}@{b}" for b in dict.fromkeys((mr_branch, ws_branch, "" if mr else ref_id[1:])) if b]
    if any(step in so.get(k, []) for k in keys):
        return None
    return own or "me", f"{name}: {req} Sign it off?", ["python3", str(HERE / "greenlight.py"), "signoff", proj, ref_id, step]


def _check_verify(proj, mr):
    """The built-in `verified` step: a fresh verifier's SHA-pinned pass on the head commit. A fail goes back to the builder, then to a
    fresh verifier once a fix lands, up to VERIFY_MAX_ROUNDS; then (or on uncertain) to the user."""
    v = verify_status(proj, mr["iid"], mr.get("sha"))
    if not v:
        return "agent", "Verify changes end to end (fresh verifier)", None
    if v["phase"] == "pass":
        return None
    why = v.get("summary") or (v.get("defects") or ["see the verification evidence"])[0]
    if v["phase"] == "escalated":
        if v["verdict"] == "uncertain":
            return "me", f"verifier uncertain: {why}", None
        return "me", f"verification failed {v['round']}/{VERIFY_MAX_ROUNDS} rounds: {why}", None
    if v["phase"] == "fix":
        return "agent", f"Fix verification defects (round {v['round']}/{VERIFY_MAX_ROUNDS}): {why}", None
    if v["verdict"] == "pass":
        return "agent", f"Re-verify changes (new commits since verified {(v.get('sha') or '')[:7]})", None
    return "agent", f"Re-verify after fixes (round {v['round'] + 1}/{VERIFY_MAX_ROUNDS})", None


def check(step, mr, ws, milestone):
    """-> None when the step passes, else (owner, action, cmd|None)."""
    if step not in BUILTIN:
        return _check_custom(step, mr, ws)
    iid, proj = mr["iid"], ws["project"]
    github = ws.get("forge") == "github"
    upd = ["gh", "pr", "edit", str(iid), "-R", proj] if github else ["glab", "mr", "update", str(iid), "-R", proj]
    if step == "mr_open":
        return None
    if step == "ci_green":
        st = (mr.get("head_pipeline") or {}).get("status")
        if st in (None, "success", "manual", "skipped"):
            return None
        if st in ("running", "pending", "created", "waiting_for_resource", "preparing", "scheduled"):
            return "ci", f"pipeline {st}", None
        return "agent", f"Fix CI (pipeline {st}: {mr['head_pipeline']['web_url']})", None
    if step == "no_conflicts":
        return ("agent", "Rebase Branch (merge conflicts)", None) if mr["has_conflicts"] else None
    if step == "verified":
        return _check_verify(proj, mr)
    if step == "review_bot":
        bot = repo_bot(proj)
        if not bot:
            return None
        a = {u["username"] for u in mr["assignees"]}
        r = {u["username"] for u in mr["reviewers"]}
        if bot in a and bot in r:
            return None
        return "greenlight", f"assign {bot} as assignee + reviewer", upd + (["--add-assignee", bot, "--add-reviewer", bot] if github
                                                                   else ["--assignee", f"+{bot}", "--reviewer", f"+{bot}"])
    if step == "milestone":
        if mr.get("milestone"):
            return None
        if not milestone:
            return "me", "which milestone? latest active release is ambiguous", None
        return "greenlight", f"set milestone {milestone['title']}", upd + ["--milestone", milestone["title"]]
    if step == "ready":
        if not mr["draft"]:
            return None
        ready = ["gh", "pr", "ready", str(iid), "-R", proj] if github else upd + ["--ready"]
        if intents(ws, mr)["stay_draft"]:
            return "me", "goal says keep draft but everything else is green: take it out of draft?", ready  # the card's Mark ready runs it
        return "greenlight", "mark ready for review", ready
    if step == "threads":
        ds = unresolved(mr)
        todo = [d for d in ds if awaiting_me(d)]  # a bot's unanswered finding is work too, not an answer
        if ds and not todo:  # we replied last on every one: the reviewer has to re-review
            return "reviewers", f"{len(ds)} threads answered, waiting on reviewer re-review", None
        if todo or mr.get("blocking_discussions_resolved") is False:  # GitLab's flag catches what the first-note test misses
            return "agent", f"Resolve MR Threads ({len(todo) or 'GitLab says some'} unresolved)", None
        return None
    if step == "approved":
        # GitLab's status names only its first blocker (a dependency MR hides not_approved): check the count too
        if mr.get("detailed_merge_status") in ("not_approved", "requested_changes") or mr.get("_approvals_left"):
            return "reviewers", "waiting on approvals", None
        return None
    if step == "merge":
        st = mr.get("detailed_merge_status")
        opts = lifecycle_opts(proj)
        how = repo_merge_method(proj, opts)
        merge = (["gh", "pr", "merge", str(iid), "-R", proj, f"--{how}"] if github
                 else ["glab", "mr", "merge", str(iid), "-R", proj, "--yes"] + ([f"--{how}"] if how != "merge" else []))
        if st == "mergeable" and intents(ws, mr)["is_held"]:
            return "me", "mergeable, but the goal says hold: merge now?", merge  # yes runs it (the card's Merge button too)
        base, default = mr.get("target_branch"), mr.get("_default_branch")
        if st == "mergeable" and default and base != default and "merge_any_base" not in opts:
            # a stacked PR: it lands with (or after) its base, never auto-merged into a side branch
            if mr.get("_base_mr"):
                return "dependency", f"targets {base}: waiting on {ref(ws.get('forge'), mr['_base_mr'])} to merge first", None
            return "me", f"mergeable into {base}, not {default}: merge it by hand?", None
        keys = [s["key"] for s in lifecycle_doc(proj)["stages"]]
        checks = [k for k in (keys[:keys.index("merge")] if "merge" in keys else keys) if k not in BUILTIN]
        auto_approval = "automerge" in opts and bool(checks)
        bot = repo_bot(proj)
        if st == "mergeable" and repo_human_approval(opts) and not set(mr.get("_approved_by", [])) - {ME, bot}:
            if not auto_approval:
                if "solo" in opts:  # nobody else reviews this repo: I do, then greenlight runs the cmd
                    return "me", "mergeable, and nobody else reviews this repo: review it, then merge?", merge
                # require_human_approval means "a human signs off": a reviewer where the forge requires one, else you, by
                # merging. Only the repo's own human_approval=true waits for a reviewer the forge doesn't require.
                explicit = opt_val(opts, "human_approval").strip().lower() == "true"
                if not approvals_needed(mr) and not explicit:  # the forge requires no approval: no reviewer is waited on, the merge is yours
                    return "me", "mergeable, nothing blocks it: merge it?", merge
                return "reviewers", "mergeable but no human approval yet (only a bot or you approved)", None
        if st == "mergeable":
            import audit  # every unattended merge goes through the semantic audit, regardless of approval route
            v = audit.merge_verdict(ws, mr)
            pin = ([sys.executable, str(Path(__file__).resolve().with_name("audit.py")), "checked-merge", "github" if github else "gitlab",
                    proj, str(iid), base, mr["sha"], v["target_oid"], *merge]
                   if mr.get("sha") and base and v.get("target_oid") else None)
            if v.get("pending"):
                return "ci", f"automerge waits on the semantic audit of {(mr.get('sha') or '')[:8]}", merge  # the user can still click Merge
            if not v.get("ok") or not pin:
                manual_pin = merge + ["--match-head-commit" if github else "--sha", mr["sha"]] if mr.get("sha") else merge
                return "me", f"automerge held by the semantic audit: {audit.summary(v)}. Merge anyway?", pin or manual_pin
            return "greenlight", "merge (semantic audit passed)", pin
        if st == "need_rebase":
            if "auto_rebase" in opts:
                return "agent", "Rebase Branch (behind target branch)", None
            return "me", "GitLab needs a rebase to merge (no conflict): rebase it?", None
        if st in ("ci_must_pass", "ci_still_running", "checking", "unchecked", "preparing"):
            return "ci", f"merge status {st}", None
        if st == "merge_request_blocked" and mr.get("_blocked_by"):
            return "dependency", "waiting on " + ", ".join(f"!{i}" for i in mr["_blocked_by"]) + " to merge first", None
        if st == "blocked_status":
            return "me", "GitHub blocks the merge (branch protection): check the PR", None
        return "me", f"not mergeable: {st}", None
    return None


def _step_result(proj, step, hit, name):
    owner, action, cmd = hit
    out = {"step": step, "owner": owner, "action": action, "cmd": cmd, "lifecycle": name}
    if step not in BUILTIN:
        stage = next((s for s in lifecycle_doc(proj)["stages"] if s["key"] == step), None)
        if stage and stage.get("phase"):
            out["phase"] = stage["phase"]
    return out


def evaluate(ws, mr, milestone):
    """-> {step, owner, action, cmd}."""
    goal = intents(ws, mr)
    snooze = goal["revisit_date"]
    if PLAN_RE.match(ws["goal"]):
        return {"step": "plan", "owner": "parked", "action": "being planned in the Plan view", "hidden": True}
    if goal["is_parked"] and not (mr and mr["state"] in ("merged", "closed")):
        return {"step": "parked", "owner": "parked", "action": "parked until you unpark it", "hidden": True}
    if snooze and (ws.get("_revisit", {}).get("resumes_work") or REVISIT_RE.search(said(ws, mr or {}))) and snooze > time.strftime("%Y-%m-%d") and not (mr and mr["state"] in ("merged", "closed")):
        return {"step": "parked", "owner": "parked", "action": f"parked until {snooze}"}
    name, steps = lifecycle(ws["project"])
    if not mr:
        if ws.get("branch_gone"):
            return {"step": "done", "owner": "done", "action": "its branch was deleted on the remote, so the change is over: cleanup.py removes it if its commits are on the remote, else delete it yourself"}
        if ws["branch"].startswith("auto-") and not ws["goal"]:
            return {"step": "automation", "owner": "me", "action": "automation run with no MR: delete?"}
        if not ws["goal"]:
            return {"step": "goal", "owner": "me", "action": "no MR and no goal: what's this workspace for?"}
        if AUTOMATION_RE.search(ws["goal"]):
            return {"step": "freeform", "owner": "working", "action": "run by an Orca automation"}
        if "mr_open" in steps:
            for step in steps[:steps.index("mr_open")]:
                if step not in BUILTIN and (hit := check(step, None, ws, milestone)):
                    return _step_result(ws["project"], step, hit, name)
        return {"step": "freeform", "owner": "agent", "action": f"work toward goal: {ws['goal']}"}
    pre, post = split_merge(steps)
    if mr["state"] == "merged" and mr["author"]["username"] == ME:
        for step in post:  # your own steps after the merge (deploy, verify): signoff/label/check only, the MR is done
            if step not in BUILTIN and (hit := check(step, mr, ws, milestone)):
                return _step_result(ws["project"], step, hit, name)
    if mr["state"] in ("merged", "closed"):
        return {"step": "done", "owner": "done", "action": f"MR {mr['state']}: cleanup.py removes it"}
    if mr["author"]["username"] != ME:
        if ME in mr.get("_approved_by", []):
            return {"step": "done", "owner": "done", "action": "approved by me: remove workspace"}
        mine = [d for d in unresolved(mr) if d["notes"][0]["author"]["username"] == ME]
        if not mine and not mr.get("_can_approve", True):
            return {"step": "review", "owner": "reviewers", "action": "your commits are in it: you can't approve, waiting on others"}
        if not mine:
            return {"step": "review", "owner": "me", "action": "review it"}
        if any(awaiting_me(d) for d in mine):
            return {"step": "review", "owner": "me", "action": "author replied on your threads: re-review"}
        return {"step": "review", "owner": "author", "action": f"{len(mine)} of your threads open, waiting on author"}
    for step in pre:
        if hit := check(step, mr, ws, milestone):
            return _step_result(ws["project"], step, hit, name)
    return {"step": "merge", "owner": "ci", "action": f"all steps green, waiting on {'GitHub' if ws.get('forge') == 'github' else 'GitLab'}",
            "lifecycle": name}


def upstream_gone(r):
    """The branch was pushed and its remote branch has since been deleted (as of the checkout's last `fetch --prune`,
    which cleanup.py runs). Merged or abandoned on the forge: either way the change is over."""
    if not os.path.isdir(r["path"]):
        return False
    p = subprocess.run(["git", "-C", r["path"], "for-each-ref", "--format=%(upstream:track)", f"refs/heads/{r['branch']}"],
                       capture_output=True, text=True)
    return p.stdout.strip() == "[gone]"


def screen(ws, terminals):
    """What the workspace's newest agent terminal shows (ade_common.agent_state over its last 60 lines), or None."""
    agent_type, pane = canonical_agent(ws["agent"].get("type")), ws["agent"].get("pane_key")
    if ws["agent"].get("state") in ("working", "waiting") and not pane:
        return None  # another same-type session cannot prove this agent is done or has answered its prompt
    terms = sorted((t for t in terminals.get(ws["id"], []) if
                    (f"{t.get('tabId')}:{t.get('leafId')}" == pane if pane else
                     t.get("agentIdentity") and (not agent_type or canonical_agent(t["agentIdentity"]) == agent_type))),
                   key=lambda t: t.get("lastOutputAt") or 0, reverse=True)
    if not terms:
        return None
    ws["agent"]["terminal"] = terms[0]["handle"]
    try:
        tail = orca("terminal", "read", "--terminal", terms[0]["handle"], "--limit", "60")["terminal"].get("tail", "")
    except Exception:
        return None
    lines = tail.splitlines() if isinstance(tail, str) else [str(x) for x in tail]
    seen = ade_common.agent_state(lines, ws["agent"].get("type") or terms[0].get("agentIdentity") or "")
    visible = [t for x in lines if (t := ade_common.plain(str(x)).strip()) and t.lower() != "? for shortcuts" and
               (t == "┃" and agent_type == "opencode" or not re.fullmatch(r"[│┃|\s─━═\-–—╭╮╰╯]+", t))]
    if seen["state"] == "finished" and visible:
        prompt = visible[-1]
        inner = ade_common.BOX_RE.sub("", prompt).strip() if ade_common.BOX_RE.search(prompt) else prompt
        idle_prompt = (prompt == "┃" and agent_type == "opencode") or (inner[:1] in "❯›>" and not ade_common.draft([prompt]))
        seen["idle_prompt"] = bool(idle_prompt)
        if len(visible) >= 2:
            completed = re.match(r"^(?:I )?(?:Done|Fixed|Added|Updated|Implemented|Completed|Resolved|Built|Verified|Repaired|Refactored|Wrote|Ran|Finished|Changes complete|Tests passed|"
                                 r"(?:The (?:fix|changes|work|task)|Everything) (?:is|are) (?:in place|done|complete|ready))\b", visible[-2], re.I)
            seen["idle_complete"] = bool(idle_prompt and completed)
    ws["agent"]["screen"] = {k: seen[k] for k in ("state", "prob", "provider")}
    if seen.get("ask"):
        ws["agent"]["asking"] = seen["ask"]
    else:
        ws["agent"].pop("asking", None)
    return seen


def prompt_text(ag):
    """What the agent's prompt asks (its question, and the command it wants to run), for an action line; "" if none."""
    return ade_common.format_prompt_text(ag.get("asking") if isinstance(ag, dict) else None)


def waiting_action(ag, where=""):
    """The "waiting on you" action for an agent stopped on a prompt, naming what it asks when that's known."""
    what = prompt_text(ag)
    return f"agent is waiting on you{where}: {what[:300]}" if what else f"agent is waiting on you{where}"


EXTERNAL_STALE_H = 6  # an agent outside Greenlight running this long (since its process started: idle can't be seen)
                      # is the user's to close, not "working"
SURE = 0.7  # how sure the screen reading must be before it overrides Orca's state or the step's owner


def agent_overlay(ws, terminals):
    """Agent-owned work is only actionable if no agent is already on it, and the agent is alive. The terminal says
    which: logged out, out of usage, looping, a draft left in the box, or a stale "waiting" (a background task's
    or a restored session's alert, while the screen shows it working or done)."""
    nxt, ag, seen = ws["next"], ws["agent"], False  # False: not read yet
    if ag.get("external") and nxt["owner"] in ("agent", "greenlight"):  # its terminal isn't ours to read or type into
        # the outside agent's own kind (the shown one may be Greenlight's); none: by_worktree_safe's "the check failed"
        who, h = ag.get("ext_type") or "an agent", (NOW_MS - ag["since"]) / 3.6e6 if ag.get("since") else 0
        if not ag.get("ext_type"):
            nxt.update(owner="working", action="the process check failed, so whether an agent runs here outside Greenlight "
                                               "can't be told: greenlight holds off until it can (diag.py errors says why)")
        elif h >= EXTERNAL_STALE_H:  # open for hours: likely done and idle at its prompt, which only the user can close
            # no hour count in the text: the question's marker is the action, and a changing one re-asks every hour
            nxt.update(owner="me", action=f"{who} has been running here outside Greenlight for over {EXTERNAL_STALE_H}h: once "
                                          "it's done, close it (or take it over in the Basement) and greenlight carries on")
        else:
            nxt.update(owner="working", action=f"{who} runs here outside Greenlight: nothing is dispatched or merged until "
                                               "it exits (then take it over in the Basement)")
        return
    if ag["state"] == "waiting":
        seen = screen(ws, terminals)
        if seen and seen["prob"] >= SURE and seen["state"] == "working":  # still at it: nobody acts on the workspace meanwhile
            ag["state"] = "working"
            if nxt["owner"] in ("agent", "greenlight"):
                nxt["owner"] = "working"
            return
        if seen and seen["prob"] >= SURE and seen["state"] == "unsent_draft":
            nxt.update(owner="me", action=f"a draft sits unsent in {ag['terminal']}'s input box: send or clear it, then greenlight dispatches")
            return
        if seen and seen["prob"] >= SURE and seen["state"] in ("finished", "rate_limited") and nxt["owner"] == "agent":
            if seen["state"] == "finished":
                ag["state"] = "done"  # only to dispatch again: a stale "waiting" never lets greenlight merge or mark ready
        elif seen and seen["prob"] >= SURE and seen["state"] == "logged_out":
            nxt.update(owner="me", action=f"agent logged out: /login in {ag['terminal']}, then greenlight re-dispatches")
            return
        else:
            where = f" in {ag['terminal']}" if ag.get("terminal") else ""
            nxt.update(owner="me", action=waiting_action(ag, where) if ag.get("asking") else f"agent is waiting on you: {ag['last'][:200]}")
            return
    if nxt["owner"] == "greenlight" and ag["state"] == "working":
        seen = screen(ws, terminals) if seen is False else seen
        if seen and seen["prob"] >= SURE and seen["state"] in ("logged_out", "rate_limited", "unsent_draft", "waiting_for_user", "stuck_looping"):
            ag["state"] = "waiting" if isinstance(seen, dict) and seen.get("state") == "waiting_for_user" else "done"  # a prompt is never done
            asks = f": {prompt_text(ag)[:300]}" if ag.get("asking") else ""
            action = {"logged_out": f"agent logged out: /login in {ag['terminal']} before greenlight continues",
                      "rate_limited": f"agent rate limited in {ag['terminal']}: inspect before greenlight continues",
                      "unsent_draft": f"a draft sits unsent in {ag['terminal']}: send or clear it before greenlight continues",
                      "waiting_for_user": f"agent is waiting on you in {ag['terminal']} before greenlight continues{asks}",
                      "stuck_looping": f"agent looks stuck in a loop in {ag['terminal']}: inspect before greenlight continues"}[seen["state"]]
            nxt.update(owner="me", action=action)
            return
        if not seen or seen["state"] != "finished" or not seen.get("idle_prompt") or (seen["prob"] < SURE and not seen.get("idle_complete")):
            nxt["owner"] = "working"  # a mechanical step cannot run while an agent is editing
        return
    if nxt["owner"] != "agent":
        return
    if ag["state"] == "working":
        seen = screen(ws, terminals) if seen is False else seen
        if seen and seen["state"] == "finished" and seen.get("idle_prompt") and (seen["prob"] >= SURE or seen.get("idle_complete")):
            ag["state"] = "done"
        elif not seen or seen["prob"] < SURE or seen["state"] in ("working", "finished"):
            nxt["owner"] = "working"
            return
    seen = screen(ws, terminals) if seen is False else seen
    if not seen or seen["prob"] < SURE:
        return
    h = ag["terminal"]
    if seen["state"] == "working":
        nxt["owner"] = "working"
    elif seen["state"] == "logged_out":
        nxt.update(owner="me", action=f"agent logged out: /login in {h}, then greenlight re-dispatches")
    elif seen["state"] == "rate_limited":
        cur_type = canonical_agent(ag.get("type"))
        now = cur_type if cur_type in CONFIG["agents"] else (CONFIG["agents"][0] if CONFIG["agents"] else "agent")
        others = [a for a in CONFIG["agents"] if a != now]
        if others:
            nxt["agent"] = others[0]
            nxt["action"] += f" [{now} out of tokens: dispatch with {nxt['agent']}]"
        else:
            nxt.update(owner="me", action=f"{now} out of tokens in {h}: wait for reset or configure another agent")
    elif seen["state"] == "stuck_looping":
        nxt.update(owner="me", action=f"agent looks stuck in a loop in {h}: look at it, then stop or redirect it")
    elif seen["state"] == "unsent_draft":
        nxt.update(owner="me", action=f"a draft sits unsent in {h}'s input box: send or clear it, then greenlight dispatches")
    elif seen["state"] == "waiting_for_user":
        ag["state"] = "waiting"  # stopped on a prompt: never idle, so nothing dispatches over it or cleans it up
        nxt.update(owner="me", action=waiting_action(ag, f" in {h}"))


def backfills_load():
    try:
        return json.loads((STATE / "goal_backfill.json").read_text())
    except (OSError, ValueError):
        return {}


def backfill_revision():
    try:
        stat = (STATE / "goal_backfill.json").stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


@contextlib.contextmanager
def backfill_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "goal_backfill.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class BackfillChanged(Exception):
    """A scan must recompute against the goal overlay it publishes with."""


ANY_BACKFILL_REVISION = object()


def backfills_update(change):
    """Serialize goal overlay edits from ticks and standalone reports. Only writes if data actually changed."""
    with backfill_lock():
        data = backfills_load()
        before = json.dumps(data, sort_keys=True)
        change(data)
        if json.dumps(data, sort_keys=True) != before:
            backfills_write_unlocked(data)
            return True
        return False


def backfills_write_unlocked(data):
    tmp = STATE / "goal_backfill.tmp"
    tmp.write_text(json.dumps(data))
    os.replace(tmp, STATE / "goal_backfill.json")


def goal_with_backfill(comment, worktree_id, backfills, branch=None, linked_mr=None, forge=None, project=None):
    """A generated goal applies only while the user's Orca comment still matches the original one."""
    goal = "" if STATUS_LINE_RE.search(comment) else comment
    remembered = backfills.get(worktree_id)
    if not remembered:
        return goal, False
    if ((comment != remembered.get("comment") and not (not remembered.get("comment") and not goal))
            or branch is not None and remembered.get("branch", branch) != branch
            or forge is not None and remembered.get("forge") != forge
            or project is not None and remembered.get("project") != project
            or "linked_mr" in remembered and remembered["linked_mr"] != linked_mr):
        return goal, True  # the user has superseded this generated goal
    return goal or remembered.get("goal") or "", False


def scan(save=True, _backfill_retries=0):
    scan_started_ns = time.monotonic_ns()
    started = int(time.time())  # wall clock, for the dashboard: a scan that began after an action can show its result
    clock_id = scan_clock_id()
    wts = orca("worktree", "ps", "--limit", "10000")["worktrees"]
    full = {w["id"]: w for w in orca("worktree", "list", "--limit", "10000")["worktrees"]}
    terminals = {}
    for t in orca("terminal", "list", "--limit", "10000")["terminals"]:
        terminals.setdefault(t["worktreeId"], []).append(t)

    projects = {w["repoId"]: fp for w in full.values() if (fp := forge_project(w.get("projectId")))}
    backfill_before = backfill_revision()
    backfills = backfills_load()
    backfill_read = backfill_revision()
    if backfill_before != backfill_read:
        if _backfill_retries >= 2:
            raise OSError("goal backfill changed repeatedly during scan")
        return scan(save=save, _backfill_retries=_backfill_retries + 1)
    superseded_backfills = {}
    rows, skipped, bases = [], 0, base_branches(wts)
    for w in wts:
        branch = (w.get("branch") or "").removeprefix("refs/heads/")
        if w.get("isMainWorktree") or not branch or w["repoId"] not in projects or on_base(w, bases):
            skipped += 1  # main checkouts, detached HEADs (cache trees), repos on neither GitLab nor GitHub, base-branch copies
            continue
        forge, project = projects[w["repoId"]]
        ag = primary_agent(w.get("agents"))
        comment = (w.get("comment") or "").strip()
        linked = linked_number(w.get("linkedPR" if forge == "github" else "linkedGitLabMR"))
        goal, superseded = goal_with_backfill(comment, w["worktreeId"], backfills, branch, linked, forge, project)
        if superseded:
            superseded_backfills[w["worktreeId"]] = backfills[w["worktreeId"]]
        rows.append({
            "id": w["worktreeId"], "name": w["displayName"], "path": w["path"], "branch": branch,
            "project": project, "forge": forge, "goal": goal,
            "_goal_backfill": bool(not superseded and backfills.get(w["worktreeId"]) and
                                   goal == backfills[w["worktreeId"]].get("goal") and (not comment or STATUS_LINE_RE.search(comment))),
            "comment": comment,
            "pinned": w.get("isPinned"), "linked_mr": linked,
            "agent": {"state": ag.get("state", "none"), "type": canonical_agent(ag.get("agentType")),
                      # any outside agent, even behind our own (an auto-merge must not land under it)
                      **({"external": True, "since": ext.get("stateStartedAt"), "ext_type": canonical_agent(ext.get("agentType"))}
                         if (ext := next((a for a in w.get("agents") or [] if a.get("external")), None)) else {}),
                      "pane_key": ag.get("paneKey"),
                      "last": (ag.get("lastAssistantMessage") or "")[-400:],
                      **({"asking": ag["asking"]} if isinstance(ag.get("asking"), dict) else {}),  # the prompt it's stopped on
                      "idle_h": w.get("lastOutputAt") and round((NOW_MS - w["lastOutputAt"]) / 3.6e6, 1)},
        })
    if superseded_backfills and save:
        fresh = {w["worktreeId"]: w for w in orca("worktree", "ps", "--limit", "10000")["worktrees"]}
        with backfill_lock():
            if backfill_revision() == backfill_read:
                current = backfills_load()
                before = json.dumps(current, sort_keys=True)
                for wid, old in superseded_backfills.items():
                    live = fresh.get(wid)
                    if not live or current.get(wid) != old:
                        continue
                    identity = projects.get(live.get("repoId"))
                    if not identity:
                        continue
                    live_forge, live_project = identity
                    link = linked_number(live.get("linkedPR" if live_forge == "github" else "linkedGitLabMR"))
                    if goal_with_backfill((live.get("comment") or "").strip(), wid, current,
                                          (live.get("branch") or "").removeprefix("refs/heads/"), link,
                                          live_forge, live_project)[1]:
                        current.pop(wid, None)
                if json.dumps(current, sort_keys=True) != before:
                    backfills_write_unlocked(current)
                    backfill_read = backfill_revision()

    # One open-MR/PR listing per project resolves most branches without per-branch lookups.
    open_by_branch, closes, milestones = {}, {}, {}
    for forge, proj in {(r["forge"], r["project"]) for r in rows}:
        if forge == "github":
            for p in gh_open_prs(proj):
                open_by_branch[(proj, p["headRefName"])] = p["number"]
                for i in p.get("closingIssuesReferences") or []:
                    closes.setdefault((proj, i["number"]), p["number"])
        else:
            for m in gl_all(f"projects/{pid(proj)}/merge_requests?state=opened"):
                open_by_branch[(proj, m["source_branch"])] = m["iid"]
        try:
            milestones[proj] = release_milestone(proj, forge) if "milestone" in lifecycle(proj)[1] else None
        except Exception:
            milestones[proj] = None

    def closing(r, issue):
        """The open MR/PR that closes issue N ("Merge MR for #N")."""
        if r["forge"] == "github":
            return closes.get((r["project"], issue))
        try:
            hits = [m for m in gl(f"projects/{pid(r['project'])}/issues/{issue}/closed_by") if m["state"] == "opened"]
        except RuntimeError:  # no such issue (or not visible): fall back to the branch
            return None
        return hits[0]["iid"] if hits else None

    def by_branch(r, branch):
        """Newest MR/PR of any state whose source branch is this one."""
        if r["forge"] == "github":
            hits = gh("pr", "list", "-R", r["project"], "--head", branch, "--state", "all", "--limit", "1", "--json", "number")
            return hits[0]["number"] if hits else None
        q = urllib.parse.urlencode({"source_branch": branch, "state": "all", "order_by": "created_at"})
        hits = gl(f"projects/{pid(r['project'])}/merge_requests?{q}")
        return hits[0]["iid"] if hits else None

    def find_mr(r):
        upstream = r["branch"]
        if os.path.isdir(r["path"]):
            p = subprocess.run(["git", "-C", r["path"], "config", f"branch.{r['branch']}.merge"],
                               capture_output=True, text=True)
            upstream = p.stdout.strip().removeprefix("refs/heads/") or upstream
        gitlab = r["forge"] == "gitlab"
        named = re.fullmatch(r"mr-(\d+)(?:-.*)?", r["branch"])  # review checkouts: mr-5191 (adopt names PR ones mr-N too)
        iid = (r["linked_mr"] or open_by_branch.get((r["project"], upstream))
               or open_by_branch.get((r["project"], r["branch"])) or (named and int(named[1])))
        if not iid and (m := re.search(r"!(\d+)" if gitlab else r"^(?:Review|Merge) #(\d+)", r["comment"])):  # old status line: "Thread !129 — dispatched …"
            iid = int(m[1])
        if not iid and (m := ISSUE_GOAL_RE.match(r["goal"])):  # a sent plan: the MR/PR that closes its issue
            iid = closing(r, int(m[1]))
        if not iid:
            iid = by_branch(r, upstream)
        if not iid:
            return None
        mr = mr_details(r["project"], iid, r["forge"])
        if mr.get("_default_branch") and mr.get("target_branch") != mr["_default_branch"]:
            mr["_base_mr"] = open_by_branch.get((r["project"], mr["target_branch"]))  # stacked: the PR its base comes from
        return mr

    face = lambda u: {"name": u.get("login") or u["username"], "avatar": u.get("avatar_url")}
    roster = lifestage.blocks()  # blocking.py's last scan: the MRs someone is waiting on me for

    prune_candidates = []

    def one(r):
        try:
            mr = find_mr(r)
            remembered = backfills.get(r["id"])
            if remembered and "resolved_mr" in remembered and remembered["resolved_mr"] != (mr or {}).get("iid"):
                r["goal"] = "" if STATUS_LINE_RE.search(r["comment"]) else r["comment"]
                if save:
                    prune_candidates.append((r["id"], remembered, (mr or {}).get("iid")))
            r["mr"] = mr and {k: mr.get(k) for k in ("iid", "web_url", "title", "state", "draft", "updated_at")} | {
                "ref": ref(r["forge"], mr["iid"]), "forge": r["forge"],
                "author": mr["author"].get("login") or mr["author"]["username"], "role": "author" if mr["author"]["username"] == ME else "review",
                # Basement card byline: forge avatars, username on hover
                "by": face(mr["author"]), "assignees": [face(a) for a in mr.get("assignees") or []],
                # hex signals (dashboard): as of this scan
                "pipeline": (mr.get("head_pipeline") or {}).get("status"), "conflicts": bool(mr.get("has_conflicts")),
                "threads": len(unresolved(mr)), "approvals": len(mr.get("_approved_by", [])),
                "threads_fingerprint": hashlib.sha1(json.dumps(sorted(thread_line(d) for d in unresolved(mr))).encode()).hexdigest()[:12],
                "approvals_required": mr.get("_approvals_required", 0), "mergeable": mr.get("detailed_merge_status") == "mergeable", "rebase": mr.get("detailed_merge_status") == "need_rebase",
                # hover card (dashboard)
                "source_branch": mr.get("source_branch"), "target_branch": mr.get("target_branch"), "labels": mr.get("labels", []),
                "description": (mr.get("description") or "")[:2000],  # Basement ⌘J matches on it; ponytail: first 2000 chars
                "milestone": (mr.get("milestone") or {}).get("title"), "approved_by": mr.get("_approved_by", []),
                "pipeline_url": (mr.get("head_pipeline") or {}).get("web_url"),
                "sha": mr.get("sha"), "reviewed_sha": mr.get("_reviewed_sha"),  # the review prompt's {sha} and {last_review_sha}
                "todo_threads": [thread_line(d) for d in unresolved(mr) if awaiting_me(d)][:40],
                "failed_jobs": mr.get("_failed_jobs", [])[:40],  # the Resolve threads / Fix CI prompts' {threads} / {failed_jobs}
                "threads_total": sum(1 for d in mr.get("_discussions", []) if d["notes"] and d["notes"][0].get("resolvable")),
                "verify": verify_status(r["project"], mr["iid"], mr.get("sha"))}
            r["branch_gone"] = not mr and upstream_gone(r)
            r["next"] = evaluate(r, mr, milestones.get(r["project"]))
            r["_base_next"] = r["next"].copy()  # agent overlay may append failover instructions to the visible action
            life = lifestage.place(r)  # before the overlay: who the step itself is waiting on
            agent_overlay(r, terminals)
        except Exception as e:
            r["mr"], r["next"] = None, {"step": "error", "owner": "me", "action": f"greenlight couldn't evaluate: {e}"}
            life = lifestage.place(r)
        r["life"] = lifestage.now(life, r["agent"]["state"], "logged out" in r["next"].get("action", ""), lifestage.block_of(r, roster),
                                   r["agent"].get("asking"))
        return r

    prev_file = STATE / "last.json"
    try:
        prev = json.loads(prev_file.read_text()) if prev_file.exists() else {}
    except (OSError, ValueError):
        prev = {}
    prev_rows = {r["id"]: r for r in prev.get("rows", []) if isinstance(r, dict) and "id" in r}
    for r in rows:
        p = prev_rows.get(r["id"])
        if p and p.get("goal") == r["goal"] and p.get("comment") == r["comment"] and p.get("_revisit"):
            r["_revisit"] = p["_revisit"]

    with ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(one, rows))
    rows.sort(key=lambda r: (OWNERS.index(r["next"]["owner"]), r["project"], r["name"]))
    import ws_state
    ws_state.keyed(rows, migrate=save)  # previews resolve legacy identities without writing owner pointers

    STATE.mkdir(exist_ok=True)
    for r in rows:
        p = prev_rows.get(r["id"])
        if p and (p["next"]["step"], p["next"]["owner"]) != (r["next"]["step"], r["next"]["owner"]):
            r["changed"] = f"{p['next']['step']}/{p['next']['owner']} → {r['next']['step']}/{r['next']['owner']}"
    gone = [p["name"] for i, p in prev_rows.items() if i not in {r["id"] for r in rows}]
    scan_id = uuid.uuid4().hex
    if save:
        for r in rows:
            r["_scan_id"] = scan_id
    result = {"scan_id": scan_id, "scan_started_ns": scan_started_ns, "scan_clock_id": clock_id,
              "started": started, "ts": int(time.time()), "prev_ts": prev.get("ts"), "skipped": skipped, "gone": gone, "rows": rows}
    if save:  # an actionable precheck mustn't overwrite the snapshot the run's report diffs against
        try:
            accepted = keep(result, expected_backfill_revision=backfill_read)
        except BackfillChanged:
            if _backfill_retries >= 2:
                raise OSError("goal backfill changed repeatedly during scan")
            return scan(save=save, _backfill_retries=_backfill_retries + 1)
        if not accepted:
            return json.loads((STATE / "last.json").read_text())
        result = json.loads((STATE / "last.json").read_text())
        if prune_candidates:
            with backfill_lock(), ws_state.lock():
                accepted = {r["id"]: (r.get("mr") or {}).get("iid") for r in json.loads((STATE / "last.json").read_text())["rows"]}
                current = backfills_load()
                before = json.dumps(current, sort_keys=True)
                for wid, remembered, iid in prune_candidates:
                    if wid in accepted and accepted[wid] == iid and current.get(wid) == remembered:
                        current.pop(wid, None)
                if json.dumps(current, sort_keys=True) != before:
                    backfills_write_unlocked(current)
        for r in rows:
            if r.get("_goal_backfill") and (r.get("mr") or {}).get("ref"):
                decisions.backfilled_goal(r["id"], r["mr"]["ref"], r["goal"], r.get("project"), r.get("forge"))
    return result


def scan_clock_id():
    """Identify a boot so monotonic scan starts are comparable across processes, not across reboots."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        try:
            return subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                                  timeout=2, check=True).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"


def keep(result, expected_backfill_revision=ANY_BACKFILL_REVISION):
    STATE.mkdir(exist_ok=True)
    import ws_state
    with backfill_lock(), ws_state.lock():
        if expected_backfill_revision is not ANY_BACKFILL_REVISION and backfill_revision() != expected_backfill_revision:
            raise BackfillChanged()
        saved = STATE / "last.json"
        try:
            previous = json.loads(saved.read_text())
            previous_started = previous.get("scan_started_ns", 0)
        except (OSError, ValueError):
            previous = {}
            previous_started = 0
        same_clock = (result.get("scan_clock_id") == previous.get("scan_clock_id")
                      and result.get("scan_clock_id") != "unknown")
        if same_clock and result.get("scan_started_ns", 0) < previous_started:
            patches = previous.get("dashboard_merges") or []
            baseline = previous.get("accepted_scan_started_ns", previous.get("dashboard_base_started_ns", previous_started))
            if not patches or result.get("scan_started_ns", 0) < baseline:
                return False
            rows = [row.copy() for row in result["rows"]]
            for patch in patches:
                for i, row in enumerate(rows):
                    mr = row.get("mr") or {}
                    same_request = (row.get("forge"), row.get("project"), mr.get("iid")) == \
                                   (patch.get("forge"), patch.get("project"), patch["iid"]) and \
                                   (patch.get("request") or mr.get("sha") == patch["sha"])
                    same_worktree = patch.get("request") or row.get("path") == patch.get("path") and row.get("id") == patch.get("id")
                    if same_request and same_worktree and mr.get("state") in ("opened", "open"):
                        merged = row.copy()
                        merged["mr"] = mr | {"state": "merged"}
                        if "next" in patch["row"]:
                            merged["next"] = patch["row"]["next"]
                        merged["life"] = lifestage.place(merged) if patch.get("request") else patch["row"].get("life", merged.get("life"))
                        rows[i] = merged
            result = result | {"rows": rows, "scan_started_ns": previous_started, "dashboard_merges": patches,
                               "dashboard_base_started_ns": previous.get("dashboard_base_started_ns", previous_started),
                               "accepted_scan_started_ns": result.get("scan_started_ns", 0)}
        result = result | {"rows": [row.copy() for row in result["rows"]]}
        scan_id = result.setdefault("scan_id", uuid.uuid4().hex)
        for row in result["rows"]:
            row["_scan_id"] = scan_id
        tmp = STATE / f"last.{uuid.uuid4().hex}.json"
        marker = STATE / f"scan_id.{uuid.uuid4().hex}"
        try:
            tmp.write_text(json.dumps(result))
            tmp.replace(STATE / "last.json")
            marker.write_text(f"{scan_id} { (STATE / 'last.json').stat().st_ino }")
            marker.replace(STATE / "scan_id")
        finally:
            tmp.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        close_stale(result["rows"])
    return True


def outbox(ev):
    STATE.mkdir(exist_ok=True)
    path = STATE / "outbox.jsonl"
    with path.open("ab+") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.write(b"\n")  # keep a killed append from swallowing the next event
        fh.write((json.dumps({"ts": int(time.time())} | ev) + "\n").encode())


def pinged(update=None):
    """Open questions keyed by workspace name or id: {key: ts}. ~/.greenlight/pinged.json"""
    f = STATE / "pinged.json"
    if update is None:
        try:
            return json.loads(f.read_text()) if f.exists() else {}
        except (OSError, ValueError):
            return {}
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(update))
    os.replace(tmp, f)


def ping(text, key=None, ws=(), kind=None, proactive=False, ask=False):
    """Serialize the decision, audit and outbox path across concurrent scans."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "ping.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        diag.event("ping", ping_kind=kind, ws_n=len(ws or ()), proactive=proactive, ask=ask)
        return _ping(text, key, ws, kind, proactive, ask)


def _ping(text, key=None, ws=(), kind=None, proactive=False, ask=False):
    """ws: the workspaces (names) it's about; a workspace key counts as one. The app lifts their hexes until it's answered.
    kind: the sort of question, for the decision record (decisions.py); guessed from the key or the lifecycle step when not given.
    A kind autonomy.py knows gets its proposal (tier 1: answered for you and returned as {"auto": option, "id": audit id};
    tier 2: preselected in the app). proactive: only when autonomy.gate() lets it interrupt; held: returns {"held": gate}.
    ask: always ask, no proposal (an undo asking again)."""
    if not ask and key:
        # Recover a committed auto answer whose outbox append failed, without making another decision.
        records = {r["id"]: r for r in decisions.load()}
        for a in reversed(autonomy.audit_log()):
            r = records.get(a.get("record"))
            if (a.get("act") == "auto" and not a.get("undone") and not a.get("cancelled") and a.get("key") == key and a.get("text") == text
                    and a.get("ts", 0) > time.time() - FLAP and r and r.get("answer_via") == "auto"):
                if not any(e.get("auto", {}).get("id") == a["id"] for e in outbox_events()):
                    outbox({"text": text, "key": key, "ws": a.get("ws") or [],
                            "auto": {"id": a["id"], "choice": a["choice"], "prob": a["prob"]}})
                    print(f"auto: {a['choice']}  (recovered; undo: autonomy.py undo {a['id']}). Act on it now as the user's answer.")
                    return {"auto": a["choice"], "id": a["id"]}
                break
    if not ask and key and key.startswith("@") and key in recent_keys():  # puck blocks flap in and out of its list (an undo asks regardless)
        print(f"skipped: {key} was pinged in the last {FLAP // 3600}h")
        return
    # A scan may ask the same workspace question again; don't repeatedly answer it for the user.
    if not ask and key and any(e.get("key") == key and e.get("text") == text and e.get("auto") and e.get("ts", 0) > time.time() - FLAP
                               for e in outbox_events()):
        print(f"skipped: {key} was answered for you in the last {FLAP // 3600}h")
        return
    names = scanned_names()
    raw_ws = list(ws)
    mine = [names.get(key, key)] if key and not key.startswith("@") else []
    display_ws = list(dict.fromkeys(mine + [names.get(w, w) for w in raw_ws]))
    decision_ws = list(dict.fromkeys(([key] if key and not key.startswith("@") else []) + raw_ws))
    slug = decisions.slug(kind) if kind else None
    if proactive and not slug:
        kind = slug = "proactive"
    if proactive or slug in autonomy.PROACTIVE:
        try:
            g = autonomy.gate(slug or "proactive", text)
        except Exception as e:  # a broken gate never swallows a question
            print(f"autonomy: {e}", file=sys.stderr)
            g = {"ok": True}
        if not g["ok"]:
            autonomy.audit({"act": "held", "kind": g["kind"], "score": g["score"], "threshold": g["threshold"], "text": text, "key": key})
            print(f"held: {g['kind']} scores {g['score']} < {g['threshold']} (acted on {g['acted']}, dismissed {g['dismissed']}, "
                  f"{g['waiting']} waiting); nothing sent")
            return {"held": g}
    prop = None if ask or not slug else autonomy.quiet_propose(slug, text)
    ev = {"text": text} | ({"key": key} if key else {}) | ({"ws": display_ws} if display_ws else {})
    if prop and prop["tier"] == 1:
        rid = decisions.asked(text, "ping", key, decision_ws, kind, proposal=prop)
        try:
            a = autonomy.act(rid, prop, text, key, display_ws)
        except Exception as e:  # couldn't log it: ask instead
            print(f"autonomy: {e}", file=sys.stderr)
        else:
            outbox({"record": rid, "auto": {"id": a["id"], "choice": prop["choice"], "prob": prop["prob"], "count": prop["count"], "agree": prop["agree"]}} | ev)
            print(f"auto: {prop['choice']}  (P {prop['prob']}, {prop['agree']} of {prop['count']} past {slug} answers; "
                  f"undo: autonomy.py undo {a['id']}). Act on it now as the user's answer.")
            return {"auto": prop["choice"], "id": a["id"]}
    if prop and prop["tier"] == 2:
        ev["suggest"] = {f: prop[f] for f in ("choice", "index", "prob", "count", "agree")}
    if ask:
        # Undo must be able to link the user's eventual answer before the app sees this re-ask.
        rid = decisions.asked(text, "ping", key, decision_ws, kind)
        outbox({"record": rid} | ev)
        if key:
            pinged(pinged() | {key: int(time.time())})
        return {"asked": rid}
    rid = decisions.asked(text, "ping", key, decision_ws, kind, proposal=prop)
    if key and not key.startswith("@") and any(e.get("record") == rid and e.get("key") == key and e.get("text") == text and
                                                   e.get("ts", 0) > time.time() - FLAP for e in outbox_events()):
        pinged(pinged() | {key: int(time.time())})  # recover a crash after this exact decision was already delivered
        return {"asked": rid}
    outbox({"record": rid} | ev)
    if key:
        pinged(pinged() | {key: int(time.time())})
    return {"asked": rid} if rid else None


FLAP = 24 * 3600


def outbox_events():
    f = STATE / "outbox.jsonl"
    events = []
    for line in f.read_text(errors="replace").splitlines() if f.exists() else []:
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    return events


def recent_keys():
    """Keys pinged within FLAP, read from the outbox itself."""
    cut = time.time() - FLAP
    return {e["key"] for e in outbox_events()
            if "text" in e and e.get("key") and e["ts"] > cut}


def scanned_names():
    """{name or id: name} for every workspace in the last scan (~/.greenlight/last.json)."""
    f = STATE / "last.json"
    try:
        rows = json.loads(f.read_text()).get("rows", []) if f.exists() else []
    except (OSError, ValueError):
        rows = []
    return {r["id"]: r["name"] for r in rows} | {r["name"]: r["name"] for r in rows}


def ping_cli(args):
    """greenlight.py ping [--key K] [--kind KIND] [--ws W]... [--proactive] <text>: --ws takes a workspace name or id; an unknown one sends nothing."""
    key, kind, ws, proactive = None, None, [], False
    while len(args) > 1 and args[0] in ("--key", "--kind", "--ws", "--proactive"):
        if args[0] == "--proactive":
            proactive, args = True, args[1:]
            continue
        flag, val, args = args[0], args[1], args[2:]
        if flag == "--key":
            key = val
        elif flag == "--kind":
            kind = val
        else:
            ws.append(val)
    if not re.search(r"\nOptions: *\S.*\|", " ".join(args)):  # no buttons, only "Reply", in the app
        sys.exit("greenlight.py ping: end the text with a line `Options: first | second`; nothing sent")
    names = scanned_names()
    if unknown := [w for w in ws if w not in names]:
        sys.exit(f"greenlight.py ping: unknown workspace {', '.join(unknown)} (not in the last scan); nothing sent")
    ping(" ".join(args), key, ws, kind, proactive)


def resolve(key, why):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "ping.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        outbox({"resolve": key, "why": why})
        decisions.quiet("resolve", key, why)
        pinged({k: t for k, t in pinged().items() if k != key})


def close_stale(rows):
    """Questions about a workspace close once it's gone or no longer waiting on me."""
    open_pings = pinged()
    if not open_pings:
        return
    by_key = {r["name"]: r for r in rows} | {r["id"]: r for r in rows}
    ws_prefixes = ("@stuck:", "@drift:", "@rereview:")
    undo_ws, stuck_ws, stuck_ids = {}, {}, {}
    if any(k.startswith(("@undo:", *ws_prefixes)) for k in open_pings):
        for e in outbox_events():
            k = str(e.get("key", ""))
            if k.startswith("@undo:") and e.get("ws"):
                undo_ws[k] = e["ws"]
            elif k.startswith(ws_prefixes) and e.get("ws"):
                stuck_ws[k] = e["ws"]
    if any(k.startswith(ws_prefixes) for k in open_pings):
        stuck_ids = {r.get("key"): [t.get("worktree_id") for t in [r] + (r.get("workspaces") or []) if t.get("worktree_id")]
                     for r in decisions.load() if str(r.get("key", "")).startswith(ws_prefixes)}
    for key in open_pings:
        if key.startswith(ws_prefixes):
            names = stuck_ids.get(key) or stuck_ws.get(key, [])
            if names and not any(name in by_key for name in names):
                resolve(key, "workspace removed")
            continue
        if key.startswith("@undo:"):
            names = undo_ws.get(key, [])
            if names and not any((r := by_key.get(name)) and r["next"]["owner"] == "me" for name in names):
                resolve(key, "workspaces no longer waiting on you")
            continue
        if key.startswith("@"):  # other grouped questions are closed by hand with `greenlight.py resolve`
            continue
        r = by_key.get(key)
        if r is None:
            resolve(key, "workspace removed")
        elif r["next"]["owner"] != "me":
            resolve(key, f"now {r['next']['owner']}: {r['next']['action'][:120]}")


def report(res):
    rows = [r for r in res["rows"] if not r["next"].get("hidden")]
    counts = {o: sum(r["next"]["owner"] == o for r in rows) for o in OWNERS}
    label = {"me": "Needs you", "greenlight": "Greenlight will do", "agent": "Idle — needs an agent",
             "working": "Agent working", "ci": "Waiting on CI", "reviewers": "Waiting on reviewers",
             "author": "Waiting on author", "dependency": "Waiting on another MR/PR", "parked": "Parked (revisit later)", "done": "Done — cleanup"}
    e = html.escape
    bar = "".join(f'<span class="seg o-{o}" style="flex:{n}" title="{label[o]}: {n}"></span>' for o, n in counts.items() if n)
    legend = "".join(f'<li><i class="o-{o}"></i>{label[o]} <b>{n}</b></li>' for o, n in counts.items() if n)
    changed = [r for r in rows if r.get("changed")]
    sections = []
    if changed or res["gone"]:
        items = "".join(f"<li><b>{e(r['name'])}</b> {e(r['changed'])}</li>" for r in changed)
        items += "".join(f"<li><b>{e(n)}</b> removed</li>" for n in res["gone"])
        sections.append(f"<section><h2>Since last run</h2><ul class=changes>{items}</ul></section>")
    def vtag(r):  # the verify step's receipt, linked to its evidence
        v = (r.get("mr") or {}).get("verify")
        if not v:
            return ""
        st = "stale" if v.get("stale") and v.get("verdict") == "pass" else v.get("verdict") or ""
        ev = str(v.get("evidence") or "")  # a web page or a file on this host links; anything else (javascript:, a relative path) is named
        href = ev if re.match(r"https?://", ev) else "file://" + ev if ev.startswith("/") else ""
        ev = f' · <a href="{e(href)}">evidence</a>' if href else f" · evidence {e(ev)}" if ev else ""
        return f"<div class=sub>verify: {e(st)}{ev}</div>"
    for o in OWNERS:
        group = [r for r in rows if r["next"]["owner"] == o]
        if not group:
            continue
        trs = "".join(
            "<tr><td>{n}<div class=sub>{p}</div></td><td>{mr}</td><td><code>{s}</code></td><td>{a}{g}{v}</td><td>{ag}</td></tr>".format(
                n=e(r["name"]), p=e(r["project"].rsplit("/", 1)[-1] + " · " + r["branch"]),
                mr=(f'<a href="{e(r["mr"]["web_url"])}">{e(r["mr"].get("ref") or "!" + str(r["mr"]["iid"]))}</a><div class=sub>{e(r["mr"]["role"])}</div>' if r.get("mr") else "—"),
                s=e(r["next"]["step"]), a=e(r["next"]["action"]),
                g=f'<div class=sub>goal: {e(r["goal"][:140])}</div>' if r["goal"] else '<div class="sub warn">no goal</div>', v=vtag(r),
                ag=f'{e(r["agent"]["state"])}<div class=sub>{"—" if r["agent"]["idle_h"] is None else f"{r['agent']['idle_h']}h idle"}</div>')
            for r in group)
        sections.append(f'<section><h2><i class="o-{o}"></i>{label[o]} <span class=n>{len(group)}</span></h2>'
                        f"<table><tr><th>Workspace</th><th>MR</th><th>Step</th><th>Next</th><th>Agent</th></tr>{trs}</table></section>")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(res["ts"]))
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Greenlight Report</title><style>
:root{{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--card:#fff;
--me:#dc2626;--greenlight:#7c3aed;--agent:#ea580c;--working:#16a34a;--ci:#0891b2;--reviewers:#2563eb;--author:#64748b;--dependency:#94a3b8;--parked:#d6d3d1;--done:#a8a29e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0c0a09;--fg:#f5f5f4;--mut:#a8a29e;--line:#292524;--card:#1c1917}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1100px;margin:auto}}h1{{margin:0 0 4px}}.meta{{color:var(--mut);margin-bottom:16px}}
.bar{{display:flex;height:18px;border-radius:6px;overflow:hidden;gap:2px}}.seg{{min-width:6px}}
ul.legend{{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:6px 16px;margin:10px 0 24px}}
i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}}
{"".join(f".o-{o}{{background:var(--{o})}}" for o in OWNERS)}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:16px;overflow-x:auto}}
h2{{font-size:16px;margin:4px 0 10px}}.n{{color:var(--mut);font-weight:400}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}}
th{{color:var(--mut);font-weight:500;border-top:0}}.sub{{color:var(--mut);font-size:12px}}.warn{{color:var(--me)}}
a{{color:var(--reviewers)}}code{{font-size:12px}}ul.changes{{margin:0;padding-left:18px}}
</style></head><body><main><h1>Greenlight</h1>
<div class=meta>{when} · {len(rows)} workspaces tracked · {res['skipped']} skipped (main checkouts / detached / not on GitLab or GitHub)</div>
<div class=bar>{bar}</div><ul class=legend>{legend}</ul>{''.join(sections)}</main></body></html>"""


def history_file(name):
    # one markdown file per workspace, named after the workspace (me/x -> me__x)
    return STATE / "history" / (name.replace("/", "__") + ".md")


def note(name, text):
    f = history_file(name)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {text}\n")


SCAN_TOOLS = {"glab": "GitLab", "gh": "GitHub", "wt": "Worktrunk", "orca": "Orca", "orca-ide": "Orca",
              "tmux": "tmux", "cmux": "cmux", "herdr": "herdr", "t3": "t3"}
SCAN_WHY = {"auth": "login expired", "rate_limit": "rate limited", "timeout": "timed out", "network": "unreachable",
            "transient": "server error", "permission": "permission denied"}
SCAN_FIX_AUTH = {"glab": "run glab auth login --hostname gitlab.com, then ↻", "gh": "run gh auth login, then ↻"}
SCAN_FIX_CLASS = {"rate_limit": "wait a few minutes, then ↻", "timeout": "try ↻ again",
                  "transient": "try ↻ again", "network": "check your connection, then ↻"}


def _scan_error_newer(f, started_ns):
    """True when scan_error.json already holds a failure from a scan started after `started_ns`."""
    try:
        prev = json.loads(f.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(prev, dict):
        return False
    pns = prev.get("started_ns")
    if isinstance(pns, int) and not isinstance(pns, bool):
        return pns > started_ns
    pst = prev.get("started")
    return isinstance(pst, (int, float)) and not isinstance(pst, bool) and pst > started_ns / 1_000_000_000


def scan_failed(e, started_ns=None):
    """Why the last scan died, for the dashboard footer (scan_error.json); a newer scan that finishes removes it.
    Only the tool, the error's class and the fix go in it: the message has argv (project paths)."""
    f = STATE / "scan_error.json"
    st_ns = started_ns if started_ns is not None else time.time_ns()
    if e is None:
        with contextlib.suppress(OSError):
            STATE.mkdir(parents=True, exist_ok=True)
            with (STATE / "scan_error.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if started_ns is not None and _scan_error_newer(f, st_ns):
                    return
                f.unlink(missing_ok=True)
        return
    if isinstance(e, subprocess.TimeoutExpired):
        cmd0 = e.cmd[0] if isinstance(e.cmd, (list, tuple)) and e.cmd else str(e.cmd or "").split(" ", 1)[0]
        cand = str(cmd0).rsplit("/", 1)[-1]
        tool, cls = cand if cand in SCAN_TOOLS else "", "timeout"
    elif getattr(e, "_gl_tool", None) is not None:
        cand = str(getattr(e, "_gl_tool", "") or "").rsplit("/", 1)[-1]
        tool = cand if cand in SCAN_TOOLS else ""
        cls = getattr(e, "_gl_class", None) or "other"
    else:
        text = str(e)
        head, sep, err = text.partition(": ")
        cand = head.split(" ", 1)[0].rsplit("/", 1)[-1]
        tool = cand if cand in SCAN_TOOLS else ""
        # classify stderr only when the message carries argv (same rule as _run): a repo named "token-*" isn't auth
        cls = diag.classify(err if tool and sep else text)
    why = SCAN_WHY.get(cls, "failed")
    if cls == "auth":
        fix = SCAN_FIX_AUTH.get(tool, "")
    elif tool in ("orca", "orca-ide") and cls in ("network", "timeout", "not_found", "other"):
        fix = "check Orca is running, then ↻"
    else:
        fix = SCAN_FIX_CLASS.get(cls, "")
    out = {"ts": int(time.time()), "started": st_ns // 1_000_000_000, "started_ns": st_ns, "class": cls, "tool": tool,
           "msg": f"{SCAN_TOOLS[tool]} {why}" if tool else why if why != "failed" else "crashed",
           "fix": fix}
    with contextlib.suppress(OSError):  # the traceback still reaches stderr
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "scan_error.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _scan_error_newer(f, st_ns):
                return out
            tmp = f.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(out))
            os.replace(tmp, f)
    return out


def scan_cause(e, info):
    """One line for a precheck whose scan died: scan_failed()'s summary and fix, then the error's last line
    (without run()'s argv prefix: it has project paths, and it pushed the summary out of auto.py's 200 chars)."""
    text = str(e).partition(": ")[2] if getattr(e, "_gl_tool", None) is not None else str(e)
    tail = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), type(e).__name__)
    head = (info or {}).get("msg") or "crashed"
    fix = (info or {}).get("fix")
    return f"scan failed ({head}{f'; {fix}' if fix else ''}): {tail[:200]}"


def scan_cli(cmd):
    """main()'s scan, timed in the trail, its failure in scan_error.json. A precheck whose scan dies exits 2 with
    its cause on stdout: 1 is "nothing to do", and auto.py records a 2 as a failed run, not a skipped one."""
    info = None
    try:
        with diag.span("scan", op=cmd) as d:
            scan_start_ns = time.time_ns()
            try:
                res = scan(save=cmd != "precheck")
            except Exception as e:
                info = scan_failed(e, started_ns=scan_start_ns)
                raise
            scan_failed(None, started_ns=scan_start_ns)
            d["rows"] = len(res["rows"])
            for o in OWNERS:  # how many rows each owner holds: the shape of the queue, no names
                d[f"n_{o}"] = sum(r["next"]["owner"] == o for r in res["rows"]) or None
    except Exception as e:
        if cmd != "precheck":
            raise
        import traceback
        traceback.print_exc()  # the run's log keeps the whole story
        print(scan_cause(e, info), flush=True)
        sys.exit(2)
    return res


def precheck_cli(res):
    """Exit 0 when a run has work: an actionable row, an MR to adopt, or a workspace cleanup.py would delete.
    One command for the automation's precheck: a shell `a || b` would turn a's exit 2 into b's verdict."""
    n = sum(r["next"]["owner"] in ("greenlight", "agent") for r in res["rows"])
    try:  # MRs with no workspace yet: adopt.py creates them, so a run should follow
        import adopt
        n += len(adopt.plan(res["rows"], res.get("ts"))["create"])
    except Exception as e:
        print(f"adopt plan failed: {e}", file=sys.stderr)
    if not n:  # no run follows its scan, so this is the freshest snapshot the dashboard will get
        keep(res)
    print(f"{n} actionable workspace(s) or adoption(s)")
    if not n:
        try:  # finished workspaces: the run's cleanup.py apply deletes them
            import cleanup
            gone = sum(r["verdict"] == "candidate" for r in cleanup.scan())
            print(f"{gone} workspace(s) to delete")
            n += gone
        except Exception as e:
            print(f"cleanup check failed: {e}", file=sys.stderr)
    sys.exit(0 if n else 1)


def repos_cli(argv):
    """The native engine's repo registry (repos.json): list it, or add/rm repos by any path inside them. Exit code."""
    import ade_basement
    op, paths = (argv[0] if argv else "list"), argv[1:]
    if op == "list":
        print(json.dumps([{"path": r, "exists": Path(r).is_dir()} for r in ade_basement.saved()], indent=1))
        return 0
    if op not in ("add", "rm") or not paths:
        print("usage: greenlight.py repos [list | add <path>... | rm <path>...]", file=sys.stderr)
        return 2
    rc = 0
    for p in paths:
        try:
            if op == "add":
                print(f"added {ade_basement.register(p)}")
            else:
                repo = ade_basement.main_checkout(p) or str(Path(p).expanduser().resolve())
                if repo not in ade_basement.saved():
                    raise RuntimeError(f"not registered: {repo}")
                ade_basement.forget(repo)
                print(f"removed {repo}")
        except RuntimeError as e:
            print(e, file=sys.stderr)
            rc = 1
    return rc


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "note":  # greenlight.py note <workspace> "<what happened>"
        note(sys.argv[2], " ".join(sys.argv[3:]))
        return
    if cmd == "ping":  # notification + bell-inbox question on the Mac menubar app (it tails outbox.jsonl)
        ping_cli(sys.argv[2:])
        return
    if cmd == "resolve":
        resolve(sys.argv[2], " ".join(sys.argv[3:]) or "no longer needed")
        return
    if cmd == "ade":  # greenlight.py ade worktree set --worktree id:<id> --comment "<goal>"
        if why := create_refused(sys.argv[2:]):
            print(why, file=sys.stderr)
            sys.exit(3)
        os.environ.pop("GREENLIGHT_REQUEST", None)  # not for the ADE: a tmux server started here would hand it to every pane
        try:
            out = orca(*[a for a in without_change(sys.argv[2:]) if a != "--json"])
        except PlacementRefused as e:
            print(e, file=sys.stderr)
            sys.exit(3)
        print(json.dumps(out, indent=1))
        if isinstance(out, dict) and out.get("error"):  # done in part (native: made, but its agent didn't start)
            sys.exit(1)
        return
    if cmd == "import":  # greenlight.py import [detect|preview|apply] [--from S]... [--path DIR]...: importers.py
        import importers
        try:
            print(json.dumps(importers.run(sys.argv[2:]), indent=1))
        except (SystemExit, RuntimeError) as e:
            print(e, file=sys.stderr)
            sys.exit(2)
        return
    if cmd == "repos":  # greenlight.py repos [list | add <path>... | rm <path>...]: the native engine's repos.json
        sys.exit(repos_cli(sys.argv[2:]))
    if cmd == "signoff":  # greenlight.py signoff <project> <iid|@branch> <stage>: the user signed off a custom stage
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "signoffs.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            so = signoffs()
            k = f"{sys.argv[2]}{sys.argv[3]}" if sys.argv[3].startswith("@") else f"{sys.argv[2]}#{sys.argv[3]}"
            so[k] = sorted(set(so.get(k, [])) | {sys.argv[4]})
            tmp = (STATE / "signoffs.json").with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(so, indent=1))
            os.replace(tmp, STATE / "signoffs.json")
        return
    if cmd == "verify":  # greenlight.py verify <project> <iid> <pass|fail|uncertain|reset> --sha S [--summary T] ...
        verify_cli(sys.argv[2:])
        return
    if cmd == "history":  # greenlight.py history <workspace>
        f = history_file(sys.argv[2])
        print(f.read_text() if f.exists() else "(no history)")
        return
    res = scan_cli(cmd)
    if cmd == "precheck":
        precheck_cli(res)
    if cmd == "report":
        out = STATE / "report.html"
        out.write_text(report(res))
        print(out)
        return
    if "--json" in sys.argv:
        print(json.dumps(res, indent=1))
        return
    for r in res["rows"]:
        mr = r["mr"].get("ref") or f"!{r['mr']['iid']}" if r.get("mr") else "-"
        print(f"{r['next']['owner']:10} {r['next']['step']:12} {mr:6} {r['name'][:40]:40} {r['next']['action'][:90]}")


if __name__ == "__main__":
    diag.install("greenlight")
    main()
