"""Tests for the release tooling: scripts/ed25519.py, scripts/release.py, app/release.sh's checks, and the release workflow."""
import base64
import json
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

import ed25519
import release
import update

ROOT = Path(__file__).resolve().parent.parent

# RFC 8032 section 7.1: TEST 1, 2 and 3 (secret, public, message, signature)
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7", "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("sk,pk,msg,sig", VECTORS)
def test_ed25519_matches_rfc8032(sk, pk, msg, sig):
    sk, pk, msg, sig = map(bytes.fromhex, (sk, pk, msg, sig))
    assert ed25519.public_key(sk) == pk and ed25519.sign(sk, msg) == sig and ed25519.verify(pk, msg, sig)
    assert not ed25519.verify(pk, msg + b"x", sig)
    assert not ed25519.verify(pk, msg, sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not ed25519.verify(pk, msg, sig[:32] + ed25519.Q.to_bytes(32, "little"))  # s >= the group order
    assert not ed25519.verify(pk[:31], msg, sig) and not ed25519.verify(pk, msg, sig[:63])
    with pytest.raises(ValueError):
        ed25519.public_key(sk[:31])


def sh(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "tests").mkdir()
    for f, body in {"scripts/greenlight.py": "print(1)\n", "scripts/update.py": "x = 1\n", "scripts/diag.py": "y = 2\n",
                    "SKILL.md": "# skill\n", "LICENSE": "MIT\n", "tests/test_x.py": "def test(): pass\n"}.items():
        (r / f).write_text(body)
    (r / "scripts" / "greenlight.py").chmod(0o755)
    sh(r, "init", "-q", "-b", "main")
    sh(r, "add", "-A")
    sh(r, "commit", "-q", "-m", "first (#1)")
    (r / "scripts" / "untracked.py").write_text("never shipped\n")
    return r


def test_version_for_main_before_and_after_a_stable_tag_and_for_stable(repo):
    commit = sh(repo, "rev-parse", "HEAD")
    v = release.version("main", root=repo)
    assert v == {"channel": "main", "version": f"main-1-{commit[:7]}", "short": "0.0.0", "build": 1, "commit": commit, "tag": f"main-1-{commit[:7]}"}
    assert update.VERSION_RE.fullmatch(v["version"])
    sh(repo, "tag", "v1.2.3")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "second (#2)")
    assert release.version("main", root=repo)["short"] == "1.2.3" and release.version("main", root=repo)["build"] == 2
    s = release.version("stable", "v1.2.3", root=repo)
    assert (s["version"], s["short"], s["tag"]) == ("1.2.3", "1.2.3", "v1.2.3")
    assert release.version("stable", "v0.10.0", root=repo)["version"] == "0.10.0"
    for bad in ("1.2.3", "v1.2", "v1.2.3-rc1", "v01.2.3", "v1.02.3", "v1.2.03", "main", None):  # v1.02.3 would be a second 1.2.3
        with pytest.raises(release.ReleaseError):
            release.version("stable", bad, root=repo)
    with pytest.raises(release.ReleaseError):
        release.version("beta", root=repo)


def test_main_short_version_is_the_highest_real_tag_not_a_nearer_rc(repo):
    sh(repo, "tag", "v1.2.0")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "second (#2)")
    for t in ("v1.3.0-rc1", "v1.3.0.1", "v01.9.0"):
        sh(repo, "tag", t)
    sh(repo, "commit", "-q", "--allow-empty", "-m", "third (#3)")
    assert release.version("main", root=repo)["short"] == "1.2.0"
    sh(repo, "tag", "v1.10.0")
    sh(repo, "tag", "v1.9.0", "HEAD~1")  # older and nearer by date, lower by version
    assert release.version("main", root=repo)["short"] == "1.10.0"


def test_backend_ships_the_commit_not_uncommitted_edits(repo, tmp_path):
    clean = release.backend("main-1-abc1234", tmp_path / "a", root=repo)
    (repo / "SKILL.md").write_text("# edited, not committed\n")
    (repo / "LICENSE").unlink()
    dirty = release.backend("main-1-abc1234", tmp_path / "b", root=repo)
    assert dirty["sha256"] == clean["sha256"]
    with tarfile.open(dirty["path"]) as tf:
        assert tf.extractfile("greenlight-main-1-abc1234/SKILL.md").read() == b"# skill\n"
        assert tf.extractfile("greenlight-main-1-abc1234/LICENSE").read() == b"MIT\n"


def test_guard_lets_the_feed_only_move_forward(signed, tmp_path, capsys):
    assert release.guard(3, None) == {"ok": True, "current": None}
    assert release.guard(3, tmp_path / "none.json") == {"ok": True, "current": None}  # no feed yet: the first build
    assert release.guard(7, signed["out"]) == {"ok": True, "current": 7}  # the same build again: a re-run
    assert release.guard(8, signed["out"])["current"] == 7
    with pytest.raises(release.ReleaseError, match="older than the feed's build 7"):
        release.guard(6, signed["out"])
    capsys.readouterr()
    assert release.main(["guard", "--build", "6", "--current", str(signed["out"])]) == 1
    assert "nothing published" in json.loads(capsys.readouterr().out)["error"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not a feed")
    with pytest.raises(release.ReleaseError, match="can't read"):
        release.guard(9, bad)
    # the feed release's title keeps the floor when a failed upload has already deleted the file
    with pytest.raises(release.ReleaseError, match="older than the feed's build 11"):
        release.guard(9, None, floor=11)
    assert release.guard(11, None, floor=11)["current"] == 11 and release.guard(9, signed["out"], floor=3)["current"] == 7
    with pytest.raises(release.ReleaseError, match="older than the feed's build 11"):
        release.guard(9, signed["out"], floor=11)  # a stale file can't lower the floor either


def test_guard_refuses_a_lower_stable_version_on_the_same_build(signed):
    # the signed feed is stable 1.0.0 at build 7: a v0.9.9 tag on the same commit would move the version back
    with pytest.raises(release.ReleaseError, match="0.9.9 is lower than the feed's 1.0.0"):
        release.guard(7, signed["out"], ver="0.9.9")
    assert release.guard(7, signed["out"], ver="1.0.0")["ok"] and release.guard(7, signed["out"], ver="1.0.1")["ok"]
    assert release.guard(8, signed["out"], ver="0.9.9")["ok"]  # a newer build wins whatever its version
    assert release.guard(7, signed["out"], ver="main-7-abc1234")["ok"]  # main versions don't compare


def test_guard_compares_a_version_only_with_that_builds_own(signed):
    # no file (a failed upload deleted it): the title's build and version still refuse a lower tag on that build
    with pytest.raises(release.ReleaseError, match="0.9.9 is lower than the feed's 1.0.0"):
        release.guard(9, None, floor=9, ver="0.9.9", floor_ver="1.0.0")
    assert release.guard(9, None, floor=9, ver="1.0.0", floor_ver="1.0.0")["ok"]  # the re-run that repairs it
    # a stale file (build 7, 1.0.0) under a newer floor (build 9, 0.5.0): build 9's version is the title's, not the file's
    assert release.guard(9, signed["out"], floor=9, ver="0.5.0", floor_ver="0.5.0")["current"] == 9
    with pytest.raises(release.ReleaseError, match="0.4.0 is lower than the feed's 0.5.0"):
        release.guard(9, signed["out"], floor=9, ver="0.4.0", floor_ver="0.5.0")
    # a title with no version: the file's version counts only when the file is that same build
    assert release.guard(9, signed["out"], floor=9, ver="0.9.9")["ok"]
    with pytest.raises(release.ReleaseError, match="lower"):
        release.guard(7, signed["out"], floor=7, ver="0.9.9")


def test_verify_can_insist_on_the_build(signed, capsys):
    capsys.readouterr()
    base = ["verify", str(signed["out"]), "--pub", signed["pub"].hex()]
    assert release.main([*base, "--build", "7"]) == 0
    capsys.readouterr()
    assert release.main([*base, "--build", "8"]) == 1 and "not 8" in json.loads(capsys.readouterr().out)["error"]


def test_backend_holds_tracked_backend_files_only_and_is_reproducible(repo, tmp_path):
    a = release.backend("main-1-abc1234", tmp_path / "a", root=repo)
    b = release.backend("main-1-abc1234", tmp_path / "b", root=repo)
    assert Path(a["path"]).read_bytes() == Path(b["path"]).read_bytes() and a["sha256"] == b["sha256"]
    with tarfile.open(a["path"]) as tf:
        members = {m.name: m for m in tf.getmembers()}
        assert sorted(members) == sorted(f"greenlight-main-1-abc1234/{f}" for f in
                                         ("LICENSE", "SKILL.md", "VERSION", "scripts/diag.py", "scripts/greenlight.py", "scripts/update.py"))
        top = "greenlight-main-1-abc1234/"
        assert members[top + "scripts/greenlight.py"].mode == 0o755 and members[top + "SKILL.md"].mode == 0o644
        assert {m.uid for m in members.values()} == {0} and tf.extractfile(top + "VERSION").read() == b"main-1-abc1234\n"
    with pytest.raises(release.ReleaseError):
        release.backend("../evil", tmp_path / "c", root=repo)


def test_this_repos_backend_installs_with_update_py(gl_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GREENLIGHT_SKILL", str(tmp_path / "home" / "skill"))
    tgz = release.backend("main-9-feedbee", tmp_path / "out")
    assert update.install(Path(tgz["path"])) == {"ok": True, "version": "main-9-feedbee", "installed": "new"}
    root = update.versions_dir() / "main-9-feedbee"
    assert (root / "scripts" / "update.py").is_file() and not (root / "tests").exists() and not (root / "app").exists()


def test_notes_list_a_stable_tags_commits_or_a_main_builds_commit(repo):
    sh(repo, "tag", "v0.1.0")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "Fix the thing (#7)")
    sh(repo, "commit", "-q", "--allow-empty", "-m", "Add the other (#8)")
    sh(repo, "tag", "v0.2.0")
    assert release.notes("stable", "v0.2.0", root=repo) == "Changes since v0.1.0:\n\n- Add the other (#8)\n- Fix the thing (#7)\n"
    assert release.notes("stable", "v0.1.0", root=repo) == "The first stable release.\n\n- first (#1)\n"  # not "since v0.2.0"
    sh(repo, "commit", "-q", "--allow-empty", "-m", "After the tag (#9)")
    assert "After the tag" not in release.notes("stable", "v0.2.0", root=repo)  # the tag's commits, not HEAD's
    with pytest.raises(release.ReleaseError):
        release.notes("stable", "main", root=repo)
    commit = sh(repo, "rev-parse", "HEAD")
    assert release.notes("main", repo="o/r", root=repo) == f"A build of main at {commit[:7]}: After the tag (#9)\n\nCommit: https://github.com/o/r/commit/{commit}\n"


@pytest.fixture()
def signed(tmp_path, monkeypatch):
    sk = bytes(range(32))
    monkeypatch.setenv(release.KEY_ENV, sk.hex())
    app, tgz = tmp_path / "Greenlight-1.0.0.zip", tmp_path / "greenlight-backend-1.0.0.tar.gz"
    app.write_bytes(b"app bytes")
    tgz.write_bytes(b"backend bytes")
    out = tmp_path / "channel-stable.json"
    args = ["feed", "--channel", "stable", "--version", "1.0.0", "--build", "7", "--commit", "c0ffee", "--app", str(app),
            "--backend", str(tgz), "--base-url", "https://github.com/o/r/releases/download/v1.0.0/", "--signing", "developer_id",
            "--notarized", "--out", str(out)]
    assert release.main(args) == 0
    return {"pub": ed25519.public_key(sk), "out": out, "app": app, "tgz": tgz, "args": args}


def test_feed_signs_its_exact_bytes_and_lists_each_file(signed):
    f = json.loads(signed["out"].read_text())
    body = json.loads(base64.b64decode(f["body"]))
    assert f["key"] == release.key_id(signed["pub"])
    assert (body["channel"], body["version"], body["build"], body["signing"], body["notarized"]) == ("stable", "1.0.0", 7, "developer_id", True)
    assert (body["schema"], body["compat"], body["protocol"]) == (update.SCHEMA, list(update.COMPAT), update.PROTOCOL)
    assert body["app"] == {"url": "https://github.com/o/r/releases/download/v1.0.0/Greenlight-1.0.0.zip", **release.asset(signed["app"])}
    assert body["backend"]["url"].endswith("/greenlight-backend-1.0.0.tar.gz")
    ok = release.verify(signed["out"], signed["pub"], {"app": signed["app"], "backend": signed["tgz"]})
    assert ok["ok"] and ok["checked"] == ["app", "backend"]


def test_verify_refuses_a_tampered_feed_file_or_key(signed):
    with pytest.raises(release.ReleaseError, match="bad signature"):
        release.verify(signed["out"], ed25519.public_key(bytes(32)), {})
    f = json.loads(signed["out"].read_text())
    body = json.loads(base64.b64decode(f["body"]))
    body["app"]["url"] = "https://evil.example/x.zip"
    signed["out"].write_text(json.dumps({**f, "body": base64.b64encode(json.dumps(body).encode()).decode()}))
    with pytest.raises(release.ReleaseError, match="bad signature"):
        release.verify(signed["out"], signed["pub"], {})
    signed["out"].write_text(json.dumps(f))
    signed["app"].write_bytes(b"app bytes, changed")
    with pytest.raises(release.ReleaseError, match="app doesn't match"):
        release.verify(signed["out"], signed["pub"], {"app": signed["app"]})
    signed["out"].write_text("{not a feed")
    with pytest.raises(release.ReleaseError, match="not a feed"):
        release.verify(signed["out"], signed["pub"], {})
    with pytest.raises(release.ReleaseError, match="no public key"):
        release.verify(signed["out"], None, {})


def test_feed_needs_a_key_https_and_a_known_channel(signed, monkeypatch, capsys):
    capsys.readouterr()
    args = signed["args"]
    i = args.index("--channel")
    assert release.main([*args[:i], "--channel", "beta", *args[i + 2:]]) == 1
    assert "unknown channel" in json.loads(capsys.readouterr().out)["error"]
    monkeypatch.setenv(release.KEY_ENV, "abc")
    assert release.main(args) == 1 and "keygen" in json.loads(capsys.readouterr().out)["error"]
    monkeypatch.setenv(release.KEY_ENV, bytes(range(32)).hex())
    i = args.index("--base-url")
    assert release.main([*args[:i], "--base-url", "http://x/", *args[i + 2:]]) == 1
    assert "https" in json.loads(capsys.readouterr().out)["error"]


def test_keygen_and_pubkey_agree(monkeypatch, capsys):
    assert release.main(["keygen"]) == 0
    k = json.loads(capsys.readouterr().out)
    assert len(bytes.fromhex(k["private"])) == 32 and k["key"] == release.key_id(bytes.fromhex(k["public"]))
    monkeypatch.setenv(release.KEY_ENV, k["private"])
    assert release.main(["pubkey"]) == 0
    assert json.loads(capsys.readouterr().out)["public"] == k["public"]


@pytest.mark.parametrize("args,why", [([], "usage"), (["../x", "1.0", "1", "b", "o"], "bad version"), (["v1", "1.0", "x1", "b", "o"], "bad build"),
                                      (["v1", "1.0b", "1", "b", "o"], "bad short"), (["v1", "1.0", "1", "/nonexistent.tgz", "o"], "no backend")])
def test_release_sh_refuses_bad_arguments_before_building(args, why):
    p = subprocess.run(["bash", str(ROOT / "app" / "release.sh"), *args], capture_output=True, text=True)
    assert p.returncode == 2 and why in p.stderr


def test_release_and_dev_builds_share_the_bundle_and_agree_on_macos():
    build, rel = ((ROOT / "app" / f).read_text() for f in ("build.sh", "release.sh"))
    for script in (build, rel):
        assert ". ./bundle.sh" in script and "gl_plist " in script and "gl_resources " in script
    assert re.search(r"^min_macos=(\S+)", rel, re.M)[1] == release.MIN_MACOS
    assert "--options runtime" in rel and "com.apple.security.device.audio-input" in rel


def test_release_workflow_runs_only_here_and_keeps_expressions_out_of_scripts():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "if: github.repository == 'cbarraford/greenlight'" in wf and "vars.RELEASE_MAIN == 'true'" in wf
    assert "pull_request" not in wf  # a fork's PR never reaches the signing secrets
    in_run = False
    for line in wf.splitlines():
        indent = len(line) - len(line.lstrip())
        if re.match(r"\s*run: \|", line):
            in_run, run_indent = True, indent
            continue
        if in_run and line.strip() and indent <= run_indent:
            in_run = False
        assert not (in_run and "${{" in line), f"an expression inside a run script (use env:): {line.strip()}"


def _step(wf: str, name: str) -> str:
    """One step's text, from its `- name:` line to the next step."""
    start = wf.index(f"- name: {name}")
    nxt = wf.find("\n      - ", start + 1)
    return wf[start:nxt if nxt > 0 else len(wf)]


def test_release_workflow_guards_the_key_the_feed_and_reruns():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    # the signing key reaches only the step that signs; the rest only learn whether it is set
    assert wf.count("secrets.GREENLIGHT_FEED_KEY }}") == 1 and "GREENLIGHT_FEED_KEY: ${{" in _step(wf, "Notes and the signed feed")
    # no feed-key.pub, no build: never a check against the secret's own public half
    assert "app/feed-key.pub" in _step(wf, "Check the tools") and "pubkey" not in wf
    assert '--pub "$(cat app/feed-key.pub)"' in _step(wf, "Notes and the signed feed")
    # an older build than the channel's feed stops before anything is built
    order = [wf.index(f"- name: {n}") for n in ("Version", "Only forward", "Backend tarball", "Publish this build")]
    guard = _step(wf, "Only forward")
    assert order == sorted(order)
    assert 'python3 scripts/release.py guard --build "$BUILD" --version "$VER" "${cur[@]}" "${floor[@]}"' in guard
    assert 'floor=(--floor "$floor_build" --floor-version "$floor_ver")' in guard
    # the floor: the feed release's title records the build and version before the upload, which deletes the old file first
    feed = _step(wf, "Point the channel's feed at it")
    title = re.search(r'title="([^"]+)"', feed)[1]
    assert feed.index("--title") < feed.index("gh release upload")
    # the guard's own sed patterns read back what the feed step writes
    rendered = title.replace("$CHANNEL", "stable").replace("$BUILD", "412").replace("$VER", "1.3.0")
    got = [subprocess.run(["sed", "-n", pat], input=f"{rendered}\nchannel-stable.json\n", capture_output=True, text=True,
                          check=True).stdout.strip() for pat in re.findall(r"sed -n '([^']+)'", guard)]
    assert got == ["412", "1.3.0"]
    # a draft is deleted only when it holds nothing but this build's own files; a repair needs all three of them
    assert 'grep -qvxF -e "${ours[0]}" -e "${ours[1]}" -e "${ours[2]}"' in guard
    assert guard.index('[ -z "$assets" ]') < guard.index("an empty draft release") < guard.index('gh release delete "$TAG"')
    assert "publish or delete" not in guard  # publishing a draft without this build's files only fails the next run
    drafts = [ln for ln in guard.splitlines() if "draft release" in ln and "::error::" in ln]
    assert len(drafts) == 2 and all("save what you need from it, delete it on GitHub" in ln for ln in drafts)
    assert guard.index("grep -qvxF") < guard.index('gh release delete "$TAG"')
    assert '"$(grep -cxF -e "${ours[0]}" -e "${ours[1]}" -e "${ours[2]}" <<<"$assets")" = 3' in guard
    # a published build is never rebuilt or re-uploaded (the live feed may point at its files): it only re-points the feed;
    # a draft left by a stopped run is deleted and built again
    assert "isDraft" in guard and 'gh release delete "$TAG"' in guard and "--cleanup-tag" not in guard
    assert 'verify "$out/channel-$CHANNEL.json" --pub "$(cat app/feed-key.pub)" --build "$BUILD"' in guard
    for name in ("Backend tarball", "Signing identity", "Build, sign and zip", "Notes and the signed feed", "Publish this build"):
        assert "steps.g.outputs.repair != 'true'" in _step(wf, name), name
    pub = _step(wf, "Publish this build")
    assert "--clobber" not in pub and "gh release edit" not in pub  # nor its hand-edited notes overwritten
    # the feed's delete-then-upload is retried, and a final failure fails the job
    assert "for try in 1 2 3" in feed and "exit 1" in feed
    # a re-run (an old commit) never cancels the main build that's running
    assert "github.run_attempt == '1'" in wf
    assert "never cancelled, one tag at a time" not in wf  # GitHub replaces a pending run in the group
