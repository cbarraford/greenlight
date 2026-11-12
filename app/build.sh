#!/usr/bin/env bash
# Build and install ~/Applications/Greenlight.app. Notifications need a real (ad-hoc signed) bundle.
set -euo pipefail
cd "$(dirname "$0")"
app=~/Applications/Greenlight.app
mkdir -p "$app/Contents/MacOS"
# Command Line Tools swiftc: avoids needing the Xcode license accepted
DEVELOPER_DIR=/Library/Developer/CommandLineTools swiftc -O Greenlight.swift -o "$app/Contents/MacOS/Greenlight"
cat > "$app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>dev.greenlight.app</string>
  <key>CFBundleName</key><string>Greenlight</string>
  <key>CFBundleExecutable</key><string>Greenlight</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>Orb</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSMicrophoneUsageDescription</key><string>Speak to Greenlight: your voice is transcribed into the message box.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Transcribes what you say to Greenlight, on this Mac.</string>
</dict></plist>
EOF
mkdir -p "$app/Contents/Resources" && cp dashboard.html terminal.html ../brand/icons/{Orb,MergeLight,ThreeFloors}.icns "$app/Contents/Resources/" && cp vendor/ghostty-web.js "$app/Contents/Resources/"  # the left pane, the Dock icons (../brand/icons/*.svg → rsvg-convert → iconutil; Orb is the bundle's)
codesign -fs - "$app"
# re-register so Finder/Dock pick up icon changes instead of a cached blank one
touch "$app"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
echo "$app"
