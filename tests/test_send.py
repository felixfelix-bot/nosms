"""RED-first tests for POST /api/send — the money-moving path.

End-to-end through the ASGI app with a real NIP-98 signature, a StubMint (offline
escrow) and a FakeTransport. No network, no live SMS.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.cashu import decode_token
from app.pricing import DEFAULT_PRICE_SATS, normalize_e164
from app.transports import FakeTransport
from tests.conftest import BASE_URL, build_event, nip98_header
from tests.stubs import MINT_URL, StubMint, make_app, make_token, proof

SEND_URL = f"{BASE_URL}/api/send"
# ADR-0002 pricing is FLAT: the rail's plan is unlimited including international,
# so every destination costs the same 2,900 sats (the rugpull-risk premium).
DEST_DE = "+491****5678"
DEST_US = "+155****4567"
PRICE = DEFAULT_PRICE_SATS
#: what a rail actually receives: the E.164 form, formatting stripped
DEST_DE_E164 = normalize_e164(DEST_DE)


def send(client: TestClient, *, token, to=DEST_DE, text="hello there", sk_hex=None, url=SEND_URL):
    """POST a signed, byte-exact /api/send request.

    Each call carries a fresh nonce tag: two identical bodies signed inside the
    same second would otherwise produce the same event id and trip the replay
    cache, which would make a test look like a quota rejection.
    """
    body = json.dumps({"to": to, "text": text}).encode()
    kw = {"extra_tags": [["nonce", uuid4().hex]]}
    if sk_hex:
        kw["sk_hex"] = sk_hex
    headers = {"Authorization": nip98_header(url=url, method="POST", body=body, **kw),
               "Content-Type": "application/json"}
    if token is not None:
        headers["X-Cashu"] = token
    return client.post(url, content=body, headers=headers)


@pytest.fixture()
def env(tmp_path):
    mint = StubMint(fee_ppk=0)
    transport = FakeTransport()
    app = make_app(tmp_path, transport=transport, mint=mint)
    return TestClient(app), app, mint, transport


# --- happy path --------------------------------------------------------------

def test_send_escrows_the_token_and_returns_id_price_and_status_url(env):
    client, app, mint, transport = env
    r = send(client, token=make_token([4096]))                    # 4096 sats
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message_id"]
    assert body["price_sats"] == PRICE
    assert body["change_sats"] == 4096 - PRICE
    assert body["status"] in ("queued", "sent", "delivered")
    assert body["status_url"].endswith(f"/api/message/{body['message_id']}/status")
    # the message really reached the rail, with the destination and the text
    assert transport.sent == [(DEST_DE_E164, "hello there")]
    # escrow consumed the sender's proofs at the mint (one swap, no double-spend)
    assert len(mint.swap_calls) == 1
    assert sum(mint.swap_calls[0]["outputs"]) == 4096
    assert len(mint.spent_secrets()) == 1
    # ...and the service now holds exactly the price
    rec = app.state.escrow.get(body["message_id"])
    assert rec.amount == PRICE and rec.price == PRICE and rec.change == 4096 - PRICE
    assert rec.pubkey


def test_send_requires_the_token_header(env):
    client, *_ = env
    r = send(client, token=None)
    assert r.status_code == 400
    assert r.headers["x-reason"] == "token_missing"
    assert r.headers["x-hint"]


def test_send_rejects_a_bad_destination_before_spending_anything(env):
    client, app, mint, transport = env
    r = send(client, token=make_token([4096]), to="+")
    assert r.status_code == 400
    assert r.headers["x-reason"] == "bad_destination"
    assert mint.swap_calls == [] and transport.sent == []


# --- payment rejection -------------------------------------------------------

def test_send_rejects_an_underpaid_token_and_names_the_shortfall(env):
    client, app, mint, transport = env
    r = send(client, token=make_token([1024]))                    # 1024 sats, needs 2900
    assert r.status_code == 402
    assert r.headers["x-reason"] == "insufficient_funds"
    assert str(PRICE - 1024) in r.headers["x-hint"]
    assert r.json()["shortfall_sats"] == PRICE - 1024
    assert mint.swap_calls == [] and transport.sent == []


def test_send_rejects_a_token_from_another_mint(env):
    client, app, _, transport = env
    r = send(client, token=make_token([4096], mint="https://mint.example.com"))
    assert r.status_code == 400
    assert r.headers["x-reason"] == "token_wrong_mint"
    assert transport.sent == []


def test_send_rejects_an_already_spent_token(env):
    client, app, mint, transport = env
    spent = proof(4096)
    mint.spent.add(spent["secret"])
    from app.cashu import encode_token
    r = send(client, token=encode_token(MINT_URL, [spent, proof(64), proof(16), proof(8)]))
    assert r.status_code == 409
    assert r.headers["x-reason"] == "token_already_spent"
    assert transport.sent == []


def test_send_rejects_a_malformed_token(env):
    client, *_ = env
    r = send(client, token="cashuAnot-real")
    assert r.status_code == 400
    assert r.headers["x-reason"] == "token_invalid"


def test_send_accepts_a_v4_token_from_the_fixture(tmp_path):
    """cashuB (CBOR) tokens are what other clients emit by default."""
    from tests.test_cashu import v4_fixture_token
    token = v4_fixture_token()
    mint = StubMint(fee_ppk=0)
    transport = FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint))
    # the fixture carries 100 sats: enough for the old +1 price, nowhere near 2,900
    r = send(client, token=token, to=DEST_US)
    assert r.status_code == 402, r.text
    assert r.headers["x-reason"] == "insufficient_funds"
    assert r.json()["shortfall_sats"] == PRICE - 100


def test_send_accepts_a_v4_token_that_covers_the_flat_price(tmp_path):
    """A cashuB (CBOR) token is what other clients emit by default."""
    from app.cashu import encode_token
    from tests.stubs import proof
    token = encode_token(MINT_URL, [proof(2048), proof(1024)])    # 3072 sats
    mint = StubMint(fee_ppk=0)
    transport = FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint))
    r = send(client, token=token, to=DEST_US)
    assert r.status_code == 200, r.text
    assert r.json()["price_sats"] == PRICE and r.json()["change_sats"] == 3072 - PRICE


# --- the mint's input fee (real testnut charges 100 ppk) ---------------------

def test_mint_input_fee_is_borne_by_the_sender_not_the_service(tmp_path):
    mint = StubMint(fee_ppk=100)          # testnut's real sat keyset
    transport = FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint))
    # 1024 sats in one proof -> 1 sat fee -> 1023 net, short of the 2,900 price
    r = send(client, token=make_token([1024]), to=DEST_US)
    assert r.status_code == 402
    assert r.headers["x-reason"] == "insufficient_funds"
    assert r.json()["shortfall_sats"] == PRICE - 1023
    assert r.json()["mint_fee_sats"] == 1
    # overpaying by the fee works and the escrow still holds exactly the price.
    # NUT-02 charges ceil(ppk * n_inputs / 1000), so two inputs at 100 ppk still
    # cost ONE sat, not two: 3072 - 1 = 3071 net, 2900 postage -> 171 change.
    r2 = send(client, token=make_token([2048, 1024]))             # 3072 in
    assert r2.status_code == 200, r2.text
    assert r2.json()["price_sats"] == PRICE
    assert r2.json()["mint_fee_sats"] == 1
    assert r2.json()["change_sats"] == 171


def test_mint_failure_is_502_and_creates_no_message(tmp_path):
    mint = StubMint(fee_ppk=0)
    mint.fail_swap = "mint is down"
    transport = FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint))
    r = send(client, token=make_token([4096]))
    assert r.status_code == 502
    assert r.headers["x-reason"] == "mint_error"
    assert transport.sent == []


# --- transport failure ------------------------------------------------------

def test_a_hard_transport_failure_is_refunded_immediately(tmp_path):
    mint = StubMint(fee_ppk=0)
    transport = FakeTransport(accept=False)
    app = make_app(tmp_path, transport=transport, mint=mint)
    client = TestClient(app)
    r = send(client, token=make_token([4096]))
    assert r.status_code == 502
    assert r.headers["x-reason"] == "transport_error"
    # postage was taken and then given straight back: the refund is recorded once
    recs = app.state.escrow.list_all()
    assert len(recs) == 1
    assert recs[0].status == "failed" and recs[0].refunded_at is not None
    assert recs[0].refund_amount == 4096         # nothing was delivered, all of it back
    assert decode_token(recs[0].refund_token).amount == 4096


# --- T4: a paced rail defers (429 + Retry-After); a stopped rail fails loudly --
#
# Both shapes take the postage first and hand it straight back: the message never
# left, so charging for it would be wrong. The difference is what the caller is
# told: a deferral to come back, or a rail that stopped and needs a human.

class _RaisingRail:
    """A rail double that raises the way the WhatsApp rail does (ADR-0003)."""

    name = "whatsapp"

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    @property
    def capabilities(self):
        from app.transports.base import Capabilities
        return Capabilities(available=True, best_effort=True,
                            delivery_receipts=False, countries=["*"])

    def send(self, dest, body, **kw):
        self.calls += 1
        raise self.exc


def test_a_paced_rail_answers_429_with_retry_after_and_refunds(tmp_path):
    from app.transports.errors import RailPaced

    mint = StubMint(fee_ppk=0)
    rail = _RaisingRail(RailPaced("min_gap", 42.0, detail="personal-line pacing"))
    app = make_app(tmp_path, transport=rail, mint=mint)
    client = TestClient(app)

    r = send(client, token=make_token([4096]))

    assert r.status_code == 429
    assert r.headers["x-reason"] == "rail_paced"
    assert r.headers["retry-after"] == "42"          # the deferral is machine-readable
    assert r.json()["pacing_reason"] == "min_gap"
    assert r.json()["retry_after_seconds"] == 42
    assert rail.calls == 1
    recs = app.state.escrow.list_all()
    assert len(recs) == 1
    assert recs[0].status == "failed"
    assert recs[0].provider_status == "rail_paced:min_gap"
    assert recs[0].refunded_at is not None
    assert recs[0].refund_amount == 4096             # nothing was attempted
    assert decode_token(recs[0].refund_token).amount == 4096


def test_a_stopped_rail_answers_503_rail_unavailable_and_refunds(tmp_path):
    from app.transports.errors import RailUnavailable

    mint = StubMint(fee_ppk=0)
    rail = _RaisingRail(RailUnavailable("terminated", "the account is banned"))
    app = make_app(tmp_path, transport=rail, mint=mint)
    client = TestClient(app)

    r = send(client, token=make_token([4096]))

    assert r.status_code == 503
    assert r.headers["x-reason"] == "rail_unavailable"
    assert r.json()["rail_reason"] == "terminated"
    assert "retry-after" not in r.headers          # a stop is not a "come back soon"
    recs = app.state.escrow.list_all()
    assert len(recs) == 1
    assert recs[0].provider_status == "rail_unavailable:terminated"
    assert recs[0].refunded_at is not None
    assert recs[0].refund_amount == 4096


# --- quotas -----------------------------------------------------------------

def test_second_send_to_the_same_destination_hits_the_cooldown(tmp_path):
    mint, transport = StubMint(fee_ppk=0), FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint,
                                 destination_cooldown_seconds=60))
    assert send(client, token=make_token([4096])).status_code == 200
    r = send(client, token=make_token([4096]))
    assert r.status_code == 429
    assert r.headers["x-reason"] == "destination_cooldown"
    assert transport.sent == [(DEST_DE_E164, "hello there")]   # the second never went


def test_cooldown_is_per_destination(tmp_path):
    mint, transport = StubMint(fee_ppk=0), FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint,
                                 destination_cooldown_seconds=60))
    assert send(client, token=make_token([4096])).status_code == 200
    assert send(client, token=make_token([4096]), to="+155****4567").status_code == 200


def test_daily_cap_is_per_identity_and_config_driven(tmp_path):
    mint, transport = StubMint(fee_ppk=0), FakeTransport()
    client = TestClient(make_app(tmp_path, transport=transport, mint=mint,
                                 destination_cooldown_seconds=0, daily_cap=1))
    assert send(client, token=make_token([4096])).status_code == 200
    r = send(client, token=make_token([4096]), to="+155****4567")
    assert r.status_code == 429
    assert r.headers["x-reason"] == "daily_cap_reached"
    # a different key is a different identity, so it is not capped
    r2 = send(client, token=make_token([4096]), to="+155****4567", sk_hex="22" * 32)
    assert r2.status_code == 200, r2.text
