"""Offline tests for the link's *bookkeeping* (the XMPP body needs a live link).

The background loop and stanza exchange in :mod:`app.transports.jmp_link` are
`pragma: no cover` — they need a real server. Everything the rail's correctness
depends on *around* that loop is testable offline and is pinned here: the
not-connected guard, terminal-reason short-circuit, the sqlite inbox write, and
`from_env`.
"""
from __future__ import annotations

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
