"""Unit tests for test/coverage harness scripts: run_py_tests.py, js_coverage.py, swift_coverage.py, coverage_summary.py."""
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import coverage_summary  # noqa: E402
import js_coverage  # noqa: E402
import run_py_tests  # noqa: E402
import swift_coverage  # noqa: E402


def test_classify_test_file_modes(tmp_path):
    p_pytest = tmp_path / "test_a.py"
    p_pytest.write_text('"""doc"""\ndef test_one():\n    assert True\n')
    assert run_py_tests.classify_test_file(p_pytest) == "pytest"

    p_script = tmp_path / "test_b.py"
    p_script.write_text("assert 1 == 1\n")
    assert run_py_tests.classify_test_file(p_script) == "script"

    p_both = tmp_path / "test_c.py"
    p_both.write_text('def test_two():\n    assert True\nif __name__ == "__main__":\n    test_two()\n')
    assert run_py_tests.classify_test_file(p_both) == "both"

    p_empty = tmp_path / "test_d.py"
    p_empty.write_text('"""just a docstring"""\nX = 1\n')
    assert run_py_tests.classify_test_file(p_empty) == "empty"


def test_parse_coverage_xml_emit_thresholds_and_prepare_cov_env(tmp_path):
    xml = tmp_path / "cov.xml"
    xml.write_text(
        '<coverage line-rate="0.85" branch-rate="0.75" lines-covered="17" lines-valid="20" branches-covered="6" branches-valid="8">'
        '<packages><package><classes>'
        '<class filename="foo.py"><lines>'
        '<line number="1" hits="1"/><line number="2" hits="0"/><line number="3" hits="2"/>'
        '</lines></class>'
        '</classes></package></packages></coverage>'
    )
    parsed = run_py_tests.parse_coverage_xml(xml)
    assert parsed["line_pct"] == 85.0 and parsed["branch_pct"] == 75.0
    assert parsed["files"]["foo.py"] == {"covered": 2, "total": 3, "pct": 66.67}

    env = run_py_tests._prepare_cov_env(tmp_path / "covdir")
    assert (tmp_path / "covdir" / "site" / "sitecustomize.py").is_file()
    assert str(tmp_path / "covdir" / "site") in env["PYTHONPATH"]
    assert env["COVERAGE_FILE"].endswith(".coverage")

    out_json = tmp_path / "summary.json"
    with mock.patch.object(run_py_tests.subprocess, "run", return_value=subprocess.CompletedProcess(["cov"], 0)):
        errs_ok = run_py_tests._emit_coverage(xml, env, json_path=out_json, fail_under=80.0, min_file=60.0)
        assert errs_ok == []
        assert json.loads(out_json.read_text())["line_pct"] == 85.0

        errs_fail = run_py_tests._emit_coverage(xml, env, fail_under=90.0, min_file=70.0)
        assert len(errs_fail) == 2


def test_js_coverage_aggregation_lcov_and_cli(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    dash_src = "function a() {\n  return 1;\n}\nfunction b() {\n  return 2;\n}\na();\n"
    (app_dir / "dashboard.html").write_text(f"<html>\n<body>\n<script>\n{dash_src}</script>\n</body>\n</html>\n")
    (app_dir / "terminal.html").write_text("<html><script>const x = 1;</script></html>")
    (app_dir / "stage.html").write_text("<html><script>const y = 2;</script></html>")

    cov_dir = tmp_path / "v8"
    cov_dir.mkdir()
    b_start = dash_src.index("function b()")
    b_end = dash_src.index("a();")
    (cov_dir / "v8-1-1.json").write_text(json.dumps([{
        "url": "file:///tmp/x/dashboard.html",
        "source": dash_src,
        "functions": [
            {"functionName": "", "ranges": [{"startOffset": 0, "endOffset": len(dash_src), "count": 1}]},
            {"functionName": "b", "ranges": [{"startOffset": b_start, "endOffset": b_end, "count": 0}]},
        ],
    }]))
    summary = js_coverage.aggregate_v8_coverage(cov_dir, root=tmp_path)
    dash = summary["files"]["app/dashboard.html"]
    assert dash["covered"] == 3 and dash["total"] == 5 and dash["pct"] == 60.0
    assert dash["missed_lines"] == [7, 8]

    lcov_path = tmp_path / "js.info"
    js_coverage.write_lcov(summary, lcov_path)
    text = lcov_path.read_text()
    assert "SF:app/dashboard.html" in text and "LF:5" in text and "LH:3" in text
    assert js_coverage._format_ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"
    assert js_coverage._format_ranges(list(range(0, 40, 2)), limit=3).endswith("more)")

    # Test discover_node_path + main() success & fail-under paths with mocked subprocess
    monkeypatch.setenv("NODE_PATH", "/fake/nm")
    assert js_coverage.discover_node_path() == "/fake/nm"

    fake_summary = {
        "line_pct": 90.0,
        "lines_covered": 90,
        "lines_valid": 100,
        "files": {"app/dashboard.html": {"covered": 90, "total": 100, "pct": 90.0, "missed_lines": [10], "hits": {10: 0}}},
    }
    out_json = tmp_path / "js.json"
    out_lcov = tmp_path / "js2.info"
    with (
        mock.patch.object(js_coverage.shutil, "which", return_value="/usr/bin/node"),
        mock.patch.object(js_coverage.subprocess, "run", return_value=subprocess.CompletedProcess(["node"], 0)),
        mock.patch.object(js_coverage, "aggregate_v8_coverage", return_value=fake_summary),
    ):
        assert js_coverage.main(["--json", str(out_json), "--lcov", str(out_lcov), "--fail-under", "80", "--min-file", "80"]) == 0
        assert json.loads(out_json.read_text())["line_pct"] == 90.0
        assert out_lcov.is_file()
        assert js_coverage.main(["--fail-under", "95", "--min-file", "95"]) == 1

    with mock.patch.object(js_coverage.shutil, "which", return_value=None):
        assert js_coverage.main([]) == 1


def test_swift_coverage_parser_stub_and_cli(tmp_path, monkeypatch):
    sample = {
        "data": [{
            "files": [
                {"filename": "/repo/app/Sources/Config.swift", "summary": {"lines": {"covered": 80, "count": 100}}},
                {"filename": "/repo/app/Sources/Views.swift", "summary": {"lines": {"covered": 40, "count": 100}}},
                {"filename": "/repo/app/Sources/SelfTest.swift", "summary": {"lines": {"covered": 100, "count": 100}}},
            ]
        }]
    }
    parsed = swift_coverage.parse_llvm_cov_export(sample)
    assert "app/Sources/SelfTest.swift" not in parsed["files"]
    assert parsed["core_logic_pct"] == 80.0
    assert parsed["line_pct"] == 60.0

    stub_env = swift_coverage._setup_stub_home(tmp_path)
    assert (tmp_path / "bin" / "claude").is_file()
    assert stub_env["GREENLIGHT_HOME"] == str(tmp_path / "home")

    # Non-darwin returns 0 skip
    monkeypatch.setattr(swift_coverage.sys, "platform", "linux")
    assert swift_coverage.main([]) == 0

    # Darwin full mocked pipeline
    monkeypatch.setattr(swift_coverage.sys, "platform", "darwin")
    out_json = tmp_path / "sw.json"
    out_lcov = tmp_path / "sw.info"

    def fake_run(cmd, **kw):
        s = " ".join(str(x) for x in cmd)
        if "-summary-only" in s:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(sample), stderr="")
        if "-format=lcov" in s:
            return subprocess.CompletedProcess(cmd, 0, stdout="SF:app/Sources/Config.swift\nend_of_record\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with (
        mock.patch.object(swift_coverage, "_find_tool", side_effect=lambda name: ["xcrun", name]),
        mock.patch.object(swift_coverage, "_arch_prefix", return_value=[]),
        mock.patch.object(swift_coverage.subprocess, "run", side_effect=fake_run),
    ):
        assert swift_coverage.main(["--include-ui", "--json", str(out_json), "--lcov", str(out_lcov), "--fail-under", "50", "--fail-under-logic", "75"]) == 0
        assert json.loads(out_json.read_text())["core_logic_pct"] == 80.0
        assert out_lcov.is_file()
        assert swift_coverage.main(["--skip-attach", "--fail-under", "90", "--fail-under-logic", "90"]) == 1


def test_coverage_summary_cli(tmp_path, monkeypatch, capsys):
    def fake_run_json(cmd, json_path):
        s = " ".join(str(x) for x in cmd)
        if "run_py_tests.py" in s:
            return True, {"line_pct": 86.0, "branch_pct": 80.5, "lines_covered": 860, "lines_valid": 1000}
        if "js_coverage.py" in s:
            return True, {"line_pct": 91.2, "lines_covered": 2900, "lines_valid": 3178}
        return True, {"core_logic_pct": 52.4, "core_logic_covered": 2486, "core_logic_valid": 4744, "line_pct": 39.4, "lines_covered": 6234, "lines_valid": 15818}

    monkeypatch.setattr(coverage_summary.sys, "platform", "darwin")
    with mock.patch.object(coverage_summary, "_run_json", side_effect=fake_run_json):
        assert coverage_summary.main(["--include-ui"]) == 0
    out = capsys.readouterr().out
    assert "UNIFIED COVERAGE SUMMARY" in out
    assert "86.00% line" in out and "91.20% line" in out and "52.40% line" in out
