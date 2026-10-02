"""RED-first tests for the nosms transport layer (§10 / M1b groundwork).

Behaviour pinned here:
- every transport exposes capability flags and they must be honest
- the email-to-SMS rail is US/CA only and must refuse anything else with the
  machine token `destination_unsupported`
- the email rail must never claim a delivery receipt
- a hard failure is distinguishable from acceptance (so escrow can refund)
"""
import smtplib
import pytest

from app.transports.base import Capabilities, SendResult, Transport
from app.transports.email_gateway import (
    CARRIER_GATEWAYS,
    EmailGatewayTransport,
    UnsupportedDestination,
)
from app.transports.fake import FakeTransport


# --- base contract ----------------------------------------------------------

def test_send_result_is_immutable_and_has_required_fields():
    r = SendResult(accepted=True, rail="email_gateway", best_effort=True,
                   receipt=None, detail="queued")
    assert r.accepted is True
    assert r.receipt is None
    with pytest.raises(Exception):
        r.accepted = False  # frozen dataclass


def test_capabilities_shape():
    c = Capabilities(available=True, best_effort=True, delivery_receipts=False,
                     countries=["US", "CA"])
    assert c.countries == ["US", "CA"]
    assert c.delivery_receipts is False


def test_fake_transport_implements_transport_protocol():
    t = FakeTransport()
    assert isinstance(t, Transport)
    assert t.capabilities.best_effort is False          # a fake is deterministic
    res = t.send("+15551230000", "hello")
    assert res.accepted is True and res.receipt is not None
    assert t.sent == [("+15551230000", "hello")]


# --- email rail honesty -----------------------------------------------------

def test_email_rail_advertises_best_effort_and_no_receipts():
    t = EmailGatewayTransport(smtp_factory=lambda *a, **k: _RecordingSMTP())
    assert t.capabilities.available is True
    assert t.capabilities.best_effort is True
    assert t.capabilities.delivery_receipts is False
    assert t.capabilities.countries == ["US", "CA"]


def test_email_rail_never_includes_att_without_mx():
    # txt.att.net has no MX record (verified 2026-09-26) - must not be offered
    flat = {d for domains in CARRIER_GATEWAYS.values() for d in domains}
    assert "txt.att.net" not in flat
    assert "tmomail.net" in flat and "vtext.com" in flat


@pytest.mark.parametrize("dest", ["+4915112345678", "+911234567890", "0049151", "+8613800138000"])
def test_email_rail_refuses_non_us_ca(dest):
    t = EmailGatewayTransport(smtp_factory=lambda *a, **k: _RecordingSMTP())
    with pytest.raises(UnsupportedDestination) as e:
        t.send(dest, "hello")
    assert e.value.reason == "destination_unsupported"


def test_email_rail_accepts_us_number_and_reports_no_receipt():
    smtp = _RecordingSMTP()
    t = EmailGatewayTransport(smtp_factory=lambda *a, **k: smtp)
    res = t.send("+15551234567", "hello", carrier="tmobile")
    assert res.accepted is True
    assert res.rail == "email_gateway"
    assert res.receipt is None            # a receipt we cannot observe
    assert res.best_effort is True
    assert smtp.sent and smtp.sent[0][0].endswith("@tmomail.net")


def test_email_rail_hard_failure_is_not_accepted():
    smtp = _RecordingSMTP(fail=True)
    t = EmailGatewayTransport(smtp_factory=lambda *a, **k: smtp)
    res = t.send("+15551234567", "hello", carrier="tmobile")
    assert res.accepted is False
    assert "smtp" in res.detail.lower()


def test_email_rail_requires_a_carrier_match_for_unknown_us_prefix():
    t = EmailGatewayTransport(smtp_factory=lambda *a, **k: _RecordingSMTP())
    with pytest.raises(UnsupportedDestination) as e:
        t.send("+15551234567", "hello", carrier=None)  # no carrier guess possible
    assert e.value.reason == "carrier_unknown"


class _RecordingSMTP:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendmail(self, frm, to, msg):
        if self.fail:
            raise smtplib.SMTPException("smtp refused")
        if isinstance(to, (list, tuple)):   # smtplib takes a list of recipients
            to = list(to)[0]
        self.sent.append((to, msg))

    def starttls(self, *a, **k):
        return None

    def login(self, *a, **k):
        return None
