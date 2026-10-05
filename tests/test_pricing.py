"""Tests for the ADR-0002 flat postage pricing.

Pinned behaviour:

- one flat price for EVERY destination — domestic and international alike,
  because the v1 rail's plan is unlimited including international, so
  destination no longer maps to cost (this supersedes the 100/500 split);
- the price is the rugpull-risk premium, i.e. the ADR formula
  ``ceil(MULT * rail_replacement_usd * sats_per_usd)`` at the documented rate,
  never below the ADR floor;
- there is no code path that returns 0, and no silent free send;
- a value that is not an E.164 number raises a machine-readable error instead of
  being quietly priced (the old table priced any digit string at the default).
"""
from __future__ import annotations

import math

import pytest

from app.pricing import (
    BTC_USD_DEFAULT,
    DEFAULT_PRICE_SATS,
    FLAT_PRICE_SATS,
    MULT,
    PRICE_FLOOR_SATS,
    RAIL_REPLACEMENT_USD,
    InvalidDestination,
    price_for,
    quote_sats,
    sats_per_usd,
)


# --- the price is flat for every destination --------------------------------

@pytest.mark.parametrize("dest", [
    "+14155550100", "+1 415 555 0100", "+14155550100",   # domestic
    "+4917012345678", "+447700900123", "+351912345678",  # international
    "+919876543210", "+358401234567", "+999123456789",   # incl. unknown prefix
])
def test_every_destination_pays_the_same_flat_price(dest):
    assert price_for(dest) == DEFAULT_PRICE_SATS


def test_price_is_flat_and_never_zero():
    assert DEFAULT_PRICE_SATS > 0
    assert price_for("+15555550100") == price_for("+4917012345678") != 0


# --- the formula from ADR-0002 §Pricing -------------------------------------

def test_sats_per_usd_is_one_hundred_million_over_the_btc_price():
    assert sats_per_usd(86_462.0) == pytest.approx(100_000_000 / 86_462.0)


def test_quote_follows_the_adr_formula_at_the_documented_rate():
    expected = math.ceil(MULT * RAIL_REPLACEMENT_USD * sats_per_usd(BTC_USD_DEFAULT))
    assert quote_sats() == expected


def test_quote_is_below_the_documented_rounded_default():
    """2900 is the rounded-up published number; the raw quote is 2886."""
    assert quote_sats() == 2886
    assert FLAT_PRICE_SATS == 2900


def test_quote_is_flat_across_experimental_tiers():
    """The 'sim card' reference is read as the rail's replacement cost, so only
    rail_replacement_usd changes when the operator swaps tiers — the formula and
    the walk-down are unaffected, and the flat shape is preserved."""
    cheap = quote_sats(rail_replacement_usd=2.99)
    dear = quote_sats(rail_replacement_usd=69.99)
    assert cheap < quote_sats() < dear
    assert cheap == math.ceil(MULT * 2.99 * sats_per_usd(BTC_USD_DEFAULT))
    assert cheap >= PRICE_FLOOR_SATS


def test_quote_never_goes_below_the_floor():
    # a low BTC price and a trivial rail cost still cannot quote under the floor
    assert quote_sats(btc_usd=1e9, rail_replacement_usd=0.001) == PRICE_FLOOR_SATS


def test_quote_rises_when_btc_falls_because_usd_prices_are_fixed():
    assert quote_sats(btc_usd=21_000) > quote_sats(btc_usd=210_000)


def test_quote_rejects_a_nonsense_rate():
    with pytest.raises(ValueError):
        quote_sats(btc_usd=0)


# --- invalid destinations are refused, not priced ---------------------------

@pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "+"])
def test_invalid_destination_raises(bad):
    with pytest.raises(InvalidDestination) as e:
        price_for(bad)
    assert e.value.reason == "bad_destination"
    assert e.value.hint


@pytest.mark.parametrize("too_short", ["+1", "+1555", "+49170"])
def test_too_short_destination_raises_instead_of_pricing(too_short):
    with pytest.raises(InvalidDestination):
        price_for(too_short)
