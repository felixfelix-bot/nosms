# Deploying the nosms ContextVM (CVM) server

The CVM server is the machine-facing twin of the HTTP API: MCP JSON-RPC carried
in Nostr kind-25910 gift wraps, addressed by the server's npub, discovered
through the CEP-6 catalog (kinds 11316–11320). It shares the price table, the
escrow ledger and the `Transport` interface with the HTTP service; only the wire
differs.

## What runs where

| Piece | Path | Runs as |
|---|---|---|
| CVM server | `scripts/run_cvm_server.py` | one long-lived process (relay client + notification loop) |
| CVM contract | `docs/cvm/llms.txt` (+ `llms-full.txt`) | served verbatim by the `docs` tool and as MCP resources |
| Escrow ledger | `NOSMS_DB_PATH` (`/var/lib/nosms/cvm-escrow.db`) | sqlite, same shape as the HTTP service's |
| Live verification | `scripts/cvm_live_verify.py` | operator-run, prints a JSON report |
| Mint round-trip | `scripts/live_escrow_roundtrip.py` | operator-run, proves the escrow swap |

Relays **verified live 2026-10-05** (strfry, NIP-11 OK, 131072 max message):

```
wss://relay.contextvm.org  wss://relay2.contextvm.org  wss://relay.primal.net
```

`relay.damus.io` is **excluded**: it bans this host's IP for rate-limit
violations. `relay.nostr.band` / `cvm.otherstuff.ai` were unreachable that day.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `CVM_NSEC` | key file `.cvm-server.nsec` | server identity. Generated + `chmod 600` on first run. **The client and server MUST use different keys** — a shared key makes the client receive its own requests. |
| `CVM_OWNER_PUBKEYS` | empty | comma-separated hex pubkeys that send FREE (the operator's own key). Everyone else pays. |
| `NOSMS_MINT_URL` | unset | mint postage is escrowed at (e.g. `https://testnut.cashu.space`). Unset ⇒ `sms.send` refuses with `rail_not_configured` rather than pretending. |
| `NOSMS_TRANSPORT` | `fake` | `jmp` \| `email_gateway` \| `telnyx` \| `fake`. The `fake` rail is a test double — **never deploy it as a real service**. |
| `CVM_BTC_USD` | published default | live BTC/USD used by `sms.pricing` for the ADR-0002 quote. |
| `NOSMS_CVM_CONTRACT_URL` | `https://nosms.orangesync.tech/cvm/llms.txt` | advertised in the CEP-6 catalog so `cvmi discover` can surface the contract. |
| `NOSMS_DB_PATH` | `./cvm-escrow.db` | escrow ledger. |
| `NOSMS_SMTP_HOST` / `_PORT` / `_SENDER` | `localhost` / `25` / `sms@orangesync.tech` | email rail only. |
| `NOSMS_CARRIER_MAP` | empty | email rail only: JSON `{"+14155550100": "tmobile"}` destination → carrier gateway. **Required** for the email rail to send — an unknown carrier is refused as `carrier_unknown`, because a guessed gateway silently loses mail. |

## First run

```sh
# 1. announce the CEP-6 catalog and exit (proves the relay path)
NOSMS_TRANSPORT=email_gateway NOSMS_MINT_URL=https://testnut.cashu.space \
  python3 scripts/run_cvm_server.py --announce

# 2. serve
NOSMS_TRANSPORT=email_gateway NOSMS_MINT_URL=https://testnut.cashu.space \
  NOSMS_CARRIER_MAP='{"+1XXXXXXXXXX": "tmobile"}' \
  python3 scripts/run_cvm_server.py
```

The runner prints the server npub, the rail and its **real** capability flags,
the relay set and the price. Publish the npub in `docs/cvm/llms.txt`
(`Server npub:`) and commit it, so a caller reading the contract knows whom to
address.

`nostr_sdk` is a Python-3.13 dependency and is **not** in the service `.venv`
(that venv is the HTTP service's). Run the CVM server with the interpreter that
has `nostr_sdk`, or add it to a dedicated CVM venv.

## Verification

```sh
# free surface + the VISIBLE refusal + a PAID send with a freshly minted token
python3 scripts/cvm_live_verify.py --server-npub npub1... --to +1XXXXXXXXXX

# free surface only (no mint, no postage)
python3 scripts/cvm_live_verify.py --server-npub npub1... --no-pay
```

Exit code 0 means every step produced the expected reply. Step 4 sends an
**unpaid** `sms.send` and *requires* a visible `isError` refusal — silence there
is the failure mode this harness exists to catch (it is how the send-path
`UnboundLocalError` was found: the handler died before publishing the reply and
the caller saw nothing).

## Honest limits of v1

- The rail is a single personal line (ADR-0002). `best_effort: true`,
  `delivery_receipts: false` are permanent: `accepted` is **not** delivered, and
  no surface may claim otherwise.
- The email rail requires an MTA the process can reach on `NOSMS_SMTP_HOST` and
  a configured carrier per destination. Neither is derivable from the number.
- On a rail the service cannot serve (down, terminated, paced out), the postage
  is refunded and the caller is told (`rail_unavailable`) — never charged.
- On a `best_effort` miss the postage is **not** refunded: the rail cannot prove
  non-delivery, and refunding on silence would pay out for messages that landed.
