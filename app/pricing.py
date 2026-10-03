"""Prefix pricing for SMS destinations.

One table, loaded in isolation and unit-testable without the app. Prices are in
sats and are the postage the sender pays for one message.

A destination whose prefix is not in the table — or which is too short to be a
plausible E.164 number — is priced at the DOCUMENTED default. Unknown must
never mean free: there is no code path that returns 0.
"""
from __future__ import annotations

import re

#: destination prefix -> sat price for one SMS.
#: +1 is domestic (US/CA) parity at 100 sats; the rest are international at 500.
PREFIX_PRICES: dict[str, int] = {
    "+1": 100,
    "+49": 500,
    "+44": 500,
    "+351": 500,
    "+91": 500,
}

#: price charged when the prefix is unknown (documented, never zero).
DEFAULT_PRICE_SATS = 500

#: shortest plausible E.164 national number, +CC included. Shorter input cannot
#: be prefix-matched reliably, so it takes the default rather than a wrong price.
MIN_E164_LEN = 8

_PREFIXES_BY_LENGTH = sorted(PREFIX_PRICES, key=len, reverse=True)


class InvalidDestination(Exception):
    reason = "bad_destination"
    hint = "Destination must be an E.164 phone number, e.g. +14155550100."

    def __init__(self, hint: str | None = None):
        self.hint = hint or type(self).hint
        super().__init__(self.hint)


def normalize_e164(dest: str) -> str:
    """Strip formatting; return '+<digits>'. Raise on input with no digits."""
    digits = re.sub(r"\D", "", dest or "")
    if not digits:
        raise InvalidDestination()
    return f"+{digits}"


def price_for(e164: str) -> int:
    """Sat price for one SMS to `e164`. Never returns 0."""
    number = normalize_e164(e164)
    if len(number) < MIN_E164_LEN:
        return DEFAULT_PRICE_SATS
    for prefix in _PREFIXES_BY_LENGTH:
        if number.startswith(prefix):
            return PREFIX_PRICES[prefix]
    return DEFAULT_PRICE_SATS
