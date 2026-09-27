"""Sats payment/refund adapter for the nosms escrow path.

This is the layer the escrow code calls::

    receipt = pay_to_lightning_address("254700000000@8333.mobi", 100_000, backend=node)
    ...
    refund = refund_via_lightning_address("254700000000@8333.mobi", 100_000,
                                          "SMS undeliverable", backend=node, ledger=ledger)

Design rules enforced here:

* **No secrets, no wallet, no sats in this module.** The only way value moves is
  through an injected :class:`LnBackend`. This card deliberately ships no real
  backend — the wallet wiring is a separate, funded decision.
* **Refunds resolve the address immediately before paying.** A Machankura
  lightning address belongs to a *phone number*, not to a person: the invoice
  is minted seconds before the payment, so a user who has left the service
  cannot receive a stale invoice.
* **A refund is irreversible.** Paying sats to a phone number is a final
  settlement to whoever controls that SIM at that moment. Every refund goes
  through a ledger so a re-run of the same refund never pays twice.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Union, runtime_checkable

from .lnurl_pay import (
    DEFAULT_TIMEOUT,
    Invoice,
    LightningAddress,
    LnurlError,
    PayRequest,
    Transport,
    request_invoice,
    resolve,
)

__all__ = [
    "PaymentError",
    "PaymentFailed",
    "BackendPayment",
    "LnBackend",
    "PaymentReceipt",
    "RefundResult",
    "RefundLedger",
    "InMemoryRefundLedger",
    "payment_idempotency_key",
    "refund_key",
    "pay_to_lightning_address",
    "refund_via_lightning_address",
]

AddressLike = Union[str, LightningAddress]


class PaymentError(Exception):
    """Base class for payment-adapter failures (protocol errors are :class:`LnurlError`)."""


class PaymentFailed(PaymentError):
    """The backend refused or failed the payment; nothing was recorded in the ledger."""

    def __init__(self, pr: str, status: str, reference: str = "", reason: str = "") -> None:
        detail = reason or status
        super().__init__(f"payment failed ({status}): {detail}")
        self.pr = pr
        self.status = status
        self.reference = reference
        self.reason = reason


@dataclass(frozen=True)
class BackendPayment:
    """What a backend reports back about a payment it was asked to make."""

    reference: str
    status: str = "pending"
    fee_msat: int = 0
    deduplicated: bool = False


@runtime_checkable
class LnBackend(Protocol):
    """A thing that can actually move sats.

    Contract:

    * ``pay_invoice`` returns a :class:`BackendPayment` whose ``status`` is one
      of ``"pending"``, ``"settled"`` or ``"failed"``.
    * Implementations MUST honour ``idempotency_key``: the same key twice must
      never move sats twice. The ledger in this module is best-effort
      bookkeeping; the backend is the layer that can make double-spend
      impossible even across a crash of this process.
    """

    def pay_invoice(self, pr: str, amount_msat: int, *, idempotency_key: str) -> BackendPayment:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class PaymentReceipt:
    """A completed payment, either outbound (us → user) or a paid invoice."""

    identifier: str
    amount_msat: int
    invoice: str
    reference: str
    status: str
    fee_msat: int = 0
    comment: Optional[str] = None
    comment_truncated: bool = False
    is_machankura: bool = False
    idempotency_key: str = field(default="", repr=False)

    @property
    def sats(self) -> float:
        return self.amount_msat / 1000


@dataclass(frozen=True)
class RefundResult:
    """Outcome of a refund attempt, plus whether it was replayed from the ledger."""

    receipt: PaymentReceipt
    replayed: bool = False
    reason: str = ""


@runtime_checkable
class RefundLedger(Protocol):
    """Minimal durable store: one refund key → the receipt that settled it."""

    def get(self, key: str) -> Optional[PaymentReceipt]:  # pragma: no cover - protocol
        ...

    def put(self, key: str, receipt: PaymentReceipt) -> None:  # pragma: no cover - protocol
        ...


class InMemoryRefundLedger:
    """Process-local ledger. Fine for one-process dev/test; not a durability story."""

    def __init__(self, initial: Optional[Mapping[str, PaymentReceipt]] = None) -> None:
        self._entries: dict[str, PaymentReceipt] = dict(initial or {})
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[PaymentReceipt]:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, receipt: PaymentReceipt) -> None:
        with self._lock:
            self._entries[key] = receipt

    def keys(self) -> list[str]:  # pragma: no cover - convenience
        with self._lock:
            return list(self._entries)


def _digest(*parts: object) -> str:
    basis = "|".join(str(part) for part in parts)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def payment_idempotency_key(identifier: str, amount_msat: int, nonce: str) -> str:
    """Stable key for an outbound payment: same inputs ⇒ same key ⇒ no double pay."""
    return f"nosms:pay:{_digest(identifier, amount_msat, nonce)}"


def refund_key(identifier: str, amount_msat: int, refund_id: Optional[str], reason: str) -> str:
    """Stable key for a refund.

    ``refund_id`` (an original message id, an escrow id, …) is the reliable
    basis — always pass it when one exists. Without it the key falls back to the
    normalised reason text, which is enough for the naive "re-run the same
    refund" case the escrow path hits, but cannot tell two genuinely distinct
    refunds of the same size with the same words apart.
    """
    if refund_id:
        return f"nosms:refund:{_digest(identifier, amount_msat, 'id', refund_id)}"
    return f"nosms:refund:{_digest(identifier, amount_msat, 'reason', _normalise(reason))}"


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _as_address(address: AddressLike) -> LightningAddress:
    return address if isinstance(address, LightningAddress) else LightningAddress.parse(address)


def _require_amount(amount_msat: object) -> int:
    if isinstance(amount_msat, bool) or not isinstance(amount_msat, int):
        raise TypeError(f"amount_msat must be an int, got {type(amount_msat).__name__}")
    if amount_msat <= 0:
        raise ValueError(f"amount_msat must be > 0, got {amount_msat}")
    return amount_msat


def _comment_for_reason(reason: str, comment_allowed: int) -> tuple[Optional[str], bool]:
    """Fit a refund reason into the receiver's ``commentAllowed`` budget.

    Unlike a payer-supplied comment (which is refused when it is too long — see
    :meth:`nosms.lnurl_pay.PayRequest.check_comment`) a refund reason is
    operational, not user data: we truncate it rather than failing the refund
    over an over-long sentence.
    """
    if not isinstance(reason, str):
        raise TypeError(f"reason must be a string, got {type(reason).__name__}")
    text = _normalise(reason)
    if comment_allowed <= 0:
        return None, bool(text)
    if not text:
        return None, False
    if len(text) <= comment_allowed:
        return text, False
    return text[:comment_allowed], True


def _receipt(address: LightningAddress, invoice: Invoice, payment: BackendPayment, key: str, truncated: bool) -> PaymentReceipt:
    return PaymentReceipt(
        identifier=address.identifier,
        amount_msat=invoice.amount_msat,
        invoice=invoice.pr,
        reference=payment.reference,
        status=payment.status,
        fee_msat=payment.fee_msat,
        comment=invoice.comment,
        comment_truncated=truncated,
        is_machankura=address.is_machankura,
        idempotency_key=key,
    )


def pay_to_lightning_address(
    address: AddressLike,
    amount_msat: int,
    comment: Optional[str] = None,
    *,
    transport: Transport,
    backend: LnBackend,
    timeout: float = DEFAULT_TIMEOUT,
    idempotency_key: Optional[str] = None,
    nonce: Optional[str] = None,
    pay_request: Optional[PayRequest] = None,
) -> PaymentReceipt:
    """Resolve, invoice and pay a lightning address. Returns the receipt.

    ``nonce`` (an order id, a message id, a request id) makes the payment
    idempotent at the backend: pass the same nonce to retry the same payment.
    Passing neither ``idempotency_key`` nor ``nonce`` asks the backend to pay
    with no dedupe basis at all — do that only for a payment you know cannot be
    retried.
    """
    amount = _require_amount(amount_msat)
    addr = _as_address(address)
    pr = pay_request if pay_request is not None else resolve(addr, transport=transport, timeout=timeout)
    invoice = request_invoice(pr, amount, comment, transport=transport, timeout=timeout)

    if idempotency_key is None and nonce is not None:
        idempotency_key = payment_idempotency_key(addr.identifier, amount, nonce)

    payment = backend.pay_invoice(invoice.pr, invoice.amount_msat, idempotency_key=idempotency_key or "")
    receipt = _receipt(addr, invoice, payment, idempotency_key or "", truncated=False)
    if payment.status == "failed":
        raise PaymentFailed(invoice.pr, payment.status, payment.reference)
    return receipt


def refund_via_lightning_address(
    address: AddressLike,
    amount_msat: int,
    reason: str,
    *,
    transport: Transport,
    backend: LnBackend,
    ledger: RefundLedger,
    refund_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> RefundResult:
    """Pay sats back to ``address``, at most once per refund key.

    The order of operations is deliberate: the ledger is consulted *first*, then
    the address is resolved and its invoice minted *immediately* before the
    backend is asked to pay. A re-run with the same key returns the recorded
    receipt with ``replayed=True`` and never touches the backend again — the
    double-refund guard the escrow auto-refund path depends on.

    Residual risk, stated plainly: if this process dies after the backend paid
    but before the ledger write, the guard is lost. That is exactly why
    :class:`LnBackend` must honour ``idempotency_key`` — the key this function
    passes is derived from the same refund basis, so the wallet itself refuses
    the second spend.
    """
    amount = _require_amount(amount_msat)
    if not isinstance(reason, str):
        raise TypeError(f"reason must be a string, got {type(reason).__name__}")
    addr = _as_address(address)
    key = refund_key(addr.identifier, amount, refund_id, reason)

    already = ledger.get(key)
    if already is not None:
        return RefundResult(receipt=already, replayed=True, reason=reason)

    # Fresh resolution immediately before paying: a Machankura address is a
    # phone number, and the person holding it can change (or leave the service).
    pay_request = resolve(addr, transport=transport, timeout=timeout)
    comment, truncated = _comment_for_reason(reason, pay_request.comment_allowed)
    invoice = request_invoice(pay_request, amount, comment, transport=transport, timeout=timeout)

    payment = backend.pay_invoice(invoice.pr, invoice.amount_msat, idempotency_key=key)
    if payment.status == "failed":
        # Nothing recorded: a retry with the same key must be able to try again.
        raise PaymentFailed(invoice.pr, payment.status, payment.reference)

    receipt = _receipt(addr, invoice, payment, key, truncated)
    ledger.put(key, receipt)
    return RefundResult(receipt=receipt, replayed=False, reason=reason)
