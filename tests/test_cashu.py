"""RED-first tests for the minimal Cashu client that escrow depends on.

Everything here is offline: the crypto round-trip signs with a locally generated
scalar using exactly the NUT-00 blind-signature construction, and the decoder is
pinned against a real ``cashuB`` (v4) token produced by an independent client
(the ``cashu`` python CLI) against the live testnut mint.
"""
from __future__ import annotations

import pathlib

import pytest
from coincurve import PrivateKey, PublicKey

from app.cashu import (
    CashuError,
    MINT_FEE_PPK_DEFAULT,
    blind_message,
    decode_token,
    encode_token,
    fee_for,
    hash_to_curve,
    split_amounts,
    unblind,
)
from tests.stubs import KEYSET, MINT_URL, make_token

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "v4_token_cbor.b64"


def v4_fixture_token() -> str:
    """The CBOR payload a real client emitted, with the bearer values redacted.

    The fixture is the base64url payload *without* the `cashuB` prefix: a cashu
    token is a bearer instrument, so no spendable string is committed. Only the
    framing another implementation emits is pinned (see tests/fixtures/README.md).
    """
    return "cashuB" + FIXTURE.read_text().strip()


# --- v3 round-trip -----------------------------------------------------------

def test_v3_token_round_trips():
    token = make_token([64, 32, 4])
    parsed = decode_token(token)
    assert parsed.mint == MINT_URL
    assert parsed.unit == "sat"
    assert parsed.amount == 100
    assert [p["amount"] for p in parsed.proofs] == [64, 32, 4]
    assert all(p["id"] == KEYSET for p in parsed.proofs)
    assert all(p["secret"] and p["C"] for p in parsed.proofs)


def test_v3_token_is_prefixed_and_urlsafe():
    token = make_token([1])
    assert token.startswith("cashuA")
    assert "+" not in token and "/" not in token and "=" not in token


def test_encode_token_refuses_an_empty_proof_list():
    with pytest.raises(CashuError) as e:
        encode_token(MINT_URL, [], unit="sat")
    assert e.value.reason == "token_empty"


# --- v4 (cashuB / CBOR) ------------------------------------------------------

def test_decodes_a_real_v4_token_from_another_client():
    """CBOR framing from a token the `cashu` CLI produced; bearer values redacted."""
    token = v4_fixture_token()
    assert token.startswith("cashuB")
    parsed = decode_token(token)
    assert parsed.mint == MINT_URL
    assert parsed.unit == "sat"
    assert parsed.amount == 100
    assert len(parsed.proofs) == 3
    assert sum(p["amount"] for p in parsed.proofs) == 100
    assert all(p["id"] == KEYSET for p in parsed.proofs)
    assert all(len(p["secret"]) == 64 for p in parsed.proofs)
    assert all(len(p["C"]) == 66 for p in parsed.proofs)   # 33-byte point, hex


@pytest.mark.parametrize("token", ["", "not-a-token", "cashuA!!!notb64", "cashuZabcd", "x" * 40])
def test_decode_rejects_garbage_with_a_machine_reason(token):
    with pytest.raises(CashuError) as e:
        decode_token(token)
    assert e.value.reason in ("token_invalid", "token_unsupported")
    assert e.value.hint


# --- crypto (NUT-00) ---------------------------------------------------------

def test_hash_to_curve_is_deterministic_and_a_valid_point():
    y1, y2 = hash_to_curve(b"hello"), hash_to_curve(b"hello")
    assert y1 == y2
    assert len(y1) == 33 and y1[0] in (2, 3)
    assert hash_to_curve(b"hello1") != y1


def test_hash_to_curve_matches_the_reference_implementation():
    """Cross-implementation vectors from `cashu`'s b_dhke.hash_to_curve.

    The counter is appended to sha256(domain || message), not to
    domain || message — a single-hash version passes every local check and is
    rejected by a real mint, so these vectors are the only offline guard.
    """
    expected = {
        b"hello": "021f1c0e53d12bf9184a53ca3e60e5416e1eae3a498fed34326d986609a5b797c5",
        b"deadbeef": "0248b9abd56821a1f4bed1ab6e1418334848f20f3bc6d3a4e996e6bfb68b9f56c5",
        b"s1": "02bbcd1764bd7577811cb9506997df75e07b5d83863d0dbef467431fe26642239e",
        b"s2": "0270d28d965e97010e0951aeb835af7158722e525107a63fdf008faa409ce7d59f",
    }
    for message, point in expected.items():
        assert hash_to_curve(message).hex() == point, message


def test_blind_then_unblind_matches_the_mints_own_signature():
    """Exactly the NUT-00/BIP-340 construction, with the mint as one scalar."""
    keyset = KEYSET
    mint_key = PrivateKey(bytes.fromhex("33" * 32))
    blinded, blinding = blind_message(64, keyset)
    # the mint signs the blinded point with its private key: C_ = k * B_
    c_ = PublicKey(bytes.fromhex(blinded["B_"])).multiply(mint_key.secret)
    signed = {"amount": 64, "id": keyset, "C_": c_.format(compressed=True).hex()}
    p = unblind(signed, blinding, keyset, mint_key.public_key.format(compressed=True).hex())
    assert p["amount"] == 64 and p["secret"] == blinding["secret"] and p["id"] == keyset
    # C must equal k*Y  ->  the proof is spendable at the mint with that key
    y = PublicKey(hash_to_curve(p["secret"].encode()))
    expected = y.multiply(mint_key.secret).format().hex()
    assert p["C"] == expected


# --- amounts -----------------------------------------------------------------

@pytest.mark.parametrize("total,expected", [
    (0, []), (1, [1]), (100, [64, 32, 4]), (500, [256, 128, 64, 32, 16, 4]),
    (512, [512]), (600, [512, 64, 16, 8]),
])
def test_split_amounts_is_a_power_of_two_decomposition(total, expected):
    assert split_amounts(total) == expected
    assert sum(split_amounts(total)) == total


@pytest.mark.parametrize("ppk,n,expected", [(0, 3, 0), (100, 3, 1), (100, 10, 1), (100, 11, 2)])
def test_fee_for_is_the_nut02_input_fee(ppk, n, expected):
    assert fee_for(ppk, n) == expected


def test_default_fee_constant_matches_testnut():
    assert MINT_FEE_PPK_DEFAULT == 100
