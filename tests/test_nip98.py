"""NIP-98 (kind 27235) verification: signature, tags, freshness, replay."""
from __future__ import annotations

import base64
import json
import time

import pytest

from app.nip98 import FRESHNESS_SECONDS, Nip98Error, ReplayCache, verify_nip98_header
from tests.conftest import NOSTR_SK, OTHER_SK, auth_header, build_event

URL = "http://testserver/api/send"


def header_for(ev):
    return auth_header(ev)["Authorization"]


def test_valid_event_returns_signer_pubkey():
    ev = build_event(OTHER_SK, url=URL)
    pubkey = verify_nip98_header(header_for(ev), url=URL, method="POST",
                                 replay_cache=ReplayCache())
    assert pubkey == ev["pubkey"]
    assert pubkey != build_event(NOSTR_SK, url=URL)["pubkey"]


def test_freshness_window_is_120_seconds():
    assert FRESHNESS_SECONDS == 120


def test_accepts_event_at_the_edge_of_the_window():
    now = int(time.time())
    for created_at in (now - 119, now + 119):
        ev = build_event(url=URL, created_at=created_at)
        assert verify_nip98_header(header_for(ev), url=URL, method="POST",
                                   replay_cache=ReplayCache(), now=now) == ev["pubkey"]


@pytest.mark.parametrize("offset", [-121, -3600, 121, 3600])
def test_stale_or_future_created_at_is_rejected(offset):
    now = int(time.time())
    ev = build_event(url=URL, created_at=now + offset)
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header_for(ev), url=URL, method="POST",
                            replay_cache=ReplayCache(), now=now)
    assert exc.value.reason == "auth_stale"


def test_wrong_kind_is_rejected():
    ev = build_event(url=URL, kind=1)
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header_for(ev), url=URL, method="POST",
                            replay_cache=ReplayCache())
    assert exc.value.reason == "auth_invalid"


def test_u_tag_mismatch_is_rejected():
    ev = build_event(url="http://testserver/api/other")
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header_for(ev), url=URL, method="POST",
                            replay_cache=ReplayCache())
    assert exc.value.reason == "auth_invalid"


def test_method_tag_mismatch_is_rejected():
    ev = build_event(url=URL, method="GET")
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header_for(ev), url=URL, method="POST",
                            replay_cache=ReplayCache())
    assert exc.value.reason == "auth_invalid"


def test_query_string_on_the_u_tag_is_tolerated():
    ev = build_event(url=URL + "?debug=1")
    assert verify_nip98_header(header_for(ev), url=URL, method="POST",
                               replay_cache=ReplayCache()) == ev["pubkey"]


def test_host_scheme_or_path_mismatch_is_not_tolerated():
    for wrong in ("https://testserver/api/send", "http://evil.test/api/send",
                  "http://testserver/api/send/../health"):
        ev = build_event(url=wrong)
        with pytest.raises(Nip98Error) as exc:
            verify_nip98_header(header_for(ev), url=URL, method="POST",
                                replay_cache=ReplayCache())
        assert exc.value.reason == "auth_invalid", wrong


@pytest.mark.parametrize("tamper", ["sig", "content", "id"])
def test_tampered_event_is_rejected(tamper):
    ev = build_event(url=URL, tamper=tamper)
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header_for(ev), url=URL, method="POST",
                            replay_cache=ReplayCache())
    assert exc.value.reason == "auth_invalid"


@pytest.mark.parametrize("header", [None, "", "Bearer abc", "Nostr", "Nostr !!!not-base64",
                                    "Nostr " + base64.b64encode(b"not json").decode(),
                                    "Nostr " + base64.b64encode(b"{}").decode()])
def test_missing_or_malformed_header_is_rejected(header):
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header, url=URL, method="POST", replay_cache=ReplayCache())
    assert exc.value.reason in {"auth_missing", "auth_malformed"}, exc.value.reason


def test_missing_header_token_is_auth_missing():
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(None, url=URL, method="POST", replay_cache=ReplayCache())
    assert exc.value.reason == "auth_missing"


def test_replayed_event_is_rejected_with_the_same_cache():
    cache = ReplayCache()
    ev = build_event(url=URL)
    assert verify_nip98_header(header_for(ev), url=URL, method="POST", replay_cache=cache)
    with pytest.raises(Nip98Error) as exc:
        verify_nip98_header(header_for(ev), url=URL, method="POST", replay_cache=cache)
    assert exc.value.reason == "auth_replay"


def test_replay_cache_expires_after_its_ttl():
    cache = ReplayCache()
    now = int(time.time())
    ev = build_event(url=URL, created_at=now)
    assert verify_nip98_header(header_for(ev), url=URL, method="POST",
                               replay_cache=cache, now=now)
    later = now + cache.ttl + 1
    assert cache.ttl >= FRESHNESS_SECONDS
    # After the TTL the id is forgotten; freshness would then reject it anyway,
    # so prove the cache itself has forgotten the id.
    assert cache.contains(ev["id"], now=later) is False


def test_every_rejection_carries_reason_and_hint():
    cases = [
        (None, "auth_missing"),
        (header_for(build_event(url="http://wrong/")), "auth_invalid"),
        (header_for(build_event(url=URL, created_at=int(time.time()) - 999)), "auth_stale"),
        (header_for(build_event(url=URL, kind=7)), "auth_invalid"),
    ]
    for header, reason in cases:
        with pytest.raises(Nip98Error) as exc:
            verify_nip98_header(header, url=URL, method="POST", replay_cache=ReplayCache())
        assert exc.value.reason == reason
        assert exc.value.hint and len(exc.value.hint) > 10


def test_example_event_is_well_formed():
    """The copy-pasteable example shipped in llms.txt must be a real signature."""
    from app.llms import NIP98_EXAMPLE_JSON

    ev = json.loads(NIP98_EXAMPLE_JSON)
    assert ev["kind"] == 27235
    assert {"u", "method"} <= {t[0] for t in ev["tags"]}
    # Verify the example's own signature (no freshness check - it is a fixture).
    from app.nip98 import verify_signature_only

    assert verify_signature_only(ev) is True
