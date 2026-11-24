#!/usr/bin/env bash
# Build a release Greenlight.app and zip it (docs/RELEASING.md). Not your own build (that's build.sh): nothing is installed.
#   app/release.sh <version> <short version> <build> <backend .tar.gz> <out dir>
# Signing, from the environment:
#   GREENLIGHT_SIGN_ID   a "Developer ID Application: …" identity (with GREENLIGHT_KEYCHAIN, the keychain that holds it):
#                        hardened runtime and a secure timestamp, as notarization needs. Unset: ad hoc, and the feed says so.
#   GREENLIGHT_NOTARY_KEY, GREENLIGHT_NOTARY_KEY_ID, GREENLIGHT_NOTARY_ISSUER   an App Store Connect API key (.p8): notarize
#                        and staple. Unset: not notarized.
# Prints one JSON line: {zip, signing, notarized}.
set -euo pipefail
cd "$(dirname "$0")"
[ $# -eq 5 ] || { echo "usage: app/release.sh <version> <short> <build> <backend.tar.gz> <out dir>" >&2; exit 2; }
ver=$1 short=$2 build=$3 backend=$4 out=$5
case "$ver" in ''|*[!A-Za-z0-9._+-]*|[!A-Za-z0-9]*) echo "release.sh: bad version: $ver" >&2; exit 2 ;; esac
case "$short" in ''|*[!0-9.]*) echo "release.sh: bad short version: $short" >&2; exit 2 ;; esac
case "$build" in ''|*[!0-9]*) echo "release.sh: bad build: $build" >&2; exit 2 ;; esac
[ -f "$backend" ] || { echo "release.sh: no backend at $backend" >&2; exit 2; }
min_macos=14.0  # the oldest target Sources/*.swift compiles for (13.0 doesn't); scripts/release.py's MIN_MACOS matches
name=Greenlight id=dev.greenlight.app
mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
app=$work/$name.app
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
# Universal: both architectures, each for macOS 14, joined with lipo
for arch in arm64 x86_64; do
  DEVELOPER_DIR=/Library/Developer/CommandLineTools swiftc -O -target "$arch-apple-macos$min_macos" Sources/*.swift -o "$work/$arch"
done
lipo -create "$work/arm64" "$work/x86_64" -output "$app/Contents/MacOS/$name"
# shellcheck source=app/bundle.sh
. ./bundle.sh
gl_plist "$app/Contents/Info.plist" "$id" "$name" "$short" "$build" "" "$min_macos"
gl_resources "$app"
cp "$backend" "$app/Contents/Resources/backend.tar.gz"  # installed where greenlight runs (scripts/update.py install)
printf '%s\n' "$ver" > "$app/Contents/Resources/VERSION"

# The only entitlement under the hardened runtime: the mic (voice mode). Keychain items and child processes need none.
cat > "$work/entitlements.plist" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>com.apple.security.device.audio-input</key><true/></dict></plist>
XML
signing=adhoc notarized=false
if [ -n "${GREENLIGHT_SIGN_ID:-}" ]; then
  codesign --force --options runtime --timestamp --entitlements "$work/entitlements.plist" \
    ${GREENLIGHT_KEYCHAIN:+--keychain "$GREENLIGHT_KEYCHAIN"} --sign "$GREENLIGHT_SIGN_ID" "$app"
  signing=developer_id
else
  echo "release.sh: no GREENLIGHT_SIGN_ID: signing ad hoc (macOS warns on first open, and asks for the mic again after updates)" >&2
  codesign --force --options runtime --entitlements "$work/entitlements.plist" --sign - "$app"
fi
codesign --verify --strict --deep "$app"

zip=$out/$name-$ver.zip
rm -f "$zip"
ditto -c -k --keepParent "$app" "$zip"  # ditto keeps the signature's extended attributes; zip(1) doesn't
if [ "$signing" = developer_id ] && [ -n "${GREENLIGHT_NOTARY_KEY:-}" ]; then
  xcrun notarytool submit "$zip" --key "$GREENLIGHT_NOTARY_KEY" --key-id "$GREENLIGHT_NOTARY_KEY_ID" \
    --issuer "$GREENLIGHT_NOTARY_ISSUER" --wait --timeout 30m
  xcrun stapler staple "$app"  # the ticket goes in the app, so it opens offline: zip it again
  rm -f "$zip"
  ditto -c -k --keepParent "$app" "$zip"
  spctl --assess --type execute "$app"
  notarized=true
fi
printf '{"zip": "%s", "signing": "%s", "notarized": %s}\n' "$zip" "$signing" "$notarized"
