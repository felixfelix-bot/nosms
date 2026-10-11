"""RED-first: the server must START the rail before it serves a paid tool.

The live bug this pins (2026-10-11, cobrador): ``scripts/run_cvm_server.py``
built the JMP/Cheogram rail and **never started its link**, so
:attr:`JmpCheogramTransport.capabilities.available` was ``False`` for the whole
process lifetime. The free tools all answered — the CEP-6 announcements
published, ``sms.capabilities`` / ``sms.pricing`` / ``docs`` replied — and
``sms.send`` refused ``rail_not_configured`` on **every** call, before the CEP-8
payment gate in :meth:`CvmTools.sms_send` was ever reached. A paid tool, dead on
arrival, disguised as a payment problem (and it never touched the rail, so the
refusal cost nothing to look real).

The seam is ONE explicit lifecycle on the ``Transport`` protocol — ``start()``
and ``close()`` — with stateless rails implementing them as a no-op. The
alternatives were worse:

* ``build_transport`` starting the link would make a pure factory block for up
  to 45 s on a live XMPP session — paid by every offline caller (the whole test
  suite, probes, the HTTP path) — and would start the link twice in
  ``scripts/jmp_rail_smoke.py``, which starts its own link.
* learning it from ``capabilities`` is what the bug already did.

These tests drive the server's own lifecycle helpers, so the assertion is about
the path ``main()`` runs, not a hand-rolled stand-in.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.cvm import ContractContext                               # noqa: E402
from app.cvm_tools import CvmTools                                # noqa: E402
from app.escrow import EscrowStore                                # noqa: E402
from app.pricing import DEFAULT_PRICE_SATS                        # noqa: E402
from app.transports import (                                      # noqa: E402
    EmailGatewayTransport,
    FailoverTransport,
    FakeTransport,
    JmpCheogramTransport,
    Transport,
)
import scripts.run_cvm_server as runner                           # noqa: E402

#: obviously fake, +1 / 12 chars (same fixture the other suites use)
US = "+155****0100"
CALLER = "ab" * 32


class StubLink:
    """A link with the SHAPE OF THE REAL ONE: not connected until started.

    ``SlixmppLink.is_connected()`` is ``False`` until ``start()`` has connected
    the client (app/transports/jmp_link.py) — which is exactly the call the live
    server never made.
    """

    def __init__(self, connects: bool = True, down_reason: str | None = None):
        self.connects = connects
        self.down_reason = down_reason
        self.start_calls: list[float | None] = []
        self.closed = False
        self._started = False
        self.sent: list[tuple[str, str, str | None]] = []

    def start(self, timeout: float | None = None) -> bool:
        self.start_calls.append(timeout)
        self._started = self.connects
        return self.connects

    def close(self) -> None:
        self.closed = True
        self._started = False

    def connect(self) -> None:
        """The link reconnected on its own (SlixmppLink retries with backoff)."""
        self._started = True

    def is_connected(self) -> bool:
        return self._started and not self.closed

    def send_message(self, to_jid, body, msg_id=None):
        self.sent.append((to_jid, body, msg_id))


def jmp_rail(monkeypatch, tmp_path, link=None):
    """Build the JMP rail THE WAY THE SERVER DOES, with the live stack stubbed."""
    from app.transports.jmp_link import SlixmppLink

    link = link if link is not None else StubLink()
    monkeypatch.setattr(SlixmppLink, "from_env",
                        classmethod(lambda cls, env=None, **kw: link))
    monkeypatch.setenv("NOSMS_JMP_PACING_STATE", str(tmp_path / "pacing.json"))
    return runner.build_transport("jmp"), link


def cvm(transport) -> CvmTools:
    return CvmTools(transport, docs={"llms": "contract", "llms-full": "manual"},
                    escrow=EscrowStore(":memory:"),
                    contract=ContractContext(npub="npub1testserver"))


def body_of(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# --- the server starts the link ---------------------------------------------

def test_the_servers_start_path_brings_the_link_up(monkeypatch, tmp_path):
    transport, link = jmp_rail(monkeypatch, tmp_path)
    assert transport.capabilities.available is False      # nothing started it yet

    assert runner.start_transport(transport, timeout=5.0) is True
    assert link.start_calls == [5.0]                      # the timeout is honoured
    assert link.is_connected()
    assert transport.capabilities.available is True       # the rail is serving


def test_the_lifecycle_closes_the_rail_on_the_way_out(monkeypatch, tmp_path):
    """A leaked session is a second device on a personal line: close on exit."""
    transport, link = jmp_rail(monkeypatch, tmp_path)
    with runner.started_transport(transport, timeout=5.0):
        assert link.is_connected()
    assert link.closed
    assert transport.capabilities.available is False


# --- the bug: an unpaid send must reach the PAYMENT gate --------------------

def test_a_started_rail_makes_an_unpaid_send_reach_the_payment_gate(monkeypatch, tmp_path):
    """The observed live failure, inverted.

    Live today: `sms.send` (unpaid) -> `rail_not_configured`, on every call.
    Here the rail is up, so the only thing standing between the caller and the
    message is postage — and that is a `payment_required`, with the cap.
    """
    transport, link = jmp_rail(monkeypatch, tmp_path)
    runner.start_transport(transport, timeout=5.0)
    tools = cvm(transport)

    assert body_of(tools.call("sms.capabilities"))["available"] is True

    result = tools.call("sms.send", {"to": US, "body": "hi"}, caller=CALLER)
    body = body_of(result)
    assert result["isError"] is True
    assert body["reason"] == "payment_required", body
    assert body["cap"] == f"cap:tool:sms.send:{DEFAULT_PRICE_SATS}:sats"
    assert body["gating"] == "explicit_gating"
    assert link.sent == []                    # and nothing left the process


# --- honest flags are NOT regressed -----------------------------------------

def test_a_link_that_never_connects_is_rail_unavailable_not_unconfigured(monkeypatch, tmp_path):
    """A configured rail that is down is NOT a rail that is not configured.

    `rail_not_configured` means "this service has no rail behind the
    capability" (e.g. no mint, an unconfigured Telnyx). A JMP rail whose link
    will not come up is *configured and down* — the token for that already
    exists (`rail_unavailable:`), and it must never be `payment_required`
    (paying cannot help) nor silence.
    """
    transport, link = jmp_rail(monkeypatch, tmp_path, StubLink(connects=False))

    assert runner.start_transport(transport, timeout=5.0) is False
    tools = cvm(transport)
    assert body_of(tools.call("sms.capabilities"))["available"] is False

    result = tools.call("sms.send", {"to": US, "body": "hi"}, caller=CALLER)
    body = body_of(result)
    assert result["isError"] is True
    assert body["reason"] == "rail_unavailable", body
    assert body["hint"] == "rail_unavailable:not_connected"
    assert link.sent == []


def test_a_terminal_refusal_carries_the_links_own_reason(monkeypatch, tmp_path):
    link = StubLink(connects=False, down_reason="auth_failed")
    transport, _ = jmp_rail(monkeypatch, tmp_path, link)
    runner.start_transport(transport, timeout=5.0)

    body = body_of(cvm(transport).call("sms.send", {"to": US, "body": "hi"},
                                       caller=CALLER))
    assert body["reason"] == "rail_unavailable"
    assert body["hint"] == "rail_unavailable:auth_failed"


def test_not_connected_is_live_and_clears_when_the_link_comes_back(monkeypatch, tmp_path):
    """A reconnect window must not latch the rail off.

    The degrade path branches on the *latched* ``down_reason`` (FailoverTransport
    must not divert paid traffic away from a rail that is coming back), so the
    live "why can't you serve?" answer is a separate, self-clearing read.
    """
    transport, link = jmp_rail(monkeypatch, tmp_path, StubLink(connects=False))
    runner.start_transport(transport, timeout=0.1)

    assert transport.down_reason is None          # not latched
    assert transport.unavailable_reason == "not_connected"
    assert body_of(cvm(transport).call("sms.send", {"to": US, "body": "hi"},
                                       caller=CALLER))["reason"] == "rail_unavailable"

    link.connect()                                # the link recovered on its own
    assert transport.capabilities.available is True
    assert transport.unavailable_reason is None
    assert body_of(cvm(transport).call("sms.send", {"to": US, "body": "hi"},
                                       caller=CALLER))["reason"] == "payment_required"


# --- the seam itself ---------------------------------------------------------

@pytest.mark.parametrize("rail", [FakeTransport(), EmailGatewayTransport()])
def test_stateless_rails_implement_the_lifecycle_as_a_no_op(rail):
    assert isinstance(rail, Transport)            # the protocol carries start/close
    assert rail.start(timeout=0.01) is True       # nothing to connect
    rail.close()                                  # never raises


def test_the_degrade_stack_starts_and_closes_its_primary():
    link = StubLink()
    stack = FailoverTransport(JmpCheogramTransport(link), EmailGatewayTransport())
    assert isinstance(stack, Transport)

    assert stack.start(timeout=1.0) is True
    assert link.start_calls == [1.0]              # delegated to the primary
    stack.close()
    assert link.closed


def test_an_unconfigured_rail_is_rail_not_configured():
    """The other half of the distinction: nothing wired => not configured.

    An unconfigured Telnyx has no link and no down token, so inventing
    `rail_unavailable` for it would be as wrong as calling a downed JMP line
    "unconfigured".
    """
    from app.transports import TelnyxTransport

    t = TelnyxTransport(provider=None, api_key="", from_number="")
    tools = cvm(t)
    assert body_of(tools.call("sms.capabilities"))["available"] is False
    body = body_of(tools.call("sms.send", {"to": US, "body": "hi"}, caller=CALLER))
    assert body["reason"] == "rail_not_configured", body


# --- review round 1 (cold cross-family review of PR #12) ----------------------
#
# Three findings, all in code THIS change introduced. Each is the same mistake
# in a different place: a promise the docstring makes, that the code breaks as
# soon as the rail RAISES instead of politely answering.

class RaisingLink(StubLink):
    """A link whose ``start()`` RAISES — what a real XMPP connect can do.

    ``StubLink`` answers ``False``; the live stack can also raise (DNS failure,
    a cancelled connect, slixmpp blowing up inside its own session). The
    lifecycle promises to treat both the same way: rail trouble is not fatal.
    """

    def __init__(self, exc: Exception | None = None, close_raises: bool = False):
        super().__init__(connects=False)
        self.exc = exc if exc is not None else RuntimeError("xmpp connect blew up")
        self.close_raises = close_raises

    def start(self, timeout: float | None = None) -> bool:
        self.start_calls.append(timeout)
        raise self.exc

    def close(self) -> None:
        self.closed = True
        if self.close_raises:
            raise RuntimeError("close blew up")


def test_a_raising_start_is_not_fatal_to_the_free_surface(monkeypatch, tmp_path):
    """`base.Transport` promises rail failure is NOT fatal — a raise must obey it.

    Before this round, ``transport.start()`` was called with no guard: a raising
    start escaped ``start_transport`` -> ``started_transport`` -> ``main()`` and
    killed the process, taking the FREE tools (docs / pricing / capabilities)
    down with it. The documented contract is the opposite: the free surface
    keeps answering and ``available`` stays false.
    """
    transport, _ = jmp_rail(monkeypatch, tmp_path, RaisingLink())

    assert runner.start_transport(transport, timeout=5.0) is False   # not a raise
    tools = cvm(transport)
    assert body_of(tools.call("sms.capabilities"))["available"] is False
    assert body_of(tools.call("sms.pricing"))["price"] == DEFAULT_PRICE_SATS
    assert body_of(tools.call("sms.send", {"to": US, "body": "hi"},
                              caller=CALLER))["reason"] == "rail_unavailable"


def test_the_lifecycle_closes_even_when_start_raises(monkeypatch, tmp_path):
    """A leaked XMPP session is a second device on a personal line.

    ``started_transport`` says ``close()`` is on EVERY exit path; a failing
    start is an exit path. Leaking the session is the exact harm this lifecycle
    exists to prevent.
    """
    link = RaisingLink()
    transport, _ = jmp_rail(monkeypatch, tmp_path, link)

    with runner.started_transport(transport, timeout=5.0):
        pass
    assert link.closed


def test_the_jmp_rail_close_never_raises_even_when_the_link_does():
    """`base.Transport.close` is documented idempotent AND never-raising.

    ``close()`` runs in a ``finally:``. A raise there REPLACES whatever
    exception was already in flight — so the failure you actually needed to see
    is the one you lose.
    """
    link = RaisingLink(close_raises=True)
    transport = JmpCheogramTransport(link)

    transport.close()                              # must not raise
    assert link.closed


def test_the_degrade_stack_reports_ready_when_only_the_fallback_can_serve():
    """A stack whose primary is down but whose fallback can serve IS ready.

    ``FailoverTransport.capabilities`` already falls back to the fallback's
    flags (``available: True``), so a ``start()`` that answered false here
    contradicted the stack's own capability report — and printed a spurious
    RAIL DOWN for a service that was working.
    """
    primary = JmpCheogramTransport(StubLink(connects=False))
    stack = FailoverTransport(primary, EmailGatewayTransport())

    assert stack.capabilities.available is True
    assert stack.start(timeout=1.0) is True


def test_the_degrade_stack_closes_the_fallback_even_if_the_primary_raises():
    """Both rails own their cleanup; one blowing up must not strand the other."""
    class CloseRaisingLink(StubLink):
        def close(self):
            self.closed = True
            raise RuntimeError("primary close blew up")

    primary_link = CloseRaisingLink()
    stack = FailoverTransport(JmpCheogramTransport(primary_link), EmailGatewayTransport())

    stack.close()                                  # must not raise
    assert primary_link.closed


# --- review round 2 (delta re-review of the round-1 fix) ----------------------
#
# The round-1 fix for `FailoverTransport.start()` introduced a REGRESSION: it
# reordered `primary.start()` ahead of `fallback.start()` without guarding
# either call. The base code called the fallback first, so a primary that RAISED
# still had the fallback started; after the reorder the raise skipped the
# fallback entirely and escaped to the caller. The module's own docstring states
# the rule this breaks: "A primary that raises must degrade exactly like one that
# returns" (that is why the WhatsApp rail can be wrapped at all).


class RecordingEmail(EmailGatewayTransport):
    """The real fallback, made observable: records that its start was reached."""

    def __init__(self):
        super().__init__()
        self.start_calls: list[float | None] = []

    def start(self, timeout: float | None = None) -> bool:
        self.start_calls.append(timeout)
        return super().start(timeout=timeout)


def test_the_degrade_stack_still_starts_the_fallback_when_the_primary_raises():
    """A RAISING primary must degrade exactly like one that returns False.

    Otherwise wrapping a rail whose outage is *loud* (the WhatsApp rail raises
    ``RailUnavailable`` on a ban) turns a recoverable rail outage into an
    unhandled exception -- and, after the round-1 reorder, silently skips the
    fallback that was the whole point of the stack.
    """
    fallback = RecordingEmail()
    stack = FailoverTransport(JmpCheogramTransport(RaisingLink()), fallback)

    assert stack.start(timeout=1.0) is True        # fallback can serve
    assert fallback.start_calls == [1.0]           # ...and it was actually started


def test_the_degrade_stack_answers_false_when_no_rail_can_start():
    """Both rails raising is a plain not-ready, never an escaping exception."""

    class RaisingEmail(EmailGatewayTransport):
        def start(self, timeout: float | None = None) -> bool:
            raise RuntimeError("smtp connect blew up: timeout=" + repr(timeout))

    stack = FailoverTransport(JmpCheogramTransport(RaisingLink()), RaisingEmail())

    assert stack.start(timeout=1.0) is False
