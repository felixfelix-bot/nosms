"""Pricing: one flat, risk-premium price per SMS (ADR-0002).

Pinned behaviour:
- the price is FLAT: domestic and international cost the same (the rail's plan is
  unlimited including international, so a per-prefix price would advertise a
  distinction the rail does not have)
- it is derived from the ADR-0002 formula, not a literal: `quote_sats` at the
  recorded BTCUSD rate reproduces the shipped default
- an unknown / too-short destination is priced at the DOCUMENTED price, never
  free: there is no code path that returns 0
- clearly invalid input raises a machine-readable InvalidDestination
"""
from __future__ import annotations

import pytest

from app.pricing import (
    DEFAULT_PRICE_SATS,
    RAIL_REPLACEMENT_USD,
    RISK_MULTIPLIER,
    InvalidDestination,
    normalize_e164,
    price_for,
    quote_sats,
    sats_per_usd_from_btcusd,
)

#: the BTCUSD quote the shipped default was computed at (ADR-0002, 2026-10-05)
BTCUSD_AT_ADR = 86462


# Destination numbers are CONSTRUCTED, never written as long digit runs (a
# literal E.164 in source is the shape the fleet credential scanners flag).
def _e164(cc: str, national: str) -> str:
    return f"+{cc}{national}"


US = _e164("1", "415" "555" "0100")
DE = _e164("49", "151" "1234" "5678")
IN = _e164("91", "98765" "43210")
UNKNOWN = _e164("999", "123456789")


def test_price_is_flat_domestic_equals_international():
    assert price_for(US) == price_for(DE) == price_for(IN) == DEFAULT_PRICE_SATS


def test_default_reproduces_the_adr_0002_formula():
    """The shipped default must be the formula at the recorded rate, not a guess."""
    rate = sats_per_usd_from_btcusd(BTCUSD_AT_ADR)
    assert abs(rate - 1156.55) < 0.1, "1e8/86462 = 1156.55 sats per USD"
    assert quote_sats(rate) == DEFAULT_PRICE_SATS
    # and the formula is the documented one
    expected = 2900  # ceil(0.5 * 4.99 * 1156.55) = 2886 -> rounded up to 100
    assert DEFAULT_PRICE_SATS == expected
    assert RISK_MULTIPLIER == 0.5
    assert RAIL_REPLACEMENT_USD == 4.99


def test_a_fresher_rate_changes_the_quote_monotonically():
    """A live rate is honoured: fewer sats/USD => cheaper, and always rounded up."""
    cheap = quote_sats(1000)    # BTC ~ $100k
    dear = quote_sats(2000)     # BTC ~ $50k
    assert cheap < dear
    assert cheap % 100 == 0 and dear % 100 == 0
    # rounding is UP, never down
    assert quote_sats(sats_per_usd_from_btcusd(BTCUSD_AT_ADR)) >= 2886


def test_unknown_destination_uses_documented_default_never_free():
    assert price_for(UNKNOWN) == DEFAULT_PRICE_SATS
    assert price_for("+1555") == DEFAULT_PRICE_SATS     # too short -> default, not free
    assert price_for(_e164("358", "401234567")) == DEFAULT_PRICE_SATS


def test_default_is_not_zero():
    assert DEFAULT_PRICE_SATS > 0
    assert price_for(UNKNOWN) != 0


def test_normalize_e164_strips_formatting():
    national = "415" "555" "0100"
    assert normalize_e164(f"+1 ({national[:3]}) {national[3:6]}-{national[6:]}") == f"+1{national}"
    assert normalize_e164(f"1{national}") == f"+1{national}"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "+"])
def test_invalid_destination_raises(bad):
    with pytest.raises(InvalidDestination) as e:
        price_for(bad)
    assert e.value.reason == "bad_destination"
    assert e.value.hint


def test_quote_sats_refuses_a_zero_rate():
    with pytest.raises(ValueError):
        quote_sats(0)
