#!/usr/bin/env python3
"""Offline before/after probe for the "rail never started" bug (t_f2a6f901).

Deterministic, no network: the same CvmTools + JMP/Cheogram rail the server
serves, driven through the *server's own* lifecycle helpers with a stub link
that is not connected until someone starts it — exactly how an unstarted
SlixmppLink behaves.

    python3 scripts/cvm_rail_start_probe.py --mode before   # on master
    python3 scripts/cvm_rail_start_probe.py --mode after    # on the fix
    python3 scripts/cvm_rail_start_probe.py --mode down     # configured-but-down

`before` reproduces the live 2026-10-11 reply (`rail_not_configured` for a
message the service could not send). `after` shows the paid path being reached
(`payment_required`). `down` shows the honest rail-down answer that must never
be confused with "not configured".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.cvm import ContractContext                                    # noqa: E402
from app.cvm_tools import CvmTools                                      # noqa: E402
from app.escrow import EscrowStore                                      # noqa: E402
from app.transports.jmp_cheogram import JmpCheogramTransport            # noqa: E402

CALLER = "ab" * 32          # not an owner: the payment gate is what stops us
DEST = "+15555550100"       # non-routable placeholder; nothing is dialled


class StubLink:
    """A JMP link that is dead until started — an unstarted SlixmppLink."""

    def __init__(self, connectable: bool = True) -> None:
        self.jid = "stub@jabber.fr/stub"
        self.down_reason = "not_connected"
        self._connected = False
        self._connectable = connectable

    def is_connected(self) -> bool:
        return self._connected

    def start(self, timeout: float | None = None) -> bool:
        self._connected = self._connectable
        return self._connected

    def close(self) -> None:
        self._connected = False

    def send_message(self, to_jid, body, msg_id=None):   # pragma: no cover
        raise AssertionError("this probe never reaches the wire")


def ask(tools: CvmTools, method: str, params: dict | None = None) -> dict:
    return json.loads(tools.call(method, params or {}, caller=CALLER)["content"][0]["text"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["before", "after", "down"], required=True)
    args = ap.parse_args()

    link = StubLink(connectable=args.mode != "down")
    transport = JmpCheogramTransport(link)
    started: bool | None = None

    if args.mode == "before":
        pass                                    # master: nothing starts the rail
    else:
        from scripts.run_cvm_server import start_transport   # the server's seam
        started = start_transport(transport, timeout=1.0)

    tools = CvmTools(transport, escrow=EscrowStore(":memory:"),
                     contract=ContractContext(npub="npub1testserver"))
    report = {
        "mode": args.mode,
        "started": started,
        "sms.capabilities": ask(tools, "sms.capabilities"),
        "sms.send": ask(tools, "sms.send", {"to": DEST, "body": "probe"}),
    }
    # master has no lifecycle at all — that absence IS the bug being measured.
    getattr(transport, "close", lambda: None)()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
