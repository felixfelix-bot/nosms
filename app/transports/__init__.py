"""nosms transports — one interface, several rails, honest flags.

Rails
-----
* :class:`FakeTransport` — deterministic double (tests only, never best-effort)
* :class:`EmailGatewayTransport` — carrier email-to-SMS (rail #3)
* :class:`TelnyxTransport` — the Telnyx SMS gateway (M1b); pollable
* :class:`JmpCheogramTransport` — the operator's JMP/Cheogram line (ADR-0002)
* :class:`WhatsAppTransport` — the official Android client driven over adb
  (ADR-0003); no delivery receipts, ever

Composition
-----------
* :class:`FailoverTransport` — puts the email rail behind JMP as the degrade
  path required by ADR-0002, keeping refundability honest. It is also offered
  behind the WhatsApp rail (``whatsapp_email``), which reaches the same wrapper
  by *raising* ``RailUnavailable`` rather than returning the outage token.

Entry point
-----------
:func:`build_transport` is the single place the service chooses a rail from
configuration; capability flags always come from the rail that is actually
serving. (The HTTP service has its own ``app.main.build_transport(cfg)`` for the
Telnyx/escrow path; both are kept because they serve different entry points.)

Names: ``fake`` (default), ``email_gateway``, ``jmp_cheogram`` / ``jmp_only``,
``whatsapp`` (the paced rail from ADR-0003, alone) and ``whatsapp_email`` (the
same rail with the email degrade path behind it).
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
from .whatsapp import (AdbWhatsAppDriver, WhatsAppConfig, WhatsAppTransport,
                       DEFAULT_HALT_STATE as WHATSAPP_HALT_STATE)

__all__ = [
    "Capabilities", "SendResult", "Transport", "Pollable", "TransportStatus",
    "maybe_await", "normalize_status",
    "EmailGatewayTransport", "UnsupportedDestination",
    "RailUnavailable", "RailPaced",
    "FakeTransport",
    "TelnyxTransport", "SmsGatewayNotFound", "load_sms_gateway",
    "JmpCheogramTransport", "JmpLink", "is_rail_down",
    "WhatsAppTransport", "WhatsAppConfig", "AdbWhatsAppDriver",
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
    email degrade path behind it), ``jmp_only`` (JMP alone, no degrade),
    ``whatsapp`` (the official Android client over adb, ADR-0003).
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
    if name in ("whatsapp", "wa", "whatsapp_email"):
        # Building the rail touches no device and no network: the driver only
        # records its config, and `capabilities` never probes (see the module).
        # ``from_env`` also builds the rail's OWN pacing counter (ADR-0003
        # control #2: volume is the abuse surface), and the persisted kill-switch
        # path is resolved here so a restart cannot become the retry a ban
        # forbids.
        rail = WhatsAppTransport.from_env(
            e, halt_path=e.get("NOSMS_WHATSAPP_HALT_STATE", WHATSAPP_HALT_STATE))
        if name == "whatsapp_email":
            # ADR-0002's degrade path (control #3) behind the WhatsApp rail — the
            # same composition ``jmp_cheogram`` uses. The wrapper handles this
            # rail's *raising* shutdown shape as well as JMP's returned token.
            return FailoverTransport(rail, EmailGatewayTransport(
                smtp_host=e.get("NOSMS_SMTP_HOST", "localhost"),
                smtp_port=int(e.get("NOSMS_SMTP_PORT", "25")),
                sender=e.get("NOSMS_SMTP_SENDER", "sms@orangesync.tech")))
        return rail
    raise ValueError(f"unknown transport {name!r}")
