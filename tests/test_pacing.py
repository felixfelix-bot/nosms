"""Tests for human-shaped pacing (ADR-0002 compensating control #2).

The personal line's protection is: a hard daily cap plus a jittered minimum gap
between sends, both **persisted** (a reconnect or restart must not reset them).
A paced call defers, it never fails: the caller answers 429 + Retry-After and
the payer retries; no refund, because nothing was attempted.
"""
from __future__ import annotations

import json

import pytest

from app.transports.pacing import Pacer, PacingPolicy, load_pacing_policy


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FixedRng:
    def __init__(self, value: float):
        self.value = value

    def uniform(self, a: float, b: float) -> float:
        return min(max(self.value, a), b)


class SweepRng:
    """Returns each queued gap in turn (to prove the gap is re-drawn per send)."""

    def __init__(self, *values: float):
        self.values = list(values)

    def uniform(self, a: float, b: float) -> float:
        v = self.values.pop(0) if self.values else a
        return min(max(v, a), b)


def _pacer(policy=None, clock=None, rng=None, state_path=None):
    return Pacer(policy or PacingPolicy(daily_cap=3, min_gap_seconds=10,
                                        max_gap_seconds=20),
                 state_path=state_path, now=clock or Clock(), rng=rng or FixedRng(15))


# --- policy ------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"daily_cap": 0},
    {"min_gap_seconds": -1},
    {"min_gap_seconds": 30, "max_gap_seconds": 10},
])
def test_policy_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        PacingPolicy(**kwargs)


def test_defaults_are_the_adr_personal_line_caps():
    p = PacingPolicy()
    assert (p.daily_cap, p.min_gap_seconds, p.max_gap_seconds) == (20, 60.0, 300.0)


def test_policy_loads_from_env():
    p = load_pacing_policy({"NOSMS_JMP_DAILY_CAP": "7",
                            "NOSMS_JMP_MIN_GAP_SECONDS": "90",
                            "NOSMS_JMP_MAX_GAP_SECONDS": "120"})
    assert (p.daily_cap, p.min_gap_seconds, p.max_gap_seconds) == (7, 90.0, 120.0)


# --- the daily cap -----------------------------------------------------------

def test_daily_cap_blocks_and_offers_a_retry_after():
    clock = Clock(1000.0)
    pacer = _pacer(PacingPolicy(daily_cap=2, min_gap_seconds=0, max_gap_seconds=0),
                   clock=clock)
    pacer.record_send()
    pacer.record_send()
    d = pacer.check()
    assert d.allowed is False
    assert d.reason == "daily_cap_reached"
    assert d.sends_today == 2
    assert d.retry_after > 0
    assert d.retry_after_seconds <= 86400


def test_cap_resets_on_the_next_utc_day():
    clock = Clock(0.0)
    pacer = _pacer(PacingPolicy(daily_cap=1, min_gap_seconds=0, max_gap_seconds=0),
                   clock=clock)
    pacer.record_send()
    assert pacer.check().allowed is False

    clock.t += 86400.0            # exactly one UTC day later
    d = pacer.check()
    assert d.allowed is True
    assert d.sends_today == 0


# --- human-shaped jitter -----------------------------------------------------

def test_gap_is_jittered_per_send_not_fixed():
    clock = Clock(0.0)
    pacer = _pacer(clock=clock, rng=SweepRng(12.0, 19.0))
    pacer.record_send()
    assert pacer.next_gap_seconds() == 12.0
    clock.t += 12.0
    assert pacer.check().allowed is True
    pacer.record_send()
    assert pacer.next_gap_seconds() == 19.0     # re-drawn, not a metronome
    assert pacer.check().allowed is False


def test_min_gap_reports_the_wait():
    clock = Clock(0.0)
    pacer = _pacer(clock=clock, rng=FixedRng(20.0))
    pacer.record_send()
    clock.t += 5.0
    d = pacer.check()
    assert (d.allowed, d.reason) == (False, "min_gap")
    assert d.retry_after == 15
    clock.t += 15.0
    assert pacer.check().allowed is True


# --- persistence -------------------------------------------------------------

def test_counter_and_schedule_survive_a_restart(tmp_path):
    state = tmp_path / "jmp_pacing.json"
    clock = Clock(500.0)
    first = _pacer(clock=clock, state_path=str(state))
    first.record_send()
    first.record_send()

    # a new Pacer reading the same file sees the same day and count
    second = _pacer(clock=clock, state_path=str(state))
    assert second.check().sends_today == 2
    assert second.check().allowed is False       # still inside the jitter gap

    raw = json.loads(state.read_text())
    assert raw["count"] == 2
    assert raw["next_allowed_ts"] > raw["last_send_ts"]   # a real gap was drawn


def test_missing_state_file_starts_a_fresh_day(tmp_path):
    pacer = _pacer(state_path=str(tmp_path / "nope.json"))
    assert pacer.check().allowed is True
    assert pacer.check().sends_today == 0


def test_corrupt_state_file_degrades_to_a_safe_blank(tmp_path):
    state = tmp_path / "jmp_pacing.json"
    state.write_text("{ this is not json")
    pacer = _pacer(state_path=str(state))
    d = pacer.check()
    assert d.allowed is True and d.sends_today == 0


def test_snapshot_exposes_policy_state_and_decision():
    pacer = _pacer(clock=Clock(0.0))
    pacer.record_send()
    snap = pacer.snapshot()
    assert snap["policy"]["daily_cap"] == 3
    assert snap["state"]["count"] == 1
    assert snap["decision"]["allowed"] is False
