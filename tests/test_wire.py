"""Unit tests for the dual-dialect CVM wire envelope (``app.wire``).

The 2026-10-05 live finding: the Kehto shell's NAP-CVM transport (ContextVM
CEP-4) and the ``cvmi``/``nostr_sdk`` client (NIP-59) put *different* things
inside the same kind-1059/21059 outer envelope. A server that understands only
one silently drops the other, and the caller just sees silence. These tests pin
both shapes — and the in-kind reply — with no network.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from nostr_sdk import (EventBuilder, Keys, Kind, Nip44Version, NostrSigner, PublicKey,
                       Tag, nip44_decrypt, nip44_encrypt)

from app.wire import (CEP4_WRAP_KIND, CVM_KIND, InboundEnvelope, build_inner_event,
                      unwrap_envelope, wrap_cep4)


def _keys_from_seed(n: int) -> Keys:
    return Keys.parse(f"{n:064x}")


SERVER_KEYS = _keys_from_seed(0x51)
CLIENT_KEYS = _keys_from_seed(0x22)
RPC = {"jsonrpc": "2.0", "id": "t-1", "method": "tools/call",
       "params": {"name": "sms.capabilities", "arguments": {}}}


def _cep4_envelope():
    """The shell's dialect: a SIGNED kind-25910 event inside a NIP-44 blob."""
    inner = build_inner_event(CLIENT_KEYS, SERVER_KEYS.public_key(), RPC)
    return wrap_cep4(CLIENT_KEYS, SERVER_KEYS.public_key(), inner)


def _nip59_envelope():
    """The ``nostr_sdk`` / ``cvmi`` dialect: a real NIP-59 gift wrap.

    Mirrors exactly what the rust SDK's ``gift_wrap`` builds: ``EventBuilder.seal()``
    produces the kind-13 seal (encrypted to the recipient with the SENDER's key),
    then a kind-1059 wrap encrypted to the recipient with a throwaway key.
    """
    rumor = EventBuilder(Kind(CVM_KIND), json.dumps(RPC)).tags(
        [Tag.public_key(SERVER_KEYS.public_key())]).build(CLIENT_KEYS.public_key())
    seal_builder = asyncio.run(EventBuilder.seal(
        NostrSigner.keys(CLIENT_KEYS), SERVER_KEYS.public_key(), rumor))
    seal = seal_builder.sign_with_keys(CLIENT_KEYS)
    wrap_key = _keys_from_seed(0x33)
    wrap_unsigned = EventBuilder(
        Kind(1059),
        nip44_encrypt(wrap_key.secret_key(), SERVER_KEYS.public_key(),
                      seal.as_json(), Nip44Version.V2),
    ).tags([Tag.public_key(SERVER_KEYS.public_key())]).build(wrap_key.public_key())
    return wrap_unsigned.sign_with_keys(wrap_key)


def test_cep4_envelope_roundtrip():
    """A CEP-4 envelope unwraps to the rpc and the SENDER (the inner signer)."""
    envelope = asyncio.run(unwrap_envelope(SERVER_KEYS, _cep4_envelope()))
    assert isinstance(envelope, InboundEnvelope)
    assert envelope.rpc == RPC
    assert envelope.nip59 is False
    assert envelope.sender == CLIENT_KEYS.public_key()


def test_nip59_envelope_roundtrip():
    """A NIP-59 gift wrap unwraps through ``UnwrappedGift`` (async in 0.44+)."""
    envelope = asyncio.run(unwrap_envelope(SERVER_KEYS, _nip59_envelope()))
    assert envelope.rpc == RPC
    assert envelope.nip59 is True
    assert envelope.sender == CLIENT_KEYS.public_key()


def test_wrap_cep4_is_readable_by_a_cep4_client():
    """The reply must be exactly what the shell's transport reads: a signed
    kind-25910 event (NOT a seal), p-tagged to the client, signed by the server,
    and on the ephemeral wrap kind the transport subscribes to."""
    reply = wrap_cep4(SERVER_KEYS, CLIENT_KEYS.public_key(), build_inner_event(
        SERVER_KEYS, CLIENT_KEYS.public_key(),
        {"jsonrpc": "2.0", "id": "r-1", "result": {"ok": True}}))
    assert reply.kind().as_u16() == CEP4_WRAP_KIND
    assert reply.verify()
    assert [t.as_vec() for t in reply.tags().to_vec()][0] == [
        "p", CLIENT_KEYS.public_key().to_hex()]
    inner = json.loads(nip44_decrypt(CLIENT_KEYS.secret_key(), reply.author(),
                                     reply.content()))
    assert inner["kind"] == CVM_KIND
    assert inner["pubkey"] == SERVER_KEYS.public_key().to_hex()
    assert [t for t in inner["tags"] if t[0] == "p"][0][1] == CLIENT_KEYS.public_key().to_hex()
    assert json.loads(inner["content"]) == {"jsonrpc": "2.0", "id": "r-1",
                                            "result": {"ok": True}}


def test_wrong_recipient_is_rejected_not_crashed():
    """An envelope for another server must fail cleanly (ValueError)."""
    with pytest.raises(ValueError):
        asyncio.run(unwrap_envelope(_keys_from_seed(0x77), _cep4_envelope()))


def test_forged_inner_signature_cannot_claim_a_sender():
    """The envelope proves who could READ; the inner SIGNATURE proves who sent.
    A tampered inner signature must be refused."""
    event = _cep4_envelope()
    plain = json.loads(nip44_decrypt(SERVER_KEYS.secret_key(), event.author(),
                                     event.content()))
    plain["sig"] = "00" * 64
    forged = EventBuilder(
        Kind(CEP4_WRAP_KIND),
        nip44_encrypt(CLIENT_KEYS.secret_key(), SERVER_KEYS.public_key(),
                      json.dumps(plain), Nip44Version.V2),
    ).tags([Tag.public_key(SERVER_KEYS.public_key())]).build(CLIENT_KEYS.public_key())
    with pytest.raises(ValueError):
        asyncio.run(unwrap_envelope(SERVER_KEYS, forged.sign_with_keys(CLIENT_KEYS)))


def test_signer_and_pubkey_helpers_are_real():
    """Guard against a silently-swallowed pubkey parse."""
    assert PublicKey.parse(SERVER_KEYS.public_key().to_hex()) == SERVER_KEYS.public_key()
    assert NostrSigner.keys(SERVER_KEYS) is not None
