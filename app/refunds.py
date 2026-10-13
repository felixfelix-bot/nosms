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
    """A rail can only be trusted to report non-delivery if it is *usable*.

    An unavailable rail (no credential, unreachable provider) answers "unknown"
    to every status poll. Treating that as evidence of non-delivery would refund
    messages that were in fact delivered — the opposite of the honesty rule.
    """
    caps = getattr(transport, "capabilities", None)
    return bool(getattr(caps, "delivery_receipts", False)) and bool(getattr(caps, "available", False))


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
    """Refund every message older than the window that is provably undelivered.

    Refund is a claim about *proof*, never about silence. Three states prove
    non-delivery, in order of trust:

    1. the row's own ``failed`` verdict — set by the send handler on every
       failure path (a refused send, a rail exception, a paced rail). The
       message never left, receipt or no receipt, rail observable or not.
       This is the F1 case: before it was added, a send that failed before
       obtaining a receipt was counted ``unobservable`` and skipped by every
       sweep forever — postage locked in the ledger.
    2. a polled ``failed`` from the rail (the receipt exists, the rail says
       it did not deliver);
    3. an observable rail's silence — a row that is still not ``delivered``
       on a rail that *can* report delivery, after the window.

    Silence on a rail that cannot report delivery is *never* proof, and a
    status we could not classify (``unknown``) is *never* proof either: it
    could mean delivered. The two are counted separately — ``unobservable``
    (the rail cannot observe) and ``unclassifiable`` (we could not read the
    answer) — so the report never conflates them.
    """
    moment = float(now if now is not None else time.time())
    out = {"checked": 0, "refunded": 0, "delivered": 0, "too_early": 0,
           "skipped_already_refunded": 0, "unobservable": 0, "unclassifiable": 0,
           "failed": 0}
    observable = receipts_observable(transport) and hasattr(transport, "status")
    for record in store.list_unrefunded(limit=limit):
        if record.refunded:                      # raced with another sweep
            out["skipped_already_refunded"] += 1
            continue
        if moment - float(record.created_at) < refund_after_seconds:
            out["too_early"] += 1
            continue
        out["checked"] += 1
        # (1) the row's own recorded verdict outranks anything a later poll
        # could say: it was written by the handler that saw the send fail.
        # A refund here needs no receipt — the send never happened.
        if record.status == "failed":
            try:
                if refund_all(store, transport, record.message_id, "failed_send",
                              now=moment):
                    out["refunded"] += 1
                else:
                    out["skipped_already_refunded"] += 1
            except Exception:                                # noqa: BLE001
                out["failed"] += 1
            continue
        if not record.provider_message_id or not observable:
            out["unobservable"] += 1
            continue
        try:
            status = resolve(transport.status(record.provider_message_id))
        except Exception:                                    # noqa: BLE001
            out["unobservable"] += 1
            continue
        normalized = str(getattr(status, "normalized", "unknown") or "unknown")
        raw = str(getattr(status, "raw", normalized) or normalized)
        if normalized == "unknown":
            # (F2) we cannot classify the rail's answer; it proves nothing.
            # Never refund on it, never fold it into `unobservable`. The poll
            # is still persisted (below the guard) — the live status endpoint
            # persists unknown polls too, and the operator keeps the raw
            # string — but a terminal verdict is never downgraded by one.
            if record.status != "delivered" and (normalized != record.status
                                                 or raw != record.provider_status):
                store.update_status(record.message_id, status=normalized,
                                    provider_status=raw)
            out["unclassifiable"] += 1
            continue
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
        except Exception:                                    # noqa: BLE001
            out["failed"] += 1
    return out
