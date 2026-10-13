"""RED-first tests for the honesty of the handler's immediate-refund path.

The send handler refunds postage the moment a rail reports a hard failure —
but it fires ``refund_all`` as fire-and-forget: the return value is discarded
and exceptions propagate. When the escrow write behind the refund raises
(sqlite locked by a concurrent sweep, a full disk, a dead NFS handle, a
process killed mid-transaction), the caller gets a 500, the row is left
``failed`` with no receipt, and — before the sweep fix — that row was skipped
by every sweep forever: postage locked in the ledger, the payer's sats gone.

These tests pin the handler side of that contract:

* a failed refund is still a *visible send failure* (the right status code and
  reason), never a 500 and never a claimed refund that did not happen;
* the row the failed refund leaves behind is refunded by a later sweep (F1).
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.cashu import decode_token
from app.refunds import sweep_refunds
from app.transports import FakeTransport
from tests.conftest import BASE_URL, nip98_header
from tests.stubs import StubMint, make_app, make_token

SEND_URL = f"{BASE_URL}/api/send"


def _send(client, token, to="+155****4567", text="hi"):
    body = json.dumps({"to": to, "text": text}).encode()
    return client.post(SEND_URL, content=body, headers={
        "Authorization": nip98_header(url=SEND_URL, method="POST", body=body,
                                      extra_tags=[["nonce", uuid4().hex]]),
        "Content-Type": "application/json", "X-Cashu": token})


class _RefusingRail(FakeTransport):
    """A rail that refuses every send with a hard, detected failure."""

    def send(self, dest, body, **kw):
        from app.transports.base import SendResult
        return SendResult(accepted=False, rail=self.name, best_effort=False,
                          receipt=None, detail="refused by test rail",
                          status="failed")


class _UnreliableRefundStore:
    """An escrow wrapper whose ``claim_refund`` raises once, then recovers.

    Models the transient sqlite failure the handler must survive: a locked
    database, a full disk, a process death mid-transaction. It forwards every
    other call to a real ``EscrowStore`` — including a *second* refund claim
    from the sweep, which must succeed once the transient fault clears.
    """

    def __init__(self, inner, exc):
        self._inner = inner
        self._exc = exc
        self.claims_attempted = 0
        self.refund_failures = 0
        self.raised = False

    def claim_refund(self, message_id, *, token, amount, reason, now=None):
        self.claims_attempted += 1
        if not self.raised:
            self.raised = True
            self.refund_failures += 1
            raise self._exc
        return self._inner.claim_refund(message_id, token=token, amount=amount,
                                        reason=reason, now=now)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture()
def failing_refund_app(tmp_path):
    """The app with an escrow whose first claim_refund raises OSError."""
    app = make_app(tmp_path, transport=_RefusingRail(), mint=StubMint(fee_ppk=0))
    original = app.state.escrow
    app.state.escrow = _UnreliableRefundStore(original, OSError("database is locked"))
    return app, TestClient(app)


def test_a_failed_immediate_refund_is_a_visible_transport_error_not_a_500(
        failing_refund_app):
    """The refund write raised, so the send failed AND nothing was returned:
    the caller must see the send failure (502 transport_error), not an
    unhandled 500 — and must not be told `refunded: true`."""
    app, client = failing_refund_app
    r = _send(client, token=make_token([4096]))

    assert r.status_code == 502, r.text
    assert r.headers["x-reason"] == "transport_error"
    body = r.json()
    assert body.get("refunded") is False, body
    assert body.get("refund_sats") is None, body


def test_the_row_a_failed_refund_leaves_behind_is_resolved_by_a_later_sweep(
        failing_refund_app):
    """F1 at the handler level: the immediate refund raised, the row is left
    status=failed with no receipt — the T+15 min sweep must pick it up, not
    skip it forever."""
    app, client = failing_refund_app
    r = _send(client, token=make_token([4096]))
    assert r.status_code == 502

    escrow = app.state.escrow
    mid = r.json()["message_id"]
    rec = escrow.get(mid)
    assert rec is not None
    assert rec.status == "failed"
    assert rec.provider_message_id is None
    assert rec.refunded_at is None

    out = sweep_refunds(escrow, app.state.transport,
                       refund_after_seconds=900, now=rec.created_at + 901)
    assert out["refunded"] == 1, out
    after = escrow.get(mid)
    assert after.refunded_at is not None
    assert after.refund_amount == 4096              # postage + change, all of it
    assert decode_token(after.refund_token).amount == 4096


def test_the_immediate_refund_when_it_works_is_reported_as_refunded(tmp_path):
    """The non-raise case stays honest too: the caller is told the refund
    happened because ``refund_all`` said so, not because we assume it."""
    app = make_app(tmp_path, transport=_RefusingRail(), mint=StubMint(fee_ppk=0))
    client = TestClient(app)
    r = _send(client, token=make_token([4096]))
    assert r.status_code == 502
    assert r.headers["x-reason"] == "transport_error"
    assert r.json()["refunded"] is True
    recs = app.state.escrow.list_all()
    assert len(recs) == 1 and recs[0].refunded_at is not None
    assert recs[0].refund_amount == 4096