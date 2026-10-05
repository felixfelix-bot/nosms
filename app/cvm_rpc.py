"""CVM JSON-RPC dispatch — the protocol layer, pure and offline-testable.

ContextVM traffic is MCP JSON-RPC carried inside kind-25910 events. This module
turns one decoded request into exactly one response, so the wire layer
(``scripts/run_cvm_server.py``) only has to unwrap, dispatch and gift-wrap back.

The rule that matters most here: **a request always produces a response.** The
live bug that motivated this module was a server that received an unpaid
``tools/call``, published *something*, and left the client waiting forever — the
caller could not tell a declined payment from a dead relay. Every branch below
returns a JSON-RPC object; only a genuine notification (no ``id``) is silent,
because JSON-RPC specifies that no response is sent to a notification.
"""
from __future__ import annotations

from .cvm import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    SERVER_NAME,
    SERVER_VERSION,
    jsonrpc_error,
    jsonrpc_result,
)

#: protocol revision this server speaks (MCP 2025-11-25).
PROTOCOL_VERSION = "2025-11-25"

#: methods that are notifications by JSON-RPC definition: never answered.
NOTIFICATION_PREFIX = "notifications/"


def _server_info() -> dict:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def handle_rpc(rpc: object, tools, caller: str = "") -> dict | None:
    """Dispatch one decoded CVM request. Returns the response, or None for a
    notification (which JSON-RPC forbids answering)."""
    if not isinstance(rpc, dict):
        return jsonrpc_error(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 object.")

    rpc_id = rpc.get("id")
    method = rpc.get("method")
    is_notification = rpc_id is None and isinstance(method, str)

    if rpc.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method:
        return jsonrpc_error(rpc_id, INVALID_REQUEST,
                             "Expected a JSON-RPC 2.0 request with a string method.")

    # JSON-RPC: a NOTIFICATION (no id) is never answered, for any method —
    # including one we do not know. This is the only intentional silence in the
    # protocol, and it is not a refused request.
    if is_notification and method.startswith(NOTIFICATION_PREFIX):
        return None

    if method == "initialize":
        return jsonrpc_result(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": _server_info(),
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False}},
        })

    if method == "ping":
        return jsonrpc_result(rpc_id, {})

    if method == "tools/list":
        return jsonrpc_result(rpc_id, {"tools": tools.tool_definitions()})

    if method == "resources/list":
        return jsonrpc_result(rpc_id, {"resources": tools.resource_list()})

    if method == "resources/read":
        params = rpc.get("params") or {}
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return jsonrpc_error(rpc_id, INVALID_PARAMS, "A resource `uri` is required.")
        try:
            text = tools.read_resource(uri)
        except Exception as exc:                             # noqa: BLE001
            reason = getattr(exc, "reason", "not_found")
            return jsonrpc_error(rpc_id, INVALID_PARAMS, getattr(exc, "hint", str(exc)),
                                 {"reason": reason})
        return jsonrpc_result(rpc_id, {"contents": [
            {"uri": uri, "mimeType": "text/plain", "text": text}]})

    if method == "tools/call":
        params = rpc.get("params")
        if not isinstance(params, dict):
            return jsonrpc_error(rpc_id, INVALID_PARAMS, "`params` must be an object.")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return jsonrpc_error(rpc_id, INVALID_PARAMS, "A tool `name` is required.")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return jsonrpc_error(rpc_id, INVALID_PARAMS, "`arguments` must be an object.")
        # tools.call() itself never returns nothing: every refusal is an isError
        # result (see app/cvm_tools.py), so the reply below is always populated.
        return jsonrpc_result(rpc_id, tools.call(name, arguments, caller=caller))

    return jsonrpc_error(rpc_id, METHOD_NOT_FOUND, f"Unknown method {method!r}.",
                         {"reason": "unsupported method"})
