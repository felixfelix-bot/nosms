#!/usr/bin/env python3
"""Live probe for the nosms M1a service core (board sms-gateway, t_3ccdef32).

Talks real HTTP to a locally running uvicorn instance and prints a transcript.
Signs NIP-98 events with a real key for the real request URL. Run:

    ~/nosms/.venv/bin/python live_probe.py [base_url]
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time

import httpx
from coincurve import PrivateKey

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18088"
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
    send_url = f"{BASE}/api/send"
    body = {"to": "+14155551234", "text": "m1a probe"}
    with httpx.Client(timeout=10) as c:
        show("GET /api/health", c.get(f"{BASE}/api/health"))
        show("GET /api/pricing", c.get(f"{BASE}/api/pricing"))
        r = c.get(f"{BASE}/llms.txt")
        print(f"\n### GET /llms.txt  (HTTP {r.status_code}, {len(r.text)} bytes, "
              f"{r.headers.get('content-type')})")
        print("\n".join(r.text.splitlines()[:3]))
        show("GET /llms-full.txt", c.get(f"{BASE}/llms-full.txt"))
        show("POST /api/send  (unsigned)", c.post(send_url, json=body))
        show("POST /api/send  (signed)",
             c.post(send_url, json=body, headers={"Authorization": "Nostr " + signed_event(send_url)}))
        show("POST /api/send  (signed, replayed)",
             (tok := signed_event(send_url)) and
             (c.post(send_url, json=body, headers={"Authorization": "Nostr " + tok}) and
              c.post(send_url, json=body, headers={"Authorization": "Nostr " + tok})))
        stale = signed_event(send_url, created_at=int(time.time()) - 600)
        show("POST /api/send  (signed, stale created_at)",
             c.post(send_url, json=body, headers={"Authorization": "Nostr " + stale}))
        show("POST /api/send  (signed, bad destination)",
             c.post(send_url, json={"to": "4155551234", "text": "x"},
                    headers={"Authorization": "Nostr " + signed_event(send_url, content="baddest")}))
        show("POST /api/send  (signed, wrong URL in u tag)",
             c.post(send_url, json=body,
                    headers={"Authorization": "Nostr " + signed_event(f"{BASE}/api/other", content="wrongurl")}))
        show("GET /api/nope", c.get(f"{BASE}/api/nope"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
