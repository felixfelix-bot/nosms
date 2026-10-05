"""Rail #4: the operator's JMP/Cheogram line (ADR-0002).

ADR-0002 makes the operator's own JMP/Cheogram line the **v1 rail** of the paid
public nosms service, overriding JMP's own "no automations" policy, at a
rugpull-risk-premium price, with a degrade path to the email rail.

The one link the reply-only listener never proved — a **cold** outbound send —
was verified live 2026-10-05 (`scripts/jmp_cold_send_probe.py`; raw stanza in
`evidence/cold-send-*.json`): a brand-new chat ``<message>`` addressed to
``+<E164>@cheogram.com`` (no reply/thread context) left the client with **no
error stanza**. That proves *acceptance*, never *delivery*.

Honesty rules baked in
---------------------
* Capability flags are read **from the rail's live state**, never hardcoded:
  :attr:`JmpCheogramTransport.capabilities` and
  :meth:`JmpCheogramTransport.capability_flags` are the single source the
  service serves to callers.
* ``best_effort=True`` and ``delivery_receipts=False`` are **permanent** on this
  rail — ``receipt`` is always ``None`` and nothing downstream may claim delivery.
* ``countries=["US", "CA"]`` — only a ``+1`` destination is accepted; anything
  else raises the machine token ``destination_unsupported``.
* Volume is paced (:mod:`app.transports.pacing`) because the line is personal.

One process, not two
--------------------
This rail takes a :class:`JmpLink`. The production link
(:class:`app.transports.jmp_link.SlixmppLink`) is a long-lived, in-process XMPP
client with its own sqlite inbox, so the process that serves the CVM tools is
the process holding the XMPP session — no second daemon to keep in step.
"""
from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from .base import Capabilities, SendResult
from .errors import RailPaced, RailUnavailable, UnsupportedDestination
from .pacing import Pacer

__all__ = ["JmpCheogramTransport", "JmpLink", "RAIL_UNAVAILABLE_PREFIX",
           "is_rail_down"]

#: Prefix that marks a SendResult as "the rail is down", not "this send failed".
RAIL_UNAVAILABLE_PREFIX = "rail_unavailable:"

#: Facts about the rail, not a policy knob — these never become True.
RAIL_COUNTRIES = ["US", "CA"]
RAIL_BEST_EFFORT = True
RAIL_DELIVERY_RECEIPTS = False


def _e164(dest: str) -> str:
    return "+" + "".join(ch for ch in dest if ch.isdigit())


def _is_us_ca(number: str) -> bool:
    return number.startswith("+1") and len(number) == 12


@runtime_checkable
class JmpLink(Protocol):
    """The XMPP seam. Production = SlixmppLink; tests = a fake.

    ``send_message`` returns ``None`` on acceptance and raises
    :class:`RailUnavailable` when the rail itself is the problem (auth,
    termination, no connection). It must not raise for a *bad destination* —
    that is decided before the link is touched.
    """

    def is_connected(self) -> bool: ...

    def send_message(self, to_jid: str, body: str, msg_id: str | None = None) -> None: ...


class JmpCheogramTransport:
    """Cold outbound SMS over the operator's JMP/Cheogram line."""

    name = "jmp_cheogram"

    def __init__(self, link: JmpLink, *, pacer: Pacer | None = None,
                 terminated_reasons: tuple[str, ...] = ("auth_failed", "terminated")):
        self.link = link
        self.pacer = pacer
        self._terminated_reasons = terminated_reasons
        self._down_reason: str | None = None
        self.accepted_count = 0
        self.rejected_count = 0

    # --- capabilities: served FROM the rail -------------------------------

    @property
    def capabilities(self) -> Capabilities:
        """Live flags. `available` follows the link and any terminal failure.

        `best_effort` / `delivery_receipts` / `countries` are facts about the
        rail itself — they are constants here *because the rail is the source*,
        not because a caller hardcoded them.
        """
        return Capabilities(
            available=bool(self._down_reason is None and self.link.is_connected()),
            best_effort=RAIL_BEST_EFFORT,
            delivery_receipts=RAIL_DELIVERY_RECEIPTS,
            countries=list(RAIL_COUNTRIES),
        )

    def capability_flags(self) -> dict:
        """Serialisable form for /api/health and the CVM `sms.capabilities` tool."""
        caps = self.capabilities
        return {
            "rail": self.name,
            "available": caps.available,
            "best_effort": caps.best_effort,
            "delivery_receipts": caps.delivery_receipts,
            "countries": list(caps.countries),
            "down_reason": self._down_reason,
            "countries_note": "US/Canada numbering only; SMS over SIP unsupported",
        }

    @property
    def down_reason(self) -> str | None:
        return self._down_reason

    def mark_down(self, reason: str) -> None:
        """Force the rail down (degrade path / operator kill-switch)."""
        self._down_reason = reason

    # --- sending ----------------------------------------------------------

    def send(self, dest: str, body: str, **kwargs) -> SendResult:
        """Send one cold SMS. Never claims delivery; never hides a down rail."""
        number = _e164(dest)
        if not _is_us_ca(number):
            raise UnsupportedDestination(
                "destination_unsupported",
                "JMP/Cheogram rail covers US/CA (+1) only")
        if not body:
            self.rejected_count += 1
            return SendResult(accepted=False, rail=self.name,
                              best_effort=RAIL_BEST_EFFORT, receipt=None,
                              detail="empty body")

        if self._down_reason is not None:
            self.rejected_count += 1
            return SendResult(accepted=False, rail=self.name,
                              best_effort=RAIL_BEST_EFFORT, receipt=None,
                              detail=f"{RAIL_UNAVAILABLE_PREFIX}{self._down_reason}")

        if self.pacer is not None:
            # Reserve the slot BEFORE the (blocking) send: check() then send()
            # then record_send() is a check-then-act race a threadpool turns
            # into a real cap violation. The claim persists the reservation, so
            # a second concurrent caller is refused instead of waved through.
            claim = self.pacer.claim()
            if not claim.allowed:
                # Never a failed SendResult: the message was not attempted, so
                # charging-then-refunding would be wrong. The caller defers.
                raise RailPaced(claim.reason, claim.retry_after_seconds,
                                detail=f"personal-line pacing: {claim.reason}")

        msg_id = uuid.uuid4().hex
        try:
            self.link.send_message(f"{number}@cheogram.com", body, msg_id)
        except RailUnavailable as exc:
            if self.pacer is not None:
                # The message never left the client: give the slot back.
                self.pacer.release(claim, accepted=False)
            if exc.reason in self._terminated_reasons:
                self.mark_down(exc.reason)
            self.rejected_count += 1
            return SendResult(accepted=False, rail=self.name,
                              best_effort=RAIL_BEST_EFFORT, receipt=None,
                              detail=f"{RAIL_UNAVAILABLE_PREFIX}{exc.reason}")

        if self.pacer is not None:
            self.pacer.release(claim, accepted=True)
        self.accepted_count += 1
        return SendResult(
            accepted=True, rail=self.name, best_effort=RAIL_BEST_EFFORT,
            receipt=None,
            detail="accepted by the JMP gateway; no delivery receipt exists",
        )


def is_rail_down(result: SendResult) -> bool:
    """True when a SendResult means "the rail is down", not "this send failed"."""
    return (not result.accepted
            and result.detail.startswith(RAIL_UNAVAILABLE_PREFIX))
