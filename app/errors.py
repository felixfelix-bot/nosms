"""Central error contract: every 4xx/5xx carries `X-Reason` + `X-Hint`.

`X-Reason` is a short machine token callers branch on; `X-Hint` is one human
sentence. A bare 401/404/500 is a bug in this service.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

#: status -> (reason, hint). Endpoints may override either.
HTTP_HINTS: dict[int, tuple[str, str]] = {
    400: ("bad_request", "The request could not be understood; check the body and headers."),
    401: ("auth_missing", "Add an 'Authorization: Nostr' header carrying a base64-encoded NIP-98 event."),
    404: ("not_found", "No such endpoint; see /llms.txt for the surface."),
    405: ("method_not_allowed", "That HTTP method is not allowed on this endpoint."),
    422: ("invalid_request", "The JSON body failed validation; see /llms.txt."),
    501: ("not_implemented", "This endpoint is not implemented yet; see /llms.txt."),
    500: ("internal_error", "Unexpected server error; retry or report it."),
}


def error_response(status_code: int, reason: str, hint: str,
                   extra: dict | None = None) -> JSONResponse:
    payload = {"error": reason, "hint": hint}
    if extra:
        payload.update(extra)
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"X-Reason": reason, "X-Hint": hint},
    )
