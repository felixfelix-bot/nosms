"""Escrow ledger for the paid send path.

One row per accepted send, in SQLite (the service's only state). It records what
the service *holds* — the proofs captured by the escrow swap — so a refund is a
database claim rather than a hope about which of two racing timers won.

The row deliberately stores no message body: bodies are not persisted (see
``/llms.txt``), and nothing after the send needs them.

Refund atomicity is the whole point of this module. ``claim_refund`` is a single
conditional UPDATE; SQLite serialises it, so exactly one caller can ever move a
message into the refunded state. A sweep that runs twice cannot double-refund.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS escrows (
    message_id          TEXT PRIMARY KEY,
    pubkey              TEXT NOT NULL,
    dest                TEXT NOT NULL,
    rail                TEXT NOT NULL,
    price               INTEGER NOT NULL,
    change              INTEGER NOT NULL DEFAULT 0,
    escrow_token        TEXT,
    change_token        TEXT,
    status              TEXT NOT NULL DEFAULT 'queued',
    provider_status     TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    refunded_at         REAL,
    refund_amount       INTEGER,
    refund_token        TEXT,
    refund_reason       TEXT
);
CREATE INDEX IF NOT EXISTS escrows_unrefunded ON escrows (refunded_at, created_at);
"""


@dataclass
class EscrowRecord:
    message_id: str
    pubkey: str
    dest: str
    rail: str
    price: int
    change: int
    status: str
    created_at: float
    updated_at: float
    escrow_token: str | None = None
    change_token: str | None = None
    provider_status: str = ""
    provider_message_id: str | None = None
    refunded_at: float | None = None
    refund_amount: int | None = None
    refund_token: str | None = None
    refund_reason: str | None = None

    @property
    def amount(self) -> int:
        """The postage actually held in escrow (what the send was priced at)."""
        return int(self.price)

    @property
    def held(self) -> int:
        """Everything the service holds for this message: postage + change."""
        return int(self.price) + int(self.change)

    @property
    def refunded(self) -> bool:
        return self.refunded_at is not None


_COLUMNS = ("message_id", "pubkey", "dest", "rail", "price", "change", "escrow_token",
            "change_token", "status", "provider_status", "provider_message_id", "created_at",
            "updated_at", "refunded_at", "refund_amount", "refund_token", "refund_reason")


class EscrowStore:
    """SQLite-backed escrow ledger, safe to call from several threads."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # --- writes -------------------------------------------------------------

    def create(self, *, message_id: str, pubkey: str, dest: str, rail: str, price: int,
               change: int, escrow_token: str | None = None, change_token: str | None = None,
               status: str = "queued", provider_status: str = "",
               provider_message_id: str | None = None, created_at: float | None = None
               ) -> EscrowRecord:
        now = float(created_at if created_at is not None else time.time())
        with self._lock:
            self._db.execute(
                """INSERT INTO escrows (message_id, pubkey, dest, rail, price, change,
                        escrow_token, change_token, status, provider_status,
                        provider_message_id, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (message_id, pubkey, dest, rail, int(price), int(change), escrow_token,
                 change_token, status, provider_status, provider_message_id, now, now))
            self._db.commit()
        return self.get(message_id)

    def update_status(self, message_id: str, status: str | None = None,
                      provider_status: str | None = None,
                      provider_message_id: str | None = None) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status = ?"), args.append(status)
        if provider_status is not None:
            sets.append("provider_status = ?"), args.append(provider_status)
        if provider_message_id is not None:
            sets.append("provider_message_id = ?"), args.append(provider_message_id)
        if not sets:
            return
        sets.append("updated_at = ?"), args.append(time.time())
        args.append(message_id)
        with self._lock:
            self._db.execute(f"UPDATE escrows SET {', '.join(sets)} WHERE message_id = ?", args)
            self._db.commit()

    def claim_refund(self, message_id: str, *, token: str, amount: int, reason: str,
                     now: float | None = None) -> bool:
        """Atomically move a message into the refunded state. True if *we* won.

        A second caller (a re-run sweep, a crashed run being retried) gets False
        and must not touch the wallet.
        """
        stamp = float(now if now is not None else time.time())
        with self._lock:
            cur = self._db.execute(
                """UPDATE escrows
                      SET refunded_at = ?, refund_amount = ?, refund_token = ?,
                          refund_reason = ?, updated_at = ?
                    WHERE message_id = ? AND refunded_at IS NULL""",
                (stamp, int(amount), token, reason, stamp, message_id))
            self._db.commit()
            return cur.rowcount == 1

    # --- reads --------------------------------------------------------------

    def get(self, message_id: str) -> EscrowRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM escrows WHERE message_id = ?",
                                   (message_id,)).fetchone()
        return _row(row) if row else None

    def list_all(self) -> list[EscrowRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM escrows ORDER BY created_at, rowid").fetchall()
        return [_row(r) for r in rows]

    def list_unrefunded(self, limit: int = 500) -> list[EscrowRecord]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM escrows WHERE refunded_at IS NULL
                    ORDER BY created_at LIMIT ?""", (int(limit),)).fetchall()
        return [_row(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _row(row: sqlite3.Row) -> EscrowRecord:
    return EscrowRecord(**{k: row[k] for k in _COLUMNS})
