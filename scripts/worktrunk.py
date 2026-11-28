"""Worktrunk worktree operations for Greenlight's native workspace engine.

The `wt` CLI owns creation and removal. Greenlight keeps goals and agent sessions
itself; importing another workspace app remains a separate, read-only operation.
"""
import json
import os
import subprocess


_VALUE_FLAGS = frozenset({"-C", "--config", "--config-set"})
_SHORTCUTS = frozenset({"@", "^", "-"})


def _label(args):
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in _VALUE_FLAGS:
            skip = True
            continue
        if isinstance(arg, str) and arg and not arg.startswith("-"):
            return f"wt {arg}"
    return "wt"


def _valid_ref(ref):
    if not isinstance(ref, str) or not ref:
        return False
    if ref.startswith("-") or ref in _SHORTCUTS or ":" in ref or ".." in ref:
        return False
    return not any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in ref)


def run(repo, *args, timeout=120):
    """Run Worktrunk in a known repo, with no shell or interactive prompt."""
    if not isinstance(repo, str) or not repo or repo.startswith("-"):
        raise RuntimeError("wt: invalid repo path")
    binary = os.environ.get("WORKTRUNK_BIN") or "wt"
    label = _label(args)
    try:
        p = subprocess.run([binary, "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("Worktrunk (`wt`) is required to create and remove workspaces") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} timed out") from None
    if p.returncode:
        raise RuntimeError(f"{label}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def items(repo):
    """Worktrees from Worktrunk's schema 2 JSON, excluding branch-only rows."""
    try:
        doc = json.loads(run(
            repo,
            "--config-set", "list.json-schema=2",
            "--config-set", "list.full=false",
            "--config-set", "list.branches=false",
            "--config-set", "list.remotes=false",
            "list", "--format=json",
            timeout=60,
        ))
    except ValueError:
        raise RuntimeError("wt list returned invalid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != 2 or not isinstance(doc.get("items"), list):
        raise RuntimeError("wt list requires Worktrunk JSON schema 2")
    return [item for item in doc["items"] if isinstance(item, dict) and
            isinstance(item.get("worktree"), dict) and isinstance(item["worktree"].get("path"), str)]


def _switch_path(repo, branch, stdout):
    try:
        doc = json.loads(stdout)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("path"), str) and doc["path"]:
        return doc["path"]
    paths = [item["worktree"]["path"] for item in items(repo) if item.get("branch") == branch]
    if len(paths) != 1:
        raise RuntimeError(f"wt switch created {branch!r}, but wt list did not identify one worktree")
    return paths[0]


def create(repo, branch, base=None):
    """Create a branch and worktree; return its actual path from Worktrunk."""
    if not _valid_ref(branch) or (base is not None and not _valid_ref(base)):
        raise RuntimeError("wt switch: invalid branch or base ref")
    args = ["switch", "--create", "--no-cd", "--no-hooks", "--format=json", branch]
    if base is not None:
        args.extend(["--base", base])
    return _switch_path(repo, branch, run(repo, *args))


def attach(repo, branch):
    """Attach a worktree to an existing local branch without modifying its commits."""
    if not _valid_ref(branch):
        raise RuntimeError("wt switch: invalid branch or base ref")
    return _switch_path(repo, branch, run(repo, "switch", "--no-cd", "--no-hooks", "--format=json", branch))


def remove(repo, path, force=False):
    """Remove the worktree while preserving its branch for later inspection or recovery."""
    if not isinstance(path, str) or not os.path.isabs(path) or path.startswith("-"):
        raise RuntimeError("wt remove: invalid worktree path")
    args = ["remove", "--foreground", "--no-hooks", "--no-delete-branch"]
    if force:
        args.append("--force")
    run(repo, *args, "--", path, timeout=300)
