#!/usr/bin/env bash
# Copy greenlight to the host it runs on, into ~/.claude/skills/greenlight. The host: GREENLIGHT_HOST, else the menubar app's `host` setting.
# A local memory/ (notes greenlight writes there) syncs both ways, newest file wins.
# ponytail: deletions don't propagate; remove a note on both sides by hand
set -euo pipefail
cd "$(dirname "$0")/.."
H=${GREENLIGHT_HOST:-$(defaults read dev.greenlight.app host 2>/dev/null || true)}
[ -n "$H" ] && [ "$H" != local ] || { echo "usage: GREENLIGHT_HOST=<ssh host> scripts/sync.sh" >&2; exit 1; }
[ -d memory ] && { rsync -au "$H":.claude/skills/greenlight/memory/ memory/ 2>/dev/null || true; }  # pull notes the host's runs wrote
git rev-parse --short HEAD > VERSION  # the host's copy has no .git: diag.py reads its version from here
rsync -a --exclude .git --exclude .gitignore --exclude memory --exclude app ./ "$H":.claude/skills/greenlight/
[ -d memory ] && rsync -au memory/ "$H":.claude/skills/greenlight/memory/ || true
