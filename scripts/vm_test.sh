#!/usr/bin/env bash
# vm_test.sh: Tier 2 pooled 1-2 slot macOS VM runner (Tart or Lume) for true OS-level
# WindowServer HID testing (real ⌘V paste, Finder drag-drop, menubar popovers, notifications)
# completely off your host keyboard, mouse, and screen.
#
# Apple's Virtualization.framework enforces a hard cap of 2 concurrent macOS VMs per Mac.
# This script coordinates parallel worktrees across 2 atomic lock slots so agents queue cleanly.
#
# Usage:
#   scripts/vm_test.sh --setup                      Print one-time base VM bootstrap instructions
#   scripts/vm_test.sh --dry-run                    Test atomic slot locking without booting a VM
#   scripts/vm_test.sh --run [CMD] [--out DIR]      Clone base VM, mount repo, run CMD inside VM
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="${GREENLIGHT_VM_BACKEND:-auto}"   # auto | tart | lume
BASE_IMAGE="${GREENLIGHT_VM_IMAGE:-greenlight-macos-base}"
VM_USER="${GREENLIGHT_VM_USER:-admin}"
MAX_SLOTS="${GREENLIGHT_VM_SLOTS:-2}"
LOCK_DIR="${TMPDIR:-/tmp}/greenlight-vm-locks"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

detect_backend() {
  if [ "$BACKEND" != "auto" ]; then echo "$BACKEND"; return; fi
  if command -v tart >/dev/null 2>&1; then echo "tart"
  elif command -v lume >/dev/null 2>&1; then echo "lume"
  else echo "none"; fi
}

print_setup() {
  cat <<'EOF'
One-time Tier 2 macOS VM bootstrap (pick Tart OR Lume):

Option A — Tart (Cirrus Labs):
  brew install cirruslabs/cli/tart
  tart clone ghcr.io/cirruslabs/macos-sequoia-xcode:latest greenlight-macos-base
  tart run greenlight-macos-base
  # Inside the VM (once):
  #   1. brew install cliclick steipete/tap/peekaboo tmux
  #   2. System Settings -> Privacy & Security -> Accessibility & Screen Recording:
  #      enable for Terminal / sshd-keygen-wrapper
  #   3. Shut down VM cleanly.

Option B — Lume (Cua):
  curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/lume/scripts/install.sh | bash
  lume pull macos-sequoia-xcode:latest greenlight-macos-base
  # Same one-time Accessibility + Screen Recording grant inside the VM.

Then run from any Greenlight worktree:
  scripts/vm_test.sh --run scripts/vm_hid_smoke.sh --out /tmp/vm-shots
EOF
}

SLOT_LOCK=""
acquire_slot() {
  mkdir -p "$LOCK_DIR"
  while true; do
    for slot in $(seq 1 "$MAX_SLOTS"); do
      local dir="$LOCK_DIR/slot-${slot}.lock"
      if mkdir "$dir" 2>/dev/null; then
        echo "$$" > "$dir/pid"
        SLOT_LOCK="$dir"
        return 0
      fi
      # Reclaim stale lock if holder PID is dead
      if [ -f "$dir/pid" ]; then
        local holder
        holder="$(cat "$dir/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
          rm -rf "$dir"
          continue
        fi
      fi
    done
    echo "vm_test: all $MAX_SLOTS VM slots busy; waiting..." >&2
    sleep 3
  done
}

release_slot() {
  if [ -n "${EPHEMERAL_VM:-}" ]; then
    case "${ACTIVE_BACKEND:-}" in
      tart) tart stop "$EPHEMERAL_VM" >/dev/null 2>&1 || true; tart delete "$EPHEMERAL_VM" >/dev/null 2>&1 || true ;;
      lume) lume stop "$EPHEMERAL_VM" >/dev/null 2>&1 || true; lume delete "$EPHEMERAL_VM" >/dev/null 2>&1 || true ;;
    esac
  fi
  if [ -n "$SLOT_LOCK" ] && [ -d "$SLOT_LOCK" ]; then
    rm -rf "$SLOT_LOCK"
  fi
}
trap release_slot EXIT

MODE=""
RUN_CMD="scripts/vm_hid_smoke.sh"
OUT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --setup) MODE="setup"; shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --run) MODE="run"; shift; if [ $# -gt 0 ] && [[ "$1" != --* ]]; then RUN_CMD="$1"; shift; fi ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$MODE" in
  setup) print_setup; exit 0 ;;
  dry-run)
    acquire_slot
    echo "ok  acquired atomic VM slot: $SLOT_LOCK (backend=$(detect_backend), base=$BASE_IMAGE, max_slots=$MAX_SLOTS)"
    exit 0
    ;;
  run)
    ACTIVE_BACKEND="$(detect_backend)"
    if [ "$ACTIVE_BACKEND" = "none" ]; then
      echo "vm_test: neither tart nor lume is installed. Run 'scripts/vm_test.sh --setup' first." >&2
      exit 2
    fi
    OUT_DIR="${OUT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/gl-vm-shots-XXXXXX")}"
    mkdir -p "$OUT_DIR"
    acquire_slot
    EPHEMERAL_VM="gl-ephemeral-$$-$(date +%s)"
    echo "vm_test: cloning $BASE_IMAGE -> $EPHEMERAL_VM via $ACTIVE_BACKEND (slot=$SLOT_LOCK)"
    if [ "$ACTIVE_BACKEND" = "tart" ]; then
      tart clone "$BASE_IMAGE" "$EPHEMERAL_VM"
      tart run --no-graphics --dir="repo:${REPO_ROOT}" --dir="out:${OUT_DIR}" "$EPHEMERAL_VM" >/dev/null 2>&1 &
      VM_PID=$!
      IP=""
      for _ in $(seq 1 60); do
        IP="$(tart ip "$EPHEMERAL_VM" 2>/dev/null || true)"
        [ -n "$IP" ] && break
        sleep 2
      done
      [ -n "$IP" ] || { echo "vm_test: timed out waiting for VM IP" >&2; kill "$VM_PID" 2>/dev/null || true; exit 1; }
      for _ in $(seq 1 30); do
        ssh "${SSH_OPTS[@]}" "${VM_USER}@${IP}" true 2>/dev/null && break
        sleep 2
      done
      q_cmd="$(printf '%q' "$RUN_CMD")"
      ssh "${SSH_OPTS[@]}" "${VM_USER}@${IP}" \
        "cd '/Volumes/My Shared Files/repo' && GREENLIGHT_VM_OUT='/Volumes/My Shared Files/out' bash $q_cmd"
    else
      lume clone "$BASE_IMAGE" "$EPHEMERAL_VM"
      lume run "$EPHEMERAL_VM" --no-display --shared-dir "${REPO_ROOT}:rw" --shared-dir "${OUT_DIR}:rw" >/dev/null 2>&1 &
      VM_PID=$!
      IP=""
      for _ in $(seq 1 60); do
        IP="$(lume get "$EPHEMERAL_VM" 2>/dev/null | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1 || true)"
        [ -n "$IP" ] && break
        sleep 2
      done
      [ -n "$IP" ] || { echo "vm_test: timed out waiting for Lume VM IP" >&2; kill "$VM_PID" 2>/dev/null || true; exit 1; }
      for _ in $(seq 1 30); do
        ssh "${SSH_OPTS[@]}" "${VM_USER}@${IP}" true 2>/dev/null && break
        sleep 2
      done
      q_repo="$(printf '%q' "/Volumes/My Shared Files/$(basename "$REPO_ROOT")")"
      q_out="$(printf '%q' "/Volumes/My Shared Files/$(basename "$OUT_DIR")")"
      q_cmd="$(printf '%q' "$RUN_CMD")"
      ssh "${SSH_OPTS[@]}" "${VM_USER}@${IP}" \
        "cd $q_repo && GREENLIGHT_VM_OUT=$q_out bash $q_cmd"
    fi
    echo "ok  VM HID run complete; artifacts in $OUT_DIR"
    ;;
  *) usage; exit 2 ;;
esac
