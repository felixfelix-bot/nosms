"""The CVM contract must not drift from the code that serves it.

Two contracts exist and they must agree: `llms.txt` (HTTP) and
`docs/cvm/llms.txt` (ContextVM). Both advertise a price, capability flags and an
error vocabulary. If any of those is written down twice and edited once, a
caller branching on the documented set is wrong — so this file derives the
emitted values from the source and fails when the documents disagree.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.cvm import SERVER_NAME
from app.cvm_tools import CvmTools
from app.escrow import EscrowStore
from app.pricing import DEFAULT_PRICE_SATS
from app.transports import EmailGatewayTransport, FakeTransport

REPO = pathlib.Path(__file__).resolve().parent.parent
CVM_LLMS = REPO / "docs" / "cvm" / "llms.txt"
CVM_FULL = REPO / "docs" / "cvm" / "llms-full.txt"
HTTP_LLMS = REPO / "llms.txt"


def tools(transport=None) -> CvmTools:
    return CvmTools(transport or FakeTransport(), escrow=EscrowStore(":memory:"),
                    docs={"llms": CVM_LLMS.read_text(), "llms-full": CVM_FULL.read_text()})


# --- the price is written once, and both documents agree --------------------

def test_cvm_contract_advertises_the_flat_price_from_the_code():
    body = CVM_LLMS.read_text()
    assert str(DEFAULT_PRICE_SATS) in body
    assert "flat" in body.lower()


def test_the_http_contract_advertises_the_same_flat_price():
    """One price table serves both surfaces; the documents must not disagree."""
    assert str(DEFAULT_PRICE_SATS) in HTTP_LLMS.read_text()


def test_the_cep8_cap_tag_in_the_contract_matches_the_served_tag():
    caps = [t for t in tools().capability_tags() if t[0] == "cap"]
    assert caps == [["cap", "tool:sms.send", str(DEFAULT_PRICE_SATS), "sats"]]
    assert f"cap:tool:sms.send:{DEFAULT_PRICE_SATS}:sats" in CVM_LLMS.read_text()


# --- the honesty flags are stated, not implied ------------------------------

def test_cvm_contract_states_the_capability_flags_the_rail_reports():
    body = CVM_LLMS.read_text()
    caps = tools(EmailGatewayTransport()).sms_capabilities()
    assert caps["best_effort"] is True and caps["delivery_receipts"] is False
    assert "best_effort:        true" in body
    assert "delivery_receipts:  false" in body
    assert "[US, CA]" in body
    # the warning must be explicit, not a footnote
    assert "NOT \"delivered\"" in body or 'NOT “delivered”' in body


def test_the_manual_repeats_the_no_delivery_receipt_warning():
    body = CVM_FULL.read_text()
    assert "delivery_receipts` — `false`" in body or "delivery_receipts" in body
    assert "No delivery receipts" in body
    assert "Termination risk" in body          # the ADR-0002 risk, not hidden


# --- every emitted reason token is documented -------------------------------

REASON_LITERAL = re.compile(r'ToolError\(\s*"([a-z_]+)"')
RPC_REASON = re.compile(r'"reason":\s*"([a-z_ ]+)"')


def _emitted_reasons() -> set[str]:
    emitted: set[str] = set()
    for name in ("cvm_tools.py", "cvm_rpc.py"):
        source = (REPO / "app" / name).read_text()
        emitted |= set(REASON_LITERAL.findall(source))
        emitted |= set(RPC_REASON.findall(source))
    # tokens raised by delegated modules and reported through this surface
    emitted |= {"token_invalid", "token_unsupported", "token_empty",
                "mint_unreachable", "destination_unsupported", "owner_only",
                "rate_limited", "unsupported method"}
    return emitted


def test_every_reason_the_cvm_can_emit_is_documented():
    documented = set(re.findall(r"`([a-z_ ]+)`", CVM_LLMS.read_text()))
    emitted = _emitted_reasons()
    assert emitted, "the scanner found no reason tokens — it has drifted from the code"
    missing = emitted - documented
    assert not missing, f"undocumented CVM error tokens: {sorted(missing)}"


def test_the_contract_names_every_tool_the_server_serves():
    body = CVM_LLMS.read_text()
    for tool in tools().tool_definitions():
        assert f"`{tool['name']}`" in body, tool["name"]
    for resource in tools().resource_list():
        assert resource["uri"] in body


def test_the_manual_documents_the_same_tools_as_the_contract():
    full = CVM_FULL.read_text()
    for tool in tools().tool_definitions():
        assert f"`{tool['name']}`" in full, tool["name"]


# --- the contract is what the docs tool serves, byte for byte ---------------

def test_docs_tool_serves_these_exact_files():
    t = tools()
    assert t.docs_tool("llms") == CVM_LLMS.read_text()
    assert t.docs_tool("llms-full") == CVM_FULL.read_text()


def test_the_contract_files_advertise_the_rails_and_their_gates():
    """T4: adding a rail changes what the contract may advertise.

    The WhatsApp rail (ADR-0003) rides a second personal line, so the contract
    has to name the rails, the pacing gate (`rail_paced` + `Retry-After`) and the
    loud stop (`rail_unavailable`, never retried). Both documents are checked:
    `llms.txt` is the contract and `llms-full.txt` is the manual served by
    `docs` / `llms-full`.
    """
    contract = CVM_LLMS.read_text()
    manual = CVM_FULL.read_text()
    for body in (contract, manual):
        assert "rail_paced" in body
        assert "rail_unavailable" in body
        assert "whatsapp" in body.lower()
    assert "Retry-After" in contract
    assert "never retried" in contract
    # the honesty rule that survives the new rail: no delivery claims, ever
    assert "delivery_receipts" in contract and "delivery_receipts" in manual


def test_the_manual_documents_the_ban_stop_rule():
    """A halting rail must be documented, not discovered (ADR-0003)."""
    manual = CVM_FULL.read_text()
    assert "stops the rail" in manual          # the loud stop
    assert "persists the halt" in manual       # survives a restart
    assert "retry loop" in manual              # and why it is never retried


def test_the_announced_server_name_is_the_one_in_the_contract():
    assert SERVER_NAME in CVM_LLMS.read_text()
    assert tools().server_announcement()["name"] == SERVER_NAME


@pytest.mark.parametrize("path", [CVM_LLMS, CVM_FULL, HTTP_LLMS])
def test_contract_files_are_substantial_and_plain_text(path):
    text = path.read_text()
    assert len(text) > 1500
    assert "\x00" not in text
