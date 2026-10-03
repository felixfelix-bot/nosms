"""Shared fixtures for the nosms test suite.

Two standing rules are enforced here:

1. **Offline by construction.** An autouse fixture blocks ``socket.socket.connect``
   so any test that accidentally reaches for the network fails loudly. The whole
   suite must run with zero outbound traffic (no live SMS, no live Telnyx).
2. **Real signatures.** NIP-98 tests sign with real secp256k1 keys and a real
   BIP-340 schnorr signature; the verifier under test is never handed a stub.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sys
import time

import pytest
from coincurve import PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Two distinct identities: NOSTR_ID signs the happy-path events, OTHER_ID is used
# to prove the verifier reports the *signer's* pubkey rather than a constant.
NOSTR_SK = "11" * 32
OTHER_SK = "22" * 32


def public_key_hex(sk_hex: str) -> str:
    return PrivateKey(bytes.fromhex(sk_hex)).public_key.format(compressed=True)[1:].hex()


def compute_event_id(ev: dict) -> str:
    serialized = json.dumps(
        [0, ev["pubkey"], int(ev["created_at"]), int(ev["kind"]), ev["tags"], ev["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def build_event(
    sk_hex: str = NOSTR_SK,
    *,
    url: str,
    method: str = "POST",
    created_at: int | None = None,
    kind: int = 27235,
    tags: list | None = None,
    content: str = "",
    tamper: str | None = None,
) -> dict:
    """Build a fully signed NIP-98 event.

    ``tamper`` mutates the event *after* signing to produce a deterministic
    invalid case: ``"sig"`` corrupts the signature, ``"content"`` changes the
    signed payload, ``"id"`` rewrites the id field.
    """
    if created_at is None:
        created_at = int(time.time())
    if tags is None:
        tags = [["u", url], ["method", method]]
    ev = {
        "pubkey": public_key_hex(sk_hex),
        "created_at": int(created_at),
        "kind": int(kind),
        "tags": tags,
        "content": content,
    }
    eid = compute_event_id(ev)
    sig = PrivateKey(bytes.fromhex(sk_hex)).sign_schnorr(
        bytes.fromhex(eid), aux_randomness=b"\x00" * 32
    ).hex()
    ev.update({"id": eid, "sig": sig})

    if tamper == "sig":
        ev["sig"] = ("00" if sig[:2] != "00" else "11") + sig[2:]
    elif tamper == "content":
        ev["content"] = ev["content"] + "tampered"
    elif tamper == "id":
        ev["id"] = ("00" if eid[:2] != "00" else "11") + eid[2:]
    return ev


def auth_header(ev: dict) -> dict:
    raw = base64.b64encode(json.dumps(ev).encode()).decode()
    return {"Authorization": f"Nostr {raw}"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard-fail any attempt to open a real TCP connection."""
    real_connect = socket.socket.connect

    def explode(self, *args, **kwargs):  # pragma: no cover - guard only
        raise AssertionError(
            "outbound network access attempted during the offline test suite"
        )

    monkeypatch.setattr(socket.socket, "connect", explode, raising=True)
    try:
        yield
    finally:
        monkeypatch.setattr(socket.socket, "connect", real_connect, raising=False)
