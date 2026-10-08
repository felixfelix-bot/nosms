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

Reserve now, count after the send
---------------------------------
A service endpoint is served from a threadpool, so two callers can arrive at the
same instant. :meth:`Pacer.check` followed by a blocking send followed by
:meth:`Pacer.record_send` is a check-then-act race: both callers see
``count == daily_cap - 1`` and both send. So the *only* supported path for a real
send is :meth:`Pacer.claim`, which **reserves the slot under a lock before** the
send is attempted, and persists the reservation:

* a claim takes the slot immediately (``count`` increments, ``pending`` is set),
  so a second concurrent caller is refused rather than waved through;
* :meth:`Pacer.release` confirms the slot (``accepted=True``) — that is when the
  jittered gap starts — or refunds it (``accepted=False``) when the send never
  left the client, so a refusal never eats the day's allowance;
* the reservation is written to the state file, so a process that dies mid-send
  does not hand the same slot to its successor.

A paced call is **not a failure**: the message was never attempted, so it must
not refund. The decision carries ``retry_after_seconds`` so the caller can answer
``429`` + ``Retry-After`` and let the payer come back, instead of taking the
money and failing the send.

Fail closed on a corrupt counter
--------------------------------
State that cannot be parsed used to degrade to a blank day, which silently hands
back a *full* daily cap — the unsafe direction for an abuse surface. It now fails
**closed**: every claim is refused with ``pacing_state_corrupt`` until an operator
clears the file, and the unreadable bytes are left in place so the failure is
visible.

One counter per line
--------------------
:func:`load_pacing_policy` is the JMP rail's policy; :func:`load_whatsapp_pacing_policy`
is the WhatsApp rail's (ADR-0003). Two rails, two personal lines, two abuse
surfaces, two counters — sharing one would let a burst on one line consume the
other's allowance while the other line's cap quietly stopped being enforced.
The two policies are structurally identical; only the knob names and the
defaults differ.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass

__all__ = ["PacingPolicy", "PacingDecision", "Pacer", "load_pacing_policy",
           "load_whatsapp_pacing_policy"]

#: Reasons a claim can be refused, in the order they are evaluated.
#: ``pacing_state_corrupt`` is terminal until a human clears the state file.
REFUSAL_REASONS = ("pacing_state_corrupt", "daily_cap_reached", "min_gap",
                   "send_in_flight")

#: Outstanding+released claim tokens kept for idempotent release. Claims are
#: short-lived (one send), so this bound is only here to stop unbounded growth
#: in a long-running process.
_RELEASED_TOKENS_MAX = 4096


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
    """Outcome of a pace check. `retry_after_seconds` is for HTTP Retry-After.

    ``claim_token`` is set only by :meth:`Pacer.claim` on a granted slot: it is
    the handle the caller must pass back to :meth:`Pacer.release` once the send
    has concluded. A decision from :meth:`Pacer.check` never carries one.
    """

    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0
    sends_today: int = 0
    claim_token: str | None = None

    @property
    def retry_after(self) -> int:
        """Whole seconds, never negative (HTTP header shape)."""
        return max(0, int(self.retry_after_seconds + 0.999))


class Pacer:
    """Persisted daily cap + jittered minimum gap, with atomic slot reservation.

    ``state_path`` of ``None`` keeps state in memory (tests, ephemeral runs);
    any other value is a JSON file written atomically on every reservation and
    release.

    Thread-safe: every read-modify-write of the counter happens under
    :attr:`_lock`, so one process is enough to make the cap real. The state file
    remains the cross-process / cross-restart source of truth.
    """

    def __init__(self, policy: PacingPolicy | None = None, *,
                 state_path: str | None = None, now=None, rng=None):
        self.policy = policy or PacingPolicy()
        self.state_path = os.path.expanduser(state_path) if state_path else None
        self._now = now or time.time
        self._rng = rng or random.Random()
        #: Guards every mutation of ``_state`` (check, claim, release, roll).
        self._lock = threading.RLock()
        self._claims: dict[str, float] = {}     # outstanding token -> reserved_at
        self._released: set[str] = set()        # for idempotent release
        self._state = self._load()

    # --- state ---------------------------------------------------------------

    def _blank(self) -> dict:
        return {"day": self._today(), "count": 0, "last_send_ts": 0.0,
                "next_allowed_ts": 0.0, "pending": False, "corrupt": False}

    def _load(self) -> dict:
        """Read the persisted counter, failing **closed** on anything unreadable.

        A corrupt counter is not a fresh day: resetting it would restore a full
        daily cap, which is the direction an abuser wants. The blank returned
        here is marked ``corrupt`` and is never written back, so the refusal
        survives restarts until a human inspects and clears the file.
        """
        if not self.state_path or not os.path.exists(self.state_path):
            return self._blank()
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("pacing state is not a JSON object")
        except (OSError, ValueError):
            return self._corrupt_state()
        state = self._blank()
        state.update({k: data[k] for k in state if k in data})
        try:
            state["count"] = int(state["count"])
            state["last_send_ts"] = float(state["last_send_ts"])
            state["next_allowed_ts"] = float(state["next_allowed_ts"])
            state["pending"] = bool(state["pending"])
        except (TypeError, ValueError):
            return self._corrupt_state()
        return state

    def _corrupt_state(self) -> dict:
        state = self._blank()
        state["corrupt"] = True
        return state

    def _save(self) -> None:
        """Persist the state atomically. Never overwrites a corrupt state file."""
        if not self.state_path or self._state.get("corrupt"):
            return
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(self._state, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.state_path)   # atomic: no half-written counter

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._now()))

    def _roll_day(self) -> bool:
        """Reset the counter when the UTC day changes. Returns True if rolled.

        A corrupt counter never rolls: the day is not what is wrong with it.
        """
        if self._state.get("corrupt"):
            return False
        today = self._today()
        if self._state.get("day") != today:
            self._state = self._blank()
            self._state["day"] = today
            return True
        return False

    # --- api -----------------------------------------------------------------

    def check(self) -> PacingDecision:
        """May I send right now? Never raises; never reserves; always a reason.

        Read-only: it takes no slot, so a caller that goes on to send must use
        :meth:`claim` (which re-decides under the lock) rather than relying on
        this answer.
        """
        with self._lock:
            return self._decide()

    def claim(self) -> PacingDecision:
        """Atomically reserve one send slot, or refuse with a reason.

        This is the **only** correct pre-send check. The slot is consumed and
        persisted the moment it is granted, so two concurrent callers can never
        both pass at ``count == daily_cap - 1`` and can never both ride the same
        jittered gap. Confirm the send with :meth:`release`, or refund the slot
        there when the message never left the client.
        """
        with self._lock:
            decision = self._decide()
            if not decision.allowed:
                return decision
            self._state["count"] = int(self._state.get("count", 0) or 0) + 1
            self._state["pending"] = True
            self._save()
            token = uuid.uuid4().hex
            self._claims[token] = self._now()
            if len(self._released) > _RELEASED_TOKENS_MAX:
                self._released.clear()
            return PacingDecision(allowed=True, reason="ok",
                                  sends_today=self._state["count"],
                                  claim_token=token)

    def release(self, decision: PacingDecision | None, *, accepted: bool) -> None:
        """Conclude a reserved slot: confirm it, or refund it.

        ``accepted=True`` counts the send and starts the jittered gap.
        ``accepted=False`` (a refusal, an empty body, a link that could not take
        the message) returns the slot to the day — a message that never left the
        client must not consume the allowance, and must not move the clock
        forward either.

        Idempotent per claim token, so a double-release can never double-count
        or double-refund. Raises :class:`ValueError` for a token this pacer did
        not issue.
        """
        token = getattr(decision, "claim_token", None)
        with self._lock:
            if not token:
                raise ValueError(
                    "release() needs the PacingDecision returned by Pacer.claim()")
            if token in self._claims:
                self._claims.pop(token)
            elif token in self._released:
                return                       # already concluded: no-op
            else:
                raise ValueError("unknown or forged pacing claim")
            self._released.add(token)

            if accepted:
                now = self._now()
                self._state["last_send_ts"] = now
                self._state["next_allowed_ts"] = now + self._rng.uniform(
                    self.policy.min_gap_seconds, self.policy.max_gap_seconds)
            else:
                self._state["count"] = max(
                    0, int(self._state.get("count", 0) or 0) - 1)
            self._state["pending"] = bool(self._claims)
            self._save()

    def record_send(self) -> None:
        """Count a send that the rail accepted, and schedule the next window.

        The un-reserved shortcut for a caller that is not racing anything (tests,
        a single-threaded probe). A real service path must use
        :meth:`claim`/:meth:`release`.
        """
        with self._lock:
            self._roll_day()
            if self._state.get("corrupt"):
                raise ValueError("pacing state is corrupt; refusing to count")
            now = self._now()
            self._state["count"] = int(self._state.get("count", 0) or 0) + 1
            self._state["last_send_ts"] = now
            self._state["next_allowed_ts"] = now + self._rng.uniform(
                self.policy.min_gap_seconds, self.policy.max_gap_seconds)
            self._state["pending"] = bool(self._claims)
            self._save()

    def next_gap_seconds(self) -> float:
        """The gap that a send right now would impose (observability/tests)."""
        return self._state.get("next_allowed_ts", 0.0) - self._state.get(
            "last_send_ts", 0.0)

    def snapshot(self) -> dict:
        """Current persisted state plus the policy — for /api/health style reads."""
        with self._lock:
            self._roll_day()
            return {"policy": asdict(self.policy),
                    "state": dict(self._state),
                    "pending_claims": len(self._claims),
                    "decision": asdict(self._decide())}

    # --- internals (call with the lock held) ---------------------------------

    def _decide(self) -> PacingDecision:
        """The one place the pacing decision is made. Never reserves."""
        self._roll_day()
        count = int(self._state.get("count", 0) or 0)
        if self._state.get("corrupt"):
            return PacingDecision(allowed=False, reason="pacing_state_corrupt",
                                  sends_today=count)
        if count >= self.policy.daily_cap:
            return PacingDecision(
                allowed=False, reason="daily_cap_reached",
                retry_after_seconds=self._seconds_to_utc_midnight(), sends_today=count)
        wait = float(self._state.get("next_allowed_ts", 0.0) or 0.0) - self._now()
        if wait > 0:
            return PacingDecision(allowed=False, reason="min_gap",
                                  retry_after_seconds=wait, sends_today=count)
        if self._state.get("pending"):
            # Another caller holds the line right now: the send is in flight. The
            # retry hint is the floor of the gap this send will arm on release.
            return PacingDecision(
                allowed=False, reason="send_in_flight",
                retry_after_seconds=max(1.0, self.policy.min_gap_seconds),
                sends_today=count)
        return PacingDecision(allowed=True, reason="ok", sends_today=count)

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


def load_whatsapp_pacing_policy(env: dict | None = None) -> PacingPolicy:
    """Build the WhatsApp rail's policy from env (ADR-0003 control #2).

    Its own knobs and its own state file, on purpose: the JMP line and the
    WhatsApp line are separate abuse surfaces, and one shared counter would
    meter the wrong line. See the module docstring.

    The defaults are **tighter** than the JMP rail's. On WhatsApp the operator's
    own account is the thing a burst can cost, and the ADR records that a
    detected ban is the accepted risk: a handful of sends an hour, drawn from a
    jittered window, is the shape of a person. ``max_gap_seconds`` is wider than
    JMP's so the average rate stays low even at the cap.
    """
    e = os.environ if env is None else env
    return PacingPolicy(
        daily_cap=int(e.get("NOSMS_WHATSAPP_DAILY_CAP", "10")),
        min_gap_seconds=float(e.get("NOSMS_WHATSAPP_MIN_GAP_SECONDS", "120")),
        max_gap_seconds=float(e.get("NOSMS_WHATSAPP_MAX_GAP_SECONDS", "600")),
    )
