#!/usr/bin/env bash
# Build and install ~/Applications/Greenlight.app. Notifications need a real (ad-hoc signed) bundle.
# GREENLIGHT_APP=greenlight-<workspace> builds a copy under that name with its own bundle id (so its own settings), and
# GREENLIGHT_DEV=<dir> makes it a dev copy (scripts/dev.py): unoptimized, as that halves the build, and told where its live pages are.
set -euo pipefail
cd "$(dirname "$0")"
name=${GREENLIGHT_APP:-Greenlight} dev=${GREENLIGHT_DEV:-}
id=dev.greenlight.app
[ "$name" = Greenlight ] || id=$id.$(printf %s "${name#greenlight-}" | tr -c 'A-Za-z0-9-' -)
app=~/Applications/$name.app
# Compiled aside and moved in: a failed build leaves the installed app as it was (and a running one keeps its binary).
bin=$(mktemp -t greenlight-build)
trap 'rm -f "$bin"' EXIT
# Command Line Tools swiftc: avoids needing the Xcode license accepted
DEVELOPER_DIR=/Library/Developer/CommandLineTools swiftc "$([ -n "$dev" ] && echo -Onone || echo -O)" Greenlight.swift -o "$bin"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
mv "$bin" "$app/Contents/MacOS/$name"
cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$id</string>
  <key>CFBundleName</key><string>$name</string>
  <key>CFBundleExecutable</key><string>$name</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>Orb</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Speak to Greenlight: your voice is transcribed into the message box.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Transcribes what you say to Greenlight, on this Mac.</string>
  ${dev:+<key>GreenlightDev</key><string>$dev</string>}
</dict></plist>
EOF
mkdir -p "$app/Contents/Resources" && cp dashboard.html terminal.html stage.html kokoro.py ../brand/icons/{Orb,MergeLight,ThreeFloors}.icns "$app/Contents/Resources/" && cp vendor/ghostty-web.js "$app/Contents/Resources/"  # the left pane, the Dock icons (../brand/icons/*.svg → rsvg-convert → iconutil; Orb is the bundle's)
# Ad-hoc signing changes the app's identity every build, so macOS asks for the mic and speech again.
# A self-signed "Greenlight Dev" code-signing cert (Keychain Access → Certificate Assistant) keeps it stable.
id=$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Greenlight Dev/{print $2; exit}')
codesign -fs "${id:--}" "$app"
# re-register so Finder/Dock pick up icon changes instead of a cached blank one
touch "$app"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
echo "$app"
