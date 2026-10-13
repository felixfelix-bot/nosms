"""RED-first tests for the T+15 min auto-refund sweep.

The wallet-emptying bug this guards against is a refund double-spend: a sweep
that runs twice (a timer that fires while the previous run is still going, or a
crashed run being retried) must refund a given message exactly once.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.cashu import decode_token
from app.escrow import EscrowStore
from app.pricing import DEFAULT_PRICE_SATS
from app.refunds import sweep_refunds
from app.transports import FakeTransport
from tests.conftest import BASE_URL, nip98_header
from tests.stubs import StubMint, make_app, make_token

SEND_URL = f"{BASE_URL}/api/send"


def _send(client, token, to="+15551234567", text="hi"):
    body = json.dumps({"to": to, "text": text}).encode()
    return client.post(SEND_URL, content=body, headers={
        "Authorization": nip98_header(url=SEND_URL, method="POST", body=body,
                                      extra_tags=[["nonce", uuid4().hex]]),
        "Content-Type": "application/json", "X-Cashu": token})


def _refund(client, message_id, sk_hex=None):
    url = f"{BASE_URL}/api/refund/{message_id}"
    kw = {"extra_tags": [["nonce", uuid4().hex]]}
    if sk_hex:
        kw["sk_hex"] = sk_hex
    return client.get(url, headers={"Authorization": nip98_header(url=url, method="GET", **kw)})


@pytest.fixture()
def env(tmp_path):
    mint, transport = StubMint(fee_ppk=0), FakeTransport()
    transport.set_status("fake-1", "sent", raw="accepted")   # accepted, not delivered
    app = make_app(tmp_path, transport=transport, mint=mint, refund_after_seconds=900)
    client = TestClient(app)
    r = _send(client, make_token([4096]))                    # 4096 sats, price 2900
    assert r.status_code == 200, r.text
    return client, app, transport, r.json()["message_id"]


def test_sweep_refunds_an_undelivered_message_once_even_if_run_twice(env):
    client, app, transport, mid = env
    later = app.state.escrow.get(mid).created_at + 901
    first = sweep_refunds(app.state.escrow, transport,
                          refund_after_seconds=900, now=later)
    assert first["refunded"] == 1
    rec = app.state.escrow.get(mid)
    assert rec.refunded_at is not None
    assert rec.refund_amount == 4096                         # price + change, all of it
    token_after_first = rec.refund_token
    assert token_after_first

    # a re-run (timer overlap, crash retry) must not move another sat
    second = sweep_refunds(app.state.escrow, transport,
                           refund_after_seconds=900, now=later + 60)
    assert second["refunded"] == 0
    assert second["checked"] == 0            # a refunded row is no longer offered
    assert app.state.escrow.get(mid).refund_token == token_after_first
    assert app.state.escrow.get(mid).refund_amount == 4096


def test_refund_token_is_worth_the_escrowed_amount_and_is_unspent(env):
    client, app, transport, mid = env
    sweep_refunds(app.state.escrow, transport, refund_after_seconds=900,
                  now=app.state.escrow.get(mid).created_at + 1000)
    r = _refund(client, mid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amount_sats"] == 4096
    token = decode_token(body["token"])
    assert token.amount == 4096
    # the payer can still spend it: none of its secrets were burned at the mint
    assert not (set(p["secret"] for p in token.proofs) & app.state.mint.spent_secrets())


def test_refund_is_only_retrievable_by_the_paying_identity(env):
    client, app, transport, mid = env
    sweep_refunds(app.state.escrow, transport, refund_after_seconds=900,
                  now=app.state.escrow.get(mid).created_at + 1000)
    assert _refund(client, mid, sk_hex="22" * 32).status_code == 404


def test_refund_before_it_exists_is_a_409_not_an_empty_200(env):
    client, app, transport, mid = env
    r = _refund(client, mid)
    assert r.status_code == 409
    assert r.headers["x-reason"] == "not_refunded"


def test_sweep_leaves_a_delivered_message_alone(env):
    client, app, transport, mid = env
    rec = app.state.escrow.get(mid)
    transport.set_status(rec.provider_message_id, "delivered", raw="DELIVRD")
    out = sweep_refunds(app.state.escrow, transport, refund_after_seconds=900,
                        now=rec.created_at + 1000)
    assert out["refunded"] == 0 and out["delivered"] == 1
    assert app.state.escrow.get(mid).refunded_at is None
    assert app.state.escrow.get(mid).status == "delivered"
    assert _refund(client, mid).status_code == 409


def test_sweep_does_nothing_before_the_window_closes(env):
    client, app, transport, mid = env
    rec = app.state.escrow.get(mid)
    out = sweep_refunds(app.state.escrow, transport, refund_after_seconds=900,
                        now=rec.created_at + 60)
    assert out["refunded"] == 0 and out["too_early"] == 1
    assert app.state.escrow.get(mid).refunded_at is None


def test_claim_refund_is_atomic_across_two_callers(tmp_path):
    """The store, not the caller, decides who wins — a row-level claim."""
    store = EscrowStore(str(tmp_path / "e.db"))
    store.create(message_id="m_1", pubkey="aa" * 32, dest="+155****0100",
                 rail="fake", price=DEFAULT_PRICE_SATS, change=0, escrow_token="cashuAesc",
                 status="sent", provider_message_id="p1")
    assert store.claim_refund("m_1", token="cashuArefund",
                              amount=DEFAULT_PRICE_SATS, reason="timeout") is True
    assert store.claim_refund("m_1", token="cashuAother",
                              amount=DEFAULT_PRICE_SATS, reason="timeout") is False
    assert store.get("m_1").refund_token == "cashuArefund"
    assert store.get("m_1").refund_amount == DEFAULT_PRICE_SATS


# --- F1: a failed send with no receipt is refundable, never "unobservable" ---
#
# The old predicate `not observable or not record.provider_message_id` was
# written for rails that cannot report delivery at all. But it also caught the
# send that failed *before a receipt ever existed* — a message we know was never
# sent. Those rows were skipped by every sweep forever: postage locked in the
# ledger, the payer's sats gone. `status="failed"` is the rail's own hard
# verdict (set by the handler on every failure path); it needs no receipt.

def _stranded_send_row(app, *, price=2900, change=1196, status="failed"):
    """A paid row whose send failed before any receipt existed (the F1 shape).

    This is exactly what the handler leaves when the immediate refund raises
    (or a process dies between escrow.create and refund_all): status=failed,
    no provider_message_id, not refunded. The escrowed proofs sum to price +
    change so the refund token covers exactly what was held.
    """
    escrow = app.state.escrow
    from tests.stubs import make_token as _mt
    escrow.create(message_id="m_strand", pubkey="aa" * 32, dest="+155****4567",
                  rail="fake", price=price, change=change,
                  escrow_token=_mt([2048, 512, 256, 128]),        # 2944
                  change_token=_mt([1024, 128, 32, 8, 4]),        # 1196
                  status=status)
    return app.state.escrow.get("m_strand")


def test_a_failed_send_that_never_got_a_receipt_is_refunded_by_the_sweep(tmp_path):
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0))
    rec = _stranded_send_row(app)
    assert rec.provider_message_id is None and rec.status == "failed"

    out = sweep_refunds(app.state.escrow, app.state.transport,
                        refund_after_seconds=900, now=rec.created_at + 901)

    assert out["refunded"] == 1, out
    assert out["unobservable"] == 0
    after = app.state.escrow.get("m_strand")
    assert after.refunded_at is not None
    assert after.refund_reason == "failed_send"
    assert after.refund_amount == 2944 + 1196        # everything held, all of it
    assert decode_token(after.refund_token).amount == 2944 + 1196


def test_a_stranded_row_stays_refundable_across_sweeps_not_skipped_forever(tmp_path):
    """The old bug's signature: the row is re-visited and skipped every time.

    Before the fix a failed-but-unreceipted row counted as `unobservable` on
    every sweep — never refunded, never resolved. Now the first eligible sweep
    resolves it, and a second sweep cannot pay twice.
    """
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0))
    rec = _stranded_send_row(app)
    later = rec.created_at + 901

    first = sweep_refunds(app.state.escrow, app.state.transport,
                          refund_after_seconds=900, now=later)
    second = sweep_refunds(app.state.escrow, app.state.transport,
                           refund_after_seconds=900, now=later + 3600)

    assert first["refunded"] == 1 and first["unobservable"] == 0
    assert second["refunded"] == 0                    # idempotency: at most once
    assert second["checked"] == 0                     # the row is no longer offered
    assert app.state.escrow.get("m_strand").refund_amount == 2944 + 1196


def test_a_queued_send_with_no_receipt_is_not_refunded_on_silence(tmp_path):
    """Only `failed` is receipt-free refundable; a queued row is not failed.

    A queued row without a receipt is the unobservable case that predates this
    fix (a rail that cannot report delivery, or a crash before the rail
    answered): silence must not pay out.
    """
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0))
    rec = _stranded_send_row(app, status="queued")
    out = sweep_refunds(app.state.escrow, app.state.transport,
                        refund_after_seconds=900, now=rec.created_at + 901)
    assert out["refunded"] == 0
    assert out["unobservable"] == 1                    # the honest bucket, no refund
    assert app.state.escrow.get("m_strand").refunded_at is None


def test_a_failed_row_on_an_unobservable_rail_is_still_refunded(tmp_path):
    """A rail that cannot report delivery never hides a *detected* failure.

    The old predicate skipped any row without a receipt regardless of the
    rail's capabilities; the failure status is proof of non-delivery even when
    the rail cannot observe deliveries (the email-to-SMS shape: it cannot say
    delivered, but it can say refused).
    """
    class _NoReceiptsRail(FakeTransport):
        @property
        def capabilities(self):
            from app.transports.base import Capabilities
            return Capabilities(available=True, best_effort=True,
                                delivery_receipts=False, countries=["*"])

    app = make_app(tmp_path, transport=_NoReceiptsRail(), mint=StubMint(fee_ppk=0))
    rec = _stranded_send_row(app)
    out = sweep_refunds(app.state.escrow, app.state.transport,
                        refund_after_seconds=900, now=rec.created_at + 901)
    assert out["refunded"] == 1, out
    assert app.state.escrow.get("m_strand").refund_reason == "failed_send"


# --- F2: `unknown` is not non-delivery — it is unclassifiable -----------------

def test_an_unknown_provider_status_is_not_refunded_and_gets_its_own_bucket(tmp_path):
    """A status we could not classify proves nothing; refunding it would pay
    out postage for messages that may have arrived. It must not be folded into
    `unobservable` either — 'the rail cannot observe' and 'we could not read
    the answer' are different facts and the report must distinguish them."""
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0))
    transport = app.state.transport
    escrow = app.state.escrow
    escrow.create(message_id="m_unk", pubkey="aa" * 32, dest="+155****4567",
                  rail="fake", price=2900, change=1196,
                  escrow_token=make_token([2048, 512, 256, 128]),
                  change_token=make_token([1024, 128, 32, 8, 4]),
                  status="sent", provider_message_id="fake-9")
    transport.set_status("fake-9", "some-new-provider-word")   # -> unknown

    rec = escrow.get("m_unk")
    out = sweep_refunds(escrow, transport, refund_after_seconds=900,
                        now=rec.created_at + 901)

    assert out["refunded"] == 0
    assert out["unclassifiable"] == 1, out               # explicit new bucket
    assert out["unobservable"] == 0
    assert escrow.get("m_unk").refunded_at is None        # no payout on "unknown"
    assert escrow.get("m_unk").status == "unknown"         # the poll was persisted


def test_an_unclassifiable_poll_never_undoes_a_recorded_failed_verdict(tmp_path):
    """A recorded `failed` verdict is proof the send never happened; a later
    poll we cannot classify must neither refund-block the row (that would
    re-strand it forever) nor rewrite the verdict. The verdict refunds; the
    unknown poll is never even asked."""
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0))
    transport = app.state.transport
    escrow = app.state.escrow
    escrow.create(message_id="m_unk2", pubkey="aa" * 32, dest="+155****4567",
                  rail="fake", price=2900, change=1196,
                  escrow_token=make_token([2048, 512, 256, 128]),
                  change_token=make_token([1024, 128, 32, 8, 4]),
                  status="failed", provider_status="transport_exception:OSError",
                  provider_message_id="fake-9")
    transport.set_status("fake-9", "some-new-provider-word")   # -> unknown

    rec = escrow.get("m_unk2")
    out = sweep_refunds(escrow, transport, refund_after_seconds=900,
                        now=rec.created_at + 901)
    assert out["refunded"] == 1, out
    assert out["unclassifiable"] == 0                    # the poll was never taken
    after = escrow.get("m_unk2")
    assert after.status == "failed"                      # verdict preserved
    assert after.refund_reason == "failed_send"


def test_a_failed_status_poll_still_refunds(tmp_path):
    """The rail's explicit `failed` verdict — now that a receipt exists —
    remains the refund trigger it always was (regression guard for F2)."""
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0))
    transport = app.state.transport
    escrow = app.state.escrow
    escrow.create(message_id="m_f9", pubkey="aa" * 32, dest="+155****4567",
                  rail="fake", price=2900, change=1196,
                  escrow_token=make_token([2048, 512, 256, 128]),
                  change_token=make_token([1024, 128, 32, 8, 4]),
                  status="sent", provider_message_id="fake-9")
    transport.set_status("fake-9", "failed", raw="undelivered")

    rec = escrow.get("m_f9")
    out = sweep_refunds(escrow, transport, refund_after_seconds=900,
                        now=rec.created_at + 901)
    assert out["refunded"] == 1, out
    assert escrow.get("m_f9").refund_reason == "not_delivered"
