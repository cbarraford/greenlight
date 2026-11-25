"""Read-only, one-shot importers: bring another ADE's repos and goals into the native engine (#195).

    python3 importers.py detect                         which sources are here, JSON
    python3 importers.py preview [--from S]... [--path DIR]...   what an import would add, JSON (changes nothing)
    python3 importers.py apply   [--from S]... [--path DIR]...   do it, JSON summary

Sources: orca, t3, cmux, herdr (each read through its own read-only listing: the same `worktrees()` the old live
drivers used, never a write; goals come from Orca's comments and cmux's descriptions: T3 and herdr have none of their own), and `folder`, the git repos under --path (any ADE that keeps plain checkouts, Rex
and the terminals included). With no --from, every detected source.

What an import writes, and only there: repos.json (each repo's main checkout, once) and goals.json (a workspace's
goal, only where greenlight has none yet: an import never overwrites a goal). Nothing in the other ADE changes, so
an import can run again any time (Settings → Import, `greenlight.py import`), and a source that fails to answer
is reported and skipped.
"""
import contextlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ade  # noqa: E402
import ade_basement  # noqa: E402
import ade_common  # noqa: E402
import diag  # noqa: E402

SOURCES = ("orca", "t3", "cmux", "herdr", "folder")
STATUS_LINE_RE = re.compile(r"dispatched \d{4}-\d\d-\d\d")  # an old status line, not a goal (greenlight.STATUS_LINE_RE)
MAX_DEPTH = 3  # folder scan: ~/code/<group>/<repo>
SKIP_DIRS = {"node_modules", ".venv", "venv", "vendor", "target", "build", "dist", "Library", ".cache", ".Trash"}


def detect():
    """{source: True} for the ADEs installed here (a CLI on PATH or its state on disk). folder is always there."""
    home = Path.home()
    t3_home = Path(os.environ.get("T3CODE_HOME") or home / ".t3")
    return {
        # bare `orca` on Linux is the GNOME screen reader: only Orca's own names count there (greenlight.orca_cli)
        "orca": bool(os.environ.get("ORCA_CLI_COMMAND") or shutil.which("orca-ide")
                     or (home / ".config/orca/linux-orca-cli-shim/orca").exists()
                     or (sys.platform == "darwin" and shutil.which("orca"))),
        "t3": (t3_home / "userdata" / "statev2.sqlite").exists(),
        "cmux": bool(shutil.which("cmux")),
        "herdr": bool(shutil.which("herdr")),
        "folder": True,
    }


def from_backend(name):
    """(repos, goals) an ADE lists: each worktree's repo, and its goal (Orca's comment, cmux's description, …)."""
    rows = ade.get(name).worktrees()
    repos: list = []
    goals: dict = {}
    for w in rows:
        path = w.get("path") or ""
        if w.get("hostId") not in (None, "", "local"):
            continue  # another paired machine's: the same path here is something else
        if not path or not os.path.isdir(path) or w.get("isArchived"):
            continue  # gone
        top = ade_common.git(path, "rev-parse", "--show-toplevel")
        if not top:
            continue  # not a git checkout (a cmux workspace sitting in ~): no workspace would ever show its goal
        repos.append(top)  # plan() keeps its main checkout (an ADE's repoId may be an opaque id)
        goal = (w.get("comment") or "").strip()
        if goal and not STATUS_LINE_RE.search(goal):
            goals.setdefault(os.path.realpath(top), goal)  # keyed as the native engine keys it: the worktree's root
    if name == "orca":  # Orca knows repos with no worktree open, too: only those its project setups put on this machine
        import greenlight  # here: greenlight imports ade, which this module imports
        setups = greenlight.project_setups(tries=1)
        local = {s["path"] for s in setups or [] if greenlight.is_local(s) and isinstance(s.get("path"), str)}
        repos += [r["path"] for r in ade.get("orca").repos().get("repos") or []
                  if isinstance(r, dict) and r.get("path") in local and os.path.isdir(r["path"])]
    return repos, goals


def from_folder(root, skipped=None):
    """The main checkouts under root (a .git directory, not a linked worktree's .git file), MAX_DEPTH deep."""
    root, out = Path(root).expanduser(), []
    if not root.is_dir():
        raise RuntimeError(f"not a folder: {root}")
    try:  # list it for real: os.access doesn't see macOS privacy (~/Documents without the grant); os.walk would find nothing
        with os.scandir(root) as it:
            next(it, None)
    except OSError as e:
        raise RuntimeError(f"can't open: {root}") from e
    # a subfolder that can't be opened is named in skipped (the rest still imports), never silently missed
    for here, dirs, _ in os.walk(root, onerror=lambda e: skipped is not None and skipped.append(f"can't open: {e.filename}")):
        depth = len(Path(here).relative_to(root).parts)
        if (Path(here) / ".git").is_dir():
            out.append(str(Path(here).resolve()))  # a folder brings repos only: plain checkouts carry no goals
            if depth:  # a repo's own subfolders aren't other repos (submodules are the repo's); but the folder you
                dirs[:] = []  # named may be a repo itself (dotfiles in ~) with your checkouts below it
                continue
        dirs[:] = [] if depth >= MAX_DEPTH else [d for d in dirs if not d.startswith(".")  # never ~/.nvm, ~/.oh-my-zsh
                                                 and (d not in SKIP_DIRS or (Path(here) / d / ".git").is_dir())]  # a repo named build
    return out


def gather(sources, paths=()):
    """{source: {"repos", "goals"} or {"error"}}: what each source lists, read-only."""
    out = {}
    for s in sources:
        try:
            repos: list = []
            goals: dict = {}
            bad: list = []
            if s == "folder":
                for p in paths:  # each on its own: a typo in one never drops the others
                    try:
                        repos += from_folder(p, bad)
                    except (OSError, RuntimeError) as e:
                        bad.append(str(e)[:200])
            else:
                repos, goals = from_backend(s)
            bad = list(dict.fromkeys(bad))  # overlapping folders name a skipped one once; and only the first few
            err = "; ".join(bad[:3]) + (f"; and {len(bad) - 3} more" if len(bad) > 3 else "")
            out[s] = {"repos": list(dict.fromkeys(repos)), "goals": goals, **({"error": err} if bad else {})}
        except Exception as e:  # noqa: BLE001 - an ADE that isn't running, or changed its CLI: report it, import the rest
            out[s] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            diag.event("import", source=s, err=type(e).__name__, msg_class=diag.classify(str(e)))
    return out


def plan(found):
    """What an import adds: new repos (by main checkout) and goals for paths with none, plus what it leaves alone.
    RuntimeError when repos.json or goals.json exists but can't be read (it would look empty)."""
    readable(ade_basement.REPOS, list)
    readable(ade_common.GOALS, dict)
    have_repos, have_goals = set(ade_basement.saved()), ade_common.goals()
    repos, goals, kept = [], {}, {}
    for s, f in found.items():
        for r in f.get("repos") or []:
            main = ade_basement.main_checkout(r)
            if main and main not in have_repos and main not in repos:
                repos.append(main)
        for path, g in (f.get("goals") or {}).items():
            if have_goals.get(path):
                if have_goals[path] != g:
                    kept[path] = {"kept": have_goals[path], "from": s}  # never overwritten
            elif path not in goals:
                goals[path] = g
    return {"repos": repos, "goals": goals, "kept": kept,
            "sources": {s: ({"repos": len(f.get("repos") or []), "goals": len(f.get("goals") or {}),
                             **({"error": f["error"]} if "error" in f else {})})
                        for s, f in found.items()}}


def readable(f, kind):
    """Raise when f exists but isn't a JSON kind: its reader would see it as empty, and a write would replace it."""
    try:
        ok = isinstance(json.loads(f.read_text()), kind)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise RuntimeError(f"{f.name} can't be read: fix it or move it aside before importing (nothing was written)")


def apply(p):
    """Write the plan; what was written (a goal set since the preview stands and isn't counted)."""
    readable(ade_basement.REPOS, list)
    readable(ade_common.GOALS, dict)
    for r in p["repos"]:
        ade_basement.remember(r)
    goals = {}
    for path, g in p["goals"].items():
        if not ade_common.goals().get(path):  # re-checked: a goal set since the preview stands
            ade_common.set_goal(path, g)
            goals[path] = g
    diag.event("import", op="apply", n_repos=len(p["repos"]), n_goals=len(goals), n_kept=len(p["kept"]))
    return {**p, "goals": goals}


FIRST_RUN = "imported.json"  # in GREENLIGHT_HOME: the one automatic import has happened (what it found)


RETRY_SECS, RETRIES = 3600, 5  # a source that failed (its app not running) is tried again hourly, a few times


def first_run():
    """Once per install, the first time the native engine lists workspaces: import every detected ADE (not folders),
    so someone moving from Orca, T3, cmux or herdr keeps their repos and goals. A source that couldn't be read is
    tried again later (`pending`), a few times. Never raises; the marker (imported.json) makes it once."""
    if not ade.is_native():
        return None
    marker = ade_basement.HOME / FIRST_RUN
    if _due(marker) is None:
        return None  # the cheap answer, most of the time: nothing to do
    with _claimed(marker.with_suffix(".lock")) as mine:
        if not mine:
            return None  # another listing is importing right now
        due = _due(marker)  # again, holding the claim: another listing may have just done it
        return None if due is None else _first_import(marker, *due)


def _due(marker):
    """(marker contents or None, sources to import) when an import or a retry is due now, else None."""
    try:
        m = json.loads(marker.read_text()) if marker.exists() else None
    except (OSError, ValueError):
        return None  # unreadable: done (never import twice on a guess)
    if m is None:
        if ade_basement.REPOS.exists():  # already set up on native (repos added or removed by hand): never re-add them
            return None, []
        return None, [s for s, ok in detect().items() if ok and s != "folder"]
    num = lambda k, d: v if isinstance(m, dict) and isinstance(v := m.get(k, d), (int, float)) else d  # noqa: E731
    pending = m.get("pending") if isinstance(m, dict) and isinstance(m.get("pending"), list) else None
    if not pending or num("attempts", RETRIES) >= RETRIES or time.time() - num("ts", time.time()) < RETRY_SECS:
        return None
    # the import writes its marker after its files: anything newer is the user's own work on native, which a retry
    # would undo (re-adding a repo or goal they removed)
    if any(f.exists() and f.stat().st_mtime > marker.stat().st_mtime for f in (ade_basement.REPOS, ade_common.GOALS)):
        return None
    return m, [s for s in pending if s in SOURCES and s != "folder"]


@contextlib.contextmanager
def _claimed(lock):
    """Yield True when this process holds lock (an O_EXCL file; a stale one, over 15 min old, is taken over)."""
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 900:
            lock.unlink(missing_ok=True)
        lock.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        yield False
        return
    try:
        yield True
    finally:
        lock.unlink(missing_ok=True)


def _first_import(marker, m, sources):
    try:
        p = apply(plan(gather(sources)))
        res = {"repos": len(p["repos"]), "goals": len(p["goals"]), "sources": p["sources"],
               "pending": [s for s, v in p["sources"].items() if v.get("error")]}
    except Exception as e:  # noqa: BLE001 - the import is a convenience: listing must go on
        res = {"error": type(e).__name__, "pending": sources}
    a = m.get("attempts") if isinstance(m, dict) else None
    res["attempts"] = (a if isinstance(a, int) else 0) + 1
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"ts": int(time.time()), **res}, indent=1))
    return res


def run(argv):
    op = argv[0] if argv and not argv[0].startswith("-") else "preview"  # the verb is optional
    if argv and argv[0] == op:
        argv = argv[1:]
    if op == "detect":
        return detect()
    if op not in ("preview", "apply"):
        raise SystemExit("usage: importers.py detect | preview|apply [--from orca|t3|cmux|herdr|folder]... [--path DIR]...")
    picked = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--from"]
    paths = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--path"]
    if bad := [s for s in picked if s not in SOURCES]:
        raise SystemExit(f"unknown source {bad[0]!r}: one of {', '.join(SOURCES)}")
    if not picked:
        picked = [s for s, ok in detect().items() if ok and (s != "folder" or paths)]
    elif "folder" in picked and not paths:
        raise SystemExit("--from folder needs --path DIR")
    p = plan(gather(picked, paths))
    return apply(p) if op == "apply" else p


if __name__ == "__main__":
    diag.install("importers")
    print(json.dumps(run(sys.argv[1:]), indent=1))
