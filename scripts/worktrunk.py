"""Worktrunk worktree operations for Greenlight's native workspace engine.

The `wt` CLI owns creation and removal. Greenlight keeps goals and agent sessions
itself; importing another workspace app remains a separate, read-only operation.
"""
import json
import os
import subprocess


def run(repo, *args, timeout=120):
    """Run Worktrunk in a known repo, with no shell or interactive prompt."""
    binary = os.environ.get("WORKTRUNK_BIN") or "wt"
    try:
        p = subprocess.run([binary, "-C", repo, "-y", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("Worktrunk (`wt`) is required to create and remove workspaces") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"wt {args[0]} timed out") from None
    if p.returncode:
        raise RuntimeError(f"wt {args[0]}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def items(repo):
    """Worktrees from Worktrunk's schema 2 JSON, excluding branch-only rows."""
    try:
        doc = json.loads(run(repo, "--config-set", "list.json-schema=2", "list", "--format=json", timeout=60))
    except ValueError:
        raise RuntimeError("wt list returned invalid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != 2 or not isinstance(doc.get("items"), list):
        raise RuntimeError("wt list requires Worktrunk JSON schema 2")
    return [item for item in doc["items"] if isinstance(item, dict) and
            isinstance(item.get("worktree"), dict) and isinstance(item["worktree"].get("path"), str)]


def create(repo, branch, base=None):
    """Create a branch and worktree; return its actual path from Worktrunk's list."""
    if not branch or branch.startswith("-") or base and base.startswith("-"):
        raise RuntimeError("wt switch: invalid branch or base ref")
    args = ["switch", "--create", "--no-cd", "--no-hooks", "--format=json", branch]
    if base:
        args.extend(["--base", base])
    run(repo, *args)
    paths = [item["worktree"]["path"] for item in items(repo) if item.get("branch") == branch]
    if len(paths) != 1:
        raise RuntimeError(f"wt switch created {branch!r}, but wt list did not identify one worktree")
    return paths[0]


def remove(repo, path, force=False):
    """Remove the worktree while preserving its branch for later inspection or recovery."""
    args = ["remove", "--foreground", "--no-hooks", "--no-delete-branch"]
    if force:
        args.append("--force")
    run(repo, *args, path, timeout=300)
