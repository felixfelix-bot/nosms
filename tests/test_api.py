"""RED-first tests for the nosms M1a HTTP surface.

Offline end-to-end: the whole service is driven through an in-process ASGI
client with a FakeTransport. No live SMS, no live Telnyx, no outbound calls.
"""
from __future__ import annotations

import base64
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import BASE_URL, build_event, nip98_header


@pytest.fixture()
def client():
    return TestClient(create_app())


# --- health -----------------------------------------------------------------

def test_health_is_open_and_returns_documented_json(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "nosms"
    assert isinstance(body["version"], str) and body["version"]
    # provider name comes from config, never probed
    assert body["transport"] == "fake"
    assert "commit" in body


def test_health_needs_no_authorization(client):
    assert client.get("/api/health").status_code == 200


def test_health_reports_configured_provider_name(monkeypatch):
    app = create_app(config={"transport": "email_gateway", "commit": "deadbeef"})
    r = TestClient(app).get("/api/health")
    assert r.json()["transport"] == "email_gateway"
    assert r.json()["commit"] == "deadbeef"


# --- NIP-98 gate on /api/send ----------------------------------------------

def test_unsigned_send_is_401_auth_missing_with_headers(client):
    r = client.post("/api/send", json={"to": "+15551234567", "body": "hi"})
    assert r.status_code == 401
    assert r.headers["x-reason"] == "auth_missing"
    assert r.headers["x-hint"]


def test_signed_send_reaches_handler(client):
    header = nip98_header(url=f"{BASE_URL}/api/send", method="POST")
    r = client.post("/api/send", json={"to": "+15551234567", "body": "hi"},
                    headers={"Authorization": header})
    # the send path is the sibling card; reaching the handler is the point
    assert r.status_code == 501
    assert r.headers["x-reason"] == "send_not_implemented"
    assert r.headers["x-hint"]


def test_send_rejects_signature_for_other_url(client):
    header = nip98_header(url=f"{BASE_URL}/api/other", method="POST")
    r = client.post("/api/send", json={}, headers={"Authorization": header})
    assert r.status_code == 401
    assert r.headers["x-reason"] == "auth_url_mismatch"


def test_send_rejects_bad_signature(client):
    ev = build_event(url=f"{BASE_URL}/api/send", method="POST", override_sig="aa" * 64)
    r = client.post("/api/send", json={}, headers={"Authorization": nip98_header(ev)})
    assert r.status_code == 401
    assert r.headers["x-reason"] == "auth_invalid"


def test_send_rejects_stale_event(client):
    ev = build_event(url=f"{BASE_URL}/api/send", method="POST", created_at=1)
    r = client.post("/api/send", json={}, headers={"Authorization": nip98_header(ev)})
    assert r.status_code == 401
    assert r.headers["x-reason"] == "auth_expired"


def test_send_rejects_replayed_event(client):
    header = nip98_header(url=f"{BASE_URL}/api/send", method="POST")
    first = client.post("/api/send", json={}, headers={"Authorization": header})
    second = client.post("/api/send", json={}, headers={"Authorization": header})
    assert first.status_code == 501          # reached handler
    assert second.status_code == 401
    assert second.headers["x-reason"] == "auth_replayed"


# --- central error handler --------------------------------------------------

def test_unknown_route_has_reason_and_hint(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["x-reason"] == "not_found"
    assert r.headers["x-hint"]


def test_wrong_method_has_reason_and_hint(client):
    r = client.get("/api/send")
    assert r.status_code in (405, 401)
    assert r.headers["x-reason"]
    assert r.headers["x-hint"]


def test_at_least_four_distinct_failure_modes_carry_both_headers(client):
    seen = {}
    cases = [
        client.post("/api/send", json={}),                                   # auth_missing
        client.post("/api/send", json={}, headers={"Authorization": nip98_header(
            build_event(url=f"{BASE_URL}/api/send", method="POST", override_sig="aa" * 64))}),
        client.post("/api/send", json={}, headers={"Authorization": nip98_header(
            build_event(url=f"{BASE_URL}/api/send", method="POST", created_at=1))}),
        client.post("/api/send", json={}, headers={"Authorization": nip98_header(
            url=f"{BASE_URL}/api/zzz", method="POST")}),
        client.get("/api/nope"),
    ]
    for r in cases:
        assert r.headers.get("x-reason"), r.content
        assert r.headers.get("x-hint"), r.content
        seen[r.headers["x-reason"]] = r.status_code
    assert len(seen) >= 4, seen


# --- llms.txt ---------------------------------------------------------------

def test_llms_txt_is_plain_text_agent_docs(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # endpoints, auth, pricing units, error tokens, data stance
    assert "/api/health" in body and "/api/send" in body
    assert "NIP-98" in body and "27235" in body
    assert "Authorization: Nostr" in body
    assert "sats" in body and "X-Reason" in body and "X-Hint" in body
    assert "auth_missing" in body  # error token glossary


def test_llms_txt_has_copy_pasteable_base64_event(client):
    body = client.get("/llms.txt").text
    m = re.search(r"Authorization: Nostr ([A-Za-z0-9+/]+={0,2})", body)
    assert m, "no copy-pasteable `Authorization: Nostr <base64>` line in llms.txt"
    decoded = base64.b64decode(m.group(1), validate=True)
    assert b'"kind"' in decoded and b"27235" in decoded


def test_llms_full_txt_is_501_stub(client):
    r = client.get("/llms-full.txt")
    assert r.status_code == 501
    assert r.headers["x-reason"]
    assert r.headers["x-hint"]
