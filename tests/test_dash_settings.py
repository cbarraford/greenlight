"""Extended pytest coverage for scripts/dash.py settings(), history/record, and helpers."""
import json
import os
import sys
import tempfile
from pathlib import Path


_BOOT = tempfile.mkdtemp(prefix="gl-dash-ext-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dash  # noqa: E402


def test_settings_prompts_autorun_lifecycle_instructions_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    monkeypatch.setattr(dash, "MEMORY", tmp_path / "memory")

    # 1. Prompts: validation, set, get, reset
    assert "error" in dash.settings("prompt-get", "../evil/ci", "")
    assert "error" in dash.settings("prompt-get", "widgets/bogus", "")
    assert "error" in dash.settings("prompt-bogus", "widgets/ci", "")
    res = dash.settings("prompt-set", "widgets/ci", "Fix CI fast")
    assert res["own"] is True and res["text"].strip() == "Fix CI fast"
    res2 = dash.settings("prompt-get", "widgets/ci", "")
    assert res2["own"] is True and res2["kind"] == "ci"
    res3 = dash.settings("prompt-set", "widgets/ci", "   ")
    assert res3["own"] is False and res3["text"] == ""

    # 2. Autorun: validation, set (rejecting bool/out-of-range), get
    assert "error" in dash.settings("autorun-get", ".hidden", "")
    ar = dash.settings("autorun-set", "widgets", json.dumps({"ci": 0, "merge": True, "unknown": 0}))
    assert ar["on"] == {"ci": 0}
    assert dash.settings("autorun-get", "widgets", "")["on"] == {"ci": 0}

    # 3. Lifecycle: validation, get, set, reset
    assert "error" in dash.settings("lifecycle-get", "../bad", "")
    assert "error" in dash.settings("lifecycle-bogus", "acme/widgets", "")
    lc = dash.settings("lifecycle-get", "acme/widgets", "")
    assert "builtins" in lc and "stages" in lc
    lc_set = dash.settings("lifecycle-set", "acme/widgets", json.dumps({"stages": lc["stages"]}))
    assert "stages" in lc_set
    lc_reset = dash.settings("lifecycle-reset", "acme/widgets", "")
    assert "stages" in lc_reset

    # 4. Standing instructions: set, get, clear
    assert dash.settings("instructions-set", "-", "Always run tests")["text"].strip() == "Always run tests"
    assert dash.settings("instructions-get", "-", "")["text"].strip() == "Always run tests"
    assert dash.settings("instructions-set", "-", "")["text"] == ""

    # 5. Memory notes: validation, set, list, delete
    assert "error" in dash.settings("memory-set", "MEMORY", "nope")
    assert "error" in dash.settings("memory-set", "../bad", "nope")
    assert "error" in dash.settings("memory-bogus", "note1", "")
    m1 = dash.settings("memory-set", "note1", "---\ndescription: A useful note\n---\nBody")
    assert [n["name"] for n in m1["notes"]] == ["note1"]
    assert "A useful note" in (tmp_path / "memory" / "MEMORY.md").read_text()
    m_list = dash.settings("memory-list", "-", "")
    assert len(m_list["notes"]) == 1
    m_del = dash.settings("memory-delete", "note1", "")
    assert m_del["notes"] == []

    # 6. Unknown op
    assert "error" in dash.settings("unknown-op", "-", "")
