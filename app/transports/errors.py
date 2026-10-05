"""Rail-level error types shared by every nosms transport.

Kept in one module so a caller can catch a single class regardless of which
rail raised it — the previous shape had ``UnsupportedDestination`` living inside
the email rail, which would have forced the JMP rail (and the degrade path) to
import the email rail just to catch its own destination error.
"""
from __future__ import annotations

__all__ = ["UnsupportedDestination", "RailUnavailable", "RailPaced"]


class UnsupportedDestination(Exception):
    """The rail cannot serve this destination, with a machine reason token.

    Caller-facing: ``destination_unsupported`` / ``carrier_unknown``. This is a
    *request* problem, decided before any money moves — not a refund event.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class RailUnavailable(Exception):
    """The rail itself is down: the message was **not** sent.

    ``reason`` is a token the degrade path branches on:
    ``auth_failed`` | ``terminated`` | ``not_connected`` | ``connection_lost``.
    Anything paid for a send that raised this must become refundable.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class RailPaced(Exception):
    """The rail is deliberately holding back — retry later, do **not** refund.

    Raised (never returned as a failed SendResult) precisely because
    ``SendResult.refundable`` is ``not accepted``: a paced message was never
    attempted, so charging-and-refunding would be wrong. The caller should
    answer ``429`` with ``Retry-After`` and let the payer retry.
    """

    def __init__(self, reason: str, retry_after_seconds: float,
                 detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.detail = detail

    @property
    def retry_after(self) -> int:
        return max(0, int(self.retry_after_seconds + 0.999))
