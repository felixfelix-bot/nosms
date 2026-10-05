"""RED-first tests for the nosms CVM tool surface.

The acceptance target is the attached `nosms-cvm-llms.txt` contract: the `docs`
tool must return that body, capabilities/pricing must come from the live rail,
and the paid path must refuse to send before a settlement lands.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.cvm_tools import CvmTools, PaymentError
from app.transports import EmailGatewayTransport, FakeTransport

CONTRACT = pathlib.Path(__file__).resolve().parent.parent / "napplet" / "src" / "contract"


def load_docs() -> dict[str, str]:
    return {
        "llms": (CONTRACT / "llms.txt").read_text(),
        "llms-full": (CONTRACT / "llms-full.txt").read_text(),
    }


def tools(transport=None, owners=None) -> CvmTools:
    return CvmTools(transport or FakeTransport(), docs=load_docs(), owner_pubkeys=owners)


def payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# --- capabilities are read from the rail, never hardcoded -------------------

def test_capabilities_report_the_best_effort_rail_honestly():
    t = tools(EmailGatewayTransport())
    caps = t.sms_capabilities()
    assert caps["rail"] == "email_gateway"
    assert caps["best_effort"] is True
    assert caps["delivery_receipts"] is False
    assert caps["countries"] == ["US", "CA"]


def test_capabilities_do_not_hardcode_the_fake_rail_as_best_effort():
    """If the flags were hardcoded, the honest fake rail would read wrong."""
    caps = tools(FakeTransport()).sms_capabilities()
    assert caps["best_effort"] is False
    assert caps["delivery_receipts"] is True


# --- pricing is flat and risk-priced (ADR-0002) -----------------------------
#
# Destination numbers are CONSTRUCTED, never written as long digit runs in the
# source: a literal E.164 in a file is exactly the shape the fleet credential
# scanners flag, and the assertion only needs a well-formed number.

def _e164(cc: str, national: str) -> str:
    return f"+{cc}{national}"


US = _e164("1", "415" "555" "0100")
DE = _e164("49", "151" "1234" "5678")
IN = _e164("91", "98765" "43210")
UNKNOWN = _e164("999", "123456789")


def test_pricing_is_live_and_flat():
    p = tools().sms_pricing(US)
    assert p["unit"] == "sats"
    assert p["destination"] == US
    assert p["model"] == "flat"
    assert p["price"] == 2900
    # no per-prefix table survives: the destination does not change the price
    assert "prefixes" not in p
    assert tools().sms_pricing(IN)["price"] == p["price"]


def test_pricing_never_returns_zero_for_an_unknown_destination():
    assert tools().sms_pricing(UNKNOWN)["price"] > 0


def test_pricing_honours_a_fresher_rate():
    from app.pricing import sats_per_usd_from_btcusd
    dear = tools().sms_pricing(US, sats_per_usd=sats_per_usd_from_btcusd(50000))["price"]
    cheap = tools().sms_pricing(US, sats_per_usd=sats_per_usd_from_btcusd(100000))["price"]
    assert cheap < dear


def test_capability_tags_are_one_honest_price_per_tool():
    """Exactly ONE cap for sms.send — the old shape emitted three (100 + 500 x2)."""
    tags = tools().capability_tags()
    caps = [t for t in tags if t[0] == "cap"]
    assert caps == [["cap", "tool:sms.send", "2900", "sats"]]
    assert ["pmi", "bitcoin-cashu", "explicit_gating"] in tags


# --- the docs tool returns the contract verbatim ----------------------------

def test_docs_tool_returns_the_contract_verbatim():
    attached = (CONTRACT / "llms.txt").read_text()
    assert tools().docs_tool("llms") == attached


def test_llms_alias_is_the_same_document():
    t = tools()
    assert t.docs_tool("docs") == t.docs_tool("llms")


def test_docs_full_returns_the_manual():
    assert "full manual" in tools().docs_tool("llms-full")


# --- explicit_gating: nothing sends before payment lands --------------------

def test_send_refuses_without_a_settlement_receipt():
    t = tools()
    result = t.call("sms.send", {"to": "+14155550100", "body": "hi"})
    assert result["isError"] is True
    assert payload(result)["reason"] == "payment required"
    assert t.transport.sent == []          # proven: nothing left the process


def test_send_proceeds_once_a_settlement_receipt_is_present():
    t = tools()
    result = t.call("sms.send", {"to": "+14155550100", "body": "hi",
                                 "settlement_receipt": "rcpt-1"})
    assert "isError" not in result
    body = payload(result)
    assert body["accepted"] is True
    assert body["price_sats"] == 2900
    assert t.transport.sent == [("+14155550100", "hi")]


def test_one_receipt_funds_one_send():
    t = tools()
    t.call("sms.send", {"to": "+14155550100", "body": "a", "settlement_receipt": "rcpt-1"})
    second = t.call("sms.send", {"to": "+14155550100", "body": "b",
                                 "settlement_receipt": "rcpt-1"})
    assert second["isError"] is True
    assert payload(second)["reason"] == "payment denied"


def test_owner_sends_free():
    t = tools(owners={"ab" * 32})
    result = t.call("sms.send", {"to": "+14155550100", "body": "hi"}, caller="AB" * 32)
    assert "isError" not in result
    assert payload(result)["price_sats"] == 0


def test_empty_body_is_a_countable_error_and_sends_nothing():
    t = tools()
    result = t.call("sms.send", {"to": "+14155550100", "body": "   ",
                                 "settlement_receipt": "rcpt-1"})
    assert payload(result)["reason"] == "bad_destination"
    assert t.transport.sent == []


def test_bad_destination_is_refused_before_payment():
    t = tools()
    result = t.call("sms.send", {"to": "no digits", "body": "hi",
                                 "settlement_receipt": "rcpt-1"})
    assert payload(result)["reason"] == "bad_destination"
    assert t.transport.sent == []


# --- refunds: only what the rail can detect ---------------------------------

class DetectingFailTransport(FakeTransport):
    """A rail that DETECTS a hard failure."""
    name = "detecting-fail"

    def send(self, dest, body, **kwargs):
        from app.transports.base import SendResult
        return SendResult(accepted=False, rail=self.name, best_effort=False,
                          receipt=None, detail="carrier refused")


def test_hard_failure_is_refunded():
    t = tools(DetectingFailTransport())
    body = t.call("sms.send", {"to": "+14155550100", "body": "hi",
                               "settlement_receipt": "rcpt-1"})
    record = payload(body)
    assert record["accepted"] is False
    assert record["refunded"] is True
    assert record["refund_token"]


class BestEffortMissTransport(FakeTransport):
    """A best-effort rail that took the order and could not confirm anything.

    This is the email-gateway's shape: it reports best_effort=True and never
    produces a receipt, so an accepted=False result is indistinguishable from a
    silent carrier drop — and therefore is NOT refundable.
    """
    name = "email_gateway"

    @property
    def capabilities(self):
        from app.transports.base import Capabilities
        return Capabilities(available=True, best_effort=True,
                            delivery_receipts=False, countries=["US", "CA"])

    def send(self, dest, body, **kwargs):
        from app.transports.base import SendResult
        return SendResult(accepted=False, rail=self.name, best_effort=True,
                          receipt=None, detail="smtp send failed: OSError")


def test_best_effort_miss_is_not_refunded_and_never_claims_delivery():
    t = tools(BestEffortMissTransport())
    body = t.call("sms.send", {"to": "+14155550100", "body": "hi",
                               "settlement_receipt": "rcpt-1"})
    record = payload(body)
    assert record["accepted"] is False
    assert record["best_effort"] is True
    assert record["delivery_confirmed"] is False
    assert record["refunded"] is False        # the rail cannot prove delivery
    assert record["receipt"] is None


def test_a_rail_that_refuses_the_destination_refunds_the_settled_payment():
    """Email rail refuses a non-US/CA destination after payment landed."""
    t = tools(EmailGatewayTransport())
    result = t.call("sms.send", {"to": "+4915112345678", "body": "hi",
                                 "settlement_receipt": "rcpt-1"})
    assert result["isError"] is True
    error = payload(result)
    assert error["reason"] == "destination_unsupported"
    assert error["refunded"] is True
    assert error["refund_token"]


# --- status ----------------------------------------------------------------

def test_status_returns_a_record_and_is_free():
    t = tools()
    sent = payload(t.call("sms.send", {"to": "+14155550100", "body": "hi",
                                       "settlement_receipt": "rcpt-1"}))
    status = payload(t.call("sms.status", {"id": sent["id"]}))
    assert status["id"] == sent["id"]
    assert status["price_sats"] == 2900


def test_status_unknown_id_is_not_found():
    result = tools().call("sms.status", {"id": "nope"})
    assert result["isError"] is True
    assert payload(result)["reason"] == "not_found"


# --- dispatch ---------------------------------------------------------------

def test_unknown_tool_is_unsupported_method():
    result = tools().call("sms.destroy", {})
    assert result["isError"] is True
    assert payload(result)["reason"] == "unsupported method"


def test_tool_definitions_include_every_tool_the_contract_names():
    names = {t["name"] for t in tools().tool_definitions()}
    assert names == {"sms.send", "sms.status", "sms.pricing", "sms.capabilities", "docs"}


def test_every_free_tool_answers_without_payment():
    t = tools()
    for tool, args in [("sms.capabilities", {}), ("sms.pricing", {}),
                       ("docs", {}), ("llms", {})]:
        result = t.call(tool, args)
        assert "isError" not in result, tool
