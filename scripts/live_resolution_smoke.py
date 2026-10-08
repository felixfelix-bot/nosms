#!/usr/bin/env python3
"""Live resolution smoke test for the 8333.mobi (Machankura) LNURL endpoint.

READ-ONLY BY CONSTRUCTION. This script only performs the discovery GET
(`/.well-known/lnurlp/<identifier>`). It never calls a payRequest `callback`,
so it cannot mint an invoice, and it holds no wallet — no sats can move.

Usage::

    python3 scripts/live_resolution_smoke.py                        # the two defaults
    python3 scripts/live_resolution_smoke.py sigidli@8333.mobi

Defaults are the pair the integration card asks for: one real username and one
phone number that (as of 2026-09-27) is not a Machankura user. Do not point
this at other people's addresses: enumeration of a phone space is not a bug
report, it is a sweep.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nosms.lnurl_pay import (  # noqa: E402
    AmountOutOfRange,
    CommentTooLong,
    LnurlError,
    UnknownUser,
    UrllibTransport,
    invoice_callback_url,
    resolve,
)

DEFAULT_TARGETS = ["sigidli@8333.mobi", "254700000000@8333.mobi"]


def probe(identifier: str, transport: UrllibTransport) -> int:
    print(f"--- resolve {identifier}")
    try:
        pay_request = resolve(identifier, transport=transport)
    except UnknownUser as error:
        print(f"    HTTP 404 -> UnknownUser: {error.reason}")
        return 0
    except LnurlError as error:
        print(f"    FAILED ({type(error).__name__}): {error}")
        return 1

    print(f"    callback         = {pay_request.callback}")
    print(f"    min/max msat     = {pay_request.min_sendable_msat} / {pay_request.max_sendable_msat}"
          f"  ({pay_request.min_sendable_sats:g} / {pay_request.max_sendable_sats:g} sats)")
    print(f"    commentAllowed   = {pay_request.comment_allowed}")
    print(f"    allowsNostr      = {pay_request.allows_nostr} (nostrPubkey {pay_request.nostr_pubkey})")
    print(f"    tag              = {pay_request.tag}")
    print(f"    metadata[0]      = {pay_request.metadata_entries()[:1]}")

    # Local-only assertions: no callback is called, so nothing is minted.
    try:
        pay_request.check_amount(pay_request.min_sendable_msat - 1)
    except AmountOutOfRange:
        print("    range check      = rejects min-1 msat (local, no HTTP)")
    else:  # pragma: no cover - would be a bug in the range check
        print("    range check      = FAILED to reject an out-of-range amount")
        return 1
    if pay_request.comment_allowed:
        try:
            pay_request.check_comment("x" * (pay_request.comment_allowed + 1))
        except CommentTooLong:
            print(f"    comment check    = refuses {pay_request.comment_allowed + 1} chars (local, no HTTP)")
        else:  # pragma: no cover - would be a bug in the comment check
            print("    comment check    = FAILED to refuse an over-long comment")
            return 1
    print(f"    (callback would be {invoice_callback_url(pay_request, pay_request.min_sendable_msat)}"
          " — NOT called)")
    return 0


def main(argv: list[str]) -> int:
    targets = argv[1:] or DEFAULT_TARGETS
    transport = UrllibTransport()
    failures = sum(probe(target, transport) for target in targets)
    print(f"=== {len(targets)} probe(s), {failures} failure(s), 0 invoices requested, 0 sats moved")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
