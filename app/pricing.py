"""Prefix pricing for outbound SMS.

The table is data, not logic: a destination's price is the value of the longest
matching prefix, or the documented default when no prefix matches - an unknown
destination is **never** free (PLAN section 1: 100 sats domestic-parity, 500 sats
international).

`price_for` is deliberately isolated from the HTTP layer so it is loadable and
testable on its own, and so the M1b send path can reuse it unchanged.
"""
from __future__ import annotations

import re

#: Prefix -> price in sats. Longest prefix wins.
PREFIX_PRICES: dict[str, int] = {
    "+1": 100,    # US / Canada - domestic parity tier
    "+49": 500,   # Germany
    "+44": 500,   # United Kingdom
    "+351": 500,  # Portugal
    "+91": 500,   # India
}

#: Documented fallback for any destination not covered by the table.
DEFAULT_PRICE: int = 500

REASON = "bad_destination"
HINT = "Send an E.164 destination such as +14155551234 (a leading + plus 7-15 digits)."

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class BadDestination(ValueError):
    """The destination is not a usable E.164 phone number."""

    reason = REASON
    hint = HINT

    def __init__(self, message: str = "destination is not valid E.164"):
        super().__init__(message)


def normalize_e164(dest: object) -> str:
    """Return a validated E.164 number or raise :class:`BadDestination`."""
    if not isinstance(dest, str):
        raise BadDestination("destination must be a string in E.164 form")
    candidate = dest.strip()
    if not _E164.match(candidate):
        raise BadDestination(f"{candidate!r} is not a valid E.164 number")
    return candidate


def price_for(dest: object) -> int:
    """Price one SMS to ``dest`` in sats. Raises :class:`BadDestination`."""
    number = normalize_e164(dest)
    best: tuple[str, int] | None = None
    for prefix, price in PREFIX_PRICES.items():
        if number.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, price)
    return best[1] if best is not None else DEFAULT_PRICE


def describe_table() -> dict:
    """Machine-readable price list for ``GET /api/pricing`` and ``llms.txt``."""
    return {
        "unit": "sats",
        "currency": "sats",
        "default": DEFAULT_PRICE,
        "prefixes": dict(PREFIX_PRICES),
        "daily_limit": 100,
        "per_destination_cooldown_seconds": 60,
        "notes": "Longest matching prefix wins; unknown prefixes pay the default.",
    }
