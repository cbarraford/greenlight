"""Per-project host setting (`host=server|mac|auto`, default `server`), Mac detection, Settings ops, adopt & placement."""
import json
import sys

from conftest import ROOT

import adopt
import dash
import greenlight
import placement


def test_repo_host_mode_defaults_to_server_and_parses_options(tmp_path, monkeypatch):
    monkeypatch.setattr(greenlight, "STATE", tmp_path)
    assert greenlight.repo_host_mode("plain-repo") == "server"
    assert greenlight.repo_host_mode("plain-repo", opts={"host": "MAC"}) == "mac"
    assert greenlight.repo_host_mode("plain-repo", opts={"host": "auto"}) == "auto"
    assert greenlight.repo_host_mode("plain-repo", opts={"host": "bogus"}) == "server"

    lc_dir = tmp_path / "lifecycles"
    lc_dir.mkdir()
    (lc_dir / "ios-app.md").write_text("# ios-app\n\nOptions: `host=mac`\n\n1. `mr_open` — Open.\n")
    assert greenlight.repo_host_mode("ios-app") == "mac"


def test_needs_mac_detection_markers(tmp_path):
    # Pure Linux/server repo -> False
    server_repo = tmp_path / "backend"
    (server_repo / "src").mkdir(parents=True)
    (server_repo / "src" / "main.py").write_text("print('hi')\n")
    (server_repo / "Makefile").write_text("test:\n\tpytest\n")
    assert not greenlight.needs_mac(server_repo)
    assert greenlight.needs_mac_reason(server_repo) is None
    assert not greenlight.needs_mac(tmp_path / "does-not-exist")
    assert not greenlight.needs_mac("")

    # Xcode project directory -> True
    xc_repo = tmp_path / "ios-app"
    (xc_repo / "MyApp.xcodeproj").mkdir(parents=True)
    assert greenlight.needs_mac(xc_repo)
    assert greenlight.needs_mac_reason(xc_repo) == "Xcode project"

    # GUI tests directory / test_uitest -> True
    gui_repo = tmp_path / "gui-repo"
    (gui_repo / "tests").mkdir(parents=True)
    (gui_repo / "tests" / "test_uitest.py").write_text("# gui test\n")
    assert greenlight.needs_mac(gui_repo)
    assert greenlight.needs_mac_reason(gui_repo) == "GUI tests"

    # Swift AppKit/SwiftUI source -> True
    swift_repo = tmp_path / "mac-app"
    (swift_repo / "app" / "Sources").mkdir(parents=True)
    (swift_repo / "app" / "Sources" / "App.swift").write_text("import SwiftUI\nstruct AppView {}\n")
    assert greenlight.needs_mac(swift_repo)
    assert greenlight.needs_mac_reason(swift_repo) == "macOS/iOS app"

    # Package.swift targeting macOS -> True
    pkg_repo = tmp_path / "swift-pkg"
    pkg_repo.mkdir()
    (pkg_repo / "Package.swift").write_text("let p = Package(platforms: [.macOS(.v14)])\n")
    assert greenlight.needs_mac(pkg_repo)
    assert greenlight.needs_mac_reason(pkg_repo) == "Apple Swift package"

    # node_modules/.git skipped (does not false-positive)
    ignored = tmp_path / "web-app"
    (ignored / "node_modules" / "Foo.xcodeproj").mkdir(parents=True)
    assert not greenlight.needs_mac(ignored)

    # Greenlight itself has app/Sources + tests/test_uitest.py -> needs Mac in auto mode
    assert greenlight.needs_mac(ROOT)


def test_resolve_repo_host_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(greenlight, "STATE", tmp_path)
    mac_repo = tmp_path / "mac-app"
    (mac_repo / "App.xcodeproj").mkdir(parents=True)
    linux_repo = tmp_path / "api"
    linux_repo.mkdir()

    # Default is server even if repo has Xcode markers
    r_default = greenlight.resolve_repo_host("mac-app", path=mac_repo)
    assert r_default["mode"] == "server" and r_default["target"] == "server" and r_default["needs_mac"] is True

    # Auto switches to mac when markers exist, server otherwise
    r_auto_mac = greenlight.resolve_repo_host("mac-app", path=mac_repo, opts={"host": "auto"})
    assert r_auto_mac == {"mode": "auto", "target": "mac", "needs_mac": True, "reason": "Xcode project"}
    r_auto_srv = greenlight.resolve_repo_host("api", path=linux_repo, opts={"host": "auto"})
    assert r_auto_srv == {"mode": "auto", "target": "server", "needs_mac": False, "reason": ""}

    # Explicit mac always targets mac
    r_mac = greenlight.resolve_repo_host("api", path=linux_repo, opts={"host": "mac"})
    assert r_mac["mode"] == "mac" and r_mac["target"] == "mac"


def test_dash_settings_host_get_set_and_repo_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    mac_repo = tmp_path / "ios-app"
    (mac_repo / "App.xcworkspace").mkdir(parents=True)
    srv_repo = tmp_path / "api"
    srv_repo.mkdir()
    monkeypatch.setattr(dash, "_repo_main_paths", lambda tree_rows=None: {"ios-app": str(mac_repo), "api": str(srv_repo)})

    # Validation errors
    assert "error" in dash.settings("host-set", "../bad", "mac")
    assert "error" in dash.settings("host-set", "ios-app", "windows")
    assert "error" in dash.settings("host-get", ".hidden", "")

    # Default is server
    g0 = dash.settings("host-get", "ios-app", "")
    assert g0["mode"] == "server" and g0["target"] == "server" and g0["needs_mac"] is True

    # Set to auto -> resolves to mac because of xcworkspace
    s1 = dash.settings("host-set", "ios-app", "auto")
    assert s1["mode"] == "auto" and s1["target"] == "mac" and s1["options"].get("host") == "auto"

    # lifecycle-get returns host_info matching saved setting
    lc = dash.settings("lifecycle-get", "ios-app", "")
    assert lc["host_info"]["mode"] == "auto" and lc["host_info"]["target"] == "mac"

    # Set to mac on api -> persists in lifecycle
    s2 = dash.settings("host-set", "api", "mac")
    assert s2["mode"] == "mac" and s2["target"] == "mac"

    # repo-hosts returns all repos and saved lifecycles
    rh = dash.settings("repo-hosts", "-", "")
    assert rh["hosts"]["ios-app"]["target"] == "mac"
    assert rh["hosts"]["api"]["target"] == "mac"

    # Reset back to server removes host option (default)
    s3 = dash.settings("host-set", "api", "server")
    assert s3["mode"] == "server" and s3["target"] == "server" and "host" not in s3["options"]


def test_dash_trees_cli_includes_repo_hosts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    app_dir = tmp_path / "mac-tool"
    (app_dir / "Tool.xcodeproj").mkdir(parents=True)
    lc_dir = tmp_path / "lifecycles"
    lc_dir.mkdir()
    (lc_dir / "mac-tool.md").write_text("# mac-tool\n\nOptions: `host=auto`\n\n1. `mr_open` — Open.\n")
    fake_trees = [{"ade": "orca", "project": "mac-tool", "name": "main", "path": str(app_dir), "branch": "main",
                   "main": True, "agent": "none", "goal": "", "pinned": False, "active": None, "since": None, "created": None}]
    monkeypatch.setattr(dash, "trees", lambda: fake_trees)
    monkeypatch.setattr(dash, "basement_panes", dict)
    monkeypatch.setattr(sys, "argv", ["dash.py", "trees"])
    dash.main()
    out = json.loads(capsys.readouterr().out)
    assert out["repoHosts"]["mac-tool"]["mode"] == "auto"
    assert out["repoHosts"]["mac-tool"]["target"] == "mac"
    assert out["repoHosts"]["mac-tool"]["needs_mac"] is True


def test_adopt_skips_mac_repos_on_linux_and_adopts_on_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(greenlight, "STATE", tmp_path)
    monkeypatch.setattr(adopt, "STATE_FILE", tmp_path / "adopt.json")
    monkeypatch.setattr(adopt, "DISMISSED", tmp_path / "dismissed.json")
    mac_dir = tmp_path / "mac-app"
    mac_dir.mkdir()
    lc_dir = tmp_path / "lifecycles"
    lc_dir.mkdir()
    (lc_dir / "mac-app.md").write_text("# mac-app\n\nOptions: `host=mac`\n\n1. `mr_open` — Open.\n")

    cand = {"key": "org/mac-app!1", "forge": "github", "project": "org/mac-app", "iid": 1, "role": "author",
            "title": "Fix Mac build", "source_branch": "fix-mac", "draft": False, "web_url": "https://example.com/1", "sha": "abc"}
    monkeypatch.setattr(adopt, "repo_map", lambda: {"org/mac-app": {"id": "r1", "path": str(mac_dir), "forge": "github"}})
    monkeypatch.setattr(adopt, "candidates", lambda forges: ([dict(cand)], True))
    monkeypatch.setattr(adopt, "checked_out", lambda path: {})

    # On Linux server: skipped because host=mac
    monkeypatch.setattr(sys, "platform", "linux")
    p_linux = adopt.plan(rows=[], rows_ts=0, now=100)
    assert p_linux["create"] == []
    assert p_linux["items"][0]["verdict"] == "skip"
    assert "runs on Mac" in p_linux["items"][0]["reason"]

    # On macOS: adopted normally
    monkeypatch.setattr(sys, "platform", "darwin")
    p_mac = adopt.plan(rows=[], rows_ts=0, now=100)
    assert len(p_mac["create"]) == 1
    assert p_mac["items"][0]["verdict"] == "create"


def test_placement_respects_mac_repo_target_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(greenlight, "STATE", tmp_path)
    lc_dir = tmp_path / "lifecycles"
    lc_dir.mkdir()
    (lc_dir / "ios-app.md").write_text("# ios-app\n\nOptions: `host=mac`\n\n1. `mr_open` — Open.\n")

    ms = {"local": "devbox", "ssh:mac": "macbook"}
    wts = [
        {"worktreeId": "r::a", "displayName": "mac-ws-on-mac", "path": "/w/a", "repo": "ios-app", "repoId": "r1",
         "branch": "refs/heads/a", "hostId": "ssh:mac", "agents": [{"state": "working", "updatedAt": 1}]},
        {"worktreeId": "r::b", "displayName": "mac-ws-on-server", "path": "/w/b", "repo": "ios-app", "repoId": "r1",
         "branch": "refs/heads/b", "hostId": "local", "agents": [{"state": "working", "updatedAt": 1}]},
        {"worktreeId": "r::c", "displayName": "srv-ws-on-server", "path": "/w/c", "repo": "api", "repoId": "r2",
         "branch": "refs/heads/c", "hostId": "local", "agents": [{"state": "working", "updatedAt": 1}]},
    ]
    monkeypatch.setattr(sys, "platform", "linux")
    rs = {r["name"]: r for r in placement.rows(wts, ms, "devbox")}
    assert rs["mac-ws-on-mac"]["misplaced"] is False
    assert rs["mac-ws-on-server"]["misplaced"] is True
    assert rs["srv-ws-on-server"]["misplaced"] is False


def test_mac_detection_cache_and_fast_path(tmp_path, monkeypatch):
    greenlight._MAC_REASON_CACHE.clear()
    calls = 0
    orig = greenlight._scan_mac_tree

    def counted(root):
        nonlocal calls
        calls += 1
        return orig(root)

    monkeypatch.setattr(greenlight, "_scan_mac_tree", counted)
    repo = tmp_path / "cached-repo"
    (repo / "App.xcodeproj").mkdir(parents=True)

    # check_mac=False skips filesystem scan for explicit server/mac modes
    assert greenlight.resolve_repo_host("cached-repo", path=repo, opts={"host": "server"}, check_mac=False)["target"] == "server"
    assert greenlight.resolve_repo_host("cached-repo", path=repo, opts={"host": "mac"}, check_mac=False)["target"] == "mac"
    assert calls == 0

    # auto mode scans once, then hits TTL cache
    assert greenlight.resolve_repo_host("cached-repo", path=repo, opts={"host": "auto"}, check_mac=False)["target"] == "mac"
    assert greenlight.resolve_repo_host("cached-repo", path=repo, opts={"host": "auto"}, check_mac=False)["target"] == "mac"
    assert calls == 1


def test_org_repo_lifecycle_aligns_short_alias_and_native_host_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    lc_dir = tmp_path / "lifecycles"
    lc_dir.mkdir()
    (lc_dir / "acme__ios-app.md").write_text("# acme/ios-app\n\nOptions: `host=mac`\n\n1. `mr_open` — Open.\n")
    monkeypatch.setattr(dash, "_repo_main_paths", lambda tree_rows=None: {"ios-app": str(tmp_path), "acme/ios-app": str(tmp_path)})

    hosts = dash.repo_hosts()
    assert hosts["acme/ios-app"]["target"] == "mac"
    assert hosts["ios-app"]["target"] == "mac"

    fake_wts = [
        {"worktreeId": "r::a", "displayName": "local-ws", "path": "/w/a", "repo": "ios-app", "branch": "refs/heads/a",
         "isMainWorktree": False, "isArchived": False, "hostId": "local", "agents": []},
        {"worktreeId": "r::b", "displayName": "remote-ws", "path": "/w/b", "repo": "api", "branch": "refs/heads/b",
         "isMainWorktree": False, "isArchived": False, "hostId": "ssh:srv", "agents": []},
    ]
    monkeypatch.setattr(dash, "ADES", ("orca",))
    monkeypatch.setattr(dash.greenlight, "orca_cli", lambda *a, **kw: {"worktrees": fake_wts})
    rows = {t["name"]: t for t in dash.trees()}
    assert rows["local-ws"].get("nativeHost", True) is True
    assert rows["remote-ws"]["nativeHost"] is False

