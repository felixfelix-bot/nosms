"""Degrade path for the JMP rail (ADR-0002 compensating control #3).

"When the rail dies, the service must not die with it." On a JMP auth failure or
account termination the same ``Transport`` interface falls back to the
email-to-SMS rail, and **anything paid but unsent must be refundable**.

Refunds are the escrow layer's job (CEP-8 ``explicit_gating`` refunds when
``SendResult.accepted`` is False). This wrapper's job is to keep that flag
*honest*:

======================  ==========================  ============================
situation               returns                     refund?
======================  ==========================  ============================
primary accepted        accepted, rail=jmp          no (sent)
primary down, fallback  accepted, rail=email        no (sent, degraded)
  accepted
both fail               accepted=False              **yes**
destination not served  accepted=False              **yes** (paid, undeliverable)
==============================  ===================  ============================

Two deliberate non-degradations:

* a **paced** primary (:class:`RailPaced`) propagates. Degrading around pacing
  would defeat the whole point (protecting the personal line) and hand the load
  to the email rail's own abuse surface. The caller answers ``429``/``Retry-After``.
* a **destination** error on the primary is a request error, decided before any
  money moves — it is re-raised, never silently re-routed.
"""
from __future__ import annotations

from .base import Capabilities, SendResult
from .errors import RailPaced, UnsupportedDestination
from .jmp_cheogram import is_rail_down

__all__ = ["FailoverTransport"]


class FailoverTransport:
    """Primary rail with an email degrade path behind the same interface."""

    def __init__(self, primary, fallback, *,
                 overrides: dict | None = None):
        self.primary = primary
        self.fallback = fallback
        #: kwargs merged into the fallback send (e.g. the recipient's carrier)
        self.overrides = dict(overrides or {})
        self.name = f"failover:{primary.name}->{fallback.name}"
        self.degraded_count = 0

    # --- capability flags: whatever rail is actually serving --------------

    @property
    def capabilities(self) -> Capabilities:
        primary = self.primary.capabilities
        if primary.available:
            return primary
        return self.fallback.capabilities

    def capability_flags(self) -> dict:
        caps = self.capabilities
        return {
            "rail": self.name,
            "serving_rail": (self.primary.name if self.primary.capabilities.available
                             else self.fallback.name),
            "available": caps.available,
            "best_effort": caps.best_effort,
            "delivery_receipts": caps.delivery_receipts,
            "countries": list(caps.countries),
            "degraded_sends": self.degraded_count,
            "primary_down": not self.primary.capabilities.available,
        }

    # --- sending ----------------------------------------------------------

    def send(self, dest: str, body: str, **kwargs) -> SendResult:
        if self.primary.capabilities.available:
            result = self.primary.send(dest, body, **kwargs)
            if result.accepted or not is_rail_down(result):
                # sent, or a genuine per-send failure the escrow will refund.
                return result
            # The rail just went down: fall through and degrade this send too.
        return self._degrade(dest, body, **kwargs)

    def _degrade(self, dest: str, body: str, **kwargs) -> SendResult:
        merged = {**self.overrides, **kwargs}
        try:
            result = self.fallback.send(dest, body, **merged)
        except UnsupportedDestination as exc:
            # Paid but undeliverable on the fallback: refundable, and loud.
            return SendResult(
                accepted=False, rail=self.name, best_effort=True, receipt=None,
                detail=f"degraded_unsupported:{exc.reason}")
        self.degraded_count += 1
        if not result.accepted:
            # Both rails failed -> the payer must get their sats back.
            return SendResult(
                accepted=False, rail=self.name, best_effort=True, receipt=None,
                detail=f"all_rails_failed: primary={getattr(self.primary, 'down_reason', None)}"
                       f" fallback={result.detail}")
        return result

    @property
    def active_rail(self) -> str:
        return (self.primary.name if self.primary.capabilities.available
                else self.fallback.name)
