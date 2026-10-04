"""RED-first tests for GET /api/message/:id/status.

The normalised status comes from the transport's provider status; the provider's
own raw string is exposed alongside it, because "delivered" we cannot observe is
the one thing this service must never emit.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

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


def _status(client, message_id, sk_hex=None):
    url = f"{BASE_URL}/api/message/{message_id}/status"
    kw = {"extra_tags": [["nonce", uuid4().hex]]}
    if sk_hex:
        kw["sk_hex"] = sk_hex
    headers = {"Authorization": nip98_header(url=url, method="GET", **kw)}
    return client.get(url, headers=headers)


@pytest.fixture()
def sent(tmp_path):
    mint = StubMint(fee_ppk=0)
    transport = FakeTransport(default_status="sent", default_raw="accepted")
    app = make_app(tmp_path, transport=transport, mint=mint)
    client = TestClient(app)
    r = _send(client, make_token([128]))
    assert r.status_code == 200, r.text
    return client, app, transport, r.json()["message_id"]


def test_status_requires_nip98(sent):
    client, app, transport, mid = sent
    r = client.get(f"/api/message/{mid}/status")
    assert r.status_code == 401
    assert r.headers["x-reason"] == "auth_missing"


def test_status_reports_normalised_and_raw_provider_status(sent):
    client, app, transport, mid = sent
    receipt = transport.sent and app.state.escrow.get(mid).provider_message_id
    assert receipt, "the rail must hand back a provider message id to poll"
    r = _status(client, mid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message_id"] == mid
    assert body["status"] in ("queued", "sent", "delivered", "failed")
    assert "provider_status" in body          # the provider's own string
    assert body["provider"] == "fake"
    assert body["price_sats"] == 100


def test_status_polls_the_provider_live_and_can_move_to_delivered(sent):
    client, app, transport, mid = sent
    rec = app.state.escrow.get(mid)
    transport.set_status(rec.provider_message_id, "delivered", raw="DELIVRD")
    r = _status(client, mid)
    assert r.json()["status"] == "delivered"
    assert r.json()["provider_status"] == "DELIVRD"
    # the poll result is persisted, not recomputed from a stale row
    assert app.state.escrow.get(mid).status == "delivered"


def test_status_exposes_failure_and_refund_state(tmp_path):
    mint, transport = StubMint(fee_ppk=0), FakeTransport(accept=False)
    app = make_app(tmp_path, transport=transport, mint=mint)
    client = TestClient(app)
    assert _send(client, make_token([128])).status_code == 502
    mid = app.state.escrow.list_all()[0].message_id
    body = _status(client, mid).json()
    assert body["status"] == "failed"
    assert body["refunded"] is True
    assert body["refund"]["amount_sats"] == 128


def test_status_is_404_for_another_identity(sent):
    client, app, transport, mid = sent
    r = _status(client, mid, sk_hex="22" * 32)
    assert r.status_code == 404
    assert r.headers["x-reason"] == "not_found"


def test_status_is_404_for_an_unknown_message(sent):
    client, *_ = sent
    r = _status(client, "m_deadbeefdeadbeef")
    assert r.status_code == 404
    assert r.headers["x-reason"] == "not_found"
