#!/usr/bin/env bash
# test_emulator_harness.sh — local, no-network test of emulator-harness.sh.
#
# Run from anywhere (uses the script's own directory). No network. Asserts:
#   1. --help exits 0
#   2. an unknown subcommand exits non-zero
#   3. preflight --dry-run behaves sanely (prints commands, does not fail on the kvm gate)
#   4. preflight correctly FAILS on this local box (CobradorWave has no /dev/kvm)
#      with a clear message
#
# The last assertion is intentionally host-specific: this test is authored for the
# no-KVM laptop. On a KVM-capable host it would not apply, but that is the point of the
# gate — the emulator must refuse to run where it cannot accelerate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR/emulator-harness.sh"

FAILURES=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILURES=$((FAILURES+1)); }

[ -f "$HARNESS" ] || { echo "FATAL: $HARNESS not found" >&2; exit 1; }

echo "================================================================"
echo "1. --help exits 0"
echo "================================================================"
if "$HARNESS" --help >/dev/null 2>&1; then pass "--help exit 0"; else fail "--help exit 0"; fi

echo
echo "================================================================"
echo "2. unknown subcommand exits non-zero"
echo "================================================================"
if "$HARNESS" bogus-subcommand >/dev/null 2>&1; then
  fail "unknown subcommand exited 0 (expected non-zero)"
else
  pass "unknown subcommand exits non-zero"
fi

echo
echo "================================================================"
echo "3. preflight --dry-run behaves sanely"
echo "================================================================"
DRY_OUT="$("$HARNESS" --dry-run preflight 2>&1 || true)"
echo "$DRY_OUT"
# --dry-run must not abort on the kvm gate (it only prints commands).
if echo "$DRY_OUT" | grep -q "FATAL"; then
  fail "preflight --dry-run printed FATAL (dry-run should not abort)"
else
  pass "preflight --dry-run does not abort"
fi
if echo "$DRY_OUT" | grep -q "\[dry-run\]"; then
  pass "preflight --dry-run prints [dry-run] markers"
else
  fail "preflight --dry-run missing [dry-run] markers"
fi

echo
echo "================================================================"
echo "4. preflight FAILS on this no-KVM box with a clear message"
echo "================================================================"
PRE_OUT="$("$HARNESS" preflight 2>&1 || true)"
echo "$PRE_OUT"
# On CobradorWave /dev/kvm is absent: gate 1 must fail loudly.
if echo "$PRE_OUT" | grep -q "FATAL"; then
  pass "preflight printed FATAL on no-KVM host"
else
  fail "preflight did NOT fail on no-KVM host (expected FATAL)"
fi
if echo "$PRE_OUT" | grep -qi "kvm"; then
  pass "failure message mentions kvm"
else
  fail "failure message does not mention kvm"
fi

echo
echo "================================================================"
echo "RESULT: $FAILURES failure(s)"
echo "================================================================"
exit "$FAILURES"
