"""Human-shaped pacing for a personal-line SMS rail (ADR-0002 compensating control #2).

The JMP rail rides the operator's **own personal line**, so volume *is* the
abuse surface: a burst is what trips JMP's anti-abuse gate, the FUP and, in the
worst case, the account (the accepted risk in ADR-0002). The rail therefore
paces itself two ways, and both are persisted so a reconnect or a process
restart cannot silently reset them:

* a hard **daily cap** (UTC day), and
* a **human-shaped minimum gap** between sends, re-jittered after every send.

Why jitter: a fixed interval is itself a signature — a metronome. Drawing the
next gap uniformly from ``[min_gap_seconds, max_gap_seconds]`` turns the send
train into "a person who happens to reply at irregular times".

A paced call is **not a failure**: the message was never attempted, so it must
not refund. :meth:`Pacer.check` returns a :class:`PacingDecision` carrying
``retry_after_seconds`` so the caller can answer ``429`` + ``Retry-After`` and
let the payer come back, instead of taking the money and failing the send.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass

__all__ = ["PacingPolicy", "PacingDecision", "Pacer", "load_pacing_policy"]


@dataclass(frozen=True)
class PacingPolicy:
    """Limits for a personal-line rail. Deliberately low by default."""

    daily_cap: int = 20
    min_gap_seconds: float = 60.0
    max_gap_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.daily_cap < 1:
            raise ValueError("daily_cap must be >= 1")
        if self.min_gap_seconds < 0 or self.max_gap_seconds < self.min_gap_seconds:
            raise ValueError("need 0 <= min_gap_seconds <= max_gap_seconds")


@dataclass(frozen=True)
class PacingDecision:
    """Outcome of a pace check. `retry_after_seconds` is for HTTP Retry-After."""

    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0
    sends_today: int = 0

    @property
    def retry_after(self) -> int:
        """Whole seconds, never negative (HTTP header shape)."""
        return max(0, int(self.retry_after_seconds + 0.999))


class Pacer:
    """Persisted daily cap + jittered minimum gap.

    ``state_path`` of ``None`` keeps state in memory (tests, ephemeral runs);
    any other value is a JSON file written atomically on every send.
    """

    def __init__(self, policy: PacingPolicy | None = None, *,
                 state_path: str | None = None, now=None, rng=None):
        self.policy = policy or PacingPolicy()
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self._now = now or time.time
        self._rng = rng or random.Random()
        self._state = self._load()

    # --- state ---------------------------------------------------------------

    def _blank(self) -> dict:
        return {"day": self._today(), "count": 0,
                "last_send_ts": 0.0, "next_allowed_ts": 0.0}

    def _load(self) -> dict:
        if not self.state_path or not os.path.exists(self.state_path):
            return self._blank()
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # A corrupt counter must fail loud-but-safe: a fresh day is safer
            # than an unbounded counter, and the daily cap still applies.
            return self._blank()
        state = self._blank()
        state.update({k: data[k] for k in state if k in data})
        state["count"] = int(state["count"])
        return state

    def _save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self._state, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.state_path)   # atomic: no half-written counter

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._now()))

    def _roll_day(self) -> bool:
        """Reset the counter when the UTC day changes. Returns True if rolled."""
        today = self._today()
        if self._state.get("day") != today:
            self._state = {"day": today, "count": 0, "last_send_ts": 0.0,
                           "next_allowed_ts": 0.0}
            return True
        return False

    # --- api -----------------------------------------------------------------

    def check(self) -> PacingDecision:
        """May I send right now? Never raises; always answers with a reason."""
        self._roll_day()
        count = int(self._state.get("count", 0))
        if count >= self.policy.daily_cap:
            return PacingDecision(
                allowed=False, reason="daily_cap_reached",
                retry_after_seconds=self._seconds_to_utc_midnight(), sends_today=count)
        wait = float(self._state.get("next_allowed_ts", 0.0)) - self._now()
        if wait > 0:
            return PacingDecision(allowed=False, reason="min_gap",
                                  retry_after_seconds=wait, sends_today=count)
        return PacingDecision(allowed=True, reason="ok", sends_today=count)

    def record_send(self) -> None:
        """Count a send that the rail accepted, and schedule the next window."""
        self._roll_day()
        now = self._now()
        self._state["count"] = int(self._state.get("count", 0)) + 1
        self._state["last_send_ts"] = now
        self._state["next_allowed_ts"] = now + self._rng.uniform(
            self.policy.min_gap_seconds, self.policy.max_gap_seconds)
        self._save()

    def next_gap_seconds(self) -> float:
        """The gap that a send right now would impose (observability/tests)."""
        return self._state.get("next_allowed_ts", 0.0) - self._state.get(
            "last_send_ts", 0.0)

    def snapshot(self) -> dict:
        """Current persisted state plus the policy — for /api/health style reads."""
        self._roll_day()
        return {"policy": asdict(self.policy),
                "state": dict(self._state),
                "decision": asdict(self.check())}

    def _seconds_to_utc_midnight(self) -> float:
        now = self._now()
        return max(1.0, 86400.0 - (now % 86400.0))


def load_pacing_policy(env: dict | None = None) -> PacingPolicy:
    """Build the policy from env, defaulting to the ADR-0002 personal-line caps."""
    e = os.environ if env is None else env
    return PacingPolicy(
        daily_cap=int(e.get("NOSMS_JMP_DAILY_CAP", "20")),
        min_gap_seconds=float(e.get("NOSMS_JMP_MIN_GAP_SECONDS", "60")),
        max_gap_seconds=float(e.get("NOSMS_JMP_MAX_GAP_SECONDS", "300")),
    )
