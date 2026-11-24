"""Cross-layer contract tests between Swift, HTML/JS, and Python backends."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_BOOT = tempfile.mkdtemp(prefix="gl-contracts-")
os.environ.setdefault("GREENLIGHT_HOME", _BOOT)
os.environ.setdefault("GREENLIGHT_ME", "octo")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_safety  # noqa: E402
import dash  # noqa: E402
import diag  # noqa: E402


def _swift_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in check_safety.swift_files(ROOT))


def _exported_js_methods(html_path: Path, namespace: str) -> set[str]:
    """Extract method names attached to `window.<namespace> = { ... }` in an HTML page."""
    text = html_path.read_text(encoding="utf-8")
    m = re.search(rf"window\.{namespace}\s*=\s*\{{(.*?)\n\}};", text, re.S)
    if not m:
        m = re.search(rf"window\.{namespace}\s*=\s*\{{(.*?)\}};", text, re.S)
    assert m, f"Could not find window.{namespace} export object in {html_path.name}"
    body = m.group(1)
    # Match object keys (`foo:`, `foo(`, or shorthand property `foo,` / `foo\n`)
    methods = set(re.findall(r"(?:^|[,{\s])([A-Za-z_$][\w$]*)\s*(?::|\(|,|$)", body, re.M))
    # Also catch `window.<ns>.<name> =` direct assignments
    methods.update(re.findall(rf"\b(?:window\.)?{namespace}\.([A-Za-z_$][\w$]*)\s*=", text))
    return methods


def test_swift_to_js_bridge_exports_exist():
    """Every `callJS("...<ns>.<method>")` in Swift must exist on window.<ns> in its HTML page."""
    swift = _swift_source()
    bridges = {
        "mc": _exported_js_methods(ROOT / "app" / "dashboard.html", "mc"),
        "term": _exported_js_methods(ROOT / "app" / "terminal.html", "term"),
        "stage": _exported_js_methods(ROOT / "app" / "stage.html", "stage"),
    }
    for ns, exported in bridges.items():
        called = set(re.findall(rf'callJS\(\s*"[^"]*\b{ns}\.([A-Za-z_$][\w$]*)"', swift))
        if ns == "stage":  # voice mode's js(_:_:) helper calls the stage by name too
            called |= set(re.findall(r'\bjs\(\s*"stage\.([A-Za-z_$][\w$]*)"', swift))
        missing = called - exported
        assert not missing, f"Swift callJS invokes missing {ns}.<method> not exported in HTML: {sorted(missing)}"


def test_dash_snapshot_contract_shape(tmp_path, monkeypatch):
    """dash.snapshot() always returns the required top-level keys consumed by dashboard.html."""
    monkeypatch.setattr(dash.greenlight, "STATE", tmp_path)
    (tmp_path / "last.json").write_text(json.dumps({"ts": 1700000000, "rows": []}))
    monkeypatch.setattr(dash.greenlight, "orca", lambda *a, **kw: {"worktrees": [], "terminals": [], "repos": []})
    snap = dash.snapshot()
    assert isinstance(snap, dict)
    for required in ("ts", "pucks"):
        assert required in snap, f"dash.snapshot() missing required key {required!r}"
    assert isinstance(snap["pucks"], list)


def test_diag_summary_contract_shape(tmp_path, monkeypatch):
    """diag.summary() returns stable structured keys with zero leaked free-text."""
    monkeypatch.setenv("GREENLIGHT_HOME", str(tmp_path))
    monkeypatch.setattr(diag, "_leak", False)
    s = diag.summary(diag.since("24h"))
    for key in ("version", "env", "config", "events", "spans"):
        assert key in s, f"diag.summary() missing key {key!r}"
    # Verify JSON round-trip succeeds cleanly
    encoded = json.dumps(s)
    assert json.loads(encoded)["version"] == s["version"]
