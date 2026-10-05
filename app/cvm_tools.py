"""nosms ContextVM (CVM) tool surface — MCP tools over Nostr with CEP-8 postage.

This is the machine-facing twin of the HTTP API (`/llms.txt`): the same rail,
the same price table, the same escrow, exposed as MCP tools transported over
Nostr kind 25910 and addressed by the server's npub.

Design rules baked in, each one a test:

* **Tools read the rail, they do not describe it.** ``sms.capabilities`` and
  ``sms.pricing`` answer from the injected :class:`Transport` and the shared
  price table, so the advertised contract cannot drift from the behaviour.
* **Nothing is sent before payment lands** (CEP-8 ``explicit_gating``), and a
  refusal is a *visible* MCP error — never silence. An unreachable server and a
  declined payment must not look the same to a caller.
* **A token is not escrow until it is swapped.** ``sms.send`` is handed a Cashu
  token; the service captures it with a NUT-03 swap at the mint exactly like the
  HTTP path, so the sender cannot spend it twice.
* **Never claim delivery.** A rail reporting ``delivery_receipts=False`` cannot
  produce one, and no code path here invents one.
* **Owner sends free.** Paying yourself is pointless.

The wire is injected: this module is pure and unit-testable with zero network.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .cashu import (
    CashuError,
    decode_token,
    encode_token,
    fee_for,
    split_amounts,
)
from .cvm import (
    ContractContext,
    SERVER_ABOUT,
    SERVER_NAME,
    SERVER_VERSION,
    mcp_tool_error,
    mcp_tool_result,
)
from .escrow import EscrowStore
from .pricing import (
    COUNTRIES,
    DEFAULT_PRICE_SATS,
    FLAT_PRICE_SATS,
    MULT,
    PRICE_FLOOR_SATS,
    RAIL_REPLACEMENT_USD,
    InvalidDestination,
    normalize_e164,
    price_for,
    quote_sats,
)
from .refunds import refund_all
from .transports.base import Transport

class ToolError(Exception):
    """A refusal that must reach the caller as a visible MCP tool error."""

    def __init__(self, reason: str, hint: str = "", **extra: Any):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.extra = extra


#: tool names, in contract order.
TOOL_SEND = "sms.send"
TOOL_STATUS = "sms.status"
TOOL_PRICING = "sms.pricing"
TOOL_CAPABILITIES = "sms.capabilities"
TOOL_DOCS = "docs"
#: `llms` is a documented alias for `docs`; `llms-full` returns the manual.
DOCS_ALIASES = (TOOL_DOCS, "llms")
DOCS_FULL_ALIASES = ("llms-full", "llms_full")

FREE_TOOLS = (TOOL_STATUS, TOOL_PRICING, TOOL_CAPABILITIES, TOOL_DOCS, "llms")


@dataclass
class SendRecord:
    """One CVM send attempt, in the shape the contract promises."""

    id: str
    to: str
    price_sats: int
    rail: str
    accepted: bool
    best_effort: bool
    receipt: str | None
    detail: str
    refunded: bool = False
    refund_token: str | None = None
    status: str = "unknown"
    owner_free: bool = False
    created_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "to": self.to,
            "price_sats": self.price_sats,
            "rail": self.rail,
            "accepted": self.accepted,
            "best_effort": self.best_effort,
            # honest labelling: the rail's own flag, never a claim
            "delivery_confirmed": False if self.best_effort else bool(self.receipt),
            "receipt": self.receipt,
            "detail": self.detail,
            "status": self.status,
            "refunded": self.refunded,
            "refund_token": self.refund_token,
            "created_at": self.created_at,
        }


class CvmTools:
    """The MCP tool surface. Pure: rail, mint, escrow and docs are injected."""

    def __init__(self, transport: Transport, docs: dict[str, str] | None = None,
                 owner_pubkeys: set[str] | None = None, *,
                 mint=None, escrow: EscrowStore | None = None,
                 contract: ContractContext | None = None,
                 btc_usd: float | None = None):
        self.transport = transport
        self.docs = docs or {}
        self.owner_pubkeys = {p.lower() for p in (owner_pubkeys or set())}
        self.mint = mint
        self.escrow = escrow
        self.contract = contract or ContractContext()
        #: live BTC/USD for the quote. None = the documented default rate.
        self.btc_usd = btc_usd
        #: quotes handed out and not yet consumed: quote_id -> {price, dest, at}
        self.quotes: dict[str, dict] = {}
        self.records: dict[str, SendRecord] = {}
        self.payments: list[dict] = []

    # --- free tools -------------------------------------------------------

    def sms_capabilities(self) -> dict:
        """Rail name + capability flags, read FROM THE RAIL."""
        caps = self.transport.capabilities
        return {
            "rail": self.transport.name,
            "available": bool(caps.available),
            "best_effort": bool(caps.best_effort),
            "delivery_receipts": bool(caps.delivery_receipts),
            "countries": list(caps.countries),
        }

    def sms_pricing(self, to: str | None = None) -> dict:
        """Live price in sats. Flat per ADR-0002; never 0, never below the floor."""
        out: dict[str, Any] = {
            "unit": "sats",
            "model": "flat",
            "price": DEFAULT_PRICE_SATS,
            "flat": True,
            "quote_sats": quote_sats(self.btc_usd),
            "btc_usd": self.btc_usd,
            "formula": "ceil(MULT * rail_replacement_usd * (1e8 / btc_usd))",
            "mult": MULT,
            "rail_replacement_usd": RAIL_REPLACEMENT_USD,
            "floor_sats": PRICE_FLOOR_SATS,
            "countries": list(COUNTRIES),
            "note": ("JMP's plan is unlimited including international, so the "
                     "destination does not change the price."),
        }
        if to:
            out["destination"] = normalize_e164(to)
            out["destination_price"] = price_for(to)
        return out

    def sms_status(self, id: str) -> dict:
        record = self.records.get(id or "")
        if record is None:
            raise ToolError("not_found", f"No send record {id!r}.")
        return record.as_dict()

    def docs_tool(self, name: str = TOOL_DOCS) -> str:
        """Return the contract (or the manual) verbatim. Primary deliverable."""
        key = "llms-full" if name in DOCS_FULL_ALIASES else "llms"
        if key not in self.docs:
            raise ToolError("not_found", f"Document {key!r} is not bundled.")
        return self.docs[key]

    def quote(self, to: str) -> dict:
        """Issue a CEP-8 quote for one destination, with the cap + PMI tags."""
        number = normalize_e164(to)
        price = price_for(number)
        quote_id = "q_" + secrets.token_hex(8)
        self.quotes[quote_id] = {"price": price, "dest": number, "at": time.time()}
        self.quotes = {q: v for q, v in self.quotes.items()
                       if time.time() - v["at"] <= 900}          # 15 min TTL
        return {
            "quote_id": quote_id,
            "destination": number,
            "price_sats": price,
            "gating": "explicit_gating",
            "pmi": "bitcoin-cashu",
            "cap": f"cap:tool:sms.send:{price}:sats",
            "floor_sats": PRICE_FLOOR_SATS,
            "quote_ttl_seconds": 900,
        }

    def _pay_with_token(self, token: str, price: int, caller: str) -> dict:
        """Capture a Cashu token with a NUT-03 swap. Returns the held proofs."""
        if self.mint is None:
            raise ToolError("rail_not_configured",
                            "No mint is configured; postage cannot be escrowed.")
        try:
            token_obj = decode_token(token)
        except CashuError as exc:
            raise ToolError(exc.reason, exc.hint) from exc

        mint_url = getattr(self.mint, "mint_url", None)
        if mint_url and token_obj.mint.rstrip("/") != str(mint_url).rstrip("/"):
            raise ToolError(
                "token_wrong_mint",
                f"This service escrows against {mint_url}; that token is from "
                f"{token_obj.mint}.")

        states = list(self.mint.check_states(token_obj.proofs))
        if any(str(s) == "SPENT" for s in states):
            raise ToolError("token_already_spent", "At least one proof was already redeemed.")
        if any(str(s) != "UNSPENT" for s in states):
            raise ToolError("token_state_unknown", "The mint reports a non-final proof state.")

        try:
            ppk = int(self.mint.fee_ppk_for(token_obj.proofs[0]["id"]))
        except Exception:                                        # noqa: BLE001
            ppk = 0
        mint_fee = fee_for(ppk, len(token_obj.proofs))
        net = token_obj.amount - mint_fee
        if net < price:
            raise ToolError(
                "insufficient_funds",
                f"token is {token_obj.amount} sats; the price is {price} sats "
                f"(short {price - net}; the mint takes a {mint_fee} sat input fee).",
                shortfall_sats=price - net, price_sats=price,
                token_sats=token_obj.amount, mint_fee_sats=mint_fee, net_sats=net)

        change = net - price
        keep_amounts = split_amounts(price)
        change_amounts = split_amounts(change)
        try:
            captured = list(self.mint.swap(token_obj.proofs, keep_amounts + change_amounts))
        except CashuError as exc:
            raise ToolError("mint_error", exc.hint) from exc
        except Exception as exc:                                 # noqa: BLE001
            raise ToolError("mint_error",
                            f"Escrow swap failed ({type(exc).__name__}); no sats taken.") from exc
        escrow_proofs = captured[:len(keep_amounts)]
        change_proofs = captured[len(keep_amounts):]
        self.payments.append({"caller": caller, "sats": price, "at": time.time()})
        return {"escrow_proofs": escrow_proofs, "change_proofs": change_proofs,
                "change_sats": change, "mint_fee": mint_fee, "token_sats": token_obj.amount}

    # --- paid tool --------------------------------------------------------

    def sms_send(self, to: str, body: str, caller: str = "",
                 cashu_token: str | None = None,
                 settlement_receipt: str | None = None) -> dict:
        """Send one SMS. Postage must be captured first unless the caller owns it.

        ``settlement_receipt`` names a CEP-8 settlement (the shell's receipt for
        the payment it made); ``cashu_token`` is the ecash itself. Either is
        accepted; when a token is present the service swaps it, so value is
        actually held rather than assumed.
        """
        try:
            number = normalize_e164(to)
        except InvalidDestination as exc:
            raise ToolError("bad_destination", exc.hint) from exc
        if not body or not body.strip():
            raise ToolError("bad_destination", "Message body is empty.")

        caps = self.transport.capabilities
        if not caps.available:
            raise ToolError("rail_not_configured", "The rail is not configured.")

        # A destination the rail cannot serve is refused BEFORE postage is
        # captured (the rail itself is the authority and raises with a reason
        # token; the country list is checked first so no money moves).
        if caps.countries and not number.startswith("+1") and \
                sorted(caps.countries) == sorted(COUNTRIES):
            raise ToolError("destination_unsupported", "The rail covers US/CA (+1) only.")

        price = price_for(number)
        owner = caller.lower() in self.owner_pubkeys
        held: dict | None = None
        if not owner:
            if cashu_token:
                # The real thing: swap the ecash so the value is genuinely held.
                held = self._pay_with_token(cashu_token, price, caller)
            elif not settlement_receipt:
                # No token and no receipt: refuse, loudly and visibly.
                raise ToolError(
                    "payment_required",
                    "CEP-8 explicit_gating: attach a Cashu token (or a settlement "
                    "receipt) — nothing is sent before payment lands.",
                    cap=f"cap:tool:sms.send:{price}:sats", price_sats=price,
                    pmi="bitcoin-cashu", gating="explicit_gating")

        record_id = "snd_" + secrets.token_hex(8)

        def _escrow_held() -> tuple[str | None, str | None]:
            if held and self.escrow is not None:
                return (encode_token(getattr(self.mint, "mint_url", ""),
                                     held["escrow_proofs"]),
                        encode_token(getattr(self.mint, "mint_url", ""),
                                     held["change_proofs"]) if held["change_proofs"] else None)
            return None, None

        escrow_token, change_token = _escrow_held()

        # --- hand it to the rail -----------------------------------------
        try:
            result = self.transport.send(number, body)
        except Exception as exc:                                 # noqa: BLE001
            # A rail that RAISES refused the message and the refusal carries a
            # countable reason; postage that was captured goes straight back.
            reason = getattr(exc, "reason", "transport_error")
            detail = getattr(exc, "detail", "") or str(exc)
            if self.escrow is not None:
                self.escrow.create(
                    message_id=record_id, pubkey=caller or "anonymous", dest=number,
                    rail=getattr(self.transport, "name", "unknown"),
                    price=0 if owner else price, change=0 if owner else change_sats,
                    escrow_token=escrow_token, change_token=change_token,
                    status="failed", provider_status=f"{reason}: {detail}")
            refund_token = None if owner else self._refund(reason, held=held,
                                                           message_id=record_id)
            raise ToolError(reason, detail, refunded=refund_token is not None,
                            refund_token=refund_token) from exc

        change_sats = (held or {}).get("change_sats", 0)
        if self.escrow is not None:
            self.escrow.create(
                message_id=record_id, pubkey=caller or "anonymous", dest=number,
                rail=result.rail, price=0 if owner else price,
                change=0 if owner else change_sats,
                escrow_token=escrow_token, change_token=change_token,
                status=result.status or ("queued" if result.accepted else "failed"),
                provider_status=result.detail or result.status,
                provider_message_id=result.receipt)

        record = SendRecord(
            id=record_id, to=number, price_sats=0 if owner else price, rail=result.rail,
            accepted=result.accepted, best_effort=result.best_effort,
            receipt=result.receipt, detail=result.detail,
            status=result.status or ("queued" if result.accepted else "failed"),
            owner_free=owner, created_at=time.time())

        # explicit_gating + refund: a failure the rail DETECTED is refundable.
        # A best_effort miss is NOT — the rail cannot observe it, so silence is
        # indistinguishable from delivery (see the email-gateway docstring).
        # Either way the caller gets a *result*: the money is already settled
        # (captured and, where refundable, handed back), so an error envelope
        # would hide a receipt they are entitled to.
        if not result.accepted and not result.best_effort and not owner:
            record.refunded = True
            record.refund_token = self._refund("transport_error", held=held,
                                              message_id=record_id)

        self.records[record.id] = record
        return record.as_dict()

    def _refund(self, reason: str, *, held: dict | None = None,
                message_id: str | None = None) -> str | None:
        """Return postage the rail did not spend. Money in, money out.

        The escrow ledger is the single source of truth: the refund is claimed
        atomically on the row (``EscrowStore.claim_refund``), so a re-run cannot
        hand the same sats back twice, and the bearer token comes from the row.
        Only when no ledger is wired do we encode the held proofs directly.
        """
        if self.escrow is not None and message_id:
            refund_all(self.escrow, self.transport, message_id, reason)
            record = self.escrow.get(message_id)
            if record is not None and record.refund_token:
                return record.refund_token
        if held and held.get("escrow_proofs"):
            try:
                mint_url = getattr(self.mint, "mint_url", "")
                return encode_token(mint_url,
                                    list(held["escrow_proofs"]) + list(held["change_proofs"]))
            except Exception:                                    # noqa: BLE001
                return None
        return None

    # --- MCP dispatch -----------------------------------------------------

    def tool_definitions(self) -> list[dict]:
        """The MCP tools/list payload. Names match the published contract."""
        return [
            {"name": TOOL_SEND,
             "description": ("Send one SMS (PAID). `to` is E.164; postage is "
                             "100 sats flat per ADR-0002 - read sms.pricing."),
             "inputSchema": {"type": "object",
                             "properties": {"to": {"type": "string"},
                                            "body": {"type": "string"},
                                            "cashu_token": {"type": "string"},
                                            "settlement_receipt": {"type": "string"}},
                             "required": ["to", "body"]}},
            {"name": TOOL_STATUS, "description": "Look up a send record (free).",
             "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                             "required": ["id"]}},
            {"name": TOOL_PRICING,
             "description": "Live price in sats (free). Flat per ADR-0002.",
             "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}},
            {"name": TOOL_CAPABILITIES,
             "description": "Rail name + capability flags (free).",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": TOOL_DOCS,
             "description": "The llms.txt contract, verbatim (free). Alias: llms.",
             "inputSchema": {"type": "object",
                             "properties": {"name": {"type": "string"}}}},
        ]

    def resource_list(self) -> list[dict]:
        """CEP-6/MCP resources: the contract is readable without a tool call."""
        return [
            {"uri": "nosms://llms.txt", "name": "llms.txt",
             "description": "The nosms contract, verbatim.", "mimeType": "text/plain"},
            {"uri": "nosms://llms-full.txt", "name": "llms-full.txt",
             "description": "The nosms manual.", "mimeType": "text/plain"},
        ]

    def read_resource(self, uri: str) -> str:
        key = "llms-full" if str(uri).endswith("llms-full.txt") else "llms"
        if key not in self.docs:
            raise ToolError("not_found", f"Document {key!r} is not bundled.")
        return self.docs[key]

    def capability_tags(self) -> list[list[str]]:
        """CEP-8 tags: the `cap` price and the PMI, from live values."""
        price = price_for("+10000000000")
        return [
            ["cap", "tool:sms.send", str(price), "sats"],
            ["pmi", "bitcoin-cashu", "explicit_gating"],
        ]

    def server_announcement(self) -> dict:
        return {"name": SERVER_NAME, "about": SERVER_ABOUT,
                "version": SERVER_VERSION, "tools": [t["name"] for t in self.tool_definitions()],
                "docs": {"url": self.contract.contract_url, "tool": TOOL_DOCS,
                         "alias": "llms"}}

    def call(self, tool: str, args: dict | None = None, caller: str = "") -> dict:
        """Dispatch one MCP tools/call and ALWAYS return a reply.

        The returned object is either an MCP tool result or an MCP tool error;
        there is no path that returns nothing, because a caller that cannot see
        the refusal cannot tell it from an unreachable server.
        """
        args = args or {}
        try:
            if tool == TOOL_SEND:
                return mcp_tool_result(
                    self.sms_send(args.get("to", ""), args.get("body", ""), caller=caller,
                                  cashu_token=args.get("cashu_token"),
                                  settlement_receipt=args.get("settlement_receipt")))
            if tool == TOOL_STATUS:
                return mcp_tool_result(self.sms_status(args.get("id", "")))
            if tool == TOOL_PRICING:
                return mcp_tool_result(self.sms_pricing(args.get("to")))
            if tool == TOOL_CAPABILITIES:
                return mcp_tool_result(self.sms_capabilities())
            if tool in DOCS_ALIASES or tool in DOCS_FULL_ALIASES:
                # `docs(name=...)`: an explicitly named document that does not
                # exist is not_found — never silently swapped for another one.
                default = "llms-full" if tool in DOCS_FULL_ALIASES else "llms"
                name = args.get("name") or default
                if "name" in args and name not in DOCS_ALIASES + DOCS_FULL_ALIASES:
                    raise ToolError("not_found", f"No contract document named {name!r}.")
                return {"content": [{"type": "text", "text": self.docs_tool(name)}]}
            if tool == "sms.quote":
                return mcp_tool_result(self.quote(args.get("to", "")))
            raise ToolError("unsupported method", f"Unknown tool {tool!r}.")
        except ToolError as exc:
            return mcp_tool_error(exc.reason, exc.hint, **exc.extra)
