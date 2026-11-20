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
# Ad-hoc signing changes the app's identity every build, so macOS asks for the mic, speech and Keychain keys again.
# A self-signed "Greenlight Dev" cert keeps it stable. It lives in its own keychain (password in a 0600 file), because
# codesign can't use a key in the login keychain without your login password (errSecInternalComponent).
d=~/.config/greenlight kc=~/.config/greenlight/signing.keychain-db
if [ ! -f "$kc" ]; then
  (umask 077; mkdir -p "$d"; openssl rand -hex 16 > "$d/signing.pass"); t=$(mktemp -d)
  security create-keychain -p "$(cat "$d/signing.pass")" "$kc" && security set-keychain-settings "$kc"
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$t/k" -out "$t/c" -days 3650 -subj "/CN=Greenlight Dev" -addext basicConstraints=critical,CA:false \
    -addext keyUsage=critical,digitalSignature -addext extendedKeyUsage=critical,codeSigning 2>/dev/null
  openssl pkcs12 -export -legacy -inkey "$t/k" -in "$t/c" -out "$t/p.p12" -passout pass:x 2>/dev/null || openssl pkcs12 -export -inkey "$t/k" -in "$t/c" -out "$t/p.p12" -passout pass:x
  security unlock-keychain -p "$(cat "$d/signing.pass")" "$kc" && security import "$t/p.p12" -k "$kc" -P x -T /usr/bin/codesign >/dev/null; rm -rf "$t"
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$(cat "$d/signing.pass")" "$kc" >/dev/null
fi
security unlock-keychain -p "$(cat "$d/signing.pass")" "$kc"
# codesign only finds identities in keychains on the search list (--keychain alone says "no identity found"): add it once, keeping the rest
kcs=(); while IFS= read -r l; do l=${l#*\"}; kcs+=("${l%\"}"); done < <(security list-keychains -d user)
case " ${kcs[*]} " in *" $kc "*) ;; *) security list-keychains -d user -s "${kcs[@]}" "$kc" ;; esac
id=$(security find-identity -p codesigning "$kc" | awk '/"Greenlight Dev"/{print $2; exit}')
# The Keychain pins a key Greenlight saves to the build that saved it, unless the build has a Team ID: its partition list says
# `cdhash:<this build>` (seen in a login keychain on macOS 26.5), so every rebuild of Greenlight or of a dev copy asked for your
# password again, for each key it read. An Apple Development cert (free with an Apple ID: Xcode → Settings → Accounts → Manage
# Certificates) gives the build one, and the key is pinned to `teamid:` instead, so set one up and every build after it uses it.
# Either way the requirement names the signer, not the bundle id: every workspace's dev copy has its own id (scripts/dev.py), and
# Keychain entries trust apps by this requirement. Code that could sign as that signer could already claim Greenlight's id, so
# this lets in nothing new. (`identifier` takes no wildcard, and the Keychain didn't match a copy on `info[CFBundleIdentifier]`.)
apple=$(security find-identity -v -p codesigning | awk '/"Apple Development: /{print $2; exit}' || true)
team=$([ -n "$apple" ] && codesign -fs "$apple" "$app" 2>/dev/null && codesign -dv "$app" 2>&1 | sed -n 's/^TeamIdentifier=\([A-Z0-9]\{10\}\)$/\1/p' || true)
if [ -n "$team" ] && codesign -fs "$apple" -r="designated => anchor apple generic and certificate leaf[subject.OU] = \"$team\"" "$app"; then :
elif [ -z "$id" ]; then codesign -fs - "$app"
else
  [ -z "$apple" ] || echo "build.sh: signing with your Apple Development cert failed; using Greenlight Dev" >&2
  codesign -fs "$id" --keychain "$kc" -r="designated => certificate leaf = H\"$id\"" "$app" ||
    { echo "build.sh: signing with Greenlight Dev failed; signing ad hoc (macOS will ask for the mic, speech and Keychain again)" >&2; codesign -fs - "$app"; }
fi
# re-register so Finder/Dock pick up icon changes instead of a cached blank one
touch "$app"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
echo "$app"
