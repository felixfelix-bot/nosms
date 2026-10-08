"""Offline test harness — shared helpers + fixtures captured from the live 8333.mobi endpoint.

This file is the union of two lineages:

* NIP-98 event signing (`build_event` / `nip98_header`) — real BIP-340 signed
  kind-27235 events so the auth middleware is tested against genuine
  signatures rather than a stub.
* Machankura LNURL-pay fixtures + `OfflineTransport` — captured from the live
  endpoint, with an offline guarantee.

PROVENANCE OF THE LIVE FIXTURES
-------------------------------
Captured 2026-09-27T00:39Z from this host with two read-only GETs (no account,
no key, no invoice requested, nothing paid)::

    curl -sS -D- https://8333.mobi/.well-known/lnurlp/sigidli
      HTTP/1.1 200 OK
      Content-Type: application/json; charset=utf-8
      Content-Length: 457
      -> fixtures/lnurlp_sigidli_200.json          (body stored verbatim, 457 bytes)

    curl -sS -D- https://8333.mobi/.well-known/lnurlp/254700000000
      HTTP/1.1 404 Not Found
      Content-Type: application/json; charset=utf-8
      Content-Length: 83
      -> fixtures/lnurlp_unknown_user_404.json     (body stored verbatim, 83 bytes)

`tests/test_lnurl_pay.py::test_live_fixtures_match_the_captured_content_lengths`
pins both files to those Content-Lengths, so a fixture cannot silently drift
away from what the service actually answered.

`invoice_callback_200.synthetic.json` is NOT a live capture — it is a LUD-06
shaped synthetic body. We deliberately never ask Machankura to mint an invoice
we have no intention of paying.

OFFLINE GUARANTEE
-----------------
No network, no live SMS, no Telnyx, no Machankura calls in the suite. No test
builds a :class:`~nosms.lnurl_pay.UrllibTransport`. Every request goes through
:class:`OfflineTransport`, which raises ``AssertionError`` for any URL it was
not explicitly handed. A test that accidentally tries to reach the network
therefore fails loudly instead of quietly passing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import time
from typing import Any, Mapping, Optional

import pytest
from coincurve import PrivateKey

from nosms.lnurl_pay import DEFAULT_TIMEOUT, HttpResult

# ── NIP-98 signing helpers (service-surface tests) ─────────────────────────

#: fixed test key (never used anywhere real)
SK_HEX = "11" * 32
BASE_URL = "http://testserver"

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: The two live endpoints cited in every Machankura test module.
MACHANKURA_USER_URL = "https://8333.mobi/.well-known/lnurlp/sigidli"
MACHANKURA_CALLBACK = "https://8333.mobi/.well-known/lnurlp/sigidli"
MACHANKURA_UNKNOWN_URL = "https://8333.mobi/.well-known/lnurlp/254700000000"

MACHANKURA_USER = "sigidli@8333.mobi"
MACHANKURA_UNKNOWN_PHONE = "254700000000@8333.mobi"


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


# ── Machankura / LNURL-pay offline harness (sats-rail tests) ───────────────

def fixture_bytes(name: str) -> bytes:
    """The captured body bytes.

    The files carry one trailing newline for git hygiene; the responses the
    service actually sent did not, and the tests pin the real Content-Lengths.
    """
    return (FIXTURES / name).read_bytes().rstrip(b"\n")


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class OfflineTransport:
    """A transport that can only answer URLs the test registered up front."""

    def __init__(self, events: Optional[list[Any]] = None) -> None:
        self._exact: dict[str, HttpResult] = {}
        self._callbacks: dict[str, HttpResult] = {}
        self._bases: dict[str, HttpResult] = {}
        self.calls: list[str] = []
        self.events: list[Any] = events if events is not None else []

    # -- registration -----------------------------------------------------
    def add(self, url: str, status_code: int, body: bytes) -> "OfflineTransport":
        """Register a *discovery* (query-less) URL."""
        self._bases[url] = HttpResult(url=url, status_code=status_code, body=body)
        return self

    def add_callback(self, url: str, status_code: int, body: bytes) -> "OfflineTransport":
        """Register the invoice callback: answers any ``?amount=…`` request to ``url``."""
        self._callbacks[url] = HttpResult(url=url, status_code=status_code, body=body)
        return self

    def add_exact(self, url: str, status_code: int, body: bytes) -> "OfflineTransport":
        """Register one exact URL, query string included."""
        self._exact[url] = HttpResult(url=url, status_code=status_code, body=body)
        return self

    # -- Transport protocol ----------------------------------------------
    def get(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> HttpResult:
        self.calls.append(url)
        self.events.append(("get", url))
        base, _, query = url.partition("?")
        if url in self._exact:
            return self._exact[url]
        if query and base in self._callbacks:
            return self._callbacks[base]
        if not query and base in self._bases:
            return self._bases[base]
        raise AssertionError(
            f"test made an unregistered HTTP request (the suite must stay offline): {url}"
        )


def make_transport(
    *,
    discovery_body: Optional[bytes] = None,
    discovery_status: int = 200,
    discovery_url: str = MACHANKURA_USER_URL,
    callback_body: Optional[bytes] = None,
    callback_status: int = 200,
    callback_url: str = MACHANKURA_CALLBACK,
    events: Optional[list[Any]] = None,
) -> OfflineTransport:
    """Build the standard transport: live payRequest fixture + synthetic invoice fixture."""
    transport = OfflineTransport(events=events)
    transport.add(
        discovery_url,
        discovery_status,
        discovery_body if discovery_body is not None else fixture_bytes("lnurlp_sigidli_200.json"),
    )
    transport.add_callback(
        callback_url,
        callback_status,
        callback_body if callback_body is not None else fixture_bytes("invoice_callback_200.synthetic.json"),
    )
    return transport


@pytest.fixture()
def transport() -> OfflineTransport:
    return make_transport()


def mutate_pay_request_document(**overrides: Mapping[str, Any]) -> bytes:
    """Return the live 200 body with fields replaced/removed (``None`` removes)."""
    import json

    document = json.loads(fixture_bytes("lnurlp_sigidli_200.json"))
    for key, value in overrides.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    return json.dumps(document).encode("utf-8")
