"""Tests for the CEP-6 announcement layer and the relay set.

Discovery is not decoration: ``cvmi discover`` only finds the server if the
catalog events carry the right kinds and tags, and a caller only learns the
contract if the announcement points at it. Both are asserted here, offline.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.cvm import (                                              # noqa: E402
    ANNOUNCE_RESOURCES,
    ANNOUNCE_SERVER,
    ANNOUNCE_TOOLS,
    CVM_MESSAGE_KIND,
    GIFT_WRAP_KINDS,
    WORKING_RELAYS,
    ContractContext,
    contract_tags,
    jsonrpc_error,
    jsonrpc_result,
    mcp_tool_error,
    mcp_tool_result,
)
from app.cvm_tools import CvmTools                                  # noqa: E402
from app.escrow import EscrowStore                                  # noqa: E402
from app.pricing import DEFAULT_PRICE_SATS                          # noqa: E402
from app.transports import FakeTransport                            # noqa: E402
from app.transports.email_gateway import load_carrier_map           # noqa: E402
import scripts.run_cvm_server as runner                             # noqa: E402


def tools() -> CvmTools:
    return CvmTools(FakeTransport(), escrow=EscrowStore(":memory:"),
                    docs={"llms": "contract", "llms-full": "manual"})


# --- protocol constants -----------------------------------------------------

def test_message_traffic_kind_is_the_ephemeral_cvm_kind():
    assert CVM_MESSAGE_KIND == 25910
    assert 20000 <= CVM_MESSAGE_KIND < 30000            # ephemeral by range
    assert GIFT_WRAP_KINDS == (1059, 21059)


def test_catalog_kinds_are_the_cep6_replaceable_set():
    assert ANNOUNCE_SERVER == 11316
    assert ANNOUNCE_TOOLS == 11317
    assert ANNOUNCE_RESOURCES == 11318


def test_relay_set_matches_the_live_probe():
    """relay.contextvm.org is NOT unreachable (the card body was stale), and
    relay.damus.io is excluded because it banned this host's IP."""
    assert "wss://relay.contextvm.org" in WORKING_RELAYS
    assert "wss://relay2.contextvm.org" in WORKING_RELAYS
    assert "wss://relay.primal.net" in WORKING_RELAYS
    assert not any("damus" in r for r in WORKING_RELAYS)


# --- announcement tags ------------------------------------------------------

def test_announcement_tags_carry_the_contract_url_and_docs_tool():
    ctx = ContractContext(npub="npub1xyz", contract_url="https://nostr.example/cvm/llms.txt",
                          relays=("wss://relay.test",), naddr="naddr1qqq")
    tags = contract_tags(ctx, tools().capability_tags())
    flat = [tuple(t) for t in tags]
    assert ("contract", "https://nostr.example/cvm/llms.txt") in flat
    assert ("docs", "docs") in flat
    assert ("docs", "llms") in flat
    assert ("d", "nosms") in flat
    assert ("naddr", "naddr1qqq") in flat
    assert ("t", "contextvm") in flat


def test_announcement_tags_carry_the_cep8_cap_and_pmi():
    tags = contract_tags(ContractContext(), tools().capability_tags())
    caps = [t for t in tags if t[0] == "cap"]
    assert ["cap", "tool:sms.send", str(DEFAULT_PRICE_SATS), "sats"] in caps
    assert ["pmi", "bitcoin-cashu", "explicit_gating"] in tags


def test_announcement_tags_use_relative_urls_only_for_the_contract():
    """Every relay advertised must be a relay URL, never a bare host."""
    tags = contract_tags(ContractContext(relays=WORKING_RELAYS), [])
    assert all(t[1].startswith("wss://") for t in tags if t[0] == "relay")


# --- envelopes --------------------------------------------------------------

def test_mcp_tool_result_is_text_content_without_an_error_flag():
    out = mcp_tool_result({"a": 1})
    assert out == {"content": [{"type": "text", "text": json.dumps({"a": 1})}]}
    assert "isError" not in out


def test_mcp_tool_error_is_always_an_error_with_a_reason_and_hint():
    out = mcp_tool_error("payment_required", "pay first", cap="cap:x")
    assert out["isError"] is True
    body = json.loads(out["content"][0]["text"])
    assert body["reason"] == "payment_required"
    assert body["hint"] == "pay first"
    assert body["cap"] == "cap:x"


def test_mcp_tool_error_drops_empty_extras_but_keeps_falsey_ones():
    body = json.loads(mcp_tool_error("x", "y", a=None, b=False, c=0)["content"][0]["text"])
    assert "a" not in body
    assert body["b"] is False and body["c"] == 0


def test_jsonrpc_result_and_error_shapes():
    assert jsonrpc_result("9", {"ok": True}) == {"jsonrpc": "2.0", "id": "9",
                                                 "result": {"ok": True}}
    err = jsonrpc_error("9", -32601, "nope", {"reason": "unsupported method"})
    assert err["error"]["code"] == -32601
    assert err["error"]["data"]["reason"] == "unsupported method"
    assert "data" not in jsonrpc_error("9", -32603, "boom")["error"]


# --- the runner's wiring ----------------------------------------------------

def test_runner_loads_the_committed_contract():
    docs = runner.load_docs()
    assert "ContextVM" in docs["llms"]
    assert len(docs["llms-full"]) > len(docs["llms"])


def test_runner_builds_the_requested_rail_and_names_it():
    assert runner.build_transport("fake").name == "fake"
    assert runner.build_transport("email_gateway").name == "email_gateway"


def test_runner_refuses_a_rail_it_cannot_load_rather_than_faking_it():
    """A rail we cannot import must stop the server: describing a capability we
    do not have is the one thing this service must never do."""
    with pytest.raises(SystemExit) as exc:
        runner.build_transport("jmp")
    assert "not available" in str(exc.value)


def test_runner_mint_is_absent_unless_configured(monkeypatch):
    monkeypatch.delenv("NOSMS_MINT_URL", raising=False)
    assert runner.build_mint() is None
    monkeypatch.setenv("NOSMS_MINT_URL", "https://testnut.cashu.space")
    assert runner.build_mint().mint_url == "https://testnut.cashu.space"


def test_default_contract_url_is_absolute_and_ends_in_llms_txt():
    ctx = ContractContext()
    assert ctx.contract_url.startswith("https://")
    assert ctx.contract_url.endswith("llms.txt")


# --- the email rail's carrier config is explicit, never guessed -------------

def test_carrier_map_parses_config_and_normalises_the_number():
    m = load_carrier_map('{"+1 415 555 0100": "T-Mobile"}')
    assert m == {"+14155550100": "t-mobile"}


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '"str"', "{bad"])
def test_a_malformed_carrier_map_is_empty_not_a_crash(raw):
    """An unknown carrier must stay `carrier_unknown` — a truthful refusal —
    rather than taking down a service that moves money."""
    assert load_carrier_map(raw) == {}


def test_email_rail_uses_the_configured_carrier_for_a_destination(monkeypatch):
    from app.transports.email_gateway import EmailGatewayTransport
    sent = []
    rail = EmailGatewayTransport(
        carrier_map={"+14155550100": "tmobile"},
        smtp_factory=lambda h, p: sent.append((h, p)) or _NullSMTP())
    result = rail.send("+14155550100", "hi")
    assert result.accepted is True
    assert sent, "the rail must use the configured carrier gateway"


def test_email_rail_still_refuses_an_unknown_carrier_rather_than_guessing():
    from app.transports.email_gateway import EmailGatewayTransport, UnsupportedDestination
    rail = EmailGatewayTransport(carrier_map={"+14155550100": "tmobile"})
    with pytest.raises(UnsupportedDestination) as exc:
        rail.send("+14085550123", "hi")
    assert exc.value.reason == "carrier_unknown"


class _NullSMTP:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendmail(self, *args, **kwargs):
        return {}
