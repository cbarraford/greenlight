"""Unit tests for Greenlight's quality guardrails: check_safety.py, check_diff_quality.py, run_py_tests.py, and agent_hook.py."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

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
    (tmp / "app" / "Sources" / "Config.swift").write_text('let ud: UserDefaults = UserDefaults.standard\n')
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
    (tmp / "scripts" / "drift_baseline.json").write_text("{}\n")


def init_git(repo: Path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)


# 1. Clean synthetic repo passes every check_safety guardrail
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    make_clean_repo(root)
    assert check_safety.run_all(root) == [], check_safety.run_all(root)

    # 2. Catches single-line, multi-line, inner-paren, leading-paren, and triple-quoted raw interpolation in evaluateJavaScript
    (root / "app" / "Sources" / "App.swift").write_text(
        'web.evaluateJavaScript("mc.open(\\"\\(name)\\")")\n'
        'web.evaluateJavaScript(\n    "term.go(\'\\(x)\')"\n)\n'
        'web.evaluateJavaScript("foo(1); bar(\'\\(u)\')")\n'
        'web.evaluateJavaScript("""\n  mc.open("\\(u)")\n""")\n'
        'cb() { web.evaluateJavaScript(\n    "mc.open(\'\\(u)\')"\n) }\n'
    )
    eval_errs = check_safety.check_swift_eval_js(root)
    assert len(eval_errs) == 5, eval_errs
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

    # 6. Catches missing CSP and inline handlers (even on the same line as allowed avatar onerror)
    (root / "app" / "dashboard.html").write_text('<button onclick="evil()">x</button>\n')
    html_errs = check_safety.check_html_csp_and_handlers(root)
    assert len(html_errs) == 2, html_errs
    (root / "app" / "dashboard.html").write_text(
        '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">\n'
        '<img onerror="this.remove()" onclick="evil()">\n'
    )
    assert len(check_safety.check_html_csp_and_handlers(root)) == 1
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

# 8. run_py_tests classifies and executes script, pytest, hybrid ('both'), syntax-error, and empty suites + coverage
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    f_pytest = d / "test_a.py"
    f_pytest.write_text("def test_one():\n    assert 1 == 1\n")
    f_script = d / "test_b.py"
    f_script.write_text("assert 2 + 2 == 4\n")
    f_both = d / "test_both.py"
    f_both.write_text('def test_ok():\n    assert True\nif __name__ == "__main__":\n    assert False\n')
    assert run_py_tests.classify_test_file(f_pytest) == "pytest"
    assert run_py_tests.classify_test_file(f_script) == "script"
    assert run_py_tests.classify_test_file(f_both) == "both"
    # Hybrid file fails because its __main__ block asserts False (not silently skipped by pytest)
    assert run_py_tests.main(["--tests-dir", str(d)]) == 1
    f_both.unlink()
    assert run_py_tests.main(["--tests-dir", str(d)]) == 0

    # Empty and SyntaxError test files fail cleanly without crashing collection
    (d / "test_empty.py").write_text('"""doc only"""\ndef helper():\n    return 1\n')
    (d / "test_syntax.py").write_text("def broken(:\n")
    assert run_py_tests.main(["--tests-dir", str(d)]) == 1

# 9. check_diff_quality: Swift evalJS strictness, hunk-safe parsing, staged blobs, base-ref fail-closed, legacy sigs
sample_diff = """diff --git a/app/Sources/App.swift b/app/Sources/App.swift
--- a/app/Sources/App.swift
+++ b/app/Sources/App.swift
@@ -10,0 +11,5 @@
+web.evaluateJavaScript("mc.run(\\(x))")
+web.evaluateJavaScript("term.focus()")
+web.evaluateJavaScript("mc.ok(\\(y))") // safety-ok
+web.evaluateJavaScript("alert(1); term.focus()")
+web.evaluateJavaScript("ok()"); web.evaluateJavaScript("evil(\\(x))")
"""
added = check_diff_quality.parse_added_lines(sample_diff)
assert list(added["app/Sources/App.swift"]) == [11, 12, 13, 14, 15], added
swift_errs = check_diff_quality.check_new_swift_eval_js(added)
assert len(swift_errs) == 3, swift_errs
assert all(f":{ln}:" in "".join(swift_errs) for ln in (11, 14, 15)), swift_errs

# Adding literal `++ /dev/null` inside a hunk must not corrupt parse_added_lines
tricky_diff = """diff --git a/scripts/m.py b/scripts/m.py
--- a/scripts/m.py
+++ b/scripts/m.py
@@ -1,0 +2,2 @@
+++ /dev/null
+breakpoint()
"""
parsed_tricky = check_diff_quality.parse_added_lines(tricky_diff)
assert parsed_tricky == {"scripts/m.py": {2: "++ /dev/null", 3: "breakpoint()"}}, parsed_tricky

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "scripts").mkdir()
    big_fn = "def huge(a=1):\n" + "".join(f"    x{i} = {i}\n" for i in range(105)) + "    return x0\n"
    (root / "scripts" / "big.py").write_text(big_fn)
    assert len(check_diff_quality.check_new_func_lengths(root, {"scripts/big.py": {1: "def huge(a=1):"}}, max_lines=100)) == 1
    assert check_diff_quality.check_new_func_lengths(root, {"scripts/big.py": {50: "    x49 = 49"}}, max_lines=100) == []

    # 10. Ruff, mypy, shellcheck, diff.noprefix=true, missing --base, --staged blob check, legacy sig edit
    (root / "scripts" / "typed.py").write_text(
        "def old_fn(x: int) -> int:\n    return 'bad-legacy'\ndef new_fn(y: int) -> int:\n    return 'bad-new'\n"
    )
    if shutil.which("uv") or shutil.which("mypy"):
        mypy_errs = check_diff_quality.check_mypy_on_added(root, {"scripts/typed.py": {4: "    return 'bad-new'"}})
        assert len(mypy_errs) == 1 and ":4:" in mypy_errs[0], mypy_errs
        assert check_diff_quality.check_mypy_on_added(root, {"scripts/typed.py": {3: "def new_fn(y: int) -> int:"}}) == []

    if shutil.which("ruff"):
        (root / "scripts" / "linted.py").write_text("breakpoint()\nx = 1\n")
        assert any("T100" in e for e in check_diff_quality.check_ruff_on_added(root, {"scripts/linted.py": {1: "breakpoint()"}}))
        assert check_diff_quality.check_ruff_on_added(root, {"scripts/linted.py": {2: "x = 1"}}) == []

    if shutil.which("shellcheck"):
        (root / "scripts" / "bad.sh").write_text('#!/usr/bin/env bash\necho "$unassigned_var"\n')
        assert any("SC2154" in e for e in check_diff_quality.check_changed_shell(root, {"scripts/bad.sh": {2: 'echo "$unassigned_var"'}}))
        (root / "scripts" / "good.sh").write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "$1"\n')
        assert check_diff_quality.check_changed_shell(root, {"scripts/good.sh": {3: 'echo "$1"'}}) == []

    git_repo = root / "repo"
    git_repo.mkdir()
    init_git(git_repo)
    subprocess.run(["git", "-C", str(git_repo), "config", "diff.noprefix", "true"], check=True)
    (git_repo / "scripts").mkdir()
    (git_repo / "scripts" / "mod.py").write_text(big_fn)
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "init"], check=True)
    # Missing explicit --base must fail closed (exit 1), never silently exit 0
    assert check_diff_quality.main(["--path", str(git_repo), "--base", "origin/does-not-exist", "--skip-mypy"]) == 1
    assert check_diff_quality.main(["--path", str(git_repo), "--base", "main", "--skip-mypy"]) == 0
    # Editing signature of a pre-existing >100-line function must NOT trigger the new-function length cap
    (git_repo / "scripts" / "mod.py").write_text(big_fn.replace("def huge(a=1):", "def huge(a=2):"))
    assert check_diff_quality.main(["--path", str(git_repo), "--base", "main", "--skip-mypy"]) == 0
    # --staged checks the staged blob even if working tree was reverted AND works with diff.noprefix=true
    (git_repo / "scripts" / "mod.py").write_text("x = 1\nbreakpoint()\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "scripts/mod.py"], check=True)
    (git_repo / "scripts" / "mod.py").write_text("x = 1\n")  # revert working tree without re-staging
    if shutil.which("ruff"):
        assert check_diff_quality.main(["--path", str(git_repo), "--staged", "--skip-mypy"]) == 1

# 11. agent_hook: bypasses, zero auto-approve, zero false positives, relative paths, termination reasons, configs
assert agent_hook.handle_agy_pre_tool({"toolCall": {"args": {"CommandLine": "rm -rf /"}}}) == {"decision": "ask"}
assert agent_hook.handle_agy_pre_tool({"toolCall": {"args": {"CommandLine": "git commit -m 'fix: block --no-verify'"}}}) == {"decision": "ask"}
assert agent_hook.handle_agy_pre_tool({"toolCall": {"args": {"CommandLine": 'echo "git commit --no-verify"'}}}) == {"decision": "ask"}
for bad_cmd in (
    "git commit --no-verify -m x",
    "git commit -n -m x",
    "git commit -anm x",
    "git -C /tmp/repo commit --no-verify -m x",
    "git -c core.hooksPath=/dev/null commit -m x",
    'git commit -m "a; b" --no-verify',
    "git commit \\\n  --no-verify -m x",
    "git push --no-verify origin feat",
):
    assert agent_hook.handle_agy_pre_tool({"toolCall": {"args": {"CommandLine": bad_cmd}}})["decision"] == "deny", bad_cmd
    assert agent_hook.handle_claude_pre_bash({"tool_input": {"command": bad_cmd}})[0] == 2, bad_cmd

assert agent_hook.handle_agy_stop({"continuationActive": True, "cwd": str(ROOT)}) == {}
assert agent_hook.handle_agy_stop({"terminationReason": "MAX_INVOCATIONS", "cwd": str(ROOT)}) == {}
assert agent_hook.handle_agy_stop({"terminationReason": "MAX_TOKEN_BUDGET_EXCEEDED", "cwd": str(ROOT)}) == {}
assert agent_hook.handle_claude_pre_bash({"tool_input": {"command": "git push origin feat"}})[0] == 0

with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / "proj"
    outside = Path(td) / "outside.py"
    outside.write_text("x = undefined_var\n")
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app").mkdir()
    (repo / "scripts" / "check_safety.py").write_text("import sys\n\nsys.exit(0)\n")
    bad_py = repo / "scripts" / "bad.py"
    bad_py.write_text("x = undefined_var\n")
    # Relative path resolved against cwd, and files outside cwd are ignored
    if shutil.which("ruff"):
        code, msg = agent_hook.handle_claude_post_edit({"tool_input": {"file_path": "scripts/bad.py"}, "cwd": str(repo)}, root=repo)
        assert code == 2 and "F821" in msg, (code, msg)
    assert agent_hook.handle_claude_post_edit({"tool_input": {"file_path": str(outside)}, "cwd": str(repo)}, root=repo) == (0, "")
    bad_py.write_text("x = 1\n")
    assert agent_hook.handle_claude_post_edit({"tool_input": {"file_path": "scripts/bad.py"}, "cwd": str(repo)}, root=repo) == (0, "")

    init_git(repo)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    assert agent_hook.handle_agy_stop({"continuationActive": False, "cwd": str(repo)}, root=repo) == {}
    if shutil.which("ruff"):
        bad_py.write_text("x = undefined_var\n")
        stop_res = agent_hook.handle_agy_stop({"continuationActive": False, "cwd": str(repo)}, root=repo)
        assert stop_res.get("decision") == "continue" and "ruff failed" in stop_res.get("reason", ""), stop_res
        bad_py.write_text("x = 2\n")
        assert agent_hook.handle_agy_stop({"continuationActive": False, "cwd": str(repo)}, root=repo) == {}

    with mock.patch("sys.stdin", io.StringIO('{"toolCall":{"args":{"CommandLine":"git commit --no-verify"}}}')):
        assert agent_hook.main(["--agy-pre-tool"]) == 0
    with mock.patch("sys.stdin", io.StringIO('{"continuationActive":true}')):
        assert agent_hook.main(["--agy-stop"]) == 0
    with mock.patch("sys.stdin", io.StringIO('{"tool_input":{"command":"git push --no-verify"}}')):
        assert agent_hook.main(["--claude-pre-bash"]) == 2
    with mock.patch("sys.stdin", io.StringIO('{"tool_input":{"file_path":""}}')):
        assert agent_hook.main(["--claude-post-edit"]) == 0

# 12. Committed hook config files are valid JSON and Claude Code hooks use $CLAUDE_PROJECT_DIR
agy_hooks = json.loads((ROOT / ".agents" / "hooks.json").read_text())
assert "greenlight-quality-guards" in agy_hooks
claude_settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
for event in ("PreToolUse", "PostToolUse"):
    cmds = [h["command"] for grp in claude_settings["hooks"][event] for h in grp["hooks"]]
    assert cmds and all("$CLAUDE_PROJECT_DIR" in c for c in cmds), cmds

print("ok  test_check_safety.py passed")
