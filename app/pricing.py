"""Flat postage pricing for SMS destinations.

Source of truth: ``docs/adr/0002-jmp-rail-override-and-risk-pricing.md`` and
``PLAN.md`` §12. The v1 rail is a single personal JMP/Cheogram line, whose plan is
unlimited including international — so **destination no longer maps to cost** and
the earlier +1 = 100 / international = 500 split is superseded.

The price is a *rugpull-risk premium*: revenue from a handful of sends covers the
cost of the rail being terminated (the risk ADR-0002 accepts on purpose).

    price_sats = ceil(MULT * rail_replacement_usd * sats_per_usd)
    sats_per_usd = 1e8 / btc_usd

With ``MULT = 0.5``, ``rail_replacement_usd = 4.99`` and BTC at 86,462 USD that is
``ceil(0.5 * 4.99 * 1156.6) = 2886`` sats, published as the documented default
``2900`` sats. The walk-down (MULT -0.05 per 100 clean sends, reset on abuse,
floor ``max(rail marginal cost, 1000 sats)``) lives in the ADR; the floor is
enforced here so no caller can quote below it.

There is no code path that returns 0: an unknown destination is never free.
"""
from __future__ import annotations

import math
import re

#: operator multiplier on the rail's replacement cost (ADR-0002 §Pricing).
MULT = 0.5

#: monthly cost of replacing the rail: JMP plan, $4.99/mo, unlimited incl. intl.
RAIL_REPLACEMENT_USD = 4.99

#: hard floor from the ADR: never quote below max(rail marginal cost, 1000 sats).
PRICE_FLOOR_SATS = 1000

#: BTC/USD used for the published default (Binance BTCUSDT, 2026-10-05).
BTC_USD_DEFAULT = 86_462.0

#: sats per USD: 1e8 sats per whole BTC.
SATOSHIS_PER_BTC = 100_000_000


def sats_per_usd(btc_usd: float) -> float:
    """Sats in one US dollar at the given BTC price."""
    if btc_usd <= 0:
        raise ValueError("btc_usd must be positive")
    return SATOSHIS_PER_BTC / float(btc_usd)


def quote_sats(btc_usd: float | None = None, *, mult: float = MULT,
               rail_replacement_usd: float = RAIL_REPLACEMENT_USD) -> int:
    """Price one SMS in sats per the ADR-0002 formula, never below the floor.

    ``btc_usd=None`` uses the published default rate, so the service can quote
    offline without inventing a live number; pass a live rate to quote from it.
    """
    rate = BTC_USD_DEFAULT if btc_usd is None else float(btc_usd)
    quoted = math.ceil(mult * rail_replacement_usd * sats_per_usd(rate))
    return max(int(quoted), PRICE_FLOOR_SATS)


#: the documented default price: ADR-0002's "rounded 2,900 sats", flat for every
#: destination. Kept explicit (not derived at import) so a rate change cannot
#: silently move the advertised number; ``tests/test_pricing.py`` pins it to the
#: formula at the documented rate.
FLAT_PRICE_SATS = 2900

#: price charged for every destination, known prefix or not. Never zero.
DEFAULT_PRICE_SATS = FLAT_PRICE_SATS

#: destinations the v1 rail will attempt. The rail refuses anything else; price
#: is flat, so this table exists for capability reporting, not for pricing.
COUNTRIES = ["US", "CA"]

#: shortest plausible E.164 national number, +CC included. Shorter input cannot
#: be a phone number, so it is rejected rather than silently priced.
MIN_E164_LEN = 8


class InvalidDestination(Exception):
    reason = "bad_destination"
    hint = "Destination must be an E.164 phone number, e.g. +141****0100."

    def __init__(self, hint: str | None = None):
        self.hint = hint or type(self).hint
        super().__init__(self.hint)


def normalize_e164(dest: str) -> str:
    """Strip formatting; return '+<digits>'. Raise on input with no digits."""
    digits = re.sub(r"\D", "", dest or "")
    if not digits:
        raise InvalidDestination()
    return f"+{digits}"


def country_of(e164: str) -> str | None:
    """'US'/'CA' for a +1 number, else None (the rail only covers US/CA)."""
    number = normalize_e164(e164)
    return "US" if number.startswith("+1") else None


def price_for(e164: str) -> int:
    """Sat price for one SMS to `e164`. Flat, and never 0.

    The destination is still validated: a value with no digits raises
    :class:`InvalidDestination` rather than being priced at the default.
    """
    number = normalize_e164(e164)
    if len(number) < MIN_E164_LEN:
        raise InvalidDestination(
            f"'{e164}' is too short to be an E.164 number (need at least "
            f"{MIN_E164_LEN} characters including the country code).")
    return DEFAULT_PRICE_SATS
