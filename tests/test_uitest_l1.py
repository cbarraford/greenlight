"""End-to-end: several Greenlight Center (L1) windows, each with its own projects, in a real --uitest app (macOS only).

Proves, against the real WKWebView dashboards and the app's own window bookkeeping:
  1. Two more L1 windows opened with different projects each show only their own workspaces, while the main one shows all.
  2. Open projects in one L1 window (the dialog the app opens, clicked through) changes that window alone; the main
     window's pick, which the terminals share, changes only the main one.
  3. Open projects > New window opens another L1 window with the ticked projects, leaving the one it came from as it was.
  4. File > New Window (⌘N) in an L1 window opens another L1 window with that window's projects (All stays All, never
     the main window's pick); in a terminal, a terminal. In a view the main window detached, File > Open Projects… opens
     there and ⌘N makes a full-size L1 window; in a terminal it waits for the palette to close. New windows stay on screen.
  5. Each window's title and Chief scope follow its projects; after a relaunch every L1 window comes back with its
     projects, and one left on a monitor that's gone is put back on a screen.
  6. Its close button forgets it.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import uitest

SHOWN = "JSON.stringify(arr(ui.snap && ui.snap.pucks).filter(inView).map(p => p.name).sort())"  # what the page's own filter lets through
CELLS = "JSON.stringify([...document.querySelectorAll('#main .cell')].map(e => e.dataset.name).sort())"  # what it drew


def wait(pred, timeout=15.0, what="condition"):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def l1(ui):
    r = ui.action("l1State")
    assert r["ok"], r
    return r["l1"]


def shown(ui, target):
    """The workspaces one window shows, by the page's filter and by its drawn hexes, once they agree."""
    def both():
        a, b = ui.eval(target, SHOWN), ui.eval(target, CELLS)
        if a.get("ok") and b.get("ok") and a["result"] and a["result"] == b["result"]:
            return a["result"]
        return None
    return json.loads(wait(both, what=f"{target} to draw its workspaces"))


def other(state, wid):
    return next(o for o in state["others"] if o["id"] == wid)


def pick(ui, target, ids, here=True):
    """Open projects in `target` (its title-bar chip; here=False: ⌘⇧N, on New window), tick exactly `ids`, submit."""
    r = ui.action("openProjects", window=target, here=here)
    assert r["ok"], r
    wait(lambda: ui.eval(target, "Boolean(document.querySelector('#pp form'))").get("result"), what=f"Open projects in {target}")
    want = json.dumps(sorted(ids))
    js = ("(() => { const want = new Set(" + want + "), boxes = [...document.querySelectorAll('#pp .tile input')];"
          " for (const b of boxes) if (b.checked !== want.has(b.value)) b.click();"
          " const on = boxes.filter(b => b.checked).map(b => b.value).sort();"
          " document.querySelector('#pp button.pri').click(); return on; })()")
    r = ui.eval(target, js)
    assert r["ok"] and r["result"] == sorted(ids), r


def on_screen(frame, screens):
    x, y, w, h = frame
    return any(sx <= x + 60 and x <= sx + sw - 60 and sy <= y + h - 12 and y + h <= sy + sh + 1 for sx, sy, sw, sh in screens)


def main():
    if sys.platform != "darwin":
        print("skip test_uitest_l1.py (macOS only)")
        return
    binary = Path(os.environ["UITEST_BINARY"]) if os.environ.get("UITEST_BINARY") else uitest.build_test_binary()
    shots = Path(os.environ.get("UITEST_SHOT_DIR") or tempfile.mkdtemp(prefix="gl-uitest-l1-"))
    shots.mkdir(parents=True, exist_ok=True)
    with uitest.UITestSession(fixture="projects", binary=binary, keep_defaults=True) as ui:
        wait(lambda: l1(ui)["projectsLoaded"], what="the projects list")
        assert ui.action("floor", to="center")["ok"]
        both = ["add-metrics", "fix-auth"]
        assert shown(ui, "dashboard") == both
        print("ok  main Greenlight Center shows every project:", both)

        # 1. two more L1 windows, one project each
        a = ui.action("newL1", only=["pauth"])["id"]
        b = ui.action("newL1", only=["pmetrics"])["id"]
        assert a != b
        assert shown(ui, f"l1:{a}") == ["fix-auth"] and shown(ui, f"l1:{b}") == ["add-metrics"]
        assert shown(ui, "dashboard") == both, "opening L1 windows changed the main one"
        st = l1(ui)
        assert other(st, a)["chip"] == "Auth" and other(st, b)["chip"] == "Metrics" and st["main"]["chip"] == "All projects", st
        assert other(st, a)["title"].endswith("· Auth") and other(st, b)["title"].endswith("· Metrics"), st
        assert other(st, a)["scope"] and other(st, b)["scope"] and other(st, a)["scope"] != other(st, b)["scope"], "each Chief has its own scope"
        for t, name in (("dashboard", "l1-main.png"), (f"l1:{a}", "l1-auth.png"), (f"l1:{b}", "l1-metrics.png")):
            r = ui.screenshot(shots / name, window=t)
            assert r["ok"] and (shots / name).read_bytes().startswith(b"\x89PNG") and r["bytes"] > 2000, r
        print(f"ok  L1 windows {a} (Auth) and {b} (Metrics) each show only their project; titles {other(st, a)['title']!r}, {other(st, b)['title']!r}")

        # 2. Open projects in one window changes only it; the main window's pick changes only the main one
        pick(ui, f"l1:{b}", ["pauth", "pmetrics"])
        wait(lambda: other(l1(ui), b)["only"] == ["pauth", "pmetrics"], what="window b's new pick")
        assert shown(ui, f"l1:{b}") == both and shown(ui, f"l1:{a}") == ["fix-auth"] and shown(ui, "dashboard") == both
        assert l1(ui)["main"]["only"] is None
        pick(ui, "dashboard", ["pmetrics"])
        wait(lambda: l1(ui)["main"]["only"] == ["pmetrics"], what="the main window's pick")
        assert shown(ui, "dashboard") == ["add-metrics"] and shown(ui, f"l1:{a}") == ["fix-auth"] and shown(ui, f"l1:{b}") == both
        st = l1(ui)
        assert st["main"]["title"].endswith("· Metrics"), st["main"]
        print("ok  Open projects in one L1 window leaves the others alone (main: Metrics, a: Auth, b: Auth + Metrics)")

        # 3. Open projects > New window: another L1 window with the ticked projects
        before = {o["id"] for o in l1(ui)["others"]}
        pick(ui, f"l1:{a}", ["pmetrics"], here=False)
        c = wait(lambda: ({o["id"] for o in l1(ui)["others"]} - before) or None, what="Open projects' New window").pop()
        assert other(l1(ui), c)["only"] == ["pmetrics"] and other(l1(ui), a)["only"] == ["pauth"]
        assert shown(ui, f"l1:{c}") == ["add-metrics"] and shown(ui, f"l1:{a}") == ["fix-auth"]
        print(f"ok  Open projects > New window from L1 {a} opened L1 {c} (Metrics); {a} still Auth")

        # 4. ⌘N in an L1 window: another L1 window with its projects; in a terminal, a terminal window
        r = ui.action("newWindow", window=f"l1:{a}")
        assert r["ok"] and len(r["newL1"]) == 1 and r["newTerminals"] == 0, r
        d = r["newL1"][0]
        assert other(r["l1"], d)["only"] == ["pauth"] and shown(ui, f"l1:{d}") == ["fix-auth"]
        r = ui.action("newWindow", window="dashboard")
        assert r["ok"] and len(r["newL1"]) == 1 and r["newTerminals"] == 0, r
        e = r["newL1"][0]
        assert other(r["l1"], e)["only"] == ["pmetrics"], "⌘N in the main window copies its projects"
        r = ui.action("newWindow", window="terminal")
        assert r["ok"] and r["newL1"] == [] and r["newTerminals"] == 1, r
        print(f"ok  ⌘N in L1 {a} opened L1 {d} (Auth), in the main one L1 {e} (Metrics), in a terminal a terminal window")
        extra = [d, e]

        # 4b. an L1 window on All projects: ⌘N keeps All, not the main window's pick (Metrics)
        f = ui.action("newL1", only=None)["id"]
        assert other(l1(ui), f)["only"] is None and shown(ui, f"l1:{f}") == both
        r = ui.action("newWindow", window=f"l1:{f}")
        assert r["ok"] and len(r["newL1"]) == 1 and other(r["l1"], r["newL1"][0])["only"] is None, r
        extra += [f, r["newL1"][0]]
        print(f"ok  ⌘N in L1 {f} (All projects) opened another on All projects, not the main window's Metrics")

        # 4c. a view the main window gave its own window: Open Projects… opens there; ⌘N from it is a full-size L1 window
        assert ui.eval("dashboard", "(post({cmd: 'detach', view: 'agents'}), true)")["ok"]
        wait(lambda: ui.eval("view:agents", "Boolean(window.mc)").get("result"), what="the Agents view's own window")
        before = {o["id"] for o in l1(ui)["others"]}
        pick(ui, "view:agents", ["pauth"], here=False)
        g = wait(lambda: ({o["id"] for o in l1(ui)["others"]} - before) or None, what="New window from the Agents window").pop()
        assert other(l1(ui), g)["only"] == ["pauth"] and l1(ui)["main"]["only"] == ["pmetrics"]
        r = ui.action("newWindow", window="view:agents")
        assert r["ok"] and len(r["newL1"]) == 1, r
        h = other(r["l1"], r["newL1"][0])
        assert h["only"] == ["pmetrics"] and h["frame"][2] >= 480 and h["frame"][3] >= 520, h
        extra += [g, h["id"]]
        assert ui.action("close", window="view:agents")["ok"]
        print(f"ok  Open Projects… in the Agents window opened its dialog there (L1 {g}, Auth); ⌘N there made a full-size L1 {h['id']}: {h['frame']}")

        # 4d. File › Open Projects… in a terminal never opens over its palette
        ui.wait_for("terminal", "Boolean(window.term && document.querySelector('#tabs .tab'))", timeout=15)
        assert ui.eval("terminal", "(term.palette(true), !$('pal').hidden)")["result"] is True
        assert ui.action("openProjects", window="terminal", here=False)["ok"]
        time.sleep(0.5)
        assert ui.eval("terminal", "$('dlg').hidden && !$('pal').hidden")["result"] is True, "Open projects opened over the palette"
        assert ui.eval("terminal", "(term.palette(false), $('pal').hidden)")["result"] is True
        assert ui.action("openProjects", window="terminal", here=False)["ok"]
        ui.wait_for("terminal", "!$('dlg').hidden && /Open projects/.test($('dlg').textContent)", timeout=5)
        print("ok  File › Open Projects… in a terminal waits for its palette to close, then opens")

        # 4e. File › Open Projects… from Settings, Greenlight Center closed: its dialog, in its window, which comes back up
        assert ui.action("close", window="dashboard")["ok"]
        wait(lambda: not ui.state()["windows"]["dashboard"]["visible"], what="the main window to close")
        assert ui.action("openSettings")["ok"]
        assert ui.action("openProjects", window="settings", here=False)["ok"]
        ui.wait_for("dashboard", "Boolean(document.querySelector('#pp form'))", timeout=5)
        assert ui.state()["windows"]["dashboard"]["visible"], "the main window came up"
        assert ui.eval("dashboard", "([...document.querySelectorAll('#pp button')].find(b => b.textContent === 'Cancel').click(), true)")["ok"]
        ui.action("closeSettings")
        print("ok  File › Open Projects… from Settings opens Greenlight Center's dialog in its (visible) window")

        # every L1 window so far, each cascaded off another, is inside the screen
        st = l1(ui)
        def inside(f, s):
            return s[0] <= f[0] and f[0] + f[2] <= s[0] + s[2] and s[1] <= f[1] and f[1] + f[3] <= s[1] + s[3]
        for o in st["others"]:
            assert any(inside(o["frame"], s) for s in st["screens"]), (o["id"], o["frame"], st["screens"])  # each within one screen
        print(f"ok  all {len(st['others'])} L1 windows are each inside a screen {st['screens']}")

        # 5. relaunch: every L1 window back with its projects; one on an unplugged monitor back on a screen
        for wid in extra:
            assert ui.action("close", window=f"l1:{wid}")["ok"]
        st = l1(ui)
        assert sorted(o["id"] for o in st["others"]) == sorted([a, b, c]) and sorted(x["id"] for x in st["saved"]) == sorted([a, b, c]), st
        r = ui.action("setFrame", window=f"l1:{a}", x=-30000, y=-30000, width=1000, height=700)
        assert r["ok"], r
        kept = other(l1(ui), b)["frame"]
        print(f"    before relaunch: a's frame {other(l1(ui), a)['frame']}, screens {st['screens']}")
        ui.relaunch()
        wait(lambda: l1(ui)["projectsLoaded"], what="the projects list after relaunch")
        st = l1(ui)
        got = {o["id"]: o["only"] for o in st["others"]}
        assert got == {a: ["pauth"], b: ["pauth", "pmetrics"], c: ["pmetrics"]}, got
        assert st["main"]["only"] == ["pmetrics"], st["main"]
        assert shown(ui, f"l1:{a}") == ["fix-auth"] and shown(ui, f"l1:{b}") == both and shown(ui, f"l1:{c}") == ["add-metrics"]
        fa = other(st, a)["frame"]
        assert on_screen(fa, st["screens"]), (fa, st["screens"])
        assert fa[2:] == [1000, 700] or fa[2] <= st["screens"][0][2], fa
        assert other(st, b)["frame"] == kept, (other(st, b)["frame"], kept)
        assert all(other(st, w)["visible"] for w in (a, b, c))
        print(f"ok  after relaunch L1 {a} (Auth), {b} (Auth + Metrics), {c} (Metrics) are back as they were; {a} was moved back on screen to {fa}")

        # no page errors in any L1 window
        for t in ("dashboard", f"l1:{a}", f"l1:{b}", f"l1:{c}"):
            r = ui.eval(t, "(window.__uitestErrors || []).concat(window.__uitestConsoleErrors || [])")
            assert r["ok"] and r["result"] == [], (t, r)

        # 6. close buttons forget them
        for wid in (a, b, c):
            assert ui.action("close", window=f"l1:{wid}")["ok"]
        st = l1(ui)
        assert st["others"] == [] and st["saved"] == [], st
        print("ok  closing every extra L1 window forgets it (nothing left to reopen); zero page errors")
    print(f"all L1 multi-window uitest checks passed (shots in {shots})")


if __name__ == "__main__":
    main()
