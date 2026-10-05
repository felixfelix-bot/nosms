"""Offline tests for the link's *bookkeeping* (the XMPP body needs a live link).

The background loop and stanza exchange in :mod:`app.transports.jmp_link` are
`pragma: no cover` — they need a real server. Everything the rail's correctness
depends on *around* that loop is testable offline and is pinned here: the
not-connected guard, terminal-reason short-circuit, the sqlite inbox write, and
`from_env`.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.transports.errors import RailUnavailable
from app.transports.jmp_link import MAX_TERMINAL_RETRIES, SlixmppLink


def _link(tmp_path, **kw):
    return SlixmppLink("rail@example.test", "pw",
                       inbox_db=str(tmp_path / "inbox.db"), **kw)


def test_send_before_start_is_a_rail_unavailable_not_a_silent_drop(tmp_path):
    link = _link(tmp_path)
    assert link.is_connected() is False
    with pytest.raises(RailUnavailable) as excinfo:
        link.send_message("+15551230000@cheogram.com", "hi")
    assert excinfo.value.reason == "not_connected"


def test_terminal_reason_short_circuits_every_send(tmp_path):
    link = _link(tmp_path)
    link._set_disconnected("auth_failed")
    assert link.down_reason == "auth_failed"
    assert link.is_connected() is False
    with pytest.raises(RailUnavailable) as excinfo:
        link.send_message("+15551230000@cheogram.com", "hi")
    assert excinfo.value.reason == "auth_failed"


def test_transient_disconnect_does_not_set_a_terminal_reason(tmp_path):
    link = _link(tmp_path)
    link._set_disconnected("connection_lost")
    assert link.down_reason is None            # it may still reconnect


def test_send_timeout_raises_rail_unavailable_not_a_bare_timeout(tmp_path):
    """The caught class must be concurrent.futures.TimeoutError (distinct from the
    builtin before 3.11): a timeout that escaped would break the refund contract."""
    import concurrent.futures

    link = _link(tmp_path, send_timeout=0.01)
    link._ready.set()
    link._connected = True

    class _Outbox:
        def put_nowait(self, item):
            raise AssertionError("must never be reached: the loop is broken")

    class _Loop:
        def call_soon_threadsafe(self, fn, *a):
            # exactly what a closed/broken loop does inside fut.result()
            raise concurrent.futures.TimeoutError()

    link._loop = _Loop()
    link._outbox = _Outbox()

    with pytest.raises(RailUnavailable) as excinfo:
        link.send_message("+1" + "5551230000@cheogram.com", "hi")
    assert excinfo.value.reason == "connection_lost"


def test_supervise_stops_after_repeated_terminal_failures(tmp_path):
    """A terminal reason is sticky, so retrying forever is repeated auth attempts
    against the server. The loop must give up after MAX_TERMINAL_RETRIES."""
    import asyncio

    link = _link(tmp_path)
    cycles = []

    async def fake_cycle():
        cycles.append(1)
        link._terminal_reason = "auth_failed"

    link._cycle = fake_cycle
    link.reconnect_min = 0.0
    link.reconnect_max = 0.0
    asyncio.run(link._supervise())

    assert len(cycles) == MAX_TERMINAL_RETRIES
    assert link._terminal_exhausted is True
    assert link.is_connected() is False


def test_inbox_table_is_created_and_written(tmp_path):
    link = _link(tmp_path)
    link._log("in", "+15551230000@cheogram.com", "hello", raw_type="chat")
    link._log("out", "+15551230000@cheogram.com", "reply")

    with sqlite3.connect(tmp_path / "inbox.db") as con:
        rows = list(con.execute(
            "SELECT direction, peer, body FROM messages ORDER BY id"))
    assert rows == [("in", "+15551230000@cheogram.com", "hello"),
                    ("out", "+15551230000@cheogram.com", "reply")]


def test_inbox_is_optional(tmp_path):
    link = SlixmppLink("rail@example.test", "pw", inbox_db=None)
    link._log("in", "+15551230000@cheogram.com", "hello")   # must not raise


def test_close_without_a_thread_is_safe(tmp_path):
    _link(tmp_path).close()


def test_from_env_reads_the_runtime_credential_file(tmp_path, monkeypatch):
    cred = tmp_path / "cred.json"
    cred.write_text(json.dumps({"jid": "hermes-jmp@jabber.fr",
                                "password": "s3cret", "domain": "jabber.fr"}))
    cred.chmod(0o600)
    link = SlixmppLink.from_env({
        "JMP_JID": "ignored@example.test",
        "JMP_CRED_JSON": str(cred),
        "JMP_INBOX_DB": str(tmp_path / "inbox.db"),
        "NOSMS_JMP_RESOURCE": "rail-test",
    })
    assert link.jid == "hermes-jmp@jabber.fr"   # the file wins over the env JID
    assert link.password == "s3cret"
    assert link.resource == "rail-test"
    assert link.inbox_db.endswith("inbox.db")


# --- a timed-out send must NOT still go out later (double delivery) ----------
#
# The caller gives up on a timeout. If the future is left un-cancelled the queue
# entry survives, `_sender` later dequeues it, calls `msg.send()`, and completes
# the future — so the stanza goes out AFTER the rail already reported
# `accepted=False` and the failover re-sent over email. The recipient then gets
# BOTH. The timeout path must cancel the future so `_sender` skips the entry.

def _timed_out_entry(tmp_path):
    """Drive `send_message` into a real ack-timeout and return the queue entry."""
    link = _link(tmp_path, send_timeout=0.01)
    link._ready.set()
    link._connected = True
    link._outbox = asyncio.Queue()

    class _StuckLoop:
        """Accepts the queue put but never lets the future resolve (a link whose
        ack never arrives) — the exact shape of a send that timed out."""

        def call_soon_threadsafe(self, fn, *a):
            fn(*a)                             # put_nowait runs; nothing acks

    link._loop = _StuckLoop()
    with pytest.raises(RailUnavailable) as excinfo:
        link.send_message("+1" + "5551230000@cheogram.com", "hi")
    assert excinfo.value.reason == "connection_lost"
    return link, link._outbox.get_nowait()


def test_a_timed_out_send_leaves_a_cancelled_future(tmp_path):
    """Pin the mechanism: no cancel -> the entry stays transmittable."""
    _, entry = _timed_out_entry(tmp_path)
    fut = entry[3]
    assert fut.done() is True                  # the defect: was False before the fix
    assert fut.cancelled() is True


def test_a_timed_out_send_is_never_transmitted_by_the_sender(tmp_path):
    """`_sender` skips a cancelled entry, so the stanza never goes out late."""
    link, entry = _timed_out_entry(tmp_path)

    transmitted: list[str] = []

    class _Client:
        def is_connected(self):
            return True

        def Message(self):                     # noqa: N802 - slixmpp API
            class _M:
                def __setitem__(self, k, v):
                    pass

                def send(self_inner):
                    transmitted.append("sent")
            return _M()

    async def drive():
        queue = asyncio.Queue()
        queue.put_nowait(entry)
        link._outbox = queue
        task = asyncio.ensure_future(link._sender(_Client()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert transmitted == []                   # the stanza never went out


def test_an_accepted_send_still_transmits(tmp_path):
    """Non-vacuity control: with a live (un-cancelled) future the sender DOES
    transmit, so the test above is not passing because `_sender` is inert."""
    link = _link(tmp_path)
    transmitted: list[str] = []

    class _Client:
        def is_connected(self):
            return True

        def Message(self):                     # noqa: N802 - slixmpp API
            class _M:
                def __setitem__(self, k, v):
                    pass

                def send(self_inner):
                    transmitted.append("sent")
            return _M()

    async def drive():
        queue = asyncio.Queue()
        fut = asyncio.get_event_loop().create_future()
        queue.put_nowait(("+1" + "5551230000@cheogram.com", "hi", "m1", fut))
        link._outbox = queue
        task = asyncio.ensure_future(link._sender(_Client()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return fut

    fut = asyncio.run(drive())
    assert transmitted == ["sent"]
    assert fut.result() is True
