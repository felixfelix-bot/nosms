"""Test doubles for the sats path — usable from any suite (unit, e2e, dev).

These never touch a network and never move a sat. ``FakeLnBackend`` also
enforces the :class:`nosms.payments.LnBackend` idempotency contract, so a test
can prove that a retried payment is refused at the *backend* level too, not
merely skipped by the ledger.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from .payments import BackendPayment

__all__ = ["FakeLnBackend"]


class FakeLnBackend:
    """An :class:`~nosms.payments.LnBackend` that records calls and pays nothing."""

    def __init__(
        self,
        status: str = "settled",
        *,
        fee_msat: int = 0,
        fail_with: Optional[BaseException] = None,
        events: Optional[list[Any]] = None,
    ) -> None:
        if status not in ("pending", "settled", "failed"):
            raise ValueError(f"unknown status {status!r}")
        self.status = status
        self.fee_msat = fee_msat
        self.fail_with = fail_with
        self.calls: list[dict[str, Any]] = []
        self.payments: dict[str, BackendPayment] = {}
        #: optional shared event log, so a test can assert the *order* of
        #: transport and backend operations (e.g. "resolve, then pay").
        self.events: list[Any] = events if events is not None else []

    def pay_invoice(self, pr: str, amount_msat: int, *, idempotency_key: str) -> BackendPayment:
        self.calls.append({"pr": pr, "amount_msat": amount_msat, "idempotency_key": idempotency_key})
        self.events.append(("pay_invoice", amount_msat, idempotency_key))
        if self.fail_with is not None:
            raise self.fail_with
        if idempotency_key and idempotency_key in self.payments:
            return replace(self.payments[idempotency_key], deduplicated=True)

        payment = BackendPayment(
            reference=f"fake-{len(self.calls)}",
            status=self.status,
            fee_msat=self.fee_msat,
        )
        if idempotency_key and self.status != "failed":
            self.payments[idempotency_key] = payment
        return payment

    @property
    def total_paid_msat(self) -> int:
        """Sum of what the fake would have moved — the "no double pay" assertion."""
        return sum(call["amount_msat"] for call in self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)
