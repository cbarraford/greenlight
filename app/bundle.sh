# shellcheck shell=bash
# What every Greenlight.app holds, for app/build.sh (your own build) and app/release.sh (a release): sourced, run from app/.

# gl_plist <Info.plist> <bundle id> <name> <short version> <build> [dev pages dir] [minimum macOS]
gl_plist() {
  python3 - "$@" <<'PY'
import plistlib, sys
out, bid, name, short, build = sys.argv[1:6]
dev = sys.argv[6] if len(sys.argv) > 6 else ""
min_os = sys.argv[7] if len(sys.argv) > 7 else ""
pl = {
    "CFBundleIdentifier": bid,
    "CFBundleName": name,
    "CFBundleExecutable": name,
    "CFBundlePackageType": "APPL",
    "CFBundleIconFile": "GreenlightDebug" if dev else "Greenlight",  # a dev copy wears the grey-light Debug icon
    "CFBundleShortVersionString": short,
    "CFBundleVersion": build,
    "NSMicrophoneUsageDescription": "Speak to Greenlight: your voice is transcribed into the message box.",
    "NSSpeechRecognitionUsageDescription": "Transcribes what you say to Greenlight, on this Mac.",
}
if dev:
    pl["GreenlightDev"] = dev
if min_os:
    pl["LSMinimumSystemVersion"] = min_os
with open(out, "wb") as f:
    plistlib.dump(pl, f)
PY
}

# gl_resources <app bundle>: the pages, the app icon and its Debug variant (scripts/build_icons.sh builds them from
# brand/icon/layers), and the mark: the menu bar's template (lit while agents work) and the orb's glyph (blades + light)
gl_resources() {
  mkdir -p "$1/Contents/Resources" &&
    cp dashboard.html terminal.html stage.html kokoro.py ../brand/icons/{Greenlight,GreenlightDebug}.icns "$1/Contents/Resources/" &&
    cp vendor/ghostty-web.js "$1/Contents/Resources/" || return
  local m
  for m in mark:GreenlightMark mark-idle:GreenlightMarkIdle mark-light:GreenlightMarkLight; do
    cp "../brand/icon/${m%%:*}.svg" "$1/Contents/Resources/${m#*:}.svg" || return
  done
}
