#!/usr/bin/env python3
"""Live smoke of the JMP rail **as the service builds it** (`build_transport`).

Where `jmp_cold_send_probe.py` proves the raw stanza, this exercises the adapter
the CVM server will actually call: the long-lived link, the paced Transport, and
the capability flags it serves. One real SMS leaves the operator's line.

Run:
  python scripts/jmp_rail_smoke.py                # to the newest inbox peer
  python scripts/jmp_rail_smoke.py --to +1...     # to an arbitrary handset

Exit 0 = adapter accepted the send; 2 = rail refused; 3 = link never connected.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SMS_JID = re.compile(r"^(\+\d{7,15})@cheogram\.com$", re.I)
INBOX = os.path.expanduser("~/.hermes/profiles/manager/state/jmp_inbox.db")


def mask(number: str) -> str:
    d = "".join(ch for ch in number if ch.isdigit())
    return f"+{d[:1]}{'*' * (len(d) - 5)}{d[-4:]}" if len(d) > 5 else "*" * len(d)


def last_inbound_number(db: str = INBOX) -> str | None:
    with sqlite3.connect(db, timeout=10) as con:
        rows = list(con.execute("SELECT peer, direction FROM messages ORDER BY id"))
    for peer, direction in reversed(rows):
        if direction == "in" and (m := SMS_JID.match(peer or "")):
            return "+" + "".join(c for c in m.group(1) if c.isdigit())
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default=None)
    ap.add_argument("--body", default=None)
    ap.add_argument("--resource", default="railtest")
    ap.add_argument("--pacing-state", default="/tmp/nosms-jmp-pacing-smoke.json")
    args = ap.parse_args()

    from app.transports import build_transport

    env = {
        "NOSMS_TRANSPORT": "jmp_only",
        "NOSMS_JMP_RESOURCE": args.resource,
        "NOSMS_JMP_PACING_STATE": args.pacing_state,
        "NOSMS_JMP_DAILY_CAP": "50",
        "NOSMS_JMP_MIN_GAP_SECONDS": "0",
        "NOSMS_JMP_MAX_GAP_SECONDS": "0",
    }
    transport = build_transport(env=env)
    link = transport.link

    if not link.start(timeout=45):
        print(json.dumps({"event": "connect_failed", "jid": link.jid,
                          "down_reason": link.down_reason}))
        return 3
    print(json.dumps({"event": "connected", "jid": link.jid,
                      "capabilities": transport.capability_flags()}))

    dest = args.to or last_inbound_number()
    if not dest:
        print(json.dumps({"event": "error", "err": "no destination"}))
        link.close()
        return 3

    body = args.body or (f"nosms rail smoke {time.strftime('%H%M%SZ', time.gmtime())}"
                         " — adapter path, not a reply")
    result = transport.send(dest, body)
    print(json.dumps({"event": "send_result", "to": mask(dest),
                      "accepted": result.accepted, "rail": result.rail,
                      "receipt": result.receipt, "detail": result.detail}))
    link.close()
    return 0 if result.accepted else 2


if __name__ == "__main__":
    sys.exit(main())
