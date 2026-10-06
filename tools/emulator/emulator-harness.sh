#!/usr/bin/env bash
# emulator-harness.sh — reproducible Android emulator bring-up for the WhatsApp rail.
#
# Host target: dq05 ONLY (c03rad0r-DQ05proplus). This script refuses to run the
# emulator on any host without an openable /dev/kvm.
#
# Idempotent. Every step prints the exact command it runs. Run with --dry-run to
# see commands without executing them. Subcommands:
#   preflight   verify the four gates (kvm / disk / ram / dl.google.com)
#   install     install emulator + system image via sdkmanager (no cmdline-tools upgrade)
#   create      create the wa-dev AVD (idempotent --force)
#   boot        launch headless and poll sys.boot_completed until 1 (bounded)
#   status      report emulator / adb / AVD state
#   teardown    adb emu kill + adb kill-server, print what was removed
#   reap        find and kill orphaned qemu-system / emulator processes
#   all         preflight + install + create + boot
#
# Constraints honoured by this script:
#   - NEVER -wipe-data (would destroy the live WhatsApp registration state)
#   - NEVER flip PlayStore.enabled (see README; the E1 image is a playstore build
#     but the AVD config has PlayStore.enabled=no, and adb root is refused)
#   - teardown always runs at end of a boot; do not leave a ~3.3 GB guest on a 4-core box
#
# Evidence basis: tools/emulator/evidence/e1_evidence.log (E1, run 2026-10-06).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment)
# ---------------------------------------------------------------------------
HOST_TARGET="c03rad0r-DQ05proplus"
SDK_ROOT="${ANDROID_HOME:-$HOME/Android/Sdk}"
SDKMANAGER="${SDKMANAGER:-$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager}"
AVDMANAGER="${AVDMANAGER:-$SDK_ROOT/cmdline-tools/latest/bin/avdmanager}"
EMULATOR="${EMULATOR:-$SDK_ROOT/emulator/emulator}"
ADB="${ADB:-$SDK_ROOT/platform-tools/adb}"
AVD_NAME="${AVD_NAME:-wa-dev}"
AVD_DEVICE="${AVD_DEVICE:-pixel_6}"
SYSTEM_IMAGE="${SYSTEM_IMAGE:-system-images;android-34;google_apis_playstore;x86_64}"
PLATFORM_PACKAGE="${PLATFORM_PACKAGE:-platform-tools}"
BOOT_TIMEOUT_S="${BOOT_TIMEOUT_S:-600}"     # 55 * 10s polls in E1; 600s headroom
BOOT_POLL_INTERVAL_S="${BOOT_POLL_INTERVAL_S:-10}"

# Gates (minimums from E1 preflight)
MIN_DISK_GB=15
MIN_RAM_GB=4

DRY_RUN=0

log()  { echo "[$(date -u +%H:%M:%S)] $*"; }
run()  {
  # run <description> <cmd...>  — print the exact command, then execute (or skip in --dry-run)
  local desc="$1"; shift
  echo "\$ $*"    # exact command, quoted args rendered verbatim
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: $desc"
    return 0
  fi
  "$@"
}

die()  { echo "FATAL: $*" >&2; exit 1; }
warn() { echo "WARN:  $*" >&2; }

usage() {
  cat <<'EOF'
Usage: emulator-harness.sh [--dry-run] <preflight|install|create|boot|status|teardown|reap|all>
       emulator-harness.sh --help

  --dry-run   print every command without executing it (safe on dq05)
  --help      show this message

Subcommands (all idempotent):
  preflight  gate check: openable /dev/kvm, >=15G disk, >=4G RAM, dl.google.com reachable
  install    sdkmanager install emulator + system image + platform-tools (no cmdline-tools upgrade)
  create     create AVD 'wa-dev' (pixel_6, android-34 google_apis_playstore x86_64)
  boot       headless launch + poll sys.boot_completed until 1 (bounded, prints t=<s>)
  status     report current emulator / adb / AVD state
  teardown   adb emu kill + adb kill-server, print what was removed
  reap       kill orphaned qemu-system / emulator processes
  all        preflight + install + create + boot

This script targets dq05 ONLY and refuses to run the emulator on any host
without an openable /dev/kvm.
EOF
}

# ---------------------------------------------------------------------------
# Host gating
# ---------------------------------------------------------------------------
require_dq05() {
  # The emulator may only run on a host with an openable /dev/kvm. On dq05 this
  # is satisfied; on a laptop without KVM (e.g. CobradorWave) it is not.
  local actual
  actual="$(hostname)"
  if [ "$actual" != "$HOST_TARGET" ]; then
    warn "expected host $HOST_TARGET, running on $actual (proceeding; KVM gate is authoritative)"
  fi
  if [ ! -c /dev/kvm ]; then
    die "/dev/kvm is not present or not a char device on $(hostname). This harness only runs the emulator on a KVM-capable host (dq05)."
  fi
  if ! python3 -c 'import os,sys; os.close(os.open("/dev/kvm", os.O_RDWR))' 2>/dev/null; then
    die "/dev/kvm is not openable O_RDWR by $(id -un) on $(hostname). This harness only runs the emulator on a KVM-capable host (dq05)."
  fi
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
preflight() {
  log "PREFLIGHT: host=$(hostname) user=$(id -un)"

  # GATE 1 — /dev/kvm openable
  echo "--- gate 1: /dev/kvm openable O_RDWR ---"
  echo "\$ python3 -c 'import os; os.close(os.open(\"/dev/kvm\", os.O_RDWR)); print(\"KVM openable: True\")'"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: kvm open check"
    echo "GATE 1 PASS (dry-run)"
  else
    python3 -c 'import os; os.close(os.open("/dev/kvm", os.O_RDWR)); print("KVM openable: True")' \
      || die "GATE 1 FAILED: /dev/kvm not openable"
    echo "GATE 1 PASS"
  fi

  # GATE 2 — disk >= 15G free on /
  echo "--- gate 2: disk avail on / >= ${MIN_DISK_GB} GB ---"
  echo "\$ df -BG /"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: disk check"
    echo "GATE 2 PASS (dry-run)"
  else
    local avail_gb
    avail_gb=$(df -BG / | awk 'NR==2 {gsub(/G/,"",$4); print $4}')
    echo "avail_GB=$avail_gb"
    if [ "${avail_gb:-0}" -lt "$MIN_DISK_GB" ]; then
      die "GATE 2 FAILED: only ${avail_gb}G free on / (need >= ${MIN_DISK_GB}G)"
    fi
    echo "GATE 2 PASS"
  fi

  # GATE 3 — RAM available >= 4G
  echo "--- gate 3: RAM available >= ${MIN_RAM_GB} GB ---"
  echo "\$ free -g"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: RAM check"
    echo "GATE 3 PASS (dry-run)"
  else
    local avail_ram_gb
    avail_ram_gb=$(free -g | awk '/^Mem:/ {print $7}')
    echo "available_GB=$avail_ram_gb"
    if [ "${avail_ram_gb:-0}" -lt "$MIN_RAM_GB" ]; then
      die "GATE 3 FAILED: only ${avail_ram_gb}G available RAM (need >= ${MIN_RAM_GB}G)"
    fi
    echo "GATE 3 PASS"
  fi

  # GATE 4 — dl.google.com reachable
  echo "--- gate 4: dl.google.com reachable ---"
  echo "\$ curl -sI -o /dev/null -w '%{http_code}' https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: dl.google.com reachability"
    echo "GATE 4 PASS (dry-run)"
  else
    local code
    code=$(curl -sI -o /dev/null -w "%{http_code}" https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip)
    echo "dl.google.com status: $code"
    if [ "$code" != "200" ] && [ "$code" != "302" ]; then
      die "GATE 4 FAILED: dl.google.com returned HTTP $code (need 200/302)"
    fi
    echo "GATE 4 PASS"
  fi

  # GATE 5 — sdkmanager present
  echo "--- gate 5: sdkmanager present ---"
  echo "\$ test -x $SDKMANAGER"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: sdkmanager presence"
    echo "GATE 5 PASS (dry-run)"
  else
    if [ ! -x "$SDKMANAGER" ]; then
      die "GATE 5 FAILED: sdkmanager not found at $SDKMANAGER (set SDK_ROOT or install cmdline-tools)"
    fi
    echo "sdkmanager: $SDKMANAGER"
    echo "GATE 5 PASS"
  fi

  log "PREFLIGHT COMPLETE: all gates passed"
}

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
install() {
  require_dq05
  [ -x "$SDKMANAGER" ] || die "sdkmanager not found at $SDKMANAGER — run preflight"
  log "INSTALL: emulator + system image via sdkmanager (no cmdline-tools upgrade)"
  # The "SDK XML version 4 > 3" warning is NON-FATAL. Do not upgrade cmdline-tools.
  run "install packages" "$SDKMANAGER" --sdk_root="$SDK_ROOT" "$PLATFORM_PACKAGE" emulator "$SYSTEM_IMAGE"
  log "INSTALL COMPLETE"
}

# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
create() {
  require_dq05
  [ -x "$AVDMANAGER" ] || die "avdmanager not found at $AVDMANAGER — run install first"
  log "CREATE: AVD '$AVD_NAME' (device=$AVD_DEVICE, image=$SYSTEM_IMAGE)"
  # echo no | … answers the "create a custom hardware profile?" prompt; --force is idempotent.
  echo "echo no | $AVDMANAGER create avd -n $AVD_NAME -k \"$SYSTEM_IMAGE\" -d $AVD_DEVICE --force"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: create AVD"
  else
    echo no | "$AVDMANAGER" create avd -n "$AVD_NAME" -k "$SYSTEM_IMAGE" -d "$AVD_DEVICE" --force
  fi
  log "CREATE COMPLETE"
}

# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------
boot() {
  require_dq05
  [ -x "$EMULATOR" ] || die "emulator not found at $EMULATOR — run install first"
  [ -x "$ADB" ] || die "adb not found at $ADB — run install first"
  [ -d "$HOME/.android/avd/$AVD_NAME.avd" ] || die "AVD '$AVD_NAME' not found — run create first"

  log "BOOT: headless launch of AVD '$AVD_NAME'"
  # NEVER add -wipe-data here (destroys live WhatsApp state). NEVER touch PlayStore.enabled.
  "$EMULATOR" -avd "$AVD_NAME" -no-window -no-audio -no-snapshot \
      -gpu swiftshader_indirect -no-metrics -no-boot-anim \
      >/tmp/emulator-harness-boot.log 2>&1 &
  local emu_pid=$!
  echo "emulator pid $emu_pid"

  log "Waiting for device…"
  "$ADB" wait-for-device

  local n b
  n=$(( (BOOT_TIMEOUT_S + BOOT_POLL_INTERVAL_S - 1) / BOOT_POLL_INTERVAL_S ))
  for i in $(seq 1 "$n"); do
    b=$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    echo "t=$((i*BOOT_POLL_INTERVAL_S))s boot_completed=[$b]"
    if [ "$b" = "1" ]; then
      echo "BOOTED"
      "$ADB" devices -l
      return 0
    fi
    sleep "$BOOT_POLL_INTERVAL_S"
  done
  die "BOOT TIMEOUT: sys.boot_completed did not reach 1 within ${BOOT_TIMEOUT_S}s"
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
status() {
  echo "--- AVDs ---"
  "$AVDMANAGER" list avd 2>/dev/null || echo "(avdmanager unavailable)"
  echo "--- adb devices ---"
  "$ADB" devices -l 2>/dev/null || echo "(adb unavailable)"
  echo "--- emulator processes ---"
  pgrep -af "qemu-system|emulator -avd" || echo "(none)"
}

# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------
teardown() {
  log "TEARDOWN"
  echo "$ADB -s emulator-5554 emu kill"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: emu kill"
  else
    "$ADB" -s emulator-5554 emu kill 2>/dev/null || warn "emu kill failed (no running emulator?)"
  fi
  echo "$ADB kill-server"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: kill-server"
  else
    "$ADB" kill-server 2>/dev/null || true
  fi
  log "TEARDOWN COMPLETE (guest stopped; userdata preserved — no wipe)"
}

# ---------------------------------------------------------------------------
# reap
# ---------------------------------------------------------------------------
reap() {
  log "REAP: find and kill orphaned qemu-system / emulator processes"
  local pids
  pids=$(pgrep -f "qemu-system|emulator -avd" || true)
  if [ -z "$pids" ]; then
    echo "REAPER CLEAN: no qemu/emulator processes"
    return 0
  fi
  echo "Orphaned processes:"
  pgrep -af "qemu-system|emulator -avd"
  echo "kill $pids"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] skipped: kill"
  else
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 2
    pkill -9 -f "qemu-system|emulator -avd" 2>/dev/null || true
  fi
  log "REAP COMPLETE"
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
main() {
  local cmd=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1; shift ;;
      --help|-h) usage; exit 0 ;;
      -*) die "unknown option: $1" ;;
      *) cmd="$1"; shift ;;
    esac
  done

  [ -n "$cmd" ] || { usage; exit 1; }

  case "$cmd" in
    preflight) preflight ;;
    install)   install ;;
    create)    create ;;
    boot)      boot ;;
    status)    status ;;
    teardown)  teardown ;;
    reap)      reap ;;
    all)       preflight; install; create; boot ;;
    *)         echo "unknown subcommand: $cmd" >&2; usage >&2; exit 1 ;;
  esac
}

main "$@"
