"""Offline test harness: fixtures captured from the live 8333.mobi endpoint.

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
No test builds a :class:`~nosms.lnurl_pay.UrllibTransport`. Every request goes
through :class:`OfflineTransport`, which raises ``AssertionError`` for any URL
it was not explicitly handed. A test that accidentally tries to reach the
network therefore fails loudly instead of quietly passing.
"""

from __future__ import annotations

import pathlib
from typing import Any, Mapping, Optional

import pytest

from nosms.lnurl_pay import DEFAULT_TIMEOUT, HttpResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: The two live endpoints cited in every test module.
MACHANKURA_USER_URL = "https://8333.mobi/.well-known/lnurlp/sigidli"
MACHANKURA_CALLBACK = "https://8333.mobi/.well-known/lnurlp/sigidli"
MACHANKURA_UNKNOWN_URL = "https://8333.mobi/.well-known/lnurlp/254700000000"

MACHANKURA_USER = "sigidli@8333.mobi"
MACHANKURA_UNKNOWN_PHONE = "254700000000@8333.mobi"


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
