"""Tests for human-shaped pacing (ADR-0002 compensating control #2).

The personal line's protection is: a hard daily cap plus a jittered minimum gap
between sends, both **persisted** (a reconnect or restart must not reset them).
A paced call defers, it never fails: the caller answers 429 + Retry-After and
the payer retries; no refund, because nothing was attempted.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from app.transports.pacing import (
    Pacer,
    PacingDecision,
    PacingPolicy,
    load_pacing_policy,
)


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


def test_corrupt_state_file_fails_closed_not_open(tmp_path):
    """A corrupt counter must not silently restore a FULL daily cap.

    The abuse surface is volume, so the safe direction after corruption is
    "send nothing until an operator looks" — not "reset to zero and keep
    going". The corrupt bytes are left in place (never overwritten by a blank
    state) so the failure is visible and auditable.
    """
    state = tmp_path / "jmp_pacing.json"
    state.write_text("{ this is not json")
    pacer = _pacer(state_path=str(state))

    d = pacer.check()
    assert d.allowed is False
    assert d.reason == "pacing_state_corrupt"

    claim = pacer.claim()
    assert claim.allowed is False
    assert claim.reason == "pacing_state_corrupt"

    # the corrupt file was NOT rewritten as a fresh (full-cap) day
    assert state.read_text() == "{ this is not json"
    # and the snapshot stays readable for /api/health
    assert pacer.snapshot()["state"]["corrupt"] is True


def test_a_corrupt_pacer_cannot_be_revived_by_a_new_day(tmp_path):
    state = tmp_path / "jmp_pacing.json"
    state.write_text("garbage")
    pacer = _pacer(clock=Clock(0.0), state_path=str(state))
    pacer._now = lambda: 86400.0 * 5          # five UTC days later
    assert pacer.check().reason == "pacing_state_corrupt"
    assert pacer.claim().allowed is False


def test_snapshot_exposes_policy_state_and_decision():
    pacer = _pacer(clock=Clock(0.0))
    pacer.record_send()
    snap = pacer.snapshot()
    assert snap["policy"]["daily_cap"] == 3
    assert snap["state"]["count"] == 1
    assert snap["decision"]["allowed"] is False


# --- atomic claim / reserve (the check-then-act fix) --------------------------
#
# The daily cap and the jittered gap are the abuse-surface controls, so they must
# be *reserved before* the blocking send, not counted after it returns. Two
# claimants arriving at `daily_cap - 1` must not both send.

def _contend(pacer, n=2, timeout=5.0):
    """Run ``n`` concurrent claimants and return (granted, refused).

    Deterministic, not probabilistic: a claimant that was granted a slot holds
    it until *every* other claimant has recorded its attempt, so the losers
    provably arrive while the slot is still reserved.
    """
    granted: list[PacingDecision] = []
    refused: list[PacingDecision] = []
    attempts = 0
    cond = threading.Condition()

    def worker():
        nonlocal attempts
        d = pacer.claim()
        with cond:
            attempts += 1
            (granted if d.allowed else refused).append(d)
            cond.notify_all()
            if d.allowed:
                deadline = time.monotonic() + timeout
                while attempts < n and time.monotonic() < deadline:
                    cond.wait(timeout)
        if d.allowed:
            pacer.release(d, accepted=True)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 5)
    assert not any(t.is_alive() for t in threads), "a claimant deadlocked"
    return granted, refused


def test_concurrent_claimants_cannot_blow_the_daily_cap():
    clock = Clock(0.0)
    pacer = Pacer(PacingPolicy(daily_cap=2, min_gap_seconds=0, max_gap_seconds=0),
                  now=clock, rng=FixedRng(0.0))
    pacer.record_send()                       # count == daily_cap - 1

    granted, refused = _contend(pacer, 2)

    assert len(granted) == 1                  # exactly one send was attempted
    assert len(refused) == 1
    assert refused[0].reason == "daily_cap_reached"
    assert pacer.snapshot()["state"]["count"] == 2   # the cap was never exceeded


def test_concurrent_claimants_cannot_both_ride_the_jitter_gap():
    """The other half of the same race: the gap, not the counter."""
    clock = Clock(0.0)
    pacer = Pacer(PacingPolicy(daily_cap=5, min_gap_seconds=60, max_gap_seconds=60),
                  now=clock, rng=FixedRng(60.0))
    pacer.record_send()                       # next_allowed_ts = 0 + 60
    clock.t = 61.0                            # the gap has elapsed for BOTH

    granted, refused = _contend(pacer, 2)

    assert len(granted) == 1                  # the min-gap was not voided
    assert len(refused) == 1
    assert refused[0].reason == "send_in_flight"
    assert pacer.snapshot()["state"]["count"] == 2


def test_claim_reserves_immediately_so_check_agrees():
    clock = Clock(0.0)
    pacer = _pacer(clock=clock)
    claim = pacer.claim()
    assert claim.allowed is True
    assert pacer.check().allowed is False     # slot reserved, not yet confirmed
    assert pacer.snapshot()["state"]["pending"] is True


def test_release_refunds_the_slot_when_the_send_did_not_leave():
    clock = Clock(0.0)
    pacer = _pacer(PacingPolicy(daily_cap=1, min_gap_seconds=0, max_gap_seconds=0),
                   clock=clock)
    claim = pacer.claim()
    pacer.release(claim, accepted=False)      # refused / RailUnavailable

    state = pacer.snapshot()["state"]
    assert state["count"] == 0                # the slot came back
    assert state["pending"] is False
    assert pacer.check().allowed is True      # and the day is still usable


def test_release_confirms_the_slot_and_starts_the_jitter_gap():
    clock = Clock(0.0)
    pacer = _pacer(clock=clock, rng=FixedRng(15.0))
    claim = pacer.claim()
    pacer.release(claim, accepted=True)

    state = pacer.snapshot()["state"]
    assert state["count"] == 1
    assert state["pending"] is False
    assert state["next_allowed_ts"] == 15.0   # the gap starts at release time
    assert pacer.check().allowed is False


def test_a_released_claim_is_idempotent_and_never_double_counts():
    pacer = _pacer(clock=Clock(0.0))
    claim = pacer.claim()
    pacer.release(claim, accepted=True)
    pacer.release(claim, accepted=True)       # a retry must not count twice
    pacer.release(claim, accepted=False)      # nor refund after confirming
    assert pacer.snapshot()["state"]["count"] == 1


def test_release_rejects_a_forged_claim():
    pacer = _pacer(clock=Clock(0.0))
    forged = PacingDecision(allowed=True, reason="ok", claim_token="not-a-real-claim")
    with pytest.raises(ValueError):
        pacer.release(forged, accepted=True)
    with pytest.raises(ValueError):
        pacer.release(None, accepted=True)        # no token at all
    assert pacer.snapshot()["state"]["count"] == 0


def test_record_send_refuses_when_the_state_is_corrupt(tmp_path):
    """The un-reserved shortcut must not quietly count against a corrupt file."""
    state = tmp_path / "jmp_pacing.json"
    state.write_text("{ broken")
    pacer = _pacer(state_path=str(state))
    with pytest.raises(ValueError):
        pacer.record_send()


def test_a_state_that_is_json_but_not_an_object_is_corrupt_too(tmp_path):
    """A list/number is valid JSON but not a counter: fail closed, never crash."""
    state = tmp_path / "jmp_pacing.json"
    state.write_text("[1, 2, 3]")
    assert _pacer(state_path=str(state)).check().reason == "pacing_state_corrupt"

    state.write_text('{"count": "not-a-number"}')
    assert _pacer(state_path=str(state)).check().reason == "pacing_state_corrupt"


def test_a_paced_claim_is_never_recorded_as_a_send():
    clock = Clock(0.0)
    pacer = _pacer(PacingPolicy(daily_cap=2, min_gap_seconds=0, max_gap_seconds=0),
                   clock=clock)
    pacer.record_send()
    pacer.record_send()
    claim = pacer.claim()
    assert claim.allowed is False
    assert (claim.reason, claim.sends_today) == ("daily_cap_reached", 2)
    assert claim.retry_after > 0
    assert pacer.snapshot()["state"]["count"] == 2   # never consumes a slot


def test_claim_reservation_survives_a_crash_so_the_cap_is_not_available_twice(tmp_path):
    """Reserve-now, count-after-send: a process that dies mid-send must not
    hand the same slot to the next process."""
    state = tmp_path / "jmp_pacing.json"
    clock = Clock(0.0)
    first = Pacer(PacingPolicy(daily_cap=1, min_gap_seconds=0, max_gap_seconds=0),
                  state_path=str(state), now=clock, rng=FixedRng(0.0))
    claim = first.claim()                     # reserved, then "crashed" - never released

    second = Pacer(PacingPolicy(daily_cap=1, min_gap_seconds=0, max_gap_seconds=0),
                   state_path=str(state), now=clock, rng=FixedRng(0.0))
    assert second.check().allowed is False
    assert second.claim().allowed is False
    assert second.snapshot()["state"]["count"] == 1
    assert first.snapshot()["state"]["pending"] is True
