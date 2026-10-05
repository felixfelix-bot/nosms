"""RED-first tests for the JMP/Cheogram rail (ADR-0002).

Behaviour pinned here:
- capability flags come **from the rail** and are honest by construction:
  best_effort=True, delivery_receipts=False, countries=["US", "CA"];
- a send is a **cold** chat message to ``+<E164>@cheogram.com`` — not a reply;
- the rail never returns a receipt and never claims delivery;
- a non-+1 destination is refused with the machine token
  ``destination_unsupported`` before the link is touched;
- an auth failure / termination is reported as ``rail_unavailable:<reason>``
  (a refund signal) and the rail goes down for good;
- pacing defers work without spending it (`RailPaced`, never a failed send).

All offline: the XMPP link is a double, no network, no slixmpp.
"""
from __future__ import annotations

import pytest

from app.transports.base import SendResult, Transport
from app.transports.errors import RailPaced, RailUnavailable, UnsupportedDestination
from app.transports.jmp_cheogram import (
    RAIL_UNAVAILABLE_PREFIX,
    JmpCheogramTransport,
    is_rail_down,
)
from app.transports.pacing import Pacer, PacingPolicy

US = "+15551230000"          # obviously fake, +1 / 12 chars


class FakeLink:
    """Records what the rail asked the link to send."""

    def __init__(self, connected: bool = True, fail: RailUnavailable | None = None):
        self.connected = connected
        self.fail = fail
        self.sent: list[tuple[str, str, str | None]] = []

    def is_connected(self) -> bool:
        return self.connected

    def send_message(self, to_jid, body, msg_id=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append((to_jid, body, msg_id))


# --- capability honesty ------------------------------------------------------

def test_rail_implements_transport_protocol():
    rail = JmpCheogramTransport(FakeLink())
    assert isinstance(rail, Transport)
    assert rail.name == "jmp_cheogram"


def test_capability_flags_are_served_from_the_rail():
    rail = JmpCheogramTransport(FakeLink())
    caps = rail.capabilities
    assert caps.available is True
    assert caps.best_effort is True            # permanent on this rail
    assert caps.delivery_receipts is False     # no receipts, ever (ADR-0002)
    assert caps.countries == ["US", "CA"]

    flags = rail.capability_flags()
    assert flags["rail"] == "jmp_cheogram"
    assert flags["best_effort"] is True
    assert flags["delivery_receipts"] is False
    assert flags["countries"] == ["US", "CA"]


def test_capability_available_follows_the_link_not_a_constant():
    link = FakeLink(connected=False)
    rail = JmpCheogramTransport(link)
    assert rail.capabilities.available is False
    link.connected = True
    assert rail.capabilities.available is True


# --- the cold send -----------------------------------------------------------

def test_send_is_a_cold_chat_message_to_the_cheogram_jid():
    link = FakeLink()
    rail = JmpCheogramTransport(link)
    res = rail.send(US, "hello from a service")

    assert res.accepted is True
    assert res.rail == "jmp_cheogram"
    assert res.best_effort is True
    assert res.receipt is None                 # never a receipt
    assert res.refundable is False
    (to_jid, body, msg_id), = link.sent
    assert to_jid == f"{US}@cheogram.com"      # cold address, built from E.164
    assert body == "hello from a service"
    assert msg_id                          # a fresh id, i.e. not a reply


def test_send_normalises_a_pretty_printed_number():
    link = FakeLink()
    JmpCheogramTransport(link).send("+1 (555) 123-0000", "hi")
    assert link.sent[0][0] == "+15551230000@cheogram.com"


@pytest.mark.parametrize("dest", ["+4915112345678", "+911234567890", "0049151"])
def test_non_us_ca_is_refused_before_the_link_is_touched(dest):
    link = FakeLink()
    rail = JmpCheogramTransport(link)
    with pytest.raises(UnsupportedDestination) as excinfo:
        rail.send(dest, "hello")
    assert excinfo.value.reason == "destination_unsupported"
    assert link.sent == []


def test_empty_body_is_a_rejected_send_not_an_exception():
    link = FakeLink()
    res = JmpCheogramTransport(link).send(US, "")
    assert res.accepted is False
    assert res.refundable is True
    assert link.sent == []


# --- rail-down / degrade signals ---------------------------------------------

@pytest.mark.parametrize("reason", ["auth_failed", "terminated"])
def test_terminal_link_failure_marks_the_rail_down_and_is_refundable(reason):
    link = FakeLink(fail=RailUnavailable(reason, "simulated"))
    rail = JmpCheogramTransport(link)

    res = rail.send(US, "hello")
    assert res.accepted is False
    assert res.refundable is True
    assert res.detail == f"{RAIL_UNAVAILABLE_PREFIX}{reason}"
    assert is_rail_down(res) is True

    # the rail stays down: no further attempt reaches the link
    assert rail.capabilities.available is False
    assert rail.down_reason == reason
    assert rail.send(US, "again").detail == f"{RAIL_UNAVAILABLE_PREFIX}{reason}"
    assert link.sent == []


def test_transient_failure_is_reported_but_does_not_terminate_the_rail():
    link = FakeLink(fail=RailUnavailable("not_connected", "reconnecting"))
    rail = JmpCheogramTransport(link)
    res = rail.send(US, "hello")
    assert res.detail == f"{RAIL_UNAVAILABLE_PREFIX}not_connected"
    assert rail.down_reason is None            # transient: no permanent mark
    assert rail.capabilities.available is True


def test_mark_down_supports_an_operator_kill_switch():
    rail = JmpCheogramTransport(FakeLink())
    rail.mark_down("terminated")
    assert rail.capabilities.available is False
    assert rail.send(US, "hello").accepted is False


# --- pacing is a defer, not a failure ----------------------------------------

class _FixedRng:
    def __init__(self, value: float):
        self.value = value
        self.drawn = 0

    def uniform(self, a: float, b: float) -> float:
        self.drawn += 1
        return min(max(self.value, a), b)


def test_pacing_defers_without_spending_the_send_or_the_link():
    clock = {"t": 1_000_000.0}
    pacer = Pacer(PacingPolicy(daily_cap=5, min_gap_seconds=60, max_gap_seconds=60),
                  now=lambda: clock["t"], rng=_FixedRng(60.0))
    link = FakeLink()
    rail = JmpCheogramTransport(link, pacer=pacer)

    assert rail.send(US, "first").accepted is True
    assert len(link.sent) == 1

    with pytest.raises(RailPaced) as excinfo:
        rail.send(US, "too soon")
    assert excinfo.value.reason == "min_gap"
    assert excinfo.value.retry_after == 60
    assert len(link.sent) == 1                 # the link was never called
    assert pacer.snapshot()["state"]["count"] == 1   # the day's count is untouched

    clock["t"] += 60.0
    assert rail.send(US, "later").accepted is True
    assert len(link.sent) == 2


def test_pacing_counts_only_accepted_sends():
    clock = {"t": 0.0}
    pacer = Pacer(PacingPolicy(daily_cap=5, min_gap_seconds=0, max_gap_seconds=0),
                  now=lambda: clock["t"], rng=_FixedRng(0.0))
    link = FakeLink(fail=RailUnavailable("connection_lost"))
    rail = JmpCheogramTransport(link, pacer=pacer)
    assert rail.send(US, "nope").accepted is False
    assert pacer.snapshot()["state"]["count"] == 0


# --- the result surface stays compatible -------------------------------------

def test_send_result_is_still_the_shared_contract():
    res = JmpCheogramTransport(FakeLink()).send(US, "hi")
    assert isinstance(res, SendResult)
    assert (res.accepted, res.rail, res.best_effort, res.receipt) == (
        True, "jmp_cheogram", True, None)


# --- the factory is the only place a rail is chosen --------------------------

def test_build_transport_unknown_name_is_loud():
    from app.transports import build_transport
    with pytest.raises(ValueError):
        build_transport("carrier-pigeon")


def test_build_transport_fake_is_the_default():
    from app.transports import build_transport
    from app.transports.fake import FakeTransport
    assert isinstance(build_transport(env={}), FakeTransport)


def test_build_transport_email_uses_env():
    from app.transports import build_transport
    t = build_transport(env={"NOSMS_TRANSPORT": "email_gateway",
                             "NOSMS_SMTP_SENDER": "sms@example.test"})
    assert t.name == "email_gateway"
    assert t.sender == "sms@example.test"


def test_build_transport_jmp_only_and_degrading(tmp_path):
    from app.transports import build_transport
    from app.transports.failover import FailoverTransport
    env = {"NOSMS_TRANSPORT": "jmp_only", "NOSMS_JMP_DAILY_CAP": "2",
           "NOSMS_JMP_PACING_STATE": str(tmp_path / "pace.json")}
    solo = build_transport(env=env, link=FakeLink())
    assert isinstance(solo, JmpCheogramTransport)
    assert solo.pacer.policy.daily_cap == 2
    assert solo.capability_flags()["countries"] == ["US", "CA"]

    stacked = build_transport(env={**env, "NOSMS_TRANSPORT": "jmp_cheogram"},
                              link=FakeLink())
    assert isinstance(stacked, FailoverTransport)
    assert stacked.active_rail == "jmp_cheogram"

