"""Transport contract for every nosms SMS rail.

A rail is only allowed to advertise what it can actually prove. The email-to-SMS
carrier rail, for example, cannot observe delivery - so it must report
``best_effort=True`` and ``delivery_receipts=False``, and it must never return a
``receipt``. Escrow/pricing code reads these flags and must not assume a
receipt exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """What a rail can actually do - never aspirational."""
    available: bool
    best_effort: bool
    delivery_receipts: bool
    countries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SendResult:
    """Outcome of one send attempt."""
    accepted: bool
    rail: str
    best_effort: bool
    receipt: str | None
    detail: str = ""

    @property
    def refundable(self) -> bool:
        """A hard failure the rail could detect is refundable; silence is not."""
        return not self.accepted


@runtime_checkable
class Transport(Protocol):
    name: str

    @property
    def capabilities(self) -> Capabilities: ...

    def send(self, dest: str, body: str, **kwargs) -> SendResult: ...
