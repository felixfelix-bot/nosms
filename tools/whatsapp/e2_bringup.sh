#!/usr/bin/env bash
# e2_bringup.sh — dq05-side bring-up for the E2 registration pass, run FROM
# CobradorWave. Reuses tools/emulator/emulator-harness.sh verbatim over ssh
# (never a second harness), then establishes the adb port-forward so the whole
# OTP loop is local (shape (b)).
#
#   e2_bringup.sh boot     boot emulator on dq05 via the canonical harness
#   e2_bringup.sh tunnel   ensure ssh -L 15037:dq05:5037 forward is alive
#   e2_bringup.sh teardown harness teardown + close local tunnel
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HARNESS="$HERE/../emulator/emulator-harness.sh"
PORT="${ADB_FORWARD_PORT:-15037}"

case "${1:-}" in
  boot)
    ssh dq05 'bash -s' < "$HARNESS" boot
    ;;
  status)
    ssh dq05 'bash -s' < "$HARNESS" status
    ;;
  tunnel)
    if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PORT"; then
      echo "tunnel already listening on 127.0.0.1:$PORT"
    else
      ssh -f -N -L "$PORT:127.0.0.1:5037" dq05
      sleep 1
      ss -ltn | grep "127.0.0.1:$PORT" || { echo "tunnel FAILED to come up"; exit 1; }
      echo "tunnel up: 127.0.0.1:$PORT -> dq05:5037"
    fi
    ;;
  teardown)
    ssh dq05 'bash -s' < "$HARNESS" teardown || true
    pkill -f "ssh -f -N -L $PORT:127.0.0.1:5037" 2>/dev/null || true
    echo "local teardown done"
    ;;
  *)
    echo "usage: $0 boot|status|tunnel|teardown" >&2
    exit 2
    ;;
esac
