"""Extended pytest coverage for scripts/cleanup.py (scan, log, TOCTOU guards, CLI)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

_BOOT = tempfile.mkdtemp(prefix="gl-cleanup-ext-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cleanup  # noqa: E402


def test_scan_and_log(tmp_path, monkeypatch):
    log_path = tmp_path / "cleanup.jsonl"
    monkeypatch.setattr(cleanup, "LOG", log_path)
    wt_dir = tmp_path / "wt1"
    wt_dir.mkdir()

    def fake_orca(*args):
        if args[:2] == ("worktree", "list"):
            return {"worktrees": [
                {"id": "w-main", "repoId": "r1", "path": str(tmp_path / "main"), "isMainWorktree": True, "branch": "main"},
                {"id": "w1", "repoId": "r1", "path": str(wt_dir), "isMainWorktree": False, "branch": "refs/heads/feat-1", "displayName": "feat-1"},
                {"id": "w-unk", "repoId": "r-missing", "path": str(tmp_path / "none"), "isMainWorktree": False, "branch": "x"},
            ]}
        if args[:2] == ("terminal", "list"):
            return {"terminals": [{"worktreeId": "w-main", "lastOutputAt": time.time() * 1000}]}
        if args[:2] == ("repo", "list"):
            return {"repos": [{"id": "r1", "path": str(tmp_path / "main")}]}
        return {}

    monkeypatch.setattr(cleanup, "orca", fake_orca)
    monkeypatch.setattr(cleanup, "Repo", lambda path: mock.MagicMock(path=path))
    monkeypatch.setattr(cleanup, "git", lambda *_a, **_kw: "abc123")
    monkeypatch.setattr(cleanup, "evaluate", lambda w, _r, _b: ("candidate", "merged") if w["id"] == "w1" else ("keep", "main"))

    rows = cleanup.scan()
    by_id = {r["id"]: r for r in rows}
    assert by_id["w1"]["verdict"] == "candidate" and by_id["w1"]["tip"] == "abc123"
    assert by_id["w-unk"]["verdict"] == "skip" and by_id["w-unk"]["reason"] == "unknown repo"

    cleanup.log({"name": "feat-1", "path": str(wt_dir), "branch": "feat-1", "reason": "merged", "removed": True})
    logged = json.loads(log_path.read_text().strip())
    assert logged["name"] == "feat-1" and logged["removed"] is True


def test_cli_scan_precheck_and_apply_toctou_guards(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "cleanup.jsonl"
    monkeypatch.setattr(cleanup, "LOG", log_path)
    wt_ok = tmp_path / "wt-ok"
    wt_dirty = tmp_path / "wt-dirty"
    wt_moved = tmp_path / "wt-moved"
    wt_busy = tmp_path / "wt-busy"
    for d in (wt_ok, wt_dirty, wt_moved, wt_busy):
        d.mkdir()

    def make_rows():
        return [
            {"id": "w-ok", "ids": ["w-ok"], "name": "ok", "path": str(wt_ok), "tip": "t1", "branch": "b1", "verdict": "candidate", "reason": "merged"},
            {"id": "w-dirty", "ids": ["w-dirty"], "name": "dirty", "path": str(wt_dirty), "tip": "t2", "branch": "b2", "verdict": "candidate", "reason": "merged"},
            {"id": "w-moved", "ids": ["w-moved"], "name": "moved", "path": str(wt_moved), "tip": "t3", "branch": "b3", "verdict": "candidate", "reason": "merged"},
            {"id": "w-busy", "ids": ["w-busy"], "name": "busy", "path": str(wt_busy), "tip": "t4", "branch": "b4", "verdict": "candidate", "reason": "merged"},
            {"id": "w-keep", "ids": ["w-keep"], "name": "keep", "path": str(tmp_path / "k"), "tip": "", "branch": "main", "verdict": "keep", "reason": "main"},
        ]

    monkeypatch.setattr(cleanup, "scan", make_rows)
    monkeypatch.setattr(sys, "argv", ["cleanup.py", "scan"])
    cleanup.main()
    out = capsys.readouterr().out
    assert "WOULD DELETE (4)" in out and "KEPT (1)" in out

    monkeypatch.setattr(sys, "argv", ["cleanup.py", "precheck"])
    with pytest.raises(SystemExit) as ex:
        cleanup.main()
    assert ex.value.code == 0
    capsys.readouterr()

    def fake_git(path, *args, **_kw):
        if args[:2] == ("rev-parse", "HEAD"):
            return "t3-new" if path == str(wt_moved) else {"ok": "t1", "dirty": "t2", "busy": "t4"}.get(Path(path).name.split("-")[-1], "")
        if args[0] == "status":
            return " M file.py" if path == str(wt_dirty) else ""
        return ""

    removed_ids = []
    def fake_orca(*args):
        if args[:2] == ("terminal", "list"):
            return {"terminals": [{"worktreeId": "w-busy", "lastOutputAt": time.time() * 1000}]}
        if args[:2] == ("worktree", "rm"):
            removed_ids.append(args[-1])
            return {}
        return {}

    monkeypatch.setattr(cleanup, "git", fake_git)
    monkeypatch.setattr(cleanup, "orca", fake_orca)
    monkeypatch.setattr(sys, "argv", ["cleanup.py", "apply", "--json"])
    cleanup.main()
    res = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert res["w-ok"]["removed"] is True and removed_ids == ["id:w-ok"]
    assert res["w-dirty"]["removed"] is False and "uncommitted" in res["w-dirty"]["error"]
    assert res["w-moved"]["removed"] is False and "HEAD changed" in res["w-moved"]["error"]
    assert res["w-busy"]["removed"] is False and "terminal became active" in res["w-busy"]["error"]
