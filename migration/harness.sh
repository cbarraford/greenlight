#!/usr/bin/env bash
# Black-box harness: run a scenario against the greenlight binary, in a sealed temp HOME/GREENLIGHT_HOME
# with fake CLIs on PATH, and diff the result against goldens (first recorded from the Python original;
# deliberate changes since are in BUGS.md).
#
#   harness.sh record [scenario...]        write scenarios/<s>/expected/ from the current binary
#   harness.sh check  [scenario...]        diff against expected/
#   harness.sh show   <scenario>           print the normalized result
#   harness.sh reply  <scenario> [--rc N] [--nth N] [--err FILE] <tool> [args...] < stdout
#                                          add a canned reply (--nth: only the Nth identical call;
#                                          write the sandbox root as <W> in args and replies)
#
# Defaults: GREENLIGHT_ME=me (an env line "GREENLIGHT_ME=" exercises the lookup), TZ=UTC,
# GREENLIGHT_NOW=1794484800 (2026-11-12T12:00Z; fixture files are stamped with it). Some setup.sh build fixtures
# with python3 (test-only; PYTHONDONTWRITEBYTECODE keeps its caches out of the sandbox home).
# Scenario dir: cmd (one line: "greenlight.py report --json"), optional stdin, env (KEY=VAL lines),
# home/ (initial $GREENLIGHT_HOME), userhome/ (initial $HOME), setup.sh (runs in the sandbox first,
# e.g. to build real git repos), timeout (seconds, default 60), fakes/<tool>/<key>.{out,rc,err,args} (see ./fake).
# Result = exit code + stdout + stderr + every fake call (sorted: threads reorder them) + the files
# left under both homes, with sandbox paths and clock values normalized away.
set -uo pipefail
M=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$M")
S="$M/scenarios"
RS=${GREENLIGHT_BIN:-$REPO/target/release/greenlight}; [ -e "$RS" ] && RS=$(cd "$(dirname "$RS")" && pwd -P)/$(basename "$RS")
# Every CLI greenlight might shell out to. All are faked so nothing reaches a real forge, agent or
# scheduler; git is real unless a scenario fakes it.
FAKED="glab gh orca cmux herdr t3 claude codex opencode pi launchctl systemctl ssh osascript open notify-send crontab"

norm() {  # $1 = sandbox root
  R="$1" perl -pe 's#$ENV{R}#<W>#g;
    s#\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)?#<TS>#g;
    s#(?:\b[A-Z][a-z]{2} )?\b[A-Z][a-z]{2} +\d+ \d\d:\d\d(?::\d\d)?(?: \d{4})?#<TS>#g;
    s#\b17\d{8}(\d{3})?(\.\d+)?\b#<EPOCH>#g;
    s#"tps": [\d.]+#"tps": <N>#g;
    s#^Error: #<error> #' | B="$RS" perl -0pe '
    s/Traceback \(most recent call last\):\n(?:[ \t].*\n)*(?:\w+\.)*\w+(?:: (.*))?$/<error> $1/mg;
    s/^usage: .*\n(?:[ \t]+.*\n)*(?=\S+: error: )//mg;  # argparse usage text is Python-only, the error line is ours
    s/"\Q$ENV{B}\E"/"<BIN>"/g'  # a next.cmd that runs greenlight
}

run() {  # $1 = scenario; prints the normalized result
  local d="$S/$1" W; W=$(mktemp -d "${TMPDIR:-/tmp}/glh.XXXXXX"); W=$(cd "$W" && pwd -P)
  mkdir -p "$W/bin" "$W/home" "$W/u" "$W/cwd" "$W/repo"
  # A throwaway checkout (GREENLIGHT_ROOT): some commands write into it (chat memory-*).
  cp -R "$REPO/lifecycles" "$REPO/prompts" "$W/repo/"
  [ -d "$d/home" ] && cp -R "$d/home/." "$W/home/"
  [ -d "$d/userhome" ] && cp -R "$d/userhome/." "$W/u/"
  for t in $FAKED $( [ -d "$d/fakes" ] && ls "$d/fakes" ); do ln -sf "$M/fake" "$W/bin/$t"; done
  : > "$W/calls"
  local envs=(HOME="$W/u" GREENLIGHT_HOME="$W/home" PATH="$W/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    FAKES="$d/fakes" FAKE_LOG="$W/calls" FAKE_COUNT="$W/fakecount" GREENLIGHT_ME=me GREENLIGHT_NOW=1794484800 PYTHONDONTWRITEBYTECODE=1 TZ=UTC LANG=en_US.UTF-8 GREENLIGHT_ROOT="$W/repo" W="$W")
  [ -f "$d/env" ] && while IFS= read -r l; do [ -n "$l" ] && envs+=("${l//\$W/$W}"); done < "$d/env"
  TZ=UTC find "$W/home" "$W/u" -exec touch -t 202611121200.00 {} + 2>/dev/null
  [ -f "$d/setup.sh" ] && (cd "$W/cwd" && env -i "${envs[@]}" bash "$d/setup.sh" >/dev/null 2>&1)
  local script args in=/dev/null rc; read -r script args < "$d/cmd"; [ -f "$d/stdin" ] && in="$d/stdin"
  eval "set -- $args"  # cmd is shell words
  set -- "$RS" "${script%.py}" "$@"  # cmd still names the old script (greenlight.py report)
  local t=60; [ -f "$d/timeout" ] && t=$(cat "$d/timeout")
  (cd "$W/cwd" && env -i "${envs[@]}" perl -e 'alarm shift; exec @ARGV or die' "$t" "$@" < "$in" > "$W/out" 2> "$W/err"); rc=$?
  echo "rc=$rc"
  echo "--- stdout"; cat "$W/out"
  echo "--- stderr"; cat "$W/err"
  echo "--- calls"; sort "$W/calls"
  echo "--- files"
  (cd "$W" && { find home u -type f; find repo -type f | while read -r f; do
      cmp -s "$f" "$REPO/${f#repo/}" || echo "$f"; done; } | LC_ALL=C sort | while read -r f; do
    echo "== $f"; if LC_ALL=C grep -qI . "$f" || [ ! -s "$f" ]; then cat "$f"; else echo "<binary $(shasum < "$f" | cut -c1-12)>"; fi
  done)
} 2>&1
result() { run "$@" | norm "$(cd "${TMPDIR:-/tmp}" && pwd -P)/glh\.[A-Za-z0-9]{6}"; }

scen() { if [ $# -gt 0 ]; then printf '%s\n' "$@"; else (cd "$S" && ls -d */cmd | sed 's#/cmd$##'); fi; }

case "${1:-}" in
  record) shift; for s in $(scen "$@"); do mkdir -p "$S/$s/expected"; result "$s" > "$S/$s/expected/result"; echo "recorded $s"; done ;;
  check) shift; fail=0
    for s in $(scen "$@"); do
      if diff -u "$S/$s/expected/result" <(result "$s") > "$M/.diff.$s"; then echo "ok   $s"; rm -f "$M/.diff.$s"
      else echo "FAIL $s  (migration/.diff.$s)"; fail=1; fi
    done; exit $fail ;;
  show) shift; result "$1" ;;
  reply) shift; s=$1; shift; rc=0; nth=""; err=""
    while :; do case "$1" in --rc) rc=$2; shift 2;; --nth) nth=.$2; shift 2;; --err) err=$2; shift 2;; *) break;; esac; done; tool=$1; shift
    k=$(printf '%s\0' "$@" | shasum | cut -c1-12); f="$S/$s/fakes/$tool/$k$nth"; mkdir -p "$(dirname "$f")"
    cat > "$f.out"; echo "$rc" > "$f.rc"; [ -n "$err" ] && cp "$err" "$f.err"; printf '%q ' "$@" > "$f.args"; echo "$f" ;;
  *) sed -n '2,11p' "$0"; exit 2 ;;
esac
