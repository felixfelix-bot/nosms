"""ContextVM (CVM) protocol constants, CEP-6 announcements and contract rendering.

ContextVM transports MCP JSON-RPC over Nostr. Traffic rides **kind 25910**
(ephemeral — relays may drop it), while discovery rides the public CEP-6 service
catalog (replaceable kinds 11316-11320). Encryption is NIP-17/NIP-59 gift wrap:
kind 1059 for the wrapped traffic.

Nothing here talks to a relay: this module is pure so the whole protocol surface
is unit-testable offline. ``scripts/run_cvm_server.py`` owns the wire.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

#: all CVM message traffic (ephemeral; 20000-30000).
CVM_MESSAGE_KIND = 25910

#: CEP-6 service catalog: 11316 server, 11317 tools, 11318 resources,
#: 11319 resource templates, 11320 prompts. All replaceable.
ANNOUNCE_SERVER = 11316
ANNOUNCE_TOOLS = 11317
ANNOUNCE_RESOURCES = 11318
ANNOUNCE_RESOURCE_TEMPLATES = 11319
ANNOUNCE_PROMPTS = 11320

#: the catalog kinds this server actually publishes.
ANNOUNCEMENT_KINDS = (ANNOUNCE_SERVER, ANNOUNCE_TOOLS)

#: gift-wrap kinds carrying the encrypted traffic.
GIFT_WRAP_KINDS = (1059, 21059)

#: Relays verified live on 2026-10-05 by an independent second-key run.
#:
#: * relay.contextvm.org and relay2.contextvm.org are BOTH alive (strfry,
#:   NIP-11 OK, 131072-byte max message) — the older note that only
#:   relay2 answered is stale, and relay.contextvm.org is NOT unreachable.
#: * relay.primal.net is kept: it accepted every gift-wrap.
#: * relay.damus.io is dropped: it banned this host's IP for rate-limit
#:   violations ("banned: too many rate-limit violations, try again later").
#: * relay.nostr.band / cvm.otherstuff.ai: unreachable or no NIP-11 that day.
WORKING_RELAYS = (
    "wss://relay.contextvm.org",
    "wss://relay2.contextvm.org",
    "wss://relay.primal.net",
)

#: NIP-98-style freshness window for a CEP-8 quote: a quote the caller pays
#: against must be recent, so a stale price cannot be replayed at a new rate.
QUOTE_TTL_SECONDS = 15 * 60

#: service identity published in the CEP-6 catalog.
SERVER_NAME = "nosms"
SERVER_VERSION = "0.2.0"
SERVER_ABOUT = ("Send SMS on behalf of others over ContextVM. Agent-first: pay per "
                "send in Cashu. No account, no API key, no signup.")


@dataclass(frozen=True)
class ContractContext:
    """Values that only exist at publish time and are baked into the contract."""

    npub: str = "<set at publish>"
    relays: tuple[str, ...] = WORKING_RELAYS
    contract_url: str = "https://nosms.orangesync.tech/cvm/llms.txt"
    naddr: str | None = None

    def as_dict(self) -> dict:
        return {"npub": self.npub, "relays": list(self.relays),
                "contract_url": self.contract_url, "naddr": self.naddr}


def mcp_tool_result(payload: dict) -> dict:
    """An MCP tool result: JSON text content, no error flag."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def mcp_tool_error(reason: str, hint: str, **extra) -> dict:
    """An MCP tool *error* result — always a visible reply, never silence.

    Every refusal on the paid path goes through here. A refusal the caller
    cannot see is indistinguishable from a dead server, which is exactly the
    bug this shape exists to prevent.
    """
    body = {"reason": reason, "hint": hint}
    body.update({k: v for k, v in extra.items() if v is not None})
    return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": True}


def jsonrpc_result(rpc_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def jsonrpc_error(rpc_id, code: int, message: str, data: dict | None = None) -> dict:
    error: dict = {"code": int(code), "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


#: JSON-RPC error codes we emit (never a bare HTTP-ish 500).
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def contract_tags(ctx: ContractContext, cap_tags: list[list[str]]) -> list[list[str]]:
    """CEP-6 announcement tags: identity, discovery topics, contract, CEP-8 caps.

    The contract URL is carried as a ``contract`` tag **and** the docs tool name
    is advertised, so ``cvmi discover`` surfaces how to learn the contract.
    """
    tags = [
        ["d", SERVER_NAME],
        ["name", SERVER_NAME],
        ["t", "sms"],
        ["t", "contextvm"],
        ["t", "mcp"],
        ["contract", ctx.contract_url],
        ["docs", "docs"],
        ["docs", "llms"],
    ]
    if ctx.naddr:
        tags.append(["naddr", ctx.naddr])
    for relay in ctx.relays:
        tags.append(["relay", relay])
    tags.extend(cap_tags)
    return tags
