#!/usr/bin/env python3
"""pane_log: background tmux pipe-pane recorder and timestamped log reader for Greenlight.

    pane_log.py record --session NAME [--cwd PATH] [--max-bytes N]
    pane_log.py record --pane %ID [--socket SOCK] [--max-bytes N]
    pane_log.py read SESSION [--before CURSOR] [--limit N]
    pane_log.py workspace PATH [--before CURSOR] [--limit N]

Records raw PTY output from tmux panes (`pipe-pane -O`) as compact timestamped JSONL chunks under
`$GREENLIGHT_HOME/pane-logs/<session>.jsonl` (rotated to `.jsonl.1` at half the cap, default 2 MiB
total per session). Idle panes block in `os.read()` with zero polling. Stdlib only.
"""
from __future__ import annotations

import codecs
import contextlib
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import diag

SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
PANE_ID_RE = re.compile(r"^%\d{1,12}$")
SOCKET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
CURSOR_RE = re.compile(r"^([A-Za-z0-9_.-]{1,128})\.g(\d{1,9}):(\d{1,9})$")

DEFAULT_MAX_BYTES = 2 << 20  # 2 MiB total per session (1 MiB active + 1 MiB rotated .1)
COALESCE_SECS = 0.05
COALESCE_BYTES = 16384
STALE_SECS = 7 * 86400
MAX_SESSIONS = 100
MAX_LINE_LEN = 4000

# Terminal escape sequences: OSC, DCS/APC/PM/SOS, CSI, and 2-byte ESC.
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
DCS_RE = re.compile(r"\x1b[P_^X][^\x1b]*(?:\x1b\\)")
CSI_RE = re.compile(r"\x1b\[([0-?]*[ -/]*)([@-~])")
ESC_RE = re.compile(r"\x1b(?:[()#%*+\-./][0-9A-Za-z]|[@-Z\\\-_])")
CTRL_RE = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")
TOOL_RE = re.compile(
    r"^\s*[●⏺•▸▹▶❯*]\s*(Bash|Read|Edit|Write|MultiEdit|Grep|Glob|WebFetch|WebSearch|Agent|Task|NotebookEdit|TodoWrite|Skill)\s*\((.{0,200}?)\)?\s*$"
)
CODEX_TOOL_RE = re.compile(r"^\s*[•●]\s*(?:Ran|Running|Explored|Edited)\s+(.{1,200})$")


def default_log_dir(home: Path | str | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
    return base / "pane-logs"


def valid_session(name: str) -> str:
    if not name or name.startswith(".") or ".." in name or not SESSION_RE.fullmatch(name):
        raise ValueError(f"invalid session name: {name!r}")
    return name


def _ensure_dir(d: Path) -> Path:
    if d.is_symlink():
        raise OSError(f"refusing symlink log dir: {d}")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _open_append(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def _read_header(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline(8192)
        obj = json.loads(first) if first else None
        meta = obj.get("meta") if isinstance(obj, dict) else None
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_pane(pane_id: str, socket: str) -> tuple[str, str]:
    if not PANE_ID_RE.fullmatch(pane_id):
        raise ValueError(f"invalid pane id: {pane_id!r}")
    sock = socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"
    if not SOCKET_RE.fullmatch(sock):
        raise ValueError(f"invalid tmux socket: {sock!r}")
    p = subprocess.run(
        ["tmux", "-L", sock, "display-message", "-p", "-t", pane_id, "#{session_name}\t#{pane_current_path}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if p.returncode != 0 or "\t" not in p.stdout:
        raise RuntimeError(f"tmux display-message failed for {pane_id}")
    sess, _, cwd = p.stdout.rstrip("\r\n").partition("\t")
    return valid_session(sess.strip()), cwd.strip()


def prune_stale(
    d: Path,
    keep: set[str] | None = None,
    max_age: float = STALE_SECS,
    max_sessions: int = MAX_SESSIONS,
    now: float | None = None,
) -> int:
    """Remove old dead session logs from `d` so disk usage stays bounded across sessions."""
    if not d.is_dir() or d.is_symlink():
        return 0
    keep = keep or set()
    ts_now = time.time() if now is None else now
    sessions: dict[str, tuple[float, list[Path]]] = {}
    for entry in d.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        name = entry.name
        if name.endswith(".jsonl.1"):
            stem = name[:-8]
        elif name.endswith(".jsonl"):
            stem = name[:-6]
        else:
            continue
        try:
            mt = entry.stat().st_mtime
        except OSError:
            continue
        prev_mt, files = sessions.get(stem, (0.0, []))
        files.append(entry)
        sessions[stem] = (max(prev_mt, mt), files)

    removed = 0
    ordered = sorted(sessions.items(), key=lambda kv: kv[1][0], reverse=True)
    for idx, (stem, (mt, files)) in enumerate(ordered):
        if stem in keep:
            continue
        if (ts_now - mt) > max_age or idx >= max_sessions:
            for f in files:
                with contextlib.suppress(OSError):
                    f.unlink()
                    removed += 1
    return removed


def _open_session_log(cur: Path, prev: Path, session: str, cwd: str) -> tuple[Any, int, int]:
    """Open active log file, reusing its generation if non-empty or writing a fresh meta header."""
    if cur.is_symlink() or prev.is_symlink():
        raise OSError("refusing symlink session log")
    if cur.is_file() and cur.stat().st_size > 0:
        meta = _read_header(cur)
        gen = int(meta.get("gen", 0)) if isinstance(meta.get("gen"), int) else 0
        fh = _open_append(cur)
        return fh, gen, cur.stat().st_size
    prev_meta = _read_header(prev)
    gen = (int(prev_meta["gen"]) + 1) if isinstance(prev_meta.get("gen"), int) else 0
    fh = _open_append(cur)
    header = json.dumps({"ts": int(time.time() * 1000), "meta": {"session": session, "cwd": cwd, "gen": gen}}) + "\n"
    fh.write(header)
    fh.flush()
    return fh, gen, len(header.encode("utf-8"))


def _rotate_session_log(fh: Any, cur: Path, prev: Path, session: str, cwd: str, gen: int) -> tuple[Any, int, int]:
    fh.flush()
    fh.close()
    if prev.is_symlink():
        raise OSError("refusing symlink rotated log")
    os.replace(cur, prev)
    new_gen = gen + 1
    new_fh = _open_append(cur)
    header = json.dumps({"ts": int(time.time() * 1000), "meta": {"session": session, "cwd": cwd, "gen": new_gen}}) + "\n"
    new_fh.write(header)
    new_fh.flush()
    return new_fh, new_gen, len(header.encode("utf-8"))


def _read_coalesced(fd: int, first: bytes, limit: int = COALESCE_BYTES) -> bytes:
    """Coalesce rapid PTY writes arriving within COALESCE_SECS up to `limit` bytes."""
    buf = bytearray(first)
    deadline = time.monotonic() + COALESCE_SECS
    while len(buf) < limit:
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], rem)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            more = os.read(fd, min(8192, limit - len(buf)))
        except OSError:
            break
        if not more:
            break
        buf.extend(more)
    return bytes(buf)


def record(
    session: str = "",
    cwd: str = "",
    pane_id: str = "",
    socket: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
    log_dir: Path | None = None,
    in_fd: int = 0,
) -> int:
    """Stream raw bytes from `in_fd` into bounded timestamped JSONL chunks. Returns bytes written."""
    if pane_id and not session:
        session, resolved_cwd = _resolve_pane(pane_id, socket)
        cwd = cwd or resolved_cwd
    session = valid_session(session)
    d = _ensure_dir(log_dir or default_log_dir())
    prune_stale(d, keep={session})

    cur = d / f"{session}.jsonl"
    prev = d / f"{session}.jsonl.1"
    half = max(4096, int(max_bytes) // 2)
    chunk_cap = max(512, min(COALESCE_BYTES, half // 2))
    fh, gen, size = _open_session_log(cur, prev, session, cwd)
    has_data = cur.is_file() and cur.stat().st_size > size
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    total_written = 0
    stop = False

    def _handle_stop(_sig: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    prev_term = signal.signal(signal.SIGTERM, _handle_stop) if os.getpid() and in_fd == 0 else None
    try:
        while not stop:
            try:
                raw = os.read(in_fd, min(8192, chunk_cap))
            except InterruptedError:
                continue
            except OSError:
                break
            if not raw:
                break
            ts = int(time.time() * 1000)
            chunk = _read_coalesced(in_fd, raw, limit=chunk_cap)
            text = decoder.decode(chunk, final=False)
            if not text:
                continue
            rec = json.dumps({"ts": ts, "d": text}, ensure_ascii=False) + "\n"
            rec_len = len(rec.encode("utf-8"))
            if has_data and size + rec_len > half:
                fh, gen, size = _rotate_session_log(fh, cur, prev, session, cwd, gen)
                has_data = False
            fh.write(rec)
            fh.flush()
            size += rec_len
            has_data = True
            total_written += rec_len
        tail = decoder.decode(b"", final=True)
        if tail:
            rec = json.dumps({"ts": int(time.time() * 1000), "d": tail}, ensure_ascii=False) + "\n"
            fh.write(rec)
            fh.flush()
            total_written += len(rec.encode("utf-8"))
    finally:
        with contextlib.suppress(OSError):
            fh.flush()
            fh.close()
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)
    return total_written


def _csi_sub(match: re.Match[str]) -> str:
    params, cmd = match.group(1), match.group(2)
    if cmd in ("H", "f", "E", "F", "B"):
        return "\n"
    if cmd == "G" or (cmd == "K" and params.startswith("2")):
        return "\r"
    return ""


def _clean_chunk(text: str) -> str:
    s = OSC_RE.sub("", text)
    s = DCS_RE.sub("", s)
    s = CSI_RE.sub(_csi_sub, s)
    s = ESC_RE.sub("", s)
    return CTRL_RE.sub("", s)


def parse_chunks(chunks: list[tuple[int, str]], session: str, gen: int = 0) -> list[dict[str, Any]]:
    """Turn timestamped raw PTY chunks into clean display lines with per-line epoch-ms timestamps."""
    lines: list[dict[str, Any]] = []
    cur: list[str] = []
    cur_ts = 0
    idx = 0

    def _emit(ts: int) -> None:
        nonlocal idx
        text = "".join(cur).rstrip()[:MAX_LINE_LEN]
        cur.clear()
        if not text:
            if not lines or lines[-1]["text"] == "":
                return
        elif lines and lines[-1]["text"] == text and abs(ts - lines[-1]["ts"]) < 2000:
            lines[-1]["ts"] = ts  # collapse rapid duplicate TUI spinner redraws
            return
        lines.append({"id": f"{session}.g{gen}:{idx}", "role": "line", "text": text, "ts": ts, "kind": "pane"})
        idx += 1

    for ts, raw in chunks:
        cleaned = _clean_chunk(raw)
        i, n = 0, len(cleaned)
        while i < n:
            ch = cleaned[i]
            if ch == "\r":
                if i + 1 < n and cleaned[i + 1] == "\n":
                    _emit(cur_ts or ts)
                    cur_ts = ts
                    i += 2
                    continue
                cur.clear()
                cur_ts = ts
            elif ch == "\n":
                _emit(cur_ts or ts)
                cur_ts = ts
            elif ch == "\x08":
                if cur:
                    cur.pop()
            else:
                if not cur:
                    cur_ts = ts
                cur.append(ch)
            i += 1
    if cur:
        _emit(cur_ts or (chunks[-1][0] if chunks else 0))
    while lines and not lines[-1]["text"]:
        lines.pop()
    return lines


def _load_file_entries(path: Path, session: str) -> tuple[list[dict[str, Any]], int]:
    if not path.is_file() or path.is_symlink():
        return [], 0
    chunks: list[tuple[int, str]] = []
    gen = 0
    max_ts = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                try:
                    obj = json.loads(raw_line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("ts")
                if isinstance(ts, (int, float)) and ts > max_ts:
                    max_ts = int(ts)
                meta = obj.get("meta")
                if isinstance(meta, dict) and isinstance(meta.get("gen"), int):
                    gen = meta["gen"]
                    continue
                d = obj.get("d")
                if isinstance(d, str) and isinstance(ts, (int, float)):
                    chunks.append((int(ts), d))
    except OSError:
        return [], 0
    return parse_chunks(chunks, session, gen), max_ts


def detect_tool(entries: list[dict[str, Any]]) -> tuple[str, str]:
    """Extract the most recent tool invocation from parsed terminal lines, if any."""
    for e in reversed(entries[-40:]):
        text = e.get("text") or ""
        if m := TOOL_RE.match(text):
            return m.group(1), " ".join(m.group(2).split())[:160]
        if m := CODEX_TOOL_RE.match(text):
            return "Shell", " ".join(m.group(1).split())[:160]
    return "", ""


def _slice_before(entries: list[dict[str, Any]], before: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    if not entries:
        return [], None
    end = len(entries)
    if before:
        m = CURSOR_RE.fullmatch(before)
        if m:
            b_sess, b_gen, b_idx = m.group(1), int(m.group(2)), int(m.group(3))
            end = next(
                (
                    i
                    for i, e in enumerate(entries)
                    if e["id"] == before
                    or (
                        (em := CURSOR_RE.fullmatch(e["id"])) is not None
                        and em.group(1) == b_sess
                        and (int(em.group(2)), int(em.group(3))) >= (b_gen, b_idx)
                    )
                ),
                0,
            )
    start = max(0, end - max(1, limit))
    page = entries[start:end]
    more = page[0]["id"] if start > 0 and page else None
    return page, more


def read_session(
    session: str,
    before: str | None = None,
    limit: int = 200,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Read paginated timestamped lines for one recorded tmux session."""
    session = valid_session(session)
    d = log_dir or default_log_dir()
    prev, cur = d / f"{session}.jsonl.1", d / f"{session}.jsonl"
    prev_entries, prev_ts = _load_file_entries(prev, session)
    cur_entries, cur_ts = _load_file_entries(cur, session)
    entries = prev_entries + cur_entries
    last_ts = max(prev_ts, cur_ts)
    if not last_ts and cur.is_file():
        with contextlib.suppress(OSError):
            last_ts = int(cur.stat().st_mtime * 1000)
    tool, tool_input = detect_tool(entries)
    page, more = _slice_before(entries, before, limit)
    return {
        "entries": page,
        "more": more,
        "before": before,
        "recent": [],
        "source": "pane",
        "session": session,
        "last_output_ts": last_ts or None,
        "tool": tool,
        "tool_input": tool_input,
    }


def _under_path(cwd: str, root: str) -> bool:
    if not cwd or not root:
        return False
    a, b = cwd.rstrip("/"), root.rstrip("/")
    return a == b or a.startswith(b + "/")


def workspace_sessions(
    path: str,
    live_panes: list[dict[str, Any]] | None = None,
    log_dir: Path | None = None,
) -> list[str]:
    """Recorded tmux sessions belonging to workspace `path`, newest first."""
    d = log_dir or default_log_dir()
    if not d.is_dir() or d.is_symlink():
        return []
    matched: set[str] = set()
    if live_panes is None:
        try:
            import ade_basement

            live_panes = ade_basement.panes()
        except Exception:
            live_panes = []
    for p in live_panes:
        s = p.get("session") or ""
        if s and _under_path(p.get("cwd") or "", path):
            with contextlib.suppress(ValueError):
                matched.add(valid_session(s))
    for f in d.glob("*.jsonl"):
        if f.is_symlink():
            continue
        stem = f.stem
        if stem in matched:
            continue
        meta = _read_header(f)
        if _under_path(str(meta.get("cwd") or ""), path):
            with contextlib.suppress(ValueError):
                matched.add(valid_session(stem))

    def _mtime(sess: str) -> float:
        mt = 0.0
        for p in (d / f"{sess}.jsonl", d / f"{sess}.jsonl.1"):
            if p.is_file() and not p.is_symlink():
                with contextlib.suppress(OSError):
                    mt = max(mt, p.stat().st_mtime)
        return mt

    existing = [s for s in matched if (d / f"{s}.jsonl").is_file() or (d / f"{s}.jsonl.1").is_file()]
    return sorted(existing, key=_mtime, reverse=True)


def read_workspace(
    path: str,
    live_panes: list[dict[str, Any]] | None = None,
    before: str | None = None,
    limit: int = 200,
    log_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Read the newest recorded pane log for `path` (or the session named by `before`), else None."""
    sessions = workspace_sessions(path, live_panes=live_panes, log_dir=log_dir)
    if not sessions:
        return None
    if before:
        m = CURSOR_RE.fullmatch(before)
        if not m or m.group(1) not in sessions:
            return None
        target = m.group(1)
    else:
        target = sessions[0]
    return read_session(target, before=before, limit=limit, log_dir=log_dir)


def pipe_command(code_path: str, home_path: str, socket: str = "greenlight") -> str:
    """Remote-safe POSIX shell command for tmux `pipe-pane -O` (no-ops cleanly if script is absent)."""
    if not SOCKET_RE.fullmatch(socket):
        raise ValueError(f"invalid tmux socket: {socket!r}")
    script = f"{code_path.rstrip('/')}/scripts/pane_log.py"
    return (
        f"[ -f {shlex.quote(script)} ] && "
        f"GREENLIGHT_HOME={shlex.quote(home_path)} "
        f"exec python3 {shlex.quote(script)} record --pane #{{pane_id}} --socket {shlex.quote(socket)}"
    )


def attach(session: str, cwd: str = "", socket: str | None = None, code_dir: Path | None = None) -> bool:
    """Idempotently attach `pipe-pane -O` to a tmux session if not already piping."""
    session = valid_session(session)
    sock = socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight"
    if not SOCKET_RE.fullmatch(sock):
        return False
    check = subprocess.run(
        ["tmux", "-L", sock, "display-message", "-p", "-t", f"={session}:", "#{pane_pipe}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check.returncode != 0 or check.stdout.strip() == "1":
        return check.returncode == 0
    script = (code_dir or Path(__file__).resolve().parent) / "pane_log.py"
    if not script.is_file():
        return False
    home = os.environ.get("GREENLIGHT_HOME") or str(Path.home() / ".greenlight")
    cmd = (
        f"GREENLIGHT_HOME={shlex.quote(home)} exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(script))} record --session {shlex.quote(session)}"
        + (f" --cwd {shlex.quote(cwd)}" if cwd else "")
    )
    res = subprocess.run(
        ["tmux", "-L", sock, "pipe-pane", "-O", "-t", f"={session}:", cmd],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return res.returncode == 0


def _arg(args: list[str], flag: str, default: str = "") -> str:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def main() -> None:
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    cmd = a[0]
    if cmd == "record":
        session = _arg(a, "--session")
        pane_id = _arg(a, "--pane")
        cwd = _arg(a, "--cwd")
        socket = _arg(a, "--socket")
        max_bytes = int(_arg(a, "--max-bytes", str(DEFAULT_MAX_BYTES)))
        with diag.span("pane_log_record") as sp:
            sp["op"] = "record"
            record(session=session, cwd=cwd, pane_id=pane_id, socket=socket, max_bytes=max_bytes)
    elif cmd == "read" and len(a) >= 2:
        before = _arg(a, "--before") or None
        limit = int(_arg(a, "--limit", "200"))
        print(json.dumps(read_session(a[1], before=before, limit=limit)))
    elif cmd == "workspace" and len(a) >= 2:
        before = _arg(a, "--before") or None
        limit = int(_arg(a, "--limit", "200"))
        print(json.dumps(read_workspace(a[1], before=before, limit=limit) or {"entries": [], "more": None, "before": before, "recent": []}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    diag.install("pane_log")
    main()
