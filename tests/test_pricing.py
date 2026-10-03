"""RED-first tests for the prefix pricing table (nosms M1a).

Pinned behaviour:
- known destination prefixes map to their configured sat price (domestic +1)
- an unknown prefix falls back to the DOCUMENTED default
- the default is never zero: an unknown destination is never a silent free send
- clearly invalid input raises a machine-readable InvalidDestination
"""
from __future__ import annotations

import pytest

from app.pricing import (
    DEFAULT_PRICE_SATS,
    PREFIX_PRICES,
    InvalidDestination,
    price_for,
)


def test_domestic_us_is_cheapest():
    assert price_for("+14155550100") == PREFIX_PRICES["+1"]
    assert price_for("+1 415 555 0100") == PREFIX_PRICES["+1"]


@pytest.mark.parametrize("dest", ["+4915112345678", "+447700900123",
                                  "+351912345678", "+919876543210"])
def test_international_prefixes_have_prices(dest):
    price = price_for(dest)
    assert isinstance(price, int) and price > 0


def test_unknown_prefix_uses_documented_default():
    assert price_for("+358401234567") == DEFAULT_PRICE_SATS
    assert price_for("+1555") == DEFAULT_PRICE_SATS  # too short -> default, not free


def test_default_is_not_zero():
    assert DEFAULT_PRICE_SATS > 0
    assert price_for("+9999999999") == DEFAULT_PRICE_SATS
    assert price_for("+9999999999") != 0


def test_all_prices_are_positive():
    assert all(p > 0 for p in PREFIX_PRICES.values())
    assert all(k.startswith("+") for k in PREFIX_PRICES)


@pytest.mark.parametrize("bad", ["", "   ", "not-a-number", "+"])
def test_invalid_destination_raises(bad):
    with pytest.raises(InvalidDestination) as e:
        price_for(bad)
    assert e.value.reason == "bad_destination"
    assert e.value.hint
