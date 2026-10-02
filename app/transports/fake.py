"""Deterministic in-memory rail for tests.

Not a simulation of a vendor - a double. It is deliberately NOT best-effort so
that a test asserting "the email rail is best-effort" cannot pass by accident
against the fake.
"""
from __future__ import annotations

from .base import Capabilities, SendResult


class FakeTransport:
    name = "fake"

    def __init__(self, accept: bool = True):
        self.sent: list[tuple[str, str]] = []
        self.accept = accept

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(available=True, best_effort=False,
                            delivery_receipts=True, countries=["*"])

    def send(self, dest: str, body: str, **kwargs) -> SendResult:
        if not dest or not body:
            return SendResult(accepted=False, rail=self.name, best_effort=False,
                              receipt=None, detail="dest and body are required")
        if not self.accept:
            return SendResult(accepted=False, rail=self.name, best_effort=False,
                              receipt=None, detail="fake configured to reject")
        self.sent.append((dest, body))
        return SendResult(accepted=True, rail=self.name, best_effort=False,
                          receipt=f"fake-{len(self.sent)}", detail="delivered")
