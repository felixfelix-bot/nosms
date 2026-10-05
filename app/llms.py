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
    # The price is rendered from the SAME module `sms.pricing` and the CEP-8
    # `cap` tag read, so the contract's printed default cannot drift from what
    # the service meters with (ADR-0002 / CEP draft 0001 P4).
    from .pricing import (
        DEFAULT_PRICE_SATS,
        PRICE_ROUNDING_SATS,
        RAIL_REPLACEMENT_USD,
        RISK_MULTIPLIER,
    )

    PRICE = DEFAULT_PRICE_SATS
    ROUNDING = PRICE_ROUNDING_SATS
    MULT = RISK_MULTIPLIER
    RAIL_USD = RAIL_REPLACEMENT_USD
    SATS_PER_USD = round(100_000_000 / 86462, 2)  # at the recorded BTCUSD quote
    return f"""# nosms — SMS for nostr keys (cashu.email parity)

Service: {cfg.service}
Version: {cfg.version}
Transport: {cfg.transport}

Send a text message to any phone number. Your nostr key is your identity; you
pay sats per message, postage-style. No account, no API key, no KYC.

## Endpoints

- `GET  /api/health`   -> JSON {{service, version, commit, transport, ok}}. Open, cheap, no outbound calls.
- `POST /api/send`     -> send one SMS. Requires NIP-98 auth. Body: {{"to": "<E.164>", "body": "..."}}.
- `GET  /llms.txt`     -> this document.
- `GET  /llms-full.txt`-> 501 stub in M1a (full manual ships later).

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

## Pricing (sats, per message)

Prices are charged in satoshis. The unit is sats — there is no fiat billing.

**One flat price, domestic and international alike: {PRICE} sats per message.**
The v1 rail is a single personal JMP/Cheogram line whose plan is unlimited
including international, so the destination does not change the cost and a
per-country price would advertise a distinction the rail does not have
(ADR-0002).

The price is derived, not a magic number:

    price_sats = ceil(MULT * rail_replacement_usd * sats_per_usd), rounded up to {ROUNDING}
    = ceil({MULT} * {RAIL_USD} * {SATS_PER_USD}) = {PRICE} sats

`MULT` is the operator's abuse premium and walks down as the abuse record stays
clean; `rail_replacement_usd` is the monthly cost of replacing the rail. Treat
the number here as a default and read the live one from the server.

`GET /api/health` and `GET /llms.txt` are free. `POST /api/send` costs the price
above, per message, and never sends before payment lands.

## Discovery — the CEP-6 tags a client sees

The service announces itself as a ContextVM (CEP-6) catalog: kind `11316`
(server announcement) and `11317` (tools), both replaceable and both carrying the
same discoverable tag surface. These are the tags a client — and the registry
dashboard — actually reads:

    ["d","nosms"]                             stable instance id (P1)
    ["t","cvm:service:sms"]                   namespaced service class (P2 MUST)
    ["t","sms"] ["t","contextvm"]             plain human words (P2 SHOULD)
    ["t","cvm:req:payment.amount"]            declared flow input: money, nothing else (P15)
    ["t","cvm:req:none"]                      P15 sentinel: no personal data is required
    ["t","cvm:tier:financial"]                the recomputed max tier (P15 / D14)
    ["cap","tool:sms.send","{PRICE}","sats"]  CEP-8 price for the one paid tool
    ["r","https://nosms.orangesync.tech/llms.txt"]   docs link (P2 SHOULD)
    ["pmi","bitcoin-cashu","explicit_gating"]        CEP-8 payment method + gating
    ["name","nosms"] ["about",…] ["website",…]       payload only, not filterable (D2)

What that means for a client, in plain terms:

- **Only `d`, `r` and `t` are filterable.** Everything else is payload; do not
  try to filter a relay on `cap`, `pmi`, `name` or `about` (D2).
- **No geohash.** This service has no fixed location, and the spec forbids
  publishing a meaningless `["g",…]` (P2).
- **The declared appetite is money and nothing else.** `to` and `body` are tool
  arguments, not fields a user is asked for, so they are not declared. The
  recomputed tier is therefore `financial` (rank 1), and the `cvm:req:none`
  sentinel rides along because rank <= financial means "no personal data".
- **One `cap`, for one tool.** `sms.send` is the only tool that costs money; the
  free tools carry no `cap`, because a price on a free tool would be a lie (P4).
- The tier tag is a cache of the declared fields. If a reader ever finds them
  disagreeing, the **fields win** — use the recomputed value and surface the
  mismatch (ADR-0001 D14).

## Error contract

Every 4xx/5xx response carries two headers:

- `X-Reason` — short machine token to branch on.
- `X-Hint` — one human-readable sentence.

Tokens you can rely on: `auth_missing`, `auth_invalid`, `auth_expired`,
`auth_replayed`, `auth_url_mismatch`, `auth_method_mismatch`,
`auth_payload_mismatch`, `bad_destination`, `invalid_request`, `not_found`,
`method_not_allowed`, `send_not_implemented`, `transport_error`,
`insufficient_funds`, `internal_error`.

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
