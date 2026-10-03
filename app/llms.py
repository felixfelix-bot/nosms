"""Agent-facing documentation served at /llms.txt.

The base64 credential example is derived from :data:`NIP98_EXAMPLE_JSON` at import
time so that the *signature in the documentation is real* - the example is a
genuinely signed kind-27235 event, not a decorative string. Its ``created_at`` is a
fixed past timestamp (documented below), so a caller must re-sign before use; the
documentation says that out loud rather than shipping an example that quietly
cannot work.
"""
from __future__ import annotations

import base64

from .pricing import DEFAULT_PRICE, PREFIX_PRICES

#: A real, signed kind-27235 event used as the copy-pasteable example.
NIP98_EXAMPLE_JSON = (
    '{"id":"a8debd9cbd514a409b23e48930ef1d2c0302601cb6e3a7d3367d7df5b4845705",'
    '"pubkey":"f76a39d05686e34a4420897e359371836145dd3973e3982568b60f8433adde6e",'  # gitleaks:allow (public key material, not a credential)
    '"created_at":1760000000,"kind":27235,'
    '"tags":[["u","https://nosms.orangesync.tech/api/send"],["method","POST"]],'
    '"content":"",'
    '"sig":"464d5ddb8719c1b77afed393a0efb3649e701dd2470002418b7e67c8c071fb3588adb4e28517ac53582a3910358b66d2a7195e0de4f8ff06b62217d58cd92c16"}'
)

NIP98_EXAMPLE_B64 = base64.b64encode(NIP98_EXAMPLE_JSON.encode("utf-8")).decode("ascii")

_PRICE_LINES = "\n".join(
    f"  {prefix:<6} {price} sats" for prefix, price in PREFIX_PRICES.items()
)

LLMS_FULL_TXT_STUB = (
    "# nosms - full agent documentation\n"
    "Not published yet (M1a ships the short surface only).\n"
    "See /llms.txt for endpoints, auth, pricing and the error contract.\n"
)

_TEMPLATE = """# nosms - SMS for nostr keys

Send an SMS paid in sats, authenticated with your nostr key. No account, no API
key, no KYC: the same posture as cashu.email, applied to SMS.

Base URL: https://nosms.orangesync.tech
Status: M1a (auth, health, pricing, errors, docs). The send path ships in M1b.

## Auth (NIP-98, kind 27235)

Every call to /api/send must carry a signed kind-27235 event in the request
header. GET /api/health, /api/pricing, /llms.txt and /llms-full.txt are open.

The event must satisfy all of:

- kind == 27235
- a "u" tag equal to the request URL (scheme + host + path; query ignored)
- a "method" tag equal to the HTTP method
- created_at within 120 seconds of server time
- an event id used at most once (replayed ids are rejected)

Copy-pasteable example (this is a real signature, but created_at is a fixed past
timestamp - re-sign with a fresh created_at before calling):

Authorization: Nostr __NIP98_B64__

The same event as JSON, decoded from that base64:

__NIP98_JSON__

Element 0 of the signed array is 0, then pubkey, created_at, kind, tags, content.

## Endpoints

- GET  /api/health          liveness: service version, build commit, configured
                            transport name, and ok:true. No outbound calls.
- GET  /api/pricing         the prefix price table and the daily limit.
- POST /api/send            (auth) send one SMS. 501 in M1a: the price is quoted,
                            nothing is sent and no funds move.
- GET  /api/message/:id/status  (auth) queued | sent | delivered | failed, plus the
                            rail's raw status string. The configured rail is
                            best-effort and cannot observe delivery - a
                            "delivered" claim is only made when the rail itself
                            reports it.
- GET  /llms.txt            this document.
- GET  /llms-full.txt       501 stub until the extended documentation lands.

POST /api/send body: {"to": "+<E164>", "text": "<message>"}

## Pricing

Prices are in sats, charged per SMS, from the destination's longest matching
prefix; a destination that matches no prefix pays the default. The price is always
shown BEFORE anything is sent, and an unknown prefix is never free.

__PRICE_LINES__
  default price: __DEFAULT_PRICE__ sats

Daily limit: 100 sends per identity. Per-destination cooldown: 60 seconds. Both are
server-side and both are enforced before a token is escrowed.

## Errors

Every 4xx/5xx carries two headers:

- X-Reason: a short machine token
- X-Hint:   one sentence telling you what to do next

Tokens you can rely on:

  auth_missing        no Authorization header on an auth-required route
  auth_malformed      header present but not base64 JSON of a kind-27235 event
  auth_invalid        signature, kind, "u" tag or "method" tag does not match
  auth_stale          created_at outside the 120s freshness window
  auth_replay         this event id was already used
  bad_destination     "to" is not E.164, e.g. +14155551234
  validation_error    request body is not {"to": ..., "text": ...}
  insufficient_funds  the Cashu token is worth less than the quoted price
  transport_error     the rail failed after accepting; poll the status endpoint
  not_found           no such route
  not_implemented     endpoint lands in a later milestone
  internal_error      unexpected failure; nothing was sent, no funds moved

Error bodies are JSON: {"error": {"reason": "...", "hint": "..."}}.

## Data handling

Message bodies are NIP-44 encrypted client-to-gateway and are never stored in
plaintext at the service. The data retained is the destination number, the sats
charged and the rail's status, kept for accounting and refunds. The rail currently in
configuration is the email-to-SMS carrier gateway, which is best-effort: it
returns no delivery receipt, and this service never claims one on its behalf.
"""

LLMS_TXT = (
    _TEMPLATE
    .replace("__NIP98_B64__", NIP98_EXAMPLE_B64)
    .replace("__NIP98_JSON__", NIP98_EXAMPLE_JSON)
    .replace("__PRICE_LINES__", _PRICE_LINES)
    .replace("__DEFAULT_PRICE__", str(DEFAULT_PRICE))
)
