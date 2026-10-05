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
    store.create(message_id="m_1", pubkey="aa" * 32, dest="+15555550100",
                 rail="fake", price=DEFAULT_PRICE_SATS, change=0, escrow_token="cashuAesc",
                 status="sent", provider_message_id="p1")
    assert store.claim_refund("m_1", token="cashuArefund",
                              amount=DEFAULT_PRICE_SATS, reason="timeout") is True
    assert store.claim_refund("m_1", token="cashuAother",
                              amount=DEFAULT_PRICE_SATS, reason="timeout") is False
    assert store.get("m_1").refund_token == "cashuArefund"
    assert store.get("m_1").refund_amount == DEFAULT_PRICE_SATS
