"""ASGI app factory for the nosms M1a service core.

Surface: ``GET /api/health`` (open), ``GET /api/pricing`` (open), ``GET /llms.txt``
(open), ``GET /llms-full.txt`` (501 stub), ``POST /api/send`` (NIP-98 authed).

``POST /api/send`` authenticates and validates in M1a and then answers 501 with a
reason and a quoted price: the money-moving path is the sibling M1b card. Saying so
honestly is the point - M1a must never look like it sent something it did not.

The transport is injected, never constructed per request, so the health endpoint
stays cheap and the suite runs with zero network.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .config import Config
from .errors import ApiError, error_response, hint_for, register_error_handlers
from .llms import LLMS_FULL_TXT_STUB, LLMS_TXT
from .nip98 import Nip98Error, ReplayCache, verify_nip98_header
from .pricing import BadDestination, describe_table, normalize_e164, price_for

SERVICE_TITLE = "nosms"


class SendRequest(BaseModel):
    """The documented send body. Kept minimal on purpose."""

    to: str
    text: str


def create_app(
    config: Config | None = None,
    transport: object | None = None,
    transport_name: str | None = None,
) -> FastAPI:
    """Build the ASGI app. Everything environment-dependent is injectable."""
    resolved = config or Config.from_env()
    if transport_name:
        resolved = resolved.with_transport(transport_name)

    app = FastAPI(
        title=SERVICE_TITLE,
        version=resolved.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = resolved
    app.state.transport = transport
    app.state.replay_cache = ReplayCache()
    register_error_handlers(app)

    @app.exception_handler(BadDestination)
    async def _bad_destination(_request, exc: BadDestination):
        return error_response(400, "bad_destination", getattr(exc, "hint", None))

    def require_nip98(request: Request) -> str:
        """Authenticate the request or raise an :class:`ApiError`."""
        try:
            return verify_nip98_header(
                request.headers.get("authorization"),
                url=str(request.url),
                method=request.method,
                replay_cache=request.app.state.replay_cache,
            )
        except Nip98Error as exc:
            raise ApiError(exc.status_code, exc.reason, exc.hint) from exc

    @app.get("/api/health")
    def health(request: Request) -> dict:
        """Cheap liveness. Configuration only - no outbound calls, no probing."""
        cfg: Config = request.app.state.config
        return {
            "ok": True,
            "service": cfg.service,
            "version": cfg.version,
            "commit": cfg.commit,
            "transport": cfg.transport,
            "env": cfg.env,
        }

    @app.get("/api/pricing")
    def pricing() -> dict:
        return describe_table()

    @app.post("/api/send")
    def send(payload: SendRequest, request: Request,
             pubkey: str = Depends(require_nip98)) -> dict:
        dest = normalize_e164(payload.to)
        price = price_for(dest)
        raise ApiError(
            501,
            "not_implemented",
            f"Send lands with M1b; the quoted price for {dest} is {price} sats.",
        )

    @app.get("/llms.txt", response_class=PlainTextResponse)
    def llms_txt() -> str:
        return LLMS_TXT

    @app.get("/llms-full.txt")
    def llms_full() -> str:
        raise ApiError(501, "not_implemented", str(hint_for("not_implemented")))

    return app


app = create_app()
