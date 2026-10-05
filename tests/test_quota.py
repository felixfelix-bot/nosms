"""RED-first tests for the per-destination cooldown and the per-identity daily cap.

Both are config-driven: the same store must behave differently under different
config values, which is the only way a "config-driven" claim is worth anything.
"""
from __future__ import annotations

import pytest

from app.quota import QuotaError, QuotaStore

IDENT = "aa" * 32
OTHER = "bb" * 32
DEST = "+15551234567"


def test_cooldown_blocks_the_same_destination_twice(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=60, daily_cap=100)
    q.check_and_record(IDENT, DEST, now=1000)
    with pytest.raises(QuotaError) as e:
        q.check_and_record(IDENT, DEST, now=1030)
    assert e.value.reason == "destination_cooldown"
    assert "30" in e.value.hint            # names the seconds remaining
    assert e.value.retry_after == 30


def test_cooldown_expires_and_lets_the_next_send_through(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=60, daily_cap=100)
    q.check_and_record(IDENT, DEST, now=1000)
    q.check_and_record(IDENT, DEST, now=1060)      # exactly at the boundary is fine


def test_cooldown_zero_disables_the_check(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=0, daily_cap=100)
    q.check_and_record(IDENT, DEST, now=1000)
    q.check_and_record(IDENT, DEST, now=1000)


def test_cooldown_is_per_destination_and_per_identity(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=60, daily_cap=100)
    q.check_and_record(IDENT, DEST, now=1000)
    q.check_and_record(IDENT, "+15559999999", now=1000)     # other destination
    q.check_and_record(OTHER, DEST, now=1000)               # other identity


def test_daily_cap_counts_per_identity_over_a_rolling_window(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=0, daily_cap=2)
    q.check_and_record(IDENT, "+15550000001", now=1000)
    q.check_and_record(IDENT, "+15550000002", now=1001)
    with pytest.raises(QuotaError) as e:
        q.check_and_record(IDENT, "+15550000003", now=1002)
    assert e.value.reason == "daily_cap_reached"
    assert "2" in e.value.hint
    # a different identity has its own budget
    q.check_and_record(OTHER, "+15550000003", now=1002)
    # ...and the window rolls: 24h later the first two have aged out
    q.check_and_record(IDENT, "+15550000003", now=1002 + 24 * 3600)


def test_daily_cap_zero_means_unlimited(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=0, daily_cap=0)
    for i in range(50):
        q.check_and_record(IDENT, f"+1555000{i:04d}", now=1000 + i)


def test_usage_is_visible_for_monitoring(tmp_path):
    q = QuotaStore(str(tmp_path / "q.db"), cooldown_seconds=0, daily_cap=100)
    q.check_and_record(IDENT, DEST, now=1000)
    assert q.usage(IDENT, now=1000) == 1
    assert q.usage(OTHER, now=1000) == 0
