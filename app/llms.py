"""Agent-facing documentation served at /llms.txt (cashu.email parity).

Rendered from the live Config so the advertised surface cannot drift from the
running service. `llms-full.txt` is a deliberate 501 stub for M1a.
"""
from __future__ import annotations

import base64
import json

#: A real, valid kind-27235 event (from the NIP-98 spec) used for the
#: copy-pasteable example. `created_at` is from the spec; treat it as a sample.
EXAMPLE_EVENT = {
    "id": "fe964e758903360f28d8424d092da8494ed207cba823110be3a57dfe4b578734",
    "pubkey": "63fe6318dc58583cfe16810f86dd09e18bfd76aabc24a0081ce2856f330504ed",
    "content": "",
    "kind": 27235,
    "created_at": 1682327852,
    "tags": [
        ["u", "https://nosms.orangesync.tech/api/send"],
        ["method", "POST"],
    ],
    "sig": ("5ed9d8ec958bc854f997bdc24ac337d005af372324747efe4a00e24f4c30437f"
            "f4dd8308684bed467d9d6be3e5a517bb43b1732cc7d33949a3aaf86705c22184"),
}


def example_header() -> str:
    raw = json.dumps(EXAMPLE_EVENT, separators=(",", ":")).encode("utf-8")
    return "Nostr " + base64.b64encode(raw).decode("ascii")


def llms_txt(cfg) -> str:
    header = example_header()
    return f"""# nosms — SMS for nostr keys (cashu.email parity)

Service: {cfg.service}
Version: {cfg.version}
Transport: {cfg.transport}

Send a text message to any phone number. Your nostr key is your identity; you
pay sats per message, postage-style. No account, no API key, no KYC.

## Two surfaces, one contract

| Surface | Who uses it | How |
|---|---|---|
| HTTP (this document) | scripts, `curl`, LLMs with network access | NIP-98-authenticated `POST /api/send` |
| ContextVM (MCP over Nostr) | agents, in-shell clients | MCP JSON-RPC on kind `25910`, addressed by the server npub |

The CVM surface has its own machine contract, reachable from the server itself
via its `docs` tool: `docs/cvm/llms.txt` in this repo, and
`{getattr(cfg, "cvm_contract_url", "https://nosms.orangesync.tech/cvm/llms.txt")}`
at runtime. Both surfaces share one price table and one `Transport`, so the same
numbers and the same capability flags apply to both.

## Paying — accepted rails

| Rail | Direction | Status |
|---|---|---|
| Cashu token (escrow, testnut mint) | in | live |
| Lightning (bolt11 invoice, or LNURL-pay) | in | sats modules implemented + tested; wired at M1 |
| **Machankura (`8333.mobi`) lightning address** | in + **refund out** | sats modules implemented + tested; wired at M1 |

### Machankura / `8333.mobi` lightning addresses

Machankura is a Bitcoin/Lightning wallet reachable from any phone over USSD
shortcodes (`*483*8333#` Kenya, `*347*8333#` Nigeria, `*384*8333#`
Malawi/Zambia, `*142*8333#` Namibia, `*9141#` Côte d'Ivoire, `*920*8333#`
Ghana, `54052.co.za` South Africa) and SMS long numbers (Tanzania
`+255 679 066 977`, Uganda `+256 744 830 624`). No app, no internet.

Every Machankura user therefore has a **lightning address**:

    <phone in international format, no plus>@8333.mobi      e.g. 255679066977@8333.mobi
    <username>@8333.mobi                                    e.g. sigidli@8333.mobi

which resolves through the standard LNURL-pay endpoint:

    GET https://8333.mobi/.well-known/lnurlp/<identifier>

**This is the rail for people who have a handset but no internet and no
email.** They can pay us in sats from their keypad, and we can push a sat
refund back to their phone number instead of only issuing a Cashu token.

nosms accepts a `8333.mobi` address in two places:

1. **As the payer's identity for an order.** The address is how we know who
   paid and, if the message cannot be delivered, where the postage goes back.
   Paying is done by the payer from their own handset (they receive a bolt11
   invoice or a lightning address through SMS/USSD reachable rails; they
   cannot open a URL, so never send them one).
2. **As a refund destination.** On non-delivery (T+{getattr(cfg, "refund_after_seconds", 900) // 60} min) the escrow is
   refunded to the payer's lightning address — Machankura included.

`commentAllowed` on Machankura is `60`, so a payer can attach a short comment
to a payment. For a USSD/SMS user that comment is the only data channel we
have; nosms uses it for an order reference. A comment longer than the
advertised limit is **refused before the request is sent**, never silently
truncated — a silently dropped reference code is worse than a loud failure.
Operational refund reasons are the exception: they are truncated to fit, since
failing a refund over a long sentence would strand the payer's sats.

#### Read this before relying on a Machankura refund

- **A sat refund to a phone number is irreversible.** It settles to whoever
  controls that SIM at that moment. There is no chargeback, no reversal, no
  support line that can pull sats back.
- **The address is only valid while that user is on Machankura.** An address
  that resolved yesterday can answer `404 {{"status":"error","reason":"User
  <x> doesn't use the Machankura service."}}` today. nosms therefore resolves
  the address **immediately before paying** and never pays a cached invoice.
- **Refunds are idempotent.** One refund key settles at most once: a re-run
  (retry, crash recovery, operator re-click) replays the recorded receipt and
  does not move sats again. The `8333.mobi` endpoint is the only Machankura
  surface nosms depends on — there is no public HTTP send API (`/api` → 404)
  and no account.
- Refund policy is otherwise unchanged: postage is refunded when the message
  is not delivered, and the refund goes back to the rail the payer used.

## Endpoints

- `GET  /api/health`   -> JSON {{service, version, commit, transport, mint, ok}}. Open, cheap, no outbound calls.
- `POST /api/send`     -> send one SMS. Requires NIP-98 auth and a Cashu postage token. Body: {{"to": "<E.164>", "text": "..."}}.
- `GET  /api/message/<message_id>/status` -> queued / sent / delivered / failed, plus the rail's own raw status string. Requires NIP-98 auth as the paying key.
- `GET  /api/refund/<message_id>`         -> the refund token for an undelivered message. Requires NIP-98 auth as the paying key.
- `GET  /llms.txt`     -> this document.
- `GET  /llms-full.txt`-> 501 stub (full manual ships later).

## Authentication — NIP-98 (kind 27235)

Every `POST /api/send` MUST carry a signed NIP-98 event: the header value is the
scheme `Nostr`, a space, then the event JSON base64-encoded.

    Authorization: Nostr (base64 of the event JSON below)

The event is a normal nostr event of kind `27235` with `content` empty and two
MUST tags:

- `["u", "<absolute request URL>"]` — scheme + host + path must match; query
  params signed in the event must also be present on the request.
- `["method", "<HTTP method>"]` — must match the HTTP method, e.g. `POST`.
- `["payload", "<sha256(body) hex>"]` — SHOULD be included for POST bodies.

The server verifies the event id, the BIP-340 signature, `kind == 27235`, a
freshness window of +/- {cfg.freshness_seconds} seconds on `created_at`, and
rejects replayed event ids for {cfg.replay_ttl_seconds} seconds.

Copy-pasteable example header (sample event; re-sign with your own key and a
current `created_at`):

    Authorization: {header}

The same event as JSON:

    {json.dumps(EXAMPLE_EVENT, separators=(",", ":"))}

## Sending — `POST /api/send`

Body (JSON):

    {{"to": "+4915112345678", "text": "hello"}}

`to` MUST be an E.164 destination (`+` and digits). `text` is the message; the
alias `body` is accepted for compatibility. The postage token travels in a
header, not in the body — it is never logged with the message:

    X-Cashu: cashuAeyJ0b2tlbiI6W3sibWludCI6...     (v3, base64url JSON)
    X-Cashu: cashuBo2F0gaJhaVghAYQjfmPONCPffb...   (v4, base64url CBOR)

Both token encodings are accepted. The token's mint MUST be
`{getattr(cfg, "mint_url", "https://testnut.cashu.space")}` — postage is escrowed there and nowhere else.

What happens, in order:

1. The destination is priced from the flat ADR-0002 price below (one price for
   every destination — JMP's plan is unlimited including international).
2. Every proof in the token is checked against the mint (NUT-07); a spent proof
   is `409 token_already_spent`.
3. The mint's own input fee (0.1 sat per proof on testnut) is deducted from the
   amount the token carries; that fee is yours, postage is ours. If what is left
   is below the price the request is refused with `402 insufficient_funds` and
   `X-Hint` names the shortfall in sats.
4. The proofs are **swapped at the mint** for fresh ones this service holds —
   that is the escrow. Your original token can no longer be spent, which is the
   only sense in which holding a token is escrow at all.
5. The message goes to the rail. On success you get `200` with:

       {{"message_id": "m_…", "price_sats": {getattr(cfg, "price_sats", 2900)}, "change_sats": 28,
         "status": "queued", "status_url": "/api/message/m_…/status",
         "refund_url": "/api/refund/m_…",
         "escrow": {{"mint": "…", "amount_sats": {getattr(cfg, "price_sats", 2900)}}}}}

**`price_sats` is {getattr(cfg, "price_sats", 2900)} for every destination.**

**`queued` is not `delivered`.** Poll `status_url`; the reply carries both the
normalised `status` (queued / sent / delivered / failed) and `provider_status`,
the rail's own unmodified string. This service never upgrades a status the rail
did not report: if the rail has no delivery receipts, the status never says
"delivered".

### Refunds

If a message is still not delivered at T+{getattr(cfg, "refund_after_seconds", 900) // 60} minutes, the postage *and* the
unspent change go back to the paying key: `GET status_url` then shows
`"refunded": true` with a `refund` block, and `GET /api/refund/<message_id>`
returns the bearer token for that amount. Refunds are claimed atomically, so a
sweep that runs twice cannot pay out twice. A rail that cannot observe delivery
is never auto-refunded — silence is not proof of non-delivery.

### Limits

Config-driven, per key:

- per-destination cooldown: {getattr(cfg, "destination_cooldown_seconds", 60)} s → `429 destination_cooldown`
- daily cap: {getattr(cfg, "daily_cap", 100)} messages / rolling 24 h → `429 daily_cap_reached`

**The rail is a second, independent gate.** Every rail rides a *personal* line,
so volume is the abuse surface: a persisted daily cap plus a jittered minimum
gap, enforced by the rail itself, against the rail's own counter.

- the rail is pacing → `429` `rail_paced` **and a `Retry-After` header**. The
  message was never attempted (no slot was consumed), so the postage is refunded
  and you come back after the indicated wait.
- the rail has stopped (a detected ban / a terminated or unregistered line) →
  `503` `rail_unavailable`. There is no `Retry-After`: this is not a "come back
  soon", it is a rail that a human has to clear. The postage is refunded.

Both are refund events — nothing was sent — and neither is ever charged for.

## Pricing (sats, per message)

Prices are charged in satoshis. The unit is sats — there is no fiat billing.

**Flat: {getattr(cfg, "price_sats", 2900)} sats per message, for every destination** — domestic
and international alike. The v1 rail's plan is unlimited *including*
international, so the destination does not change the cost; the old
domestic/international split is superseded (ADR-0002).

The price is a rugpull-risk premium, not a cost pass-through:

```
price_sats = ceil(MULT * rail_replacement_usd * (1e8 / btc_usd))
```

with `MULT = 0.5`, `rail_replacement_usd = 4.99`, and `btc_usd` live from Binance
`BTCUSDT`. The published figure ({getattr(cfg, "price_sats", 2900)}) is the rounded default; the raw
formula value is 2886. There is a hard floor of 1000 sats.

An unknown prefix is never free — there is no code path that returns 0.

`GET /api/health` is free. `POST /api/send` costs the price above, per message.

## Error contract

Every 4xx/5xx response carries two headers:

- `X-Reason` — short machine token to branch on.
- `X-Hint` — one human-readable sentence.

Tokens you can rely on: `bad_request`, `auth_missing`, `auth_invalid`,
`auth_expired`, `auth_replayed`, `auth_url_mismatch`, `auth_method_mismatch`,
`auth_payload_mismatch`, `bad_destination`, `empty_body`, `body_too_long`,
`token_missing`, `token_invalid`, `token_unsupported`, `token_empty`,
`token_wrong_mint`, `token_already_spent`, `token_state_unknown`,
`insufficient_funds`, `destination_cooldown`, `daily_cap_reached`,
`rail_paced`, `rail_unavailable`,
`mint_error`, `mint_unreachable`, `transport_error`, `not_refunded`,
`invalid_request`, `not_found`, `method_not_allowed`,
`llms_full_not_implemented`, `internal_error`.

A bare `401` is never returned; always read `X-Reason`.

## Data handling

Message bodies are NIP-44-encrypted client-to-gateway and are never stored in
plaintext at the service. The destination number and the send outcome are kept
only as long as needed to meter postage and settle refunds. The v1 rail has no
delivery receipts: a send it accepts is reported as accepted, never as
"delivered".
"""


def llms_full_txt() -> None:
    """M1a ships only the stub; the full manual is a later milestone."""
    return None
