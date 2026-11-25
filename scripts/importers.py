"""Read-only, one-shot importers: bring another ADE's repos and goals into the native engine (#195).

    python3 importers.py detect                         which sources are here, JSON
    python3 importers.py preview [--from S]... [--path DIR]...   what an import would add, JSON (changes nothing)
    python3 importers.py apply   [--from S]... [--path DIR]...   do it, JSON summary

Sources: orca, t3, cmux, herdr (each read through its own read-only listing: the same `worktrees()` the old live
drivers used, never a write), and `folder`, the git repos under --path (any ADE that keeps plain checkouts, Rex
and the terminals included). With no --from, every detected source.

What an import writes, and only there: repos.json (each repo's main checkout, once) and goals.json (a workspace's
goal, only where greenlight has none yet: an import never overwrites a goal). Nothing in the other ADE changes, so
an import can run again any time (Settings → Import, `greenlight.py import`), and a source that fails to answer
is reported and skipped.
"""
import json
import os
import re
import shutil
import sys
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
    repos, goals = [], {}
    for w in rows:
        path = w.get("path") or ""
        if not path or not os.path.isdir(path) or w.get("isArchived"):
            continue  # another host's, or gone
        if w.get("repoId") and os.path.isdir(w["repoId"]):
            repos.append(w["repoId"])
        goal = (w.get("comment") or "").strip()
        if goal and not STATUS_LINE_RE.search(goal):
            goals[path] = goal
    if name == "orca":  # Orca knows repos with no worktree open, too
        repos += [r["path"] for r in ade.get("orca").repos().get("repos") or [] if r.get("path") and os.path.isdir(r["path"])]
    return repos, goals


def from_folder(root):
    """The main checkouts under root (a .git directory, not a linked worktree's .git file), MAX_DEPTH deep."""
    root, out = Path(root).expanduser(), []
    if not root.is_dir():
        raise RuntimeError(f"not a folder: {root}")
    for here, dirs, _ in os.walk(root):
        depth = len(Path(here).relative_to(root).parts)
        if (Path(here) / ".git").is_dir():
            out.append(str(Path(here).resolve()))
            dirs[:] = []  # a repo's own subfolders aren't other repos (submodules are the repo's)
            continue
        dirs[:] = [] if depth >= MAX_DEPTH else [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
    return out, {}


def gather(sources, paths=()):
    """{source: {"repos", "goals"} or {"error"}}: what each source lists, read-only."""
    out = {}
    for s in sources:
        try:
            repos: list = []
            goals: dict = {}
            if s == "folder":
                for p in paths:
                    r, g = from_folder(p)
                    repos += r
                    goals |= g
            else:
                repos, goals = from_backend(s)
            out[s] = {"repos": list(dict.fromkeys(repos)), "goals": goals}
        except Exception as e:  # noqa: BLE001 - an ADE that isn't running, or changed its CLI: report it, import the rest
            out[s] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            diag.event("import", source=s, err=type(e).__name__, msg_class=diag.classify(str(e)))
    return out


def plan(found):
    """What an import adds: new repos (by main checkout) and goals for paths with none, plus what it leaves alone."""
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
            "sources": {s: ({"error": f["error"]} if "error" in f else {"repos": len(f["repos"]), "goals": len(f["goals"])})
                        for s, f in found.items()}}


def apply(p):
    for r in p["repos"]:
        ade_basement.remember(r)
    for path, g in p["goals"].items():
        if not ade_common.goals().get(path):  # re-checked: a goal set since the preview stands
            ade_common.set_goal(path, g)
    diag.event("import", op="apply", n_repos=len(p["repos"]), n_goals=len(p["goals"]), n_kept=len(p["kept"]))
    return p


def run(argv):
    op = argv[0] if argv else "preview"
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
