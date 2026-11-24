# Releasing Greenlight

Releases come out on two update channels (see `docs/UPDATES.md`), both built by `.github/workflows/release.yml`:

| channel | when | version | GitHub release |
|---|---|---|---|
| `stable` | you push a `vX.Y.Z` tag on a commit that's on `main` | `X.Y.Z` | `vX.Y.Z` (the repo's latest release) |
| `main` | every push to `main`, once `RELEASE_MAIN` is on | `main-<commits>-<sha>` | a prerelease of that name; the 20 newest are kept |

The build number (the app's `CFBundleVersion`) is the commit count, so it orders builds across both channels. A main build's
short version is the highest stable tag behind it (`0.0.0` before the first). A tag is `vX.Y.Z` with no leading zeros
(`v1.02.3` doesn't build), so no two tags give the same version.

A channel's feed only moves forward: a build older than the one the feed already offers (an old tag pushed late, an old run
re-run) stops before it builds anything, and so does a lower stable tag on the same commit. The feed release's title records
the build and version it holds (`Greenlight stable channel (build 412: 1.3.0)`), so both rules hold even while a failed
upload has left it with no file.

Re-running a build that's already published rebuilds nothing and leaves its files alone (the live feed may point at them): it
checks that build's own signed feed and points the channel at it again (a release missing any of its three files stops the
run: delete it on GitHub to build it fresh). A draft holding only this build's files, left by a run that stopped partway, is
deleted and built again; any other draft for the tag (say, one you started by hand) stops the run and is left alone.

For main, use *Run workflow* (main's current commit: built if it has no release yet, else re-pointed) rather than re-running
an old run: a re-run never cancels the build that's running, but while waiting it can still replace a newer run that's
pending.

Each release holds three files:
- `Greenlight-<version>.zip`: the app. It's universal (Apple silicon and Intel), runs on macOS 14 or later, and carries the
  backend inside.
- `greenlight-backend-<version>.tar.gz`: the backend, the same for every machine. It's read from the commit (uncommitted edits
  never get in), so the same commit gives the same tar anywhere. The gzip bytes match too where zlib is the same build, as on
  the release runner; another machine's zlib can compress the same tar differently.
- `channel-<name>.json`: the signed feed for this build.

The rolling prereleases `feed-stable` and `feed-main` hold each channel's current feed. That's what the app checks:

```
https://github.com/cbarraford/greenlight/releases/download/feed-stable/channel-stable.json
https://github.com/cbarraford/greenlight/releases/download/feed-main/channel-main.json
```

## One-time setup (the repo owner)

Nothing is published until the feed key is set. A stable release also needs the Apple signing and notarization secrets; a
main build without them is signed ad hoc, and its feed says so (`"signing": "adhoc"`).

1. **The feed key.** It signs every feed; the app trusts only its public half.

   ```bash
   (umask 077; python3 scripts/release.py keygen > /tmp/feed-key.json)
   python3 -c 'import json; print(json.load(open("/tmp/feed-key.json"))["private"])' | gh secret set GREENLIGHT_FEED_KEY
   python3 -c 'import json; print(json.load(open("/tmp/feed-key.json"))["public"])' > app/feed-key.pub
   rm /tmp/feed-key.json   # keep no other copy of the private half
   ```

   Commit `app/feed-key.pub`. Each build checks its feed against that file before it publishes anything, and a build
   without the file stops at its first step.
2. **Apple Developer ID** (needs the Apple Developer Program, $99/yr).
   - Make a **Developer ID Application** certificate.
   - Export it from Keychain Access as a `.p12` **with its chain**: select the certificate and Apple's *Developer ID
     Certification Authority* intermediate. The build only accepts an identity that validates.
   - Then set the secrets:

     ```bash
     base64 -i DeveloperID.p12 | gh secret set DEVELOPER_ID_P12
     gh secret set DEVELOPER_ID_P12_PASSWORD
     ```
3. **Notarization.** In App Store Connect, make an API key (Users and Access › Integrations, role *Developer*). Then:

   ```bash
   gh secret set NOTARY_KEY_P8 < AuthKey_XXXXXXXXXX.p8
   gh secret set NOTARY_KEY_ID      # the key's ID
   gh secret set NOTARY_ISSUER      # the issuer ID on that page
   ```
4. **Turn on main builds.** Run `gh variable set RELEASE_MAIN --body true`. Until then, pushes to main build nothing; stable tags
   build either way.

The release job runs on the self-hosted macOS runner. It needs the Command Line Tools (`swiftc`, `lipo`, `codesign`, `xcrun
notarytool`, `stapler`), `python3` 3.10 or later, `gh` and `openssl` on its `PATH`. Its first step checks for them. The
signing identity goes into a throwaway keychain that the job deletes when it ends, even if it fails. The feed key reaches only
the step that signs the feed.

**The runner is the trust boundary.** That runner also builds pull requests from this repo's own branches (`tests.yml`) and the
nightly coverage, so anything that can push a branch here could leave something on it (a `PATH` shim, a shell rc) that the
next release runs with the secrets. Fork PRs never reach it. Only people and agents you'd trust with the secrets should be able
to push branches; for more than that, give releases their own runner (a separate label or machine), and put the secrets in a
GitHub Environment limited to `main` and `v*` tags.

## Cutting a stable release

Merge everything for it, then tag the merged commit:

```bash
git fetch origin && git tag v0.1.0 origin/main && git push origin v0.1.0
```

Push one tag at a time, and the next once the first is running: GitHub keeps only one pending run per channel, so a third
tag pushed while one builds and one waits replaces the waiting one (re-run it from the Actions tab once the others finish, if
it's still the newest). The notes are the commit subjects since the previous stable tag. To fix them after the release,
edit them on GitHub (a re-run leaves them alone); the feed keeps the notes it was built with.

## Building locally (a dry run, nothing published)

```bash
python3 scripts/release.py version --channel main
python3 scripts/release.py backend --version main-1-abcdef0 --out /tmp/rel
app/release.sh main-1-abcdef0 0.0.0 1 /tmp/rel/greenlight-backend-main-1-abcdef0.tar.gz /tmp/rel   # on a Mac; ad hoc
(umask 077; python3 scripts/release.py keygen > /tmp/k.json)   # a throwaway key
GREENLIGHT_FEED_KEY=$(python3 -c 'import json; print(json.load(open("/tmp/k.json"))["private"])') \
  python3 scripts/release.py feed --channel main --version main-1-abcdef0 --build 1 --commit abcdef0 \
  --app /tmp/rel/Greenlight-main-1-abcdef0.zip --backend /tmp/rel/greenlight-backend-main-1-abcdef0.tar.gz \
  --base-url https://example.invalid --signing adhoc --out /tmp/rel/channel-main.json
python3 scripts/release.py verify /tmp/rel/channel-main.json --pub "$(python3 -c 'import json; print(json.load(open("/tmp/k.json"))["public"])')"
```

## The feed

`channel-<name>.json` is `{"body": base64(JSON), "sig": base64(Ed25519 signature), "key": key id}`. The signature covers the
exact bytes of `body`, so the app checks those bytes before it parses them. The body holds:
- `format`, `channel`, `version`, `build`, `commit`, `published`;
- `schema`, `compat` and `protocol` (from `scripts/update.py`);
- `min_macos`, `signing` (`developer_id` or `adhoc`), `notarized` and `notes`;
- `app` and `backend`, each `{url, sha256, size}`.

Because the sha256s are inside the signed body, one signature covers the files too.

Replacing a channel's feed deletes the old `channel-<name>.json` before uploading the new one (GitHub has no atomic replace),
so for a moment, or until a re-run if every upload try fails, the URL can 404. The app treats a missing or unreachable feed
as "no update".

`scripts/ed25519.py` signs in CI: stdlib Python, RFC 8032's reference algorithm, checked against the RFC's test vectors. The app
will verify with CryptoKit.

**Rotating the key:** ship an app that trusts both the old and the new public key, wait until people have it, then switch the
secret and `app/feed-key.pub`. An app that only knows the old key ignores feeds signed with the new one.
