"""The X-Reason / X-Hint error contract.

Every non-2xx response from this service carries two headers:

* ``X-Reason`` - a short machine token (``auth_missing``, ``bad_destination``, ...)
* ``X-Hint``   - one human sentence saying what to do next

There is no such thing as a bare ``401`` or a template 404: a client that gets an
error always learns both *what* went wrong and *how* to fix it. Hint strings are
ASCII-only because HTTP header values must be latin-1 encodable.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

#: Canonical token -> hint. Keep ASCII only.
HINTS: dict[str, str] = {
    "auth_missing": "Add an Authorization header carrying a signed NIP-98 event; see /llms.txt.",
    "auth_malformed": "The Authorization header must be 'Nostr ' plus base64 JSON of a kind 27235 event.",
    "auth_invalid": "Check the signature, the kind (27235), the u tag (request URL) and the method tag.",
    "auth_stale": "created_at must be within 120 seconds of server time; re-sign and retry.",
    "auth_replay": "This signed event was already used; sign a fresh event for every request.",
    "bad_destination": "Send an E.164 destination such as +14155551234 (a leading + plus 7-15 digits).",
    "validation_error": 'The body must be JSON of the form {"to": "+<E164>", "text": "..."}.',
    "insufficient_funds": "The Cashu token is worth less than the quoted price for this destination.",
    "transport_error": "The SMS rail failed after accepting the request; poll /api/message/:id/status.",
    "not_found": "No such route; the full API surface is listed at /llms.txt.",
    "method_not_allowed": "That HTTP method is not allowed on this route; see /llms.txt.",
    "not_implemented": "This endpoint lands with a later milestone; the price is quoted, nothing was sent.",
    "internal_error": "Unexpected server error; nothing was sent and no funds were moved.",
}

#: Fallback for a status code we did not enumerate.
_STATUS_TOKENS = {
    400: "bad_request",
    401: "auth_invalid",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    429: "rate_limited",
    501: "not_implemented",
    503: "unavailable",
}
_STATUS_HINTS = {
    "bad_request": "The request could not be parsed; see /llms.txt for the expected shape.",
    "forbidden": "This identity may not perform that action.",
    "rate_limited": "Slow down; the per-identity daily cap or per-destination cooldown was hit.",
    "unavailable": "The service is temporarily unable to serve this request; retry shortly.",
}


def hint_for(reason: str) -> str:
    return HINTS.get(reason) or _STATUS_HINTS.get(reason) or "Request rejected; see /llms.txt."


class ApiError(Exception):
    """An error the client can act on. Always rendered with both headers."""

    def __init__(self, status_code: int, reason: str, hint: str | None = None):
        self.status_code = int(status_code)
        self.reason = reason
        self.hint = hint or hint_for(reason)
        super().__init__(f"{self.status_code} {reason}")


def error_response(status_code: int, reason: str, hint: str | None = None) -> JSONResponse:
    """Build the one error shape this service ever emits."""
    resolved = hint or hint_for(reason)
    # Header values must be latin-1 safe; hints are authored ASCII.
    safe = resolved.encode("ascii", "replace").decode("ascii")
    return JSONResponse(
        status_code=status_code,
        content={"error": {"reason": reason, "hint": safe}},
        headers={"X-Reason": reason, "X-Hint": safe},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install handlers so *every* error path carries the contract."""

    @app.exception_handler(ApiError)
    async def _api_error(_request, exc: ApiError):
        return error_response(exc.status_code, exc.reason, exc.hint)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request, exc: StarletteHTTPException):
        reason = _STATUS_TOKENS.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) else ""
        stock = {"Not Found", "Method Not Allowed", "Unauthorized", "Forbidden", "Bad Request"}
        hint = detail if detail and detail not in stock else hint_for(reason)
        return error_response(exc.status_code, reason, hint)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request, exc: RequestValidationError):
        return error_response(422, "validation_error", hint_for("validation_error"))

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc: Exception):  # pragma: no cover - safety net
        return error_response(500, "internal_error", hint_for("internal_error"))
