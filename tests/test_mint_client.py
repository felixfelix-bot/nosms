"""Offline tests for the real MintClient (NUT-00/02/03/07 wire protocol).

The mint is stubbed at the HTTP layer with a scripted transport, so every branch
of the client — key discovery, proof states, the swap payload, signature
unblinding, and each error mapping — is executed without a network. `live.py`
then re-runs the same client against the real testnut mint.
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from coincurve import PrivateKey, PublicKey

from app.cashu import CashuError, MintClient, blind_message, hash_to_curve

KEYSET = "0184237e63ce3423df7db2dcedc7329cff722a12b90206db53185fc31a4ca5ed96"
MINT_KEY = PrivateKey(bytes.fromhex("44" * 32))
MINT_PUB = MINT_KEY.public_key.format(compressed=True).hex()


def _proof(amount: int, secret: str | None = None) -> dict:
    return {"amount": amount, "id": KEYSET, "C": "02" + "ab" * 32,
            "secret": secret or hashlib.sha256(str(amount).encode()).hexdigest()}


class Handler:
    """Scripted mint: records requests, answers from a small route table."""

    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []
        self.states = "UNSPENT"
        self.swap_status = 200
        self.keys_status = 200
        self.transport_error = False
        self.bad_keys = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content.decode())
            except ValueError:
                body = None
        self.requests.append((request.url.path, body))
        if self.transport_error:
            raise httpx.ConnectError("mint down", request=request)
        if request.url.path == "/v1/keysets":
            return httpx.Response(200, json={"keysets": [
                {"id": KEYSET, "unit": "sat", "active": True, "input_fee_ppk": 100},
                {"id": "aa" * 32, "unit": "usd", "active": False, "input_fee_ppk": 0}]})
        if request.url.path.startswith("/v1/keys/"):
            if self.keys_status != 200:
                return httpx.Response(self.keys_status, json={"detail": "no"})
            amounts = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
            if self.bad_keys:
                amounts = [1]
            return httpx.Response(200, json={"keysets": [{"id": KEYSET, "unit": "sat",
                                                          "keys": {str(a): MINT_PUB for a in amounts}}]})
        if request.url.path == "/v1/checkstate":
            return httpx.Response(200, json={"states": [
                {"Y": y, "state": self.states, "witness": None}
                for y in (body or {}).get("Ys", [])]})
        if request.url.path == "/v1/swap":
            if self.swap_status != 200:
                return httpx.Response(self.swap_status, json={"detail": "insufficient"})
            sigs = []
            for out in (body or {}).get("outputs", []):
                c_ = PublicKey(bytes.fromhex(out["B_"])).multiply(MINT_KEY.secret)
                sigs.append({"amount": out["amount"], "id": KEYSET,
                             "C_": c_.format(compressed=True).hex()})
            return httpx.Response(200, json={"signatures": sigs})
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture()
def mint():
    handler = Handler()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mint.test")
    return MintClient("https://mint.test", http=client), handler


# --- keysets -----------------------------------------------------------------

def test_keyset_id_picks_the_active_sat_keyset(mint):
    client, handler = mint
    assert client.keyset_id() == KEYSET
    assert client.fee_ppk_for(KEYSET) == 100
    assert client.fee_ppk_for("does-not-exist") == 100      # documented default
    # the fetch is cached: one keysets call even though we asked three times
    assert [p for p, _ in handler.requests].count("/v1/keysets") == 1


def test_keyset_id_fails_loudly_when_no_active_sat_keyset(mint):
    client, handler = mint
    handler.__call__  # keep the handler referenced
    handler.requests.clear()

    def no_sat(request):
        return httpx.Response(200, json={"keysets": [
            {"id": "bb" * 32, "unit": "usd", "active": True, "input_fee_ppk": 0}]})

    client._http = httpx.Client(transport=httpx.MockTransport(no_sat),
                                base_url="https://mint.test")
    with pytest.raises(CashuError) as e:
        client.keyset_id()
    assert e.value.reason == "mint_error"


def test_keys_returns_amount_to_pubkey_and_rejects_an_unknown_keyset(mint):
    client, handler = mint
    keys = client.keys(KEYSET)
    assert keys[1] == MINT_PUB and keys[512] == MINT_PUB
    handler.keys_status = 404
    with pytest.raises(CashuError) as e:
        client.keys("cc" * 32)
    assert e.value.reason == "mint_error"


# --- NUT-07 proof states -----------------------------------------------------

def test_check_states_posts_the_hash_to_curve_of_every_secret(mint):
    client, handler = mint
    proofs = [_proof(1, "s1"), _proof(2, "s2")]
    assert client.check_states(proofs) == ["UNSPENT", "UNSPENT"]
    path, body = handler.requests[-1]
    assert path == "/v1/checkstate"
    assert body["Ys"] == [hash_to_curve(b"s1").hex(), hash_to_curve(b"s2").hex()]


def test_check_states_reports_a_spent_proof(mint):
    client, handler = mint
    handler.states = "SPENT"
    assert client.check_states([_proof(1)]) == ["SPENT"]


def test_check_states_needs_at_least_one_proof(mint):
    client, _ = mint
    with pytest.raises(CashuError) as e:
        client.check_states([])
    assert e.value.reason == "token_empty"


def test_check_states_rejects_a_short_state_list(mint):
    client, _ = mint
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"states": []})),
        base_url="https://mint.test")
    with pytest.raises(CashuError) as e:
        client.check_states([_proof(1)])
    assert e.value.reason == "mint_error"


# --- NUT-03 swap -------------------------------------------------------------

def test_swap_returns_spendable_proofs_verifiable_against_the_mint_key(mint):
    client, handler = mint
    inputs = [_proof(64), _proof(32), _proof(4)]
    proofs = client.swap(inputs, [64, 32, 4])
    assert [p["amount"] for p in proofs] == [64, 32, 4]
    assert all(p["id"] == KEYSET for p in proofs)
    assert len({p["secret"] for p in proofs}) == 3          # fresh secrets
    # every returned C must be k*Y for that proof's own secret
    for p in proofs:
        y = PublicKey(hash_to_curve(p["secret"].encode()))
        assert p["C"] == y.multiply(MINT_KEY.secret).format().hex()
    swaps = [(p, b) for p, b in handler.requests if p == "/v1/swap"]
    assert len(swaps) == 1
    path, body = swaps[0]
    assert [i["secret"] for i in body["inputs"]] == [p["secret"] for p in inputs]
    assert sum(o["amount"] for o in body["outputs"]) == 100
    assert all(o["B_"].startswith(("02", "03")) for o in body["outputs"])


def test_swap_refuses_to_sign_an_amount_the_mint_has_no_key_for(mint):
    client, handler = mint
    handler.bad_keys = True
    with pytest.raises(CashuError) as e:
        client.swap([_proof(4)], [4])
    assert e.value.reason == "mint_error"
    assert "never published" in e.value.hint


def test_swap_requires_inputs(mint):
    client, _ = mint
    with pytest.raises(CashuError) as e:
        client.swap([], [1])
    assert e.value.reason == "token_empty"


# --- error mapping -----------------------------------------------------------

def test_a_mint_refusal_becomes_mint_error_not_a_traceback(mint):
    client, handler = mint
    handler.swap_status = 400
    with pytest.raises(CashuError) as e:
        client.swap([_proof(4)], [4])
    assert e.value.reason == "mint_error"
    assert "400" in e.value.hint


def test_an_unreachable_mint_becomes_mint_unreachable(mint):
    client, handler = mint
    handler.transport_error = True
    with pytest.raises(CashuError) as e:
        client.check_states([_proof(1)])
    assert e.value.reason == "mint_unreachable"
    assert "retry" in e.value.hint.lower()


def test_a_non_json_body_is_a_mint_error(mint):
    client, _ = mint
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")),
        base_url="https://mint.test")
    with pytest.raises(CashuError) as e:
        client.keysets()
    assert e.value.reason == "mint_error"


def test_blind_message_wraps_the_secret_in_a_real_point(mint):
    blinded, blinding = blind_message(8, KEYSET)
    assert blinded["amount"] == 8 and blinded["id"] == KEYSET
    y = PublicKey(hash_to_curve(blinding["secret"].encode()))
    r_g = PrivateKey(blinding["r"].to_bytes(32, "big")).public_key
    assert blinded["B_"] == PublicKey.combine_keys([y, r_g]).format().hex()

