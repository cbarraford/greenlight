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

import diag

SESSION_RE = re.compile(r"^mc-[\w-]{1,64}\Z")
PANE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,64}\Z")
SOCKET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}\Z")
WARM_PREFIX = "gl-warm-"
DEFAULT_TMUX_MARK = "\x1b]6973;greenlight-tmux\x07"
DEFAULT_RUN_MARK = "\x1b]6973;greenlight-run\x07"
FAST_PATH_DIRS = (
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/nix/var/nix/profiles/default/bin",
    "~/.nix-profile/bin",
)


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
        return cwd
    return home


def build_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = "Greenlight"
    cur = [p for p in env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":") if p]
    extra = [os.path.expanduser(d) for d in FAST_PATH_DIRS]
    prefix = [d for d in extra if d not in cur]
    env["PATH"] = ":".join(prefix + cur)
    if not any(env.get(k) for k in ("LANG", "LC_ALL", "LC_CTYPE")):
        env["LANG"] = "en_US.UTF-8"
    return env


def set_winsize(fd: int, cols: int, rows: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _attach_ctty() -> None:
    os.setsid()
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _split_tmux_cmds(cmds: list[str]) -> list[str]:
    argv: list[str] = []
    for raw in cmds:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            toks = shlex.split(raw)
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
        set_winsize(self.master_fd, cols, rows)

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        self.master_fd = -1
        if self.proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGHUP)
            with contextlib.suppress(OSError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    self.proc.kill()
                with contextlib.suppress(OSError):
                    self.proc.wait(timeout=0.5)


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

    def set_opts(self, tmux_opts: list[str], hooks: list[str]) -> None:
        self.opts_argv = _split_tmux_cmds([*tmux_opts, *hooks])

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
            self.warm_name = None
            self.warm_cwd = ""

    def warm(self, cwd: str) -> None:
        if not self.tmux_bin:
            return
        target = resolve_cwd(cwd)
        self._reap_proc()
        if self.warm_name and self.warm_cwd == target:
            if self.warm_proc is not None or self.has_session(self.warm_name):
                return
            self.warm_name = None
        if self.warm_name:
            old = self.warm_name
            self.warm_name = None
            self.warm_cwd = ""
            if self.warm_proc is not None and self.warm_proc.poll() is None:
                with contextlib.suppress(OSError):
                    self.warm_proc.terminate()
                self.warm_proc = None
            self.kill_session(old)
        name = f"{WARM_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
        argv = [*self._base(), "start-server", *self.opts_argv, ";", "new-session", "-d", "-s", name, "-c", target]
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
        except OSError:
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
                self.warm_name = None
                self.warm_cwd = ""
                return False
        name = self.warm_name
        self.warm_name = None
        self.warm_cwd = ""
        return self._run_quick(["rename-session", "-t", f"={name}", session], timeout=2.0) == 0

    def cleanup(self) -> None:
        if self.warm_proc is not None and self.warm_proc.poll() is None:
            with contextlib.suppress(OSError):
                self.warm_proc.terminate()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                self.warm_proc.wait(timeout=0.5)
        self.warm_proc = None
        if self.warm_name:
            name = self.warm_name
            self.warm_name = None
            self.warm_cwd = ""
            self.kill_session(name)


def _run_shell_cmd(shell: str, arg_expr: str, value: str) -> str:
    body = f'trap : INT; eval "{arg_expr}"; exec {shlex.quote(shell)} -l'
    return f"exec {shlex.quote(shell)} -lic {shlex.quote(body)} greenlight {shlex.quote(value)}"


class TermBridge:
    """Multiplexes PTY streams and a StandbyPool over a single JSONL stdio transport."""

    def __init__(self, socket: str | None = None, in_fd: int | None = None, out_fd: int | None = None) -> None:
        self.env = build_env()
        self.socket = valid_socket(socket or os.environ.get("GREENLIGHT_TMUX_SOCKET") or "greenlight")
        self.tmux_bin = shutil.which("tmux", path=self.env["PATH"])
        self.pool = StandbyPool(self.tmux_bin, self.socket, self.env)
        self.tmux_mark = DEFAULT_TMUX_MARK
        self.run_mark = DEFAULT_RUN_MARK
        self.in_fd = sys.stdin.fileno() if in_fd is None else in_fd
        self.out_fd = sys.stdout.fileno() if out_fd is None else out_fd
        self.sel = selectors.DefaultSelector()
        self.panes: dict[str, PaneProc] = {}
        self.fd_to_pane: dict[int, PaneProc] = {}
        self._in_buf = bytearray()
        self._stop = False
        self._configured_server = False

    def send(self, obj: dict[str, Any]) -> None:
        payload = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        view = memoryview(payload)
        sent = 0
        while sent < len(payload):
            try:
                n = os.write(self.out_fd, view[sent:])
                if n <= 0:
                    self._stop = True
                    return
                sent += n
            except BlockingIOError:
                time.sleep(0.002)
            except OSError:
                self._stop = True
                return

    def _emit_out(self, pane_id: str, data: bytes, mark: bool = False) -> None:
        if data:
            msg: dict[str, Any] = {"e": "out", "id": pane_id, "d": base64.b64encode(data).decode("ascii")}
            if mark:
                msg["mark"] = True
            self.send(msg)

    def _write_run_file(self, run: str) -> str | None:
        tmp_dir = self.env.get("TMPDIR") or tempfile.gettempdir()
        path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="greenlight-run.", dir=tmp_dir, delete=False
            ) as fh:
                path = fh.name
                os.chmod(path, 0o600)
                fh.write(run)
            return path
        except OSError:
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            return None

    def _build_launch_argv(
        self, pane_id: str, session: str, cwd: str, run: str
    ) -> tuple[list[str], bool]:
        shell = self.env.get("SHELL") or "/bin/zsh"
        if not self.tmux_bin:
            if run:
                self._emit_out(pane_id, self.run_mark.encode("utf-8"), mark=True)
                return ["/bin/sh", "-c", _run_shell_cmd(shell, "$1", run)], False
            return [shell, "-l"], False

        live = self.pool.has_session(session)
        if not run and not live and self.pool.claim(cwd, session):
            self._emit_out(pane_id, self.tmux_mark.encode("utf-8"), mark=True)
            return [self.tmux_bin, "-L", self.socket, "attach-session", "-t", f"={session}"], True

        run_cmd = ""
        if run:
            tmp_file = None if live else self._write_run_file(run)
            self._emit_out(pane_id, self.run_mark.encode("utf-8"), mark=True)
            if not live and not tmp_file:
                return ["/bin/sh", "-c", _run_shell_cmd(shell, "$1", run)], False
            run_cmd = _run_shell_cmd(
                shell,
                '$(command cat "$1" 2>/dev/null && command rm -f "$1")',
                tmp_file or "/dev/null/live",
            )

        self._emit_out(pane_id, self.tmux_mark.encode("utf-8"), mark=True)
        base = [self.tmux_bin, "-L", self.socket, "-f", "/dev/null"]
        if not self._configured_server or not live:
            self._configured_server = True
            argv = [*base, "start-server", *self.pool.opts_argv, ";", "new-session", "-A", "-s", session, "-c", cwd]
        else:
            argv = [*base, "new-session", "-A", "-s", session, "-c", cwd]
        if run_cmd:
            argv.append(run_cmd)
        return argv, False

    def _spawn_pty(self, pane_id: str, session: str, cwd: str, cols: int, rows: int, argv: list[str]) -> PaneProc:
        master_fd, slave_fd = os.openpty()
        try:
            set_winsize(slave_fd, cols, rows)
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
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
        self.fd_to_pane[master_fd] = pane
        self.sel.register(master_fd, selectors.EVENT_READ, pane)
        return pane

    def _drop_pane(self, pane_id: str, emit_exit: bool = False) -> None:
        pane = self.panes.pop(pane_id, None)
        if not pane:
            return
        if pane.master_fd >= 0:
            self.fd_to_pane.pop(pane.master_fd, None)
            with contextlib.suppress(KeyError, ValueError):
                self.sel.unregister(pane.master_fd)
        code = 0
        if emit_exit:
            try:
                rc = pane.proc.wait(timeout=0.5)
                code = rc if rc >= 0 else 128 + (-rc)
            except subprocess.TimeoutExpired:
                code = -1
        pane.stop()
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
        raw_run = msg.get("run")
        run = raw_run if isinstance(raw_run, str) else ""
        try:
            argv, warm = self._build_launch_argv(pane_id, session, cwd, run)
            self._spawn_pty(pane_id, session, cwd, cols, rows, argv)
            self.send({"e": "opened", "id": pane_id, "warm": warm})
            if not run:
                self.pool.warm(cwd)
        except OSError:
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
        if not data:
            return
        pane.write_buf.extend(data)
        self._flush_pane_write(pane)

    def _flush_pane_write(self, pane: PaneProc) -> None:
        if pane.master_fd < 0:
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
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if pane.write_buf else 0)
        with contextlib.suppress(KeyError, ValueError):
            self.sel.modify(pane.master_fd, events, pane)

    def _handle_size(self, msg: dict[str, Any]) -> None:
        pane = self.panes.get(str(msg.get("id") or ""))
        if pane:
            cols, rows = clamp_size(msg.get("cols"), msg.get("rows"))
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
        opts = [x for x in raw_opts if isinstance(x, str)] if isinstance(raw_opts, list) else []
        hooks = [x for x in raw_hooks if isinstance(x, str)] if isinstance(raw_hooks, list) else []
        self.pool.set_opts(opts, hooks)
        raw_tmux_mark = msg.get("tmux_mark")
        if isinstance(raw_tmux_mark, str) and raw_tmux_mark:
            self.tmux_mark = raw_tmux_mark
        raw_run_mark = msg.get("run_mark")
        if isinstance(raw_run_mark, str) and raw_run_mark:
            self.run_mark = raw_run_mark

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
        while True:
            nl = self._in_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._in_buf[:nl]).decode("utf-8", errors="replace").strip()
            del self._in_buf[: nl + 1]
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                self._handle_message(obj)

    def _read_pane(self, pane: PaneProc) -> None:
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

    def run(self) -> int:
        flags = fcntl.fcntl(self.in_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.in_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.sel.register(self.in_fd, selectors.EVENT_READ, "stdin")
        self.send({"e": "ready", "v": 1, "tmux": bool(self.tmux_bin)})
        try:
            while not self._stop:
                ready = self.sel.select(timeout=1.0)
                for key, mask in ready:
                    if key.data == "stdin":
                        self._read_stdin()
                    elif isinstance(key.data, PaneProc):
                        pane = key.data
                        if mask & selectors.EVENT_READ:
                            self._read_pane(pane)
                        if (mask & selectors.EVENT_WRITE) and pane.pane_id in self.panes:
                            self._flush_pane_write(pane)
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        for pane_id in list(self.panes):
            self._drop_pane(pane_id, emit_exit=False)
        with contextlib.suppress(KeyError, ValueError):
            self.sel.unregister(self.in_fd)
        self.sel.close()
        self.pool.cleanup()


def main() -> int:
    diag.install("term_bridge")
    bridge = TermBridge()

    def _on_sig(_signum: int, _frame: Any) -> None:
        bridge._stop = True

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)
    return bridge.run()


if __name__ == "__main__":
    raise SystemExit(main())
