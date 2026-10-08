"""Tests for the CVM JSON-RPC dispatch — the layer where silence used to live.

The regression that matters: the server published a "reply" for an unpaid
`sms.send` that the client never received, so the caller saw nothing at all. At
this layer the invariant is exact: **every request with an `id` gets a response
with the same `id`**, including every error. Only a true notification is quiet.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.cvm import ContractContext, jsonrpc_error, jsonrpc_result
from app.cvm_rpc import PROTOCOL_VERSION, handle_rpc
from app.cvm_tools import CvmTools
from app.escrow import EscrowStore
from app.pricing import DEFAULT_PRICE_SATS
from app.transports import FakeTransport
from tests.stubs import StubMint, make_token

CONTRACT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "cvm"


def tools(mint=None) -> CvmTools:
    return CvmTools(
        FakeTransport(), mint=mint, escrow=EscrowStore(":memory:"),
        docs={"llms": (CONTRACT / "llms.txt").read_text(),
              "llms-full": (CONTRACT / "llms-full.txt").read_text()},
        contract=ContractContext(npub="npub1testserver"))


def req(method: str, params=None, rpc_id="1") -> dict:
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


# --- the invariant: an id gets an answer -----------------------------------

@pytest.mark.parametrize("rpc", [
    req("initialize"),
    req("tools/list"),
    req("resources/list"),
    req("tools/call", {"name": "sms.capabilities"}),
    req("tools/call", {"name": "sms.send", "arguments": {"to": "+15555550100",
                                                        "body": "hi"}}),
    req("tools/call", {"name": "sms.status", "arguments": {"id": "nope"}}),
    req("tools/call", {"name": "no.such.tool"}),
    req("resources/read", {"uri": "nosms://llms.txt"}),
    req("resources/read", {"uri": "nosms://missing.txt"}),
    req("no/such/method"),
    req("tools/call"),                        # no params at all
    req("tools/call", {"name": ""}),          # bad params
]
)
def test_every_request_with_an_id_gets_a_matching_response(rpc):
    out = handle_rpc(rpc, tools(StubMint(fee_ppk=0)), caller="ab" * 32)
    assert out is not None, f"no response for {rpc['method']}"
    assert out["jsonrpc"] == "2.0"
    assert out["id"] == rpc["id"]
    assert ("result" in out) ^ ("error" in out)          # exactly one of the two


def test_unpaid_send_over_json_rpc_is_a_VISIBLE_error_result():
    """The regression: an unpaid call must come back as a readable error."""
    out = handle_rpc(req("tools/call", {"name": "sms.send",
                                        "arguments": {"to": "+15555550100", "body": "hi"}}),
                     tools(StubMint(fee_ppk=0)), caller="ab" * 32)
    result = out["result"]
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert body["reason"] == "payment_required"
    assert body["cap"] == f"cap:tool:sms.send:{DEFAULT_PRICE_SATS}:sats"


def test_a_malformed_request_is_invalid_request_not_a_crash():
    for bad in [None, [], "nope", {}, {"jsonrpc": "2.0", "id": 1},
                {"jsonrpc": "1.0", "id": 1, "method": "ping"}]:
        out = handle_rpc(bad, tools())
        assert out is not None and "error" in out, bad


def test_a_notification_is_never_answered():
    """JSON-RPC: a notification (no id) gets no response. This is the one
    intentional silence, and it is not a refusal."""
    assert handle_rpc(req("notifications/initialized", rpc_id=None), tools()) is None
    assert handle_rpc(req("notifications/cancelled", rpc_id=None), tools()) is None


def test_a_request_with_no_id_that_is_not_a_notification_still_gets_an_error():
    """Absence of an id does not licence silence for a real request: an unknown
    method with no id is still answered, with a null id."""
    out = handle_rpc({"jsonrpc": "2.0", "method": "fs/read"}, tools())
    assert out is not None and "error" in out
    assert out["id"] is None


def test_initialize_reports_the_protocol_and_server_info():
    out = handle_rpc(req("initialize"), tools())
    result = out["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "nosms"
    assert "tools" in result["capabilities"]


def test_tools_list_matches_the_contract():
    out = handle_rpc(req("tools/list"), tools())
    names = {t["name"] for t in out["result"]["tools"]}
    # The JMP tools (jmp.status / jmp.funding / jmp.credentials) are part of the
    # advertised surface as of the funding-facts card; the SMS tools are
    # unchanged. Every name here must also be documented in docs/cvm/llms.txt
    # (test_cvm_contract enforces that).
    assert names == {"sms.send", "sms.status", "sms.pricing", "sms.capabilities", "docs",
                     "jmp.status", "jmp.funding", "jmp.credentials"}


def test_resources_read_returns_the_contract_verbatim():
    out = handle_rpc(req("resources/read", {"uri": "nosms://llms.txt"}), tools())
    contents = out["result"]["contents"]
    assert contents[0]["uri"] == "nosms://llms.txt"
    assert contents[0]["text"] == (CONTRACT / "llms.txt").read_text()


def test_unknown_method_is_method_not_found():
    out = handle_rpc(req("fs/read"), tools())
    assert out["error"]["code"] == -32601
    assert out["error"]["data"]["reason"] == "unsupported method"


def test_paid_tool_call_carries_the_caller_for_the_owner_allowlist():
    t = tools(StubMint(fee_ppk=0))
    out = handle_rpc(req("tools/call", {"name": "sms.send",
                                        "arguments": {"to": "+15555550100", "body": "hi"}}),
                     t, caller="AB" * 32)
    assert out["result"]["isError"] is True          # not an owner -> must pay
    t2 = CvmTools(FakeTransport(), owner_pubkeys={"ab" * 32},
                  escrow=EscrowStore(":memory:"), mint=StubMint(fee_ppk=0))
    out2 = handle_rpc(req("tools/call", {"name": "sms.send",
                                         "arguments": {"to": "+15555550100", "body": "hi"}}),
                      t2, caller="ab" * 32)
    assert "isError" not in out2["result"]
    assert json.loads(out2["result"]["content"][0]["text"])["price_sats"] == 0
