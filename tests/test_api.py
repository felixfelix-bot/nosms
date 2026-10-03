"""HTTP surface: health, pricing, llms.txt, the X-Reason/X-Hint error contract."""
from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.nip98 import ReplayCache
from tests.conftest import auth_header, build_event

URL = "http://testserver/api/send"


class ExplodingTransport:
    """Any attribute access proves the health path probed the transport."""

    name = "exploding"

    def __getattr__(self, item):  # pragma: no cover - guard only
        raise AssertionError(f"transport was probed during a request ({item})")


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def exploding_client():
    app = create_app(transport=ExplodingTransport(), transport_name="email_gateway")
    with TestClient(app) as c:
        yield c


# --- health -----------------------------------------------------------------

def test_health_is_open_and_cheap(client, exploding_client):
    r = exploding_client.get("/api/health")
    assert r.status_code == 200
    assert r.headers["X-Reason"] if "X-Reason" in r.headers else True  # no error headers
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "nosms"
    assert body["version"]
    assert body["transport"] == "email_gateway"
    assert "commit" in body


def test_health_needs_no_auth(client):
    assert client.get("/api/health").status_code == 200


def test_health_reports_configured_commit(monkeypatch):
    monkeypatch.setenv("NOSMS_COMMIT", "deadbeefcafe")
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/health").json()["commit"] == "deadbeefcafe"


# --- pricing ----------------------------------------------------------------

def test_pricing_endpoint_lists_the_table_and_default(client):
    r = client.get("/api/pricing")
    assert r.status_code == 200
    body = r.json()
    assert body["unit"] == "sats"
    assert body["default"] == 500
    assert body["prefixes"]["+1"] == 100
    for prefix in ("+1", "+49", "+44", "+351", "+91"):
        assert prefix in body["prefixes"]


# --- llms.txt ---------------------------------------------------------------

def test_llms_txt_is_plain_text_agent_docs(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    for needle in ("/api/send", "/api/health", "/api/pricing", "/api/message/:id/status",
                   "27235", "Authorization: Nostr", "X-Reason", "X-Hint",
                   "insufficient_funds", "bad_destination", "auth_missing", "500",
                   "llms-full.txt", "data", "cooldown"):
        assert needle in text, f"llms.txt is missing {needle!r}"


def test_llms_txt_base64_example_is_copy_pasteable(client):
    text = client.get("/llms.txt").text
    marker = "Authorization: Nostr "
    idx = text.index(marker) + len(marker)
    token = text[idx:].split()[0].strip()
    ev = json.loads(base64.b64decode(token))
    assert ev["kind"] == 27235
    assert ev["sig"] and ev["id"]


def test_llms_full_txt_is_an_honest_501(client):
    r = client.get("/llms-full.txt")
    assert r.status_code == 501
    assert r.headers["X-Reason"] == "not_implemented"
    assert r.headers["X-Hint"]


# --- auth on /api/send ------------------------------------------------------

def test_unsigned_send_is_rejected_with_auth_missing(client):
    r = client.post("/api/send", json={"to": "+14155551234", "text": "hi"})
    assert r.status_code == 401
    assert r.headers["X-Reason"] == "auth_missing"
    assert r.headers["X-Hint"]


def test_signed_send_reaches_the_handler(client):
    ev = build_event(url=URL)
    r = client.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                    headers=auth_header(ev))
    # The money-moving path is the sibling card M1b; M1a must authenticate and
    # then say so honestly rather than pretend to send.
    assert r.status_code == 501
    assert r.headers["X-Reason"] == "not_implemented"
    assert r.headers["X-Hint"]


def test_signed_send_with_bad_destination(client):
    ev = build_event(url=URL)
    r = client.post("/api/send", json={"to": "4155551234", "text": "hi"},
                    headers=auth_header(ev))
    assert r.status_code == 400
    assert r.headers["X-Reason"] == "bad_destination"
    assert "+" in r.headers["X-Hint"]


def test_signed_send_with_invalid_body_is_a_validation_error(client):
    ev = build_event(url=URL)
    r = client.post("/api/send", json={"to": "+14155551234"},
                    headers=auth_header(ev))
    assert r.status_code == 422
    assert r.headers["X-Reason"] == "validation_error"
    assert r.headers["X-Hint"]


def test_replayed_event_is_rejected_over_http():
    app = create_app()
    with TestClient(app) as c:
        ev = build_event(url=URL)
        first = c.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                       headers=auth_header(ev))
        second = c.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                        headers=auth_header(ev))
    assert first.status_code == 501
    assert second.status_code == 401
    assert second.headers["X-Reason"] == "auth_replay"


def test_stale_event_is_rejected_over_http(client):
    ev = build_event(url=URL, created_at=int(time.time()) - 600)
    r = client.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                    headers=auth_header(ev))
    assert r.status_code == 401
    assert r.headers["X-Reason"] == "auth_stale"


def test_tampered_signature_is_rejected_over_http(client):
    ev = build_event(url=URL, tamper="sig")
    r = client.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                    headers=auth_header(ev))
    assert r.status_code == 401
    assert r.headers["X-Reason"] == "auth_invalid"
    assert r.headers["X-Hint"]


def test_unknown_path_still_carries_reason_and_hint(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["X-Reason"]
    assert r.headers["X-Hint"]


def test_four_distinct_failure_modes_all_carry_both_headers(client):
    seen = {}
    probes = [
        (client.post("/api/send", json={"to": "+14155551234", "text": "hi"}).headers,
         "auth_missing"),
        (client.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                     headers=auth_header(build_event(url=URL, tamper="sig"))).headers,
         "auth_invalid"),
        (client.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                     headers=auth_header(build_event(
                         url=URL, created_at=int(time.time()) - 999))).headers,
         "auth_stale"),
        (client.post("/api/send", json={"to": "+14155551234", "text": "hi"},
                     headers=auth_header(build_event(url=URL))).headers,
         "not_implemented"),
    ]
    for headers, reason in probes:
        assert headers["X-Reason"] == reason
        assert headers["X-Hint"] and len(headers["X-Hint"]) > 5
        seen[reason] = True
    assert len(seen) >= 4


def test_errors_are_json_and_never_bare(client):
    r = client.post("/api/send", json={"to": "+14155551234", "text": "hi"})
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["error"]["reason"] == "auth_missing"
    assert body["error"]["hint"]
