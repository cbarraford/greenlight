"""The Basement's wheel against a real tmux: App.tmuxOpts (read out of App.swift) on a pty, fed what terminal.html sends.

tmux repaints only the last screen of a burst, so the page's own scrollback misses most output: the wheel has to reach
tmux's history. Skipped where there's no tmux.
"""
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UP, DOWN = b"\x1b[<64;10;10M", b"\x1b[<65;10;10M"
EXIT, CLEAR = b"\x1b[<93;1;1M", b"\x1b[<92;1;1M"  # terminal.html's HIST_EXIT, HIST_CLEAR

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")


def tmux_opts() -> list[str]:
    src = (ROOT / "app/Sources/App.swift").read_text()
    body = re.search(r"static let tmuxOpts = \[(.*?)\]\n", src, re.S).group(1)
    body = re.sub(r"//[^\n]*", "", body)
    return [re.sub(r"\\(.)", r"\1", s) for s in re.findall(r'"((?:[^"\\]|\\.)*)"', body)]


class Pane:
    def __init__(self, editor: str, sock: str = ""):
        self.sock = sock or f"gl-scroll-test-{os.getpid()}-{editor}"
        cmd = f"exec tmux -L {self.sock} -f /dev/null start-server \\; " + " \\; ".join(tmux_opts()) + " \\; new-session -A -s s1"
        pid, self.fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", PS1="$ ", SHELL="/bin/sh", EDITOR=editor, ENV="/dev/null")
            os.execvp("sh", ["sh", "-c", cmd])
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            self.out = b""
            self.pump(1.5)
        except BaseException:
            self.close()
            raise

    def pump(self, secs: float) -> None:
        end = time.time() + secs
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    self.out += os.read(self.fd, 65536)
                except OSError:
                    return

    def send(self, b: bytes, secs: float = 0.5) -> None:
        os.write(self.fd, b)
        self.pump(secs)

    def tmux(self, *a: str) -> str:
        return subprocess.run(["tmux", "-L", self.sock, *a], capture_output=True, text=True, timeout=10).stdout.strip()

    def fmt(self, f: str) -> str:
        return self.tmux("display", "-p", "-t", "s1", f)

    def close(self) -> None:
        try:
            subprocess.run(["tmux", "-L", self.sock, "kill-server"], capture_output=True, timeout=10)
        finally:
            os.close(self.fd)


@pytest.fixture(params=["nano", "vim"])  # vim in $EDITOR: tmux's copy-mode-vi table
def pane(request):
    p = Pane(request.param)
    yield p
    p.close()


def test_the_wheel_scrolls_tmux_history_a_line_at_a_time(pane):
    assert all(m in pane.out for m in (b"?1000h", b"?1006h")), "mouse on: the page sees the app asking for the mouse"
    pane.send(b"seq 1 300\r", 1.0)
    pane.send(UP * 10)
    assert pane.fmt("#{pane_in_mode} #{scroll_position}") == "1 9", "the first event enters copy-mode, the rest a line each"
    pane.send(DOWN * 4)
    assert pane.fmt("#{scroll_position}") == "5"
    pane.send(DOWN * 6)
    assert pane.fmt("#{pane_in_mode}") == "0", "down to the bottom leaves copy-mode"


def test_typing_after_a_wheel_up_leaves_copy_mode_and_reaches_the_shell(pane):
    pane.send(b"seq 1 100\r", 1.0)
    pane.send(UP * 3)
    assert pane.fmt("#{pane_in_mode}") == "1"
    pane.send(EXIT + b"echo TYPED\r", 0.8)
    assert pane.fmt("#{pane_in_mode}") == "0"
    assert "TYPED" in pane.tmux("capture-pane", "-p", "-t", "s1").splitlines()
    pane.send(EXIT + b"echo AGAIN\r", 0.8)
    lines = pane.tmux("capture-pane", "-p", "-t", "s1").splitlines()
    assert "AGAIN" in lines and not any("[<" in x for x in lines), "outside copy-mode the exit is a no-op"


def test_clear_drops_tmux_history(pane):
    pane.send(b"seq 1 100\r", 1.0)
    assert int(pane.fmt("#{history_size}")) > 50
    pane.send(UP * 2 + EXIT + CLEAR)
    assert pane.fmt("#{pane_in_mode} #{history_size}") == "0 0"


def test_the_alternate_screen_gets_arrow_keys(pane, tmp_path):
    got = tmp_path / "keys"
    pane.send(f"printf '\\033[?1049h'; od -c > {got}; printf '\\033[?1049l'\r".encode(), 0.8)
    assert pane.fmt("#{alternate_on}") == "1"
    pane.send(UP)
    assert pane.fmt("#{pane_in_mode}") == "0", "no copy-mode on the alternate screen"
    pane.send(b"\r\x04", 0.8)
    assert re.search(r"033\s+\[\s+A", got.read_text()), "the wheel arrived as ↑"


def test_a_reattach_leaves_copy_mode(pane):
    pane.send(b"seq 1 100\r", 1.0)
    pane.send(UP * 3)
    assert pane.fmt("#{pane_in_mode}") == "1"
    pane.tmux("detach-client", "-s", "s1")  # the app quits or reloads: the session lives on, still in copy-mode
    pane.pump(0.5)
    os.close(pane.fd)
    again = Pane("nano", pane.sock)  # the next launch: new-session -A, and a page that sent no wheel up
    pane.fd = again.fd
    assert pane.fmt("#{pane_in_mode}") == "0"
    again.send(b"echo BACK\r", 0.8)
    assert "BACK" in pane.tmux("capture-pane", "-p", "-t", "s1").splitlines(), "the first keys reach the shell"


def test_the_page_and_the_app_agree_on_the_tmux_mark():
    swift = re.search(r'static let tmuxMark = "(.*?)"\n', (ROOT / "app/Sources/App.swift").read_text()).group(1)
    page = re.search(r'const TMUX_MARK = "(.*?)";', (ROOT / "app/terminal.html").read_text()).group(1)
    printed = swift.replace("\\\\", "\\").replace("\\033", "\x1b").replace("\\007", "\x07")  # printf's escapes
    assert printed == page.replace("\\x1b", "\x1b").replace("\\x07", "\x07")
    source = (ROOT / "app/Sources/App.swift").read_text()
    assert "unset TMUX; printf '\\(App.tmuxMark)'; " in source, "printed before attaching or starting tmux"
    assert "exec \\(App.tmux) start-server" in source, "our options apply before -A attaches a live session"


def test_the_page_and_the_app_agree_on_the_run_mark():
    swift = re.search(r'static let runMark = "(.*?)"\n', (ROOT / "app/Sources/App.swift").read_text()).group(1)
    page = re.search(r'const RUN_MARK = "(.*?)";', (ROOT / "app/terminal.html").read_text()).group(1)
    printed = swift.replace("\\\\", "\\").replace("\\033", "\x1b").replace("\\007", "\x07")  # printf's escapes
    assert printed == page.replace("\\x1b", "\x1b").replace("\\x07", "\x07")
    body = re.search(r"func paneCmd\(.*?\n    \}\n", (ROOT / "app/Sources/App.swift").read_text(), re.S).group(0)
    # one runMark, on every path: after the if/elif chain that saves the brief closes, just before this shell would run it
    assert body.count("printf '\\(App.runMark)'") == 1
    pinned = re.search(r'fi; "\s*\+\s*"printf \'\\\(App\.runMark\)\'; \[ -n \\"\$f\\" \] \|\|', body)
    assert pinned and 0 <= body.find('>| \\"$f\\"') < pinned.start(), "the mark after the brief is saved, on every path, before this shell would run it"


def test_pane_cmd_and_launch_skip_outer_login_shells_and_support_ssh_mux():
    src = (ROOT / "app/Sources/App.swift").read_text()
    body = re.search(r"func paneCmd\(.*?\n    \}\n", src, re.S).group(0)
    assert "App.fastPathPrefix" in body, "paneCmd prepends standard tool dirs so sh finds tmux without a login shell"
    assert '"exec sh -c "' in body and '"exec $SHELL -lc "' not in body, "remote paneCmd uses sh -c, not a redundant login shell"
    launch = re.search(r"func paneLaunch\(.*?\n    \}\n", src, re.S).group(0)
    assert '"/bin/sh"' in launch and '"/usr/bin/ssh"' in launch
    assert "SshMux.slaveArgs" in launch and "SshMux.directArgs" in launch
    assert "final class SshMux" in src and "NSWorkspace.didWakeNotification" in src and "NWPathMonitor" in src
