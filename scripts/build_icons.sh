#!/usr/bin/env bash
# Build the app icon's .icns files from brand/icon/layers (brand/DESIGN.md §8.5). Runs on a Mac: iconutil is macOS-only.
#
#   scripts/build_icons.sh [OUT_DIR]    # default OUT_DIR: brand/icons
#
# Writes Greenlight.icns (the bundle icon) and GreenlightDebug.icns (the same icon with a neutral-grey light, for dev
# copies). Each variant is composited from the layer SVGs by brand/icon/generate.py inside the macOS icon grid's
# squircle (an 824 tile in 1024), rendered by rsvg-convert at every iconutil size (16 to 512, @1x and @2x) into an
# .iconset, then packed with `iconutil -c icns`. Needs rsvg-convert (`brew install librsvg`), iconutil and python3.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-"$root/brand/icons"}
for tool in rsvg-convert iconutil python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "build_icons.sh: error: needs $tool (rsvg-convert: brew install librsvg)" >&2
    exit 1
  fi
done
mkdir -p "$out"
work=$(mktemp -d "${TMPDIR:-/tmp}/greenlight-icons.XXXXXX")
trap 'rm -rf "$work"' EXIT

for variant in default debug; do
  name=Greenlight
  [ "$variant" = debug ] && name=GreenlightDebug
  PYTHONDONTWRITEBYTECODE=1 python3 "$root/brand/icon/generate.py" icns "$variant" >"$work/$name.svg"
  iconset="$work/$name.iconset"
  mkdir "$iconset"
  for size in 16 32 128 256 512; do
    rsvg-convert -w "$size" -h "$size" -o "$iconset/icon_${size}x${size}.png" "$work/$name.svg"
    rsvg-convert -w $((size * 2)) -h $((size * 2)) -o "$iconset/icon_${size}x${size}@2x.png" "$work/$name.svg"
  done
  iconutil -c icns -o "$out/$name.icns" "$iconset"
  echo "build_icons.sh: wrote $out/$name.icns"
done
