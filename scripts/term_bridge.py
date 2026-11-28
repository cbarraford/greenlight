#!/usr/bin/env python3
"""term_bridge: single persistent multiplexed PTY/tmux bridge + standby shell pool for Greenlight.

Runs one long-lived process per host (locally or over a single SSH stdio stream) so opening a new
terminal tab needs zero local forkpty/ssh spawns and can claim a pre-warmed `gl-warm-*` tmux
session for the active workspace via `tmux rename-session` (~6 ms on host + 1 RTT).

Protocol (line-delimited JSON on stdin/stdout; binary PTY payloads are base64-encoded):
  bridge -> app: {"e":"ready","v":1,"tmux":true}
                 {"e":"opened","id":"0:1","warm":true}
                 {"e":"out","id":"0:1","d":"<b64>"}
                 {"e":"exit","id":"0:1","code":0}
                 {"e":"pong"}
  app -> bridge: {"op":"config","tmux_opts":[...],"hooks":[...],"tmux_mark":"...","run_mark":"..."}
                 {"op":"open","id":"0:1","session":"mc-...","cwd":"/path","cols":80,"rows":24,"run":""}
                 {"op":"in","id":"0:1","d":"<b64>"}
                 {"op":"size","id":"0:1","cols":120,"rows":40}
                 {"op":"close","id":"0:1","session":"mc-...","kill":true}
                 {"op":"warm","cwd":"/path"}
                 {"op":"kill","session":"mc-..."}
                 {"op":"ping"}

Stdlib only. Quitting the bridge detaches user `mc-*` sessions (never kills them) and cleans up
only its own unused `gl-warm-*` standby session.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import selectors
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from typing import Any

import bins
import diag

SESSION_RE = re.compile(r"^mc-[\w-]{1,64}\Z")
PANE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,64}\Z")
SOCKET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")
WARM_PREFIX = "gl-warm-"
WARM_PID_RE = re.compile(r"^gl-warm-(\d+)-[0-9a-f]+\Z")
DEFAULT_TMUX_MARK = "\x1b]6973;greenlight-tmux\x07"
DEFAULT_RUN_MARK = "\x1b]6973;greenlight-run\x07"
NON_POSIX_SHELLS = frozenset({"fish", "csh", "tcsh", "nu", "xonsh"})
MAX_IN_BUF = 4 * 1024 * 1024
MAX_WRITE_BUF = 1 * 1024 * 1024
OUT_HIGH_WATER = 2 * 1024 * 1024
OUT_LOW_WATER = 512 * 1024
FAST_PATH_DIRS = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/nix/var/nix/profiles/default/bin",
    "~/.nix-profile/bin",
)


def sanitize_text(val: Any) -> str:
    if not isinstance(val, str):
        return ""
    return val.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")


def valid_session(name: Any) -> str | None:
    return name if isinstance(name, str) and SESSION_RE.fullmatch(name) else None


def valid_pane_id(pane_id: Any) -> str | None:
    return pane_id if isinstance(pane_id, str) and PANE_ID_RE.fullmatch(pane_id) else None


def valid_socket(name: str) -> str:
    if not name or name[0] in ".-" or ".." in name or not SOCKET_RE.fullmatch(name):
        raise ValueError(f"invalid tmux socket: {name!r}")
    return name


def clamp_size(cols: Any, rows: Any) -> tuple[int, int]:
    c = cols if isinstance(cols, int) and not isinstance(cols, bool) else 80
    r = rows if isinstance(rows, int) and not isinstance(rows, bool) else 24
    return max(10, min(c, 65535)), max(2, min(r, 65535))


def resolve_cwd(cwd: Any) -> str:
    home = os.path.expanduser("~")
    if isinstance(cwd, str) and cwd.startswith("/") and ".." not in cwd.split("/") and os.path.isdir(cwd):
        return os.path.normpath(cwd)
    return home


def default_shell(env: dict[str, str]) -> str:
    shell = sanitize_text(env.get("SHELL")).strip()
    if shell:
        return shell
    return "/bin/zsh" if os.access("/bin/zsh", os.X_OK) else "/bin/sh"


_SYS_PATH_DIRS = frozenset({"/usr/bin", "/bin", "/usr/sbin", "/sbin"})


def build_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.pop("GREENLIGHT_RUN", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = "Greenlight"
    cur = [p for p in (env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin").split(":") if p]
    extra = list(dict.fromkeys(os.path.expanduser(d) for d in FAST_PATH_DIRS))
    extra_set = set(extra)
    rest = [p for p in cur if p not in extra_set]
    # first ahead of system dirs even when diag.install's bins.ensure_path appended them, without demoting user venv/pyenv prefixes
    sys_idx = next((i for i, p in enumerate(rest) if p in _SYS_PATH_DIRS), len(rest))
    env["PATH"] = ":".join(rest[:sys_idx] + extra + rest[sys_idx:])
    if not any(env.get(k) for k in ("LANG", "LC_ALL", "LC_CTYPE")):
        env["LANG"] = "en_US.UTF-8"
    return env


def set_winsize(fd: int, cols: int, rows: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _set_nonblock(fd: int) -> None:
    with contextlib.suppress(OSError):
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _attach_ctty() -> None:
    os.setsid()
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _split_tmux_cmds(cmds: list[str]) -> list[str]:
    argv: list[str] = []
    for raw in cmds:
        clean = sanitize_text(raw)
        if not clean.strip():
            continue
        try:
            toks = shlex.split(clean)
        except ValueError:
            continue
        if toks:
            argv.extend([";", *toks])
    return argv


class PaneProc:
    """One attached PTY child process multiplexed by TermBridge."""

    def __init__(self, pane_id: str, session: str, master_fd: int, proc: subprocess.Popen[bytes]) -> None:
        self.pane_id = pane_id
        self.session = session
        self.master_fd = master_fd
        self.proc = proc
        self.write_buf = bytearray()

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd >= 0:
            set_winsize(self.master_fd, cols, rows)

    def close_master(self) -> None:
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1

    def terminate_group(self) -> None:
        if self.proc.poll() is not None:
            return
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGHUP)
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            self.proc.terminate()

    def kill_group(self) -> None:
        if self.proc.poll() is not None:
            return
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.proc.kill()

    def stop(self) -> None:
        self.close_master()
        if self.proc.poll() is None:
            self.terminate_group()
            try:
                self.proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.kill_group()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    self.proc.wait(timeout=0.3)


class StandbyPool:
    """Keeps at most one pre-warmed `gl-warm-*` tmux session for the active workspace."""

    def __init__(self, tmux_bin: str | None, socket: str, env: dict[str, str]) -> None:
        self.tmux_bin = tmux_bin
        self.socket = socket
        self.env = env
        self.opts_argv: list[str] = []
        self.warm_name: str | None = None
        self.warm_cwd: str = ""
        self.warm_proc: subprocess.Popen[bytes] | None = None
        self.last_size: tuple[int, int] = (80, 24)
        self._retired: list[tuple[subprocess.Popen[bytes], str]] = []
        self._swept = False

    def set_opts(self, tmux_opts: list[str], hooks: list[str]) -> None:
        self.opts_argv = _split_tmux_cmds([*tmux_opts, *hooks])

    def set_size(self, cols: int, rows: int) -> None:
        self.last_size = (cols, rows)

    def _base(self) -> list[str]:
        assert self.tmux_bin is not None
        return [self.tmux_bin, "-L", self.socket, "-f", "/dev/null"]

    def _run_quick(self, args: list[str], timeout: float = 3.0) -> int:
        if not self.tmux_bin:
            return 127
        try:
            r = subprocess.run(
                [self.tmux_bin, "-L", self.socket, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
                timeout=timeout,
            )
            return r.returncode
        except (OSError, subprocess.SubprocessError):
            return 1

    def _list_sessions(self) -> list[str]:
        if not self.tmux_bin:
            return []
        try:
            r = subprocess.run(
                [self.tmux_bin, "-L", self.socket, "list-sessions", "-F", "#{session_name}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
                text=True,
                timeout=2.0,
            )
            return r.stdout.splitlines() if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            return []

    def sweep_stale(self) -> None:
        if self._swept or not self.tmux_bin:
            return
        self._swept = True
        me = os.getpid()
        for name in self._list_sessions():
            m = WARM_PID_RE.fullmatch(name)
            if not m:
                continue
            pid = int(m.group(1))
            if pid <= 0 or pid == me:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                self.kill_session(name)
            except OSError:
                pass

    def has_session(self, session: str) -> bool:
        return self._run_quick(["has-session", "-t", f"={session}"], timeout=2.0) == 0

    def kill_session(self, session: str) -> None:
        if session and self.tmux_bin:
            self._run_quick(["kill-session", "-t", f"={session}"], timeout=2.0)

    def _reap_proc(self) -> None:
        if self.warm_proc is None:
            return
        rc = self.warm_proc.poll()
        if rc is None:
            return
        self.warm_proc = None
        if rc != 0:
            name = self.warm_name
            self.warm_name = None
            self.warm_cwd = ""
            if name and self.has_session(name):
                self.kill_session(name)

    def tick(self) -> None:
        self._reap_proc()
        if not self._retired:
            return
        still: list[tuple[subprocess.Popen[bytes], str]] = []
        for proc, name in self._retired:
            if proc.poll() is None:
                still.append((proc, name))
            else:
                self.kill_session(name)
        self._retired = still

    def _retire_current(self) -> None:
        if not self.warm_name:
            return
        old = self.warm_name
        old_proc = self.warm_proc
        self.warm_name = None
        self.warm_cwd = ""
        self.warm_proc = None
        if old_proc is not None and old_proc.poll() is None:
            with contextlib.suppress(OSError):
                old_proc.terminate()
            self._retired.append((old_proc, old))
        else:
            self.kill_session(old)

    def warm(self, cwd: str) -> None:
        if not self.tmux_bin:
            return
        target = resolve_cwd(cwd)
        self.tick()
        if self.warm_name and self.warm_cwd == target:
            if self.warm_proc is not None or self.has_session(self.warm_name):
                return
            self.warm_name = None
        self._retire_current()
        name = f"{WARM_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
        cols, rows = self.last_size
        argv = [
            *self._base(),
            "start-server",
            *self.opts_argv,
            ";",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            target,
            "-x",
            str(cols),
            "-y",
            str(rows),
        ]
        try:
            self.warm_proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
                cwd=target,
                close_fds=True,
            )
            self.warm_name = name
            self.warm_cwd = target
        except (OSError, ValueError):
            self.warm_name = None
            self.warm_cwd = ""
            self.warm_proc = None

    def claim(self, cwd: str, session: str) -> bool:
        if not self.tmux_bin or not self.warm_name:
            return False
        target = resolve_cwd(cwd)
        if self.warm_cwd != target:
            return False
        if self.warm_proc is not None:
            try:
                rc = self.warm_proc.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                return False
            self.warm_proc = None
            if rc != 0:
                name = self.warm_name
                self.warm_name = None
                self.warm_cwd = ""
                if name and self.has_session(name):
                    self.kill_session(name)
                return False
        name = self.warm_name
        self.warm_name = None
        self.warm_cwd = ""
        if self._run_quick(["rename-session", "-t", f"={name}", session], timeout=2.0) == 0:
            return True
        if self.has_session(name):
            self.kill_session(name)
        return False

    def cleanup(self) -> None:
        if self.warm_proc is not None:
            if self.warm_proc.poll() is None:
                with contextlib.suppress(OSError):
                    self.warm_proc.terminate()
                try:
                    self.warm_proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        self.warm_proc.kill()
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        self.warm_proc.wait(timeout=0.3)
                except OSError:
                    pass
            self.warm_proc = None
        if self.warm_name:
            name = self.warm_name
            self.warm_name = None
            self.warm_cwd = ""
            self.kill_session(name)
        for proc, name in self._retired:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.terminate()
                try:
                    proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        proc.kill()
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        proc.wait(timeout=0.3)
            self.kill_session(name)
        self._retired.clear()


def _run_shell_cmd(shell: str, arg_expr: str, value: str) -> str:
    body = f'trap : INT; eval "{arg_expr}"; exec {shlex.quote(shell)} -l'
    if os.path.basename(shell) in NON_POSIX_SHELLS:
        return f"exec /bin/sh -c {shlex.quote(body)} greenlight {shlex.quote(value)}"
    return f"exec {shlex.quote(shell)} -lic {shlex.quote(body)} greenlight {shlex.quote(value)}"


class TermBridge:
    """Multiplexes PTY streams and a StandbyPool over a single JSONL stdio transport."""

    def __init__(
        self,
        socket: str | None = None,
        in_fd: int | None = None,
        out_fd: int | None = None,
        wake_fd: int | None = None,
    ) -> None:
        self.env = build_env()
        self.socket = valid_socket(socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight")
        ov = bins.configured("tmux")
        if ov:
            cand = ov if os.sep in ov else shutil.which(ov, path=self.env["PATH"]) or bins.find(ov)
            self.tmux_bin = cand if cand and bins.is_executable(cand) else None
        else:
            self.tmux_bin = shutil.which("tmux", path=self.env["PATH"])
        self.pool = StandbyPool(self.tmux_bin, self.socket, self.env)
        self.tmux_mark = DEFAULT_TMUX_MARK
        self.run_mark = DEFAULT_RUN_MARK
        self.in_fd = sys.stdin.fileno() if in_fd is None else in_fd
        self.out_fd = sys.stdout.fileno() if out_fd is None else out_fd
        self.wake_fd = wake_fd
        self.sel = selectors.DefaultSelector()
        self.panes: dict[str, PaneProc] = {}
        self._in_buf = bytearray()
        self._in_drop_until_nl = False
        self._out_buf = bytearray()
        self._out_registered = False
        self._paused_read = False
        self._reaping: list[tuple[PaneProc, float]] = []
        self._stop = False

    def _update_pane_events(self, pane: PaneProc) -> None:
        if pane.master_fd < 0 or self.panes.get(pane.pane_id) is not pane:
            return
        events = (0 if self._paused_read else selectors.EVENT_READ) | (selectors.EVENT_WRITE if pane.write_buf else 0)
        with contextlib.suppress(KeyError, ValueError):
            try:
                self.sel.get_key(pane.master_fd)
                registered = True
            except KeyError:
                registered = False
            if events and registered:
                self.sel.modify(pane.master_fd, events, pane)
            elif events and not registered:
                self.sel.register(pane.master_fd, events, pane)
            elif not events and registered:
                self.sel.unregister(pane.master_fd)

    def _set_read_pause(self, pause: bool) -> None:
        if self._paused_read == pause:
            return
        self._paused_read = pause
        for pane in list(self.panes.values()):
            self._update_pane_events(pane)

    def _sync_out_registration(self) -> None:
        want = bool(self._out_buf) and not self._stop
        if want and not self._out_registered:
            with contextlib.suppress(KeyError, ValueError, OSError):
                self.sel.register(self.out_fd, selectors.EVENT_WRITE, "stdout")
                self._out_registered = True
        elif not want and self._out_registered:
            with contextlib.suppress(KeyError, ValueError):
                self.sel.unregister(self.out_fd)
            self._out_registered = False

    def _flush_out(self) -> None:
        while self._out_buf and not self._stop:
            try:
                n = os.write(self.out_fd, self._out_buf)
                if n <= 0:
                    self._stop = True
                    break
                del self._out_buf[:n]
            except BlockingIOError:
                break
            except OSError:
                self._stop = True
                break
        if len(self._out_buf) >= OUT_HIGH_WATER:
            self._set_read_pause(True)
        elif len(self._out_buf) <= OUT_LOW_WATER:
            self._set_read_pause(False)
        self._sync_out_registration()

    def send(self, obj: dict[str, Any]) -> None:
        if self._stop:
            return
        payload = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        self._out_buf.extend(payload)
        self._flush_out()

    def _emit_out(self, pane_id: str, data: bytes, mark: bool = False) -> None:
        if data:
            msg: dict[str, Any] = {"e": "out", "id": pane_id, "d": base64.b64encode(data).decode("ascii")}
            if mark:
                msg["mark"] = True
            self.send(msg)

    def _write_run_file(self, run: str) -> str | None:
        dirs: list[str] = []
        env_tmp = self.env.get("TMPDIR")
        if env_tmp:
            dirs.append(env_tmp)
        sys_tmp = tempfile.gettempdir()
        if sys_tmp not in dirs:
            dirs.append(sys_tmp)
        clean = sanitize_text(run)
        for tmp_dir in dirs:
            path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", prefix="greenlight-run.", dir=tmp_dir, delete=False
                ) as fh:
                    path = fh.name
                    os.chmod(path, 0o600)
                    fh.write(clean)
                return path
            except (OSError, UnicodeError):
                if path:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
        return None

    def _build_launch_argv(
        self, session: str, cwd: str, run: str
    ) -> tuple[list[str], bool, list[bytes], str | None]:
        shell = default_shell(self.env)
        marks: list[bytes] = []
        if not self.tmux_bin:
            if run:
                marks.append(self.run_mark.encode("utf-8", errors="replace"))
                return ["/bin/sh", "-c", _run_shell_cmd(shell, "$1", run)], False, marks, None
            return [shell, "-l"], False, marks, None

        live = self.pool.has_session(session)
        if not run and not live and self.pool.claim(cwd, session):
            marks.append(self.tmux_mark.encode("utf-8", errors="replace"))
            return [self.tmux_bin, "-L", self.socket, "attach-session", "-t", f"={session}"], True, marks, None

        run_cmd = ""
        tmp_file: str | None = None
        if run:
            tmp_file = None if live else self._write_run_file(run)
            marks.append(self.run_mark.encode("utf-8", errors="replace"))
            if not live and not tmp_file:
                return ["/bin/sh", "-c", _run_shell_cmd(shell, "$1", run)], False, marks, None
            run_cmd = _run_shell_cmd(
                shell,
                '$(command cat "$1" 2>/dev/null && command rm -f "$1")',
                tmp_file or "/dev/null/live",
            )

        marks.append(self.tmux_mark.encode("utf-8", errors="replace"))
        base = [self.tmux_bin, "-L", self.socket, "-f", "/dev/null"]
        argv = [*base, "start-server", *self.pool.opts_argv, ";", "new-session", "-A", "-s", session, "-c", cwd]
        if run_cmd:
            argv.append(run_cmd)
        return argv, False, marks, tmp_file

    def _spawn_pty(self, pane_id: str, session: str, cwd: str, cols: int, rows: int, argv: list[str]) -> PaneProc:
        master_fd, slave_fd = os.openpty()
        try:
            set_winsize(slave_fd, cols, rows)
            _set_nonblock(master_fd)
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=self.env,
                preexec_fn=_attach_ctty,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        pane = PaneProc(pane_id, session, master_fd, proc)
        self.panes[pane_id] = pane
        if not self._paused_read:
            self.sel.register(master_fd, selectors.EVENT_READ, pane)
        return pane

    def _reap_exiting(self) -> None:
        if not self._reaping:
            return
        now = time.monotonic()
        still: list[tuple[PaneProc, float]] = []
        for pane, deadline in self._reaping:
            if pane.proc.poll() is not None:
                continue
            if now >= deadline:
                pane.kill_group()
            still.append((pane, deadline))
        self._reaping = still

    def _drop_pane(self, pane_id: str, emit_exit: bool = False) -> None:
        pane = self.panes.pop(pane_id, None)
        if not pane:
            return
        if pane.master_fd >= 0:
            with contextlib.suppress(KeyError, ValueError):
                self.sel.unregister(pane.master_fd)
        pane.close_master()
        code = 0
        if emit_exit:
            try:
                rc = pane.proc.wait(timeout=0.15)
                code = rc if rc >= 0 else 128 + (-rc)
            except subprocess.TimeoutExpired:
                code = -1
        if pane.proc.poll() is None:
            pane.terminate_group()
            self._reaping.append((pane, time.monotonic() + 0.5))
        if emit_exit:
            self.send({"e": "exit", "id": pane_id, "code": code})

    def _handle_open(self, msg: dict[str, Any]) -> None:
        pane_id = valid_pane_id(msg.get("id"))
        session = valid_session(msg.get("session"))
        if not pane_id or not session:
            return
        self._drop_pane(pane_id, emit_exit=False)
        cwd = resolve_cwd(msg.get("cwd"))
        cols, rows = clamp_size(msg.get("cols"), msg.get("rows"))
        self.pool.set_size(cols, rows)
        run = sanitize_text(msg.get("run"))
        tmp_file: str | None = None
        try:
            argv, warm, marks, tmp_file = self._build_launch_argv(session, cwd, run)
            self._spawn_pty(pane_id, session, cwd, cols, rows, argv)
            for mark in marks:
                self._emit_out(pane_id, mark, mark=True)
            self.send({"e": "opened", "id": pane_id, "warm": warm})
            if not run:
                self.pool.warm(cwd)
        except (OSError, ValueError):
            if tmp_file:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_file)
            self.send({"e": "exit", "id": pane_id, "code": 127})

    def _handle_in(self, msg: dict[str, Any]) -> None:
        pane = self.panes.get(str(msg.get("id") or ""))
        raw = msg.get("d")
        if not pane or not isinstance(raw, str):
            return
        try:
            data = base64.b64decode(raw, validate=True)
        except ValueError:
            return
        if not data or len(pane.write_buf) + len(data) > MAX_WRITE_BUF:
            return
        pane.write_buf.extend(data)
        self._flush_pane_write(pane)

    def _flush_pane_write(self, pane: PaneProc) -> None:
        if pane.master_fd < 0 or self.panes.get(pane.pane_id) is not pane:
            return
        while pane.write_buf:
            try:
                n = os.write(pane.master_fd, pane.write_buf)
                if n > 0:
                    del pane.write_buf[:n]
                else:
                    break
            except BlockingIOError:
                break
            except OSError:
                self._drop_pane(pane.pane_id, emit_exit=True)
                return
        self._update_pane_events(pane)

    def _handle_size(self, msg: dict[str, Any]) -> None:
        pane = self.panes.get(str(msg.get("id") or ""))
        if pane:
            cols, rows = clamp_size(msg.get("cols"), msg.get("rows"))
            self.pool.set_size(cols, rows)
            pane.resize(cols, rows)

    def _handle_close(self, msg: dict[str, Any]) -> None:
        pane_id = valid_pane_id(msg.get("id"))
        if pane_id:
            self._drop_pane(pane_id, emit_exit=False)
        session = valid_session(msg.get("session"))
        if msg.get("kill") and session:
            self.pool.kill_session(session)

    def _handle_kill(self, msg: dict[str, Any]) -> None:
        session = valid_session(msg.get("session"))
        if session:
            self.pool.kill_session(session)

    def _handle_config(self, msg: dict[str, Any]) -> None:
        raw_opts = msg.get("tmux_opts")
        raw_hooks = msg.get("hooks")
        opts = [sanitize_text(x) for x in raw_opts if isinstance(x, str)] if isinstance(raw_opts, list) else []
        hooks = [sanitize_text(x) for x in raw_hooks if isinstance(x, str)] if isinstance(raw_hooks, list) else []
        self.pool.set_opts(opts, hooks)
        tmux_mark = sanitize_text(msg.get("tmux_mark"))
        if tmux_mark:
            self.tmux_mark = tmux_mark
        run_mark = sanitize_text(msg.get("run_mark"))
        if run_mark:
            self.run_mark = run_mark

    def _handle_message(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "open":
            self._handle_open(msg)
        elif op == "in":
            self._handle_in(msg)
        elif op == "size":
            self._handle_size(msg)
        elif op == "close":
            self._handle_close(msg)
        elif op == "warm":
            self.pool.warm(resolve_cwd(msg.get("cwd")))
        elif op == "kill":
            self._handle_kill(msg)
        elif op == "config":
            self._handle_config(msg)
        elif op == "ping":
            self.send({"e": "pong"})

    def _read_stdin(self) -> None:
        try:
            chunk = os.read(self.in_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            self._stop = True
            return
        if not chunk:
            self._stop = True
            return
        self._in_buf.extend(chunk)
        while not self._stop:
            nl = self._in_buf.find(b"\n")
            if nl < 0:
                if len(self._in_buf) > MAX_IN_BUF:
                    self._in_buf.clear()
                    self._in_drop_until_nl = True
                break
            raw_line = bytes(self._in_buf[:nl])
            del self._in_buf[: nl + 1]
            if self._in_drop_until_nl:
                self._in_drop_until_nl = False
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                try:
                    self._handle_message(obj)
                except Exception as exc:
                    diag.event("bridge_err", **diag.failure(exc))

    def _read_pane(self, pane: PaneProc) -> None:
        if pane.master_fd < 0 or self.panes.get(pane.pane_id) is not pane:
            return
        try:
            chunk = os.read(pane.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in (errno.EINTR, errno.EAGAIN):
                return
            self._drop_pane(pane.pane_id, emit_exit=True)
            return
        if not chunk:
            self._drop_pane(pane.pane_id, emit_exit=True)
            return
        self._emit_out(pane.pane_id, chunk)

    def _drain_wake(self) -> None:
        if self.wake_fd is None:
            return
        with contextlib.suppress(OSError):
            os.read(self.wake_fd, 256)

    def _dispatch_event(self, key: selectors.SelectorKey, mask: int) -> None:
        data = key.data
        if data == "stdin":
            self._read_stdin()
        elif data == "stdout":
            self._flush_out()
        elif data == "wake":
            self._drain_wake()
        elif isinstance(data, PaneProc):
            if self.panes.get(data.pane_id) is not data:
                return
            if mask & selectors.EVENT_READ:
                self._read_pane(data)
            if (mask & selectors.EVENT_WRITE) and self.panes.get(data.pane_id) is data:
                self._flush_pane_write(data)

    def run(self) -> int:
        _set_nonblock(self.in_fd)
        _set_nonblock(self.out_fd)
        self.sel.register(self.in_fd, selectors.EVENT_READ, "stdin")
        if self.wake_fd is not None:
            _set_nonblock(self.wake_fd)
            self.sel.register(self.wake_fd, selectors.EVENT_READ, "wake")
        self.pool.sweep_stale()
        self.send({"e": "ready", "v": 1, "tmux": bool(self.tmux_bin)})
        try:
            while not self._stop:
                ready = self.sel.select(timeout=0.5)
                for key, mask in ready:
                    self._dispatch_event(key, mask)
                    if self._stop:
                        break
                self.pool.tick()
                self._reap_exiting()
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        for pane_id in list(self.panes):
            pane = self.panes.pop(pane_id, None)
            if not pane:
                continue
            if pane.master_fd >= 0:
                with contextlib.suppress(KeyError, ValueError):
                    self.sel.unregister(pane.master_fd)
            pane.stop()
        for pane, _ in self._reaping:
            pane.stop()
        self._reaping.clear()
        if self._out_registered:
            with contextlib.suppress(KeyError, ValueError):
                self.sel.unregister(self.out_fd)
            self._out_registered = False
        with contextlib.suppress(KeyError, ValueError):
            self.sel.unregister(self.in_fd)
        if self.wake_fd is not None:
            with contextlib.suppress(KeyError, ValueError):
                self.sel.unregister(self.wake_fd)
        self.sel.close()
        self.pool.cleanup()


def main() -> int:
    diag.install("term_bridge")
    wake_r, wake_w = os.pipe()
    _set_nonblock(wake_r)
    _set_nonblock(wake_w)
    prev_wake = signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    bridge = TermBridge(wake_fd=wake_r)

    def _on_sig(_signum: int, _frame: Any) -> None:
        bridge._stop = True

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)
    try:
        return bridge.run()
    finally:
        signal.set_wakeup_fd(prev_wake)
        for fd in (wake_r, wake_w):
            with contextlib.suppress(OSError):
                os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
