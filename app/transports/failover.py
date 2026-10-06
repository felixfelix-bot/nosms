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
======================  ===================  ====================================

What counts as "down" is narrowed deliberately
----------------------------------------------
The degrade path exists for a rail that will not come back on its own. A routine
reconnect is **not** that:

* **terminal** (``auth_failed`` / ``terminated``) — degrade. The account is gone
  or the credentials are wrong; retrying will not fix it.
* **transient** (``not_connected`` / ``connection_lost`` during a reconnect, or a
  destination/``carrier`` problem on the fallback) — do **not** degrade. Diverting
  paid traffic to the email rail during every reconnect window would hand the load
  to the email rail's own abuse surface, and the JMP link is designed to
  reconnect. The caller gets a refundable failure (``SendResult.accepted=False``),
  so the money is safe either way.

A genuine per-send failure that is not a rail-outage token (an empty body, say) is
returned untouched.

Both shapes of "the rail is down" are handled
---------------------------------------------
The JMP rail *returns* ``SendResult.detail == "rail_unavailable:<reason>"``; the
WhatsApp rail *raises* :class:`RailUnavailable` (ADR-0003: a ban must be loud and
must stop the rail, not be flattened into one more failed send). A primary that
raises must degrade exactly like one that returns, or wrapping the WhatsApp rail
in this wrapper would turn every ban into an unhandled exception.

Two deliberate non-degradations:

* a **paced** primary (:class:`RailPaced`) propagates. Degrading around pacing
  would defeat the whole point (protecting the personal line) and hand the load
  to the email rail's own abuse surface. The caller answers ``429``/``Retry-After``.
* a **destination** error on the primary is a request error, decided before any
  money moves — it is re-raised, never silently re-routed.
"""
from __future__ import annotations

import logging

from .base import Capabilities, SendResult
from .errors import RailPaced, RailUnavailable, UnsupportedDestination
from .jmp_cheogram import is_rail_down

__all__ = ["FailoverTransport"]

logger = logging.getLogger(__name__)

#: A rail-outage token that the degrade path treats as *terminal* — the JMP rail
#: will not recover by itself, so the same send moves to the email rail. Anything
#: else marked ``rail_unavailable:*`` is a transient window and does **not**
#: divert paid traffic away from the primary.
TERMINAL_RAIL_REASONS = ("auth_failed", "terminated")


def _reason_of(result: SendResult) -> str:
    """The ``rail_unavailable:<reason>`` token of a result, or ''."""
    detail = result.detail or ""
    return detail.split(":", 1)[1] if detail.startswith("rail_unavailable:") else ""


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
        if self._primary_serving():
            try:
                result = self.primary.send(dest, body, **kwargs)
            except UnsupportedDestination:
                # A request error, decided before any money moved: never re-routed.
                raise
            except RailPaced:
                # Pacing is not an outage: the caller defers (429/Retry-After).
                raise
            except RailUnavailable as exc:
                # A primary that RAISES (the WhatsApp rail, ADR-0003) must degrade
                # exactly like one that returns `rail_unavailable:<reason>` (the
                # JMP rail) — otherwise a ban would escape as a raw exception.
                if exc.reason in TERMINAL_RAIL_REASONS:
                    self._warn("primary rail terminal (raised): %s:%s",
                               exc.reason, exc.detail)
                    return self._degrade(dest, body, **kwargs)
                self._warn("primary rail transient (raised): %s:%s",
                           exc.reason, exc.detail)
                return SendResult(
                    accepted=False, rail=self.name, best_effort=True, receipt=None,
                    detail=f"primary_transient:rail_unavailable:{exc.reason}")
            if result.accepted or not is_rail_down(result):
                # sent, or a genuine per-send failure the escrow will refund.
                return result
            if _reason_of(result) not in TERMINAL_RAIL_REASONS:
                # A reconnect window, not a dead rail: do not divert paid
                # traffic to the email rail. Refundable, and loud about why.
                self._warn("primary rail transient: %s", result.detail)
                return SendResult(
                    accepted=False, rail=self.name, best_effort=True, receipt=None,
                    detail=f"primary_transient:{result.detail}")
            # The rail is terminally down: fall through and degrade this send too.
        return self._degrade(dest, body, **kwargs)

    def _primary_serving(self) -> bool:
        """True when the primary rail is up *now*.

        A primary that is not connected because it is mid-reconnect still gets the
        send: the send itself will fail with a transient token and be refunded,
        which is cheaper than diverting every send during every reconnect.
        """
        return self.primary.capabilities.available or \
            getattr(self.primary, "down_reason", None) is None

    def _degrade(self, dest: str, body: str, **kwargs) -> SendResult:
        merged = {**self.overrides, **kwargs}
        try:
            result = self.fallback.send(dest, body, **merged)
        except UnsupportedDestination as exc:
            # Paid but undeliverable on the fallback: refundable, and loud.
            return SendResult(
                accepted=False, rail=self.name, best_effort=True, receipt=None,
                detail=f"degraded_unsupported:{exc.reason}")
        except RailUnavailable as exc:
            # The fallback rail is down too. This must be a *visible* refundable
            # failure, not a raw exception the escrow layer happens to catch.
            self._warn("fallback rail unavailable: %s:%s", exc.reason, exc.detail)
            return SendResult(
                accepted=False, rail=self.name, best_effort=True, receipt=None,
                detail=f"all_rails_failed: primary={self._primary_state()}"
                       f" fallback=rail_unavailable:{exc.reason}")
        except Exception as exc:                       # noqa: BLE001 - loud, never silent
            self._warn("fallback raised %s: %s", type(exc).__name__, exc)
            return SendResult(
                accepted=False, rail=self.name, best_effort=True, receipt=None,
                detail=f"all_rails_failed: primary={self._primary_state()}"
                       f" fallback=exception:{type(exc).__name__}")
        if not result.accepted:
            # Both rails failed -> the payer must get their sats back.
            return SendResult(
                accepted=False, rail=self.name, best_effort=True, receipt=None,
                detail=f"all_rails_failed: primary={self._primary_state()}"
                       f" fallback={result.detail}")
        self.degraded_count += 1
        logger.warning("nosms degrade: send served by %s (primary %s)",
                       self.fallback.name, self._primary_state())
        return result

    def _primary_state(self) -> str:
        return f"{getattr(self.primary, 'name', 'primary')}:" \
               f"{getattr(self.primary, 'down_reason', None)}"

    def _warn(self, msg: str, *args) -> None:
        logger.warning("nosms failover: " + msg, *args)

    @property
    def active_rail(self) -> str:
        return (self.primary.name if self.primary.capabilities.available
                else self.fallback.name)
