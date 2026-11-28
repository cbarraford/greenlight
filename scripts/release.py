#!/usr/bin/env python3
"""Build and sign Greenlight releases for the stable and main channels (docs/RELEASING.md). JSON on stdout.

    release.py version --channel stable|main [--ref REF]   {version, short, build, commit, tag} for this checkout
    release.py backend --version V --out DIR              the backend as a reproducible greenlight-backend-V.tar.gz
    release.py feed --channel C --version V --build N --commit SHA --app ZIP --backend TGZ --base-url URL
                    --signing developer_id|adhoc [--notarized] [--notes FILE] --out FILE
                                                           the channel's feed, signed with $GREENLIGHT_FEED_KEY
    release.py verify FEED [--pub HEX] [--app ZIP] [--backend TGZ] [--build N]   the feed's signature, the files' sha256
    release.py notes --channel C [--ref REF] [--repo OWNER/NAME]   release notes (Markdown): a stable tag's commits since
                                                           the previous one, or a main build's own commit
    release.py guard --build N [--version V] [--current FEED] [--floor N [--floor-version V]]
                                                           refuse a build older than the channel's feed
    release.py keygen                                      a new feed key pair (the private half goes in a CI secret)
    release.py pubkey                                      the public half of $GREENLIGHT_FEED_KEY

A feed is {"body": base64(JSON), "sig": base64(Ed25519 over those exact bytes), "key": key id}: the app checks the bytes
it got, then parses them, so nothing depends on two JSON encoders agreeing. The body lists each file's sha256, so the
signature covers the app zip and the backend tarball too.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import diag  # noqa: E402
import ed25519  # noqa: E402
import update  # noqa: E402

ROOT = Path(os.path.realpath(__file__)).parent.parent
PUBKEY_FILE = ROOT / "app" / "feed-key.pub"  # the public half, compiled into the app; missing until the owner runs keygen
CHANNELS = ("stable", "main")
TAG_RE = re.compile(r"v(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\Z")  # no leading zeros: v1.02.3 would be 1.2.3 too
BACKEND = ("scripts/", "lifecycles/", "prompts/", "examples/", "SKILL.md", "README.md", "LICENSE")  # tracked files only
MIN_MACOS = "14.0"
KEY_ENV = "GREENLIGHT_FEED_KEY"


class ReleaseError(Exception):
    pass


def git(*args: str, root: Path = ROOT) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise ReleaseError(f"git {args[0]} failed: {p.stderr.strip()[-300:]}")
    return p.stdout.strip()


def version(channel: str, ref: str | None = None, root: Path = ROOT) -> dict[str, Any]:
    """stable: a vX.Y.Z tag gives X.Y.Z. main: main-<commits>-<sha>, its short version the latest stable tag's (0.0.0
    before the first). build, the commit count, orders every build on both channels."""
    commit = git("rev-parse", "HEAD", root=root)
    build = int(git("rev-list", "--count", "HEAD", root=root))
    if channel == "stable":
        tag = ref or ""
        m = TAG_RE.fullmatch(tag)
        if not m:
            raise ReleaseError(f"a stable release needs a vX.Y.Z tag, not {tag!r}")
        short = ".".join(str(int(x)) for x in m.groups())
        return {"channel": channel, "version": short, "short": short, "build": build, "commit": commit, "tag": tag}
    if channel != "main":
        raise ReleaseError(f"unknown channel {channel!r}")
    tags = [t for t in git("tag", "--list", "v*", "--merged", "HEAD", root=root).split() if TAG_RE.fullmatch(t)]
    last = max(tags, key=tag_num, default="")  # the highest vX.Y.Z behind HEAD: not a v1.3.0-rc1 that happens to be nearer
    short = last[1:] if last else "0.0.0"
    ver = f"main-{build}-{commit[:7]}"
    return {"channel": channel, "version": ver, "short": short, "build": build, "commit": commit, "tag": ver}


def tag_num(tag: str) -> tuple[int, ...]:
    m = TAG_RE.fullmatch(tag)
    return tuple(int(x) for x in m.groups()) if m else ()


def notes(channel: str, ref: str | None = None, repo: str = "", root: Path = ROOT) -> str:
    """Each commit's subject (its PR number links on GitHub by itself), newest first."""
    if channel == "stable":
        tag = ref or ""
        if not TAG_RE.fullmatch(tag):
            raise ReleaseError(f"a stable release needs a vX.Y.Z tag, not {ref!r}")
        older = [t for t in git("tag", "--list", "v*", "--merged", tag, root=root).split() if TAG_RE.fullmatch(t) and tag_num(t) < tag_num(tag)]
        prev = max(older, key=tag_num, default=None)  # the release before this one, by version: not just any other tag
        span = f"{prev}..{tag}" if prev else tag
        head = f"Changes since {prev}:" if prev else "The first stable release."
        lines = git("log", "--no-merges", "--format=%s", "-n", "200", span, root=root).splitlines()
        body = [head, "", *(f"- {s}" for s in lines if s)]
    else:
        commit = git("rev-parse", "HEAD", root=root)
        subject = git("log", "-1", "--format=%s", root=root)
        link = f"https://github.com/{repo}/commit/{commit}" if repo else commit
        body = [f"A build of main at {commit[:7]}: {subject}", "", f"Commit: {link}"]
    return "\n".join(body) + "\n"


def committed(root: Path = ROOT) -> list[tuple[str, int, str]]:
    """(path, mode, blob) for each backend file in HEAD: what the commit holds, not what the worktree has now."""
    out = git("ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", *BACKEND, root=root)
    rows = []
    for row in filter(None, out.split("\0")):
        meta, path = row.split("\t", 1)
        mode, kind, blob = meta.split()
        if kind == "blob" and mode != "120000" and "__pycache__" not in path:
            rows.append((path, int(mode, 8), blob))
    return sorted(rows)


def blobs(shas: list[str], root: Path = ROOT) -> list[bytes]:
    """Each blob's bytes, in one `git cat-file --batch`."""
    p = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], input="".join(f"{s}\n" for s in shas).encode(),
                       capture_output=True, timeout=120)
    if p.returncode:
        raise ReleaseError(f"git cat-file failed: {p.stderr.decode(errors='replace').strip()[-300:]}")
    data, at = [], 0
    for sha in shas:
        nl = p.stdout.index(b"\n", at)
        got, _, size = p.stdout[at:nl].decode().split()
        if got != sha:
            raise ReleaseError(f"git cat-file gave {got}, not {sha}")
        data.append(p.stdout[nl + 1:nl + 1 + int(size)])
        at = nl + 1 + int(size) + 1
    return data


def backend(ver: str, out: Path, root: Path = ROOT) -> dict[str, Any]:
    """HEAD's backend files under greenlight-<v>/, plus VERSION: read from the commit (uncommitted edits stay out), sorted,
    owned by root, dated by the commit, and gzipped without a timestamp. The tar is the same for a commit on any machine;
    the gzip bytes are too wherever zlib is the same build (the CI runner)."""
    if not update.VERSION_RE.fullmatch(ver):
        raise ReleaseError(f"bad version {ver!r}")
    when = int(git("log", "-1", "--format=%ct", root=root))
    top = f"greenlight-{ver}"
    files = committed(root)
    entries = [(path, data, mode) for (path, mode, _), data in zip(files, blobs([b for _, _, b in files], root))]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel, data, mode in [*entries, ("VERSION", f"{ver}\n".encode(), 0o644)]:
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = len(data), when, 0, 0, "", ""
            ti.mode = 0o755 if mode & 0o111 else 0o644
            tf.addfile(ti, io.BytesIO(data))
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"greenlight-backend-{ver}.tar.gz"
    with open(path, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
        gz.write(raw.getvalue())
    return {"path": str(path), **asset(path)}


def asset(path: Path) -> dict[str, Any]:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"sha256": h.hexdigest(), "size": path.stat().st_size}


def secret_key() -> bytes:
    raw = os.environ.get(KEY_ENV, "").strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise ReleaseError(f"${KEY_ENV} must hold the 64 hex digits `release.py keygen` printed as private")
    return key


def key_id(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()[:16]


def feed(a: argparse.Namespace) -> dict[str, Any]:
    if a.channel not in CHANNELS:
        raise ReleaseError(f"unknown channel {a.channel!r}")
    if not a.base_url.startswith("https://"):
        raise ReleaseError("--base-url must be https")
    notes = Path(a.notes).read_text()[:20000] if a.notes else ""
    app, tgz = Path(a.app), Path(a.backend)
    base = a.base_url.rstrip("/")
    body = {"format": 1, "channel": a.channel, "version": a.version, "build": a.build, "commit": a.commit,
            "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "schema": update.SCHEMA,
            "compat": list(update.COMPAT), "protocol": update.PROTOCOL, "min_macos": MIN_MACOS,
            "signing": a.signing, "notarized": bool(a.notarized), "notes": notes,
            "app": {"url": f"{base}/{app.name}", **asset(app)}, "backend": {"url": f"{base}/{tgz.name}", **asset(tgz)}}
    raw = json.dumps(body, indent=1, sort_keys=True).encode()
    key = secret_key()
    signed = {"body": base64.b64encode(raw).decode(), "sig": base64.b64encode(ed25519.sign(key, raw)).decode(),
              "key": key_id(ed25519.public_key(key))}
    Path(a.out).write_text(json.dumps(signed, indent=1) + "\n")
    return {"ok": True, "out": a.out, "key": signed["key"], "version": a.version}


def default_pub() -> bytes | None:
    try:
        return bytes.fromhex(PUBKEY_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def verify(path: Path, pub: bytes | None, files: dict[str, Path | None]) -> dict[str, Any]:
    """The signature over the body's bytes with the public key (app/feed-key.pub unless --pub), then each given file's hash."""
    if pub is None or len(pub) != 32:
        raise ReleaseError("no public key: pass --pub or commit app/feed-key.pub")
    try:
        signed = json.loads(path.read_text())
        raw, sig = base64.b64decode(signed["body"], validate=True), base64.b64decode(signed["sig"], validate=True)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ReleaseError(f"not a feed: {e}") from None
    if not ed25519.verify(pub, raw, sig):
        raise ReleaseError("bad signature")
    body = json.loads(raw)
    for name, f in files.items():
        if f is not None and asset(f) != {k: body[name][k] for k in ("sha256", "size")}:
            raise ReleaseError(f"{name} doesn't match the feed")
    return {"ok": True, "channel": body["channel"], "version": body["version"], "build": body["build"],
            "checked": sorted(k for k, f in files.items() if f is not None)}


def guard(build: int, current: Path | None, floor: int | None = None, ver: str | None = None,
          floor_ver: str | None = None) -> dict[str, Any]:
    """Refuse a build older than the one the channel's feed offers (an old tag pushed late, an old run re-run): the feed
    only moves forward. `floor`/`floor_ver` are the build and version the feed release's title records, which a failed
    upload can't erase. The same build again is a re-run and passes, unless it's a lower stable version on that build:
    versions compare only against the version of that same build."""
    now, was = floor, floor_ver
    if current is not None and current.exists():
        try:
            body = json.loads(base64.b64decode(json.loads(current.read_text())["body"], validate=True))
            got, got_ver = int(body["build"]), str(body["version"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ReleaseError(f"can't read the channel's current feed: {e}") from None
        if now is None or got > now:
            now, was = got, got_ver
        elif got == now and not was:
            was = got_ver
    if now is None:
        return {"ok": True, "current": None}
    if build < now:
        raise ReleaseError(f"build {build} is older than the feed's build {now}: nothing published")
    new, old = tag_num(f"v{ver}"), tag_num(f"v{was}")  # () for a main version: only stable versions compare
    if build == now and new and old and new < old:
        raise ReleaseError(f"{ver} is lower than the feed's {was} on the same build: nothing published")
    return {"ok": True, "current": now}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="release.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("version")
    v.add_argument("--channel", required=True, choices=CHANNELS)
    v.add_argument("--ref")
    b = sub.add_parser("backend")
    b.add_argument("--version", required=True)
    b.add_argument("--out", required=True)
    f = sub.add_parser("feed")
    for k in ("--channel", "--version", "--commit", "--app", "--backend", "--base-url", "--out"):
        f.add_argument(k, required=True)
    f.add_argument("--build", type=int, required=True)
    f.add_argument("--signing", required=True, choices=("developer_id", "adhoc"))
    f.add_argument("--notarized", action="store_true")
    f.add_argument("--notes")
    c = sub.add_parser("verify")
    c.add_argument("feed")
    c.add_argument("--pub")
    c.add_argument("--app")
    c.add_argument("--backend")
    c.add_argument("--build", type=int)
    n = sub.add_parser("notes")
    n.add_argument("--channel", required=True, choices=CHANNELS)
    n.add_argument("--ref")
    n.add_argument("--repo", default="")
    g = sub.add_parser("guard")
    g.add_argument("--build", type=int, required=True)
    g.add_argument("--current")
    g.add_argument("--floor", type=int)
    g.add_argument("--floor-version")
    g.add_argument("--version")
    sub.add_parser("keygen")
    sub.add_parser("pubkey")
    return ap


def run(a: argparse.Namespace) -> dict[str, Any]:
    if a.cmd == "version":
        return version(a.channel, a.ref)
    if a.cmd == "backend":
        return backend(a.version, Path(a.out))
    if a.cmd == "feed":
        return feed(a)
    if a.cmd == "verify":
        pub = bytes.fromhex(a.pub) if a.pub else default_pub()
        ok = verify(Path(a.feed), pub, {"app": Path(a.app) if a.app else None, "backend": Path(a.backend) if a.backend else None})
        if a.build is not None and ok["build"] != a.build:
            raise ReleaseError(f"the feed is build {ok['build']}, not {a.build}")
        return ok
    if a.cmd == "guard":
        return guard(a.build, Path(a.current) if a.current else None, a.floor, a.version, a.floor_version)
    if a.cmd == "notes":
        return {"notes": notes(a.channel, a.ref, a.repo)}
    if a.cmd == "keygen":
        sk = secrets.token_bytes(32)
        pub = ed25519.public_key(sk)
        return {"private": sk.hex(), "public": pub.hex(), "key": key_id(pub)}
    pub = ed25519.public_key(secret_key())
    return {"public": pub.hex(), "key": key_id(pub)}


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        out = run(a)
    except ReleaseError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    diag.install("release")
    sys.exit(main())
