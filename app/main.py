"""ASGI app factory for the nosms service core (M1a).

Surface: `GET /api/health` (open), `POST /api/send` (NIP-98), `GET /llms.txt`,
`GET /llms-full.txt`. Everything takes its configuration from env; the
transport is injected so the whole service is testable with zero network.

The money-moving send path is the sibling M1b card: `/api/send` authenticates
and then answers 501 with an `X-Reason`, which proves an authorised request
reaches the handler.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse

from .config import Config
from .errors import HTTP_HINTS, error_response
from .llms import llms_txt
from .nip98 import InMemoryReplayCache, Nip98Error, Nip98Verifier, verify_nip98
from .transports import FakeTransport

SEND_NOT_IMPLEMENTED = (
    "send_not_implemented",
    "The paid send path ships in M1b; GET /llms.txt documents the contract.",
)


def create_app(config: Config | dict | None = None, transport=None) -> FastAPI:
    if isinstance(config, Config):
        cfg = config
    elif isinstance(config, dict):
        cfg = Config.from_env(**config)
    else:
        cfg = Config.from_env()

    app = FastAPI(title=cfg.service, version=cfg.version)
    app.state.config = cfg
    app.state.transport = transport if transport is not None else FakeTransport()
    app.state.nip98 = Nip98Verifier(
        freshness_seconds=cfg.freshness_seconds,
        replay_cache=InMemoryReplayCache(cfg.replay_ttl_seconds),
    )

    # --- central error contract: every 4xx/5xx carries X-Reason + X-Hint ---

    @app.exception_handler(Nip98Error)
    async def _nip98_handler(_request: Request, exc: Nip98Error):
        return error_response(401, exc.reason, exc.hint)

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_request: Request, exc: StarletteHTTPException):
        reason, hint = HTTP_HINTS.get(
            exc.status_code, (f"http_{exc.status_code}", "Request failed."))
        return error_response(exc.status_code, reason, hint)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, _exc: RequestValidationError):
        reason, hint = HTTP_HINTS[422]
        return error_response(422, reason, hint)

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, _exc: Exception):
        return error_response(500, "internal_error", HTTP_HINTS[500][1])

    # --- auth dependency --------------------------------------------------

    async def require_nip98(request: Request) -> str:
        body = await request.body()
        return verify_nip98(
            request.headers.get("authorization"),
            url=str(request.url),
            method=request.method,
            body=body or None,
            verifier=request.app.state.nip98,
        )

    # --- routes -----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        # cheap: no outbound calls, provider name straight from config
        return {
            "service": cfg.service,
            "version": cfg.version,
            "commit": cfg.commit,
            "transport": cfg.transport,
            "ok": True,
        }

    @app.post("/api/send")
    async def send(_request: Request, pubkey: str = Depends(require_nip98)) -> object:
        reason, hint = SEND_NOT_IMPLEMENTED
        return error_response(501, reason, hint, extra={"sender": pubkey})

    @app.get("/llms.txt", response_class=PlainTextResponse)
    async def llms() -> PlainTextResponse:
        return PlainTextResponse(llms_txt(cfg), media_type="text/plain; charset=utf-8")

    @app.get("/llms-full.txt")
    async def llms_full():
        return error_response(
            501, "llms_full_not_implemented",
            "The full manual ships in a later milestone; GET /llms.txt has the contract.",
        )

    return app
