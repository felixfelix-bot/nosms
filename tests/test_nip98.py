"""RED-first tests for the NIP-98 auth module (nosms M1a).

Pinned behaviour:
- a freshly signed kind-27235 event for the exact request URL+method is accepted
- event id and BIP-340 signature are verified (never trusted from the payload)
- kind must be 27235, created_at inside the +/- freshness window
- u tag must match scheme+host+path (query comparison tolerant)
- method tag must match the HTTP method
- payload tag, when present, must match sha256(body)
- a replayed event id is rejected within the replay TTL
"""
from __future__ import annotations

import time

import pytest

from app.nip98 import Nip98Error, Nip98Verifier, verify_nip98

URL = "http://testserver/api/send"


def _check(event, *, url=URL, method="POST", body=None, now=None, verifier=None):
    verifier = verifier or Nip98Verifier(now=lambda: now or int(time.time()))
    return verifier.verify_event(event, url=url, method=method, body=body)


def test_valid_event_is_accepted_and_returns_pubkey(build):
    ev = build(url=URL, method="POST")
    pub = _check(ev)
    assert pub == ev["pubkey"]
    assert len(pub) == 64


def test_bad_signature_is_rejected(build):
    ev = build(url=URL, method="POST", override_sig="aa" * 64)
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_invalid"


def test_tampered_content_is_rejected(build):
    ev = build(url=URL, method="POST")
    ev["content"] = "tampered"
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_invalid"


def test_wrong_kind_is_rejected(build):
    ev = build(url=URL, method="POST", kind=1)
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_invalid"


def test_stale_created_at_is_rejected(build):
    ev = build(url=URL, method="POST", created_at=int(time.time()) - 121)
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_expired"


def test_freshness_lower_bound_is_rejected(build):
    ev = build(url=URL, method="POST", created_at=int(time.time()) + 999)
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_expired"


def test_replayed_event_id_is_rejected(build):
    v = Nip98Verifier(now=lambda: int(time.time()))
    ev = build(url=URL, method="POST")
    assert v.verify_event(ev, url=URL, method="POST") == ev["pubkey"]
    with pytest.raises(Nip98Error) as e:
        v.verify_event(ev, url=URL, method="POST")
    assert e.value.reason == "auth_replayed"


def test_url_path_mismatch_is_rejected(build):
    ev = build(url="http://testserver/api/other", method="POST")
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_url_mismatch"


def test_host_mismatch_is_rejected(build):
    ev = build(url="http://evil.example/api/send", method="POST")
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_url_mismatch"


def test_query_comparison_is_tolerant_of_extra_request_params(build):
    ev = build(url=URL + "?a=1", method="POST")
    # request carries an extra param the event did not sign -> still accepted
    assert _check(ev, url=URL + "?a=1&b=2")


def test_query_param_absent_from_request_is_rejected(build):
    ev = build(url=URL + "?a=1", method="POST")
    with pytest.raises(Nip98Error) as e:
        _check(ev, url=URL + "?b=2")
    assert e.value.reason == "auth_url_mismatch"


def test_method_mismatch_is_rejected(build):
    ev = build(url=URL, method="GET")
    with pytest.raises(Nip98Error) as e:
        _check(ev, method="POST")
    assert e.value.reason == "auth_method_mismatch"


def test_missing_u_tag_is_rejected(build):
    ev = build(url=URL, method="POST", omit_u=True)
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_invalid"


def test_missing_method_tag_is_rejected(build):
    ev = build(url=URL, method="POST", omit_method=True)
    with pytest.raises(Nip98Error) as e:
        _check(ev)
    assert e.value.reason == "auth_invalid"


def test_payload_tag_matches_body(build):
    body = b'{"to":"+15551234567"}'
    ev = build(url=URL, method="POST", body=body)
    assert _check(ev, body=body) == ev["pubkey"]


def test_payload_mismatch_is_rejected(build):
    ev = build(url=URL, method="POST", body=b"one")
    with pytest.raises(Nip98Error) as e:
        _check(ev, body=b"two")
    assert e.value.reason == "auth_payload_mismatch"


def test_header_parse_missing_is_auth_missing():
    with pytest.raises(Nip98Error) as e:
        verify_nip98(None, url=URL, method="POST")
    assert e.value.reason == "auth_missing"


def test_header_parse_bad_scheme_is_auth_invalid():
    with pytest.raises(Nip98Error) as e:
        verify_nip98("Bearer abc", url=URL, method="POST")
    assert e.value.reason == "auth_invalid"


def test_header_parse_bad_base64_is_auth_invalid():
    with pytest.raises(Nip98Error) as e:
        verify_nip98("Nostr !!!not-base64!!!", url=URL, method="POST")
    assert e.value.reason == "auth_invalid"


def test_every_error_carries_a_hint(build):
    for ev, kw in [
        (build(url=URL, method="POST", override_sig="aa" * 64), {}),
        (build(url=URL, method="POST", created_at=0), {}),
        (build(url=URL, method="GET"), {}),
    ]:
        with pytest.raises(Nip98Error) as e:
            _check(ev, **kw)
        assert e.value.hint and isinstance(e.value.hint, str)
        assert e.value.reason and e.value.hint
