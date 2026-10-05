#!/usr/bin/env bash
# Start the nosms CVM wire server (real relays) in the background, detached.
# Usage:  scripts/start_cvm_server.sh [logfile]
set -euo pipefail
cd "$(dirname "$0")/.."
LOG="${1:-/tmp/nosms-cvm-server.log}"
export PATH="$HOME/.local/bin:$PATH"
export NOSMS_TRANSPORT="${NOSMS_TRANSPORT:-email_gateway}"
pkill -f 'run_cvm_serve[r]\.py' 2>/dev/null || true   # bracket: never match self
sleep 1
setsid python3.13 -u scripts/run_cvm_server.py \
  --relays wss://relay.contextvm.org wss://relay2.contextvm.org wss://relay.primal.net \
  --key-file "${CVM_KEY_FILE:-.cvm-server.nsec}" >"$LOG" 2>&1 </dev/null &
sleep 12
grep -E 'server npub|transport|listening' "$LOG" || true
echo "log: $LOG"
