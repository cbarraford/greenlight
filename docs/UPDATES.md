# Over-the-air updates: design

Status: draft spec, nothing built yet.

## Goals (decided)

1. **Anyone can download Greenlight and keep it current**: no clone and no compiler on the Mac.
2. **The user is asked first**: Greenlight never restarts itself without a yes.
3. **Two channels**: `stable` (tagged releases) and `main` (every merge to main).
4. **Chief turns survive the restart**: a turn that is running finishes, and its reply shows after relaunch.
5. **Terminals come back with their history**: the scrollback too, not just tmux's current screen.

A restart must leave everything as it was: windows, floors, terminals, chats, automations and workspace agents.

## One release ships both halves

Today the Mac app and the backend (`scripts/`, `SKILL.md`, `lifecycles/`, `prompts/`) are installed separately (`build.sh`, and
`git clone` plus `sync.sh`). They talk over ssh, so automatic updates would let them drift apart. Instead:

- **`Greenlight.app` carries its backend** in `Contents/Resources/backend/`, at the app's own version.
- **The app installs that backend** where Greenlight runs: locally, or over ssh on `GREENLIGHT_HOST`. That replaces `sync.sh` for
  people who download the app.
- **A checkout with `.git` is "managed by git"**: the app never overwrites it. It only tells you when the checkout is behind its
  own version. Developers keep working as they do now.
- **The two halves check each other's version.** `diag.py version` returns `{version, schema, protocol}`. An app that finds a
  backend with a different `protocol` installs its own copy (after asking) before it sends any chat or dashboard command.

## Building and signing (CI)

- **A GitHub Actions job on `macos-latest`** runs `app/build.sh` in a release mode that also copies the backend in. It signs with a
  **Developer ID Application** cert, **notarizes** (`notarytool`), staples, zips, and signs the zip with an **Ed25519** key.
  CryptoKit's `Curve25519` verifies it, so the app needs no extra dependency.
- **Why Developer ID**: the signing identity stays the same from release to release. macOS keeps the mic, speech and notification
  permissions, and the Keychain stops asking for access after every update (the `cdhash:` pinning issue from #54).
- **Channels:**
  - `stable`: a tag `vX.Y.Z` makes a GitHub Release.
  - `main`: every merge to main makes a prerelease `main-<n>-<sha>`, where `<n>` is the commit count, so builds sort in order.
- **Feed**: each channel gets a `channel-<name>.json` release asset:
  `{version, build, schema, protocol, url, sha256, ed25519, notes_md, min_macos}`.
  The feed file is signed with the same key, so a tampered feed fails like a tampered zip.
- **Secrets**: the Developer ID cert, its notary credentials and the Ed25519 private key are CI secrets. Fork CI (#122) never sees
  them: forks build unsigned and publish nothing.

## Checking and asking

- **The app checks the feed** at launch and every 6 h. The request is an anonymous GET to GitHub with no identifiers in the URL or
  headers. Settings › Updates can turn it off (and has the channel and a "Check now" button).
- **A newer build** is downloaded in the background and checked (sha256, Ed25519, then `SecStaticCodeCheckValidity` against
  Greenlight's Team ID requirement). Then a quiet prompt:
  **"Greenlight 0.5.0 is ready. Everything comes back where it is."** with **Restart now**, **When idle**, **Later** and the release notes.
  - **When idle**: wait until nothing below holds the restart back, then restart without asking again. The user already said yes.
  - **Skip this version**: the next build asks again.
- **What holds a restart back**: an open voice session, a backend migration the new version can't undo (see State), or keystrokes
  in the last 10 s. Chief turns and running automations don't hold it back, because they survive it.
- **Changing channel**: `main` → `stable` moves only forward. It waits for a stable release newer than the installed main build,
  unless the user picks "Install stable now". That is a downgrade, and it is allowed only when the state schema matches.

## The restart

1. **Save the session**:
   - the existing `terminals*.json` (written now, not on the page's next save);
   - a new `resume.json`: which floor, each window's frame and screen, the open hex card, the chat drafts and scroll positions,
     and the running Chief turn ids per window (`chat`, `chat-<n>`).
2. **Update the backend first** (Backend swap below), on the host where it runs. If that fails, stop: nothing was swapped.
3. **Leave the app's helpers running.** The app writes `restart.planned` so the next launch doesn't log `prev_crash`, then
   **detaches**: `chat.py` turns keep running (Chief turns). Tmux sessions are already separate from the app. The helpers that are
   safe to restart (`dash.py stream`, outbox, trees, port tunnels, Kokoro) are terminated as they are today.
4. **A helper** (`Contents/Resources/update.sh`, copied to a temp dir first) waits for the app's pid to exit. It moves the old
   bundle to `~/Library/Application Support/Greenlight/previous/` and moves the new one into place (a rename on the same volume,
   as `build.sh` does now). Then it runs `open` on the app.
5. **The new app reads `resume.json`**: it restores the windows, reattaches the terminals with their history, starts the helpers
   before the first frame (so the dashboard doesn't flash empty), and picks up the running Chief turns.

## Chief turns survive the restart (new)

Today the app runs `ssh host chat.py send` and reads its stdout. If the app quits, ssh hangs up, and `chat.py`'s SIGHUP handler
kills the turn. Instead:

- **`chat.py send` starts the turn as a separate process.** A runner (`start_new_session`, stdin from `/dev/null` once the message
  is passed, SIGHUP ignored) writes each event as a line to `chat/turns/<id>.jsonl`, then a final `{"kind":"end"}`. The old pid file
  and birth check stay as they are.
- **The app follows the turn**: `chat.py follow <id> --from <offset>` streams lines from that file and exits at `end`. Today's
  `send` becomes "start, then follow", so the app's event handling doesn't change.
- **After a restart**, the app runs `follow` for each turn id in `resume.json`, from the offset it had already shown. A turn that
  ended during the restart shows its reply as soon as the app is back.
- **Old turn files** are deleted after 24 h. They hold message text, so they live under `chat/` (private, like the transcript) and
  never go into a bundle.
- `stop` and `new` still work, through the pid file.

## Terminal history (new)

- **Before reattaching** a pane, the app runs `tmux capture-pane -p -e -J -S - -E -1 -t =<session>` (the history above the
  screen, with colors), writes it into ghostty-web's scrollback, and then attaches. Tmux redraws the screen itself, so nothing
  shows twice.
- **This helps any reattach**, not only after an update: a relaunch or a reloaded page also brings the scrollback back. It replaces
  the "current screen only" ponytail at `App.swift:1793`.
- **Tmux's `history-limit` of 50000 lines** caps how much comes back. Fetching it is one ssh round trip per pane, done in parallel.

## Backend swap (local and remote)

- **Layout:**

  ```
  ~/.greenlight/code/versions/<version>/   (one folder per version)
  ~/.greenlight/code/current               (symlink to the active version)
  ~/.claude/skills/greenlight              (symlink to current, for a downloaded install)
  ```

- **Switching**: after the `orchestrate.py` tick lock and `ws_state.lock()` are taken, the app swaps `current` with
  `ln -sfn` plus `rename(2)`, so the switch is atomic.
  - Processes that were already running keep their own folder. **Every script must resolve its own path with `realpath`**, and
    most already use `Path(__file__).resolve()`. `chat.py` uses `abspath` (`SCRIPTS`, and the `SKILL.md` path in the prompt), so a
    turn running across a switch would start the new version's scripts partway through. Fix: use `realpath`.
  - **Bug this exposes**: `auto.py install` writes `Path(__file__).resolve()` into the systemd/launchd unit, which would pin the
    scheduler to one version folder. The unit must point at `current/scripts/auto.py`, not the resolved path. `auto.py install` is
    rerun on every swap so an old unit is corrected.
- **Smoke test**: the new version must pass `diag.py doctor --quick` within 30 s of the switch, or `current` goes back to the
  previous version. Two versions are kept on disk.

## State and schema

- **`~/.greenlight/schema.json`** holds `{"schema": N}`. Each release declares the `schema` it needs and a `compat` range of
  schemas it can read.
- **Additive changes are the default**: new keys, old keys left in place. Most releases don't change `schema`.
- **A real migration**: back up the files it touches to `~/.greenlight/backup/<from>-<to>/`, run it forward only, and record it.
  The prompt says "this update upgrades your saved data". Rolling back restores the backup.

## Rollback

- **Crash check**: if the new app crashes within 60 s of starting, `restart.planned` is still there and `prev_crash` fires. If that
  happens twice, the next launch (launchd's `KeepAlive` on the helper, or the user opening the app) swaps `previous/` back in,
  switches the backend back, and marks the version as skipped. A notice then says what happened.
- **Settings › Updates › Go back to <previous>** runs the same swap on purpose.

## Diagnostics (enum tokens only, per AGENTS.md)

Events `update_check`, `update_download`, `update_apply` and `rollback`, with `channel` (`stable`/`main`), `outcome`
(`ok`, `bad_sig`, `bad_team`, `net`, `smoke_failed`, `crash_loop`, `declined`) and `secs`. The version numbers are already in
`v`. Never URLs, paths or host names. The new keys go in `diag.KEYS`.

## Work, in order

1. **Backend**:
   - use `realpath` everywhere, and point the `auto.py` unit at `current`;
   - the versioned install layout, a locked `swap` subcommand, and `doctor --quick`;
   - `diag.py version`.
   - Testable on Linux.
2. **Chief turns survive**: the separate turn runner, `chat.py follow`, and the app following turns and picking them up again.
3. **Terminal history**: capture-pane on reattach.
4. **Restart path**: `resume.json`, `restart.planned`, `update.sh`, crash-check rollback. Built and tested on the Mac mini.
5. **CI release pipeline**: Developer ID signing, notarization, the Ed25519-signed feed, `stable` and `main` channels.
6. **Updater UI**: Settings › Updates, the prompt, "when idle", the version handshake with a remote backend.

Steps 1–3 help even without an updater (a clean relaunch, a page reload), so they can merge first.

## Needs the owner

- **An Apple Developer Program membership** ($99/yr) for the Developer ID cert and notarization. Without one, step 5 can ship only
  ad-hoc builds, and macOS warns on first open and re-asks for permissions after every update.
- **An Ed25519 signing key pair** made once, the private half stored as a CI secret; the public half is compiled into the app.
