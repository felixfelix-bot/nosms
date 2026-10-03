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

- `+1` (US/CA): 100 sats
- `+49`, `+44`, `+351`, `+91`: 500 sats
- any other prefix: 500 sats (documented default; unknown is never free)

`GET /api/health` is free. `POST /api/send` costs the price above, per message.

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
