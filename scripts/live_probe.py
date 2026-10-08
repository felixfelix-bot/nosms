#!/usr/bin/env python3
"""Live probe for a running nosms M1a deployment.

Talks real HTTP to a running instance and prints what it actually answers. It
deliberately asserts NOTHING about the `X-Reason` tokens: the M1a surface has
drifted between implementations (e.g. `auth_replayed` vs `auth_replay`,
`auth_expired` vs `auth_stale`), so hardcoding expectations here would produce
confidently wrong output. Read the printed `x-reason` values.

    ~/nosms/.venv/bin/python scripts/live_probe.py [base_url]     # default 127.0.0.1:8088

Signs NIP-98 events with a throwaway key, bound to the real request URL.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time

import httpx
from coincurve import PrivateKey

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8088"
SK = bytes.fromhex("33" * 32)
PUB = PrivateKey(SK).public_key.format(compressed=True)[1:].hex()


def signed_event(url: str, method: str = "POST", created_at: int | None = None,
                 content: str = "") -> str:
    created_at = int(time.time()) if created_at is None else int(created_at)
    tags = [["u", url], ["method", method]]
    payload = json.dumps([0, PUB, created_at, 27235, tags, content], separators=(",", ":"))
    eid = hashlib.sha256(payload.encode()).hexdigest()
    sig = PrivateKey(SK).sign_schnorr(bytes.fromhex(eid), aux_randomness=b"\x00" * 32).hex()
    event = {"id": eid, "pubkey": PUB, "created_at": created_at, "kind": 27235,
             "tags": tags, "content": content, "sig": sig}
    return base64.b64encode(json.dumps(event).encode()).decode()


def show(label: str, r: httpx.Response, body: str | None = None) -> None:
    print(f"\n### {label}")
    print(f"HTTP {r.status_code}")
    for key in ("content-type", "x-reason", "x-hint"):
        if key in r.headers:
            print(f"{key}: {r.headers[key]}")
    print((body if body is not None else r.text)[:400])


def main() -> int:
    send = f"{BASE}/api/send"
    body = {"to": "+14155551234", "text": "m1a probe"}
    auth = lambda tok: {"Authorization": "Nostr " + tok}  # noqa: E731
    with httpx.Client(timeout=10) as c:
        show("GET /api/health", c.get(f"{BASE}/api/health"))
        r = c.get(f"{BASE}/llms.txt")
        print(f"\n### GET /llms.txt  (HTTP {r.status_code}, {len(r.text)} bytes, "
              f"{r.headers.get('content-type')})")
        print("\n".join(r.text.splitlines()[:3]))
        show("GET /llms-full.txt", c.get(f"{BASE}/llms-full.txt"))
        show("POST /api/send  (unsigned)", c.post(send, json=body))

        show("POST /api/send  (signed)",
             c.post(send, json=body, headers=auth(signed_event(send, content="d1"))))
        tok = signed_event(send, content="replay")
        c.post(send, json=body, headers=auth(tok))
        show("POST /api/send  (signed, SAME event replayed)",
             c.post(send, json=body, headers=auth(tok)))
        show("POST /api/send  (signed, created_at -600s)",
             c.post(send, json=body,
                    headers=auth(signed_event(send, created_at=int(time.time()) - 600,
                                              content="stale"))))
        show("POST /api/send  (signed, u tag = another URL)",
             c.post(send, json=body,
                    headers=auth(signed_event(f"{BASE}/api/other", content="wrongurl"))))
        show("POST /api/send  (signed, malformed destination)",
             c.post(send, json={"to": "4155551234", "text": "x"},
                    headers=auth(signed_event(send, content="baddest"))))
        show("GET /api/nope", c.get(f"{BASE}/api/nope"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
