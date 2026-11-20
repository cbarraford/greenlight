#!/usr/bin/env python3
"""greenlight: work out the next lifecycle step, and who it's waiting on, for every Orca workspace.

    greenlight.py scan [--json]   facts + next step per workspace; snapshot saved to ~/.greenlight/last.json. Each row also
                                  carries `life`: its lifecycle stage and sub-stage, who holds it, and the suggested move (lifestage.py)
    greenlight.py report          scan, then write ~/.greenlight/report.html (needs-you first)
    greenlight.py precheck        Orca automation gate: exit 0 when something is actionable, else 1
    greenlight.py note <ws> <text>  append a dated line to ~/.greenlight/history/<ws>.md
    greenlight.py history <ws>      print that workspace's history
    greenlight.py ping [--key <ws>|@k] [--kind <kind>] [--ws <ws>]... <text>  question for the Greenlight menubar app
                                       (outbox.jsonl); --ws names a workspace it's about (its hex is lifted until answered);
                                       --kind names the sort of question for the decision record (decisions.py)
    greenlight.py resolve <key> <why>       close that question in the app
    greenlight.py ade <orca args>           an `orca` call routed to the ADE in use (GREENLIGHT_ADE=orca|cmux|herdr|t3), JSON out

Read-only: it never changes anything. The greenlight skill acts on `next.cmd` / `next.owner`.
Deleting finished workspaces is cleanup.py's job (next to this file); creating workspaces for MRs
that have none is adopt.py's.

Stdlib only. Needs `glab` (gitlab.com repos) and `gh` (github.com repos) authenticated. A GitHub PR is
normalised into the GitLab MR shape (gh_pr), so the lifecycle steps, report and dashboard treat both alike. ORCA_ENVIRONMENT=<name> targets a paired runtime
(handy from a laptop; git upstream lookups are skipped when worktree paths aren't local).

State and config live in $GREENLIGHT_HOME (default ~/.greenlight). config.json there overrides DEFAULTS below.
"""

import html
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions  # noqa: E402
import lifestage  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # the skill dir: lifecycles/, prompts/, examples/, memory/
STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
# Only what differs between users. config.json's keys replace these whole (no deep merge).
DEFAULTS = {
    "me": None,                       # your GitLab/GitHub username; None = ask glab (else gh) once, cached in me.json
    "review_bot": None,               # a bot the `review_bot` step assigns as assignee + reviewer; None skips the step
    "agents": ["claude", "codex"],    # dispatch order: on a usage limit greenlight moves to the next one (claude|codex|opencode|pi|gemini)
    "harnesses": {},                  # per agent: {"bypass": true} for full access, {"bin": path}; see harness.py
    "llm_harness": "claude",          # runs the dashboard's own LLM calls (design, suggestions)
    "merge_method": "merge",          # merge | squash | rebase
    "require_human_approval": True,   # False: a mergeable MR with no other human's approval merges anyway
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
DEAD_RE = re.compile(r"\b401\b|OAuth access token has been revoked|Please run /login|login expired|API key (?:has expired|is invalid|not valid|expired)", re.I)
LIMIT_RE = re.compile(r"usage limit|limit reached|5-hour limit|weekly limit|out of (?:extra )?usage"
                      r"|rate limit exceeded|exceeded your current quota|insufficient_quota|429 Too Many Requests"
                      r"|exhausted your daily quota|quota will reset after", re.I)  # last line: Gemini CLI's own words (a bare RESOURCE_EXHAUSTED is any gRPC error)
# Instructions I add to a goal. A word joined to its neighbour by - or _ ("security-hold") is part of a name, not an instruction.
KEEP_DRAFT_RE = re.compile(r"(?<![\w-])(stay|keep|remain)s? (?:a |as )?draft(?![\w-])", re.I)
HOLD_RE = re.compile(r"(?<![\w-])(?:hold|do ?n[o'’]t merge)(?![\w-])", re.I)
AUTOMATION_RE = re.compile(r"\(automation\)", re.I)  # free-form goal driven by an Orca automation: don't dispatch
REVISIT_RE = re.compile(r"revisit (\d{4}-\d\d-\d\d)", re.I)  # "(revisit 2026-11-06)" parks it until then
PARKED_RE = re.compile(r"\(parked\)", re.I)  # parked with no end date, and hidden from the dashboard and report
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
    for i in range(tries):
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
            if last or not TRANSIENT_RE.search(err):
                raise RuntimeError(f"{' '.join(cmd)}: {err[:300]}")
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
    """An Orca CLI call. Any other GREENLIGHT_ADE answers the same calls from another ADE, in Orca's shape (ade_<GREENLIGHT_ADE>.py)."""
    if os.environ.get("GREENLIGHT_ADE", "orca") != "orca":
        return importlib.import_module(f"ade_{os.environ['GREENLIGHT_ADE']}").call(*args)
    return orca_cli(*args, timeout=timeout)


def orca_cli(*args, timeout=120):
    """The Orca CLI itself, whatever GREENLIGHT_ADE is (dash.py trees asks every ADE)."""
    # Bare `orca` on Linux outside an Orca terminal is the GNOME screen reader.
    shim = Path.home() / ".config/orca/linux-orca-cli-shim/orca"  # ssh / cron shells have no orca-ide on PATH
    exe = os.environ.get("ORCA_CLI_COMMAND") or shutil.which("orca-ide") or (shim.exists() and str(shim)) or "orca"
    env = ["--environment", os.environ["ORCA_ENVIRONMENT"]] if os.environ.get("ORCA_ENVIRONMENT") else []
    out = json.loads(run([exe, *args, *env, "--json"], timeout=timeout))
    if not out.get("ok"):
        raise RuntimeError(f"orca {' '.join(args)}: {out.get('error')}")
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
           r"kill|stop|start|run|launch|dispatch|retry|re-?run|draft|publish|resolve|address|tag|release|save|record|"
           r"archive|clean|turn (on|off)|hold|park|rotate|revoke|pay|buy|order|schedule|book|undo|apply|land|split|"
           r"squash|rewrite|document|test|grant|give|invite|share|allow|deny|block|unblock|take over|pick up|handle|"
           r"spin up|try|continue|proceed|finish|resume|do|go ahead|keep|use|switch|replace|reset|clear|mark|ping|"
           r"notify|e-?mail|message|get|port|hook|wire|file|sort|triage|translate|retrigger|kick off|page|auto-?merge|"
           r"what about|how about|what if|why not|check off|tick off|list (?:it|us|our \w+|my \w+) on|let|put|renew|roll|nudge|"
           r"reassign|cherry-pick|force-push|re-review|check in|check out|generate|is it possible to|in(?:to)? (?:a|the|our|my|its|this) (?:[\w-]+ )?(?:docs?|readme|wiki|changelog|description|report|notes?)|"
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
    rf"(?!(?:[ \t]+\w+)?(?:\*\*|__)?[ \t]*:)(?!(?:[ \t]+(?!(?:{DET})\b)\w+){{0,2}}(?:\*\*|__)?[ \t]*[$€£])"  # "- Agent runs: $3", "- Test runs cost $3"
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


def without_change(args):
    """args minus --change <why>, which is greenlight's own and not the ADE's."""
    args = [x for x in args if not x.startswith("--change=")]
    if "--change" in args:
        k = args.index("--change")
        del args[k:k + 2]
    return args


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
    """~/.greenlight/lifecycles/<repo>.md, else the bundled lifecycles/<repo>.md, else default.md."""
    name = f"{project.rsplit('/', 1)[-1]}.md"
    return next((f for f in (STATE / "lifecycles" / name, ROOT / "lifecycles" / name) if f.exists()),
                ROOT / "lifecycles" / "default.md")


BUILTIN = {"mr_open": "An MR exists for the branch.", "ci_green": "The head pipeline succeeded (or there's no CI).",
           "no_conflicts": "No merge conflicts.", "review_bot": "The review bot is assignee and reviewer.",
           "milestone": "A release milestone is set.", "ready": "Not a draft.", "threads": "No unresolved threads.",
           "approved": "The required approvals are in.", "merge": "The forge reports it mergeable."}
STAGE_RE = re.compile(r"^\s*\d+\.\s+`(\w+)`[ \t]*(?:—|-)?[ \t]*(.*)$", re.M)
DONE_RE = re.compile(r"\s*Done:\s*`(signoff|label=[^`]+)`")
RUN_RE = re.compile(r"\s*Run:\s*`([^`]+)`")  # a signoff step's command: the card's button runs it on the host, then signs it off


def lifecycle_doc(project):
    """The lifecycle as data: {stages: [{key, req, done}], options, rules, intro, file, source}.
    A stage that isn't built in passes on `Done: `signoff`` (the user signs it off once per MR) or `Done: `label=x``."""
    f = lifecycle_file(project)
    head, _, rules = f.read_text().partition("\n## Rules")
    stages = []
    for key, rest in STAGE_RE.findall(head):
        m, run = DONE_RE.search(rest), RUN_RE.search(rest)
        stages.append({"key": key, "req": RUN_RE.sub("", DONE_RE.sub("", rest)).strip(),
                       "done": m[1] if m else ("" if key in BUILTIN else "signoff"), "run": run[1] if run else ""})
    m = re.search(r"^Options:(.*)$", head, re.M)
    first = STAGE_RE.search(head) or m
    intro = "" if f.name == "default.md" and f.parent == ROOT / "lifecycles" else \
        re.sub(r"^Options:.*$|\A#.*$", "", head[:first.start()] if first else head, flags=re.M).strip()
    return {"stages": stages, "options": dict(re.findall(r"`(\w+)=?([^`]*)`", m[1])) if m else {},
            "rules": rules.strip(), "intro": intro, "file": str(f),
            "source": "yours" if f.parent == STATE / "lifecycles" else "default" if f.name == "default.md" else "bundled"}


def lifecycle_md(repo, doc):
    """The inverse of lifecycle_doc. Raises ValueError on a bad or duplicate key, or a label with a backtick."""
    one = lambda t: " ".join(str(t or "").split())
    keys = [s["key"] for s in doc["stages"]]
    if not keys or len(set(keys)) != len(keys) or not all(re.fullmatch(r"\w+", k) for k in keys):
        raise ValueError(f"stage keys must be unique words: {keys}")
    if late := [k for k in split_merge(keys)[1] if k in BUILTIN]:
        raise ValueError(f"only your own steps can come after merge: {late}")
    out = [f"# {repo} lifecycle", ""] + ([doc["intro"].strip(), ""] if doc.get("intro", "").strip() else [])
    opts = doc.get("options") or {}
    if opts:
        out += ["Options: " + " ".join(f"`{k}={v}`" if v else f"`{k}`" for k, v in opts.items()), ""]
    for i, s in enumerate(doc["stages"], 1):
        done = "" if s["key"] in BUILTIN else one(s.get("done")) or "signoff"
        if not re.fullmatch(r"|signoff|label=[^`]+", done):
            raise ValueError(f"bad Done for {s['key']}: {done}")
        run = one(s.get("run")) if done == "signoff" else ""
        if "`" in run:
            raise ValueError(f"a Run command can't hold a backtick: {s['key']}")
        out.append(f"{i}. `{s['key']}` — {one(s.get('req'))}" + (f" Done: `{done}`" if done else "") + (f" Run: `{run}`" if run else ""))
    if doc.get("rules", "").strip():
        out += ["", "## Rules", "", doc["rules"].strip()]
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


def signoffs():
    f = STATE / "signoffs.json"
    return json.loads(f.read_text()) if f.exists() else {}


def release_milestone(project, forge="gitlab"):
    """The active milestone named <prefix>x.y.z (prefix from `milestone=`, default Release-) due latest."""
    prefix = re.escape(lifecycle_opts(project).get("milestone") or "Release-")
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
        if (pl := mr.get("head_pipeline") or {}).get("status") == "failed":  # the Fix CI prompt's {failed_jobs}
            mr["_failed_jobs"] = [{"name": j["name"], "stage": j["stage"], "url": j["web_url"]}
                                  for j in gl_all(f"projects/{pid(project)}/pipelines/{pl['id']}/jobs?scope[]=failed") if not j.get("allow_failure")]
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
        # ponytail: classic branch protection only (null unless you can read it); rulesets aren't counted
        "_approvals_required": (((p.get("baseRef") or {}).get("branchProtectionRule") or {}).get("requiredApprovingReviewCount") or 0), "_approvals_left": int(decision == "REVIEW_REQUIRED"),
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
    texts = dict.fromkeys(t for t in (ws.get("goal", ""), ws.get("comment", "")) if t)
    if title:
        texts = [re.sub(re.escape(title), " ", t, flags=re.I) for t in texts]
    return "\n".join(texts)


def check(step, mr, ws, milestone):
    """-> None when the step passes, else (owner, action, cmd|None)."""
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
    if step == "review_bot":
        if not BOT:
            return None
        a = {u["username"] for u in mr["assignees"]}
        r = {u["username"] for u in mr["reviewers"]}
        if BOT in a and BOT in r:
            return None
        return "greenlight", f"assign {BOT} as assignee + reviewer", upd + (["--add-assignee", BOT, "--add-reviewer", BOT] if github
                                                                   else ["--assignee", f"+{BOT}", "--reviewer", f"+{BOT}"])
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
        if KEEP_DRAFT_RE.search(said(ws, mr)):
            return "me", "goal says keep draft but everything else is green: take it out of draft?", ready  # the card's Mark ready runs it
        return "greenlight", "mark ready for review", ready
    if step == "threads":
        ds = unresolved(mr)
        todo = [d for d in ds if d["notes"][-1]["author"]["username"] not in (ME, BOT)]
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
        how = CONFIG["merge_method"]
        merge = (["gh", "pr", "merge", str(iid), "-R", proj, f"--{how}"] if github
                 else ["glab", "mr", "merge", str(iid), "-R", proj, "--yes"] + ([f"--{how}"] if how != "merge" else []))
        if st == "mergeable" and HOLD_RE.search(said(ws, mr)):
            return "me", "mergeable, but the goal says hold: merge now?", merge  # yes runs it (the card's Merge button too)
        opts = lifecycle_opts(proj)
        base, default = mr.get("target_branch"), mr.get("_default_branch")
        if st == "mergeable" and default and base != default and "merge_any_base" not in opts:
            # a stacked PR: it lands with (or after) its base, never auto-merged into a side branch
            if mr.get("_base_mr"):
                return "dependency", f"targets {base}: waiting on {ref(ws.get('forge'), mr['_base_mr'])} to merge first", None
            return "me", f"mergeable into {base}, not {default}: merge it by hand?", None
        if st == "mergeable" and CONFIG["require_human_approval"] and not set(mr.get("_approved_by", [])) - {ME, BOT}:
            keys = [s["key"] for s in lifecycle_doc(proj)["stages"]]
            checks = [k for k in (keys[:keys.index("merge")] if "merge" in keys else keys) if k not in BUILTIN]
            if "automerge" in opts and checks:  # the lifecycle's own steps (the agent's reviews, its try-out) stand in for a human approval; none, no automerge
                return "greenlight", "merge (automerge: every step before it passed)", merge
            if "solo" in opts:  # nobody else reviews this repo: I do, then greenlight runs the cmd
                return "me", "mergeable, and nobody else reviews this repo: review it, then merge?", merge
            return "reviewers", "mergeable but no human approval yet (repo doesn't require one)", None
        if st == "mergeable":
            return "greenlight", "merge", merge
        if st == "need_rebase":
            # user rule: rebase only on a real conflict, so ask rather than dispatch
            return "me", "GitLab needs a rebase to merge (no conflict): rebase it?", None
        if st in ("ci_must_pass", "ci_still_running", "checking", "unchecked", "preparing"):
            return "ci", f"merge status {st}", None
        if st == "merge_request_blocked" and mr.get("_blocked_by"):
            return "dependency", "waiting on " + ", ".join(f"!{i}" for i in mr["_blocked_by"]) + " to merge first", None
        if st == "blocked_status":
            return "me", "GitHub blocks the merge (branch protection): check the PR", None
        return "me", f"not mergeable: {st}", None
    stage = next(s for s in lifecycle_doc(proj)["stages"] if s["key"] == step)
    name, req = step.replace("_", " "), stage["req"] or "done?"
    if stage["done"].startswith("label="):
        label = stage["done"][6:]
        return None if label in mr.get("labels", []) else ("agent", f"{name}: {req} (add the label `{label}` when it's met)", None)
    if step in signoffs().get(f"{proj}#{iid}", []):
        return None
    return "me", f"{name}: {req} Sign it off?", ["python3", str(HERE / "greenlight.py"), "signoff", proj, str(iid), step]


def evaluate(ws, mr, milestone):
    """-> {step, owner, action, cmd}."""
    snooze = REVISIT_RE.search(ws["goal"])
    if snooze and snooze[1] > time.strftime("%Y-%m-%d") and not (mr and mr["state"] in ("merged", "closed")):
        return {"step": "parked", "owner": "parked", "action": f"parked until {snooze[1]}"}
    if PLAN_RE.match(ws["goal"]):
        return {"step": "plan", "owner": "parked", "action": "being planned in the Plan view", "hidden": True}
    if PARKED_RE.search(ws["goal"]) and not (mr and mr["state"] in ("merged", "closed")):
        return {"step": "parked", "owner": "parked", "action": "parked until you unpark it", "hidden": True}
    if not mr:
        if ws.get("branch_gone"):
            return {"step": "done", "owner": "done", "action": "its branch was deleted on the remote, so the change is over: cleanup.py removes it if its commits are on the remote, else delete it yourself"}
        if ws["branch"].startswith("auto-") and not ws["goal"]:
            return {"step": "automation", "owner": "me", "action": "automation run with no MR: delete?"}
        if not ws["goal"]:
            return {"step": "goal", "owner": "me", "action": "no MR and no goal: what's this workspace for?"}
        if AUTOMATION_RE.search(ws["goal"]):
            return {"step": "freeform", "owner": "working", "action": "run by an Orca automation"}
        return {"step": "freeform", "owner": "agent", "action": f"work toward goal: {ws['goal']}"}
    name, steps = lifecycle(ws["project"])
    pre, post = split_merge(steps)
    if mr["state"] == "merged" and mr["author"]["username"] == ME:
        for step in post:  # your own steps after the merge (deploy, verify): signoff/label only, the MR is done
            if step not in BUILTIN and (hit := check(step, mr, ws, milestone)):
                owner, action, cmd = hit
                return {"step": step, "owner": owner, "action": action, "cmd": cmd, "lifecycle": name}
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
        if any(d["notes"][-1]["author"]["username"] != ME for d in mine):
            return {"step": "review", "owner": "me", "action": "author replied on your threads: re-review"}
        return {"step": "review", "owner": "author", "action": f"{len(mine)} of your threads open, waiting on author"}
    for step in pre:
        hit = check(step, mr, ws, milestone)
        if hit:
            owner, action, cmd = hit
            return {"step": step, "owner": owner, "action": action, "cmd": cmd, "lifecycle": name}
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


def agent_overlay(ws, terminals):
    """Agent-owned work is only actionable if no agent is already on it, and the agent is alive."""
    nxt, ag = ws["next"], ws["agent"]
    if ag["state"] == "waiting":
        nxt.update(owner="me", action=f"agent is waiting on you: {ag['last'][:200]}")
        return
    if nxt["owner"] != "agent":
        return
    if ag["state"] == "working":
        nxt["owner"] = "working"
        return
    terms = sorted((t for t in terminals.get(ws["id"], []) if t.get("agentIdentity")),
                   key=lambda t: t.get("lastOutputAt") or 0, reverse=True)
    if not terms:
        return
    ws["agent"]["terminal"] = terms[0]["handle"]
    try:
        tail = orca("terminal", "read", "--terminal", terms[0]["handle"], "--limit", "200")["terminal"].get("tail", "")
    except Exception:
        return
    tail = tail if isinstance(tail, str) else "\n".join(map(str, tail))
    if DEAD_RE.search(tail[-3000:]):
        nxt.update(owner="me", action=f"agent logged out: /login in {terms[0]['handle']}, then greenlight re-dispatches")
    elif LIMIT_RE.search(tail[-3000:]) and len(CONFIG["agents"]) > 1:
        first, nxt["agent"] = CONFIG["agents"][:2]  # ponytail: assumes the stuck agent is the first; walk the list if they mix
        nxt["action"] += f" [{first} out of tokens: dispatch with {nxt['agent']}]"


def scan(save=True):
    wts = orca("worktree", "ps", "--limit", "10000")["worktrees"]
    full = {w["id"]: w for w in orca("worktree", "list", "--limit", "10000")["worktrees"]}
    terminals = {}
    for t in orca("terminal", "list", "--limit", "10000")["terminals"]:
        terminals.setdefault(t["worktreeId"], []).append(t)

    projects = {w["repoId"]: fp for w in full.values() if (fp := forge_project(w.get("projectId")))}
    rows, skipped = [], 0
    for w in wts:
        branch = (w.get("branch") or "").removeprefix("refs/heads/")
        if w.get("isMainWorktree") or not branch or w["repoId"] not in projects:
            skipped += 1  # main checkouts, detached HEADs (cache trees), repos on neither GitLab nor GitHub
            continue
        forge, project = projects[w["repoId"]]
        agents = sorted(w.get("agents", []), key=lambda a: a.get("updatedAt") or 0, reverse=True)
        ag = agents[0] if agents else {}
        rows.append({
            "id": w["worktreeId"], "name": w["displayName"], "path": w["path"], "branch": branch,
            "project": project, "forge": forge, "goal": "" if STATUS_LINE_RE.search(w.get("comment") or "") else (w.get("comment") or "").strip(),
            "comment": (w.get("comment") or "").strip(),
            "pinned": w.get("isPinned"), "linked_mr": linked_number(w.get("linkedPR" if forge == "github" else "linkedGitLabMR")),
            "agent": {"state": ag.get("state", "none"), "type": ag.get("agentType"),
                      "last": (ag.get("lastAssistantMessage") or "")[-400:],
                      "idle_h": w.get("lastOutputAt") and round((NOW_MS - w["lastOutputAt"]) / 3.6e6, 1)},
        })

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

    def one(r):
        try:
            mr = find_mr(r)
            r["mr"] = mr and {k: mr.get(k) for k in ("iid", "web_url", "title", "state", "draft")} | {
                "ref": ref(r["forge"], mr["iid"]), "forge": r["forge"],
                "author": mr["author"].get("login") or mr["author"]["username"], "role": "author" if mr["author"]["username"] == ME else "review",
                # Basement card byline: forge avatars, username on hover
                "by": face(mr["author"]), "assignees": [face(a) for a in mr.get("assignees") or []],
                # hex signals (dashboard): as of this scan
                "pipeline": (mr.get("head_pipeline") or {}).get("status"), "conflicts": bool(mr.get("has_conflicts")),
                "threads": len(unresolved(mr)), "approvals": len(mr.get("_approved_by", [])),
                "approvals_required": mr.get("_approvals_required", 0), "mergeable": mr.get("detailed_merge_status") == "mergeable", "rebase": mr.get("detailed_merge_status") == "need_rebase",
                # hover card (dashboard)
                "source_branch": mr.get("source_branch"), "target_branch": mr.get("target_branch"), "labels": mr.get("labels", []),
                "description": (mr.get("description") or "")[:2000],  # Basement ⌘J matches on it; ponytail: first 2000 chars
                "milestone": (mr.get("milestone") or {}).get("title"), "approved_by": mr.get("_approved_by", []),
                "pipeline_url": (mr.get("head_pipeline") or {}).get("web_url"),
                "sha": mr.get("sha"), "reviewed_sha": mr.get("_reviewed_sha"),  # the review prompt's {sha} and {last_review_sha}
                "todo_threads": [thread_line(d) for d in unresolved(mr) if d["notes"][-1]["author"]["username"] not in (ME, BOT)][:40],
                "failed_jobs": mr.get("_failed_jobs", [])[:40],  # the Resolve threads / Fix CI prompts' {threads} / {failed_jobs}
                "threads_total": sum(1 for d in mr.get("_discussions", []) if d["notes"] and d["notes"][0].get("resolvable"))}
            r["branch_gone"] = not mr and upstream_gone(r)
            r["next"] = evaluate(r, mr, milestones.get(r["project"]))
            life = lifestage.place(r)  # before the overlay: who the step itself is waiting on
            agent_overlay(r, terminals)
        except Exception as e:
            r["mr"], r["next"] = None, {"step": "error", "owner": "me", "action": f"greenlight couldn't evaluate: {e}"}
            life = lifestage.place(r)
        r["life"] = lifestage.now(life, r["agent"]["state"], "logged out" in r["next"].get("action", ""), lifestage.block_of(r, roster))
        return r

    with ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(one, rows))
    rows.sort(key=lambda r: (OWNERS.index(r["next"]["owner"]), r["project"], r["name"]))

    STATE.mkdir(exist_ok=True)
    prev_file = STATE / "last.json"
    prev = json.loads(prev_file.read_text()) if prev_file.exists() else {}
    prev_rows = {r["id"]: r for r in prev.get("rows", [])}
    for r in rows:
        p = prev_rows.get(r["id"])
        if p and (p["next"]["step"], p["next"]["owner"]) != (r["next"]["step"], r["next"]["owner"]):
            r["changed"] = f"{p['next']['step']}/{p['next']['owner']} → {r['next']['step']}/{r['next']['owner']}"
    gone = [p["name"] for i, p in prev_rows.items() if i not in {r["id"] for r in rows}]
    result = {"ts": int(time.time()), "prev_ts": prev.get("ts"), "skipped": skipped, "gone": gone, "rows": rows}
    if save:  # an actionable precheck mustn't overwrite the snapshot the run's report diffs against
        keep(result)
    return result


def keep(result):
    (STATE / "last.json").write_text(json.dumps(result))
    close_stale(result["rows"])


def outbox(ev):
    STATE.mkdir(exist_ok=True)
    with (STATE / "outbox.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": int(time.time())} | ev) + "\n")


def pinged(update=None):
    """Open questions keyed by workspace name or id: {key: ts}. ~/.greenlight/pinged.json"""
    f = STATE / "pinged.json"
    if update is None:
        return json.loads(f.read_text()) if f.exists() else {}
    f.write_text(json.dumps(update))


def ping(text, key=None, ws=(), kind=None):
    """ws: the workspaces (names) it's about; a workspace key counts as one. The app lifts their hexes until it's answered.
    kind: the sort of question, for the decision record (decisions.py); guessed from the key or the lifecycle step when not given."""
    if key and key.startswith("@") and key in recent_keys():  # puck blocks flap in and out of its list
        print(f"skipped: {key} was pinged in the last {FLAP // 3600}h")
        return
    mine = [scanned_names().get(key, key)] if key and not key.startswith("@") else []  # an id key lifts that workspace by name
    ws = list(dict.fromkeys(mine + list(ws)))
    outbox({"text": text} | ({"key": key} if key else {}) | ({"ws": ws} if ws else {}))
    decisions.quiet("asked", text, "ping", key, ws, kind)
    if key:
        pinged(pinged() | {key: int(time.time())})


FLAP = 24 * 3600


def recent_keys():
    """Keys pinged within FLAP, read from the outbox itself."""
    f, cut = STATE / "outbox.jsonl", time.time() - FLAP
    return {e["key"] for e in map(json.loads, f.read_text().splitlines() if f.exists() else [])
            if "text" in e and e.get("key") and e["ts"] > cut}


def scanned_names():
    """{name or id: name} for every workspace in the last scan (~/.greenlight/last.json)."""
    f = STATE / "last.json"
    rows = json.loads(f.read_text()).get("rows", []) if f.exists() else []
    return {r["id"]: r["name"] for r in rows} | {r["name"]: r["name"] for r in rows}


def ping_cli(args):
    """greenlight.py ping [--key K] [--kind KIND] [--ws W]... <text>: --ws takes a workspace name or id; an unknown one sends nothing."""
    key, kind, ws = None, None, []
    while len(args) > 1 and args[0] in ("--key", "--kind", "--ws"):
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
    ping(" ".join(args), key, [names[w] for w in ws], kind)


def resolve(key, why):
    outbox({"resolve": key, "why": why})
    decisions.quiet("resolve", key, why)
    pinged({k: t for k, t in pinged().items() if k != key})


def close_stale(rows):
    """Questions about a workspace close once it's gone or no longer waiting on me."""
    by_key = {r["name"]: r for r in rows} | {r["id"]: r for r in rows}
    for key in pinged():
        if key.startswith("@"):  # not a workspace: closed by hand with `greenlight.py resolve`
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
    for o in OWNERS:
        group = [r for r in rows if r["next"]["owner"] == o]
        if not group:
            continue
        trs = "".join(
            "<tr><td>{n}<div class=sub>{p}</div></td><td>{mr}</td><td><code>{s}</code></td><td>{a}{g}</td><td>{ag}</td></tr>".format(
                n=e(r["name"]), p=e(r["project"].rsplit("/", 1)[-1] + " · " + r["branch"]),
                mr=(f'<a href="{e(r["mr"]["web_url"])}">{e(r["mr"].get("ref") or "!" + str(r["mr"]["iid"]))}</a><div class=sub>{e(r["mr"]["role"])}</div>' if r.get("mr") else "—"),
                s=e(r["next"]["step"]), a=e(r["next"]["action"]),
                g=f'<div class=sub>goal: {e(r["goal"][:140])}</div>' if r["goal"] else '<div class="sub warn">no goal</div>',
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
        print(json.dumps(orca(*[a for a in without_change(sys.argv[2:]) if a != "--json"]), indent=1))
        return
    if cmd == "signoff":  # greenlight.py signoff <project> <iid> <stage>: the user signed off a custom stage on this MR
        so, k = signoffs(), f"{sys.argv[2]}#{sys.argv[3]}"
        so[k] = sorted(set(so.get(k, [])) | {sys.argv[4]})
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "signoffs.json").write_text(json.dumps(so, indent=1))
        return
    if cmd == "history":  # greenlight.py history <workspace>
        f = history_file(sys.argv[2])
        print(f.read_text() if f.exists() else "(no history)")
        return
    res = scan(save=cmd != "precheck")
    if cmd == "precheck":
        n = sum(r["next"]["owner"] in ("greenlight", "agent") for r in res["rows"])
        try:  # MRs with no workspace yet: adopt.py creates them, so a run should follow
            import adopt
            n += len(adopt.plan(res["rows"], res.get("ts"))["create"])
        except Exception as e:
            print(f"adopt plan failed: {e}", file=sys.stderr)
        if not n:  # no run follows, so this is the freshest snapshot the dashboard will get
            keep(res)
        print(f"{n} actionable workspace(s) or adoption(s)")
        sys.exit(0 if n else 1)
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
    main()
