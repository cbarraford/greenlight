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


def test_task_repos_and_repo_remove(tmp_path, monkeypatch):
    import subprocess
    import ade_basement
    import diag

    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    monkeypatch.setattr(ade_basement, "HOME", tmp_path)
    monkeypatch.setattr(ade_basement, "REPOS", tmp_path / "repos.json")
    monkeypatch.setenv("GREENLIGHT_ADE", "native")
    monkeypatch.setattr(dash.greenlight, "orca", lambda *a, **kw: {"repos": []})

    r1 = tmp_path / "code" / "widgets"
    r1.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r1, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/widgets.git"], cwd=r1, check=True)
    r2 = tmp_path / "code" / "local-only"
    r2.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r2, check=True)
    ade_basement.register(str(r1))
    ade_basement.register(str(r2))

    # task repos returns checkout + registered + ade
    monkeypatch.setattr(dash, "repos", lambda: [{"id": str(r1.resolve()), "name": "widgets", "host": "github", "path": "acme/widgets"}])
    listed = dash.task("repos", "-", "")
    assert listed["ade"] == "native"
    assert listed["repos"] == [{"name": "widgets", "host": "github", "path": "acme/widgets", "checkout": str(r1.resolve())}]
    assert set(listed["registered"]) == {str(r1.resolve()), str(r2.resolve())}

    # Validation errors
    assert "error" in dash.settings("repo-remove", "../evil", "")
    assert "error" in dash.settings("repo-remove", ".hidden", "")
    assert "error" in dash.settings("repo-remove", "widgets", "{bad")
    assert "error" in dash.settings("repo-remove", "widgets", "[]")
    assert "error" in dash.settings("repo-remove", "widgets", json.dumps({"path": "../bad"}))
    assert "error" in dash.settings("repo-remove", "widgets", json.dumps({"checkout": "relative/path"}))
    assert "error" in dash.settings("repo-remove", "widgets", json.dumps({"checkout": "/a/../b"}))

    # Populate per-repo saved state for widgets and an unrelated repo (keep)
    stages = [{"key": "ci_green", "req": "CI passes", "done": ""}]
    dash.settings("lifecycle-set", "widgets", json.dumps({"stages": stages}))
    dash.settings("lifecycle-set", "acme/widgets", json.dumps({"stages": stages}))
    dash.settings("lifecycle-set", "keep", json.dumps({"stages": stages}))
    dash.settings("prompt-set", "widgets/ci", "Custom CI")
    dash.settings("prompt-set", "keep/ci", "Keep CI")
    dash.settings("autorun-set", "widgets", json.dumps({"ci": 0}))
    dash.settings("autorun-set", "keep", json.dumps({"ci": 0}))

    events = []
    monkeypatch.setattr(diag, "event", lambda kind, **f: events.append((kind, diag.clean(f))))

    # Remove widgets on native (not listed by ADE after removal)
    res = dash.settings("repo-remove", "widgets", "")
    assert res["ok"] is True and res["repo"] == "widgets"
    assert res["unregistered"] == [str(r1.resolve())]
    assert res["removed_config"] == ["autorun", "lifecycle", "prompts"]
    assert res["still_in_ade"] is False and "note" not in res
    assert ade_basement.saved() == [str(r2.resolve())]
    assert r1.is_dir()  # actual git checkout on disk is never touched
    assert not (tmp_path / "lifecycles" / "widgets.md").exists()
    assert not (tmp_path / "lifecycles" / "acme__widgets.md").exists()
    assert not (tmp_path / "prompts" / "widgets").exists()
    assert "widgets" not in dash.autorun()
    # Unrelated repo state stays untouched
    assert (tmp_path / "lifecycles" / "keep.md").exists()
    assert (tmp_path / "prompts" / "keep" / "ci.md").exists()
    assert "keep" in dash.autorun()
    assert events and events[-1][0] == "repo" and "dropped" not in events[-1][1]

    # Ambiguous bare name refuses without touching repos.json, explicit checkout succeeds
    dup = tmp_path / "more" / "local-only"
    dup.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dup, check=True)
    ade_basement.register(str(dup))
    amb = dash.settings("repo-remove", "local-only", "")
    assert "error" in amb and len(ade_basement.saved()) == 2
    ok_dup = dash.settings("repo-remove", "local-only", json.dumps({"checkout": str(dup.resolve())}))
    assert ok_dup["ok"] is True and ok_dup["unregistered"] == [str(dup.resolve())]
    assert ade_basement.saved() == [str(r2.resolve())]

    # Still listed in native (open Basement terminal) -> Basement note
    monkeypatch.setattr(dash.greenlight, "orca", lambda *a, **kw: {
        "repos": [{"id": str(r2.resolve()), "path": str(r2.resolve()), "displayName": "local-only"}]
    })
    still_nat = dash.settings("repo-remove", "local-only", "")
    assert still_nat["ok"] is True and still_nat["still_in_ade"] is True
    assert "Basement terminal" in still_nat["note"]

    # On Orca -> cleans Greenlight config and explains Orca still lists it
    monkeypatch.setenv("GREENLIGHT_ADE", "orca")
    monkeypatch.setattr(dash.greenlight, "orca", lambda *a, **kw: {
        "repos": [{"id": "o1", "path": "/orca/keep", "displayName": "keep",
                   "gitRemoteIdentity": {"canonicalKey": "github.com/acme/keep"}}]
    })
    still_orca = dash.settings("repo-remove", "keep", json.dumps({"path": "acme/keep"}))
    assert still_orca["ok"] is True and still_orca["ade"] == "orca" and still_orca["still_in_ade"] is True
    assert still_orca["removed_config"] == ["autorun", "lifecycle", "prompts"]
    assert "Orca" in still_orca["note"]
