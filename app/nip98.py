"""NIP-98 HTTP auth (kind 27235) with freshness and a replay cache.

A request authenticates by carrying a signed nostr event whose tags bind it to the
exact HTTP request:

* ``Authorization: Nostr <base64(JSON event)>``
* ``kind == 27235``
* a ``u`` tag equal to the request URL (scheme + host + path; query is tolerated)
* a ``method`` tag equal to the HTTP method
* ``created_at`` within :data:`FRESHNESS_SECONDS` of server time
* an event id seen at most once while the replay TTL holds

The event id is **recomputed** from the signed fields rather than trusted, and the
declared ``id`` must match it - otherwise a copy-paste of a valid event with a
rewritten id field would sail through.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
from urllib.parse import urlparse

from coincurve import PublicKeyXOnly

#: Tolerance on ``created_at``, in seconds (PLAN M1: +/- 120 s).
FRESHNESS_SECONDS = 120
#: How long a seen event id is remembered. Must be >= the freshness window.
DEFAULT_REPLAY_TTL = 300
#: The only event kind that may authenticate an HTTP request.
NIP98_KIND = 27235

_REQUIRED_FIELDS = ("id", "pubkey", "created_at", "kind", "tags", "content", "sig")


class Nip98Error(Exception):
    """A rejected NIP-98 credential. Always has a machine reason and a hint."""

    def __init__(self, reason: str, hint: str, status_code: int = 401):
        self.reason = reason
        self.hint = hint
        self.status_code = status_code
        super().__init__(f"{reason}: {hint}")


class ReplayCache:
    """In-memory, TTL-bounded set of event ids already used.

    Deliberately simple: a single process is the M1 deployment target and the TTL
    is short. A multi-process deployment swaps this for a shared store behind the
    same three methods.
    """

    def __init__(self, ttl: int = DEFAULT_REPLAY_TTL):
        if ttl < FRESHNESS_SECONDS:
            raise ValueError("replay TTL must be >= the freshness window")
        self.ttl = int(ttl)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for event_id, expiry in list(self._seen.items()):
                if expiry <= now:
                    del self._seen[event_id]

    def contains(self, event_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            expiry = self._seen.get(event_id)
            if expiry is None:
                return False
            if expiry <= now:
                del self._seen[event_id]
                return False
            return True

    def check_and_store(self, event_id: str, now: float | None = None) -> bool:
        """Record ``event_id``; return ``False`` if it was already present."""
        now = time.time() if now is None else now
        with self._lock:
            expiry = self._seen.get(event_id)
            if expiry is not None and expiry > now:
                return False
            self._seen[event_id] = now + self.ttl
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


def serialize_for_id(event: dict) -> str:
    """The exact string hashed into a nostr event id (NIP-01)."""
    return json.dumps(
        [0, event["pubkey"], int(event["created_at"]), int(event["kind"]),
         event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def event_id_of(event: dict) -> str:
    """Recompute the event id from the signed fields."""
    return hashlib.sha256(serialize_for_id(event).encode("utf-8")).hexdigest()


def _tag_values(event: dict, name: str) -> list[str]:
    out: list[str] = []
    for tag in event.get("tags") or []:
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == name:
            out.append(str(tag[1]))
    return out


def _normalized_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(str(url))
    return (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/")


def verify_signature_only(event: dict) -> bool:
    """BIP-340 schnorr verification of an event, with no freshness/replay checks.

    Used by the verifier and to prove the copy-pasteable example in ``llms.txt``
    is a genuinely signed event.
    """
    try:
        declared_id = str(event["id"]).lower()
        if declared_id != event_id_of(event):
            return False
        signature = bytes.fromhex(str(event["sig"]))
        pubkey = bytes.fromhex(str(event["pubkey"]))
        if len(signature) != 64 or len(pubkey) != 32:
            return False
        return bool(PublicKeyXOnly(pubkey).verify(signature, bytes.fromhex(declared_id)))
    except (KeyError, TypeError, ValueError, binascii.Error):
        return False


def verify_nip98_header(
    header: str | None,
    *,
    url: str,
    method: str,
    replay_cache: ReplayCache | None = None,
    now: int | None = None,
) -> str:
    """Validate an ``Authorization`` header. Returns the signer's pubkey (hex)."""
    now = int(time.time()) if now is None else int(now)

    if header is None or not str(header).strip():
        raise Nip98Error("auth_missing", "No Authorization header was supplied.")

    parts = str(header).split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "nostr":
        raise Nip98Error("auth_malformed", "The Authorization scheme must be 'Nostr'.")

    try:
        raw = base64.b64decode(parts[1], validate=True)
        event = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise Nip98Error("auth_malformed", "The credential is not base64-encoded JSON.")

    if not isinstance(event, dict) or not set(_REQUIRED_FIELDS) <= set(event):
        raise Nip98Error("auth_malformed", "The event is missing required NIP-01 fields.")

    try:
        kind = int(event["kind"])
        created_at = int(event["created_at"])
    except (TypeError, ValueError):
        raise Nip98Error("auth_malformed", "kind and created_at must be integers.")

    if kind != NIP98_KIND:
        raise Nip98Error("auth_invalid", f"kind must be {NIP98_KIND} (NIP-98), got {kind}.")

    if abs(now - created_at) > FRESHNESS_SECONDS:
        raise Nip98Error(
            "auth_stale",
            f"created_at is {abs(now - created_at)}s from server time (limit {FRESHNESS_SECONDS}s).",
        )

    u_tags = _tag_values(event, "u")
    method_tags = _tag_values(event, "method")
    if not u_tags:
        raise Nip98Error("auth_invalid", "The event has no 'u' tag binding it to a URL.")
    if not method_tags:
        raise Nip98Error("auth_invalid", "The event has no 'method' tag.")

    if _normalized_url(url) not in {_normalized_url(u) for u in u_tags}:
        raise Nip98Error("auth_invalid", "The 'u' tag does not match this request URL.")
    if str(method).upper() not in {m.upper() for m in method_tags}:
        raise Nip98Error("auth_invalid", "The 'method' tag does not match this HTTP method.")

    if not verify_signature_only(event):
        raise Nip98Error("auth_invalid", "The event signature does not verify.")

    event_id = event_id_of(event)
    if replay_cache is not None and not replay_cache.check_and_store(event_id, now=now):
        raise Nip98Error("auth_replay", "This event id has already been used.")

    return str(event["pubkey"])
