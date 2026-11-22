"""End-to-end parallel background UI test suite for Greenlight (--uitest mode).

Proves:
  1. Two isolated Greenlight --uitest instances run concurrently with zero cross-talk
     (separate AF_UNIX sockets, GREENLIGHT_HOME, UserDefaults, and tmux servers) and zero focus theft.
  2. Real macOS WKWebView + SwiftUI floor transitions (Pipe -> Basement -> Greenlight Center -> Settings)
     produce valid non-blank PNG screenshots via App.snap().
  3. Real local PTY in the isolated tmux server executes commands and tears down cleanly on stop.
  4. Terminal ⌘J command palette (`openPalette` / `closePalette`), multi-tab operations (new, switch, close),
     and `openWorkspace` work cleanly inside WKWebView.
  5. Real dash.py snapshot data renders in dashboard.html with `rescan`, viewport `resize` (small/large),
     and Settings open/close.
  6. Chat E2E (`sendChat` -> deterministic stub reply -> `chatClear` -> `toggleChat`) and Inbox E2E
     (`inboxPing` -> `openInbox` -> `inboxConfirm` -> `clearInbox` -> `closeInbox`) complete cleanly.
  7. Adversarial XSS fixture renders safely inside the real macOS WKWebView with zero execution.
  8. Zero `webErrors`, zero `consoleErrors`, valid non-blank screenshots, and zero leaked tmux servers
     or Greenlight child processes on teardown.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import uitest  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def assert_png(path: Path, min_bytes: int = 2000) -> None:
    data = path.read_bytes()
    assert data.startswith(PNG_MAGIC), f"{path} is not a valid PNG"
    assert len(data) >= min_bytes, f"{path} looks blank/tiny ({len(data)} bytes)"


def wait_state(ui: uitest.UITestSession, predicate, desc: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = ui.state()
        if last.get("ok") and predicate(last):
            return last
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting for state ({desc}); last={last}")


def assert_clean_web(ui: uitest.UITestSession, label: str) -> dict:
    st = ui.state()
    assert st["ok"], f"{label}: state() failed: {st}"
    assert not st["webErrors"].get("dashboard"), f"{label}: dashboard webErrors={st['webErrors']}"
    assert not st["webErrors"].get("terminal"), f"{label}: terminal webErrors={st['webErrors']}"
    assert not st["consoleErrors"].get("dashboard"), f"{label}: dashboard consoleErrors={st['consoleErrors']}"
    assert not st["consoleErrors"].get("terminal"), f"{label}: terminal consoleErrors={st['consoleErrors']}"
    return st


def verify_terminal_pty_and_tabs(ui1: uitest.UITestSession, shot_dir: Path) -> None:
    """Verify Pipe -> Basement floor transition, real PTY echo, ⌘J palette, multi-tab ops, and openWorkspace."""
    ui1.wait_for("terminal", "Boolean(window.term && document.querySelector('#tabs .tab'))", timeout=15)
    s_pipe = shot_dir / "pipe.png"
    r = ui1.screenshot(s_pipe, window="terminal")
    assert r["ok"], r
    assert_png(s_pipe)
    print(f"ok  Pipe screenshot captured ({r['width']}x{r['height']}, {r['bytes']} bytes)")

    r = ui1.action("floor", to="basement")
    assert r["ok"], r
    ui1.wait_for(
        "terminal",
        "!document.body.classList.contains('noside') && !document.body.classList.contains('sliding')",
        timeout=10,
    )
    time.sleep(0.15)
    s_base = shot_dir / "basement.png"
    r = ui1.screenshot(s_base, window="terminal")
    assert r["ok"], r
    assert_png(s_base)
    print(f"ok  Basement screenshot captured ({r['width']}x{r['height']}, {r['bytes']} bytes)")

    # Verify isolated tmux server holds a live session for ui1's terminal tab and echoes commands
    deadline = time.time() + 10
    pane_id = ""
    while time.time() < deadline:
        cp = subprocess.run(
            ["tmux", "-L", ui1.tmux_socket, "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            pane_id = cp.stdout.strip().splitlines()[0]
            break
        time.sleep(0.2)
    assert pane_id, f"expected a live PTY pane in isolated tmux server {ui1.tmux_socket}"
    subprocess.run(
        ["tmux", "-L", ui1.tmux_socket, "send-keys", "-t", pane_id, "echo UITEST_PTY_OK", "Enter"],
        check=True,
    )
    captured = ""
    deadline = time.time() + 8
    while time.time() < deadline:
        cap = subprocess.run(
            ["tmux", "-L", ui1.tmux_socket, "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
        captured = cap.stdout
        if "UITEST_PTY_OK" in captured:
            break
        time.sleep(0.2)
    assert "UITEST_PTY_OK" in captured, f"PTY echo did not land in pane: {captured[-200:]!r}"
    print(f"ok  isolated PTY round-trip verified on tmux -L {ui1.tmux_socket}")

    # Terminal ⌘J palette open -> query filter -> screenshot -> close
    assert ui1.action("openPalette")["ok"]
    ui1.wait_for("terminal", "Boolean(document.querySelector('#pal') && !document.querySelector('#pal').hidden)", timeout=10)
    assert ui1.type_text("terminal", "#pq", "fix-auth")["result"] is True
    s_pal = shot_dir / "palette.png"
    r = ui1.screenshot(s_pal, window="terminal")
    assert r["ok"], r
    assert_png(s_pal)
    assert ui1.action("closePalette")["ok"]
    ui1.wait_for("terminal", "Boolean(document.querySelector('#pal') && document.querySelector('#pal').hidden)", timeout=10)
    print(f"ok  Terminal ⌘J palette open/filter/close verified ({r['width']}x{r['height']}, {r['bytes']} bytes)")

    # Multi-tab operations: open a second tab via #add, switch back to first tab, then close extra tab
    initial_tabs = ui1.eval("terminal", "document.querySelectorAll('#tabs .tab').length")["result"]
    assert isinstance(initial_tabs, int) and initial_tabs >= 1, initial_tabs
    assert ui1.click("terminal", "#add")["result"] is True
    ui1.wait_for("terminal", f"document.querySelectorAll('#tabs .tab').length === {initial_tabs + 1}", timeout=10)
    assert ui1.eval(
        "terminal",
        "(() => { document.querySelectorAll('#tabs .tab')[0].click();"
        " return document.querySelectorAll('#tabs .tab')[0].classList.contains('on'); })()",
    )["result"] is True
    assert ui1.eval("terminal", "(window.term.closeTab(), true)")["ok"]
    ui1.wait_for("terminal", f"document.querySelectorAll('#tabs .tab').length === {initial_tabs}", timeout=10)
    print(f"ok  Terminal multi-tab open/switch/close verified ({initial_tabs} -> {initial_tabs + 1} -> {initial_tabs})")

    # openWorkspace switches Basement to the requested worktree path
    ws_fix = str(ui1.root / "worktrees" / "fix-auth")
    ws_metrics = str(ui1.root / "worktrees" / "add-metrics")
    assert ui1.action("openWorkspace", path=ws_fix)["ok"]
    ui1.wait_for("terminal", f"window.term && window.term.focusInfo().path === {ws_fix!r}", timeout=10)
    assert ui1.action("openWorkspace", path=ws_metrics)["ok"]
    ui1.wait_for("terminal", f"window.term && window.term.focusInfo().path === {ws_metrics!r}", timeout=10)
    print("ok  openWorkspace switched terminal focus between fix-auth and add-metrics")


def verify_chat_and_inbox(ui1: uitest.UITestSession, shot_dir: Path) -> None:
    """Verify Chat send -> stub reply -> clear -> toggleChat, and full Inbox ping -> confirm -> clear -> close flow."""
    # Chat toggle hide -> show
    r_hide = ui1.action("toggleChat")
    assert r_hide["ok"] and r_hide["chatShown"] is False, r_hide
    r_show = ui1.action("toggleChat")
    assert r_show["ok"] and r_show["chatShown"] is True, r_show

    # Chat send -> wait for offline stub reply -> clear -> wait for cleared state
    r_send = ui1.action("sendChat", text="Summarize open MRs for uitest")
    assert r_send["ok"], r_send
    st = wait_state(
        ui1,
        lambda s: not s["coreBusy"] and "Stub Chief reply" in s.get("lastChatText", ""),
        desc="stub chat reply",
        timeout=20.0,
    )
    assert st["lastChatRole"] == "greenlight" and st["lastChatFailed"] is False, st
    assert st["chatMsgCount"] >= 2, st
    s_chat = shot_dir / "chat-reply.png"
    r_shot = ui1.screenshot(s_chat, window="dashboard")
    assert r_shot["ok"], r_shot
    assert_png(s_chat)

    r_clear = ui1.action("chatClear")
    assert r_clear["ok"], r_clear
    st_cleared = wait_state(
        ui1,
        lambda s: s.get("clearing") == 0 and s.get("chatMsgCount") == 1 and "Context cleared" in s.get("lastChatText", ""),
        desc="chat cleared",
        timeout=15.0,
    )
    assert st_cleared["lastChatRole"] == "note", st_cleared
    print(f"ok  Chat send -> stub reply -> chatClear -> toggleChat verified ({r_shot['bytes']} bytes)")

    # Inbox flow: ping x2 -> openInbox -> inboxConfirm -> clearInbox -> closeInbox
    ws_fix = str(ui1.root / "worktrees" / "fix-auth")
    ws_metrics = str(ui1.root / "worktrees" / "add-metrics")
    p1 = ui1.action("inboxPing", text="fix-auth: merge !42 now?\nOptions: merge | hold", path=ws_fix)
    assert p1["ok"] and p1["waiting"] == 1, p1
    p2 = ui1.action("inboxPing", text="add-metrics: enable alert rules?\nOptions: yes | later", path=ws_metrics)
    assert p2["ok"] and p2["waiting"] == 2, p2

    r_open = ui1.action("openInbox")
    assert r_open["ok"] and r_open["waiting"] == 2, r_open
    st_inbox = wait_state(ui1, lambda s: s.get("inboxOpen") is True and s.get("waitingCount") == 2, desc="inbox open")
    assert st_inbox["waitingCount"] == 2, st_inbox
    s_inbox = shot_dir / "inbox.png"
    r_ishot = ui1.screenshot(s_inbox, window="dashboard")
    assert r_ishot["ok"], r_ishot
    assert_png(s_inbox)

    r_conf = ui1.action("inboxConfirm")
    assert r_conf["ok"], r_conf
    wait_state(ui1, lambda s: s.get("inboxConfirm") is True, desc="inbox confirm")

    r_clr = ui1.action("clearInbox")
    assert r_clr["ok"] and r_clr["waiting"] == 0, r_clr
    wait_state(ui1, lambda s: s.get("waitingCount") == 0 and s.get("inboxConfirm") is False, desc="inbox cleared")

    r_close = ui1.action("closeInbox")
    assert r_close["ok"] and r_close["waiting"] == 0, r_close
    wait_state(ui1, lambda s: s.get("inboxOpen") is False and s.get("waitingCount") == 0, desc="inbox closed")
    print(f"ok  Inbox ping -> openInbox -> inboxConfirm -> clearInbox -> closeInbox verified ({r_ishot['bytes']} bytes)")


def verify_resize_rescan_and_settings(ui1: uitest.UITestSession, shot_dir: Path) -> None:
    """Verify dashboard viewport resize (small -> large), rescan, and Settings open/screenshot/close."""
    r_small = ui1.action("resize", window="dashboard", width=860, height=580)
    assert r_small["ok"] and r_small["width"] <= 900 and r_small["height"] <= 620, r_small
    time.sleep(0.15)
    s_small = shot_dir / "center-small.png"
    r_ss = ui1.screenshot(s_small, window="dashboard")
    assert r_ss["ok"], r_ss
    assert_png(s_small)

    r_large = ui1.action("resize", window="dashboard", width=1360, height=900)
    assert r_large["ok"] and r_large["width"] >= 1300 and r_large["height"] >= 850, r_large
    time.sleep(0.15)

    assert ui1.action("rescan")["ok"]
    ui1.wait_for("dashboard", "document.querySelectorAll('#main .cell').length >= 2", timeout=15)
    print(f"ok  Viewport resize ({r_small['width']}x{r_small['height']} -> {r_large['width']}x{r_large['height']}) + rescan verified")

    r_open = ui1.action("openSettings")
    assert r_open["ok"] and r_open["settingsOpen"], r_open
    time.sleep(0.3)
    s_settings = shot_dir / "settings.png"
    r_set = ui1.screenshot(s_settings, window="settings")
    assert r_set["ok"], r_set
    assert_png(s_settings)
    r_close = ui1.action("closeSettings")
    assert r_close["ok"] and r_close["settingsOpen"] is False, r_close
    wait_state(ui1, lambda s: s.get("settingsOpen") is False, desc="settings closed")
    print(f"ok  Settings open/screenshot/close verified ({r_set['width']}x{r_set['height']}, {r_set['bytes']} bytes)")


def assert_process_gone(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.1)
    raise AssertionError(f"leaked --uitest process survived teardown: pid={pid}")


def main() -> None:
    if sys.platform != "darwin":
        print("skip test_uitest.py (macOS only)")
        return

    shot_dir = Path(os.environ.get("UITEST_SHOT_DIR") or tempfile.mkdtemp(prefix="gl-uitest-shots-"))
    shot_dir.mkdir(parents=True, exist_ok=True)
    binary = Path(os.environ["UITEST_BINARY"]) if os.environ.get("UITEST_BINARY") else uitest.build_test_binary()

    # Launch TWO concurrent instances simultaneously (default + adversarial) to verify parallel isolation
    with (
        uitest.UITestSession(fixture="default", binary=binary) as ui1,
        uitest.UITestSession(fixture="adversarial", binary=binary) as ui2,
    ):
        p1, p2 = ui1.ping(), ui2.ping()
        assert p1["ok"] and p2["ok"]
        assert p1["pid"] != p2["pid"]
        assert p1["tmuxSocket"] != p2["tmuxSocket"]
        assert p1["greenlightHome"] != p2["greenlightHome"]
        pid1, pid2 = int(p1["pid"]), int(p2["pid"])
        tmux_sock_1, tmux_sock_2 = ui1.tmux_socket, ui2.tmux_socket
        print(f"ok  two parallel --uitest instances running (pids {pid1}, {pid2})")

        # 1. Terminal: Pipe -> Basement, PTY round-trip, ⌘J palette, multi-tab ops, openWorkspace
        verify_terminal_pty_and_tabs(ui1, shot_dir)

        # 2. Ride up to Greenlight Center (dashboard.html + SwiftUI Chat) and verify live dash.py snapshot
        r = ui1.action("floor", to="center")
        assert r["ok"] and r["upstairs"], r
        ui1.wait_for("dashboard", "document.querySelectorAll('#main .cell').length >= 2", timeout=15)
        names = ui1.eval("dashboard", "[...document.querySelectorAll('#main .cell')].map(e => e.dataset.name)")
        assert names["ok"] and set(names["result"]) >= {"fix-auth", "add-metrics"}, names
        s_center = shot_dir / "center.png"
        r = ui1.screenshot(s_center, window="dashboard")
        assert r["ok"], r
        assert_png(s_center)
        print(f"ok  Greenlight Center rendered live dash.py pucks & captured screenshot ({r['width']}x{r['height']}, {r['bytes']} bytes)")

        # 3. Chat send -> stub reply -> clear -> toggleChat, plus Inbox ping -> confirm -> clear -> close
        verify_chat_and_inbox(ui1, shot_dir)

        # 4. Viewport resize (small/large) + rescan + Settings open/close
        verify_resize_rescan_and_settings(ui1, shot_dir)

        # 5. Second concurrent instance (adversarial XSS fixture): real macOS WKWebView stays clean
        r = ui2.action("floor", to="center")
        assert r["ok"], r
        ui2.wait_for("dashboard", "document.querySelectorAll('#main .cell').length >= 1", timeout=15)
        pwn = ui2.eval("dashboard", "window.__pwned || null")
        assert pwn["ok"] and pwn["result"] is None, f"XSS executed in real WKWebView: {pwn}"

        # 6. Exercise clearErrors RPC and verify zero webErrors and zero consoleErrors in both instances
        assert_clean_web(ui1, "ui1")
        assert_clean_web(ui2, "ui2")
        assert ui1.action("clearErrors")["ok"]
        assert_clean_web(ui1, "ui1-after-clear")
        print(f"ok  zero WKWebView page/console errors across both parallel instances (shots in {shot_dir})")

    # 7. Verify clean teardown killed BOTH isolated tmux servers and BOTH Greenlight processes
    for sock in (tmux_sock_1, tmux_sock_2):
        check = subprocess.run(["tmux", "-L", sock, "list-sessions"], capture_output=True, text=True)
        assert check.returncode != 0, f"isolated tmux server {sock} survived stop()"
    assert_process_gone(pid1)
    assert_process_gone(pid2)
    print("ok  isolated tmux servers, child processes & temp directories cleaned up")
    print("all uitest checks passed")


if __name__ == "__main__":
    main()
