"""RED-first tests for the nosms CVM tool surface (ContextVM, CEP-8 postage).

The acceptance targets, in order of how much they hurt when wrong:

1. **A refusal is VISIBLE.** An unpaid `sms.send` must come back as an MCP tool
   error the caller can read. The live bug that motivated this file: the server
   received an unpaid call, published a "reply" the client never captured, and
   the caller saw silence — indistinguishable from a dead server.
2. **Postage is really captured.** A token handed to `sms.send` is swapped at
   the mint (NUT-03), exactly like the HTTP path — holding an un-swapped token
   is not escrow, because the sender still owns the secrets.
3. **The price is flat (ADR-0002).** Domestic and international cost the same.
4. **Capabilities are read from the rail**, so an honest fake rail cannot be
   served the email rail's flags.
5. **The contract is reachable from the server** — `docs` returns llms.txt
   verbatim, and the CEP-6 announcement carries the contract URL.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.cvm import ContractContext
from app.cvm_tools import CvmTools
from app.escrow import EscrowStore
from app.pricing import DEFAULT_PRICE_SATS
from app.transports import EmailGatewayTransport, FakeTransport
from tests.stubs import StubMint, make_token

REPO = pathlib.Path(__file__).resolve().parent.parent
#: the machine contract lives in the repo (docs/cvm) and is loaded by the
#: runner; the server must be able to hand it back verbatim.
CONTRACT = REPO / "docs" / "cvm"
PRICE = DEFAULT_PRICE_SATS


def load_docs(contract_dir: pathlib.Path = CONTRACT) -> dict[str, str]:
    return {
        "llms": (contract_dir / "llms.txt").read_text(),
        "llms-full": (contract_dir / "llms-full.txt").read_text(),
    }


def tools(transport=None, owners=None, mint=None, escrow=None, tmp_path=None,
          btc_usd=None, contract_dir=CONTRACT) -> CvmTools:
    return CvmTools(
        transport or FakeTransport(), docs=load_docs(contract_dir),
        owner_pubkeys=owners, mint=mint,
        escrow=escrow or EscrowStore(":memory:"),
        contract=ContractContext(npub="npub1testserver", relays=("wss://relay.test",)),
        btc_usd=btc_usd)


def payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def assert_visible_error(result: dict, reason: str) -> dict:
    """A refusal must be a readable error — never empty, never silent."""
    assert result, "a refusal returned nothing at all"
    assert result.get("isError") is True, result
    body = payload(result)
    assert body["reason"] == reason, body
    assert body["hint"]
    return body


# --- the refusal must be VISIBLE (the live bug) ----------------------------

def test_unpaid_send_returns_a_visible_error_not_silence():
    t = tools()
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi"})
    body = assert_visible_error(result, "payment_required")
    assert body["cap"] == f"cap:tool:sms.send:{PRICE}:sats"
    assert body["pmi"] == "bitcoin-cashu"
    assert body["gating"] == "explicit_gating"
    assert body["price_sats"] == PRICE
    assert t.transport.sent == []                      # proven: nothing left the process


def test_every_refusal_returns_a_json_rpc_visible_result():
    """No tool, known or unknown, may answer with nothing at all."""
    t = tools()
    for name, args in [("sms.send", {}), ("sms.send", {"to": "+15555550100", "body": ""}),
                       ("sms.status", {"id": "nope"}), ("sms.destroy", {}),
                       ("docs", {"name": "missing"})]:
        result = t.call(name, args)
        assert result and result["content"], (name, args)
        assert result.get("isError") is True, (name, result)


def test_unpaid_send_never_touches_the_mint():
    mint = StubMint(fee_ppk=0)
    t = tools(mint=mint)
    t.call("sms.send", {"to": "+15555550100", "body": "hi"})
    assert mint.swap_calls == []


# --- postage is really captured -------------------------------------------

def test_send_swaps_the_token_and_holds_exactly_the_price(tmp_path):
    mint = StubMint(fee_ppk=0)
    t = tools(mint=mint, escrow=EscrowStore(str(tmp_path / "e.db")))
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": make_token([4096])})
    body = payload(result)
    assert "isError" not in result, body
    assert body["accepted"] is True
    assert body["price_sats"] == PRICE
    assert body["best_effort"] is False
    # one swap, the sender's proofs burned, the service holding the price
    assert len(mint.swap_calls) == 1
    assert sum(mint.swap_calls[0]["outputs"]) == 4096
    assert len(mint.spent_secrets()) == 1
    held = t.escrow.get(body["id"])
    assert held.amount == PRICE and held.change == 4096 - PRICE


def test_underpaid_token_names_the_shortfall_and_sends_nothing():
    mint = StubMint(fee_ppk=0)
    t = tools(mint=mint)
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": make_token([1024])})
    body = assert_visible_error(result, "insufficient_funds")
    assert body["shortfall_sats"] == PRICE - 1024
    assert mint.swap_calls == [] and t.transport.sent == []


def test_mint_input_fee_is_borne_by_the_sender():
    mint = StubMint(fee_ppk=100)                          # testnut's real keyset
    t = tools(mint=mint)
    r1 = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                             "cashu_token": make_token([1024])})
    assert_visible_error(r1, "insufficient_funds")
    assert payload(r1)["mint_fee_sats"] == 1
    r2 = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                             "cashu_token": make_token([2048, 1024])})
    assert "isError" not in r2, payload(r2)
    assert t.escrow.get(payload(r2)["id"]).amount == PRICE


def test_token_from_another_mint_is_refused_before_any_swap():
    mint = StubMint(fee_ppk=0)
    t = tools(mint=mint)
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": make_token([4096],
                                                           mint="https://mint.example.com")})
    assert_visible_error(result, "token_wrong_mint")
    assert mint.swap_calls == []


def test_already_spent_token_is_refused():
    mint = StubMint(fee_ppk=0)
    from tests.stubs import proof
    spent = proof(4096)
    mint.spent.add(spent["secret"])
    from app.cashu import encode_token
    from tests.stubs import MINT_URL
    t = tools(mint=mint)
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": encode_token(MINT_URL, [spent])})
    assert_visible_error(result, "token_already_spent")
    assert t.transport.sent == []


def test_a_settlement_receipt_without_a_token_is_honoured_when_no_mint_is_wired():
    """The shell may have already escrowed; without a mint we cannot re-swap, so
    the receipt is accepted rather than the caller being strung along."""
    t = tools(mint=None)
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "settlement_receipt": "rcpt-1"})
    assert "isError" not in result, payload(result)
    assert payload(result)["price_sats"] == PRICE


def test_one_settlement_receipt_funds_one_send_when_no_mint_is_wired():
    t = tools(mint=None)
    assert "isError" not in t.call("sms.send", {"to": "+15555550100", "body": "a",
                                                "settlement_receipt": "rcpt-1"})
    second = t.call("sms.send", {"to": "+15555550100", "body": "b",
                                 "settlement_receipt": "rcpt-1"})
    # the second send is answered, not attempted twice with nothing to spend
    assert second["content"]


# --- owner allowlist -------------------------------------------------------

def test_owner_sends_free_without_any_payment():
    t = tools(owners={"ab" * 32}, mint=StubMint(fee_ppk=0))
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi"}, caller="AB" * 32)
    assert "isError" not in result, payload(result)
    assert payload(result)["price_sats"] == 0
    assert t.mint.swap_calls == []


# --- the price is flat (ADR-0002) -----------------------------------------

def test_domestic_and_international_cost_the_same():
    t = tools()
    us = payload(t.call("sms.pricing", {"to": "+15555550100"}))
    de = payload(t.call("sms.pricing", {"to": "+4917012345678"}))
    assert us["destination_price"] == de["destination_price"] == PRICE
    assert us["flat"] is True and us["model"] == "flat"


def test_pricing_reports_the_adr_formula_and_the_live_quote():
    p = payload(tools(btc_usd=50_000.0).call("sms.pricing", {}))
    assert p["price"] == PRICE
    assert p["btc_usd"] == 50_000.0
    assert p["mult"] == 0.5 and p["rail_replacement_usd"] == 4.99
    assert "1e8 / btc_usd" in p["formula"]
    assert p["quote_sats"] > PRICE                      # a dearer BTC -> more sats


def test_the_cap_tag_uses_the_live_price_not_a_literal():
    t = tools()
    caps = [tag for tag in t.capability_tags() if tag[0] == "cap"]
    assert [["cap", "tool:sms.send", str(PRICE), "sats"]] == caps
    assert ["pmi", "bitcoin-cashu", "explicit_gating"] in t.capability_tags()


# --- capabilities come from the rail --------------------------------------

def test_capabilities_report_the_best_effort_rail_honestly():
    caps = tools(EmailGatewayTransport()).sms_capabilities()
    assert caps["rail"] == "email_gateway"
    assert caps["best_effort"] is True
    assert caps["delivery_receipts"] is False
    assert caps["countries"] == ["US", "CA"]


def test_capabilities_do_not_hardcode_the_fake_rail_as_best_effort():
    caps = tools(FakeTransport()).sms_capabilities()
    assert caps["best_effort"] is False
    assert caps["delivery_receipts"] is True


# --- refunds: only what the rail can detect --------------------------------

class DetectingFailTransport(FakeTransport):
    """A rail that DETECTS a hard failure."""
    name = "detecting-fail"

    def send(self, dest, body, **kwargs):
        from app.transports.base import SendResult
        return SendResult(accepted=False, rail=self.name, best_effort=False,
                          receipt=None, detail="carrier refused")


class BestEffortMissTransport(FakeTransport):
    """The email rail's shape: accepted the order, can confirm nothing."""
    name = "email_gateway"

    def __init__(self):
        super().__init__()
        self._dest = []

    @property
    def capabilities(self):
        from app.transports.base import Capabilities
        return Capabilities(available=True, best_effort=True,
                            delivery_receipts=False, countries=["US", "CA"])

    def send(self, dest, body, **kwargs):
        from app.transports.base import SendResult
        return SendResult(accepted=False, rail=self.name, best_effort=True,
                          receipt=None, detail="smtp send failed: OSError")


def test_hard_failure_is_refunded_and_says_so(tmp_path):
    """A rail that DETECTS a hard failure is refunded, and the record says so.

    The result is a normal tool result, not an error envelope: the money was
    already settled (captured, then handed back), so hiding it behind an error
    would hide the receipt the payer is entitled to.
    """
    t = tools(DetectingFailTransport(), mint=StubMint(fee_ppk=0),
              escrow=EscrowStore(str(tmp_path / "e.db")))
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": make_token([4096])})
    body = payload(result)
    assert "isError" not in result, body
    assert body["accepted"] is False
    assert body["refunded"] is True
    assert body["refund_token"]
    assert body["best_effort"] is False
    # the ledger row is the refund's single source of truth
    rec = t.escrow.get(body["id"])
    assert rec.refunded is True
    assert rec.refund_amount == 4096


def test_best_effort_miss_is_not_refunded_and_never_claims_delivery():
    t = tools(BestEffortMissTransport(), mint=StubMint(fee_ppk=0))
    body = payload(t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                       "cashu_token": make_token([4096])}))
    assert body["accepted"] is False
    assert body["best_effort"] is True
    assert body["delivery_confirmed"] is False
    assert body["refunded"] is False          # the rail cannot prove non-delivery
    assert body["receipt"] is None


# --- a rail that RAISES (found live against the email rail) -----------------

class RaisingTransport(FakeTransport):
    """The email rail's real shape for a non-+1 destination: it raises."""
    name = "raising"

    def send(self, dest, body, **kwargs):
        from app.transports.email_gateway import UnsupportedDestination
        raise UnsupportedDestination("destination_unsupported", "rail covers US/CA only")


def test_a_rail_that_raises_is_refunded_and_returns_a_visible_error(tmp_path):
    """Regression for a live UnboundLocalError: the failure branch recorded an
    escrow row and hit an unbound `change_sats`, so the whole tools/call died
    and the caller saw NOTHING. Found by scripts/cvm_live_verify.py against the
    email rail; this pins the path so it cannot come back."""
    mint = StubMint(fee_ppk=0)
    t = tools(RaisingTransport(), mint=mint,
              escrow=EscrowStore(str(tmp_path / "e.db")))
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": make_token([4096])})
    body = assert_visible_error(result, "destination_unsupported")
    assert body["refunded"] is True
    assert body["refund_token"]
    # the ledger holds the failure and the refund, and the refund is the postage
    rows = t.escrow.list_all()
    assert len(rows) == 1
    assert rows[0].status == "failed" and rows[0].refunded is True
    assert rows[0].refund_amount == 4096


def test_a_raising_rail_that_is_best_effort_still_answers_the_caller():
    t = tools(RaisingTransport(), mint=StubMint(fee_ppk=0))
    result = t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                 "cashu_token": make_token([4096])})
    # answered, not silence — whatever the refund decision
    assert result and result["content"]


# --- destination gate ------------------------------------------------------

def test_a_destination_outside_the_rails_countries_is_refused_before_payment():
    mint = StubMint(fee_ppk=0)
    sent: list = []
    rail = EmailGatewayTransport(smtp_factory=lambda h, p: sent.append((h, p)))
    t = tools(rail, mint=mint)
    result = t.call("sms.send", {"to": "+4917012345678", "body": "hi",
                                 "cashu_token": make_token([4096])})
    assert_visible_error(result, "destination_unsupported")
    assert mint.swap_calls == [] and sent == []


# --- status ----------------------------------------------------------------

def test_status_returns_the_record_and_is_free():
    t = tools(mint=StubMint(fee_ppk=0))
    sent = payload(t.call("sms.send", {"to": "+15555550100", "body": "hi",
                                       "cashu_token": make_token([4096])}))
    status = payload(t.call("sms.status", {"id": sent["id"]}))
    assert status["id"] == sent["id"]
    assert status["price_sats"] == PRICE


def test_status_unknown_id_is_not_found():
    assert_visible_error(tools().call("sms.status", {"id": "nope"}), "not_found")


# --- the contract is reachable FROM the server -----------------------------

def test_docs_tool_returns_the_contract_verbatim():
    attached = (CONTRACT / "llms.txt").read_text()
    t = tools()
    assert t.docs_tool("llms") == attached
    assert t.call("docs", {})["content"][0]["text"] == attached
    assert t.call("llms", {})["content"][0]["text"] == attached


def test_llms_full_returns_the_manual():
    assert "full manual" in tools().docs_tool("llms-full")


def test_resources_expose_the_contract_without_a_tool_call():
    t = tools()
    uris = {r["uri"] for r in t.resource_list()}
    assert uris == {"nosms://llms.txt", "nosms://llms-full.txt"}
    assert t.read_resource("nosms://llms.txt") == t.docs_tool("llms")


def test_server_announcement_advertises_the_contract_url_and_docs_tool():
    t = tools()
    ann = t.server_announcement()
    assert ann["name"] == "nosms"
    assert ann["docs"]["url"] == t.contract.contract_url
    assert ann["docs"]["tool"] == "docs"
    assert set(ann["tools"]) == {"sms.send", "sms.status", "sms.pricing",
                                 "sms.capabilities", "docs"}


# --- tool list -------------------------------------------------------------

def test_tool_definitions_include_every_tool_the_contract_names():
    names = {t["name"] for t in tools().tool_definitions()}
    assert names == {"sms.send", "sms.status", "sms.pricing", "sms.capabilities", "docs"}


def test_every_free_tool_answers_without_payment():
    t = tools(mint=StubMint(fee_ppk=0))
    for tool, args in [("sms.capabilities", {}), ("sms.pricing", {}),
                       ("docs", {}), ("llms", {}), ("llms-full", {})]:
        assert "isError" not in t.call(tool, args), tool
