"""Rail #3: carrier email-to-SMS gateways.

Mechanism: mail ``<number>@<carrier-gateway>`` and the carrier hands it to the
handset. Zero KYC, zero provider account, real SMS on a real phone.

Honesty rules baked in:
* **No delivery receipt exists.** The rail reports ``best_effort=True`` and
  always returns ``receipt=None``. Nothing downstream may claim delivery.
* **US/CA only.** Only +1 destinations are accepted; anything else is refused
  with the machine token ``destination_unsupported`` so callers can branch on it.
* **The recipient's carrier must be known.** A wrong gateway means silent loss, so
  no carrier guess is made: an unknown carrier is ``carrier_unknown``. Carrier
  lookup (HLR) is a later milestone.
* ``txt.att.net`` is deliberately absent: it has no MX record (verified
  2026-09-26), so mail to it is undeliverable.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .base import Capabilities, SendResult
from .errors import UnsupportedDestination   # re-exported: rail-agnostic now

#: carrier id -> the gateway domains carriers actually accept mail on.
#: Only domains verified to have MX records belong here.
CARRIER_GATEWAYS: dict[str, list[str]] = {
    "tmobile": ["tmomail.net"],
    "verizon": ["vtext.com"],
    "att": ["txt.att.net.missing-mx"],   # placeholder replaced below
    "boost": ["sms.myboostmobile.com"],
    "cricket": ["sms.cricketwireless.net"],
    "uscellular": ["email.uscc.net"],
    "googlefi": ["msg.fi.google.com"],
}
# txt.att.net has no MX (verified 2026-09-26) - drop it entirely rather than
# offer a gateway that silently loses mail.
CARRIER_GATEWAYS["att"] = ["mms.att.net"] if False else []

MAX_BODY_CHARS = 140 * 4          # carriers truncate; callers must know


# `UnsupportedDestination` is imported from `.errors` above and re-exported here
# so the historical import path keeps working and one class is caught everywhere.


def _e164(dest: str) -> str:
    digits = "".join(ch for ch in dest if ch.isdigit())
    return f"+{digits}"


class EmailGatewayTransport:
    name = "email_gateway"

    def __init__(self, smtp_host: str = "localhost", smtp_port: int = 25,
                 sender: str = "sms@orangesync.tech", smtp_factory=None):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self._smtp_factory = smtp_factory or (
            lambda host, port: smtplib.SMTP(host, port, timeout=30))

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(available=True, best_effort=True,
                            delivery_receipts=False, countries=["US", "CA"])

    def _gateway(self, number: str, carrier: str | None) -> str:
        if not carrier:
            raise UnsupportedDestination("carrier_unknown",
                                         "recipient carrier required")
        domains = CARRIER_GATEWAYS.get(carrier.strip().lower())
        if not domains:
            raise UnsupportedDestination("carrier_unknown",
                                         f"no gateway for carrier {carrier!r}")
        return domains[0]

    def send(self, dest: str, body: str, carrier: str | None = None,
             **kwargs) -> SendResult:
        number = _e164(dest)
        # country gate FIRST: a non-US/CA destination is never a carrier problem
        if not number.startswith("+1") or len(number) != 12:
            raise UnsupportedDestination("destination_unsupported",
                                         "rail covers US/CA (+1) only")
        if not body:
            return SendResult(accepted=False, rail=self.name, best_effort=True,
                              receipt=None, detail="empty body")
        to = f"{number[1:]}@{self._gateway(number, carrier)}"

        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = ""
        msg.set_content(body[:MAX_BODY_CHARS])

        try:
            with self._smtp_factory(self.smtp_host, self.smtp_port) as smtp:
                smtp.sendmail(self.sender, [to], msg.as_string())
        except Exception as e:                      # noqa: BLE001 - never die
            return SendResult(accepted=False, rail=self.name, best_effort=True,
                              receipt=None,
                              detail=f"smtp send failed: {type(e).__name__}")
        return SendResult(accepted=True, rail=self.name, best_effort=True,
                          receipt=None, detail=f"queued to {to}")
