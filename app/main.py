"""ASGI app factory for the nosms service (M1a core + M1b paid send path).

Surface:
  GET  /api/health               — open, cheap, no outbound calls
  POST /api/send                 — NIP-98 auth + Cashu postage, escrowed at the mint
  GET  /api/message/:id/status   — normalised status + the provider's raw string
  GET  /api/refund/:id           — the refund token for an undelivered message
  GET  /llms.txt                 — agent-facing contract (rendered from live Config)
  GET  /llms-full.txt            — 501 stub until a later milestone

Everything is injected: config, transport and mint. The whole service therefore
runs end to end in-process with zero network — which is how the suite exercises
it — and the money path is only as real as the objects handed in.
"""
from __future__ import annotations

import json
import os
import secrets

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse

from .cashu import CashuError, MintClient, decode_token, encode_token, fee_for, split_amounts
from .config import Config
from .errors import HTTP_HINTS, error_response
from .escrow import EscrowStore
from .llms import llms_txt
from .nip98 import InMemoryReplayCache, Nip98Error, Nip98Verifier, verify_nip98
from .pricing import InvalidDestination, normalize_e164, price_for
from .quota import QuotaError, QuotaStore
from .refunds import refund_all
from .transports import (EmailGatewayTransport, FakeTransport, TelnyxTransport,
                         WhatsAppTransport, maybe_await)
from .transports.errors import RailPaced, RailUnavailable
from .transports.whatsapp import DEFAULT_HALT_STATE as WHATSAPP_HALT_STATE


def build_transport(cfg: Config):
    """Map the configured rail name onto a transport instance."""
    name = (cfg.transport or "fake").lower()
    if name == "telnyx":
        try:
            return TelnyxTransport.from_config(cfg.sms_gateway_path)
        except Exception:                                    # noqa: BLE001
            # An unimportable sms-gateway must not take the service down; the
            # adapter then advertises itself unavailable and refuses to send.
            return TelnyxTransport(provider=None, api_key="", from_number="")
    if name in ("email", "email_gateway"):
        return EmailGatewayTransport()
    if name in ("whatsapp", "wa"):
        # ADR-0003: the official Android client over adb. Built from Config so the
        # NOSMS_WHATSAPP_* values have exactly one reader. The persisted
        # kill-switch path is resolved from the environment here, so a restart
        # after a detected ban does not re-touch the device.
        return WhatsAppTransport.from_service_config(
            cfg, halt_path=os.environ.get("NOSMS_WHATSAPP_HALT_STATE",
                                          WHATSAPP_HALT_STATE))
    return FakeTransport()


async def _json_object(request: Request) -> dict | None:
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def create_app(config: Config | dict | None = None, transport=None, mint=None) -> FastAPI:
    if isinstance(config, Config):
        cfg = config
    elif isinstance(config, dict):
        cfg = Config.from_env(**config)
    else:
        cfg = Config.from_env()

    app = FastAPI(title=cfg.service, version=cfg.version)
    app.state.config = cfg
    app.state.transport = transport if transport is not None else build_transport(cfg)
    app.state.mint = mint if mint is not None else MintClient(cfg.mint_url)
    app.state.escrow = EscrowStore(cfg.escrow_db)
    app.state.quota = QuotaStore(cfg.escrow_db,
                                 cooldown_seconds=cfg.destination_cooldown_seconds,
                                 daily_cap=cfg.daily_cap)
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
            "mint": cfg.mint_url,
            "refund_after_seconds": cfg.refund_after_seconds,
            "ok": True,
        }

    @app.post("/api/send")
    async def send(request: Request, pubkey: str = Depends(require_nip98)):
        escrow = request.app.state.escrow
        quota = request.app.state.quota
        mint = request.app.state.mint
        rail = request.app.state.transport

        payload = await _json_object(request)
        if payload is None:
            return error_response(400, "bad_request",
                                  "POST /api/send expects a JSON object body.")

        # 1. postage token (header is canonical; documented in /llms.txt)
        token = request.headers.get("x-cashu")
        if not token and isinstance(payload.get("cashu"), str):
            token = payload["cashu"]
        if not token:
            return error_response(400, "token_missing",
                                  "Attach your Cashu postage token in the `X-Cashu` header.")

        # 2. message body
        text = payload.get("text")
        if text is None:
            text = payload.get("body")
        if not isinstance(text, str) or not text.strip():
            return error_response(400, "empty_body",
                                  "The `text` field must be a non-empty message body.")
        if len(text) > cfg.max_body_chars:
            return error_response(400, "body_too_long",
                                  f"The message body is longer than {cfg.max_body_chars} characters.")

        # 3. destination + price
        try:
            dest = normalize_e164(payload.get("to"))
        except InvalidDestination as exc:
            return error_response(400, "bad_destination", exc.hint)
        price = price_for(dest)

        # 4. token: encoding, mint, proof states
        try:
            token_obj = decode_token(token)
        except CashuError as exc:
            return error_response(400, exc.reason, exc.hint)
        if token_obj.mint.rstrip("/") != cfg.mint_url.rstrip("/"):
            return error_response(
                400, "token_wrong_mint",
                f"This service escrows against {cfg.mint_url}; that token is from "
                f"{token_obj.mint}.")
        try:
            states = list(await maybe_await(mint.check_states(token_obj.proofs)))
        except CashuError as exc:
            return error_response(502, exc.reason, exc.hint)
        if any(state == "SPENT" for state in states):
            return error_response(409, "token_already_spent",
                                  "Every proof in the token must be unspent; at least one "
                                  "was already redeemed.")
        if any(state != "UNSPENT" for state in states):
            seen = ", ".join(sorted({str(s) for s in states}))
            return error_response(409, "token_state_unknown",
                                  f"The mint reports a non-final proof state ({seen}); "
                                  f"retry in a moment.")

        # 5. coverage — the sender bears the mint's input fee, we charge the price
        try:
            ppk = int(await maybe_await(mint.fee_ppk_for(token_obj.proofs[0]["id"])))
        except Exception:                                    # noqa: BLE001
            ppk = 0
        mint_fee = fee_for(ppk, len(token_obj.proofs))
        net = token_obj.amount - mint_fee
        if net < price:
            shortfall = price - net
            return error_response(
                402, "insufficient_funds",
                f"token is {token_obj.amount} sats; {dest} costs {price} sats "
                f"(short {shortfall}; the mint takes a {mint_fee} sat input fee).",
                extra={"shortfall_sats": shortfall, "price_sats": price,
                       "token_sats": token_obj.amount, "mint_fee_sats": mint_fee,
                       "net_sats": net})

        # 6. abuse controls — atomically, before any funds are consumed.
        # ``check_and_record`` holds the store lock across both the check and the
        # record, closing the check-then-act window: a separate ``check`` here and
        # ``record`` after the swap let two concurrent same-identity sends to one
        # destination both pass the cooldown. Recording before the swap means a
        # failed swap still consumes the slot, but no funds are taken (502 below).
        try:
            quota.check_and_record(pubkey, dest)
        except QuotaError as exc:
            return error_response(429, exc.reason, exc.hint,
                                  extra={"retry_after_seconds": exc.retry_after})

        # 7. escrow: swap the sender's proofs for fresh ones we control
        change = net - price
        keep_amounts = split_amounts(price)
        change_amounts = split_amounts(change)
        try:
            captured = list(await maybe_await(
                mint.swap(token_obj.proofs, keep_amounts + change_amounts)))
        except CashuError as exc:
            return error_response(502, "mint_error", exc.hint,
                                  extra={"mint": cfg.mint_url})
        except Exception as exc:                             # noqa: BLE001
            return error_response(
                502, "mint_error",
                f"The escrow swap failed ({type(exc).__name__}); no postage was taken.")
        escrow_proofs = captured[:len(keep_amounts)]
        change_proofs = captured[len(keep_amounts):]

        message_id = "m_" + secrets.token_hex(8)
        record = escrow.create(
            message_id=message_id, pubkey=pubkey, dest=dest,
            rail=getattr(rail, "name", cfg.transport), price=price, change=change,
            escrow_token=encode_token(cfg.mint_url, escrow_proofs),
            change_token=encode_token(cfg.mint_url, change_proofs) if change_proofs else None,
            status="queued")

        # 8. hand it to the rail
        try:
            result = await maybe_await(rail.send(dest, text))
        except RailPaced as exc:
            # The rail declined to attempt the send (personal-line pacing): the
            # message never left, so the postage goes straight back and the caller
            # is told *when* to come back instead of being charged for a failure.
            escrow.update_status(message_id, status="failed",
                                 provider_status=f"rail_paced:{exc.reason}")
            refund_all(escrow, rail, message_id, "rail_paced")
            return error_response(
                429, "rail_paced",
                f"The rail is pacing this personal line ({exc.reason}); the "
                f"message was not attempted. Retry after {exc.retry_after} s — "
                f"your postage was refunded.",
                retry_after=exc.retry_after,
                extra={"message_id": message_id, "pacing_reason": exc.reason,
                       "retry_after_seconds": exc.retry_after,
                       "refunded": True, "refund_sats": record.held})
        except RailUnavailable as exc:
            # The rail stopped itself (banned line, terminated account): fail
            # loudly and machine-branchably. A retry loop is the operator's
            # problem to fix, never this endpoint's to hide.
            escrow.update_status(message_id, status="failed",
                                 provider_status=f"rail_unavailable:{exc.reason}")
            refund_all(escrow, rail, message_id, "rail_unavailable")
            return error_response(
                503, "rail_unavailable",
                f"The rail is down ({exc.reason}); the message was not sent and "
                f"your postage was refunded.",
                extra={"message_id": message_id, "rail_reason": exc.reason,
                       "refunded": True, "refund_sats": record.held})
        except Exception as exc:                             # noqa: BLE001
            escrow.update_status(message_id, status="failed",
                                 provider_status=f"transport_exception:{type(exc).__name__}")
            refund_all(escrow, rail, message_id, "transport_error")
            return error_response(
                502, "transport_error",
                f"The rail raised {type(exc).__name__}; your postage was refunded.",
                extra={"message_id": message_id, "refunded": True,
                       "refund_sats": record.held})

        if not result.accepted:
            escrow.update_status(message_id, status="failed",
                                 provider_status=result.status or "failed")
            refund_all(escrow, rail, message_id, "transport_error")
            return error_response(
                502, "transport_error",
                f"{result.detail or 'The rail refused the message'}; your postage was refunded.",
                extra={"message_id": message_id, "refunded": True,
                       "refund_sats": record.held})

        status = result.status if result.status in (
            "queued", "sent", "delivered", "failed") else "queued"
        escrow.update_status(message_id, status=status, provider_status=status,
                            provider_message_id=result.receipt)
        return JSONResponse({
            "message_id": message_id,
            "price_sats": price,
            "change_sats": change,
            "mint_fee_sats": mint_fee,
            "status": status,
            "provider": getattr(rail, "name", cfg.transport),
            "status_url": f"/api/message/{message_id}/status",
            "refund_url": f"/api/refund/{message_id}",
            "escrow": {"mint": cfg.mint_url, "amount_sats": price},
            "note": "queued is not delivered: poll the status URL for the rail's own report.",
        })

    @app.get("/api/message/{message_id}/status")
    async def message_status(message_id: str, request: Request,
                             pubkey: str = Depends(require_nip98)):
        escrow = request.app.state.escrow
        rail = request.app.state.transport
        record = escrow.get(message_id)
        if record is None or record.pubkey != pubkey:
            return error_response(404, "not_found", "No such message for this identity.")
        # poll the provider, but never downgrade a terminal state
        if record.provider_message_id and record.status not in ("delivered", "failed"):
            try:
                polled = await maybe_await(rail.status(record.provider_message_id))
                escrow.update_status(message_id, status=polled.normalized,
                                     provider_status=polled.raw)
            except Exception:                                # noqa: BLE001
                pass
            record = escrow.get(message_id)
        body = {
            "message_id": record.message_id,
            "status": record.status,
            "provider_status": record.provider_status or record.status,
            "provider": record.rail,
            "dest": record.dest,
            "price_sats": record.amount,
            "change_sats": record.change,
            "refunded": record.refunded,
            "created_at": int(record.created_at),
            "status_url": f"/api/message/{record.message_id}/status",
        }
        if record.refunded:
            body["refund"] = {"amount_sats": record.refund_amount,
                              "reason": record.refund_reason,
                              "url": f"/api/refund/{record.message_id}"}
        return JSONResponse(body)

    @app.get("/api/refund/{message_id}")
    async def refund(message_id: str, request: Request,
                     pubkey: str = Depends(require_nip98)):
        escrow = request.app.state.escrow
        record = escrow.get(message_id)
        if record is None or record.pubkey != pubkey:
            return error_response(404, "not_found", "No such message for this identity.")
        if not record.refunded:
            return error_response(
                409, "not_refunded",
                "This message has not been refunded; refunds are issued by the "
                f"T+{cfg.refund_after_seconds // 60} min sweep when a message is not delivered.")
        return JSONResponse({
            "message_id": record.message_id,
            "amount_sats": record.refund_amount,
            "token": record.refund_token,
            "mint": cfg.mint_url,
            "reason": record.refund_reason,
            "note": "Bearer ecash: redeem it at the mint with any Cashu wallet.",
        })

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
