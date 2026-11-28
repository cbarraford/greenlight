#!/usr/bin/env python3
"""pane_log: background tmux pipe-pane recorder and timestamped log reader for Greenlight.

    pane_log.py record --session NAME [--cwd PATH] [--agent NAME] [--max-bytes N]
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
import fcntl
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

try:
    import bins
except ImportError:  # pane_log.py can be copied standalone alongside diag.py
    bins = None  # type: ignore[assignment]
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
MAX_ESC_CARRY = 512
REDRAW_WINDOW_MS = 10_000
END_CURSOR_GEN = 999_999_999

# Sentinel for cursor-up (CSI A / CSI F) emitted only by _csi_sub after raw control chars are stripped.
UP_MARK = "\x1e"

# Terminal escape sequences: OSC, DCS/APC/PM/SOS, CSI, and 2-byte ESC.
OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
DCS_RE = re.compile(r"\x1b[P_^X][^\x1b]*(?:\x1b\\)")
CSI_RE = re.compile(r"\x1b\[([0-?]*[ -/]*)([@-~])")
ESC_RE = re.compile(r"\x1b(?:[()#%*+\-./][0-9A-Za-z]|[@-Z\\\-_])")
RAW_CTRL_RE = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
STRAY_ESC_RE = re.compile(r"\x1b")

# Best-effort tool invocation detection from Claude Code / Codex terminal lines.
TOOL_RE = re.compile(
    r"^\s*[●⏺•▸▹▶❯*]\s*(Bash|Read|Edit|Write|MultiEdit|Grep|Glob|WebFetch|WebSearch|Agent|Task|NotebookEdit|TodoWrite|Skill)\s*\((.{0,200}?)\)?\s*$"
)
CODEX_TOOL_RE = re.compile(r"^\s*[•●]\s*(?:Ran|Running|Explored|Edited)\s+(.{1,200})$")


class _StopRecord(BaseException):
    """Raised by the SIGTERM handler so PEP 475 does not auto-retry a blocked os.read()."""


def default_log_dir(home: Path | str | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("GREENLIGHT_HOME") or Path.home() / ".greenlight")
    return base / "pane-logs"


def valid_session(name: str) -> str:
    if not name or name.startswith(".") or ".." in name or not SESSION_RE.fullmatch(name):
        raise ValueError(f"invalid session name: {name!r}")
    return name


def valid_socket(name: str) -> str:
    if not name or name[0] in ".-" or ".." in name or not SOCKET_RE.fullmatch(name):
        raise ValueError(f"invalid tmux socket: {name!r}")
    return name


def tmux_format_escape(s: str) -> str:
    """Escape literal `#` as `##` for tmux commands that run status-left format expansion."""
    return s.replace("#", "##")


def tmux_quote(s: str) -> str:
    """Escape a string as a tmux double-quoted argument (`\\`, `"`, and `$`)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


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


def _acquire_session_lock(d: Path, session: str) -> int | None:
    """Acquire a non-blocking exclusive flock for `session`. Returns lock fd, or None if already held."""
    lock_path = d / f"{session}.lock"
    if lock_path.is_symlink():
        raise OSError(f"refusing symlink session lock: {lock_path}")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def _is_session_locked(d: Path, session: str) -> bool:
    lock_path = d / f"{session}.lock"
    if not lock_path.is_file() or lock_path.is_symlink():
        return False
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


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


def _file_has_data(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            fh.readline()
            return bool(fh.readline())
    except OSError:
        return False


def _tmux_bin() -> str:
    return (bins.find("tmux") if bins else None) or "tmux"


def _resolve_pane(pane_id: str, socket: str) -> tuple[str, str]:
    if not PANE_ID_RE.fullmatch(pane_id):
        raise ValueError(f"invalid pane id: {pane_id!r}")
    sock = valid_socket(socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight")
    p = subprocess.run(
        [_tmux_bin(), "-L", sock, "display-message", "-p", "-t", pane_id, "#{session_name}\t#{pane_current_path}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if p.returncode != 0 or "\t" not in p.stdout:
        raise RuntimeError(f"tmux display-message failed for {pane_id}")
    sess, _, cwd = p.stdout.rstrip("\r\n").partition("\t")
    return valid_session(sess.strip()), cwd.strip()


def _live_tmux_sessions(socket: str) -> set[str]:
    try:
        sock = valid_socket(socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight")
        p = subprocess.run(
            [_tmux_bin(), "-L", sock, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if p.returncode != 0:
            return set()
        out: set[str] = set()
        for line in p.stdout.splitlines():
            with contextlib.suppress(ValueError):
                out.add(valid_session(line.strip()))
        return out
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()


def _collect_session_files(d: Path) -> dict[str, tuple[float, list[Path]]]:
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
    return sessions


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
    ordered = sorted(_collect_session_files(d).items(), key=lambda kv: kv[1][0], reverse=True)
    removed = 0
    for idx, (stem, (mt, files)) in enumerate(ordered):
        if stem in keep or _is_session_locked(d, stem):
            continue
        if (ts_now - mt) > max_age or idx >= max_sessions:
            for f in files:
                with contextlib.suppress(OSError):
                    f.unlink()
                    removed += 1
            with contextlib.suppress(OSError):
                (d / f"{stem}.lock").unlink()
    return removed


def _header_record(session: str, cwd: str, gen: int, agent: str = "") -> str:
    meta: dict[str, Any] = {"session": session, "cwd": cwd, "gen": gen}
    if agent:
        meta["agent"] = agent
    return json.dumps({"ts": int(time.time() * 1000), "meta": meta}) + "\n"


def _open_session_log(cur: Path, prev: Path, session: str, cwd: str, agent: str = "") -> tuple[Any, int, int, bool]:
    """Open active log file, reusing its generation if non-empty or writing a fresh meta header."""
    if cur.is_symlink() or prev.is_symlink():
        raise OSError("refusing symlink session log")
    if cur.is_file() and cur.stat().st_size > 0:
        meta = _read_header(cur)
        gen = int(meta.get("gen", 0)) if isinstance(meta.get("gen"), int) else 0
        has_data = _file_has_data(cur)
        fh = _open_append(cur)
        return fh, gen, cur.stat().st_size, has_data
    prev_meta = _read_header(prev)
    gen = (int(prev_meta["gen"]) + 1) if isinstance(prev_meta.get("gen"), int) else 0
    fh = _open_append(cur)
    header = _header_record(session, cwd, gen, agent=agent)
    fh.write(header)
    fh.flush()
    return fh, gen, len(header.encode("utf-8")), False


def _rotate_session_log(fh: Any, cur: Path, prev: Path, session: str, cwd: str, gen: int, agent: str = "") -> tuple[Any, int, int]:
    fh.flush()
    fh.close()
    if prev.is_symlink():
        raise OSError("refusing symlink rotated log")
    with contextlib.suppress(FileNotFoundError):
        os.replace(cur, prev)
    new_gen = gen + 1
    new_fh = _open_append(cur)
    header = _header_record(session, cwd, new_gen, agent=agent)
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
    agent: str = "",
) -> int:
    """Stream raw bytes from `in_fd` into bounded timestamped JSONL chunks. Returns bytes written."""
    if pane_id and not session:
        session, resolved_cwd = _resolve_pane(pane_id, socket)
        cwd = cwd or resolved_cwd
    session = valid_session(session)
    d = _ensure_dir(log_dir or default_log_dir())
    lock_fd = _acquire_session_lock(d, session)
    if lock_fd is None:
        return 0
    keep = {session} | (_live_tmux_sessions(socket) if log_dir is None else set())
    prune_stale(d, keep=keep)

    cur, prev = d / f"{session}.jsonl", d / f"{session}.jsonl.1"
    half = max(4096, int(max_bytes) // 2)
    chunk_cap = max(512, min(COALESCE_BYTES, half // 2))
    fh, gen, size, has_data = _open_session_log(cur, prev, session, cwd, agent=agent)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    total_written = 0

    def _handle_stop(_sig: int, _frame: Any) -> None:
        raise _StopRecord()

    prev_term = signal.signal(signal.SIGTERM, _handle_stop) if in_fd == 0 else None
    try:
        try:
            while True:
                try:
                    raw = os.read(in_fd, min(8192, chunk_cap))
                except OSError:
                    break
                if not raw:
                    break
                ts = int(time.time() * 1000)
                text = decoder.decode(_read_coalesced(in_fd, raw, limit=chunk_cap), final=False)
                if not text:
                    continue
                rec = json.dumps({"ts": ts, "d": text}, ensure_ascii=False) + "\n"
                rec_len = len(rec.encode("utf-8"))
                if has_data and size + rec_len > half:
                    fh, gen, size = _rotate_session_log(fh, cur, prev, session, cwd, gen, agent=agent)
                    has_data = False
                fh.write(rec)
                fh.flush()
                size += rec_len
                has_data = True
                total_written += rec_len
        except _StopRecord:
            pass
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
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)
    return total_written


def _is_incomplete_esc(tail: str) -> bool:
    if len(tail) == 1:
        return True
    ch = tail[1]
    if ch == "[":
        return CSI_RE.match(tail) is None and re.fullmatch(r"\x1b\[[0-?]*[ -/]*", tail) is not None
    if ch == "]":
        return OSC_RE.match(tail) is None
    if ch in "P_^X":
        return DCS_RE.match(tail) is None
    if ch in "()#%*+-./":
        return len(tail) == 2
    return False


def _split_incomplete_esc(text: str) -> tuple[str, str]:
    """Split off an incomplete trailing ANSI/OSC/CSI escape sequence so it carries to the next chunk."""
    start = max(0, len(text) - MAX_ESC_CARRY)
    pos = text.find("\x1b", start)
    while pos != -1:
        tail = text[pos:]
        if _is_incomplete_esc(tail):
            return text[:pos], tail
        pos = text.find("\x1b", pos + 1)
    return text, ""


def _csi_count(params: str) -> int:
    head = params.split(";")[0]
    if head.isdigit():
        return min(max(int(head), 1), 16)
    return 1


def _csi_sub(match: re.Match[str]) -> str:
    params, cmd = match.group(1), match.group(2)
    if cmd in ("A", "F"):
        return UP_MARK * _csi_count(params)
    if cmd in ("H", "f", "E", "B"):
        return "\n"
    if cmd == "G":
        return "\r" if params in ("", "1") else " "
    if cmd == "K":
        return "\r" if params.startswith("2") else ""
    if cmd == "C":
        return " "
    return ""


def _clean_chunk(text: str) -> str:
    s = OSC_RE.sub("", text)
    s = DCS_RE.sub("", s)
    s = RAW_CTRL_RE.sub("", s)
    s = CSI_RE.sub(_csi_sub, s)
    s = ESC_RE.sub("", s)
    return STRAY_ESC_RE.sub("", s)


class _LineBuilder:
    """Incremental terminal chunk parser preserving per-line timestamps and collapsing redraws."""

    def __init__(self, session: str, gen: int) -> None:
        self.session = session
        self.gen = gen
        self.lines: list[dict[str, Any]] = []
        self.cur: list[str] = []
        self.cur_ts = 0
        self.idx = 0
        self.pending_esc = ""
        self.pending_cr = False
        self.pending_cr_ts = 0

    def _emit(self, ts: int) -> None:
        text = "".join(self.cur).rstrip()[:MAX_LINE_LEN]
        self.cur.clear()
        if not text:
            if not self.lines or self.lines[-1]["text"] == "":
                return
        elif self.lines and self.lines[-1]["text"] == text and abs(ts - self.lines[-1]["ts"]) < 2000:
            self.lines[-1]["ts"] = ts
            return
        self.lines.append({"id": f"{self.session}.g{self.gen}:{self.idx}", "role": "line", "text": text, "ts": ts, "kind": "pane"})
        self.idx += 1

    def _step(self, ch: str, ts: int) -> None:
        if ch == UP_MARK:
            self.cur.clear()
            if self.lines and abs(ts - self.lines[-1]["ts"]) < REDRAW_WINDOW_MS:
                self.lines.pop()
                self.idx = max(0, self.idx - 1)
        elif ch == "\n":
            self._emit(self.cur_ts or ts)
            self.cur_ts = ts
        elif ch == "\x08":
            if self.cur:
                self.cur.pop()
        else:
            if not self.cur:
                self.cur_ts = ts
            self.cur.append(ch)

    def feed(self, ts: int, raw: str) -> None:
        buf, self.pending_esc = _split_incomplete_esc(self.pending_esc + raw)
        cleaned = _clean_chunk(buf)
        i, n = 0, len(cleaned)
        while i < n:
            ch = cleaned[i]
            if self.pending_cr:
                self.pending_cr = False
                if ch == "\n":
                    self._emit(self.cur_ts or self.pending_cr_ts)
                    self.cur_ts = ts
                    i += 1
                    continue
                self.cur.clear()
                self.cur_ts = self.pending_cr_ts
            if ch == "\r":
                if i + 1 < n and cleaned[i + 1] == "\n":
                    self._emit(self.cur_ts or ts)
                    self.cur_ts = ts
                    i += 2
                    continue
                if i + 1 == n:
                    self.pending_cr, self.pending_cr_ts = True, ts
                else:
                    self.cur.clear()
                    self.cur_ts = ts
            else:
                self._step(ch, ts)
            i += 1

    def finish(self, last_ts: int, final: bool = False) -> list[dict[str, Any]]:
        tail_text = "".join(self.cur).rstrip()[:MAX_LINE_LEN]
        if tail_text:
            tail_ts = self.cur_ts or last_ts
            if final:
                self._emit(tail_ts)
            elif self.lines and self.lines[-1]["text"] == tail_text and abs(tail_ts - self.lines[-1]["ts"]) < 2000:
                self.lines[-1]["ts"] = tail_ts
            else:
                self.lines.append(
                    {
                        "id": f"{self.session}.g{self.gen}:tail",
                        "role": "line",
                        "text": tail_text,
                        "ts": tail_ts,
                        "kind": "pane",
                        "partial": True,
                    }
                )
        else:
            while self.lines and not self.lines[-1]["text"]:
                self.lines.pop()
        return self.lines


def parse_chunks(chunks: list[tuple[int, str]], session: str, gen: int = 0, final: bool = False) -> list[dict[str, Any]]:
    """Turn timestamped raw PTY chunks into clean display lines with per-line epoch-ms timestamps."""
    builder = _LineBuilder(session, gen)
    for ts, raw in chunks:
        builder.feed(ts, raw)
    return builder.finish(chunks[-1][0] if chunks else 0, final=final)


def _load_file_entries(path: Path, session: str, final: bool = False) -> tuple[list[dict[str, Any]], int, int]:
    if not path.is_file() or path.is_symlink():
        return [], 0, -1
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
                meta = obj.get("meta")
                if isinstance(meta, dict) and isinstance(meta.get("gen"), int):
                    gen = meta["gen"]
                    continue
                ts = obj.get("ts")
                d = obj.get("d")
                if isinstance(d, str) and isinstance(ts, (int, float)):
                    its = int(ts)
                    if its > max_ts:
                        max_ts = its
                    chunks.append((its, d))
    except OSError:
        return [], 0, -1
    return parse_chunks(chunks, session, gen, final=final), max_ts, gen


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
            if b_gen != END_CURSOR_GEN:
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
    more = page[0]["id"] if start > 0 and page and not page[0].get("partial") else None
    return page, more


def _stat_inode(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _load_session_entries(cur: Path, prev: Path, session: str, need_prev: bool) -> tuple[list[dict[str, Any]], int, bool]:
    """Read cur (and prev when needed), retrying once if rotation swapped cur mid-read."""
    for attempt in range(2):
        ino_before = _stat_inode(cur)
        cur_entries, cur_ts, cur_gen = _load_file_entries(cur, session)
        has_prev = prev.is_file() and not prev.is_symlink() and prev.stat().st_size > 0
        if not need_prev and len(cur_entries) >= 1:
            return cur_entries, cur_ts, has_prev
        prev_entries, prev_ts, prev_gen = _load_file_entries(prev, session, final=True) if has_prev else ([], 0, -1)
        ino_after = _stat_inode(cur)
        rotated = ino_before != ino_after or (prev_entries and cur_entries and prev_gen >= cur_gen >= 0)
        if rotated and attempt == 0:
            continue
        return prev_entries + cur_entries, max(prev_ts, cur_ts), False
    return [], 0, False


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
    cur_only_ok = before is None
    entries, last_ts, has_unread_prev = _load_session_entries(cur, prev, session, need_prev=not cur_only_ok)
    if cur_only_ok and has_unread_prev and len(entries) < limit:
        entries, last_ts, has_unread_prev = _load_session_entries(cur, prev, session, need_prev=True)
    if before is not None and entries and entries[-1].get("partial"):
        entries = entries[:-1]
    if not last_ts and entries and cur.is_file():
        with contextlib.suppress(OSError):
            last_ts = int(cur.stat().st_mtime * 1000)
    tool, tool_input = detect_tool(entries)
    page, more = _slice_before(entries, before, limit)
    if more is None and has_unread_prev and page and not page[0].get("partial"):
        more = page[0]["id"]
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


def _path_owned(cwd: str, root: str, worktree_paths: list[str] | None = None) -> bool:
    if not _under_path(cwd, root):
        return False
    if not worktree_paths:
        return True
    target = root.rstrip("/")
    candidates = sorted({p.rstrip("/") for p in worktree_paths if p} | {target}, key=len, reverse=True)
    owner = next((w for w in candidates if _under_path(cwd, w)), None)
    return owner == target


def _session_mtime(d: Path, sess: str) -> float:
    mt = 0.0
    for p in (d / f"{sess}.jsonl", d / f"{sess}.jsonl.1"):
        if p.is_file() and not p.is_symlink():
            with contextlib.suppress(OSError):
                mt = max(mt, p.stat().st_mtime)
    return mt


def workspace_sessions(
    path: str,
    live_panes: list[dict[str, Any]] | None = None,
    log_dir: Path | None = None,
    worktree_paths: list[str] | None = None,
    agent_sessions: set[str] | None = None,
) -> list[str]:
    """Recorded tmux sessions belonging to workspace `path`, newest first."""
    d = log_dir or default_log_dir()
    if not d.is_dir() or d.is_symlink():
        return []
    if live_panes is None:
        try:
            import ade_basement

            live_panes = ade_basement.panes()
        except Exception:
            live_panes = []
    headers: dict[str, dict[str, Any]] = {}
    matched: set[str] = set()
    for f in d.glob("*.jsonl"):
        if f.is_symlink():
            continue
        with contextlib.suppress(ValueError):
            stem = valid_session(f.stem)
            meta = _read_header(f)
            headers[stem] = meta
            if _path_owned(str(meta.get("cwd") or ""), path, worktree_paths):
                matched.add(stem)
    for p in live_panes:
        s = p.get("session") or ""
        if not s or not _path_owned(p.get("cwd") or "", path, worktree_paths):
            continue
        with contextlib.suppress(ValueError):
            stem = valid_session(s)
            meta_cwd = str(headers.get(stem, {}).get("cwd") or "")
            # Allow live cwd matching only when the session didn't originate in another workspace.
            if not meta_cwd or _path_owned(meta_cwd, path, worktree_paths) or _under_path(path, meta_cwd):
                matched.add(stem)
    if agent_sessions is not None:
        matched = {s for s in matched if s in agent_sessions or bool(headers.get(s, {}).get("agent"))}
    existing = [s for s in matched if (d / f"{s}.jsonl").is_file() or (d / f"{s}.jsonl.1").is_file()]
    return sorted(existing, key=lambda s: _session_mtime(d, s), reverse=True)


def read_workspace(
    path: str,
    live_panes: list[dict[str, Any]] | None = None,
    before: str | None = None,
    limit: int = 200,
    log_dir: Path | None = None,
    worktree_paths: list[str] | None = None,
    agent_sessions: set[str] | None = None,
) -> dict[str, Any] | None:
    """Read paginated pane log across recorded sessions for `path`, newest first, or None if empty."""
    sessions = workspace_sessions(
        path, live_panes=live_panes, log_dir=log_dir, worktree_paths=worktree_paths, agent_sessions=agent_sessions
    )
    if not sessions:
        return None
    if before:
        m = CURSOR_RE.fullmatch(before)
        if not m or m.group(1) not in sessions:
            return None
        idx = sessions.index(m.group(1))
        cur_before: str | None = before
    else:
        idx = 0
        cur_before = None

    page: list[dict[str, Any]] = []
    more: str | None = None
    last_ts: int | None = None
    tool, tool_input, first_session = "", "", sessions[idx]
    while idx < len(sessions) and len(page) < limit:
        res = read_session(sessions[idx], before=cur_before, limit=limit - len(page), log_dir=log_dir)
        chunk = res["entries"]
        if last_ts is None and res["last_output_ts"]:
            last_ts = res["last_output_ts"]
        if not tool and res["tool"]:
            tool, tool_input = res["tool"], res["tool_input"]
        page[:0] = chunk
        if res["more"]:
            more = res["more"]
            break
        idx += 1
        cur_before = f"{sessions[idx]}.g{END_CURSOR_GEN}:{END_CURSOR_GEN}" if idx < len(sessions) else None
    if more is None and idx < len(sessions):
        more = f"{sessions[idx]}.g{END_CURSOR_GEN}:{END_CURSOR_GEN}"
    if not page and before is None:
        return None
    return {
        "entries": page,
        "more": more,
        "before": before,
        "recent": [],
        "source": "pane",
        "session": first_session,
        "last_output_ts": last_ts,
        "tool": tool,
        "tool_input": tool_input,
    }


def pipe_command(
    code_path: str,
    home_path: str,
    socket: str = "greenlight",
    session: str = "",
    cwd: str = "",
    agent: str = "",
    python: str = "python3",
) -> str:
    """Remote-safe POSIX shell command for tmux `pipe-pane -O` (no-ops cleanly if script is absent)."""
    sock = valid_socket(socket)
    script = tmux_format_escape(shlex.quote(f"{code_path.rstrip('/')}/scripts/pane_log.py"))
    home = tmux_format_escape(shlex.quote(home_path))
    py = tmux_format_escape(shlex.quote(python))
    if session:
        sess = tmux_format_escape(shlex.quote(valid_session(session)))
        target_args = f"--session {sess}"
        if cwd:
            target_args += f" --cwd {tmux_format_escape(shlex.quote(cwd))}"
        if agent:
            target_args += f" --agent {tmux_format_escape(shlex.quote(agent))}"
    else:
        target_args = f"--pane #{{pane_id}} --socket {tmux_format_escape(shlex.quote(sock))}"
    return f"[ -f {script} ] && GREENLIGHT_HOME={home} exec {py} {script} record {target_args}"


def attach(session: str, cwd: str = "", socket: str | None = None, code_dir: Path | None = None, agent: str = "") -> bool:
    """Idempotently attach `pipe-pane -O` to a tmux session if not already piping."""
    session = valid_session(session)
    try:
        sock = valid_socket(socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight")
    except ValueError:
        return False
    check = subprocess.run(
        [_tmux_bin(), "-L", sock, "display-message", "-p", "-t", f"={session}:", "#{pane_pipe}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check.returncode != 0 or check.stdout.strip() == "1":
        return check.returncode == 0
    root = (code_dir or Path(__file__).resolve().parent).parent
    if not (root / "scripts" / "pane_log.py").is_file():
        return False
    home = os.environ.get("GREENLIGHT_HOME") or str(Path.home() / ".greenlight")
    cmd = pipe_command(str(root), home, sock, session=session, cwd=cwd, agent=agent, python=sys.executable)
    res = subprocess.run(
        [_tmux_bin(), "-L", sock, "pipe-pane", "-O", "-t", f"={session}:", cmd],
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
        record(
            session=_arg(a, "--session"),
            cwd=_arg(a, "--cwd"),
            pane_id=_arg(a, "--pane"),
            socket=_arg(a, "--socket"),
            max_bytes=int(_arg(a, "--max-bytes", str(DEFAULT_MAX_BYTES))),
            agent=_arg(a, "--agent"),
        )
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
