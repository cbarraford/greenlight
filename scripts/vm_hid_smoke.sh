#!/usr/bin/env bash
# vm_hid_smoke.sh: OS-level WindowServer HID smoke test intended to run INSIDE a Tier 2 macOS VM
# (via scripts/vm_test.sh --run scripts/vm_hid_smoke.sh). Never run on your host desktop unless
# you explicitly want your clipboard, cursor, and focus driven.
set -euo pipefail

OUT="${GREENLIGHT_VM_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/gl-hid-XXXXXX")}"
mkdir -p "$OUT"
HOME_TMP="$(mktemp -d "${TMPDIR:-/tmp}/gl-hid-home-XXXXXX")"
TMUX_SOCK="gl-hid-$$"
trap 'tmux -L "$TMUX_SOCK" kill-server >/dev/null 2>&1 || true; rm -rf "$HOME_TMP"' EXIT

echo "vm_hid_smoke: building Greenlight inside VM..."
GREENLIGHT_APP="greenlight-vmtest" GREENLIGHT_DEV="$HOME_TMP/dev" app/build.sh >/dev/null
APP_BIN="$HOME/Applications/greenlight-vmtest.app/Contents/MacOS/greenlight-vmtest"

GREENLIGHT_HOST=local GREENLIGHT_HOME="$HOME_TMP" GREENLIGHT_CODE="$(pwd)" GREENLIGHT_TMUX_SOCKET="$TMUX_SOCK" \
  "$APP_BIN" &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null || true; tmux -L "$TMUX_SOCK" kill-server >/dev/null 2>&1 || true; rm -rf "$HOME_TMP"' EXIT
sleep 3

# 1. Focus window & verify ⌘B (Pipe <-> Basement) and ⌘⇧G (Greenlight Center) via System Events
osascript <<'OSA'
tell application "System Events"
  set frontmost of process "greenlight-vmtest" to true
  delay 0.5
  keystroke "b" using {command down}
  delay 0.6
end tell
OSA
screencapture -x "$OUT/vm-basement.png"

osascript <<'OSA'
tell application "System Events"
  keystroke "g" using {command down, shift down}
  delay 0.8
end tell
OSA
screencapture -x "$OUT/vm-center.png"

# 2. Real macOS Pasteboard PNG -> ⌘V screenshot attachment check
python3 - <<'PY'
import base64, subprocess
# 1x1 red PNG
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
p = "/tmp/gl-hid-paste.png"
open(p, "wb").write(png)
subprocess.run(["osascript", "-e", f'set the clipboard to (read (POSIX file "{p}") as «class PNGf»)'], check=True)
PY

osascript <<'OSA'
tell application "System Events"
  set frontmost of process "greenlight-vmtest" to true
  delay 0.3
  keystroke "v" using {command down}
  delay 0.5
end tell
OSA
screencapture -x "$OUT/vm-after-paste.png"

echo "ok  vm_hid_smoke captured vm-basement.png, vm-center.png, vm-after-paste.png in $OUT"
