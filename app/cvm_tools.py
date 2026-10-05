"""nosms ContextVM (CVM) server — MCP tools over Nostr, CEP-8 payment gating.

This is the machine-facing surface the napplet wraps. It exposes the same
contract as the HTTP API (`/llms.txt`) but over MCP JSON-RPC transported as
Nostr kind `25910`, addressed by the server's npub.

Design rules baked in:

* **Tools read the rail, they do not describe it.** ``sms.capabilities`` and
  ``sms.pricing`` answer from the injected :class:`Transport` and the shared
  price table, so the advertised contract cannot drift from the behaviour.
* **Nothing is sent before payment lands** (CEP-8 ``explicit_gating``). A hard
  failure the rail can detect is refunded automatically; a ``best_effort`` miss
  is not, because the rail cannot observe it.
* **Never claim delivery.** A rail that reports ``delivery_receipts=False``
  cannot produce one, and no code path here invents one.
* **Owner sends free.** Paying yourself is pointless.

The wire transport is injected: :class:`CvmTools` is pure and unit-testable with
zero network, and ``scripts/run_cvm_server.py`` wires the real relay client.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .pricing import InvalidDestination, normalize_e164, price_for
from .transports.base import Transport

#: Contract documents, bundled next to the napplet at build time.
LLMS_TXT_PATH = "llms.txt"          # resolved by the runner; see docs_loader
DOCS_ALIASES = ("docs", "llms")

#: how long a settlement receipt stays valid for one send.
RECEIPT_TTL_SECONDS = 15 * 60


class PaymentError(Exception):
    """Raised when the paid path cannot proceed. Carries a machine reason."""

    def __init__(self, reason: str, hint: str = "", **extra: Any):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint
        self.extra = extra

    def as_error(self) -> dict:
        return {"reason": self.reason, "hint": self.hint, **self.extra}


@dataclass
class SendRecord:
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
    created_at: float = field(default_factory=time.time)

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
            "refunded": self.refunded,
            "refund_token": self.refund_token,
            "created_at": self.created_at,
        }


class SettlementLedger:
    """Tracks CEP-8 settled payments and refunds.

    A "settlement" here is a receipt string the shell produced after paying a
    Cashu token. The ledger's job is to make *ordering* enforceable server-side:
    a send may not proceed without a receipt, and one receipt funds one send.
    """

    def __init__(self) -> None:
        self._spent: dict[str, float] = {}

    def consume(self, receipt: str) -> None:
        if not receipt:
            raise PaymentError(
                "payment required",
                "CEP-8 explicit_gating: nothing is sent before payment lands.",
                cap="tool:sms.send",
            )
        if receipt in self._spent:
            raise PaymentError("payment denied", "That settlement receipt was already used.")
        self._spent[receipt] = time.time()

    def prune(self, ttl: int = RECEIPT_TTL_SECONDS) -> None:
        now = time.time()
        for receipt, at in list(self._spent.items()):
            if now - at > ttl:
                del self._spent[receipt]


class CvmTools:
    """The MCP tool surface. Pure: the rail and the docs loader are injected."""

    def __init__(self, transport: Transport, docs: dict[str, str] | None = None,
                 owner_pubkeys: set[str] | None = None):
        self.transport = transport
        self.docs = docs or {}
        self.owner_pubkeys = {p.lower() for p in (owner_pubkeys or set())}
        self.ledger = SettlementLedger()
        self.records: dict[str, SendRecord] = {}
        self.payments: list[dict] = []

    # --- free tools -------------------------------------------------------

    def sms_capabilities(self) -> dict:
        """Rail name + capability flags, read FROM THE RAIL."""
        caps = self.transport.capabilities
        return {
            "rail": self.transport.name,
            "available": caps.available,
            "best_effort": caps.best_effort,
            "delivery_receipts": caps.delivery_receipts,
            "countries": list(caps.countries),
        }

    def sms_pricing(self, to: str | None = None, sats_per_usd: float | None = None) -> dict:
        """Live price in sats. ADR-0002: flat, risk-premium, never free.

        The destination is validated but no longer changes the price; a caller
        may pass a fresher ``sats_per_usd`` and the ADR-0002 formula is applied.
        """
        from .pricing import (
            DEFAULT_PRICE_SATS,
            RAIL_REPLACEMENT_USD,
            RISK_MULTIPLIER,
            quote_sats,
        )

        out: dict[str, Any] = {
            "unit": "sats",
            "model": "flat",
            "price": quote_sats(sats_per_usd) if sats_per_usd is not None else DEFAULT_PRICE_SATS,
            "note": (
                "flat domestic+international (ADR-0002): the v1 rail's plan is "
                "unlimited including international, so destination does not map to cost"
            ),
            "formula": "ceil(MULT * rail_replacement_usd * sats_per_usd), rounded up to 100",
            "risk_multiplier": RISK_MULTIPLIER,
            "rail_replacement_usd": RAIL_REPLACEMENT_USD,
        }
        if to:
            out["destination"] = normalize_e164(to)
        return out

    def sms_status(self, id: str) -> dict:
        record = self.records.get(id or "")
        if record is None:
            raise PaymentError("not_found", f"No send record {id!r}.")
        return record.as_dict()

    def docs_tool(self, name: str = "llms") -> str:
        """Return the contract verbatim. This is the primary deliverable."""
        key = "llms-full" if name in ("llms-full", "llms_full", "full") else "llms"
        if key not in self.docs:
            raise PaymentError("not_found", f"Document {key!r} is not bundled.")
        return self.docs[key]

    # --- paid tool --------------------------------------------------------

    def sms_send(self, to: str, body: str, caller: str = "",
                 settlement_receipt: str | None = None) -> dict:
        """Send one SMS. Requires settlement unless the caller is the owner."""
        try:
            number = normalize_e164(to)
        except InvalidDestination as exc:
            raise PaymentError("bad_destination", exc.hint) from exc
        if not body or not body.strip():
            raise PaymentError("bad_destination", "Message body is empty.")

        caps = self.transport.capabilities
        if not caps.available:
            raise PaymentError("rail_not_configured", "The rail is not configured.")

        price = price_for(number)
        owner = caller.lower() in self.owner_pubkeys
        if not owner:
            self.ledger.consume(settlement_receipt or "")
            self.payments.append({"receipt": settlement_receipt, "sats": price,
                                  "caller": caller, "at": time.time()})

        # A rail that refuses the DESTINATION is a countable error. If the
        # payment already landed, the refusal is refunded — the message was
        # never dispatched, so keeping the money would be theft.
        try:
            result = self.transport.send(number, body)
        except Exception as exc:                              # noqa: BLE001
            reason = getattr(exc, "reason", "transport_error")
            detail = getattr(exc, "detail", "") or str(exc)
            refund = None
            if not owner:
                refund = f"refund_{uuid.uuid4().hex[:16]}"
            raise PaymentError(reason, detail, refunded=refund is not None,
                               refund_token=refund) from exc

        record = SendRecord(
            id=f"snd_{uuid.uuid4().hex[:16]}",
            to=number,
            price_sats=0 if owner else price,
            rail=result.rail,
            accepted=result.accepted,
            best_effort=result.best_effort,
            receipt=result.receipt,
            detail=result.detail,
        )

        # explicit_gating + refund: a failure the rail DETECTED is refundable.
        # A best_effort miss is not, because the rail cannot observe it
        # (see the email-gateway docstring: it reports best_effort=True and
        # never produces a receipt, so silence is indistinguishable from loss).
        if not result.accepted and not result.best_effort:
            record.refunded = True
            record.refund_token = f"refund_{uuid.uuid4().hex[:16]}"

        self.records[record.id] = record
        self.ledger.prune()
        return record.as_dict()

    # --- MCP dispatch -----------------------------------------------------

    def tool_definitions(self) -> list[dict]:
        return [
            {"name": "sms.send", "description": "Send one SMS (paid, CEP-8).",
             "inputSchema": {"type": "object",
                             "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                             "required": ["to", "body"]}},
            {"name": "sms.status", "description": "Look up a send record (free).",
             "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                             "required": ["id"]}},
            {"name": "sms.pricing", "description": "Live per-destination price in sats (free).",
             "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}}}},
            {"name": "sms.capabilities", "description": "Rail name + capability flags (free).",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "docs", "description": "The llms.txt contract, verbatim (free). "
                                            "Alias: llms.",
             "inputSchema": {"type": "object",
                             "properties": {"name": {"type": "string"}}}},
        ]

    def capability_tags(self) -> list[list[str]]:
        """CEP-8 `cap` tags. ADR-0002: ONE flat price per tool, not a prefix table.

        The old shape emitted three `cap` tags for the same tool (100 / 500 /
        default-500) built from the retired per-prefix table. A duplicate `cap`
        for one tool has no defined meaning for a client asserting that the
        invoice equals the advertised cap (CEP draft 0001 P4), and the three
        numbers matched neither the table nor ADR-0002's flat price. One tool,
        one price.
        """
        from .pricing import DEFAULT_PRICE_SATS

        return [
            ["cap", "tool:sms.send", str(DEFAULT_PRICE_SATS), "sats"],
            ["pmi", "bitcoin-cashu", "explicit_gating"],
        ]

    def call(self, tool: str, args: dict | None = None, caller: str = "") -> dict:
        """Dispatch one MCP tools/call. Returns an MCP tool result object."""
        args = args or {}
        try:
            if tool == "sms.send":
                payload = self.sms_send(args.get("to", ""), args.get("body", ""),
                                        caller=caller,
                                        settlement_receipt=args.get("settlement_receipt"))
            elif tool == "sms.status":
                payload = self.sms_status(args.get("id", ""))
            elif tool == "sms.pricing":
                payload = self.sms_pricing(args.get("to"))
            elif tool == "sms.capabilities":
                payload = self.sms_capabilities()
            elif tool in DOCS_ALIASES:
                return {"content": [{"type": "text", "text": self.docs_tool(args.get("name", tool))}]}
            else:
                raise PaymentError("unsupported method", f"Unknown tool {tool!r}.")
        except PaymentError as exc:
            return {"content": [{"type": "text", "text": json.dumps(exc.as_error())}],
                    "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}
