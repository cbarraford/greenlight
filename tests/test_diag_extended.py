"""Extended pytest coverage for scripts/diag.py (issue_body, report, CLI subcommands)."""
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import diag  # noqa: E402


@pytest.fixture(autouse=True)
def _offline_doctor(monkeypatch):
    # report and bundle run doctor, which calls the host's real `gh/glab auth status` (network, up to 15 s each);
    # test_diag.py covers doctor itself with stub CLIs
    monkeypatch.setattr(diag, "doctor", lambda: {"ok": True, "checks": []})


def test_issue_body_truncates_large_bundles():
    big_events = [{"ts": i, "kind": "ping", "note": "x" * 600} for i in range(180)]
    bundle = {
        "manifest": {"version": "abc1234", "events": 180, "left_out": 0},
        "summary": {"ok": True},
        "doctor": {"ok": True},
        "errors": [],
        "events": big_events,
    }
    body = diag.issue_body("Steps to reproduce", bundle)
    assert len(body) <= diag.ISSUE_MAX
    assert body.endswith("</details>\n")


def test_report_preview_blocked_and_execute_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path))
    monkeypatch.setattr(diag, "_leak", False)

    # Empty title blocked
    r_empty = diag.report("  ", "desc")
    assert r_empty["ok"] is False and r_empty["blocked"] == "title"

    # Secret in description blocked fail-closed
    fake_tok = "glpat" + "-1234567890abcdefghij"
    r_sec = diag.report("Crash", f"token {fake_tok} leaked")
    assert r_sec["ok"] is False and r_sec["blocked"] == "description"

    # Preview writes bundle-report.json and returns preview=True
    r_prev = diag.report("Crash on startup", "Happens on launch", execute=False, inline=True)
    assert r_prev["ok"] is True and r_prev["preview"] is True and "data" in r_prev
    assert Path(r_prev["bundle"]).is_file()

    # Execute without `gh` returns GitHub new-issue URL
    with mock.patch.object(diag.shutil, "which", return_value=None):
        r_nogh = diag.report("Crash on startup", "Happens on launch", execute=True)
    assert r_nogh["ok"] is True and r_nogh["filed"] is False and "issues/new?" in r_nogh["url"]

    # Execute with `gh` succeeding and failing
    with (
        mock.patch.object(diag.shutil, "which", return_value="/usr/bin/gh"),
        mock.patch.object(diag.subprocess, "run", return_value=subprocess.CompletedProcess(["gh"], 0, stdout="https://github.com/cbarraford/greenlight/issues/99\n", stderr="")),
    ):
        r_ok = diag.report("Crash on startup", "Happens on launch", execute=True, shown=r_prev["body"])
    assert r_ok["ok"] is True and r_ok["filed"] is True and r_ok["url"].endswith("/99")

    with (
        mock.patch.object(diag.shutil, "which", return_value="/usr/bin/gh"),
        mock.patch.object(diag.subprocess, "run", return_value=subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="auth required")),
    ):
        r_fail = diag.report("Crash on startup", "Happens on launch", execute=True)
    assert r_fail["ok"] is False and r_fail["filed"] is False and "auth required" in r_fail["reason"]


def test_diag_cli_subcommands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path))
    monkeypatch.setattr(diag, "_leak", False)
    diag.event("ping", kind_h="abc")

    diag.main(["tail", "-n", "10", "--kind", "ping"])
    assert '"ping"' in capsys.readouterr().out

    diag.main(["errors", "--since", "24h"])
    assert isinstance(json.loads(capsys.readouterr().out), list)

    diag.main(["summary", "--since", "24h"])
    assert "version" in json.loads(capsys.readouterr().out)

    diag.main(["bundle", "--out", "-"])
    assert "manifest" in json.loads(capsys.readouterr().out)

    out_file = tmp_path / "b.json"
    diag.main(["bundle", "--out", str(out_file)])
    assert out_file.is_file()
    capsys.readouterr()

    with mock.patch.object(diag, "doctor", return_value={"ok": True, "checks": []}):
        with pytest.raises(SystemExit) as ex:
            diag.main(["doctor"])
        assert ex.value.code == 0
    capsys.readouterr()

    # report --json from stdin
    payload = json.dumps({"title": "UI bug", "body": "Something broke", "app_events": []})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as ex:
        diag.main(["report", "--json"])
    assert ex.value.code == 0
    assert json.loads(capsys.readouterr().out)["preview"] is True

    # Missing body file exits 1 cleanly
    with pytest.raises(SystemExit) as ex:
        diag.main(["report", "--title", "Bug", "--body-file", str(tmp_path / "nope.txt")])
    assert ex.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
