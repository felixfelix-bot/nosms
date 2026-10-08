"""Payment/refund adapter tests — escrow path, fake backend, zero sat movement.

The sat-moving half is a :class:`~nosms.testing.FakeLnBackend`; the network half
is the offline fixture transport from ``tests/conftest.py``. Nothing here can
reach 8333.mobi or a wallet.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import (
    MACHANKURA_CALLBACK,
    MACHANKURA_UNKNOWN_PHONE,
    MACHANKURA_UNKNOWN_URL,
    MACHANKURA_USER,
    MACHANKURA_USER_URL,
    fixture_bytes,
    make_transport,
)
from nosms import payments
from nosms.lnurl_pay import AmountOutOfRange, UnknownUser, resolve
from nosms.payments import (
    InMemoryRefundLedger,
    PaymentFailed,
    PaymentReceipt,
    payment_idempotency_key,
    pay_to_lightning_address,
    refund_key,
    refund_via_lightning_address,
)
from nosms.testing import FakeLnBackend

AMOUNT = 100_000  # msat == 100 sats, the price of a domestic SMS in PLAN.md


def _pay_request(transport, **overrides):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    return replace(pay_request, **overrides) if overrides else pay_request


# --------------------------------------------------------------------------- #
# key derivation
# --------------------------------------------------------------------------- #
def test_payment_idempotency_key_is_stable_and_domain_separated():
    key = payment_idempotency_key(MACHANKURA_USER, AMOUNT, "order-1")
    assert key == payment_idempotency_key(MACHANKURA_USER, AMOUNT, "order-1")
    assert key.startswith("nosms:pay:")
    assert key != payment_idempotency_key(MACHANKURA_USER, AMOUNT, "order-2")
    assert key != payment_idempotency_key(MACHANKURA_USER, AMOUNT + 1, "order-1")


def test_refund_key_prefers_an_explicit_id_and_otherwise_falls_back_to_the_reason():
    by_id = refund_key(MACHANKURA_USER, AMOUNT, "msg-1", "first reason")
    assert by_id == refund_key(MACHANKURA_USER, AMOUNT, "msg-1", "an entirely different reason")
    by_reason = refund_key(MACHANKURA_USER, AMOUNT, None, "  SMS   undeliverable ")
    assert by_reason == refund_key(MACHANKURA_USER, AMOUNT, None, "SMS undeliverable")
    assert by_reason != by_id
    assert by_reason.startswith("nosms:refund:")


# --------------------------------------------------------------------------- #
# payments
# --------------------------------------------------------------------------- #
def test_pay_to_lightning_address_returns_a_receipt(transport):
    backend = FakeLnBackend()

    receipt = pay_to_lightning_address(MACHANKURA_USER, AMOUNT, transport=transport, backend=backend)

    assert receipt.identifier == MACHANKURA_USER
    assert receipt.amount_msat == AMOUNT
    assert receipt.sats == 100
    assert receipt.invoice.startswith("ln")
    assert receipt.reference == "fake-1"
    assert receipt.status == "settled"
    assert receipt.is_machankura is True
    assert receipt.comment is None
    assert transport.calls == [MACHANKURA_USER_URL, f"{MACHANKURA_CALLBACK}?amount=100000"]
    assert backend.calls == [{"pr": receipt.invoice, "amount_msat": AMOUNT, "idempotency_key": ""}]


def test_pay_forwards_a_comment_and_a_nonce(transport):
    backend = FakeLnBackend()

    receipt = pay_to_lightning_address(
        MACHANKURA_USER, AMOUNT, "nosms-12", transport=transport, backend=backend, nonce="order-1"
    )

    assert receipt.comment == "nosms-12"
    assert transport.calls[-1] == f"{MACHANKURA_CALLBACK}?amount=100000&comment=nosms-12"
    assert backend.calls[0]["idempotency_key"] == payment_idempotency_key(MACHANKURA_USER, AMOUNT, "order-1")


def test_retrying_a_payment_with_the_same_nonce_is_refused_by_the_backend(transport):
    backend = FakeLnBackend()

    first = pay_to_lightning_address(
        MACHANKURA_USER, AMOUNT, transport=transport, backend=backend, nonce="order-1"
    )
    second = pay_to_lightning_address(
        MACHANKURA_USER, AMOUNT, transport=transport, backend=backend, nonce="order-1"
    )

    assert first.reference == second.reference
    assert second.status == "settled"
    assert len(backend.payments) == 1, "the backend must hold exactly one payment for one nonce"
    assert backend.call_count == 2


def test_pay_refuses_an_out_of_range_amount_without_touching_the_backend(transport):
    backend = FakeLnBackend()
    with pytest.raises(AmountOutOfRange):
        pay_to_lightning_address(MACHANKURA_USER, 10, transport=transport, backend=backend)
    assert backend.call_count == 0
    assert transport.calls == [MACHANKURA_USER_URL], "no invoice was ever requested"


@pytest.mark.parametrize("amount", [0, -5])
def test_pay_refuses_a_non_positive_amount(transport, amount):
    with pytest.raises(ValueError):
        pay_to_lightning_address(MACHANKURA_USER, amount, transport=transport, backend=FakeLnBackend())


@pytest.mark.parametrize("amount", ["100000", 100.5, None])
def test_pay_refuses_a_non_integer_amount(transport, amount):
    with pytest.raises(TypeError):
        pay_to_lightning_address(MACHANKURA_USER, amount, transport=transport, backend=FakeLnBackend())


def test_pay_can_reuse_an_already_resolved_pay_request(transport):
    backend = FakeLnBackend()
    pay_request = _pay_request(transport)
    transport.calls.clear()

    pay_to_lightning_address(MACHANKURA_USER, AMOUNT, transport=transport, backend=backend, pay_request=pay_request)

    assert transport.calls == [f"{MACHANKURA_CALLBACK}?amount=100000"], "no second resolution round-trip"


def test_pay_raises_when_the_backend_reports_a_failure(transport):
    backend = FakeLnBackend(status="failed")
    with pytest.raises(PaymentFailed) as excinfo:
        pay_to_lightning_address(MACHANKURA_USER, AMOUNT, transport=transport, backend=backend)
    assert excinfo.value.status == "failed"
    assert excinfo.value.pr.startswith("ln")


def test_pay_propagates_an_unknown_user(transport):
    transport = make_transport(
        discovery_url=MACHANKURA_UNKNOWN_URL,
        discovery_status=404,
        discovery_body=fixture_bytes("lnurlp_unknown_user_404.json"),
    )
    backend = FakeLnBackend()
    with pytest.raises(UnknownUser):
        pay_to_lightning_address(MACHANKURA_UNKNOWN_PHONE, AMOUNT, transport=transport, backend=backend)
    assert backend.call_count == 0


# --------------------------------------------------------------------------- #
# refunds
# --------------------------------------------------------------------------- #
def test_refund_pays_the_reason_as_a_comment(transport):
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()

    result = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=backend, ledger=ledger
    )

    assert result.replayed is False
    assert result.receipt.amount_msat == AMOUNT
    assert result.receipt.comment == "SMS undeliverable"
    assert result.receipt.comment_truncated is False
    assert result.receipt.is_machankura is True
    assert result.reason == "SMS undeliverable"
    assert transport.calls[-1] == f"{MACHANKURA_CALLBACK}?amount=100000&comment=SMS+undeliverable"
    assert ledger.keys() == [refund_key(MACHANKURA_USER, AMOUNT, None, "SMS undeliverable")]


def test_refund_truncates_an_overlong_reason_to_the_advertised_comment_limit(transport):
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()
    reason = "SMS undeliverable: " + "x" * 100

    result = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, reason, transport=transport, backend=backend, ledger=ledger
    )

    assert result.receipt.comment_truncated is True
    assert result.receipt.comment is not None
    assert len(result.receipt.comment) == 60
    assert result.receipt.comment == reason[:60]
    assert backend.call_count == 1


def test_refund_drops_the_reason_when_the_receiver_allows_no_comment(transport):
    transport = make_transport(
        discovery_body=(
            b'{"callback":"' + MACHANKURA_CALLBACK.encode() + b'","maxSendable":1000000000,"minSendable":1000,'
            b'"metadata":"[[\\"text/identifier\\",\\"sigidli@8333.mobi\\"]]","tag":"payRequest"}'
        )
    )
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()

    result = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=backend, ledger=ledger
    )

    assert result.receipt.comment is None
    assert result.receipt.comment_truncated is True
    assert transport.calls[-1] == f"{MACHANKURA_CALLBACK}?amount=100000"


def test_refund_resolves_immediately_before_paying(transport):
    """Order of operations: ledger → resolve → invoice → pay."""
    events: list = []
    transport = make_transport(events=events)
    backend = FakeLnBackend(events=events)

    refund_via_lightning_address(
        MACHANKURA_USER,
        AMOUNT,
        "SMS undeliverable",
        transport=transport,
        backend=backend,
        ledger=InMemoryRefundLedger(),
    )

    assert [event[0] for event in events] == ["get", "get", "pay_invoice"]
    assert events[0][1] == MACHANKURA_USER_URL
    assert events[1][1] == f"{MACHANKURA_CALLBACK}?amount=100000&comment=SMS+undeliverable"
    assert events[2][1] == AMOUNT


def test_rerunning_the_same_refund_never_pays_twice(transport):
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()

    first = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=backend, ledger=ledger
    )
    calls_after_first = list(transport.calls)
    second = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=backend, ledger=ledger
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.receipt == first.receipt, "a replayed refund returns the recorded receipt unchanged"
    assert backend.call_count == 1, "the backend must be asked exactly once"
    assert backend.total_paid_msat == AMOUNT
    assert transport.calls == calls_after_first, "a replayed refund does not even re-resolve"


def test_refund_idempotency_survives_a_changed_reason(transport):
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()

    first = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable",
        transport=transport, backend=backend, ledger=ledger, refund_id="msg-1",
    )
    second = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "operator cancelled the order",
        transport=transport, backend=backend, ledger=ledger, refund_id="msg-1",
    )

    assert second.replayed is True
    assert second.receipt == first.receipt
    assert backend.call_count == 1


def test_two_genuinely_different_refunds_do_pay_twice(transport):
    """The fallback key is per-reason; distinct refunds must both settle."""
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()

    refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=backend, ledger=ledger
    )
    refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "duplicate order", transport=transport, backend=backend, ledger=ledger
    )

    assert backend.call_count == 2
    assert backend.total_paid_msat == 2 * AMOUNT


def test_a_failed_refund_is_not_recorded_and_a_retry_can_settle(transport):
    ledger = InMemoryRefundLedger()
    failing = FakeLnBackend(status="failed")

    with pytest.raises(PaymentFailed):
        refund_via_lightning_address(
            MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=failing, ledger=ledger
        )
    assert ledger.keys() == [], "nothing may be recorded for a failed payment"

    working = FakeLnBackend()
    result = refund_via_lightning_address(
        MACHANKURA_USER, AMOUNT, "SMS undeliverable", transport=transport, backend=working, ledger=ledger
    )
    assert result.replayed is False
    assert working.call_count == 1
    assert len(ledger.keys()) == 1


def test_refund_propagates_an_unknown_user_without_paying(transport):
    transport = make_transport(
        discovery_url=MACHANKURA_UNKNOWN_URL,
        discovery_status=404,
        discovery_body=fixture_bytes("lnurlp_unknown_user_404.json"),
    )
    backend = FakeLnBackend()
    ledger = InMemoryRefundLedger()

    with pytest.raises(UnknownUser):
        refund_via_lightning_address(
            MACHANKURA_UNKNOWN_PHONE, AMOUNT, "SMS undeliverable",
            transport=transport, backend=backend, ledger=ledger,
        )

    assert backend.call_count == 0
    assert ledger.keys() == []


@pytest.mark.parametrize("amount", [0, -1])
def test_refund_refuses_a_non_positive_amount(transport, amount):
    with pytest.raises(ValueError):
        refund_via_lightning_address(
            MACHANKURA_USER, amount, "SMS undeliverable",
            transport=transport, backend=FakeLnBackend(), ledger=InMemoryRefundLedger(),
        )


@pytest.mark.parametrize("amount", ["100000", None])
def test_refund_refuses_a_non_integer_amount(transport, amount):
    with pytest.raises(TypeError):
        refund_via_lightning_address(
            MACHANKURA_USER, amount, "SMS undeliverable",
            transport=transport, backend=FakeLnBackend(), ledger=InMemoryRefundLedger(),
        )


def test_refund_requires_a_string_reason(transport):
    with pytest.raises(TypeError):
        refund_via_lightning_address(
            MACHANKURA_USER, AMOUNT, 12345,
            transport=transport, backend=FakeLnBackend(), ledger=InMemoryRefundLedger(),
        )


def test_refund_accepts_a_generic_non_machankura_address(transport):
    transport = make_transport(
        discovery_url="https://wallet.example/.well-known/lnurlp/alice",
        discovery_body=(
            b'{"callback":"https://wallet.example/.well-known/lnurlp/alice","maxSendable":1000000000,'
            b'"minSendable":1000,"metadata":"[[\\"text/identifier\\",\\"alice@wallet.example\\"]]",'
            b'"tag":"payRequest"}'
        ),
        callback_url="https://wallet.example/.well-known/lnurlp/alice",
    )
    backend = FakeLnBackend()

    result = refund_via_lightning_address(
        "alice@wallet.example", AMOUNT, "SMS undeliverable",
        transport=transport, backend=backend, ledger=InMemoryRefundLedger(),
    )

    assert result.receipt.is_machankura is False, "a generic lightning address is not somebody's phone"
    assert result.receipt.comment is None
    assert result.receipt.comment_truncated is True


# --------------------------------------------------------------------------- #
# ledger + backend doubles
# --------------------------------------------------------------------------- #
def test_in_memory_ledger_get_put_and_keys():
    ledger = InMemoryRefundLedger()
    assert ledger.get("missing") is None
    receipt = PaymentReceipt(
        identifier=MACHANKURA_USER, amount_msat=AMOUNT, invoice="lnbc1", reference="r", status="settled"
    )
    ledger.put("k", receipt)
    assert ledger.get("k") == receipt
    assert ledger.keys() == ["k"]
    assert InMemoryRefundLedger({"seed": receipt}).get("seed") == receipt


def test_fake_backend_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        FakeLnBackend(status="maybe")


def test_fake_backend_propagates_an_injected_exception():
    backend = FakeLnBackend(fail_with=RuntimeError("wallet offline"))
    with pytest.raises(RuntimeError):
        backend.pay_invoice("lnbc1", AMOUNT, idempotency_key="k")
    assert backend.call_count == 1


def test_fake_backend_records_fees_and_never_reuses_a_reference():
    backend = FakeLnBackend(fee_msat=1000)
    first = backend.pay_invoice("lnbc1", AMOUNT, idempotency_key="a")
    second = backend.pay_invoice("lnbc2", AMOUNT, idempotency_key="b")
    assert (first.reference, second.reference) == ("fake-1", "fake-2")
    assert second.fee_msat == 1000
    assert backend.total_paid_msat == 2 * AMOUNT


def test_fake_backend_does_not_cache_a_failure():
    backend = FakeLnBackend(status="failed")
    backend.pay_invoice("lnbc1", AMOUNT, idempotency_key="k")
    assert backend.payments == {}


def test_payment_receipt_is_frozen():
    receipt = PaymentReceipt(
        identifier=MACHANKURA_USER, amount_msat=AMOUNT, invoice="lnbc1", reference="r", status="settled"
    )
    with pytest.raises(Exception):
        receipt.amount_msat = 1  # type: ignore[misc]


def test_payments_module_exposes_no_http_default(monkeypatch):
    """Guard-rail: the public helpers require an explicit transport."""
    import inspect

    for func in (pay_to_lightning_address, refund_via_lightning_address):
        assert "transport" in inspect.signature(func).parameters
        assert inspect.signature(func).parameters["transport"].default is inspect.Parameter.empty
    assert payments.DEFAULT_TIMEOUT > 0
