"""Helpers the non-Orca backends share: git facts Orca reports itself, orca flag parsing, Claude draft
detection, and goal storage for ADEs with no comment/description field."""
import json
import os
import re
import subprocess
from pathlib import Path

GOALS = Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight") / "goals.json"  # {workspace path: goal}
MENU_ITEM_RE = re.compile(r"❯\s*\d\.")
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
    path = str(Path(repo).parent / f"{Path(repo).name}-{name.replace('/', '-')}")
    p = subprocess.run(["git", "-C", repo, "worktree", "add", "-b", name, path, *([base] if base else [])],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"git worktree add: {p.stderr.strip()[:300]}")
    return path


def worktree_remove(ws):
    if not ws["isMainWorktree"]:  # git refuses a dirty tree without --force, which we never pass
        p = subprocess.run(["git", "-C", ws["repoId"], "worktree", "remove", ws["path"]], capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"git worktree remove {ws['path']}: {p.stderr.strip()[:300]}")


def flags(args):
    out, i = {}, 0
    while i < len(args):
        if args[i].startswith("--"):
            val = i + 1 < len(args) and not args[i + 1].startswith("--")
            out[args[i][2:]] = args[i + 1] if val else True
            i += 2 if val else 1
        else:
            i += 1
    return out


def draft(lines):
    # Claude's input box is the last `❯` line; text after it is a draft the user hasn't sent (menu items aren't).
    # ponytail: Claude-only; a codex draft reads as empty until its prompt glyph is added here
    box = [x.strip() for x in lines if x.strip().startswith("❯")]
    return box[-1][1:].strip() if box and not MENU_ITEM_RE.match(box[-1]) else ""


def goals():
    try:
        return json.loads(GOALS.read_text())
    except (OSError, ValueError):
        return {}


def set_goal(path, goal):
    g = goals()
    g.pop(path, None) if not goal else g.update({path: goal})
    GOALS.parent.mkdir(parents=True, exist_ok=True)
    GOALS.write_text(json.dumps(g, indent=1))


def repo_list(worktrees):
    """Orca's `repo list` shape: one entry per repo, named by its folder, keyed by its remote (dash.py's New task)."""
    out = {}
    for w in worktrees:
        if w["repoId"] and w["repoId"] not in out:
            out[w["repoId"]] = {"id": w["repoId"], "path": w["repoId"], "displayName": Path(w["repoId"]).name,
                                "gitRemoteIdentity": {"canonicalKey": (w.get("projectId") or "").removeprefix("git:")}}
    return {"repos": list(out.values())}
