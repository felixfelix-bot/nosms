"""SMS pricing — one flat, risk-premium price per message.

ADR-0002 (``docs/adr/0002-jmp-rail-override-and-risk-pricing.md``) supersedes the
old per-prefix table (100 domestic / 500 international). The v1 rail is a single
JMP/Cheogram line whose plan is **unlimited including international**, so the
destination prefix no longer maps to cost, and a per-prefix price would advertise
a distinction the rail does not have.

The price is derived from the rail's replacement cost and a deliberate abuse
premium, not from what the destination costs us:

    price_sats = ceil(MULT * rail_replacement_usd * sats_per_usd)

    MULT=0.5 -> "half as costly as the sim card" (operator directive, 2026-10-05)
    rail_replacement_usd=4.99 -> JMP plan, $/mo, unlimited in+out incl. intl
    sats_per_usd -> 1e8 / BTCUSD, read live by the caller; 86462 on 2026-10-05

    0.5 * 4.99 * (1e8/86462) = 2886 -> 2900 sats (rounded up to 100)

Everything a caller can observe still holds, exactly as before ADR-0002:

* an unknown or too-short destination is priced at the DOCUMENTED price, never
  free — there is no code path that returns 0;
* a destination with no digits raises a machine-readable ``InvalidDestination``.

The advertised ``cap`` tag is this same number (``CvmTools.capability_tags``), so
the invoice can never exceed what was advertised (CEP draft 0001 P4).
"""
from __future__ import annotations

import math
import re

#: ADR-0002 inputs. Kept as named constants so the derivation is auditable.
RISK_MULTIPLIER = 0.5            # operator: "half as costly as the sim card"
RAIL_REPLACEMENT_USD = 4.99      # JMP plan price, $/mo
PRICE_ROUNDING_SATS = 100        # round the quote UP to a round number

#: Fallback when no live BTCUSD quote is available.
#:
#: This is the ADR-0002 number computed at BTCUSD=86462 (2026-10-05):
#:   ceil(0.5 * 4.99 * 1e8 / 86462) = 2886 -> 2900
#: ``sms.pricing`` is authoritative and MAY quote a different value when the
#: caller supplies a fresher ``sats_per_usd``; the contract's printed number is a
#: default only. Never zero: unknown must never mean free.
DEFAULT_PRICE_SATS = 2900

#: shortest plausible E.164 national number, +CC included. Shorter input cannot
#: be priced as a real destination, so it takes the documented default rather
#: than being rejected outright (the rail is the thing that refuses it).
MIN_E164_LEN = 8


class InvalidDestination(Exception):
    reason = "bad_destination"
    # An EXAMPLE, not a number: the 555-01xx range is reserved for fictional use.
    hint = "Destination must be an E.164 phone number, e.g. +1 415 555 0100."

    def __init__(self, hint: str | None = None):
        self.hint = hint or type(self).hint
        super().__init__(self.hint)


def normalize_e164(dest: str) -> str:
    """Strip formatting; return '+<digits>'. Raise on input with no digits."""
    digits = re.sub(r"\D", "", dest or "")
    if not digits:
        raise InvalidDestination()
    return f"+{digits}"


def sats_per_usd_from_btcusd(btc_usd: float) -> float:
    """ADR-0002 `sats_per_usd`, from a live BTCUSD quote.

    1 BTC buys ``btc_usd`` USD and is worth 1e8 sats, so one USD buys
    ``1e8 / btc_usd`` sats. At BTCUSD=86462 that is 1156.55 sats/USD.
    """
    if not btc_usd or btc_usd <= 0:
        raise ValueError("btc_usd must be positive")
    return 100_000_000.0 / btc_usd


def quote_sats(sats_per_usd: float) -> int:
    """ADR-0002 formula: the flat price at a given sats/USD rate, rounded UP."""
    if not sats_per_usd or sats_per_usd <= 0:
        raise ValueError("sats_per_usd must be positive")
    raw = RISK_MULTIPLIER * RAIL_REPLACEMENT_USD * sats_per_usd
    return int(math.ceil(raw / PRICE_ROUNDING_SATS) * PRICE_ROUNDING_SATS)


def price_for(e164: str, sats_per_usd: float | None = None) -> int:
    """Flat sat price for one SMS. Never returns 0; ADR-0002 flat pricing.

    ``e164`` is still validated: a string with no digits is a caller bug that must
    not reach the rail. The destination does not otherwise change the price.
    """
    normalize_e164(e164)
    if sats_per_usd is not None:
        return quote_sats(sats_per_usd)
    return DEFAULT_PRICE_SATS
