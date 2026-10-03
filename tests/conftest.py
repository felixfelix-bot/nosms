"""Shared test helpers — offline only (no network, no live SMS, no Telnyx).

`nip98_header` builds a real BIP-340 signed kind-27235 event and returns the
`Authorization: Nostr <base64>` header value, so the auth middleware is tested
against genuine signatures rather than a stub.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from coincurve import PrivateKey

#: fixed test key (never used anywhere real)
SK_HEX = "11" * 32
BASE_URL = "http://testserver"


def _pubkey(sk_hex: str) -> str:
    return PrivateKey(bytes.fromhex(sk_hex)).public_key.format(compressed=True)[1:].hex()


def build_event(
    url: str,
    method: str = "POST",
    *,
    sk_hex: str = SK_HEX,
    body: bytes | None = None,
    created_at: int | None = None,
    kind: int = 27235,
    content: str = "",
    omit_u: bool = False,
    omit_method: bool = False,
    extra_tags: list[list[str]] | None = None,
    override_id: str | None = None,
    override_sig: str | None = None,
    override_pubkey: str | None = None,
) -> dict:
    """Construct a real signed NIP-98 event (dict)."""
    tags: list[list[str]] = []
    if not omit_u:
        tags.append(["u", url])
    if not omit_method:
        tags.append(["method", method])
    if body is not None:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    if extra_tags:
        tags.extend(extra_tags)
    ts = int(time.time()) if created_at is None else created_at
    pubkey = override_pubkey or _pubkey(sk_hex)
    serialized = json.dumps(
        [0, pubkey, ts, kind, tags, content], separators=(",", ":"), ensure_ascii=False
    )
    eid = hashlib.sha256(serialized.encode()).hexdigest()
    sig = PrivateKey(bytes.fromhex(sk_hex)).sign_schnorr(bytes.fromhex(eid)).hex()
    return {
        "id": eid,
        "pubkey": pubkey,
        "created_at": ts,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
        **({"id": override_id} if override_id else {}),
        **({"sig": override_sig} if override_sig else {}),
    }


def nip98_header(event: dict | None = None, **kwargs) -> str:
    """Return the `Authorization` header value for a (freshly built) event."""
    ev = event if event is not None else build_event(**kwargs)
    raw = json.dumps(ev, separators=(",", ":"), ensure_ascii=False).encode()
    return "Nostr " + base64.b64encode(raw).decode()


@pytest.fixture()
def build():
    """build(url=..., method=..., **kw) -> signed NIP-98 event dict."""
    def _make(url: str = f"{BASE_URL}/api/send", method: str = "POST", **kw):
        return build_event(url=url, method=method, **kw)
    return _make


@pytest.fixture()
def signed_headers():
    """Factory: signed_headers(url=..., method=..., body=...) -> {'Authorization': ...}"""
    def _make(url: str = f"{BASE_URL}/api/send", method: str = "POST", **kw):
        return {"Authorization": nip98_header(url=url, method=method, **kw)}
    return _make
