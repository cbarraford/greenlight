#!/usr/bin/env python3
"""Workspace state cards: a small structured handoff per workspace, so a new or failed-over agent (and the chat) starts
from what is known instead of re-reading the board, the history and the transcript.

    ws_state.py show <ws> [--json]          the card
    ws_state.py brief <ws>                  the block a dispatch prompt carries (what orchestrate.py injects)
    ws_state.py set <ws> [--hypothesis T] [--decision T]... [--failed T]... [--risk T]... [--clear-risks]
                                            an agent (or you) records what it learned; lists append, newest first
    ws_state.py top <query> [-k K] [--json] the K cards and memory notes most relevant to the query
    ws_state.py observe                     refresh every card from ~/.greenlight/last.json (orchestrate.py tick does this)
    ws_state.py reindex                     rebuild the search index from the cards and memory notes

A card is ~/.greenlight/state/<ws>.json (filename special characters are percent-encoded):
    goal_contract       the goal, and what "done" means for it (the MR merging through its lifecycle, or the free-form goal)
    current_hypothesis  what the agent thinks is going on: its own note (`set --hypothesis`) while the stage holds, else its
                        last message
    decisions_made      the user's answers about this workspace (decisions.jsonl), handoffs, greenlight's judged forks
    failed_approaches   dispatches that ended with the step unchanged, and what agents recorded with `set --failed`
    touched_files       the branch's changed files against its target, plus uncommitted ones
    open_risks          what's wrong now (failed jobs, conflicts, open threads, a hold, a logged-out agent) and noted risks
    last_stage          lifecycle sub-stage and step/owner, e.g. "4.3 threads/agent"
It's updated at every transition (orchestrate.py tick → observe) and whenever an agent runs `set`.

Retrieval (`top`, chat.py's context): an inverted index in ~/.greenlight/state/index.sqlite, table
postings(term, doc, tf) with PRIMARY KEY (term, doc) WITHOUT ROWID, i.e. a B-tree keyed by term. A query of |q| terms
reads only those terms' posting lists: each lookup is O(log P) (P = postings in the index, ~ N·terms per card) plus the
length p_t of the term's list, then BM25 over the C candidates and a heap for the top K: O(|q|·log P + Σp_t + C·log K).
That is O(log N) per term for specific words (a workspace name, a file, an MR number, an error) whose lists are short;
a term that's in more than half the cards is dropped when the query has better ones, but a query of only common words
still reads lists as long as N, so the worst case is O(|q|·N), not O(log N). Writing a card re-indexes that card only:
O(its terms · log P). Nothing loads every card per query; the old BOARD sent every workspace's line (O(N) tokens).
Memory notes are indexed too; a query stats each note file to pick up edits (O(M), M = notes, a few dozen).
The doc count and total length BM25 needs are kept counters (table stats), not aggregates over every doc.

Stdlib only; never imports greenlight.py (its import reads config and may call the forge).
"""
import contextlib
import copy
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import quote
from pathlib import Path

STATE = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
DIR = STATE / "state"
ROOT = Path(__file__).resolve().parent.parent  # the skill dir
MEMORY = Path(os.environ.get("GREENLIGHT_MEMORY") or ROOT / "memory")  # the owner's notes (SKILL.md)
CAPS = {"decisions_made": 15, "noted_decisions": 15, "failed_approaches": 10, "touched_files": 40, "open_risks": 12, "noted_risks": 8,
        "touched_committed": 40, "touched_dirty": 40, "transitions": 20, "dispatches": 12}
FIELDS = ("goal_contract", "current_hypothesis", "decisions_made", "failed_approaches", "touched_files", "open_risks", "last_stage")
BRIEF_CAP = 3000  # chars of card in a dispatch prompt
STOP = set("the a an and or of to in on for is are was be it its this that with by at as from not no do does did what which "
           "who how why when where me my you your we our i next now status please thanks up get going should can could would "
           "will any all there here about just".split())  # chat filler: "what next?" names no workspace
FLOOR = 0.3  # a hit scoring under this share of the best one isn't relevant, only nearby
TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


def safe(name):
    return quote(name, safe="")


def path(name):
    return DIR / f"{safe(name)}.json"


def owner_file(worktree_id):
    return DIR / f".owner-{digest(worktree_id)}"


def owner_keys(ids, migrate=True):
    """Resolve IDs in O(N), migrating legacy cards once across all future scans."""
    owners, missing = {}, set()
    for wid in ids:
        try:
            record = json.loads(owner_file(wid).read_text())
            if record.get("id") == wid:
                owners[wid] = record["card"]
                continue
        except (OSError, ValueError, KeyError):
            pass
        missing.add(wid)
    if missing and not (DIR / ".owners-migrated").exists() and not migrate:
        # Preview callers still need legacy ownership, but must not create pointers.
        for card_file in DIR.glob("*.json"):
            with contextlib.suppress(OSError, ValueError):
                card = json.loads(card_file.read_text())
                if card.get("id") in missing and card.get("ws"):
                    owners[card["id"]] = card["ws"]
    if missing and not (DIR / ".owners-migrated").exists() and migrate:
        with index_write_lock():
            if not (DIR / ".owners-migrated").exists():
                for card_file in DIR.glob("*.json"):
                    with contextlib.suppress(OSError, ValueError):
                        card = json.loads(card_file.read_text())
                        if card.get("id") and card.get("ws"):
                            record = {"id": card["id"], "card": card["ws"]}
                            owner_file(card["id"]).write_text(json.dumps(record))
                            if card["id"] in missing:
                                owners[card["id"]] = card["ws"]
                (DIR / ".owners-migrated").touch()
    for wid in missing - owners.keys():
        with contextlib.suppress(OSError, ValueError, KeyError):
            record = json.loads(owner_file(wid).read_text())
            if record.get("id") == wid:
                owners[wid] = record["card"]
    return owners


def keyed(rows, migrate=True):
    """Assign durable card keys. A reused display name must not inherit a different worktree's state."""
    claimed_bare = set()
    assigned = set()
    literal_names = {r["name"] for r in rows}
    owners = owner_keys({r["id"] for r in rows if not r.get("card")}, migrate=migrate)
    for r in rows:
        requested = r.get("card")
        if not requested:
            requested = owners.get(r["id"])
        if requested and requested not in assigned and (not path(requested).exists() or load(requested).get("id") in (None, r["id"])):
            r["card"] = requested
            assigned.add(requested)
            continue
        name = r["name"]
        base = f"{name}@id-{digest(r['id'])}"
        qualified, suffix = base, 2
        while (qualified in assigned or qualified in literal_names and qualified != name or
               path(qualified).exists() and load(qualified).get("id") not in (None, r["id"])):
            qualified, suffix = f"{base}~{suffix}", suffix + 1
        bare_exists = path(name).exists()
        bare_id = load(name).get("id") if bare_exists else None
        owned_bare = bare_exists and bare_id == r["id"]
        free_bare = not bare_exists or bare_id is None
        if path(qualified).exists():
            r["card"] = qualified
        elif name not in assigned and (owned_bare or free_bare and name not in claimed_bare):
            r["card"] = name
            claimed_bare.add(name)
        else:
            r["card"] = qualified
        assigned.add(r["card"])
    return rows


def blank(name):
    return {"ws": name, **{k: [] if k in CAPS else "" for k in FIELDS}, "goal_source": "", "situation_source": "", "mr_ref": "", "noted_risks": [], "noted_decisions": [], "transitions": [], "dispatches": [],
            "forks": {}, "updated": 0}


def load(name):
    try:
        return blank(name) | json.loads(path(name).read_text())
    except (OSError, ValueError):
        return blank(name)


@contextlib.contextmanager
def lock():
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # the tick and an agent's `set` can land together
        yield


@contextlib.contextmanager
def index_write_lock():
    """Serialize JSON/index writes with a rebuild, including its dirty-marker cleanup."""
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / ".index-write.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def save(card):
    card["updated"] = int(time.time())
    for k, n in CAPS.items():
        card[k] = list(card.get(k) or [])[:n]
    DIR.mkdir(parents=True, exist_ok=True)
    with index_write_lock():
        index_missing = not (DIR / "index.sqlite").exists()
        marker = DIR / ".index-dirty"
        prior_dirty = marker.exists()
        token = uuid.uuid4().hex
        marker.write_text(token)  # a kill after replacing JSON must leave a repair signal
        if card.get("id"):
            owner = owner_file(card["id"])
            record = json.dumps({"id": card["id"], "card": card["ws"]})
            if not owner.exists() or owner.read_text() != record:
                owner_tmp = owner.with_suffix(".tmp")
                owner_tmp.write_text(record)
                os.replace(owner_tmp, owner)
        tmp = path(card["ws"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(card, indent=1))
        os.replace(tmp, path(card["ws"]))
        try:
            with index() as db:
                put(db, "card:" + card["ws"], "card", text_of(card))
        except sqlite3.Error:
            return  # the write-ahead marker asks the next query to rebuild
        if not prior_dirty and not index_missing and marker.read_text() == token:
            marker.unlink()  # a clean existing index received this write


def push(lst, item):
    """Newest first, no repeats."""
    item = " ".join(str(item).split())
    return [item] + [x for x in lst if x != item] if item else lst


def update(name, **kw):
    """set's API: hypothesis (str), decision/failed/risk (str or list), clear_risks. Returns the card."""
    with lock():
        c = load(name)
        if kw.get("if_situation") and situation_token(c) != kw["if_situation"]:
            raise SystemExit("workspace situation changed; refresh the state card before writing notes")
        if kw.get("hypothesis"):
            c.update(current_hypothesis=" ".join(kw["hypothesis"].split())[:600], hypothesis_by="note", hypothesis_stage=c["last_stage"])
        if kw.get("clear_risks"):
            c["noted_risks"] = []
        for key, field in (("decision", "decisions_made"), ("failed", "failed_approaches"), ("risk", "noted_risks")):
            vals = kw.get(key) or []
            for v in [vals] if isinstance(vals, str) else vals:
                c[field] = push(c[field], v)
                if key == "decision":
                    c["noted_decisions"] = push(c.get("noted_decisions", []), v)
        c["open_risks"] = merge(c.get("derived_risks", []), c["noted_risks"])
        save(c)
        return c


def merge(*lists):
    return list(dict.fromkeys(x for lst in lists for x in lst))


# ---- observe: a scan row → its card


def git(p, *args):
    r = subprocess.run(["git", "-C", p, *args], capture_output=True, text=True, errors="backslashreplace", timeout=30)
    return r.stdout if r.returncode == 0 else None


def touched(row, prior=(), prior_dirty=(), prior_committed=None, with_dirty=False):
    p = row.get("path") or ""
    if not os.path.isdir(p):
        return None
    mr = row.get("mr") or {}
    base = None
    refs = [f"origin/{mr['target_branch']}"] if mr.get("target_branch") else ["origin/HEAD", "origin/main", "origin/master"]
    for ref in refs:
        if (git(p, "rev-parse", "--verify", "-q", ref) or "").strip():
            base = ref
            break
    status_out = git(p, "status", "--porcelain", "-z", "--untracked-files=all")
    diff = git(p, "diff", "--name-only", "-z", f"{base}...HEAD") if base else ""
    if diff is None or status_out is None:
        return None  # a failed Git read is not an empty changed-file list
    committed = [x for x in diff.split("\0") if x] if base else list(prior_committed if prior_committed is not None else [x for x in prior if x not in prior_dirty])
    status = status_out.split("\0")
    dirty = []
    i = 0
    while i < len(status) and status[i]:
        entry = status[i]
        if len(entry) > 3:
            dirty.append(entry[3:])
        i += 2 if any(x in entry[:2] for x in "RC") else 1  # -z lists a rename's source after its destination
    files = merge(committed, dirty)
    return (files, committed, dirty) if with_dirty else files


def risks(row):
    mr, nxt, out = row.get("mr") or {}, row.get("next") or {}, []
    if mr.get("pipeline") == "failed":
        jobs = ", ".join(j.get("name", "?") for j in (mr.get("failed_jobs") or [])[:5])
        out.append(f"CI failed{': ' + jobs if jobs else ''}")
    if mr.get("conflicts"):
        out.append("merge conflicts")
    if mr.get("threads"):
        out.append(f"{mr['threads']} unresolved thread(s)")
    act = nxt.get("action") or ""
    if "logged out" in act:
        out.append("agent logged out")
    if "out of tokens" in act:
        out.append(act[act.index("[") + 1:].rstrip("]") if "[" in act else "agent out of tokens")
    goal_words = row.get("goal") or ""
    if mr.get("title") and not re.search(r"(?:\(|[—–-]\s*)\s*(?:hold|do ?n[o'’]t merge)\b", mr["title"], re.I):
        header = re.compile(rf"^((?:Merge|Review)\s+[#!]\d+:\s*){re.escape(mr['title'].strip())}"
                            r"(?=[.!?]?(?:$|\n)|\s+\((?!until\b)|\s+[—–-]\s+)", re.I)
        goal_words = header.sub(r"\1", goal_words, count=1)
    if re.search(r"(?<![\w-])(?:hold|do ?n[o'’]t merge)(?![\w-])", goal_words, re.I):
        out.append("goal says hold: no merge")
    if nxt.get("owner") == "me":
        out.append(f"waiting on the user: {act[:160]}")
    return out


def stage_of(row):
    nxt, life = row.get("next") or {}, row.get("life") or {}
    at = life.get("at")
    return f"{at + ' ' if at else ''}{nxt.get('step', '?')}/{nxt.get('owner', '?')}"


def contract(row):
    """The goal and completion criteria shared by manual and orchestrated handoffs."""
    goal, mr = row.get("goal") or "", row.get("mr") or {}
    if not mr:
        return goal and f"{goal} (done when the goal is met; a free-form goal has no MR)"
    try:
        import greenlight
        name, steps = greenlight.lifecycle(row["project"])
    except Exception:
        name, steps = "lifecycle", []
    verb = "merges" if mr.get("role") == "author" else "is reviewed (approved or threads left)"
    ref = mr.get("ref") or (f"!{mr['iid']}" if mr.get("iid") else "")
    return f"{goal or mr.get('title') or ''} — done when {ref} {verb} through {name}" + (f" ({', '.join(steps)})" if steps else "")


def answered(recs, row, key, since=0, allow_untagged=True):
    """The user's answers about this workspace, newest first: 'question → answer'."""
    out = []
    for r in recs:
        ids = [r.get("worktree_id")] + [w.get("worktree_id") for w in r.get("workspaces") or []]
        targets = [r] + (r.get("workspaces") or [])
        mr_ref = (row.get("mr") or {}).get("ref")
        matched = False
        for t in targets:
            identified = t.get("worktree_id") == row["id"] and t.get("mr") == mr_ref and \
                (not t.get("repo", r.get("repo")) or t.get("repo", r.get("repo")) == row.get("project"))
            if not identified:  # a name-only historical answer cannot prove which worktree received it
                continue
            goal_tag = t.get("goal", r.get("goal"))
            if goal_tag is not None and goal_tag != row.get("goal"):
                continue
            answer_sha = t.get("sha", r.get("sha"))
            if answer_sha and answer_sha != (row.get("mr") or {}).get("sha"):
                continue
            if (t.get("target_branch", r.get("target_branch")) or "") != ((row.get("mr") or {}).get("target_branch") or ""):
                continue  # an unscoped historical approval cannot authorize a different target
            if mr_ref is None and t.get("branch", r.get("branch")) != row.get("branch"):
                continue  # a free-form answer belongs to the branch on which it was asked
            if goal_tag is None and (not allow_untagged or (r.get("answered") or r.get("ts") or 0) < since):
                continue
            matched = True
            break
        if r.get("answer") and matched:
            q = " ".join((r.get("question") or "").split())
            out.append((r.get("answered") or r.get("ts") or 0, f"{q[:140]} → {' '.join(str(r['answer']).split())[:120]}"))
    return [t for _, t in sorted(out, reverse=True)]


def decision_records():
    f = STATE / "decisions.jsonl"
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        if line.strip():
            with contextlib.suppress(ValueError):
                records.append(json.loads(line))
    return records


def observe(row, contract=None, recs=None):
    """Refresh one card from a scan row. Returns (card, transition) where transition is (old stage, new stage) or None."""
    name = row["name"]
    published_key = row.get("card")
    touch_key = row.get("card") or name
    touch_prior = load(touch_key)
    same_files_scope = (touch_prior.get("forge") == row.get("forge") and touch_prior.get("project") == row.get("project") and
                        touch_prior.get("mr_ref") == (row.get("mr") or {}).get("ref", "") and
                        touch_prior.get("branch") == row.get("branch") and
                        touch_prior.get("target_branch") == (row.get("mr") or {}).get("target_branch"))
    touch = touched(row, touch_prior["touched_files"] if same_files_scope else [],
                    touch_prior.get("touched_dirty", []) if same_files_scope else [],
                    touch_prior.get("touched_committed") if same_files_scope else [], with_dirty=True)
    with lock():
        if row.get("_scan_id"):
            try:
                marker = (STATE / "scan_id").read_text().split()
                current_inode = str((STATE / "last.json").stat().st_ino)
            except OSError:
                marker, current_inode = [], ""
            current = len(marker) == 2 and marker == [row["_scan_id"], current_inode]
        else:
            current = True
        if not current:
            # A killed publisher may have replaced the snapshot but not its fast marker.
            try:
                snapshot = json.loads((STATE / "last.json").read_text())
            except (OSError, ValueError):
                snapshot = {}
            if snapshot.get("scan_id") != row["_scan_id"]:
                raise RuntimeError("scan superseded before card update")
        key = keyed([row])[0]["card"]  # recheck ownership under the write lock; another observer may have claimed bare
        c, now = load(key), int(time.time())
        touch_current = key == touch_key and c.get("touched_files") == touch_prior.get("touched_files") and \
            c.get("touched_committed") == touch_prior.get("touched_committed") and c.get("touched_dirty") == touch_prior.get("touched_dirty")
        touch_scope_current = (c.get("forge"), c.get("project"), c.get("mr_ref"), c.get("branch"), c.get("target_branch")) == \
            (touch_prior.get("forge"), touch_prior.get("project"), touch_prior.get("mr_ref"), touch_prior.get("branch"), touch_prior.get("target_branch"))
        stage, sha = stage_of(row), (row.get("mr") or {}).get("sha") or ""
        source = row.get("goal") or ""
        goal = contract if contract is not None else (c["goal_contract"] if c.get("goal_source") == source and c["goal_contract"] else source)
        failed_jobs = sorted([str(j.get("stage") or ""), str(j.get("name") or "")] for j in (row.get("mr") or {}).get("failed_jobs") or [])
        situation_now = [row.get("next", {}).get("step"), failed_jobs, (row.get("mr") or {}).get("threads"),
                         (row.get("mr") or {}).get("iid"), sha, (row.get("mr") or {}).get("threads_fingerprint"),
                         (row.get("mr") or {}).get("target_branch")]
        moved = c["last_stage"] != stage
        step_changed = moved and c["last_stage"] and c["last_stage"].split(" ")[-1].split("/")[0] != stage.split(" ")[-1].split("/")[0]
        branch_changed = bool(c.get("branch") and c["branch"] != row.get("branch"))
        goal_changed = bool(c["last_stage"] and c.get("goal_source", source) != source and not row.get("_goal_backfill"))
        # Missing job details are inconclusive; they must not erase a user's leave-it answer.
        old_situation = json.loads(c.get("situation_source") or "null")
        if old_situation and len(old_situation) > 3 and old_situation[3] is not None and not (row.get("mr") or {}).get("iid") and \
                row.get("next", {}).get("step") == "error":
            row["_state_reset"] = False
            row["_mr_inconclusive"] = True
            return c, None  # a transient missing MR is not evidence that the old request was replaced
        new_situation = situation_now
        old_target = old_situation[6] if old_situation and len(old_situation) > 6 else c.get("target_branch")
        new_target = new_situation[6]
        old_key = f"{c.get('forge') or row.get('forge')}:{c.get('project')}:{old_situation[3]}:{old_target}" if old_situation and len(old_situation) > 3 else ""
        new_key = f"{row.get('forge')}:{row.get('project')}:{new_situation[3]}:{new_target}"
        mr_changed = bool(old_situation and len(old_situation) > 3 and (
            old_situation[3] != new_situation[3] and
            (old_situation[3] is not None or goal_changed or new_key in c.get("mr_archive", {})) or
            old_situation[3] is not None and new_situation[3] is not None and old_target != new_target or
            c.get("project") != row.get("project") or
            c.get("forge") and c.get("forge") != row.get("forge")))
        old_mr = old_situation[3] if old_situation and len(old_situation) > 3 else None
        freeform_changed = bool((goal_changed or branch_changed) and old_mr is None and new_situation[3] is None)
        fields_for_goal = ("decisions_made", "noted_decisions", "failed_approaches", "noted_risks")
        mr_goal_changed = bool(goal_changed and old_mr is not None and not mr_changed)
        if mr_goal_changed:
            archive = c.setdefault("mr_goal_archive", {})
            archive[f"{old_key}:{digest(c.get('goal_source') or '')}"] = {field: copy.deepcopy(c.get(field)) for field in fields_for_goal} | {"sha": c.get("sha")}
            while len(archive) > 8:
                archive.pop(next(iter(archive)))
        if old_situation and old_mr is None and (goal_changed or branch_changed or mr_changed or new_situation[3] is not None):
            archive = c.setdefault("goal_archive", {})
            archive[digest(json.dumps([c.get("forge"), c.get("project"), c.get("branch"), c.get("goal_source") or ""]))] = \
                {field: copy.deepcopy(c.get(field)) for field in fields_for_goal}
            while len(archive) > 8:
                archive.pop(next(iter(archive)))
        if mr_changed:
            archive = c.setdefault("mr_archive", {})
            fields = ("decisions_made", "noted_decisions", "failed_approaches", "noted_risks", "touched_files",
                      "touched_committed", "touched_dirty", "current_hypothesis", "hypothesis_by", "hypothesis_stage",
                      "dispatches", "forks")
            if old_situation[3] is not None:
                archive[old_key] = {field: copy.deepcopy(c.get(field)) for field in fields} | {"goal_source": c.get("goal_source")}
                goal_archive = c.setdefault("mr_goal_archive", {})
                goal_archive[f"{old_key}:{digest(c.get('goal_source') or '')}"] = {field: copy.deepcopy(c.get(field)) for field in fields_for_goal} | {"sha": c.get("sha")}
                while len(goal_archive) > 8:
                    goal_archive.pop(next(iter(goal_archive)))
            while len(archive) > 8:
                archive.pop(next(iter(archive)))
        situation_changed = bool(old_situation and (old_situation[0] != new_situation[0] or
            (new_situation[1] and old_situation[1] and old_situation[1] != new_situation[1]) or
            (new_situation[2] is not None and old_situation[2] is not None and old_situation[2] != new_situation[2]) or
            mr_changed or
            len(old_situation) > 4 and old_situation[4] != new_situation[4] or
            (old_situation[5] if len(old_situation) > 5 else None) != new_situation[5]))
        situation_changed = situation_changed or bool(old_situation and
            (old_situation[6] if len(old_situation) > 6 else c.get("target_branch")) != new_situation[6])
        if old_situation and old_situation[0] == new_situation[0] and not mr_changed:
            if not new_situation[1]:
                new_situation[1] = old_situation[1]
            if new_situation[2] is None:
                new_situation[2] = old_situation[2]
        situation = json.dumps(new_situation)
        row["_state_reset"] = bool(step_changed or goal_changed or branch_changed or situation_changed)
        if row["_state_reset"]:
            c["stale_pings"] = merge(c.get("stale_pings", []), [v["ping_key"] for v in c["forks"].values() if v.get("ping_key")])[:40]
            if step_changed or goal_changed or branch_changed or mr_changed:
                c["handoff_epoch"] = uuid.uuid4().hex
            if step_changed or goal_changed or branch_changed or mr_changed:
                c["dispatches"] = [d for d in c["dispatches"] if d.get("pending")]
                c["current_hypothesis"] = ""
                c.pop("hypothesis_by", None)
                c.pop("hypothesis_stage", None)
            c["forks"] = {}
            if old_situation and len(old_situation) > 4 and old_situation[4] != new_situation[4]:
                c["decisions_made"] = list(c.get("noted_decisions") or [])
            if mr_changed:
                unresolved_launches = [d for d in c["dispatches"] if d.get("pending")]
                previous = c["mr_archive"].get(new_key, {})
                for field in fields:
                    restored = previous.get(field) if not (field in ("dispatches", "forks") or step_changed and field in ("current_hypothesis", "hypothesis_by", "hypothesis_stage")) else None
                    c[field] = copy.deepcopy(restored if restored is not None else {} if field == "forks" else "" if field in ("current_hypothesis", "hypothesis_by", "hypothesis_stage") else [])
                c["dispatches"] = unresolved_launches + c["dispatches"]
                if new_situation[3] is not None:
                    goal_previous = c.get("mr_goal_archive", {}).get(f"{new_key}:{digest(source)}")
                    if goal_previous or previous.get("goal_source") != source:
                        for field in fields_for_goal:
                            c[field] = copy.deepcopy((goal_previous or {}).get(field) or [])
                    if (goal_previous or previous).get("sha") != sha:
                        c["decisions_made"] = list(c.get("noted_decisions") or [])
                if new_situation[3] is None:
                    previous = c.get("goal_archive", {}).get(digest(json.dumps([row.get("forge"), row.get("project"), row.get("branch"), source])), {})
                    for field in fields_for_goal:
                        c[field] = copy.deepcopy(previous.get(field) or [])
                    c["goal_started_at"] = now
            elif freeform_changed:
                previous = c["goal_archive"].get(digest(json.dumps([row.get("forge"), row.get("project"), row.get("branch"), source])), {})
                for field in fields_for_goal:
                    value = previous.get(field)
                    c[field] = copy.deepcopy(value if value is not None else [])
                c["goal_started_at"] = now  # archive carries A's answers; B's later answers must not be reimported
            elif mr_goal_changed:
                previous = c["mr_goal_archive"].get(f"{new_key}:{digest(source)}", {})
                for field in fields_for_goal:
                    c[field] = copy.deepcopy(previous.get(field) or [])
                if previous.get("sha") != sha:
                    c["decisions_made"] = list(c.get("noted_decisions") or [])
        assignment_goal = c.get("assignment_goal_source", c.get("goal_source") or "")
        c.update(id=row.get("id"), display_name=name, project=row.get("project"), forge=row.get("forge"), branch=row.get("branch"),
                 mr_ref=(row.get("mr") or {}).get("ref") or "", target_branch=(row.get("mr") or {}).get("target_branch"),
                 goal_contract=goal, goal_source=source, situation_source=situation)
        c["assignment_goal_source"] = assignment_goal if row.get("_goal_backfill") and touch_prior.get("id") else source
        if moved:
            c["transitions"] = [{"ts": now, "from": c["last_stage"], "to": stage}] + c["transitions"]
            c["last_stage"] = stage
        if touch is not None and touch_current:
            c["touched_files"], c["touched_committed"], c["touched_dirty"] = touch
        elif touch is None and not same_files_scope and touch_current and touch_scope_current:
            c["touched_files"], c["touched_committed"], c["touched_dirty"] = [], [], []
        c["sha"] = sha
        last = (row.get("agent") or {}).get("last") or ""
        last = " ".join(last.split())[-600:]
        if last and last != c.get("last_agent_message") and not (c.get("hypothesis_by") == "note" and c.get("hypothesis_stage") == stage):
            c.update(current_hypothesis=last, hypothesis_by="agent")
        c["last_agent_message"] = last
        c["derived_risks"] = risks(row)
        c["open_risks"] = merge(c["derived_risks"], c["noted_risks"])
        ours = c["decisions_made"]  # preserve answers already attached to this worktree's card during migration
        c["decisions_made"] = merge(answered(decision_records() if recs is None else recs, row, key,
                                               c.get("goal_started_at", 0) if not (row.get("mr") or {}).get("iid") else 0,
                                               not bool(c.get("goal_archive") or c.get("mr_goal_archive"))),
                                    ours, c.get("noted_decisions", []))
        save(c)
        if published_key != key and row.get("_scan_id"):
            # A prior observer may have claimed the bare key after this scan chose it.
            # Keep the published card pointer in sync with the ownership recheck.
            try:
                snapshot = json.loads((STATE / "last.json").read_text())
                if snapshot.get("scan_id") == row["_scan_id"]:
                    changed = False
                    for published in snapshot.get("rows", []):
                        if published.get("id") == row["id"] and published.get("card") == published_key:
                            published["card"] = key
                            changed = True
                    if changed:
                        tmp = STATE / f"last.card.{uuid.uuid4().hex}.json"
                        marker = STATE / f"scan_id.card.{uuid.uuid4().hex}"
                        try:
                            tmp.write_text(json.dumps(snapshot))
                            tmp.replace(STATE / "last.json")
                            marker.write_text(f"{row['_scan_id']} {(STATE / 'last.json').stat().st_ino}")
                            marker.replace(STATE / "scan_id")
                        finally:
                            tmp.unlink(missing_ok=True)
                            marker.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass  # the card is saved; a later scan can repair an unavailable snapshot
        return c, (c["transitions"][0]["from"], stage) if moved else None


def brief(c):
    """The handoff block a dispatch prompt carries. Bounded: BRIEF_CAP chars."""
    clip = lambda s, n: s if len(s) <= n else s[:n - 1] + "…"
    lines = lambda xs, n: clip("".join(f"\n  - {x}" for x in xs) or " (none)", n)
    files = c["touched_files"]
    text = (f"WORKSPACE STATE (handoff card {path(c['ws'])}). Keep it current as you learn: "
            f"`python3 {shlex.quote(str(Path(__file__).resolve()))} set {shlex.quote(c['ws'])} --exact --if-situation {situation_token(c)} --hypothesis \"...\" --decision \"...\" --failed \"...\" --risk \"...\"`.\n"
            f"Goal contract: {clip(c['goal_contract'] or '(none)', 450)}\n"
            f"Last stage: {clip(c['last_stage'] or '(not scanned)', 120)}\n"
            f"Open risks:{lines(c['open_risks'][:8], 450)}\n"
            f"Touched files: {clip(', '.join(files[:25]) + (f' (+{len(files) - 25} more)' if len(files) > 25 else '') if files else '(none)', 300)}\n"
            f"Current hypothesis: {clip(c['current_hypothesis'] or '(none)', 350)}\n"
            f"Decisions made:{lines(c['decisions_made'][:8], 450)}\n"
            f"Failed approaches (don't repeat them):{lines(c['failed_approaches'][:6], 450)}")
    return text if len(text) <= BRIEF_CAP else text[:BRIEF_CAP - 1] + "…"


def summary(c, n=420):
    """One line for the chat's context."""
    parts = [c["last_stage"] and f"stage {c['last_stage']}", c["goal_contract"] and f"goal: {c['goal_contract'][:120]}",
             c["current_hypothesis"] and f"hypothesis: {c['current_hypothesis'][:140]}",
             c["failed_approaches"] and f"failed: {'; '.join(c['failed_approaches'][:2])[:140]}",
             c["open_risks"] and f"risks: {'; '.join(c['open_risks'][:3])[:140]}",
             c["decisions_made"] and f"decided: {c['decisions_made'][0][:120]}",
             c["touched_files"] and f"files: {', '.join(c['touched_files'][:6])}"]
    s = " · ".join(p for p in parts if p)
    return s if len(s) <= n else s[:n - 1] + "…"


# ---- the index


def tokens(text):
    source = (text or "").lower()
    return [t for t in TOKEN_RE.findall(re.sub(r"[-/.:#!]", " ", source) + " " + source.replace("-", "_"))
            if t not in STOP] + re.findall(r"(?<!\w)[#!](\d)(?!\d)", source)


def text_of(c):
    return " ".join([c["ws"], c.get("display_name") or "", c.get("project") or "", c.get("mr_ref") or "", c["goal_contract"], c["current_hypothesis"], c["last_stage"],
                     *c["decisions_made"], *c["failed_approaches"], *c["open_risks"], *c["touched_files"]])


@contextlib.contextmanager
def index(db_path=None):
    DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path or DIR / "index.sqlite", timeout=10)
    try:
        db.executescript("CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, kind TEXT, len INTEGER, stamp TEXT);"
                         "CREATE TABLE IF NOT EXISTS postings (term TEXT, doc TEXT, tf INTEGER, PRIMARY KEY (term, doc)) WITHOUT ROWID;"
                         "CREATE INDEX IF NOT EXISTS postings_doc ON postings (doc);"
                         "CREATE INDEX IF NOT EXISTS docs_kind ON docs (kind);"  # sync_memory reads the notes only
                         "CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, v INTEGER);")
        if not db.execute("SELECT 1 FROM stats WHERE k = 'n'").fetchone():  # an index from before the counters: counted once
            n, total = db.execute("SELECT count(*), coalesce(sum(len), 0) FROM docs").fetchone()
            db.executemany("INSERT OR REPLACE INTO stats VALUES (?, ?)", [("n", n), ("total", total)])
            db.commit()
        with db:
            yield db
    finally:
        db.close()


def bump(db, dn, dlen):
    """Keep the doc count and total length so a query never aggregates over every doc."""
    db.execute("UPDATE stats SET v = v + ? WHERE k = 'n'", (dn,))
    db.execute("UPDATE stats SET v = v + ? WHERE k = 'total'", (dlen,))


def stats(db):
    d = dict(db.execute("SELECT k, v FROM stats").fetchall())
    return d.get("n", 0), d.get("total", 0)


def put(db, doc, kind, text, stamp=""):
    drop(db, doc)
    toks = tokens(text)
    tf = {}
    for t in toks:
        tf[t] = tf.get(t, 0) + 1
    db.executemany("INSERT INTO postings VALUES (?, ?, ?)", [(t, doc, n) for t, n in tf.items()])
    db.execute("INSERT INTO docs VALUES (?, ?, ?, ?)", (doc, kind, len(toks), stamp))
    bump(db, 1, len(toks))


def drop(db, doc):
    old = db.execute("SELECT len FROM docs WHERE id = ?", (doc,)).fetchone()  # O(log N): the primary key
    db.execute("DELETE FROM postings WHERE doc = ?", (doc,))
    if old:
        db.execute("DELETE FROM docs WHERE id = ?", (doc,))
        bump(db, -1, -old[0])


def note_text(f):
    try:
        return f.read_text(errors="replace")
    except OSError:
        return ""


def sync_memory(db):
    """Index edited, new and deleted memory notes. O(M) stats."""
    have = dict(db.execute("SELECT id, stamp FROM docs WHERE kind = 'note'").fetchall())
    seen = set()
    for f in sorted(MEMORY.glob("*.md")) if MEMORY.is_dir() else []:
        if f.name == "MEMORY.md":
            continue
        doc, st = "note:" + f.name, f.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
        seen.add(doc)
        if have.get(doc) != stamp:
            put(db, doc, "note", f.stem.replace("-", " ") + " " + note_text(f), stamp)
    for doc in set(have) - seen:
        drop(db, doc)


def reindex():
    fresh = DIR / f"index.{uuid.uuid4().hex}.sqlite"
    try:
        with index(fresh) as db:
            for f in sorted(DIR.glob("*.json")):
                with contextlib.suppress(OSError, ValueError, KeyError):
                    c = blank(f.stem) | json.loads(f.read_text())
                    put(db, "card:" + c["ws"], "card", text_of(c))
            sync_memory(db)
            count = stats(db)[0]
        os.replace(fresh, DIR / "index.sqlite")
        return count
    finally:
        fresh.unlink(missing_ok=True)


def search(db, query, k=5, kinds=("card", "note"), allow=None):
    """[(score, doc id)], best first. allow: card names that may be returned (the chat's window); notes always may."""
    terms = list(dict.fromkeys(tokens(query)))[:32]
    n, total = stats(db)  # kept counters: no aggregate over every doc
    if not terms or not n:
        return []
    avg = total / n or 1
    df = {t: db.execute("SELECT count(*) FROM postings WHERE term = ?", (t,)).fetchone()[0] for t in terms}  # O(log P + p_t) each
    useful = ([t for t in terms if df[t]] if allow is not None else
              [t for t in terms if df[t] and df[t] <= n / 2] or [t for t in terms if df[t]])  # scoped matches come first
    scores, lens = {}, {}
    for t in useful:
        idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
        for doc, tf, ln, kind in db.execute("SELECT p.doc, p.tf, d.len, d.kind FROM postings p JOIN docs d ON d.id = p.doc WHERE p.term = ?", (t,)):
            if kind not in kinds or allow is not None and kind == "card" and doc[5:] not in allow:
                continue
            scores[doc] = scores.get(doc, 0) + idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * ln / avg))  # BM25, k1=1.2 b=0.75
    return heapq.nlargest(k, ((s, d) for d, s in scores.items()))


def top(query, k=5, kinds=("card", "note"), allow=None):
    """The K most relevant cards and memory notes: [{id, kind, name, score, text}], text one line each."""
    with index_write_lock():
        marker = DIR / ".index-dirty"
        if not (DIR / "index.sqlite").exists() and any(DIR.glob("*.json")):
            marker.write_text(uuid.uuid4().hex)  # survive a kill after schema creation but before rebuilding
        try:
            with index() as db:
                dirty = marker.read_text() if marker.exists() else None
                rebuild = dirty is not None or not db.execute("SELECT 1 FROM docs WHERE kind = 'card' LIMIT 1").fetchone() and any(DIR.glob("*.json"))
        except sqlite3.DatabaseError:
            dirty = marker.read_text() if marker.exists() else None
            rebuild = True
        if rebuild:
            reindex()  # cards from before the index (or a deleted index): built once
            if dirty is not None and marker.exists() and marker.read_text() == dirty:
                marker.unlink()
        with index() as db:
            if "note" in kinds:
                sync_memory(db)
            limit = k * 5 if isinstance(allow, dict) else k
            cards = {}
            while True:
                candidates = search(db, query, limit, kinds, allow)
                hits = []
                for score, doc in candidates:
                    if doc.startswith("card:") and isinstance(allow, dict):
                        name = doc[5:]
                        if name not in cards:
                            cards[name] = load(name)
                        card = cards[name]
                        identity = (card.get("forge") or "", card.get("project") or "", card.get("mr_ref") or "",
                                    card.get("sha") or "", card.get("goal_source") or "", card.get("target_branch") or "",
                                    card.get("id") or "", card.get("branch") or "", card.get("last_stage") or "")
                        if identity != allow.get(name):
                            continue
                    hits.append((score, doc))
                if not isinstance(allow, dict) or len(hits) >= k or len(candidates) < limit:
                    break
                limit *= 2
    hits = [h for h in hits if h[0] >= FLOOR * hits[0][0]] if hits else []
    out = []
    for score, doc in hits:
        kind, name = doc.split(":", 1)
        if kind == "card":
            card = cards.get(name) or load(name)
            text = summary(card)
            display_name = card.get("display_name") or name.split("@id-")[0]
        else:
            body = note_text(MEMORY / name)
            desc = re.search(r"^description:\s*(.+)$", body, re.M)
            rest = re.sub(r"\A---.*?---\s*", "", body, flags=re.S)
            text = ((desc[1].strip("\"' ") + " — ") if desc else "") + " ".join(rest.split())
            text = text[:420]
        out.append({"id": doc, "kind": kind, "name": name, "score": round(score, 3), "text": text,
                    **({"display_name": display_name} if kind == "card" else {})})
    return out[:k]


def digest(text):
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def situation_token(card):
    stage = (card.get("last_stage") or "").rsplit("/", 1)[0]
    return digest(json.dumps([card.get("forge"), card.get("project"), card.get("branch"), card.get("mr_ref"), card.get("target_branch"),
                              card.get("assignment_goal_source", card.get("goal_source")), stage, card.get("handoff_epoch")]))


def fork_token(card):
    """A verdict is bound to the observed CI and thread evidence as well as the assignment."""
    return digest(json.dumps([situation_token(card), card.get("situation_source")]))


# ---- CLI


def current_key(name, allow_ambiguous=False):
    """A manual show/brief by display name follows the current scan, not a replaced worktree's old card."""
    try:
        rows = json.loads((STATE / "last.json").read_text())["rows"]
    except (OSError, ValueError, KeyError):
        return name
    matches = [r for r in rows if r["name"] == name]
    if len(matches) > 1:
        raise SystemExit(f"{name!r} is ambiguous; use the exact card key from a dispatch brief")
    return keyed(matches)[0]["card"] if matches else name


def observe_all(scan=None):
    """Refresh from live facts so an old last.json cannot undo a generated goal."""
    if scan is None:
        import greenlight
        scan = greenlight.scan
    rows = keyed(scan(save=True)["rows"])
    recs = decision_records()
    moves = []
    for r in rows:
        _, moved = observe(r, contract(r), recs=recs)
        if moved:
            moves.append((r["name"], moved))
    return moves


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    opt = lambda k: args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else None
    many = lambda k: [args[i + 1] for i, a in enumerate(args[:-1]) if a == k]
    if cmd == "show" and len(args) > 1:
        c = load(args[1] if "--exact" in args else current_key(args[1]))
        print(json.dumps(c, indent=1) if "--json" in args else brief(c))
    elif cmd == "brief" and len(args) > 1:
        print(brief(load(args[1] if "--exact" in args else current_key(args[1]))))
    elif cmd == "set" and len(args) > 1:
        c = update(args[1] if "--exact" in args else current_key(args[1]),
                   hypothesis=opt("--hypothesis"), decision=many("--decision"), failed=many("--failed"),
                   risk=many("--risk"), clear_risks="--clear-risks" in args, if_situation=opt("--if-situation"))
        print(brief(c))
    elif cmd == "top" and len(args) > 1:
        k = int(opt("-k") or 5)
        q = " ".join(a for i, a in enumerate(args[1:], 1) if a not in ("-k", "--json") and args[i - 1] != "-k")
        hits = top(q, k)
        print(json.dumps(hits, indent=1) if "--json" in args else "\n".join(f"{h['score']:6.2f} {h['kind']} {h['name']}: {h['text']}" for h in hits))
    elif cmd == "observe":
        for name, moved in observe_all():
            print(f"{name}: {moved[0] or '(new)'} → {moved[1]}")
    elif cmd == "reindex":
        with index_write_lock():
            marker = DIR / ".index-dirty"
            token = uuid.uuid4().hex
            marker.write_text(token)
            count = reindex()
            if marker.exists() and marker.read_text() == token:
                marker.unlink()
        print(f"indexed {count} docs")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
