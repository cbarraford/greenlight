"""Functional tests for shell scripts: scripts/vm_test.sh, app/build.sh, and .githooks/pre-commit."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_vm_test_setup_dry_run_and_stale_lock_reclamation(tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path), "GREENLIGHT_VM_BACKEND": "none"}

    # --setup prints instructions
    r_setup = subprocess.run(["bash", str(ROOT / "scripts/vm_test.sh"), "--setup"], capture_output=True, text=True, env=env)
    assert r_setup.returncode == 0 and "Tart" in r_setup.stdout and "Lume" in r_setup.stdout

    # --dry-run acquires and releases an atomic slot lock
    r_dry = subprocess.run(["bash", str(ROOT / "scripts/vm_test.sh"), "--dry-run"], capture_output=True, text=True, env=env)
    assert r_dry.returncode == 0 and "acquired atomic VM slot" in r_dry.stdout
    assert not (tmp_path / "greenlight-vm-locks" / "slot-1.lock").exists()

    # Stale lock with dead PID is reclaimed automatically (even with 1 slot)
    lock1 = tmp_path / "greenlight-vm-locks" / "slot-1.lock"
    lock1.mkdir(parents=True)
    (lock1 / "pid").write_text("99999999\n")
    r_reclaim = subprocess.run(
        ["bash", str(ROOT / "scripts/vm_test.sh"), "--dry-run"],
        capture_output=True, text=True, env={**env, "GREENLIGHT_VM_SLOTS": "1"},
    )
    assert r_reclaim.returncode == 0 and "slot-1.lock" in r_reclaim.stdout
    assert not lock1.exists()

    # --run with backend=none exits 2 cleanly
    r_none = subprocess.run(["bash", str(ROOT / "scripts/vm_test.sh"), "--run"], capture_output=True, text=True, env=env)
    assert r_none.returncode == 2 and "neither tart nor lume" in r_none.stderr

    # Unknown flag exits 2
    r_bad = subprocess.run(["bash", str(ROOT / "scripts/vm_test.sh"), "--bogus-flag"], capture_output=True, text=True, env=env)
    assert r_bad.returncode == 2


def test_build_sh_rejects_invalid_app_names():
    for bad in ("../evil", ".hidden", "-dash", "bad name", "a" * 62):
        env = {**os.environ, "GREENLIGHT_APP": bad}
        r = subprocess.run(["bash", str(ROOT / "app/build.sh")], capture_output=True, text=True, env=env)
        assert r.returncode == 1, f"build.sh should reject GREENLIGHT_APP={bad!r}"
        assert "build.sh: error:" in r.stderr


def test_pre_commit_hook_blocks_bad_staged_python(tmp_path):
    # Create an isolated git repo with check_safety.py and check_diff_quality.py linked in
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GIT_")
    }
    git_env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env, check=True)

    git("init")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "check_safety.py").write_text("print('ok')\n")
    (repo / "scripts" / "check_diff_quality.py").write_text("print('ok')\n")
    (repo / "good.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-m", "init")

    # Stage a file with a real ruff F821 undefined name error
    (repo / "bad.py").write_text("print(undefined_symbol_xyz)\n")
    git("add", "bad.py")
    r_bad = subprocess.run(["bash", str(ROOT / ".githooks/pre-commit")], cwd=repo, capture_output=True, text=True, env=git_env)
    assert r_bad.returncode != 0, "pre-commit should block staged undefined variable"

    # Fix it and verify pre-commit passes
    (repo / "bad.py").write_text("val = 42\nprint(val)\n")
    git("add", "bad.py")
    r_ok = subprocess.run(["bash", str(ROOT / ".githooks/pre-commit")], cwd=repo, capture_output=True, text=True, env=git_env)
    assert r_ok.returncode == 0, r_ok.stdout + r_ok.stderr

    # BUG-INFRA-15: pre-commit also shellchecks staged .githooks/* files
    (repo / ".githooks").mkdir()
    (repo / ".githooks" / "sample-hook").write_text('#!/usr/bin/env bash\necho "$unassigned_hook_var"\n')
    git("add", ".githooks/sample-hook")
    r_hook_bad = subprocess.run(["bash", str(ROOT / ".githooks/pre-commit")], cwd=repo, capture_output=True, text=True, env=git_env)
    assert r_hook_bad.returncode != 0, "pre-commit should shellcheck staged .githooks/* scripts"
    (repo / ".githooks" / "sample-hook").write_text('#!/usr/bin/env bash\nset -euo pipefail\necho "ok"\n')
    git("add", ".githooks/sample-hook")
    r_hook_ok = subprocess.run(["bash", str(ROOT / ".githooks/pre-commit")], cwd=repo, capture_output=True, text=True, env=git_env)
    assert r_hook_ok.returncode == 0, r_hook_ok.stdout + r_hook_ok.stderr


def test_release_sh_resolves_relative_backend_and_out_before_cd(tmp_path):
    # BUG-INFRA-3: passing relative backend/out paths from another cwd must not fail `[ -f "$backend" ]`
    rel_dir = tmp_path / "dist"
    rel_dir.mkdir()
    (rel_dir / "backend.tar.gz").write_bytes(b"placeholder")
    # Put a failing swiftc shim on PATH so release.sh stops right after path checks and mkdir -p "$out"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "swiftc").write_text("#!/usr/bin/env bash\necho swiftc-stub-stop >&2\nexit 42\n")
    (fake_bin / "swiftc").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(
        ["bash", str(ROOT / "app/release.sh"), "1.0.0", "1.0.0", "1", "dist/backend.tar.gz", "dist/out"],
        cwd=tmp_path, capture_output=True, text=True, env=env,
    )
    assert "no backend at" not in r.stderr, r.stderr
    assert (tmp_path / "dist" / "out").is_dir()
    assert not (ROOT / "app" / "dist").exists()

