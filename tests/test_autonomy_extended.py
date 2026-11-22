"""Extended pytest coverage for scripts/autonomy.py CLI subcommands."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_BOOT = tempfile.mkdtemp(prefix="gl-auton-ext-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import autonomy  # noqa: E402


def test_autonomy_cli_subcommands(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(autonomy, "STATE", tmp_path)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["autonomy.py", *args])
        autonomy.main()
        return capsys.readouterr().out

    # settings (text + json)
    assert "auto answers:" in run("settings")
    s_json = json.loads(run("settings", "--json"))
    assert "auto" in s_json and "gate" in s_json

    # set --auto + --gate + human gate rejection
    set_out = json.loads(run("set", "--auto", "delete-workspace", "on", "--gate", "0.8"))
    assert "delete-workspace" in set_out["auto"] and set_out["gate"] == 0.8
    set_off = json.loads(run("set", "--auto", "delete-workspace", "off"))
    assert "delete-workspace" not in set_off["auto"]
    with pytest.raises(SystemExit):
        run("set", "--auto", "signoff", "on")

    # propose & gate
    with mock.patch.object(autonomy, "propose", return_value={"choice": "keep", "prob": 0.9}):
        assert json.loads(run("propose", "--kind", "delete-workspace", "Delete?"))["choice"] == "keep"
    with mock.patch.object(autonomy, "gate", return_value={"ok": True, "score": 0.9}):
        with pytest.raises(SystemExit) as ex:
            run("gate", "--kind", "watch", "Ask?")
        assert ex.value.code == 0

    # calibrate (text + json)
    cal = {"delete-workspace": {"n": 10, "right": 9, "acc": 0.9, "bands": {"0.9+": {"n": 10, "right": 9}}}}
    with (
        mock.patch.object(autonomy, "calibrate", return_value=cal),
        mock.patch.object(autonomy, "candidates", return_value=[{"kind": "delete-workspace", "right": 9, "n": 10}]),
    ):
        assert "graduate? delete-workspace" in run("calibrate")
        assert json.loads(run("calibrate", "--json"))["kinds"]["delete-workspace"]["right"] == 9

    # log (text + json) and undo
    (tmp_path / "autonomy.jsonl").write_text(
        json.dumps({"id": "a1", "ts": 1700000000, "act": "auto", "kind": "delete-workspace", "choice": "keep", "prob": 0.92, "text": "Keep?"}) + "\n"
        + json.dumps({"id": "a2", "ts": 1700000001, "act": "held", "kind": "watch", "score": 0.3, "threshold": 0.7, "text": "Held?"}) + "\n"
    )
    log_txt = run("log")
    assert "auto delete-workspace: keep" in log_txt and "held watch: score 0.3" in log_txt
    assert len(json.loads(run("log", "--json"))) == 2

    with mock.patch.object(autonomy, "undo", return_value={"ok": True, "id": "a1"}):
        assert json.loads(run("undo", "a1"))["ok"] is True
    with pytest.raises(SystemExit):
        run("undo")
