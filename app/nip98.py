"""NIP-98 (kind 27235) HTTP auth verification.

Independent of the web framework: given a decoded event plus the request URL,
method and body, decide whether the request is authorised. Every rejection
carries a machine `reason` (the X-Reason token) and a one-sentence `hint`.

Checks, in order (NIP-98 plus the nosms card):
1. header parses to a base64 JSON event           -> auth_missing / auth_invalid
2. `kind == 27235`                                 -> auth_invalid
3. event id == sha256(NIP-01 serialisation)        -> auth_invalid
4. BIP-340 signature verifies against the id       -> auth_invalid
5. |now - created_at| <= freshness window          -> auth_expired
6. `u` and `method` tags present                   -> auth_invalid
7. `u` matches scheme+host+path (query tolerant)   -> auth_url_mismatch
8. `method` matches the HTTP method                -> auth_method_mismatch
9. `payload` (if present) == sha256(request body)  -> auth_payload_mismatch
10. event id unseen within the replay TTL          -> auth_replayed
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from urllib.parse import parse_qsl, urlsplit

from coincurve import PublicKeyXOnly

NIP98_KIND = 27235

_HINTS = {
    "auth_missing": "Add an 'Authorization: Nostr' header carrying a base64-encoded NIP-98 event.",
    "auth_invalid": "The NIP-98 event is malformed or its signature does not verify.",
    "auth_expired": "The event's created_at is outside the accepted freshness window; re-sign it.",
    "auth_replayed": "This event id was already used; sign a fresh event for each request.",
    "auth_url_mismatch": "The `u` tag must match the request scheme+host+path.",
    "auth_method_mismatch": "The `method` tag must match the HTTP method used.",
    "auth_payload_mismatch": "The `payload` tag must be sha256(body) as hex.",
}


class Nip98Error(Exception):
    """Auth failure carrying the X-Reason token and an X-Hint sentence."""

    def __init__(self, reason: str, hint: str | None = None):
        self.reason = reason
        self.hint = hint or _HINTS.get(reason, "Request not authorised.")
        super().__init__(f"{reason}: {self.hint}")


class InMemoryReplayCache:
    """event id -> expiry. TTL must be >= the freshness window."""

    def __init__(self, ttl_seconds: int):
        self.ttl = int(ttl_seconds)
        self._seen: dict[str, int] = {}

    def check_and_store(self, key: str, now: int) -> bool:
        """Return True when the key is fresh (and record it); False on replay."""
        exp = self._seen.get(key)
        if exp is not None and exp > now:
            return False
        # opportunistic prune keeps the cache bounded
        if len(self._seen) > 4096:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        self._seen[key] = now + self.ttl
        return True


def _event_id(event: dict) -> str:
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"],
         event["tags"], event["content"]],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _tag_values(tags: list, name: str) -> list[str]:
    out = []
    for t in tags:
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            out.append(t[1])
    return out


def _url_matches(event_url: str, request_url: str) -> bool:
    """scheme+host+path must agree; event query params must all be in the request."""
    a, b = urlsplit(event_url), urlsplit(request_url)
    if a.scheme.lower() != b.scheme.lower():
        return False
    if a.netloc.lower() != b.netloc.lower():
        return False
    if (a.path or "/") != (b.path or "/"):
        return False
    have = dict(parse_qsl(b.query, keep_blank_values=True))
    for k, v in parse_qsl(a.query, keep_blank_values=True):
        if have.get(k) != v:
            return False
    return True


class Nip98Verifier:
    def __init__(self, freshness_seconds: int = 120,
                 replay_cache: InMemoryReplayCache | None = None, now=None):
        self.freshness = int(freshness_seconds)
        self.replay = replay_cache or InMemoryReplayCache(max(self.freshness, 120))
        self._now = now or (lambda: int(time.time()))

    def verify_event(self, event: dict, *, url: str, method: str,
                     body: bytes | None = None) -> str:
        if not isinstance(event, dict):
            raise Nip98Error("auth_invalid")
        try:
            pubkey = event["pubkey"]
            created_at = int(event["created_at"])
            kind = int(event["kind"])
            tags = event["tags"]
            content = event["content"]
            sig = event["sig"]
            claimed_id = event["id"]
        except (KeyError, TypeError, ValueError):
            raise Nip98Error("auth_invalid") from None
        if not isinstance(tags, list) or not isinstance(pubkey, str) \
                or not isinstance(sig, str) or not isinstance(claimed_id, str):
            raise Nip98Error("auth_invalid")

        if kind != NIP98_KIND:
            raise Nip98Error("auth_invalid")

        # recompute the id - never trust the one in the payload
        try:
            real_id = _event_id({"pubkey": pubkey, "created_at": created_at,
                                 "kind": kind, "tags": tags, "content": content})
        except (TypeError, ValueError):
            raise Nip98Error("auth_invalid") from None
        if real_id != claimed_id:
            raise Nip98Error("auth_invalid")

        try:
            ok = PublicKeyXOnly(bytes.fromhex(pubkey)).verify(
                bytes.fromhex(sig), bytes.fromhex(real_id))
        except (ValueError, TypeError, binascii.Error):
            raise Nip98Error("auth_invalid") from None
        if not ok:
            raise Nip98Error("auth_invalid")

        now = int(self._now())
        if abs(now - created_at) > self.freshness:
            raise Nip98Error("auth_expired")

        u_vals = _tag_values(tags, "u")
        m_vals = _tag_values(tags, "method")
        if not u_vals or not m_vals:
            raise Nip98Error("auth_invalid")

        if not _url_matches(u_vals[0], url):
            raise Nip98Error("auth_url_mismatch")
        if m_vals[0].upper() != (method or "").upper():
            raise Nip98Error("auth_method_mismatch")

        payload_vals = _tag_values(tags, "payload")
        if payload_vals:
            digest = hashlib.sha256(body or b"").hexdigest()
            if payload_vals[0].lower() != digest:
                raise Nip98Error("auth_payload_mismatch")

        if not self.replay.check_and_store(real_id, now):
            raise Nip98Error("auth_replayed")

        return pubkey


def verify_nip98(authorization: str | None, *, url: str, method: str,
                 body: bytes | None = None,
                 verifier: Nip98Verifier | None = None) -> str:
    """Parse an `Authorization: Nostr <base64>` header and verify the event."""
    if not authorization or not authorization.strip():
        raise Nip98Error("auth_missing")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "nostr":
        raise Nip98Error("auth_invalid")
    try:
        raw = base64.b64decode(parts[1].strip(), validate=True)
        event = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise Nip98Error("auth_invalid") from None
    verifier = verifier or Nip98Verifier()
    return verifier.verify_event(event, url=url, method=method, body=body)
