"""LNURL-pay resolution + invoice tests.

Everything here runs offline against the fixtures whose live provenance is
documented in ``tests/conftest.py`` (captured 2026-09-27T00:39Z):

* ``lnurlp_sigidli_200.json``         — the real 200 answer for
  ``https://8333.mobi/.well-known/lnurlp/sigidli`` (457 bytes)
* ``lnurlp_unknown_user_404.json``    — the real 404 answer for
  ``https://8333.mobi/.well-known/lnurlp/254700000000`` (83 bytes)
* ``invoice_callback_200.synthetic.json`` — synthetic, LUD-06 shaped (we never
  ask Machankura to mint an invoice we will not pay)
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from conftest import (
    MACHANKURA_CALLBACK,
    MACHANKURA_UNKNOWN_PHONE,
    MACHANKURA_UNKNOWN_URL,
    MACHANKURA_USER,
    MACHANKURA_USER_URL,
    OfflineTransport,
    fixture_bytes,
    make_transport,
    mutate_pay_request_document,
)
from nosms import lnurl_pay
from nosms.lnurl_pay import (
    MACHANKURA_HOST,
    AmountOutOfRange,
    CommentError,
    CommentNotAllowed,
    CommentTooLong,
    HttpResult,
    InvalidPayRequest,
    InvoiceRequestFailed,
    LightningAddress,
    MalformedAddress,
    NotAPayRequest,
    ServiceError,
    TransportFailure,
    UnknownUser,
    UrllibTransport,
    invoice_callback_url,
    request_invoice,
    resolve,
    resolve_url,
)


# --------------------------------------------------------------------------- #
# fixture integrity — the fixtures must still be the bytes the service sent
# --------------------------------------------------------------------------- #
def test_live_fixtures_match_the_captured_content_lengths():
    """457 / 83 are the Content-Lengths observed live on 2026-09-27."""
    assert len(fixture_bytes("lnurlp_sigidli_200.json")) == 457
    assert len(fixture_bytes("lnurlp_unknown_user_404.json")) == 83


def test_live_fixture_is_the_documented_pay_request():
    document = json.loads(fixture_bytes("lnurlp_sigidli_200.json"))
    assert document["tag"] == "payRequest"
    assert document["commentAllowed"] == 60
    assert document["nostrPubkey"].startswith("9b2b23d5")
    assert document["metadata"].startswith("[[\"text/identifier\",\"sigidli@8333.mobi\"]")


# --------------------------------------------------------------------------- #
# address parsing
# --------------------------------------------------------------------------- #
def test_parses_a_machankura_username_address():
    address = LightningAddress.parse(MACHANKURA_USER)
    assert (address.user, address.host) == ("sigidli", MACHANKURA_HOST)
    assert address.identifier == MACHANKURA_USER
    assert address.is_machankura is True
    assert str(address) == MACHANKURA_USER


def test_parses_a_machankura_phone_address():
    address = LightningAddress.parse("254700000000@8333.mobi")
    assert address.user == "254700000000"
    assert address.is_machankura is True


def test_parses_a_generic_lightning_address_without_machankura_special_casing():
    address = LightningAddress.parse("alice@wallet.example.org")
    assert address.identifier == "alice@wallet.example.org"
    assert address.is_machankura is False


def test_parse_folds_case_and_a_trailing_root_dot():
    address = LightningAddress.parse("  Sigidli@8333.MOBI.  ")
    assert address.identifier == MACHANKURA_USER


def test_parse_accepts_plus_and_underscore_local_parts():
    assert LightningAddress.parse("a.b+c_d@host.example").user == "a.b+c_d"


@pytest.mark.parametrize(
    "raw, reason_fragment",
    [
        (123, "expected a string"),
        ("", "empty address"),
        ("   ", "empty address"),
        ("sigidli", "exactly one '@'"),
        ("@8333.mobi", "both the user and the host are required"),
        ("sigidli@", "both the user and the host are required"),
        ("a@b@c.com", "exactly one '@'"),
        ("sig idli@8333.mobi", "contains whitespace"),
        ("Sigidli!@8333.mobi", "invalid local part"),
        (".sigidli@8333.mobi", "invalid local part"),
        ("sigidli@localhost", "invalid host"),
        ("sigidli@-bad.com", "invalid host"),
        ("sigidli@bad-.com", "invalid host"),
        ("sigidli@8333..mobi", "invalid host"),
    ],
)
def test_parse_rejects_malformed_addresses(raw, reason_fragment):
    with pytest.raises(MalformedAddress) as excinfo:
        LightningAddress.parse(raw)
    assert reason_fragment in str(excinfo.value)
    assert excinfo.value.reason


def test_parse_normalises_a_pasted_international_phone_number():
    address = LightningAddress.from_phone("+255 679 066 977")
    assert address.identifier == "255679066977@8333.mobi"
    assert LightningAddress.from_phone("+256-744-830-624").user == "256744830624"
    assert LightningAddress.from_phone("254700000000").is_machankura is True
    assert LightningAddress.from_phone("254700000000", host="other.example").host == "other.example"


@pytest.mark.parametrize("raw", ["", "12345", "+", "2547000000000000000", 254700000000])
def test_from_phone_rejects_non_numbers(raw):
    with pytest.raises(MalformedAddress):
        LightningAddress.from_phone(raw)


def test_resolve_url_is_https_and_percent_encodes_the_identifier():
    assert resolve_url(LightningAddress.parse(MACHANKURA_USER)) == MACHANKURA_USER_URL
    assert resolve_url(LightningAddress.parse(MACHANKURA_UNKNOWN_PHONE)) == MACHANKURA_UNKNOWN_URL
    assert resolve_url(LightningAddress.parse("a+b@host.example")) == "https://host.example/.well-known/lnurlp/a%2Bb"


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_resolve_parses_the_live_pay_request(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)

    assert transport.calls == [MACHANKURA_USER_URL]
    assert pay_request.callback == MACHANKURA_CALLBACK
    assert pay_request.tag == "payRequest"
    assert pay_request.min_sendable_msat == 1000
    assert pay_request.max_sendable_msat == 1_000_000_000
    assert pay_request.min_sendable_sats == 1
    assert pay_request.max_sendable_sats == 1_000_000
    assert pay_request.comment_allowed == 60
    assert pay_request.allows_nostr is True
    assert pay_request.nostr_pubkey == "9b2b23d5f8a7112903c4dc600b0a92c4317918fcfcfbd2bf55a758f1b9a4feca"
    assert pay_request.identifier == MACHANKURA_USER
    assert pay_request.is_machankura is True
    assert pay_request.raw["tag"] == "payRequest"
    assert ["text/identifier", MACHANKURA_USER] in pay_request.metadata_entries()


def test_resolve_accepts_an_already_parsed_address(transport):
    assert resolve(LightningAddress.parse(MACHANKURA_USER), transport=transport).identifier == MACHANKURA_USER


def test_resolve_maps_machankuras_404_to_unknown_user(transport):
    transport = make_transport(
        discovery_url=MACHANKURA_UNKNOWN_URL,
        discovery_status=404,
        discovery_body=fixture_bytes("lnurlp_unknown_user_404.json"),
    )
    with pytest.raises(UnknownUser) as excinfo:
        resolve(MACHANKURA_UNKNOWN_PHONE, transport=transport)

    error = excinfo.value
    assert error.reason == "User 254700000000 doesn't use the Machankura service."
    assert error.identifier == MACHANKURA_UNKNOWN_PHONE
    assert error.status_code == 404
    assert "doesn't use the Machankura service" in str(error)


def test_resolve_404_without_a_reason_still_raises_unknown_user(transport):
    transport = make_transport(discovery_status=404, discovery_body=b'{"status":"error"}')
    with pytest.raises(UnknownUser) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert excinfo.value.reason == "unknown user"


@pytest.mark.parametrize("status", [301, 400, 429, 500, 503])
def test_resolve_reports_any_other_status_as_a_service_error(transport, status):
    transport = make_transport(discovery_status=status, discovery_body=b'{"reason":"nope"}')
    with pytest.raises(ServiceError) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert excinfo.value.status_code == status
    assert excinfo.value.reason == "nope"


def test_resolve_reports_a_non_json_body_as_a_service_error(transport):
    transport = make_transport(discovery_body=b"<html>cloudflare</html>")
    with pytest.raises(ServiceError) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert "not JSON" in str(excinfo.value)


def test_resolve_reports_a_200_error_document_as_a_service_error(transport):
    transport = make_transport(
        discovery_body=json.dumps({"status": "ERROR", "reason": "temporarily unavailable"}).encode()
    )
    with pytest.raises(ServiceError) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert excinfo.value.reason == "temporarily unavailable"


def test_resolve_rejects_a_non_pay_request_tag(transport):
    transport = make_transport(discovery_body=mutate_pay_request_document(tag="withdrawRequest"))
    with pytest.raises(NotAPayRequest) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert excinfo.value.tag == "withdrawRequest"


def test_resolve_rejects_a_json_array_document(transport):
    transport = make_transport(discovery_body=b"[1, 2, 3]")
    with pytest.raises(InvalidPayRequest) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert "expected a JSON object" in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"callback": None}, "missing callback"),
        ({"callback": ""}, "missing callback"),
        ({"callback": 42}, "missing callback"),
        ({"callback": "not-a-url"}, "not an absolute URL"),
        ({"callback": "http://8333.mobi/.well-known/lnurlp/sigidli"}, "refusing a non-https callback"),
        ({"minSendable": None}, "minSendable must be an integer"),
        ({"minSendable": True}, "minSendable must be an integer"),
        ({"minSendable": -1}, "minSendable must not be negative"),
        ({"maxSendable": "1000"}, "maxSendable must be an integer"),
        ({"minSendable": 2000, "maxSendable": 1000}, "minSendable 2000 > maxSendable 1000"),
        ({"maxSendable": 0}, "maxSendable must be > 0"),
        ({"metadata": None}, "metadata must be a JSON-encoded string"),
        ({"metadata": ["text/identifier", "sigidli@8333.mobi"]}, "metadata must be a JSON-encoded string"),
        ({"metadata": "not json"}, "metadata is not valid JSON"),
        ({"metadata": '{"a": 1}'}, "metadata must decode to a JSON array"),
        ({"allowsNostr": "yes"}, "allowsNostr must be a boolean"),
        ({"nostrPubkey": "deadbeef"}, "nostrPubkey is not 64-char hex"),
        ({"nostrPubkey": 12345}, "nostrPubkey is not 64-char hex"),
        ({"nostrPubkey": None}, "allowsNostr is true but nostrPubkey is missing"),
        ({"commentAllowed": -1}, "commentAllowed must be a non-negative integer"),
        ({"commentAllowed": True}, "commentAllowed must be a non-negative integer"),
    ],
)
def test_resolve_rejects_an_invalid_pay_request(transport, overrides, fragment):
    transport = make_transport(discovery_body=mutate_pay_request_document(**overrides))
    with pytest.raises(InvalidPayRequest) as excinfo:
        resolve(MACHANKURA_USER, transport=transport)
    assert fragment in str(excinfo.value)


def test_resolve_accepts_defaults_for_optional_fields(transport):
    transport = make_transport(
        discovery_body=mutate_pay_request_document(
            **{"allowsNostr": None, "nostrPubkey": None, "commentAllowed": None}
        )
    )
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    assert pay_request.allows_nostr is False
    assert pay_request.nostr_pubkey is None
    assert pay_request.comment_allowed == 0


def test_upstream_transport_failure_is_typed_not_a_bare_urlerror(monkeypatch):
    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("name or service not known")

    monkeypatch.setattr(lnurl_pay.urllib.request, "urlopen", boom)
    with pytest.raises(TransportFailure) as excinfo:
        resolve(MACHANKURA_USER, transport=UrllibTransport())
    assert "name or service not known" in str(excinfo.value)


def test_upstream_oserror_is_typed(monkeypatch):
    def boom(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(lnurl_pay.urllib.request, "urlopen", boom)
    with pytest.raises(TransportFailure):
        resolve(MACHANKURA_USER, transport=UrllibTransport())


def test_urllib_transport_reads_a_success_body(monkeypatch):
    class _Response:
        status = 200
        headers: dict[str, str] = {}

        def read(self) -> bytes:
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(lnurl_pay.urllib.request, "urlopen", lambda *a, **k: _Response())
    result = UrllibTransport().get(MACHANKURA_USER_URL)
    assert (result.status_code, result.body, result.text) == (200, b'{"ok":true}', '{"ok":true}')


def test_urllib_transport_reads_the_error_body_of_a_404(monkeypatch):
    body = fixture_bytes("lnurlp_unknown_user_404.json")

    def raise_404(*_args, **_kwargs):
        raise urllib.error.HTTPError(MACHANKURA_UNKNOWN_URL, 404, "Not Found", {}, io.BytesIO(body))

    monkeypatch.setattr(lnurl_pay.urllib.request, "urlopen", raise_404)
    result = UrllibTransport().get(MACHANKURA_UNKNOWN_URL)
    assert result.status_code == 404
    assert b"doesn't use the Machankura service" in result.body


# --------------------------------------------------------------------------- #
# amount + comment validation (all local, all before any HTTP)
# --------------------------------------------------------------------------- #
def test_check_amount_accepts_the_advertised_boundaries(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    assert pay_request.check_amount(1000) == 1000
    assert pay_request.check_amount(1_000_000_000) == 1_000_000_000


@pytest.mark.parametrize("amount", [999, 1_000_000_001])
def test_check_amount_rejects_out_of_range(transport, amount):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(AmountOutOfRange) as excinfo:
        pay_request.check_amount(amount)
    assert (excinfo.value.amount_msat, excinfo.value.min_msat, excinfo.value.max_msat) == (
        amount,
        1000,
        1_000_000_000,
    )


@pytest.mark.parametrize("amount", ["1000", 10.5, True, None])
def test_check_amount_rejects_non_integers(transport, amount):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(TypeError):
        pay_request.check_amount(amount)


@pytest.mark.parametrize("amount", [0, -1])
def test_check_amount_rejects_non_positive(transport, amount):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(ValueError):
        pay_request.check_amount(amount)


def test_check_comment_accepts_none_and_the_exact_limit(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    assert pay_request.check_comment(None) is None
    assert pay_request.check_comment("x" * 60) == "x" * 60


def test_check_comment_rejects_one_char_over_the_advertised_limit(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(CommentTooLong) as excinfo:
        pay_request.check_comment("x" * 61)
    assert (excinfo.value.limit, excinfo.value.attempted) == (60, 61)


def test_check_comment_refuses_a_comment_the_receiver_never_allowed(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    no_comments = mutate_pay_request_document(commentAllowed=0)
    transport = make_transport(discovery_body=no_comments)
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(CommentNotAllowed) as excinfo:
        pay_request.check_comment("anything at all")
    assert excinfo.value.limit == 0
    assert isinstance(excinfo.value, CommentError)


def test_check_comment_rejects_a_non_string(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(CommentError) as excinfo:
        pay_request.check_comment(b"bytes")
    assert excinfo.value.attempted == -1


# --------------------------------------------------------------------------- #
# invoice requests
# --------------------------------------------------------------------------- #
def test_invoice_callback_url_is_the_hand_written_expected_string(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    assert invoice_callback_url(pay_request, 100_000) == f"{MACHANKURA_CALLBACK}?amount=100000"
    assert (
        invoice_callback_url(pay_request, 100_000, "nosms-12")
        == f"{MACHANKURA_CALLBACK}?amount=100000&comment=nosms-12"
    )
    assert (
        invoice_callback_url(pay_request, 100_000, "ref: AB")
        == f"{MACHANKURA_CALLBACK}?amount=100000&comment=ref%3A+AB"
    )


def test_invoice_callback_url_preserves_query_parameters_already_in_the_callback(transport):
    transport = make_transport(
        discovery_body=mutate_pay_request_document(callback="https://8333.mobi/.well-known/lnurlp/sigidli?k=v")
    )
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    assert invoice_callback_url(pay_request, 5) == (
        "https://8333.mobi/.well-known/lnurlp/sigidli?k=v&amount=5"
    )


def test_request_invoice_returns_the_callback_invoice(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    transport.calls.clear()

    invoice = request_invoice(pay_request, 100_000, transport=transport)

    assert transport.calls == [f"{MACHANKURA_CALLBACK}?amount=100000"]
    assert invoice.pr.startswith("ln")
    assert invoice.amount_msat == 100_000
    assert invoice.comment is None
    assert invoice.identifier == MACHANKURA_USER
    assert invoice.raw["routes"] == []


def test_request_invoice_forwards_a_comment_that_fits(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    transport.calls.clear()

    invoice = request_invoice(pay_request, 100_000, "ref 42", transport=transport)

    assert transport.calls == [f"{MACHANKURA_CALLBACK}?amount=100000&comment=ref+42"]
    assert invoice.comment == "ref 42"


def test_request_invoice_refuses_an_overlong_comment_instead_of_sending_it(transport):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    transport.calls.clear()

    with pytest.raises(CommentTooLong):
        request_invoice(pay_request, 100_000, "x" * 61, transport=transport)

    assert transport.calls == [], "an over-long comment must not reach the receiver at all"


def test_request_invoice_refuses_a_comment_when_the_receiver_allows_none(transport):
    transport = make_transport(discovery_body=mutate_pay_request_document(commentAllowed=0))
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    transport.calls.clear()

    with pytest.raises(CommentNotAllowed):
        request_invoice(pay_request, 100_000, "ref-42", transport=transport)

    assert transport.calls == []


@pytest.mark.parametrize("amount", [999, 1_000_000_001])
def test_request_invoice_refuses_an_out_of_range_amount_without_calling_the_callback(transport, amount):
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    transport.calls.clear()
    with pytest.raises(AmountOutOfRange):
        request_invoice(pay_request, amount, transport=transport)
    assert transport.calls == []


def test_request_invoice_maps_a_callback_error_document(transport):
    transport = make_transport(
        callback_body=json.dumps({"status": "ERROR", "reason": "amount too small"}).encode()
    )
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(InvoiceRequestFailed) as excinfo:
        request_invoice(pay_request, 1000, transport=transport)
    assert excinfo.value.reason == "amount too small"
    assert excinfo.value.callback == MACHANKURA_CALLBACK


def test_request_invoice_maps_a_callback_http_error(transport):
    transport = make_transport(callback_status=503, callback_body=b'{"reason":"try later"}')
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(InvoiceRequestFailed) as excinfo:
        request_invoice(pay_request, 1000, transport=transport)
    assert excinfo.value.status_code == 503
    assert excinfo.value.reason == "try later"


@pytest.mark.parametrize(
    "body, fragment",
    [
        (b'{"routes":[]}', "no bolt11 invoice"),
        (b'{"pr":null}', "no bolt11 invoice"),
        (b'{"pr":"http://not-an-invoice"}', "no bolt11 invoice"),
        (b'[1,2]', "expected a JSON object"),
    ],
)
def test_request_invoice_rejects_a_body_without_a_bolt11_invoice(transport, body, fragment):
    transport = make_transport(callback_body=body)
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(InvoiceRequestFailed) as excinfo:
        request_invoice(pay_request, 1000, transport=transport)
    assert fragment in str(excinfo.value)


def test_request_invoice_reports_a_non_json_callback_body_as_a_service_error(transport):
    transport = make_transport(callback_body=b"not json at all")
    pay_request = resolve(MACHANKURA_USER, transport=transport)
    with pytest.raises(ServiceError):
        request_invoice(pay_request, 1000, transport=transport)


def test_offline_transport_refuses_an_unregistered_url():
    """The guard that keeps this suite honest: any stray URL is a hard failure."""
    with pytest.raises(AssertionError) as excinfo:
        OfflineTransport().get("https://8333.mobi/.well-known/lnurlp/someone-else")
    assert "must stay offline" in str(excinfo.value)


def test_http_result_exposes_text_and_url():
    result = HttpResult(url=MACHANKURA_USER_URL, status_code=200, body=b'{"a":1}')
    assert result.text == '{"a":1}'
    assert result.url == MACHANKURA_USER_URL
