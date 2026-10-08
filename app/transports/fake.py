"""Deterministic in-memory rail for tests.

Not a simulation of a vendor - a double. It is deliberately NOT best-effort so
that a test asserting "the email rail is best-effort" cannot pass by accident
against the fake.

Unlike the M1a version it also carries a *status timeline*: each accepted
receipt starts at ``default_status`` and can be moved with ``set_status``, which
is what lets the status endpoint and the refund sweep be tested against a rail
that can actually report delivery.
"""
from __future__ import annotations

from .base import Capabilities, SendResult, TransportStatus, normalize_status


class FakeTransport:
    name = "fake"

    def __init__(self, accept: bool = True, default_status: str = "delivered",
                 default_raw: str | None = None):
        self.sent: list[tuple[str, str]] = []
        self.accept = accept
        self.default_status = normalize_status(default_status)
        self.default_raw = default_raw
        self.statuses: dict[str, tuple[str, str]] = {}
        self.status_calls: list[str] = []

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(available=True, best_effort=False,
                            delivery_receipts=True, countries=["*"])

    def set_status(self, receipt: str, normalized: str, raw: str | None = None) -> None:
        norm = normalize_status(normalized)
        self.statuses[receipt] = (norm, raw if raw is not None else norm)

    def send(self, dest: str, body: str, **kwargs) -> SendResult:
        if not dest or not body:
            return SendResult(accepted=False, rail=self.name, best_effort=False,
                              receipt=None, detail="dest and body are required",
                              status="failed")
        if not self.accept:
            return SendResult(accepted=False, rail=self.name, best_effort=False,
                              receipt=None, detail="fake configured to reject",
                              status="failed")
        self.sent.append((dest, body))
        receipt = f"fake-{len(self.sent)}"
        self.statuses.setdefault(receipt, (self.default_status,
                                           self.default_raw or self.default_status))
        return SendResult(accepted=True, rail=self.name, best_effort=False,
                          receipt=receipt, detail=self.default_status,
                          status=self.default_status)

    def status(self, receipt: str) -> TransportStatus:
        self.status_calls.append(receipt)
        norm, raw = self.statuses.get(receipt, ("unknown", "unknown"))
        return TransportStatus(normalized=norm, raw=raw, detail="")
