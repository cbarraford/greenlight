"""Unit tests for Greenlight's quality guardrails: check_safety.py, check_diff_quality.py, and run_py_tests.py."""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent_hook
import check_diff_quality
import check_safety
import run_py_tests


def make_clean_repo(tmp: Path):
    (tmp / "app" / "Sources").mkdir(parents=True)
    (tmp / "scripts").mkdir(parents=True)
    (tmp / "tests").mkdir(parents=True)
    (tmp / "app" / "Sources" / "Config.swift").write_text(
        'let ud: UserDefaults = UserDefaults.standard\n'
    )
    (tmp / "app" / "Sources" / "App.swift").write_text(
        'web.callJS("mc.openPage", [name])\n'
        'ud.set(true, forKey: "flag")\n'
    )
    (tmp / "app" / "Sources" / "VoiceEngine.swift").write_text(
        'lazy var stageWeb: WKWebView = {\n    let v = WKWebView()\n    return v\n}()\n'
    )
    (tmp / "scripts" / "ok.py").write_text('import subprocess\nsubprocess.run(["ls"])\n')
    for page in ("dashboard.html", "terminal.html", "stage.html"):
        (tmp / "app" / page).write_text(
            '<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'self\'">'
            '<script>const ok = 1 + 2;</script>\n'
        )
    (tmp / "tests" / "test_ok.cjs").write_text("const x = 42;\n")


# 1. Clean synthetic repo passes every check_safety guardrail
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    make_clean_repo(root)
    assert check_safety.run_all(root) == [], check_safety.run_all(root)

    # 2. Catches single-line and multi-line raw quoted interpolation in evaluateJavaScript
    (root / "app" / "Sources" / "App.swift").write_text(
        'web.evaluateJavaScript("mc.open(\\"\\(name)\\")")\n'
        'web.evaluateJavaScript(\n    "term.go(\'\\(x)\')"\n)\n'
    )
    eval_errs = check_safety.check_swift_eval_js(root)
    assert len(eval_errs) == 2, eval_errs
    (root / "app" / "Sources" / "App.swift").write_text(
        'web.evaluateJavaScript("mc.open(\\"\\(name)\\")") // safety-ok\n'
    )
    assert check_safety.check_swift_eval_js(root) == []

    # 3. Catches stray UserDefaults.standard outside Config.swift / App.swift cleanup
    (root / "app" / "Sources" / "Views.swift").write_text('let x = UserDefaults.standard.bool(forKey: "a")\n')
    ud_errs = check_safety.check_swift_user_defaults(root)
    assert len(ud_errs) == 1 and "Views.swift:1" in ud_errs[0], ud_errs
    (root / "app" / "Sources" / "Views.swift").unlink()

    # 4. Catches WKScriptMessageHandler attached to stageWeb
    (root / "app" / "Sources" / "VoiceEngine.swift").write_text(
        'lazy var stageWeb: WKWebView = {\n    c.userContentController.add(self, name: "x")\n}()\n'
    )
    assert len(check_safety.check_stage_web_isolated(root)) == 1

    # 5. Catches unapproved shell=True in scripts/ AND app/kokoro.py
    (root / "scripts" / "bad.py").write_text('subprocess.run("ls", shell=True)\n')
    (root / "app" / "kokoro.py").write_text('subprocess.run("echo", shell=True)\n')
    sh_errs = check_safety.check_python_shell_true(root)
    assert len(sh_errs) == 2, sh_errs
    (root / "scripts" / "bad.py").write_text('subprocess.run("ls", shell=True)  # noqa: S602\n')
    (root / "app" / "kokoro.py").unlink()
    assert check_safety.check_python_shell_true(root) == []

    # 6. Catches missing CSP and inline event handlers (while allowing avatar onerror)
    (root / "app" / "dashboard.html").write_text('<button onclick="evil()">x</button>\n')
    html_errs = check_safety.check_html_csp_and_handlers(root)
    assert len(html_errs) == 2, html_errs
    (root / "app" / "dashboard.html").write_text(
        '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">'
        '<img onerror="this.remove()">\n'
    )
    assert check_safety.check_html_csp_and_handlers(root) == []

    # 7. Catches broken JS syntax & unsafe dynamic JS/debugger in inline <script> and tests/*.cjs
    (root / "app" / "dashboard.html").write_text(
        '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">\n'
        '<script>\nconst x = eval("1+1");\ndebugger;\n</script>\n'
    )
    unsafe_js = check_safety.check_js_unsafe_patterns(root)
    assert len(unsafe_js) == 2, unsafe_js
    if shutil.which("node"):
        (root / "app" / "stage.html").write_text(
            '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">\n'
            '<script>\nfunction broken( { return 1; }\n</script>\n'
        )
        (root / "tests" / "test_ok.cjs").write_text("const = ;\n")
        js_errs = check_safety.check_js_syntax(root)
        assert len(js_errs) == 2, js_errs

# 8. run_py_tests classifies and executes script, pytest, and empty test suites
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    f_pytest = d / "test_a.py"
    f_pytest.write_text("def test_one():\n    assert 1 == 1\n")
    f_script = d / "test_b.py"
    f_script.write_text("assert 2 + 2 == 4\n")
    assert run_py_tests.classify_test_file(f_pytest) == "pytest"
    assert run_py_tests.classify_test_file(f_script) == "script"
    assert run_py_tests.main(["--tests-dir", str(d)]) == 0
    f_empty = d / "test_c.py"
    f_empty.write_text('"""doc only"""\ndef helper():\n    return 1\n')
    assert run_py_tests.classify_test_file(f_empty) == "empty"
    assert run_py_tests.main(["--tests-dir", str(d)]) == 1

# 9. check_diff_quality parses diffs, caps new function length, and blocks new raw evaluateJavaScript
sample_diff = """diff --git a/app/Sources/App.swift b/app/Sources/App.swift
--- a/app/Sources/App.swift
+++ b/app/Sources/App.swift
@@ -10,0 +11,3 @@
+web.evaluateJavaScript("mc.run(\\(x))")
+web.evaluateJavaScript("term.focus()")
+web.evaluateJavaScript("mc.ok(\\(y))") // safety-ok
"""
added = check_diff_quality.parse_added_lines(sample_diff)
assert list(added["app/Sources/App.swift"]) == [11, 12, 13], added
swift_errs = check_diff_quality.check_new_swift_eval_js(added)
assert len(swift_errs) == 1 and ":11:" in swift_errs[0], swift_errs

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "scripts").mkdir()
    big_fn = "def huge():\n" + "".join(f"    x{i} = {i}\n" for i in range(105)) + "    return x0\n"
    (root / "scripts" / "big.py").write_text(big_fn)
    added_big = {"scripts/big.py": {1: "def huge():"}}
    fn_errs = check_diff_quality.check_new_func_lengths(root, added_big, max_lines=100)
    assert len(fn_errs) == 1 and "huge" in fn_errs[0], fn_errs
    # Modifying only the body of an existing function does not trigger the new-function cap
    assert check_diff_quality.check_new_func_lengths(root, {"scripts/big.py": {50: "    x49 = 49"}}, max_lines=100) == []

# 10. check_diff_quality: ruff, mypy, shellcheck, git_diff, and main()
with tempfile.TemporaryDirectory() as td:
    import subprocess
    root = Path(td)
    (root / "scripts").mkdir()
    (root / "scripts" / "typed.py").write_text(
        "def old_fn(x: int) -> int:\n"
        "    return 'bad-legacy'\n"
        "def new_fn(y: int) -> int:\n"
        "    return 'bad-new'\n"
    )
    mypy_errs = check_diff_quality.check_mypy_on_added(root, {"scripts/typed.py": {4: "    return 'bad-new'"}})
    assert len(mypy_errs) == 1 and ":4:" in mypy_errs[0], mypy_errs
    assert check_diff_quality.check_mypy_on_added(root, {"scripts/typed.py": {3: "def new_fn(y: int) -> int:"}}) == []

    # Ruff on added lines catches T10/UP/SIM on changed lines only
    (root / "scripts" / "linted.py").write_text("breakpoint()\nx = 1\n")
    ruff_errs = check_diff_quality.check_ruff_on_added(root, {"scripts/linted.py": {1: "breakpoint()"}})
    assert any("T100" in e for e in ruff_errs), ruff_errs
    assert check_diff_quality.check_ruff_on_added(root, {"scripts/linted.py": {2: "x = 1"}}) == []

    # ShellCheck on changed .sh files (warning-level rule SC2154)
    if shutil.which("shellcheck"):
        (root / "scripts" / "bad.sh").write_text("#!/usr/bin/env bash\necho \"$unassigned_var\"\n")
        sh_errs = check_diff_quality.check_changed_shell(root, {"scripts/bad.sh": {2: 'echo "$unassigned_var"'}})
        assert any("SC2154" in e for e in sh_errs), sh_errs
        (root / "scripts" / "good.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho \"$1\"\n")
        assert check_diff_quality.check_changed_shell(root, {"scripts/good.sh": {3: 'echo "$1"'}}) == []

    # End-to-end git repo diff + main()
    git_repo = root / "repo"
    git_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(git_repo)], check=True)
    subprocess.run(["git", "-C", str(git_repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "config", "user.email", "t@example.invalid"], check=True)
    (git_repo / "scripts").mkdir()
    (git_repo / "scripts" / "mod.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "init"], check=True)
    assert check_diff_quality.main(["--path", str(git_repo), "--base", "main", "--skip-mypy"]) == 0
    (git_repo / "scripts" / "mod.py").write_text("x = 1\nbreakpoint()\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    assert check_diff_quality.main(["--path", str(git_repo), "--staged", "--skip-mypy"]) == 1

# 11. agent_hook blocks --no-verify, checks edited files, handles Stop on dirty repos, and CLI main()
import io
from unittest import mock

assert agent_hook.handle_agy_pre_tool({"toolCall": {"args": {"CommandLine": "git commit --no-verify -m x"}}})["decision"] == "deny"
assert agent_hook.handle_agy_pre_tool({"toolCall": {"args": {"CommandLine": "git commit -m x"}}})["decision"] == "allow"
assert agent_hook.handle_agy_stop({"continuationActive": True, "cwd": str(ROOT)}) == {}
assert agent_hook.handle_claude_pre_bash({"tool_input": {"command": "git push --no-verify"}})[0] == 2
assert agent_hook.handle_claude_pre_bash({"tool_input": {"command": "git push origin feat"}})[0] == 0
with tempfile.TemporaryDirectory() as td:
    import subprocess
    repo = Path(td)
    (repo / "scripts").mkdir()
    (repo / "tests").mkdir()
    (repo / "app").mkdir()
    (repo / "scripts" / "check_safety.py").write_text("import sys\nsys.exit(0)\n")
    bad_py = repo / "scripts" / "bad.py"
    bad_py.write_text("x = undefined_var\n")
    code, msg = agent_hook.handle_claude_post_edit({"tool_input": {"file_path": str(bad_py)}, "cwd": td}, root=repo)
    assert code == 2 and "F821" in msg, (code, msg)
    bad_py.write_text("x = 1\n")
    assert agent_hook.handle_claude_post_edit({"tool_input": {"file_path": str(bad_py)}, "cwd": td}, root=repo) == (0, "")

    # Stop hook triggers continue when tracked edits fail fast lint, and returns {} when clean
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    assert agent_hook.handle_agy_stop({"continuationActive": False, "cwd": td}, root=repo) == {}
    bad_py.write_text("x = undefined_var\n")
    stop_res = agent_hook.handle_agy_stop({"continuationActive": False, "cwd": td}, root=repo)
    assert stop_res.get("decision") == "continue" and "ruff failed" in stop_res.get("reason", ""), stop_res
    bad_py.write_text("x = 2\n")
    clean_res = agent_hook.handle_agy_stop({"continuationActive": False, "cwd": td}, root=repo)
    assert clean_res == {}, clean_res

    # CLI main() dispatch for all 4 modes
    with mock.patch("sys.stdin", io.StringIO('{"toolCall":{"args":{"CommandLine":"git commit --no-verify"}}}')):
        assert agent_hook.main(["--agy-pre-tool"]) == 0
    with mock.patch("sys.stdin", io.StringIO('{"continuationActive":true}')):
        assert agent_hook.main(["--agy-stop"]) == 0
    with mock.patch("sys.stdin", io.StringIO('{"tool_input":{"command":"git push --no-verify"}}')):
        assert agent_hook.main(["--claude-pre-bash"]) == 2
    with mock.patch("sys.stdin", io.StringIO('{"tool_input":{"file_path":""}}')):
        assert agent_hook.main(["--claude-post-edit"]) == 0

print("ok  test_check_safety.py passed")
