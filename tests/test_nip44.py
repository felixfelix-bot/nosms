"""NIP-44 v2 against the official vectors, plus the refusals that matter.

The vectors are vendored (`tests/fixtures/nip44.vectors.json`, trimmed from
github.com/paulmillr/nip44 — the set the NIP-44 spec points implementers at) and
are NOT regenerated from this code: that is the whole point. If this
implementation drifts, these tests fail rather than agreeing with themselves.

Also covered: a tampered payload is refused (MAC), a payload addressed to the
wrong key is refused, and the `#`-prefixed / short / wrong-version forms are
refused explicitly instead of being mis-decrypted.
"""
from __future__ import annotations

import json
import pathlib

import pytest
from coincurve import PrivateKey

from app.nip44 import (
    Nip44Error,
    calc_padded_len,
    chacha20_xor,
    conversation_key,
    decrypt,
    decrypt_with_conversation_key,
    encrypt,
    hkdf_expand,
    hkdf_extract,
    message_keys,
    pad,
    unpad,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
VECTORS = json.loads((REPO / "tests" / "fixtures" / "nip44.vectors.json").read_text())["v2"]


def pub_of(secret_hex: str) -> str:
    return PrivateKey(bytes.fromhex(secret_hex)).public_key.format(compressed=True)[1:].hex()


# --- the official vectors --------------------------------------------------

def test_conversation_key_matches_every_official_vector():
    vectors = VECTORS["valid"]["get_conversation_key"]
    assert len(vectors) >= 30
    for vector in vectors:
        assert conversation_key(vector["sec1"], vector["pub2"]).hex() == vector["conversation_key"]


def test_conversation_key_is_symmetric_between_the_two_parties():
    """conv(a, B) == conv(b, A) — the property the spec states explicitly."""
    a, b = "01" * 32, "02" * 32
    assert conversation_key(a, pub_of(b)) == conversation_key(b, pub_of(a))


def test_message_keys_match_every_official_vector():
    group = VECTORS["valid"]["get_message_keys"]
    conv = bytes.fromhex(group["conversation_key"])
    assert len(group["keys"]) >= 30
    for vector in group["keys"]:
        chacha_key, chacha_nonce, hmac_key = message_keys(conv, bytes.fromhex(vector["nonce"]))
        assert chacha_key.hex() == vector["chacha_key"]
        assert chacha_nonce.hex() == vector["chacha_nonce"]
        assert hmac_key.hex() == vector["hmac_key"]


def test_calc_padded_len_matches_every_official_vector():
    vectors = VECTORS["valid"]["calc_padded_len"]
    assert len(vectors) >= 20
    for unpadded_len, padded_len in vectors:
        assert calc_padded_len(unpadded_len) == padded_len


def test_encryption_is_byte_identical_to_the_official_payloads():
    """Not just "it round-trips" — the exact ciphertext the spec published."""
    vectors = VECTORS["valid"]["encrypt_decrypt"]
    assert len(vectors) >= 10
    for vector in vectors:
        payload = encrypt(vector["sec1"], pub_of(vector["sec2"]), vector["plaintext"],
                          nonce=bytes.fromhex(vector["nonce"]))
        assert payload == vector["payload"], vector["plaintext"][:40]


def test_every_official_payload_decrypts_in_both_directions():
    for vector in VECTORS["valid"]["encrypt_decrypt"]:
        assert conversation_key(vector["sec1"], pub_of(vector["sec2"])).hex() == \
            vector["conversation_key"]
        assert decrypt(vector["sec2"], pub_of(vector["sec1"]), vector["payload"]) == \
            vector["plaintext"]
        assert decrypt(vector["sec1"], pub_of(vector["sec2"]), vector["payload"]) == \
            vector["plaintext"]


# --- refusals --------------------------------------------------------------

def test_every_invalid_payload_is_refused():
    """The published invalid payloads, opened with the key they name."""
    vectors = VECTORS["invalid"]["decrypt"]
    assert len(vectors) >= 10
    for vector in vectors:
        with pytest.raises(Nip44Error):
            decrypt_with_conversation_key(bytes.fromhex(vector["conversation_key"]),
                                          vector["payload"])


def test_every_invalid_plaintext_length_is_refused():
    """The vector set's invalid encrypt lengths are refused, with a reason."""
    for length in VECTORS["invalid"]["encrypt_msg_lengths"]:
        with pytest.raises(Nip44Error):
            encrypt("11" * 32, pub_of("22" * 32), "a" * length)


def test_every_invalid_conversation_key_is_refused():
    for vector in VECTORS["invalid"]["get_conversation_key"]:
        with pytest.raises((Nip44Error, ValueError)):
            conversation_key(vector["sec1"], vector["pub2"])


def test_a_tampered_ciphertext_is_refused_by_the_mac():
    vector = VECTORS["valid"]["encrypt_decrypt"][1]
    payload = vector["payload"]
    flipped = payload[:-8] + ("A" if payload[-8] != "A" else "B") + payload[-7:]
    with pytest.raises(Nip44Error):
        decrypt(vector["sec2"], pub_of(vector["sec1"]), flipped)


def test_a_payload_for_someone_else_cannot_be_opened():
    a, b, c = ("11" * 32, "22" * 32, "33" * 32)
    payload = encrypt(a, pub_of(b), "for b only")
    assert decrypt(b, pub_of(a), payload) == "for b only"
    with pytest.raises(Nip44Error):
        decrypt(c, pub_of(a), payload)


@pytest.mark.parametrize("bad", ["", "short", "#notbase64", "Ag" + "A" * 120])
def test_malformed_payloads_are_refused_with_a_reason(bad):
    with pytest.raises(Nip44Error):
        decrypt("11" * 32, pub_of("22" * 32), bad)


def test_a_v1_payload_is_refused_as_unsupported_not_mis_decrypted():
    good = encrypt("11" * 32, pub_of("22" * 32), "hello")
    import base64
    raw = bytearray(base64.b64decode(good))
    raw[0] = 1
    with pytest.raises(Nip44Error) as ctx:
        decrypt("22" * 32, pub_of("11" * 32), base64.b64encode(bytes(raw)).decode())
    assert "version" in str(ctx.value)


# --- primitives and padding edges -----------------------------------------

def test_chacha20_matches_the_rfc8439_zero_key_block():
    zero_state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574] + [0] * 12
    from app.nip44 import _chacha20_block
    assert _chacha20_block(zero_state)[:8].hex() == "76b8e0ada0f13d90"
    assert chacha20_xor(b"\x00" * 32, b"\x00" * 12, b"\x00" * 64).hex().startswith("76b8e0ad")


def test_chacha20_round_trips_arbitrary_lengths():
    key, nonce = bytes(range(32)), bytes(range(12))
    for size in (0, 1, 63, 64, 65, 200, 1000):
        data = bytes((i * 7) % 256 for i in range(size))
        assert chacha20_xor(key, nonce, chacha20_xor(key, nonce, data)) == data


def test_hkdf_matches_rfc5869_test_case_1():
    """HKDF-SHA256, RFC 5869 A.1 — the primitive, not just its use."""
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    prk = hkdf_extract(salt, ikm)
    assert prk.hex() == "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5"
    assert hkdf_expand(prk, info, 42).hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")


def test_padding_lengths_are_the_spec_ones():
    """`pad` adds the spec's length prefix, so N bytes become prefix+chunk."""
    assert calc_padded_len(1) == 32 and len(pad(b"a")) == 34
    assert calc_padded_len(32) == 32 and len(pad(b"a" * 32)) == 34
    assert calc_padded_len(33) == 64 and len(pad(b"a" * 33)) == 66
    assert unpad(pad(b"a" * 100)) == b"a" * 100
    assert unpad(pad(b"a" * 10000)) == b"a" * 10000


def test_extended_prefix_is_used_above_65536_bytes():
    payload = b"a" * 70000
    assert pad(payload)[:2] == b"\x00\x00"


def test_unpad_checks_the_length_it_claims():
    with pytest.raises(Nip44Error):
        unpad(b"\x00\x09" + b"abc")
    with pytest.raises(Nip44Error):
        unpad(b"")
    with pytest.raises(Nip44Error):
        unpad(b"\x00\x05" + b"abc")


def test_empty_plaintext_is_refused():
    with pytest.raises(Nip44Error):
        encrypt("11" * 32, pub_of("22" * 32), "")


def test_a_fresh_nonce_is_used_for_each_encryption():
    a, b = "11" * 32, pub_of("22" * 32)
    first, second = encrypt(a, b, "same text"), encrypt(a, b, "same text")
    assert first != second
    assert decrypt("22" * 32, pub_of(a), first) == "same text"


def test_keys_of_the_wrong_size_are_refused():
    with pytest.raises(Nip44Error):
        conversation_key("11" * 32, "22" * 16)
    with pytest.raises(Nip44Error):
        conversation_key("nothex", "22" * 32)
    with pytest.raises(Nip44Error):
        message_keys(b"short", b"\x00" * 32)
    with pytest.raises(Nip44Error):
        chacha20_xor(b"short", b"\x00" * 12, b"x")
