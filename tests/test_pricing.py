"""Prefix pricing: table-driven, longest-prefix match, never free."""
from __future__ import annotations

import pytest

from app.pricing import (
    DEFAULT_PRICE,
    PREFIX_PRICES,
    BadDestination,
    price_for,
)

# The five prefixes the card names, plus the documented default.
KNOWN = ["+1", "+49", "+44", "+351", "+91"]


def test_table_is_documented_and_positive():
    for prefix in KNOWN:
        assert prefix in PREFIX_PRICES, f"{prefix} missing from the price table"
    assert all(p > 0 for p in PREFIX_PRICES.values())
    assert DEFAULT_PRICE > 0, "an unknown destination must never be free"


def test_price_for_table_prices_for_known_prefixes():
    for prefix in KNOWN:
        assert price_for(prefix + "5551234567") == PREFIX_PRICES[prefix]


def test_price_for_unknown_prefix_uses_documented_default():
    assert price_for("+9995551234567") == DEFAULT_PRICE
    assert price_for("+3705551234567") == DEFAULT_PRICE  # Lithuania: not in the table


def test_domestic_us_is_the_cheap_tier():
    # PLAN §1: 100 sats domestic-parity, 500 sats international.
    assert PREFIX_PRICES["+1"] == 100
    assert DEFAULT_PRICE == 500


def test_longest_prefix_wins():
    # A short prefix must not shadow a longer, more specific one.
    assert price_for("+14155551234") == PREFIX_PRICES["+1"]


@pytest.mark.parametrize("bad", ["", "  ", "4155551234", "not-a-number", "+", "+abc123"])
def test_malformed_destination_is_rejected(bad):
    with pytest.raises(BadDestination):
        price_for(bad)


@pytest.mark.parametrize("bad", [None, 12345, b"+14155551234"])
def test_non_string_destination_is_rejected(bad):
    with pytest.raises(BadDestination):
        price_for(bad)
