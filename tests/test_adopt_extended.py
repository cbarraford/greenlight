"""Extended pytest coverage for scripts/adopt.py (apply, dismiss, main CLI)."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_BOOT = tempfile.mkdtemp(prefix="gl-adopt-ext-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import adopt  # noqa: E402


def test_apply_dismiss_and_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(adopt, "DISMISSED", tmp_path / "dismissed.json")
    monkeypatch.setattr(adopt, "STATE_FILE", tmp_path / "adopt-state.json")
    monkeypatch.setattr(adopt, "LOG", tmp_path / "adopt.jsonl")

    # 1. dismiss CLI records keys
    monkeypatch.setattr(sys, "argv", ["adopt.py", "dismiss", "acme/widgets!5", "octo/app#9"])
    adopt.main()
    saved = json.loads((tmp_path / "dismissed.json").read_text())
    assert "acme/widgets!5" in saved and "octo/app#9" in saved

    # 2. apply creates workspaces, handles failure, writes log, pings
    sample_plan = {
        "items": [
            {"key": "acme/widgets!1", "verdict": "create", "role": "author", "title": "Fix bug", "reason": "no ws", "name": "fix-bug", "forge": "gitlab", "iid": 1, "project": "acme/widgets", "repo": {"id": "r1"}},
            {"key": "acme/widgets!2", "verdict": "create", "role": "reviewer", "block": "approval", "title": "Review PR", "reason": "no ws", "name": "rev-pr", "forge": "gitlab", "iid": 2, "project": "acme/widgets", "repo": {"id": "r1"}},
            {"key": "acme/widgets!3", "verdict": "deferred", "role": "author", "title": "Later", "reason": "cap"},
        ],
        "create": [],
        "dismissed": {"acme/widgets!9": {"ts": 1, "why": "no-repo"}},
        "seen": ["acme/widgets!1", "acme/widgets!2"],
        "pings": [("no-repo", "No repo for !9")],
    }
    sample_plan["create"] = [sample_plan["items"][0], sample_plan["items"][1]]

    def fake_create(c):
        if c["iid"] == 2:
            raise RuntimeError("branch conflict")
        return {"path": "/w/fix-bug", "branch": "fix-bug"}

    pings = []
    monkeypatch.setattr(adopt, "create", fake_create)
    monkeypatch.setattr(adopt.greenlight, "note", lambda *_a: None)
    monkeypatch.setattr(adopt.greenlight, "ping", lambda text, key: pings.append((key, text)))

    adopt.apply(sample_plan)
    assert sample_plan["items"][0]["created"] is True
    assert sample_plan["items"][1]["created"] is False and "branch conflict" in sample_plan["items"][1]["error"]
    assert pings == [("no-repo", "No repo for !9")]

    # 3. main() scan / precheck / apply (text + json + error exit)
    monkeypatch.setattr(adopt, "plan", lambda cap=adopt.CAP: sample_plan)
    monkeypatch.setattr(sys, "argv", ["adopt.py", "scan"])
    adopt.main()
    out = capsys.readouterr().out
    assert "WOULD CREATE" in out and "DEFERRED (cap)" in out

    monkeypatch.setattr(sys, "argv", ["adopt.py", "precheck"])
    with pytest.raises(SystemExit) as ex:
        adopt.main()
    assert ex.value.code == 0
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["adopt.py", "apply", "--json"])
    with pytest.raises(SystemExit) as ex:
        adopt.main()
    assert ex.value.code == 1
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3 and all("repo" not in r for r in rows)
