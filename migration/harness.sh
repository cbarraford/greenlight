#!/usr/bin/env bash
# Black-box harness: run a scenario against the Python scripts or the Rust binary, in a sealed
# temp HOME/GREENLIGHT_HOME with fake CLIs on PATH, and diff the result against recorded goldens.
#
#   harness.sh record [scenario...]        run Python, write scenarios/<s>/expected/
#   harness.sh check  [py|rs] [scenario...] run that impl, diff against expected/ (default rs)
#   harness.sh show   [py|rs] <scenario>   print the normalized result
#   harness.sh reply  <scenario> [--rc N] [--nth N] [--err FILE] <tool> [args...] < stdout
#                                          add a canned reply (--nth: only the Nth identical call;
#                                          write the sandbox root as <W> in args and replies)
#
# Defaults: GREENLIGHT_ME=me (an env line "GREENLIGHT_ME=" exercises the lookup), TZ=UTC, PYTHONHASHSEED=0,
# GREENLIGHT_NOW=1794484800 (2026-11-12T12:00Z; pyclock/ shifts Python to it, fixture files are stamped with it).
# Scenario dir: cmd (one line: "greenlight.py report --json"), optional stdin, env (KEY=VAL lines),
# home/ (initial $GREENLIGHT_HOME), userhome/ (initial $HOME), setup.sh (runs in the sandbox first,
# e.g. to build real git repos), timeout (seconds, default 60), fakes/<tool>/<key>.{out,rc,err,args} (see ./fake).
# Result = exit code + stdout + stderr + every fake call (sorted: threads reorder them) + the files
# left under both homes, with sandbox paths and clock values normalized away.
set -uo pipefail
M=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$M")
S="$M/scenarios"
PY=$(command -v python3)
RS=${GREENLIGHT_BIN:-$REPO/target/release/greenlight}
# Every CLI greenlight might shell out to. All are faked so nothing reaches a real forge, agent or
# scheduler; git is real unless a scenario fakes it.
FAKED="glab gh orca cmux herdr t3 claude codex opencode pi launchctl systemctl ssh osascript open notify-send crontab"

norm() {  # $1 = sandbox root
  R="$1" perl -pe 's#$ENV{R}#<W>#g;
    s#\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)?#<TS>#g;
    s#(?:\b[A-Z][a-z]{2} )?\b[A-Z][a-z]{2} +\d+ \d\d:\d\d(?::\d\d)?(?: \d{4})?#<TS>#g;
    s#\b17\d{8}(\d{3})?(\.\d+)?\b#<EPOCH>#g;
    s#"tps": [\d.]+#"tps": <N>#g;
    s#^Error: #<error> #' | perl -0pe '
    s/Traceback \(most recent call last\):\n(?:[ \t].*\n)*(?:\w+\.)*\w+(?:: (.*))?$/<error> $1/mg;
    s/^usage: .*\n(?:[ \t]+.*\n)*(?=\S+: error: )//mg'  # argparse usage text is Python's, the error line is ours
}

run() {  # $1 = py|rs, $2 = scenario; prints the normalized result
  local impl=$1 d="$S/$2" W; W=$(mktemp -d "${TMPDIR:-/tmp}/glh.XXXXXX"); W=$(cd "$W" && pwd -P)
  mkdir -p "$W/bin" "$W/home" "$W/u" "$W/cwd" "$W/repo"
  # A throwaway checkout: some commands write next to the scripts (chat.py memory-*). Rust finds it
  # through GREENLIGHT_ROOT; Python through its own path.
  cp -R "$REPO/scripts" "$REPO/lifecycles" "$REPO/prompts" "$W/repo/"; rm -rf "$W/repo/scripts/__pycache__"
  [ -d "$d/home" ] && cp -R "$d/home/." "$W/home/"
  [ -d "$d/userhome" ] && cp -R "$d/userhome/." "$W/u/"
  for t in $FAKED $( [ -d "$d/fakes" ] && ls "$d/fakes" ); do ln -sf "$M/fake" "$W/bin/$t"; done
  ln -sf "$PY" "$W/bin/python3"
  : > "$W/calls"
  local envs=(HOME="$W/u" GREENLIGHT_HOME="$W/home" PATH="$W/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    FAKES="$d/fakes" FAKE_LOG="$W/calls" FAKE_COUNT="$W/fakecount" GREENLIGHT_ME=me GREENLIGHT_NOW=1794484800 PYTHONPATH="$M/pyclock" PYTHONHASHSEED=0 TZ=UTC LANG=en_US.UTF-8 GREENLIGHT_ROOT="$W/repo" W="$W")
  [ -f "$d/env" ] && while IFS= read -r l; do [ -n "$l" ] && envs+=("${l//\$W/$W}"); done < "$d/env"
  TZ=UTC find "$W/home" "$W/u" -exec touch -t 202611121200.00 {} + 2>/dev/null
  [ -f "$d/setup.sh" ] && (cd "$W/cwd" && env -i "${envs[@]}" bash "$d/setup.sh" >/dev/null 2>&1)
  local script args in=/dev/null rc; read -r script args < "$d/cmd"; [ -f "$d/stdin" ] && in="$d/stdin"
  eval "set -- $args"  # cmd is shell words
  if [ "$impl" = py ]; then set -- "$PY" "$W/repo/scripts/$script" "$@"; else set -- "$RS" "${script%.py}" "$@"; fi
  local t=60; [ -f "$d/timeout" ] && t=$(cat "$d/timeout")
  (cd "$W/cwd" && env -i "${envs[@]}" perl -e 'alarm shift; exec @ARGV or die' "$t" "$@" < "$in" > "$W/out" 2> "$W/err"); rc=$?
  echo "rc=$rc"
  echo "--- stdout"; cat "$W/out"
  echo "--- stderr"; cat "$W/err"
  echo "--- calls"; sort "$W/calls"
  echo "--- files"
  (cd "$W" && { find home u -type f; find repo -type f ! -path '*/__pycache__/*' | while read -r f; do
      cmp -s "$f" "$REPO/${f#repo/}" || echo "$f"; done; } | LC_ALL=C sort | while read -r f; do
    echo "== $f"; if LC_ALL=C grep -qI . "$f" || [ ! -s "$f" ]; then cat "$f"; else echo "<binary $(shasum < "$f" | cut -c1-12)>"; fi
  done)
} 2>&1
result() { run "$@" | norm "$(cd "${TMPDIR:-/tmp}" && pwd -P)/glh\.[A-Za-z0-9]{6}"; }

scen() { if [ $# -gt 0 ]; then printf '%s\n' "$@"; else (cd "$S" && ls -d */cmd | sed 's#/cmd$##'); fi; }

case "${1:-}" in
  record) shift; for s in $(scen "$@"); do mkdir -p "$S/$s/expected"; result py "$s" > "$S/$s/expected/result"; echo "recorded $s"; done ;;
  check) shift; impl=rs; [[ "${1:-}" =~ ^(py|rs)$ ]] && { impl=$1; shift; }; fail=0
    for s in $(scen "$@"); do
      if diff -u "$S/$s/expected/result" <(result "$impl" "$s") > "$M/.diff.$s"; then echo "ok   $s"; rm -f "$M/.diff.$s"
      else echo "FAIL $s  (migration/.diff.$s)"; fail=1; fi
    done; exit $fail ;;
  show) shift; impl=rs; [[ "${1:-}" =~ ^(py|rs)$ ]] && { impl=$1; shift; }; result "$impl" "$1" ;;
  reply) shift; s=$1; shift; rc=0; nth=""; err=""
    while :; do case "$1" in --rc) rc=$2; shift 2;; --nth) nth=.$2; shift 2;; --err) err=$2; shift 2;; *) break;; esac; done; tool=$1; shift
    k=$(printf '%s\0' "$@" | shasum | cut -c1-12); f="$S/$s/fakes/$tool/$k$nth"; mkdir -p "$(dirname "$f")"
    cat > "$f.out"; echo "$rc" > "$f.rc"; [ -n "$err" ] && cp "$err" "$f.err"; printf '%q ' "$@" > "$f.args"; echo "$f" ;;
  *) sed -n '2,11p' "$0"; exit 2 ;;
esac
