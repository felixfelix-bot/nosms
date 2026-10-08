"""Abuse controls for the paid send path: per-destination cooldown + daily cap.

Both limits are configuration, not constants: the same store behaves differently
under different ``Config`` values, so the tests can pin the *rule* rather than a
magic number. A limit of 0 disables that check.

The cap is per identity (the NIP-98 pubkey — the sender's own key, since nosms
has no accounts) over a rolling 24 h window; the cooldown is per (identity,
destination) pair.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_sends (
    pubkey  TEXT NOT NULL,
    dest    TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_sends_identity ON quota_sends (pubkey, ts);
CREATE INDEX IF NOT EXISTS quota_sends_dest ON quota_sends (pubkey, dest, ts);
"""

WINDOW_SECONDS = 24 * 3600


class QuotaError(Exception):
    """A quota rejection carrying the X-Reason token, an X-Hint and Retry-After."""

    def __init__(self, reason: str, hint: str, retry_after: int | None = None):
        self.reason = reason
        self.hint = hint
        self.retry_after = retry_after
        super().__init__(f"{reason}: {hint}")


class QuotaStore:
    def __init__(self, path: str = ":memory:", cooldown_seconds: int = 60,
                 daily_cap: int = 100):
        self.cooldown_seconds = int(cooldown_seconds)
        self.daily_cap = int(daily_cap)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # --- checks -------------------------------------------------------------

    def check(self, pubkey: str, dest: str, now: float | None = None) -> None:
        """Raise ``QuotaError`` when this send must be refused. Records nothing."""
        moment = float(now if now is not None else time.time())
        with self._lock:
            if self.cooldown_seconds > 0:
                row = self._db.execute(
                    "SELECT MAX(ts) FROM quota_sends WHERE pubkey = ? AND dest = ?",
                    (pubkey, dest)).fetchone()
                last = row[0] if row else None
                if last is not None:
                    elapsed = moment - float(last)
                    if elapsed < self.cooldown_seconds:
                        wait = int(math.ceil(self.cooldown_seconds - elapsed))
                        raise QuotaError(
                            "destination_cooldown",
                            f"{dest} was messaged {int(elapsed)}s ago; the per-destination "
                            f"cooldown is {self.cooldown_seconds}s (wait {wait}s).",
                            retry_after=wait)
            if self.daily_cap > 0:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM quota_sends WHERE pubkey = ? AND ts > ?",
                    (pubkey, moment - WINDOW_SECONDS)).fetchone()
                used = int(row[0]) if row else 0
                if used >= self.daily_cap:
                    raise QuotaError(
                        "daily_cap_reached",
                        f"This identity has sent {used} messages in the last 24h; "
                        f"the daily cap is {self.daily_cap}.",
                        retry_after=None)

    def record(self, pubkey: str, dest: str, now: float | None = None) -> None:
        moment = float(now if now is not None else time.time())
        with self._lock:
            self._db.execute("INSERT INTO quota_sends (pubkey, dest, ts) VALUES (?,?,?)",
                             (pubkey, dest, moment))
            self._db.commit()

    def check_and_record(self, pubkey: str, dest: str, now: float | None = None) -> None:
        moment = float(now if now is not None else time.time())
        with self._lock:
            self.check(pubkey, dest, now=moment)
            self.record(pubkey, dest, now=moment)

    # --- reporting ----------------------------------------------------------

    def usage(self, pubkey: str, now: float | None = None) -> int:
        moment = float(now if now is not None else time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM quota_sends WHERE pubkey = ? AND ts > ?",
                (pubkey, moment - WINDOW_SECONDS)).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
