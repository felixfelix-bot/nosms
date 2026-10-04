"""Auto-refund sweep for messages that were paid for but never delivered.

Two callers, one rule:

* the periodic sweep (``scripts/refund_sweep.py``, T+15 min) — anything still not
  delivered inside the window gets its postage + change back;
* the send handler, for a hard transport failure — the rail refused the message
  outright, so the sats go back immediately rather than waiting for the timer.

Both go through ``EscrowStore.claim_refund``, so a message is refunded at most
once no matter how many sweeps overlap.

Honesty rule: a rail that cannot observe delivery (``delivery_receipts=False``,
e.g. the email-to-SMS rail) is *never* auto-refunded. We cannot prove it did not
deliver, and refunding on silence would pay out for messages that did arrive.
Such messages are counted as ``unobservable`` instead.
"""
from __future__ import annotations

import asyncio
import inspect
import time

from .cashu import decode_token, encode_token
from .escrow import EscrowRecord


def resolve(value):
    """Call sync and async transports with one code path (the sweep is sync)."""
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def receipts_observable(transport) -> bool:
    caps = getattr(transport, "capabilities", None)
    return bool(getattr(caps, "delivery_receipts", False))


def held_token(record: EscrowRecord, *, include_change: bool = True) -> dict:
    """The proofs a refund would hand back, and the mint they live at."""
    if not record.escrow_token:
        raise ValueError(f"escrow record {record.message_id} holds no proofs")
    escrow = decode_token(record.escrow_token)
    proofs = list(escrow.proofs)
    if include_change and record.change_token:
        proofs += list(decode_token(record.change_token).proofs)
    amount = sum(int(p["amount"]) for p in proofs)
    return {"mint": escrow.mint, "proofs": proofs, "amount": amount}


def refund_all(store, transport, message_id: str, reason: str, *,
               include_change: bool = True, now: float | None = None) -> bool:
    """Refund everything held for one message. Idempotent. True if we claimed it."""
    record = store.get(message_id)
    if record is None or record.refunded:
        return False
    held = held_token(record, include_change=include_change)
    token = encode_token(held["mint"], held["proofs"])
    return store.claim_refund(message_id, token=token, amount=held["amount"], reason=reason,
                              now=now)


def sweep_refunds(store, transport, *, refund_after_seconds: int = 900,
                  now: float | None = None, limit: int = 500) -> dict:
    """Refund every message older than the window that is still not delivered."""
    moment = float(now if now is not None else time.time())
    out = {"checked": 0, "refunded": 0, "delivered": 0, "too_early": 0,
           "skipped_already_refunded": 0, "unobservable": 0, "failed": 0}
    observable = receipts_observable(transport) and hasattr(transport, "status")
    for record in store.list_unrefunded(limit=limit):
        if record.refunded:                      # raced with another sweep
            out["skipped_already_refunded"] += 1
            continue
        if moment - float(record.created_at) < refund_after_seconds:
            out["too_early"] += 1
            continue
        out["checked"] += 1
        if not observable or not record.provider_message_id:
            out["unobservable"] += 1
            continue
        try:
            status = resolve(transport.status(record.provider_message_id))
        except Exception:                                        # noqa: BLE001
            out["unobservable"] += 1
            continue
        normalized = str(getattr(status, "normalized", "unknown") or "unknown")
        raw = str(getattr(status, "raw", normalized) or normalized)
        if normalized != record.status or raw != record.provider_status:
            store.update_status(record.message_id, status=normalized, provider_status=raw)
        if normalized == "delivered":
            out["delivered"] += 1
            continue
        try:
            if refund_all(store, transport, record.message_id, "not_delivered", now=moment):
                out["refunded"] += 1
            else:
                out["skipped_already_refunded"] += 1
        except Exception:                                        # noqa: BLE001
            out["failed"] += 1
    return out
