#!/usr/bin/env python3
"""Versioned backend installs for over-the-air updates (docs/UPDATES.md, step 1). JSON on stdout.

    update.py version                   this copy's {version, schema, compat, protocol}
    update.py status                    how the skill is installed (versions, git, copy, none) and what's there
    update.py install SRC [--version V] unpack a backend (a folder or a .tar.gz) into versions/V; doesn't switch
    update.py swap V [--wait SECS]      switch `current` to V under the tick lock, smoke-test it, switch back on failure
    update.py rollback [--wait SECS]    swap to the previous version
    update.py adopt [--wait SECS]       turn a copied install (scripts/sync.sh) into the versioned layout

Layout ($GREENLIGHT_HOME, default ~/.greenlight):
    code/versions/<v>/      one backend per version, each with memory -> ../../memory
    code/current            symlink to versions/<v>, switched with rename(2)
    code/memory/            the notes every version shares
    ~/.claude/skills/greenlight -> code/current   (GREENLIGHT_SKILL overrides the path)

A git checkout is never touched: `status` reports it as managed by git, and `swap`/`adopt` refuse.
Running processes keep the version folder they started in, since every script resolves its own path with realpath.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import diag  # noqa: E402

PROTOCOL = 1  # the app <-> backend command contract; bump when a command's input or output changes incompatibly
SCHEMA = 1  # the $GREENLIGHT_HOME state layout this version writes
COMPAT = (1, 1)  # the state schemas this version can read
ESSENTIAL = ("scripts/greenlight.py", "scripts/update.py", "scripts/diag.py", "SKILL.md")
SKIP = {".git", "memory", "__pycache__", "app", "node_modules", ".pytest_cache"}
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
MAX_EXTRACT_BYTES = 200 << 20
RETIRE_SECS = 24 * 3600  # an old version stays this long after it's replaced: a long automation may still run from it
SMOKE_SECS = 30


class UpdateError(Exception):
    """A refusal with an enum-like reason (it goes into the diag trail)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


def code_dir() -> Path:
    return diag.home() / "code"


def versions_dir() -> Path:
    return code_dir() / "versions"


def current_link() -> Path:
    return code_dir() / "current"


def skill_path() -> Path:
    return Path(os.environ.get("GREENLIGHT_SKILL") or Path.home() / ".claude" / "skills" / "greenlight").expanduser()


def state_file() -> Path:
    return code_dir() / "state.json"


def read_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, p)


def version_info(root: Path | None = None) -> dict[str, Any]:
    """This copy's version and the contracts it speaks. diag.py version prints it."""
    if root is None:
        ver = diag.version()
    else:
        try:
            ver = (root / "VERSION").read_text().strip()
        except OSError:
            ver = "unknown"
    return {"version": ver, "schema": SCHEMA, "compat": list(COMPAT), "protocol": PROTOCOL}


def state_schema() -> int:
    s = read_json(diag.home() / "schema.json", {})
    return s.get("schema", 1) if isinstance(s, dict) and isinstance(s.get("schema"), int) else 1


def stable_script(name: str, here: str | None = None) -> Path:
    """The path a scheduler unit should run: through `current` when this copy is a versioned install (so the next
    swap is picked up), else this file's real path (a git checkout or a copy)."""
    p = Path(os.path.realpath(here or __file__)).parent / name
    with contextlib.suppress(OSError, ValueError):
        if p.is_relative_to(versions_dir().resolve()):
            return current_link() / "scripts" / name
    return p


def managed() -> str:
    """How the skill path is installed: versions (a link into code/), git, copy, or none."""
    s = skill_path()
    if s.is_symlink():
        return "versions" if Path(os.path.realpath(s)).is_relative_to(code_dir().resolve()) else "link"
    if not s.exists():
        return "none"
    return "git" if (s / ".git").exists() else "copy"


def active() -> str | None:
    try:
        return Path(os.readlink(current_link())).name
    except OSError:
        return None


def status() -> dict[str, Any]:
    st = read_json(state_file(), {})
    vs = sorted(p.name for p in versions_dir().iterdir() if p.is_dir() and not p.name.startswith(".")) if versions_dir().is_dir() else []
    return {"managed": managed(), "current": active(), "previous": st.get("previous"), "versions": vs,
            "state_schema": state_schema(), **{k: v for k, v in version_info().items() if k != "version"}}


def _members_ok(tf: tarfile.TarFile, dest: Path) -> None:
    total = 0
    for m in tf.getmembers():
        parts = Path(m.name).parts
        if not m.name or m.name.startswith("/") or ".." in parts:
            raise UpdateError("unsafe_archive", m.name)
        if m.isdev() or m.isfifo() or m.islnk():
            raise UpdateError("unsafe_archive", m.name)
        if m.issym() and not (dest / m.name).parent.joinpath(m.linkname).resolve().is_relative_to(dest):
            raise UpdateError("unsafe_archive", m.name)
        total += m.size if m.isfile() else 0
        if total > MAX_EXTRACT_BYTES:
            raise UpdateError("archive_too_big")


def _unpack(src: Path, dest: Path) -> None:
    """A folder is copied (without .git, memory, app and caches); a .tar.gz is checked, then extracted."""
    if src.is_dir():
        shutil.copytree(src, dest, symlinks=True, ignore=lambda d, names: [n for n in names if n in SKIP])
        return
    dest.mkdir(parents=True)
    with tarfile.open(src, "r:*") as tf:
        _members_ok(tf, dest.resolve())
        tf.extractall(dest, filter="data")  # noqa: S202 - members checked above
    kids = [p for p in dest.iterdir()]
    if len(kids) == 1 and kids[0].is_dir() and not (dest / "SKILL.md").exists():  # a single top folder: lift it
        for p in kids[0].iterdir():
            p.rename(dest / p.name)
        kids[0].rmdir()
    for name in SKIP & {p.name for p in dest.iterdir()}:
        p = dest / name
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()


def _link_memory(root: Path) -> None:
    """root is staged and then renamed to versions/<v>: the link is relative to there, not to where it's made."""
    (code_dir() / "memory").mkdir(parents=True, exist_ok=True)
    (root / "memory").symlink_to(Path("..") / ".." / "memory")


def install(src: Path, version: str | None = None) -> dict[str, Any]:
    if not src.exists():
        raise UpdateError("no_source")
    versions_dir().mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=versions_dir()))
    try:
        root = stage / "v"
        _unpack(src, root)
        missing = [f for f in ESSENTIAL if not (root / f).is_file()]
        if missing:
            raise UpdateError("incomplete", ",".join(missing))
        ver = version
        if not ver and (root / "VERSION").is_file():
            ver = (root / "VERSION").read_text().strip()
        if not ver or not VERSION_RE.fullmatch(ver):
            raise UpdateError("bad_version")
        (root / "VERSION").write_text(ver + "\n")
        _link_memory(root)
        dest = versions_dir() / ver
        if dest.exists():
            return {"ok": True, "version": ver, "installed": "existing"}
        root.rename(dest)
        return {"ok": True, "version": ver, "installed": "new"}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


@contextlib.contextmanager
def drained(wait: float) -> Iterator[None]:
    """Hold the orchestrator's tick lock and the workspace-state lock: no tick is mid-way while `current` moves."""
    home = diag.home()
    (home / "state").mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    with open(home / "orchestrate.lock", "w") as tick, open(home / "state" / ".lock", "w") as st:
        for fh in (tick, st):
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise UpdateError("busy") from None
                    time.sleep(0.5)
        yield


def _point(ver: str) -> None:
    """Switch `current` with one rename, and link the skill path to it when nothing else is there."""
    tmp = code_dir() / f".current.{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    tmp.symlink_to(Path("versions") / ver)
    os.replace(tmp, current_link())
    s = skill_path()
    if not s.exists() and not s.is_symlink():
        s.parent.mkdir(parents=True, exist_ok=True)
        s.symlink_to(current_link())


def smoke(root: Path) -> str | None:
    """None when the version at root runs: its scripts compile, and `diag.py doctor --quick` answers with JSON.
    The checks' own verdicts (auth, disk) are the machine's, not the version's, so they don't fail it."""
    py = [sys.executable, "-I"]
    try:
        c = subprocess.run([*py, "-m", "compileall", "-q", "-x", "__pycache__", str(root / "scripts")],
                           capture_output=True, text=True, timeout=SMOKE_SECS, cwd=root)
        if c.returncode:
            return "compile"
        d = subprocess.run([*py, str(root / "scripts" / "diag.py"), "doctor", "--quick"], capture_output=True, text=True,
                           timeout=SMOKE_SECS, cwd=root, env=os.environ | {"GREENLIGHT_HOME": str(diag.home())})
        return None if isinstance(json.loads(d.stdout).get("checks"), list) else "doctor"
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, ValueError, AttributeError):
        return "doctor"


def _target_info(root: Path) -> dict[str, Any]:
    try:
        p = subprocess.run([sys.executable, "-I", str(root / "scripts" / "update.py"), "version"], capture_output=True,
                           text=True, timeout=SMOKE_SECS, cwd=root)
        info = json.loads(p.stdout)
        lo, hi = info["compat"]
        return {"compat": (int(lo), int(hi)), "schema": int(info["schema"])}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise UpdateError("unreadable_version") from None


def _check_swap(ver: str) -> Path:
    kind = managed()
    if kind not in ("versions", "none"):
        raise UpdateError(f"managed_{kind}")
    if not VERSION_RE.fullmatch(ver):
        raise UpdateError("bad_version")
    root = versions_dir() / ver
    if not all((root / f).is_file() for f in ESSENTIAL):
        raise UpdateError("not_installed")
    lo, hi = _target_info(root)["compat"]
    if not lo <= state_schema() <= hi:
        raise UpdateError("schema")
    return root


def swap(ver: str, wait: float = 120.0) -> dict[str, Any]:
    root = _check_swap(ver)
    prev = active()
    if prev == ver:
        return {"ok": True, "current": ver, "changed": False}
    with drained(wait):
        _point(ver)
    why = smoke(root)
    if why:
        if prev:
            with drained(wait):
                _point(prev)
        else:
            current_link().unlink(missing_ok=True)
        raise UpdateError("smoke_failed", why)
    st = read_json(state_file(), {})
    retired = {**st.get("retired", {}), **({prev: int(time.time())} if prev else {})}
    retired.pop(ver, None)
    write_json(state_file(), {"previous": prev, "retired": retired})
    prune()
    return {"ok": True, "current": ver, "previous": prev, "changed": True, "scheduler": reschedule()}


def prune(now: float | None = None) -> list[str]:
    """Remove versions that are neither current nor previous and were replaced over RETIRE_SECS ago."""
    st = read_json(state_file(), {})
    keep = {active(), st.get("previous")}
    now = time.time() if now is None else now
    gone = []
    for name, at in list(st.get("retired", {}).items()):
        if name not in keep and now - at > RETIRE_SECS and VERSION_RE.fullmatch(name):
            shutil.rmtree(versions_dir() / name, ignore_errors=True)
            st["retired"].pop(name)
            gone.append(name)
    if gone:
        write_json(state_file(), st)
    return gone


def scheduler_units() -> list[Path]:
    home = Path.home()
    return [p for p in (home / ".config/systemd/user/greenlight-auto.service", home / "Library/LaunchAgents/greenlight-auto.plist")
            if p.exists()]


def reschedule() -> str:
    """Rerun `auto.py install` from `current` when a scheduler unit runs a version folder or a copy that is gone."""
    target = current_link() / "scripts" / "auto.py"
    for unit in scheduler_units():
        text = unit.read_text(errors="replace")
        if str(target) in text or (str(code_dir()) not in text and str(skill_path()) not in text):
            continue  # already through current, or not ours to move (a git checkout's own unit)
        p = subprocess.run([sys.executable, str(target), "install"], capture_output=True, text=True, timeout=60)
        return "reinstalled" if p.returncode == 0 else "failed"
    return "unchanged"


def rollback(wait: float = 120.0) -> dict[str, Any]:
    prev = read_json(state_file(), {}).get("previous")
    if not prev or not (versions_dir() / prev).is_dir():
        raise UpdateError("no_previous")
    return swap(prev, wait)


def _merge_memory(src: Path) -> None:
    mem = code_dir() / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    for p in src.iterdir():
        if not (mem / p.name).exists():
            shutil.move(str(p), mem / p.name)
    shutil.rmtree(src, ignore_errors=True)  # what's left already had a copy in code/memory


def adopt(wait: float = 120.0) -> dict[str, Any]:
    """A copy (sync.sh) becomes versions/<its VERSION>: its notes move to code/memory, and the skill path links to current."""
    if managed() != "copy":
        raise UpdateError(f"managed_{managed()}")
    s = skill_path()
    ver = (s / "VERSION").read_text().strip() if (s / "VERSION").exists() else "legacy"
    ver = ver if VERSION_RE.fullmatch(ver) else "legacy"
    dest = versions_dir() / ver
    if dest.exists():
        raise UpdateError("exists")
    versions_dir().mkdir(parents=True, exist_ok=True)
    with drained(wait):
        if (s / "memory").is_dir() and not (s / "memory").is_symlink():
            _merge_memory(s / "memory")
        shutil.move(str(s), dest)
        _link_memory(dest)
        _point(ver)
    return {"ok": True, "current": ver, "adopted": True, "scheduler": reschedule()}


def run(a: argparse.Namespace) -> dict[str, Any]:
    if a.cmd == "version":
        return version_info()
    if a.cmd == "status":
        return status()
    if a.cmd == "install":
        return install(Path(a.src).expanduser(), a.version)
    if a.cmd == "swap":
        return swap(a.ver, a.wait)
    if a.cmd == "rollback":
        return rollback(a.wait)
    return adopt(a.wait)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="update.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    sub.add_parser("status")
    i = sub.add_parser("install")
    i.add_argument("src")
    i.add_argument("--version")
    for name in ("swap", "rollback", "adopt"):
        p = sub.add_parser(name)
        if name == "swap":
            p.add_argument("ver")
        p.add_argument("--wait", type=float, default=120.0, help="seconds to wait for a running tick")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    try:
        out = run(a)
    except UpdateError as e:
        out = {"ok": False, "reason": e.reason, "detail": str(e)}
    if a.cmd in ("install", "swap", "rollback", "adopt"):
        kind = "rollback" if a.cmd == "rollback" else "update_apply"
        diag.event(kind, op=a.cmd, outcome="ok" if out.get("ok") else out["reason"], secs=round(time.monotonic() - t0, 2))
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    diag.install("update")
    sys.exit(main())
