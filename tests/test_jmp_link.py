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
import sys
import threading
import time
import types
from concurrent.futures import Future

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


# --- the hand-off / acknowledgement invariant --------------------------------
#
# A deadline that expires is NOT evidence that the stanza did not go out. The
# hand-off to the stream and the caller's decision to cancel must therefore be
# mutually exclusive, leaving exactly two outcomes:
#
#   * the caller cancels first  -> `_sender` skips the entry, the stanza NEVER
#     goes out, and the caller may honestly answer a refundable
#     `connection_lost`;
#   * the hand-off goes first   -> the stanza IS on the wire and the caller must
#     be told the rail took it. Answering "not sent" there refunds a delivered
#     message *and* lets the failover re-send it over the email rail (the
#     double delivery of round-2/round-3 of the cold review).
#
# Neither outcome may end `_sender`: the round-3 review's F2 was the fix's own
# `cancel()` making `set_result` raise `InvalidStateError` and killing the task,
# leaving the rail mute while it still advertised itself as available.


class _DemoClient:
    """A fake slixmpp client: records hand-offs, can stall inside `send()`."""

    def __init__(self, *, block: bool = False, on_send=None):
        self.transmitted: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.on_send = on_send

    def is_connected(self):
        return True

    def Message(self):                         # noqa: N802 - slixmpp API
        client = self

        class _M:
            def __setitem__(self, k, v):
                pass

            def send(self_inner):
                client.transmitted.append("sent")
                client.entered.set()
                if client.on_send is not None:
                    client.on_send()           # the caller's race lands *here*
                if client.block:
                    client.release.wait(10)

        return _M()


class _RecordingQueue(asyncio.Queue):
    """The outbox, keeping every entry it was handed so a test can reach the
    future `send_message` built internally."""

    def __init__(self):
        super().__init__()
        self.seen: list = []

    def put_nowait(self, item):
        self.seen.append(item)
        super().put_nowait(item)


class _LinkDriver:
    """A real `_sender` coroutine on its own loop thread, driven by the caller
    thread exactly as the live link drives it.

    Only the XMPP client is faked; `send_message`, `_sender` and the sqlite
    inbox are the reviewed/live code, imported unmodified.
    """

    def __init__(self, tmp_path, client, *, send_timeout: float = 1.0):
        self.client = client
        self.link = SlixmppLink("rail@example.test", "pw",
                                inbox_db=str(tmp_path / "inbox.db"),
                                send_timeout=send_timeout)
        self.link._ready.set()
        self.link._connected = True
        self.loop = None
        self.task = None
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.link._loop = loop
        self.outbox = _RecordingQueue()
        self.link._outbox = self.outbox
        self.task = loop.create_task(self.link._sender(self.client))
        self._started.set()
        loop.run_forever()

    def start(self):
        self._thread.start()
        assert self._started.wait(5), "the sender loop never started"
        return self

    def close(self):
        loop = self.loop
        if loop is None or not loop.is_running():
            return

        async def _shutdown():
            if self.task is not None and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except BaseException:          # noqa: BLE001
                    pass
            loop.stop()

        asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        self._thread.join(timeout=5)

    # --- driving the link from the test thread ---------------------------

    def entry(self, msg_id: str = "m1", body: str = "hi"):
        """One queue entry, shaped exactly as `send_message` builds it."""
        return ("+1" + "5551230000" + "@cheogram.com", body, msg_id, Future())

    def put(self, entry) -> None:
        self.loop.call_soon_threadsafe(self.link._outbox.put_nowait, entry)

    def send(self, body: str = "hi"):
        """Call the real `send_message` from a worker thread.

        Returns `(thread, out)`; `out["r"]` is the caller-visible outcome —
        what `JmpCheogramTransport` (and therefore the escrow) is told. The
        future `send_message` built is `self.last_entry()[3]`.
        """
        out: dict[str, str] = {}

        def caller():
            try:
                self.link.send_message("+1" + "5551230000" + "@cheogram.com", body)
                out["r"] = "accepted"
            except RailUnavailable as exc:
                out["r"] = f"RailUnavailable({exc.reason})"
            except BaseException as exc:       # noqa: BLE001
                out["r"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=caller)
        thread.start()
        return thread, out

    def last_entry(self):
        """The entry the most recent `send_message` queued."""
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.outbox.seen:
                return self.outbox.seen[-1]
            time.sleep(0.01)
        raise AssertionError("send_message never queued an entry")

    def settled(self, thread, out, timeout: float = 10.0) -> str:
        thread.join(timeout=timeout)
        return out.get("r", "<caller never returned>")

    def sender_alive(self) -> bool:
        return self.task is not None and not self.task.done()


@pytest.fixture()
def drivers(tmp_path):
    """Build link drivers that are always unwound, even when a test fails."""
    made: list[_LinkDriver] = []

    def _make(client, **kw) -> _LinkDriver:
        drv = _LinkDriver(tmp_path, client, **kw).start()
        made.append(drv)
        return drv

    yield _make
    for drv in made:
        drv.close()


def test_a_deadline_inside_the_hand_off_is_not_reported_as_not_sent(drivers):
    """F1: the timeout lands while `_sender` is already handing the stanza over.

    The hand-off is atomic against the cancellation, so the only honest answer
    is "the rail took it" — never `connection_lost`, which would refund a
    message that went out and let the failover re-send it over email.
    """
    client = _DemoClient(block=True)
    drv = drivers(client, send_timeout=0.2)

    thread, out = drv.send()                   # blocks in fut.result(0.2)
    assert client.entered.wait(5), "the sender never reached the hand-off"
    time.sleep(0.4)                            # the caller's deadline expires
    client.release.set()                       # let the hand-off finish

    outcome = drv.settled(thread, out)
    assert outcome == "accepted", outcome
    assert client.transmitted == ["sent"]      # exactly one copy, never two
    assert drv.last_entry()[3].result() is True
    assert drv.sender_alive()


def test_a_deadline_that_cannot_recall_the_hand_off_is_never_not_sent(drivers):
    """The sender is wedged inside the hand-off past the caller's deadline.

    It cannot be recalled, and a best-effort rail with no receipts cannot prove
    the message did not go out, so "not sent" (refund + possible delivery) is
    the one answer that must never be given.
    """
    entered, release = threading.Event(), threading.Event()

    def wedged_is_connected():
        entered.set()
        release.wait(10)
        return True

    client = _DemoClient(block=True)
    client.is_connected = wedged_is_connected
    drv = drivers(client, send_timeout=0.2)

    thread, out = drv.send()
    assert entered.wait(5)
    outcome = drv.settled(thread, out, timeout=10)
    assert outcome == "accepted", outcome
    assert "RailUnavailable" not in outcome
    assert drv.sender_alive()
    release.set()


def test_a_deadline_while_the_sender_is_failing_reports_that_failure(drivers):
    """The future settled with the sender's own failure: the caller must be given
    that failure, not a second guess of its own."""

    def slow_failure():
        time.sleep(0.4)                        # past the caller's 0.2 s deadline
        return False

    client = _DemoClient()
    client.is_connected = slow_failure
    drv = drivers(client, send_timeout=0.2)

    thread, out = drv.send()
    outcome = drv.settled(thread, out)
    assert outcome == "RailUnavailable(connection_lost)", outcome
    assert client.transmitted == []
    time.sleep(0.5)                            # let the sender's failure land
    assert drv.sender_alive()


def test_a_settled_future_never_kills_the_sender(drivers):
    """F2: `set_result`/`set_exception` must never touch a settled future.

    Re-raising `InvalidStateError` out of `_sender` muted the rail while the
    capability surface still reported it available.
    """
    holder: dict = {}

    def cancel_mid_hand_off():
        holder["entry"][3].cancel()            # a cancel that lands too late

    client = _DemoClient(on_send=cancel_mid_hand_off)
    drv = drivers(client)
    entry = drv.entry()
    holder["entry"] = entry
    drv.put(entry)

    assert client.entered.wait(5)
    time.sleep(0.3)
    assert entry[3].cancelled() is True
    assert client.transmitted == ["sent"]
    assert drv.sender_alive(), "the sender died on a settled future (F2)"
    assert drv.link.is_connected() and drv.link.down_reason is None

    # the rail still carries traffic: the next send is acked, not muted
    nxt = drv.entry(msg_id="m2")
    drv.put(nxt)
    assert nxt[3].result(timeout=5) is True
    assert client.transmitted == ["sent", "sent"]


def test_a_broken_inbox_cannot_unaccept_a_transmitted_send(drivers):
    """The blocking sqlite write used to sit between `msg.send()` and the ack.

    A slow or failing log therefore turned a hand-off that HAD happened into a
    refundable `connection_lost` — the money direction of the whole defect. The
    ack must not wait behind the inbox write.
    """
    client = _DemoClient()
    drv = drivers(client)
    drv.link.inbox_db = str(drv.link.inbox_db) + "-missing-dir/inbox.db"
    entry = drv.entry()
    drv.put(entry)

    assert entry[3].result(timeout=5) is True    # acked although the log failed
    assert client.transmitted == ["sent"]
    assert drv.sender_alive()


# --- the supervised cycle must never leak a socket --------------------------

class _FakeSlixmppClient:
    """The slice of the slixmpp client surface `_cycle` touches."""

    def __init__(self, on_connect=None):
        self._handlers: dict = {}
        self._on_connect = on_connect
        self.disconnect_calls = 0
        self.boundjid = types.SimpleNamespace(resource="")

    def register_plugin(self, name):
        pass

    def add_event_handler(self, name, fn):
        self._handlers[name] = fn

    def connect(self):
        if self._on_connect is not None:
            self._on_connect(self._handlers)

    def disconnect(self):
        self.disconnect_calls += 1

    def is_connected(self):
        return True


@pytest.fixture()
def fake_slixmpp(monkeypatch):
    """`_cycle` imports slixmpp lazily, so stub the module to drive it offline."""
    module = types.ModuleType("slixmpp")
    module.ClientXMPP = _FakeSlixmppClient
    monkeypatch.setitem(sys.modules, "slixmpp", module)
    return module


def test_cycle_disconnects_after_a_failed_auth(tmp_path, fake_slixmpp):
    """Defect 2: a failed auth must not leave the socket open — the supervised
    retry would stack one leaked connection per attempt."""
    link = _link(tmp_path)
    client = _FakeSlixmppClient(on_connect=lambda h: h["failed_auth"](None))
    link._client_factory = lambda jid, pw: client

    asyncio.run(link._cycle())

    assert client.disconnect_calls == 1
    assert link._client is None
    assert link.down_reason == "auth_failed"
    assert link.is_connected() is False


def test_cycle_disconnects_when_the_connect_call_raises(tmp_path, fake_slixmpp):
    """The same leak class: an exception out of `connect()`/`wait_for` used to
    escape with a client that may hold a socket."""
    link = _link(tmp_path)

    def boom(_handlers):
        raise RuntimeError("tcp blew up")

    client = _FakeSlixmppClient(on_connect=boom)
    link._client_factory = lambda jid, pw: client

    with pytest.raises(RuntimeError):
        asyncio.run(link._cycle())

    assert client.disconnect_calls == 1
    assert link._client is None


def test_cycle_disconnects_when_the_connect_times_out(tmp_path, fake_slixmpp):
    link = _link(tmp_path, connect_timeout=0.05)
    client = _FakeSlixmppClient()              # connect() resolves nothing
    link._client_factory = lambda jid, pw: client

    asyncio.run(link._cycle())

    assert client.disconnect_calls == 1
    assert link.is_connected() is False
    assert link.down_reason is None            # a timeout is transient, not terminal
