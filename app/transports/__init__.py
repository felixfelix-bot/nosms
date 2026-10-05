"""nosms transports — one interface, several rails, honest flags.

Rails
-----
* :class:`FakeTransport` — deterministic double (tests only, never best-effort)
* :class:`EmailGatewayTransport` — carrier email-to-SMS (rail #3)
* :class:`TelnyxTransport` — the Telnyx SMS gateway (M1b); pollable
* :class:`JmpCheogramTransport` — the operator's JMP/Cheogram line (ADR-0002)

Composition
-----------
* :class:`FailoverTransport` — puts the email rail behind JMP as the degrade
  path required by ADR-0002, keeping refundability honest.

Entry point
-----------
:func:`build_transport` is the single place the service chooses a rail from
configuration; capability flags always come from the rail that is actually
serving. (The HTTP service has its own ``app.main.build_transport(cfg)`` for the
Telnyx/escrow path; both are kept because they serve different entry points.)
"""
from __future__ import annotations

import os

from .base import (Capabilities, Pollable, SendResult, Transport, TransportStatus,
                   maybe_await, normalize_status)
from .email_gateway import EmailGatewayTransport
from .errors import RailPaced, RailUnavailable, UnsupportedDestination
from .failover import FailoverTransport
from .fake import FakeTransport
from .jmp_cheogram import JmpCheogramTransport, JmpLink, is_rail_down
from .pacing import Pacer, PacingDecision, PacingPolicy, load_pacing_policy
from .telnyx import SmsGatewayNotFound, TelnyxTransport, load_sms_gateway

__all__ = [
    "Capabilities", "SendResult", "Transport", "Pollable", "TransportStatus",
    "maybe_await", "normalize_status",
    "EmailGatewayTransport", "UnsupportedDestination",
    "RailUnavailable", "RailPaced",
    "FakeTransport",
    "TelnyxTransport", "SmsGatewayNotFound", "load_sms_gateway",
    "JmpCheogramTransport", "JmpLink", "is_rail_down",
    "FailoverTransport",
    "Pacer", "PacingPolicy", "PacingDecision", "load_pacing_policy",
    "build_transport",
]

DEFAULT_PACING_STATE = "~/.hermes/profiles/manager/state/jmp_pacing.json"
DEFAULT_INBOX_DB = "~/.hermes/profiles/manager/state/jmp_inbox.db"


def build_transport(name: str | None = None, *, env: dict | None = None,
                    link=None) -> Transport:
    """Build the configured rail. The only factory the app should call.

    Names: ``fake`` (default), ``email_gateway``, ``jmp_cheogram`` (JMP with the
    email degrade path behind it), ``jmp_only`` (JMP alone, no degrade).
    """
    e = os.environ if env is None else env
    name = (name or e.get("NOSMS_TRANSPORT") or "fake").strip().lower()

    if name in ("fake", ""):
        return FakeTransport()
    if name in ("email_gateway", "email"):
        return EmailGatewayTransport(
            smtp_host=e.get("NOSMS_SMTP_HOST", "localhost"),
            smtp_port=int(e.get("NOSMS_SMTP_PORT", "25")),
            sender=e.get("NOSMS_SMTP_SENDER", "sms@orangesync.tech"))
    if name in ("jmp_cheogram", "jmp_only", "jmp"):
        from .jmp_link import SlixmppLink         # lazy: no XMPP stack to import
        if link is None:
            link = SlixmppLink.from_env(e)
        pacer = Pacer(load_pacing_policy(e),
                      state_path=e.get("NOSMS_JMP_PACING_STATE", DEFAULT_PACING_STATE))
        primary = JmpCheogramTransport(link, pacer=pacer)
        if name == "jmp_only":
            return primary
        return FailoverTransport(primary, EmailGatewayTransport(
            smtp_host=e.get("NOSMS_SMTP_HOST", "localhost"),
            smtp_port=int(e.get("NOSMS_SMTP_PORT", "25")),
            sender=e.get("NOSMS_SMTP_SENDER", "sms@orangesync.tech")))
    raise ValueError(f"unknown transport {name!r}")
