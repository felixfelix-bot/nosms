"""RED-first tests for the Telnyx transport adapter.

The adapter is a *thin* wrapper: it imports the provider classes from the
`sms-gateway` package by path (never vendoring them) and does not duplicate the
provider's own request/response logic. All tests here are offline: the provider
is injected, so no Telnyx credential and no network are involved.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from app.transports.base import SendResult, Transport
from app.transports.telnyx import SmsGatewayNotFound, TelnyxTransport, load_sms_gateway

SMS_GATEWAY_PATH = pathlib.Path.home() / "repos" / "sms-gateway"


class FakeProvider:
    """Duck-typed stand-in for sms_gateway.providers.telnyx.TelnyxProvider."""

    provider_name = "telnyx"
    delivers = True
    sends = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.normalised = "sent"
        self.raw = "accepted"

    async def send_sms(self, to, body):
        self.sent.append((to, body))
        if not self.sends:
            return _obj(message_id="", status="failed", error="HTTP 422: bad from number")
        return _obj(message_id="tlnx-123", status="sent", error=None, cost=0.004)

    async def check_delivery(self, message_id):
        return _obj(message_id=message_id, status=self.normalised, error=None)

    async def raw_status(self, message_id):
        return self.raw


def _obj(**kw):
    return type("R", (), kw)


def test_adapter_satisfies_the_transport_protocol():
    t = TelnyxTransport(provider=FakeProvider())
    assert isinstance(t, Transport)
    assert t.name == "telnyx"
    assert t.capabilities.delivery_receipts is True
    assert t.capabilities.best_effort is False


@pytest.mark.anyio
async def test_send_maps_the_provider_result_onto_our_contract():
    p = FakeProvider()
    t = TelnyxTransport(provider=p)
    res = await t.send("+15551234567", "hello")
    assert isinstance(res, SendResult)
    assert res.accepted is True
    assert res.receipt == "tlnx-123"          # the provider's message id, pollable
    assert res.rail == "telnyx"
    assert p.sent == [("+15551234567", "hello")]


@pytest.mark.anyio
async def test_a_provider_rejection_is_not_accepted_and_keeps_the_reason():
    p = FakeProvider()
    p.sends = False
    t = TelnyxTransport(provider=p)
    res = await t.send("+15551234567", "hello")
    assert res.accepted is False
    assert "422" in res.detail
    assert res.refundable is True             # escrow must give the sats back


@pytest.mark.anyio
async def test_status_exposes_the_normalised_and_the_raw_provider_string():
    p = FakeProvider()
    p.normalised, p.raw = "sent", "accepted"
    st = await TelnyxTransport(provider=p).status("tlnx-123")
    assert st.normalized == "sent"
    assert st.raw == "accepted"


@pytest.mark.anyio
async def test_status_falls_back_to_a_telnyx_style_raw_read():
    """A real TelnyxProvider has no raw_status(); the adapter reads it itself."""

    class NoRawProvider:
        provider_name = "telnyx"

        async def send_sms(self, to, body):
            return _obj(message_id="tlnx-9", status="sent", error=None)

        async def check_delivery(self, message_id):
            return _obj(message_id=message_id, status="sent", error=None)

        def _get_client(self):
            return _StubClient("delivered")

    st = await TelnyxTransport(provider=NoRawProvider()).status("tlnx-9")
    assert st.normalized == "sent"
    assert st.raw == "delivered"           # read straight off the provider's API


class _StubClient:
    def __init__(self, raw):
        self.raw = raw

    async def get(self, path):
        return _obj(raise_for_status=lambda: None,
                    json=lambda: {"data": {"status": self.raw}})


# --- real construction from config ------------------------------------------

def test_load_sms_gateway_imports_the_package_by_path():
    mod = load_sms_gateway(str(SMS_GATEWAY_PATH))
    assert hasattr(mod, "TelnyxProvider")
    assert hasattr(mod, "TelnyxConfig")


def test_load_sms_gateway_explains_a_missing_path(tmp_path):
    with pytest.raises(SmsGatewayNotFound) as e:
        load_sms_gateway(str(tmp_path / "nope"))
    assert "sms-gateway" in str(e.value)


def test_from_config_builds_a_real_provider_and_reports_unconfigured(monkeypatch):
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    monkeypatch.delenv("TELNYX_FROM_NUMBER", raising=False)
    t = TelnyxTransport.from_config(sms_gateway_path=str(SMS_GATEWAY_PATH))
    # no credentials yet -> advertised honestly as unavailable, not as "up"
    assert t.capabilities.available is False
    assert t.name == "telnyx"


@pytest.mark.anyio
async def test_unconfigured_adapter_refuses_to_send_instead_of_failing_late():
    t = TelnyxTransport.from_config(sms_gateway_path=str(SMS_GATEWAY_PATH))
    res = await t.send("+15551234567", "hello")
    assert res.accepted is False
    assert "not configured" in res.detail.lower()
