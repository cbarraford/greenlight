"""Extended pytest coverage for scripts/blocking.py (open_mrs, scan, blocks, cached, CLI)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_BOOT = tempfile.mkdtemp(prefix="gl-block-ext-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "alice")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import blocking  # noqa: E402


def test_open_mrs_milestone_and_plain(monkeypatch):
    def fake_gl_all(url):
        if "milestones?state=active" in url:
            return [{"id": 10, "title": "rel-1.0"}, {"id": 11, "title": "other"}]
        if "milestones/10/merge_requests" in url:
            return [
                {"iid": 1, "state": "opened", "draft": False, "project_id": 99},
                {"iid": 2, "state": "opened", "draft": True, "project_id": 99},
                {"iid": 3, "state": "opened", "draft": False, "project_id": 88},
            ]
        return [{"iid": 4, "state": "opened", "draft": False, "project_id": 99}]

    monkeypatch.setattr(blocking.greenlight, "gl_all", fake_gl_all)
    monkeypatch.setattr(blocking.greenlight, "gl", lambda _u: {"id": 99})
    assert [m["iid"] for m in blocking.open_mrs("acme/widgets", "rel-")] == [1]
    assert [m["iid"] for m in blocking.open_mrs("acme/widgets", None)] == [4]


def test_scan_aggregates_threads_and_handles_total_failure(monkeypatch):
    monkeypatch.setattr(blocking, "ME", "alice")
    monkeypatch.setattr(blocking, "REPOS", {"acme/widgets": None})
    mrs = [
        {"iid": 5, "web_url": "https://gl/5", "title": "Fix auth"},
        {"iid": 6, "web_url": "https://gl/6", "title": "Flaky MR"},
    ]
    monkeypatch.setattr(blocking, "open_mrs", lambda _p, _pre: mrs)

    def fake_check(_proj, m):
        if m["iid"] == 6:
            raise RuntimeError("transient 502")
        return [
            ("alice", "discussion", "thread 1"),
            ("alice", "discussion", "thread 2"),
            ("bob", "approval", "other"),
        ]

    monkeypatch.setattr(blocking, "check", fake_check)
    res = blocking.scan()
    assert "acme/widgets!5" in res
    assert res["acme/widgets!5"]["n"] == 2
    assert res["acme/widgets!5"]["why"] == "2 threads are waiting on you"

    # If every MR check fails, scan must raise
    monkeypatch.setattr(blocking, "check", lambda _p, _m: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError, match="all 2 MR blocking checks failed"):
        blocking.scan()


def test_blocks_cache_and_cli(tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / "blocking.json"
    monkeypatch.setattr(blocking, "FILE", cache_file)
    monkeypatch.setattr(blocking, "REPOS", {"acme/widgets": None})
    sample = {
        "acme/widgets!5": {"reason": "approval", "why": "awaiting your review", "n": 1, "url": "https://gl/5", "title": "Ship"},
    }
    monkeypatch.setattr(blocking, "scan", lambda: sample)
    assert blocking.cached() is None
    assert blocking.blocks(fresh=True) == sample
    assert blocking.cached() == sample

    # Stale cache returns None
    cache_file.write_text(json.dumps({"ts": int(time.time()) - 99999, "blocks": sample}))
    assert blocking.cached(max_age=60) is None

    # CLI text & json output
    monkeypatch.setattr(blocking, "blocks", lambda fresh=False: sample)
    monkeypatch.setattr(sys, "argv", ["blocking.py", "--json"])
    blocking.main()
    assert json.loads(capsys.readouterr().out) == sample

    monkeypatch.setattr(sys, "argv", ["blocking.py"])
    blocking.main()
    assert "[approval] acme/widgets!5" in capsys.readouterr().out

    monkeypatch.setattr(blocking, "blocks", lambda fresh=False: {})
    monkeypatch.setattr(sys, "argv", ["blocking.py"])
    blocking.main()
    assert "nothing is blocked on me" in capsys.readouterr().out

    monkeypatch.setattr(blocking, "blocks", lambda fresh=False: None)
    monkeypatch.setattr(sys, "argv", ["blocking.py"])
    with pytest.raises(SystemExit):
        blocking.main()
