"""Tests for the ADR-0002 degrade path: JMP down -> email rail, refunds honest.

The wrapper's contract is about **refundability**, not routing: the escrow layer
(CEP-8 explicit_gating) refunds when `SendResult.accepted` is False, so this
wrapper must keep that flag meaningful:

  * primary accepted                        -> accepted, refundable=False
  * primary down, fallback accepted         -> accepted, refundable=False
  * both fail / fallback cannot serve       -> accepted=False, refundable=True
  * a `RailPaced` primary                   -> propagates (defer, never refund)
  * a genuine per-send failure (empty body)  -> returned as-is, no degrade
"""
from __future__ import annotations

import smtplib

import pytest

from app.transports.base import SendResult, Transport
from app.transports.email_gateway import EmailGatewayTransport
from app.transports.errors import RailPaced, RailUnavailable, UnsupportedDestination
from app.transports.failover import FailoverTransport
from app.transports.jmp_cheogram import JmpCheogramTransport

US = "+15551230000"          # fake +1 number, never dialed


class FakeLink:
    def __init__(self, connected=True, fail=None):
        self.connected = connected
        self.fail = fail
        self.sent = []

    def is_connected(self):
        return self.connected

    def send_message(self, to_jid, body, msg_id=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append((to_jid, body, msg_id))


class RecordingSMTP:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendmail(self, frm, to, msg):
        if self.fail:
            raise smtplib.SMTPException("smtp refused")
        if isinstance(to, (list, tuple)):
            to = list(to)[0]
        self.sent.append((to, msg))


def _email(smtp):
    return EmailGatewayTransport(smtp_factory=lambda *a, **k: smtp)


def _stack(link=None, smtp=None, **kw):
    jmp = JmpCheogramTransport(link or FakeLink(), **kw)
    return FailoverTransport(jmp, _email(smtp or RecordingSMTP()),
                             overrides={"carrier": "tmobile"}), jmp


# --- happy path ---------------------------------------------------------------

def test_primary_accepted_is_returned_untouched():
    stack, jmp = _stack()
    res = stack.send(US, "hello")
    assert res.accepted is True
    assert res.rail == "jmp_cheogram"
    assert res.refundable is False
    assert stack.degraded_count == 0
    assert stack.active_rail == "jmp_cheogram"


def test_stack_is_a_transport():
    stack, _ = _stack()
    assert isinstance(stack, Transport)
    assert stack.name == "failover:jmp_cheogram->email_gateway"


# --- degrade -----------------------------------------------------------------

def test_termination_degrades_the_same_send_to_email():
    smtp = RecordingSMTP()
    link = FakeLink(fail=RailUnavailable("terminated", "account closed"))
    stack, jmp = _stack(link=link, smtp=smtp)

    res = stack.send(US, "hello")
    assert res.accepted is True
    assert res.rail == "email_gateway"           # actually sent, on the fallback
    assert res.refundable is False
    assert res.receipt is None                   # email rail has none either
    assert stack.degraded_count == 1
    assert jmp.down_reason == "terminated"
    assert smtp.sent and smtp.sent[0][0].endswith("@tmomail.net")


def test_subsequent_sends_go_straight_to_the_fallback():
    smtp = RecordingSMTP()
    stack, jmp = _stack(link=FakeLink(fail=RailUnavailable("auth_failed")),
                        smtp=smtp)
    stack.send(US, "one")
    jmp.mark_down("auth_failed")
    stack.send(US, "two")
    assert len(smtp.sent) == 2                   # both went out on email


def test_capabilities_follow_the_serving_rail():
    stack, jmp = _stack(link=FakeLink(fail=RailUnavailable("terminated")))
    stack.send(US, "hello")
    flags = stack.capability_flags()
    assert flags["serving_rail"] == "email_gateway"
    assert flags["primary_down"] is True
    assert flags["countries"] == ["US", "CA"]
    assert flags["delivery_receipts"] is False
    assert stack.capabilities.available is True   # email is still serving


# --- refundability ------------------------------------------------------------

def test_both_rails_failing_is_refundable():
    stack, _ = _stack(link=FakeLink(fail=RailUnavailable("terminated")),
                      smtp=RecordingSMTP(fail=True))
    res = stack.send(US, "hello")
    assert res.accepted is False
    assert res.refundable is True                  # escrow refunds on this
    assert res.detail.startswith("all_rails_failed")


def test_fallback_that_cannot_serve_the_destination_is_refundable():
    # A +1 destination with no known carrier: the email rail cannot address it.
    stack, _ = _stack(link=FakeLink(fail=RailUnavailable("terminated")))
    res = stack.send(US, "hello", carrier=None)    # overrides win? no: kw wins
    assert res.accepted is False
    assert res.refundable is True
    assert res.detail == "degraded_unsupported:carrier_unknown"


def test_non_us_ca_is_a_request_error_and_is_never_re_routed():
    smtp = RecordingSMTP()
    stack, _ = _stack(smtp=smtp)
    with pytest.raises(UnsupportedDestination) as excinfo:
        stack.send("+4915112345678", "hello")
    assert excinfo.value.reason == "destination_unsupported"
    assert smtp.sent == []                          # never quietly re-routed


# --- pacing and per-send failures must not degrade ----------------------------

def test_a_paced_primary_defers_instead_of_degrading():
    class PacedPrimary:
        name = "jmp_cheogram"

        @property
        def capabilities(self):
            from app.transports.base import Capabilities
            return Capabilities(available=True, best_effort=True,
                                delivery_receipts=False, countries=["US", "CA"])

        def send(self, dest, body, **kw):
            raise RailPaced("daily_cap_reached", 1234.0)

    smtp = RecordingSMTP()
    stack = FailoverTransport(PacedPrimary(),
                              _email(smtp), overrides={"carrier": "tmobile"})
    with pytest.raises(RailPaced) as excinfo:
        stack.send(US, "hello")
    assert excinfo.value.retry_after == 1234
    assert smtp.sent == []                          # pacing is not routed around


def test_empty_body_failure_is_not_degraded():
    smtp = RecordingSMTP()
    stack, _ = _stack(smtp=smtp)
    res = stack.send(US, "")
    assert res.accepted is False
    assert res.rail == "jmp_cheogram"               # the primary's own refusal
    assert smtp.sent == []
