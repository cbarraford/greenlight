"""Tests for scripts/update.py: versioned backend installs, the locked swap, smoke rollback, adopt, and the scheduler path."""
import fcntl
import io
import json
import os
import shutil
import tarfile
import time
from pathlib import Path
from unittest import mock

import pytest

import auto
import diag
import update

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture()
def env(gl_home, tmp_path, monkeypatch):
    """A fake $HOME (so no real scheduler unit is read) and a skill path that doesn't exist yet."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GREENLIGHT_SKILL", str(home / ".claude" / "skills" / "greenlight"))
    return {"gl": gl_home, "home": home, "skill": home / ".claude" / "skills" / "greenlight", "tmp": tmp_path}


def backend(at: Path, ver: str | None = "1.0.0", broken: bool = False) -> Path:
    """A minimal backend tree: the real diag.py and update.py (the smoke test runs them), and a stub greenlight.py."""
    (at / "scripts").mkdir(parents=True)
    for f in ("diag.py", "update.py"):
        shutil.copy(SCRIPTS / f, at / "scripts" / f)
    (at / "scripts" / "greenlight.py").write_text("def broken(:\n" if broken else "print('hi')\n")
    (at / "SKILL.md").write_text("# skill\n")
    if ver:
        (at / "VERSION").write_text(ver + "\n")
    (at / ".git").mkdir()
    (at / "memory").mkdir()
    (at / "memory" / "note.md").write_text("from the source\n")
    return at


def installed(env, ver: str, **kw) -> dict:
    return update.install(backend(env["tmp"] / f"src-{ver}", ver, **kw))


def test_install_from_folder_skips_git_and_memory_and_links_shared_memory(env):
    out = installed(env, "1.0.0")
    assert out == {"ok": True, "version": "1.0.0", "installed": "new"}
    root = update.versions_dir() / "1.0.0"
    assert (root / "scripts" / "update.py").is_file() and not (root / ".git").exists()
    assert (root / "memory").is_symlink() and (root / "memory").resolve() == (update.code_dir() / "memory").resolve()
    assert not (update.code_dir() / "memory" / "note.md").exists()  # the source's notes never overwrite the user's
    assert update.install(env["tmp"] / "src-1.0.0")["installed"] == "existing"
    assert not [p for p in update.versions_dir().iterdir() if p.name.startswith(".stage-")]


def test_install_version_flag_wins_and_a_bad_version_is_refused(env):
    src = backend(env["tmp"] / "src", ver=None)
    with pytest.raises(update.UpdateError) as e:
        update.install(src)
    assert e.value.reason == "bad_version"
    assert update.install(src, "main-5-abc1234")["version"] == "main-5-abc1234"
    with pytest.raises(update.UpdateError) as e:
        update.install(src, "../evil")
    assert e.value.reason == "bad_version"


def test_install_refuses_an_incomplete_backend(env):
    src = backend(env["tmp"] / "src")
    (src / "SKILL.md").unlink()
    with pytest.raises(update.UpdateError) as e:
        update.install(src)
    assert e.value.reason == "incomplete"
    assert not (update.versions_dir() / "1.0.0").exists()


def _tar(path: Path, entries: dict) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, spec in entries.items():
            ti = tarfile.TarInfo(name)
            if isinstance(spec, tuple):  # ("sym", target)
                ti.type, ti.linkname = tarfile.SYMTYPE, spec[1]
                tf.addfile(ti)
            else:
                data = spec.encode()
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return path


def test_install_from_tarball_lifts_a_single_top_folder(env):
    src = backend(env["tmp"] / "tree", "2.0.0")
    tgz = env["tmp"] / "b.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(src, arcname="greenlight-2.0.0")
    assert update.install(tgz)["version"] == "2.0.0"
    root = update.versions_dir() / "2.0.0"
    assert (root / "SKILL.md").is_file() and not (root / ".git").exists() and (root / "memory").is_symlink()


@pytest.mark.parametrize("entries", [{"../x": "boom"}, {"/abs": "boom"}, {"SKILL.md": "x", "link": ("sym", "../../outside")}])
def test_install_refuses_unsafe_tarballs(env, entries):
    with pytest.raises(update.UpdateError) as e:
        update.install(_tar(env["tmp"] / "bad.tar.gz", entries))
    assert e.value.reason == "unsafe_archive"
    assert not (env["tmp"] / "x").exists()


def test_swap_links_current_and_the_skill_then_rollback_goes_back(env):
    installed(env, "1.0.0")
    installed(env, "1.1.0")
    first = update.swap("1.0.0", wait=1)
    assert first["changed"] and first["previous"] is None
    assert env["skill"].is_symlink() and update.managed() == "versions"
    assert (env["skill"] / "SKILL.md").is_file()
    assert os.readlink(update.current_link()) == os.path.join("versions", "1.0.0")  # relative: the home can move
    second = update.swap("1.1.0", wait=1)
    assert (second["current"], second["previous"]) == ("1.1.0", "1.0.0")
    assert update.status()["current"] == "1.1.0" and update.status()["versions"] == ["1.0.0", "1.1.0"]
    assert update.swap("1.1.0", wait=1)["changed"] is False
    back = update.rollback(wait=1)
    assert (back["current"], back["previous"]) == ("1.0.0", "1.1.0")


def test_a_version_that_fails_the_smoke_test_is_switched_back(env):
    installed(env, "1.0.0")
    installed(env, "1.1.0", broken=True)
    update.swap("1.0.0", wait=1)
    with pytest.raises(update.UpdateError) as e:
        update.swap("1.1.0", wait=1)
    assert (e.value.reason, str(e.value)) == ("smoke_failed", "compile")
    assert update.active() == "1.0.0"
    assert update.status()["previous"] is None  # a failed swap records nothing


def test_a_first_swap_that_fails_leaves_no_current(env):
    installed(env, "1.0.0", broken=True)
    with pytest.raises(update.UpdateError):
        update.swap("1.0.0", wait=1)
    assert update.active() is None


@pytest.mark.parametrize("kind", ["git", "copy"])
def test_swap_never_touches_a_git_checkout_or_a_copy(env, kind):
    installed(env, "1.0.0")
    env["skill"].mkdir(parents=True)
    if kind == "git":
        (env["skill"] / ".git").mkdir()
    with pytest.raises(update.UpdateError) as e:
        update.swap("1.0.0", wait=1)
    assert e.value.reason == f"managed_{kind}"
    assert update.active() is None and not env["skill"].is_symlink()


def test_swap_refuses_state_it_cannot_read_and_versions_not_installed(env):
    installed(env, "1.0.0")
    (env["gl"] / "schema.json").write_text(json.dumps({"schema": 9}))
    with pytest.raises(update.UpdateError) as e:
        update.swap("1.0.0", wait=1)
    assert e.value.reason == "schema"
    with pytest.raises(update.UpdateError) as e:
        update.swap("9.9.9", wait=1)
    assert e.value.reason == "not_installed"


def test_swap_waits_for_a_running_tick_and_gives_up_busy(env):
    installed(env, "1.0.0")
    with open(env["gl"] / "orchestrate.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        t0 = time.monotonic()
        with pytest.raises(update.UpdateError) as e:
            update.swap("1.0.0", wait=0.6)
        assert e.value.reason == "busy" and time.monotonic() - t0 >= 0.5
    assert update.active() is None
    assert update.swap("1.0.0", wait=1)["ok"]


def test_adopt_turns_a_copy_into_a_version_and_keeps_its_notes(env):
    s = env["skill"]
    backend(s, "0.9.0")
    shutil.rmtree(s / ".git")
    (update.code_dir() / "memory").mkdir(parents=True)
    (update.code_dir() / "memory" / "note.md").write_text("already here\n")
    (s / "memory" / "mine.md").write_text("written by a run\n")
    out = update.adopt(wait=1)
    assert out["current"] == "0.9.0" and update.managed() == "versions"
    mem = update.code_dir() / "memory"
    assert (mem / "mine.md").read_text() == "written by a run\n"
    assert (mem / "note.md").read_text() == "already here\n"  # an existing note wins
    assert (s / "memory" / "mine.md").read_text() == "written by a run\n"  # through the links
    with pytest.raises(update.UpdateError) as e:
        update.adopt(wait=1)
    assert e.value.reason == "managed_versions"


def test_prune_keeps_current_and_previous_and_recent_ones(env):
    for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        installed(env, v)
        update.swap(v, wait=1)
    st = update.read_json(update.state_file(), {})
    assert set(st["retired"]) == {"1.0.0", "1.1.0", "1.2.0"}
    assert update.prune(now=time.time()) == []  # all replaced moments ago
    gone = update.prune(now=time.time() + update.RETIRE_SECS + 5)
    assert sorted(gone) == ["1.0.0", "1.1.0"]  # 1.2.0 is the previous one
    assert update.status()["versions"] == ["1.2.0", "1.3.0"]


def test_stable_script_goes_through_current_only_for_a_versioned_install(env):
    installed(env, "1.0.0")
    inside = update.versions_dir() / "1.0.0" / "scripts" / "auto.py"
    assert update.stable_script("auto.py", str(inside)) == update.current_link() / "scripts" / "auto.py"
    assert update.stable_script("auto.py", str(SCRIPTS / "auto.py")) == SCRIPTS / "auto.py"


def test_auto_install_writes_the_stable_path_into_the_unit(env, monkeypatch):
    installed(env, "1.0.0")
    inside = update.versions_dir() / "1.0.0" / "scripts" / "auto.py"
    monkeypatch.setattr(auto, "__file__", str(inside))
    monkeypatch.setattr(auto.sys, "argv", ["auto.py", "install"])
    monkeypatch.setattr(auto.sys, "platform", "linux")
    with mock.patch.object(auto.subprocess, "run"):
        auto.main()
    unit = (env["home"] / ".config/systemd/user/greenlight-auto.service").read_text()
    assert str(update.current_link() / "scripts" / "auto.py") in unit and "versions" not in unit


def test_reschedule_reinstalls_a_unit_that_runs_a_version_folder(env):
    unit = env["home"] / ".config/systemd/user/greenlight-auto.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"ExecStart=python3 {update.versions_dir() / '1.0.0' / 'scripts' / 'auto.py'} tick\n")
    with mock.patch.object(update.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
        assert update.reschedule() == "reinstalled"
    assert run.call_args.args[0][1:] == [str(update.current_link() / "scripts" / "auto.py"), "install"]
    unit.write_text(f"ExecStart=python3 {update.current_link() / 'scripts' / 'auto.py'} tick\n")
    assert update.reschedule() == "unchanged"
    unit.write_text("ExecStart=python3 /src/greenlight/scripts/auto.py tick\n")  # a git checkout's own unit: not ours
    assert update.reschedule() == "unchanged"


def test_cli_reports_json_exit_codes_and_an_enum_only_event(env, capsys):
    assert update.main(["version"]) == 0
    v = json.loads(capsys.readouterr().out)
    assert (v["schema"], v["compat"], v["protocol"]) == (update.SCHEMA, list(update.COMPAT), update.PROTOCOL)
    assert update.main(["swap", "nope"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["reason"]) == (False, "not_installed")
    ev = [e for e in diag.read() if e.get("kind") == "update_apply"][-1]
    assert (ev["op"], ev["outcome"]) == ("swap", "not_installed") and "detail" not in ev
    assert update.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["managed"] == "none"


def test_diag_doctor_quick_skips_network_checks_and_version_prints(env, capsys):
    with mock.patch.object(diag.subprocess, "run") as run:
        checks = {c["check"] for c in diag.doctor(quick=True)["checks"]}
    assert not run.called and not {"glab_auth", "gh_auth"} & checks
    with pytest.raises(SystemExit):
        diag.main(["doctor", "--quick"])
    assert "checks" in json.loads(capsys.readouterr().out)
    diag.main(["version"])
    assert json.loads(capsys.readouterr().out)["protocol"] == update.PROTOCOL
