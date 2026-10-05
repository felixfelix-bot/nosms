"""Transport contract for every nosms SMS rail.

A rail is only allowed to advertise what it can actually prove. The email-to-SMS
carrier rail, for example, cannot observe delivery - so it must report
``best_effort=True`` and ``delivery_receipts=False``, and it must never return a
``receipt``. Escrow/pricing code reads these flags and must not assume a
receipt exists.
"""
from __future__ import annotations

import inspect
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
    #: normalised status the rail reports at accept time (queued/sent/delivered/failed)
    status: str = "unknown"

    @property
    def refundable(self) -> bool:
        """A hard failure the rail could detect is refundable; silence is not."""
        return not self.accepted


#: statuses the service exposes. Everything the provider says is mapped into one
#: of these; the provider's own string is kept alongside as ``TransportStatus.raw``.
NORMALIZED_STATUSES = ("queued", "sent", "delivered", "failed", "unknown")

_STATUS_ALIASES = {
    "queued": "queued", "accepted": "sent", "sending": "queued", "scheduled": "queued",
    "sent": "sent", "submitted": "sent", "delivered": "delivered", "completed": "delivered",
    "delivery_failed": "failed", "failed": "failed", "undelivered": "failed",
    "rejected": "failed", "expired": "failed", "cancelled": "failed", "canceled": "failed",
    "unknown": "unknown",
}


def normalize_status(value) -> str:
    """Map any provider status (str or Enum) onto our five-value vocabulary."""
    if value is None:
        return "unknown"
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    if not text:
        return "unknown"
    return _STATUS_ALIASES.get(text, "unknown")


@dataclass(frozen=True)
class TransportStatus:
    """A polled status: our normalised value plus the provider's raw string."""
    normalized: str
    raw: str
    detail: str = ""


@runtime_checkable
class Transport(Protocol):
    name: str

    @property
    def capabilities(self) -> Capabilities: ...

    def send(self, dest: str, body: str, **kwargs) -> SendResult: ...


@runtime_checkable
class Pollable(Protocol):
    """A rail whose delivery status can be read back."""

    def status(self, receipt: str) -> TransportStatus: ...


async def maybe_await(value):
    """Call a rail that may be sync (Fake/email) or async (Telnyx) from a handler."""
    if inspect.isawaitable(value):
        return await value
    return value
