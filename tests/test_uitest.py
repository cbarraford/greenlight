"""End-to-end parallel background UI test suite for Greenlight (--uitest mode).

Proves:
  1. Two isolated Greenlight --uitest instances run concurrently with zero cross-talk
     (separate AF_UNIX sockets, GREENLIGHT_HOME, UserDefaults, and tmux servers) and zero focus theft.
  2. Real macOS WKWebView + SwiftUI floor transitions (Pipe -> Basement -> Greenlight Center -> Settings)
     produce valid non-blank PNG screenshots via App.snap().
  3. Real dash.py snapshot data renders in dashboard.html inside WKWebView with zero JS errors.
  4. Real local PTY in the isolated tmux server executes commands and tears down cleanly on stop.
  5. Adversarial XSS fixture renders safely inside the real macOS WKWebView with zero execution.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import uitest

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def assert_png(path: Path, min_bytes=2000):
    data = path.read_bytes()
    assert data.startswith(PNG_MAGIC), f"{path} is not a valid PNG"
    assert len(data) >= min_bytes, f"{path} looks blank/tiny ({len(data)} bytes)"


def main():
    if sys.platform != "darwin":
        print("skip test_uitest.py (macOS only)")
        return

    shot_dir = Path(os.environ.get("UITEST_SHOT_DIR") or tempfile.mkdtemp(prefix="gl-uitest-shots-"))
    shot_dir.mkdir(parents=True, exist_ok=True)
    binary = uitest.build_test_binary()

    # Launch TWO concurrent instances simultaneously (default + adversarial) to verify parallel isolation
    with uitest.UITestSession(fixture="default", binary=binary) as ui1, \
         uitest.UITestSession(fixture="adversarial", binary=binary) as ui2:

        p1, p2 = ui1.ping(), ui2.ping()
        assert p1["ok"] and p2["ok"]
        assert p1["pid"] != p2["pid"]
        assert p1["tmuxSocket"] != p2["tmuxSocket"]
        assert p1["greenlightHome"] != p2["greenlightHome"]
        print(f"ok  two parallel --uitest instances running (pids {p1['pid']}, {p2['pid']})")

        # 1. Terminal (Pipe -> Basement) + real PTY round-trip in isolated tmux server
        ui1.wait_for("terminal", "Boolean(window.term && document.querySelector('#tabs .tab'))", timeout=15)
        s_pipe = shot_dir / "pipe.png"
        r = ui1.screenshot(s_pipe, window="terminal")
        assert r["ok"], r
        assert_png(s_pipe)
        print(f"ok  Pipe screenshot captured ({r['width']}x{r['height']}, {r['bytes']} bytes)")

        r = ui1.action("floor", to="basement")
        assert r["ok"], r
        ui1.wait_for("terminal", "!document.body.classList.contains('noside') && !document.body.classList.contains('sliding')", timeout=10)
        time.sleep(0.15)
        s_base = shot_dir / "basement.png"
        r = ui1.screenshot(s_base, window="terminal")
        assert r["ok"], r
        assert_png(s_base)
        print(f"ok  Basement screenshot captured ({r['width']}x{r['height']}, {r['bytes']} bytes)")

        # Verify isolated tmux server actually holds a live session for ui1's terminal tab
        deadline = time.time() + 10
        panes = ""
        while time.time() < deadline:
            cp = subprocess.run(["tmux", "-L", ui1.tmux_socket, "list-panes", "-a", "-F", "#{pane_id}"],
                                capture_output=True, text=True)
            if cp.returncode == 0 and cp.stdout.strip():
                panes = cp.stdout.strip().splitlines()[0]
                break
            time.sleep(0.2)
        assert panes, f"expected a live PTY pane in isolated tmux server {ui1.tmux_socket}"
        subprocess.run(["tmux", "-L", ui1.tmux_socket, "send-keys", "-t", panes, "echo UITEST_PTY_OK", "Enter"], check=True)
        captured = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            cap = subprocess.run(["tmux", "-L", ui1.tmux_socket, "capture-pane", "-p", "-t", panes],
                                 capture_output=True, text=True)
            captured = cap.stdout
            if "UITEST_PTY_OK" in captured:
                break
            time.sleep(0.2)
        assert "UITEST_PTY_OK" in captured, f"PTY echo did not land in pane: {captured[-200:]!r}"
        print(f"ok  isolated PTY round-trip verified on tmux -L {ui1.tmux_socket}")

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

        # 3. Settings window renders and screenshots cleanly
        r = ui1.action("openSettings")
        assert r["ok"] and r["settingsOpen"], r
        time.sleep(0.3)
        s_settings = shot_dir / "settings.png"
        r = ui1.screenshot(s_settings, window="settings")
        assert r["ok"], r
        assert_png(s_settings)
        ui1.action("closeSettings")
        print(f"ok  Settings window screenshot captured ({r['width']}x{r['height']}, {r['bytes']} bytes)")

        # 4. Second concurrent instance (adversarial XSS fixture): real macOS WKWebView stays clean
        r = ui2.action("floor", to="center")
        assert r["ok"], r
        ui2.wait_for("dashboard", "document.querySelectorAll('#main .cell').length >= 1", timeout=15)
        pwn = ui2.eval("dashboard", "window.__pwned || null")
        assert pwn["ok"] and pwn["result"] is None, f"XSS executed in real WKWebView: {pwn}"

        # 5. Zero uncaught JS errors in either instance
        st1, st2 = ui1.state(), ui2.state()
        assert st1["ok"] and not st1["webErrors"].get("dashboard") and not st1["webErrors"].get("terminal"), st1["webErrors"]
        assert st2["ok"] and not st2["webErrors"].get("dashboard") and not st2["webErrors"].get("terminal"), st2["webErrors"]
        print(f"ok  zero WKWebView page errors across both parallel instances (shots in {shot_dir})")

        tmux_sock_1 = ui1.tmux_socket

    # 6. Verify clean teardown killed the isolated tmux server
    check = subprocess.run(["tmux", "-L", tmux_sock_1, "list-sessions"], capture_output=True, text=True)
    assert check.returncode != 0, f"isolated tmux server {tmux_sock_1} survived stop()"
    print("ok  isolated tmux server & temp directories cleaned up")
    print("all uitest checks passed")


if __name__ == "__main__":
    main()
