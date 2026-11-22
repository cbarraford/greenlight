#!/usr/bin/env bash
# Build and install ~/Applications/Greenlight.app. Notifications need a real (ad-hoc signed) bundle.
# GREENLIGHT_APP=greenlight-<workspace> builds a copy under that name with its own bundle id (so its own settings), and
# GREENLIGHT_DEV=<dir> makes it a dev copy (scripts/dev.py): unoptimized, as that halves the build, and told where its live pages are.
set -euo pipefail
cd "$(dirname "$0")"
name=${GREENLIGHT_APP:-Greenlight} dev=${GREENLIGHT_DEV:-}
case "$name" in
  ''|*[!A-Za-z0-9._-]*|.*|-*) echo "build.sh: error: invalid GREENLIGHT_APP: $name" >&2; exit 1 ;;
esac
[ "${#name}" -le 61 ] || { echo "build.sh: error: GREENLIGHT_APP too long" >&2; exit 1; }
id=dev.greenlight.app
[ "$name" = Greenlight ] || id=$id.$(printf %s "${name#greenlight-}" | tr -c 'A-Za-z0-9-' -)
dest=~/Applications/$name.app
# Built and signed aside, then moved in: a failed build (or signing) leaves the installed app as it was, and a running one
# keeps its binary. Staged in a hidden folder in your home, not ~/Applications: on the same volume, so the move is a rename, but
# a build killed mid-way (dev.py's timeout) leaves no second signed copy where LaunchServices looks. Its folder is named for its
# process, and swept once that's gone (not by age: a build can wait on codesign's prompt for as long as you leave it).
for f in ~/.greenlight-stage.*.*; do p=${f#"$HOME"/.greenlight-stage.}; p=${p%%.*}
  [ -d "$f" ] && [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null && rm -rf "$f" || true
done
find ~ -maxdepth 1 -name '.greenlight-stage.??????' -mmin +60 -exec rm -rf {} + 2>/dev/null || true  # this PR's earlier naming, without the pid
mkdir -p ~/Applications; stage=$(mktemp -d ~/.greenlight-stage.$$.XXXXXX) app=$stage/$name.app
bin=$(mktemp -t greenlight-build)
t=""
trap 'rm -rf "$bin" "$stage" ${t:+"$t"}' EXIT
# The signer first: a cert that can't sign fails the build in a second, not after the compile.
# Ad-hoc signing changes the app's identity every build, so macOS asks for the mic and speech again. Without an Apple
# Development cert (below), a self-signed "Greenlight Dev" one keeps those, though not the Keychain keys. It lives in its own
# keychain (password in a 0600 file), because codesign can't use a key in the login keychain over ssh (errSecInternalComponent).
d=~/.config/greenlight kc=~/.config/greenlight/signing.keychain-db
if [ ! -f "$kc" ]; then
  (umask 077; mkdir -p "$d"; openssl rand -hex 16 > "$d/signing.pass"); t=$(umask 077; mktemp -d)
  security create-keychain -p "$(cat "$d/signing.pass")" "$kc" && security set-keychain-settings "$kc"
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$t/k" -out "$t/c" -days 3650 -subj "/CN=Greenlight Dev" -addext basicConstraints=critical,CA:false \
    -addext keyUsage=critical,digitalSignature -addext extendedKeyUsage=critical,codeSigning 2>/dev/null
  openssl pkcs12 -export -legacy -inkey "$t/k" -in "$t/c" -out "$t/p.p12" -passout pass:x 2>/dev/null || openssl pkcs12 -export -inkey "$t/k" -in "$t/c" -out "$t/p.p12" -passout pass:x
  security unlock-keychain -p "$(cat "$d/signing.pass")" "$kc" && security import "$t/p.p12" -k "$kc" -P x -T /usr/bin/codesign >/dev/null; rm -rf "$t"; t=""
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$(cat "$d/signing.pass")" "$kc" >/dev/null
fi
security unlock-keychain -p "$(cat "$d/signing.pass")" "$kc"
# codesign only finds identities in keychains on the search list (--keychain alone says "no identity found"): add it once, keeping the rest
kcs=(); while IFS= read -r l; do l=${l#*\"}; kcs+=("${l%\"}"); done < <(security list-keychains -d user)
case " ${kcs[*]:-} " in *" $kc "*) ;; *) security list-keychains -d user -s ${kcs[@]+"${kcs[@]}"} "$kc" ;; esac
devid=$(security find-identity -p codesigning "$kc" | awk '/"Greenlight Dev"/{print $2; exit}')
# The Keychain pins a key Greenlight saves to the build that saved it, unless the build has a Team ID: its partition list says
# `cdhash:<this build>` (seen in a login keychain on macOS 26.5), so every rebuild of Greenlight or of a dev copy asked for your
# password again, for each key it read. An Apple Development cert (free with an Apple ID: Xcode → Settings → Accounts → Manage
# Certificates) gives the build one, and the key is pinned to `teamid:` instead, so set one up and every build after it uses it.
# Its requirement names the cert, not the bundle id: every workspace's dev copy has its own id (scripts/dev.py), and Keychain
# entries trust apps by this requirement. Any code signed with your cert matches (and could claim Greenlight's id anyway): once
# codesign may use the cert's key without asking, a process running as you can sign with it, as it could with Greenlight Dev.
# (`identifier` takes no wildcard, and the Keychain didn't match a copy on `info[CFBundleIdentifier]`.) Greenlight Dev keeps
# codesign's default: its builds are pinned to `cdhash:` whatever the requirement, and a new one would cost your mic and speech grants.
# Yours by name too, not just the team's: on a shared team, a teammate's builds would match the team alone. A renewed cert
# keeps both. Picked by name, not by the listing's order, so one with two teams doesn't switch between them.
line=$(security find-identity -v -p codesigning | grep '"Apple Development: ' | sort -t'"' -k2 | head -1 || true)
# An expired one (or one that no longer validates) isn't listed with -v: fail, not Greenlight Dev, which your grants don't match
if [ -z "$line" ] && security find-identity -p codesigning | grep '"Apple Development: ' >/dev/null; then
  echo "build.sh: error: your Apple Development cert isn't valid: renew it if it expired (Xcode → Settings → Accounts → Manage Certificates), install Apple's WWDR intermediate if it's missing, or delete the cert to sign with Greenlight Dev again" >&2; exit 1
fi
apple=$(printf %s "$line" | awk '{print $2}') cn=$(printf %s "$line" | cut -d'"' -f2)
# Its team is the cert's OU. Signing asks for the key once ("codesign wants to sign using…": Always Allow), on screen even
# for a dev copy's unattended build, which waits for it.
team=$([ -n "$apple" ] && security find-certificate -a -Z -p | awk -v h="$apple" '/^SHA-1 hash:/{on=($3==h)} on; /END CERTIFICATE/{on=0}' |
  openssl x509 -noout -subject 2>/dev/null | sed -n 's/.*OU *= *\([A-Z0-9]\{10\}\).*/\1/p' || true)
if [ -n "$apple" ] && [ -z "$team" ]; then echo "build.sh: error: couldn't read the team (OU) from \"$cn\"" >&2; exit 1; fi
# Command Line Tools swiftc: avoids needing the Xcode license accepted.
# Production builds compile ONLY Sources/*.swift without -DGREENLIGHT_TESTS so Tests/*.swift never ships in Greenlight.app.
DEVELOPER_DIR=/Library/Developer/CommandLineTools swiftc "$([ -n "$dev" ] && echo -Onone || echo -O)" Sources/*.swift -o "$bin"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
mv "$bin" "$app/Contents/MacOS/$name"
ver=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
python3 - "$app/Contents/Info.plist" "$id" "$name" "$ver" "$dev" <<'PY'
import plistlib, sys
out, bid, name, ver, dev = sys.argv[1:]
pl = {
    "CFBundleIdentifier": bid,
    "CFBundleName": name,
    "CFBundleExecutable": name,
    "CFBundlePackageType": "APPL",
    "CFBundleIconFile": "Orb",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": ver,
    "NSMicrophoneUsageDescription": "Speak to Greenlight: your voice is transcribed into the message box.",
    "NSSpeechRecognitionUsageDescription": "Transcribes what you say to Greenlight, on this Mac.",
}
if dev:
    pl["GreenlightDev"] = dev
with open(out, "wb") as f:
    plistlib.dump(pl, f)
PY
mkdir -p "$app/Contents/Resources" && cp dashboard.html terminal.html stage.html kokoro.py ../brand/icons/{Orb,MergeLight,ThreeFloors}.icns "$app/Contents/Resources/" && cp vendor/ghostty-web.js "$app/Contents/Resources/"  # the left pane, the Dock icons (../brand/icons/*.svg → rsvg-convert → iconutil; Orb is the bundle's)
if [ -n "$team" ] && codesign -fs "$apple" --timestamp=none -r="designated => anchor apple generic and certificate leaf[subject.OU] = \"$team\" and certificate leaf[subject.CN] = \"$cn\"" "$app"; then :
elif [ -n "$apple" ]; then
  # not Greenlight Dev instead: a build signed by the other one has a requirement your mic, speech and Keychain grants don't match
  echo "build.sh: error: signing with \"$cn\" failed: unlock the login keychain (it is locked over ssh), or click Always Allow when codesign asks on the Mac's screen, then build again" >&2; exit 1
elif [ -z "$devid" ]; then
  echo "build.sh: no signing cert; signing ad hoc (macOS will ask for the mic, speech and Keychain again)" >&2; codesign -fs - "$app"
else
  codesign -fs "$devid" --keychain "$kc" "$app" ||
    { echo "build.sh: signing with Greenlight Dev failed; signing ad hoc (macOS will ask for the mic, speech and Keychain again)" >&2; codesign -fs - "$app"; }
fi
# The old one goes with the staging folder, or back if the new one can't go in (unless another build put its own there meanwhile)
[ ! -e "$dest" ] || mv "$dest" "$stage/old.app"
if [ -e "$dest" ] || ! mv "$app" "$dest"; then
  [ -e "$dest" ] || [ ! -e "$stage/old.app" ] || mv "$stage/old.app" "$dest" || true; echo "build.sh: error: couldn't move the new $name.app into ~/Applications" >&2; exit 1
fi
# mv puts it inside a folder that turned up after the check (a build racing this one): take it out, leaving theirs
if [ -e "$dest/$name.app" ]; then rm -rf "$dest/$name.app"; echo "build.sh: error: another build installed $name.app meanwhile" >&2; exit 1; fi
app=$dest
# re-register so Finder/Dock pick up icon changes instead of a cached blank one
touch "$app"; /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app"
echo "$app"
