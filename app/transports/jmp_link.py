"""Long-lived in-process JMP/Cheogram XMPP link (the rail's connection model).

Mirrors the model proven by the fleet listener
(``~/.hermes/profiles/manager/scripts/jmp-sms-listener.py``): **one** long-lived
client, its own asyncio loop on a daemon thread, inbound SMS written to sqlite,
reconnect with exponential backoff, and the non-obvious connect pattern
(``xmpp.connect()`` is *not* awaited; wait on ``session_start`` with a timeout).

The difference from a listener is what it is for: this link **serves a
service**. It exposes a blocking :meth:`SlixmppLink.send_message` that the
:class:`~app.transports.jmp_cheogram.JmpCheogramTransport` calls, and it reports
rail-down conditions with tokens the degrade path branches on
(``auth_failed`` / ``terminated`` / ``not_connected`` / ``connection_lost``).

Keeping the link in-process (rather than a second daemon) satisfies the card's
"one process, not two": the process serving the CVM tools is the process holding
the XMPP session.

Notes
-----
* Inbound SMS is one-shot: it exists only while a client is attached. The link
  therefore stays attached; there is no poll.
* The destination/sender number is never printed here — the raw peer is written
  straight to the sqlite column (Hermes-style stdout redaction would destroy it
  if it ever passed through a log line).
* `slixmpp` is imported lazily so importing this module needs no XMPP stack.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError

from .errors import RailUnavailable

__all__ = ["SlixmppLink"]

DEFAULT_JID = "hermes-jmp@jabber.fr"
DEFAULT_CRED = "~/.xmpp-hermes-jmp@jabber.fr.json"
DEFAULT_INBOX = "~/.hermes/profiles/manager/state/jmp_inbox.db"

#: SASL/auth failures that mean "this account will never work again as-is".
TERMINAL_REASONS = ("auth_failed", "terminated")

#: A terminal reason is retried at most this many times before the supervise loop
#: stops for good: a wrong credential will not fix itself, and hammering the
#: server with re-auth attempts on a personal line is itself abusive-looking.
MAX_TERMINAL_RETRIES = 3


class SlixmppLink:
    """One long-lived XMPP client, owned by this process."""

    def __init__(self, jid: str, password: str, *, inbox_db: str | None = DEFAULT_INBOX,
                 resource: str = "nosms", connect_timeout: float = 45.0,
                 reconnect_min: float = 5.0, reconnect_max: float = 300.0,
                 send_timeout: float = 30.0, reply: bool = False,
                 reply_text: str | None = None, client_factory=None):
        self.jid = jid
        self.password = password
        self.inbox_db = os.path.expanduser(inbox_db) if inbox_db else None
        self.resource = resource
        self.connect_timeout = connect_timeout
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.send_timeout = send_timeout
        self.reply = reply
        self.reply_text = reply_text or "This line is operated by a nosms rail."
        self._client_factory = client_factory

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: asyncio.Queue | None = None
        self._client = None
        self._sender_task = None
        self._ready = threading.Event()
        self._connected = False
        self._terminal_reason: str | None = None
        self._terminal_exhausted = False
        self._stopping = False
        self._dump_lock = threading.Lock()
        if self.inbox_db:
            self._init_inbox()

    # --- construction helpers --------------------------------------------

    @classmethod
    def from_env(cls, env: dict | None = None, **kwargs) -> "SlixmppLink":
        e = os.environ if env is None else env
        jid = e.get("JMP_JID", DEFAULT_JID)
        cred_path = os.path.expanduser(e.get("JMP_CRED_JSON", DEFAULT_CRED))
        with open(cred_path) as fh:
            cred = json.load(fh)
        return cls(
            cred.get("jid", jid), cred["password"],
            inbox_db=e.get("JMP_INBOX_DB", DEFAULT_INBOX),
            resource=e.get("NOSMS_JMP_RESOURCE", "nosms"),
            reply=e.get("JMP_REPLY", "0") in ("1", "true", "yes"),
            reply_text=e.get("JMP_REPLY_TEXT"),
            **kwargs,
        )

    # --- lifecycle --------------------------------------------------------

    def start(self, timeout: float | None = None) -> bool:
        """Start the background client. Returns connected-within-timeout."""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="jmp-xmpp",
                                            daemon=True)
            self._thread.start()
        self._ready.wait(timeout or self.connect_timeout)
        return self.is_connected()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the client gracefully: disconnect, let the loop unwind, join.

        Deliberately does **not** call ``loop.stop()`` — that aborts a running
        ``run_until_complete`` mid-flight and leaves pending tasks to be
        destroyed. Instead it disconnects and lets the loop finish its cycle.
        """
        if self._thread is None or not self._thread.is_alive():
            return
        self._stopping = True
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
                fut.result(timeout=max(1.0, timeout - 1.0))
            except Exception:                  # noqa: BLE001
                pass
        self._thread.join(timeout=timeout)

    async def _disconnect(self) -> None:       # pragma: no cover - needs a live link
        client = self._client
        if client is not None:
            try:
                client.disconnect()
            except Exception:                  # noqa: BLE001
                pass
        for _ in range(80):                    # _cycle polls _stopping every 0.5 s
            if self._client is None:
                break
            await asyncio.sleep(0.05)

    def is_connected(self) -> bool:
        return bool(self._connected and self._terminal_reason is None)

    @property
    def down_reason(self) -> str | None:
        return self._terminal_reason

    # --- sending ----------------------------------------------------------

    def send_message(self, to_jid: str, body: str, msg_id: str | None = None) -> None:
        """Queue one cold chat message and block until it is on the wire.

        Raises :class:`RailUnavailable` — never returns silently — when the
        rail cannot carry it.
        """
        if self._terminal_reason:
            raise RailUnavailable(self._terminal_reason)
        if not self._ready.is_set() or self._loop is None or self._outbox is None:
            raise RailUnavailable("not_connected", "link has not connected yet")
        if not self.is_connected():
            raise RailUnavailable("not_connected", "link is reconnecting")
        fut: Future = Future()
        try:
            self._loop.call_soon_threadsafe(
                self._outbox.put_nowait, (to_jid, body, msg_id, fut))
            fut.result(timeout=self.send_timeout)
        except FuturesTimeoutError as exc:
            # concurrent.futures.TimeoutError, NOT the builtin: they are only
            # aliases from 3.11 on. Catching the wrong one would let a send
            # timeout escape as an unexpected exception, breaking the
            # "only raises RailUnavailable" contract the refund path relies on.
            raise RailUnavailable("connection_lost",
                                  "no acknowledgement before timeout") from exc
        except RuntimeError as exc:
            # ``call_soon_threadsafe`` on a loop that has been closed (the link
            # was shut down while this send was in flight) raises RuntimeError.
            # That is a rail-down condition too, not a raw crash: the caller must
            # still be able to refund.
            raise RailUnavailable("connection_lost",
                                  f"loop unavailable: {exc}") from exc

    # --- background loop --------------------------------------------------

    def _run(self) -> None:                    # pragma: no cover - thread body
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._outbox = asyncio.Queue()
        try:
            loop.run_until_complete(self._supervise())
        finally:
            # Cancel anything the XMPP stack left behind and let the
            # cancellations settle before the loop is closed.
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:                  # noqa: BLE001
                pass
            loop.close()

    async def _supervise(self) -> None:        # pragma: no cover - thread body
        backoff = self.reconnect_min
        terminal_attempts = 0
        while not self._stopping:
            try:
                await self._cycle()
                backoff = self.reconnect_min
            except Exception:                  # noqa: BLE001 - never die silently
                self._set_disconnected("connection_lost")
            if self._terminal_reason in TERMINAL_REASONS:
                # A wrong credential / terminated account will not fix itself.
                # Retrying forever is repeated auth attempts against the server
                # (abusive-looking on a personal line) and can never succeed, so
                # give up loudly instead of hammering.
                terminal_attempts += 1
                if terminal_attempts >= MAX_TERMINAL_RETRIES:
                    self._terminal_exhausted = True
                    self._ready.set()
                    return
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max)

    async def _cycle(self) -> None:            # pragma: no cover - needs a live link
        import slixmpp

        factory = self._client_factory or slixmpp.ClientXMPP
        x = factory(self.jid, self.password)
        for plugin in ("xep_0030", "xep_0199", "xep_0203"):
            try:
                x.register_plugin(plugin)
            except Exception:                  # noqa: BLE001
                pass
        if self.resource:
            x.boundjid.resource = self.resource

        started = asyncio.get_event_loop().create_future()

        def _on_start(_event):
            if not started.done():
                started.set_result(True)

        def _on_failed_auth(_event):
            self._terminal_reason = "auth_failed"
            if not started.done():
                started.set_result(False)

        def _on_stream_error(_event):
            if not started.done():
                started.set_result(False)

        x.add_event_handler("session_start", _on_start)
        x.add_event_handler("failed_auth", _on_failed_auth)
        x.add_event_handler("stream_error", _on_stream_error)
        x.add_event_handler("message", self._on_message)

        x.connect()                            # NOT awaited (proven pattern)
        try:
            ok = await asyncio.wait_for(started, timeout=self.connect_timeout)
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            self._set_disconnected(self._terminal_reason or "not_connected")
            return

        self._connected = True
        self._ready.set()
        sender = asyncio.ensure_future(self._sender(x))
        self._client = x
        self._sender_task = sender
        try:
            while x.is_connected() and not self._stopping:
                await asyncio.sleep(0.5)
        finally:
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
            self._client = None
            self._sender_task = None
            self._set_disconnected("connection_lost")

    async def _sender(self, x) -> None:        # pragma: no cover - needs a live link
        while True:
            to_jid, body, msg_id, fut = await self._outbox.get()
            if fut.done():
                continue
            try:
                if not x.is_connected():
                    raise RailUnavailable(self._terminal_reason or "connection_lost")
                msg = x.Message()
                msg["to"] = to_jid
                msg["type"] = "chat"
                msg["id"] = msg_id or uuid.uuid4().hex
                msg["body"] = body
                msg.send()
                self._log("out", to_jid, body)
                fut.set_result(True)
            except RailUnavailable as exc:
                fut.set_exception(exc)
            except Exception as exc:           # noqa: BLE001
                fut.set_exception(RailUnavailable("connection_lost", str(exc)[:120]))

    # --- inbound (QA) -----------------------------------------------------

    def _on_message(self, msg) -> None:        # pragma: no cover - needs a live link
        if msg["type"] not in ("chat", "normal", "headline"):
            return
        peer = msg["from"].bare
        body = (msg["body"] or "").strip()
        delayed = ""
        try:
            delayed = str(msg["delay"]["stamp"] or "")
        except Exception:                      # noqa: BLE001
            pass
        self._log("in", peer, body, delayed=delayed, raw_type=msg["type"])
        if self.reply:
            try:
                msg.reply(self.reply_text).send()
                self._log("out", peer, self.reply_text)
            except Exception:                  # noqa: BLE001 - report, never die
                pass

    # --- sqlite inbox -----------------------------------------------------

    def _init_inbox(self) -> None:
        with self._dump_lock, sqlite3.connect(self.inbox_db, timeout=15) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts REAL NOT NULL,
                       direction TEXT NOT NULL CHECK (direction IN ('in','out')),
                       peer TEXT NOT NULL,
                       body TEXT,
                       delayed TEXT,
                       raw_type TEXT)""")

    def _log(self, direction: str, peer: str, body: str, *,
             delayed: str = "", raw_type: str = "") -> None:
        if not self.inbox_db:
            return
        with self._dump_lock, sqlite3.connect(self.inbox_db, timeout=15) as con:
            con.execute(
                "INSERT INTO messages (ts, direction, peer, body, delayed, raw_type)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), direction, peer, body, delayed, raw_type))

    def _set_disconnected(self, reason: str) -> None:
        self._connected = False
        if reason in TERMINAL_REASONS:
            self._terminal_reason = reason
