# nosms sender — a NIP-5D napplet wrapping the paid ContextVM SMS service

One screen: a destination, a message, the price **before** you send, and the
server's own honesty flags. The napplet draws UI and nothing else — the shell
holds the keys, talks to relays, and pays.

## Why ContextVM and not the HTTP API

A napplet runs in a sandboxed iframe with an opaque origin. It has **no HTTP
verb**: NAP-RESOURCE is bytes-in/bytes-out, so `POST /api/send` — a POST with a
JSON body — is *not expressible* from a napplet. The HTTP surface
(`https://nosms.orangesync.tech`) stays the surface for scripts, `curl` and LLMs
with network access; the CVM surface is for in-shell clients. Both sit on the
same `Transport` and the same price table.

```
napplet (UI only: no keys, no tokens, no network)
  └─ shell NAP-CVM (`cvm` domain)   ← the shell signs, encrypts, pays
        └─ paid CVM server (npub-addressed; kind 25910 JSON-RPC)
              └─ Transport interface → the SMS rail
```

## Boundary contract

| Rule | How this napplet complies |
|---|---|
| One hard capability | `requires: ['cvm']` — everything else optional |
| No keys, no tokens | payment runs through the shell's CEP-8 flow; the napplet never sees a token |
| No relays, no fetch, no WebSocket | every server interaction is `cvm.callTool` |
| No `localStorage` | nothing persisted at all |
| Optional domains degrade | theme is feature-detected; a partial shell yields a reason, never a crash |
| Boot cannot break | boot-time work is wrapped and degrades visibly |

## The contract cannot drift

`llms.txt` and `llms-full.txt` are bundled at build time (`?raw` imports) **and**
the live price and capability flags are read from the server. The "best effort,
no delivery receipt" warning is rendered *from the server's own flags* — there is
no place in this source that hardcodes `delivery_receipts: false`.

The price table is generated from the Python service's own table, so the bundled
fallback and the service cannot disagree:

```bash
python3 ../scripts/export_contract.py          # regenerate
python3 ../scripts/export_contract.py --check  # fail if stale
```

## Tools the napplet calls

- `sms.capabilities` — rail name + flags (free)
- `sms.pricing` — live per-destination price (free)
- `sms.send(to, body)` — PAID (CEP-8 `explicit_gating`)
- `sms.status(id)` — free
- `docs` — returns the `llms.txt` contract verbatim (free)

## Verify

```bash
pnpm install
pnpm verify            # unit tests + type-check + build
pnpm test:conformance  # real allow-scripts iframe, boundary contract
napplet deploy --dry-run
```

## Run in a shell

```bash
pnpm paja   # kehto paja --target-url http://127.0.0.1:5173 -- pnpm vite --host 127.0.0.1
```

Open the **Paja runtime URL** Paja prints, not the Vite target URL. Paja's
bundled host implements NAP-CVM only when the relay is live
(`getSimulation().cvm.enabled && getSimulation().relay.mode === "live"`), so use
the default live relay mode and approve the ACL prompt for `cvm:call`.
