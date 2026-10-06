#!/usr/bin/env bash
# Start the whole nosms E2E environment, for real:
#
#   1. the ContextVM wire server (app/cvm_tools.py over real relays)
#   2. Kehto's Paja shell runtime, which serves the napplet in the sandboxed
#      iframe and owns NAP-CVM (relay routing, signing, NIP-44, payment)
#   3. the napplet's Vite dev server, as Paja's target
#
# Nothing is stubbed: the shell talks to wss://relay.contextvm.org /
# relay2.contextvm.org / relay.primal.net and the server answers through them.
# `relay.damus.io` is IP-banned from this host (2026-10-05) and is not used.
#
# The server npub is written to e2e/.e2e-env.json so the spec asserts against the
# identity that is actually running, never a remembered one.
#
# Usage:  scripts/start_e2e_env.sh [--paja-port 5197] [--fresh]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PAJA_PORT="${PAJA_PORT:-5197}"
VITE_PORT="${VITE_PORT:-5173}"
RELAY_ARGS=(--relay-url wss://relay.contextvm.org --relay-url wss://relay2.contextvm.org --relay-url wss://relay.primal.net)
RUN_DIR="${RUN_DIR:-/tmp/nosms-e2e}"
KEY_FILE="${RUN_DIR}/server.nsec"

FRESH=0
for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    --paja-port) shift ;;
  esac
done

export PATH="$HOME/.deno/bin:$HOME/.local/bin:$HOME/toolchains/node-v22.17.0-linux-x64/bin:$PATH"
mkdir -p "$RUN_DIR"
if [ "$FRESH" = "1" ]; then rm -f "$KEY_FILE" "$RUN_DIR"/*.log; fi

echo "== stopping any previous run =="
pkill -f 'run_cvm_serve[r]\.py' 2>/dev/null || true
pkill -f 'kehto.*paj[a]' 2>/dev/null || true
pkill -f "vite.*--por[t] ${VITE_PORT}" 2>/dev/null || true
sleep 1

echo "== 1/3 CVM wire server =="
NOSMS_TRANSPORT="${NOSMS_TRANSPORT:-email_gateway}" \
  setsid python3.13 -u scripts/run_cvm_server.py \
  --relays wss://relay.contextvm.org wss://relay2.contextvm.org wss://relay.primal.net \
  --key-file "$KEY_FILE" >"$RUN_DIR/server.log" 2>&1 </dev/null &
for _ in $(seq 1 30); do grep -q 'listening' "$RUN_DIR/server.log" 2>/dev/null && break; sleep 1; done
SERVER_NPUB="$(grep -o 'server npub npub1[a-z0-9]*' "$RUN_DIR/server.log" | head -1 | awk '{print $3}')"
[ -n "$SERVER_NPUB" ] || { echo "server did not come up:"; cat "$RUN_DIR/server.log"; exit 1; }
echo "   server npub $SERVER_NPUB"

echo "== 2/3 Paja shell runtime on :$PAJA_PORT =="
# Pin the identity the napplet must resolve DIRECTLY, so the E2E is deterministic
# even when other nosms instances are announced on the same relays. Vite reads
# these and bakes them in via `define`.
export VITE_NOSMS_CVM_PUBKEY="$SERVER_NPUB"
export VITE_NOSMS_CVM_RELAYS="wss://relay.contextvm.org,wss://relay2.contextvm.org,wss://relay.primal.net"
setsid kehto paja \
  --target-url "http://127.0.0.1:${VITE_PORT}" \
  --host 127.0.0.1 --port "$PAJA_PORT" \
  --relay-mode live "${RELAY_ARGS[@]}" \
  --acl-mode allow --storage-mode memory --cache-mode memory \
  --ready-timeout 60000 \
  -- bash -lc "cd '$REPO/napplet' && exec pnpm vite --host 127.0.0.1 --port $VITE_PORT" \
  >"$RUN_DIR/paja.log" 2>&1 </dev/null &
for _ in $(seq 1 60); do
  curl -sf -o /dev/null "http://127.0.0.1:${PAJA_PORT}/" && break
  sleep 1
done
curl -sf -o /dev/null "http://127.0.0.1:${PAJA_PORT}/" || { echo "paja did not come up:"; cat "$RUN_DIR/paja.log"; exit 1; }
echo "   paja  http://127.0.0.1:${PAJA_PORT}/"

echo "== 3/3 write runtime facts =="
python3.13 - "$SERVER_NPUB" "$PAJA_PORT" "$RUN_DIR" <<'PY'
import json, pathlib, sys
npub, port, run_dir = sys.argv[1], sys.argv[2], sys.argv[3]
# The address a human writes is an npub; what the shell's cvm.callTool needs is
# the hex. Record both so the spec never has to convert (or guess).
from nostr_sdk import PublicKey
hexpk = PublicKey.parse(npub).to_hex()
out = pathlib.Path("napplet/e2e/.e2e-env.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "server_npub": npub,
    "server_pubkey_hex": hexpk,
    "paja_url": f"http://127.0.0.1:{port}/",
    "run_dir": run_dir,
    "relays": ["wss://relay.contextvm.org", "wss://relay2.contextvm.org", "wss://relay.primal.net"],
    "transport": "email_gateway",
}, indent=2) + "\n")
print(out.read_text())
PY

echo "== ready =="
