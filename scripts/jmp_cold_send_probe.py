#!/usr/bin/env python3
"""Cold outbound verification for the JMP/Cheogram rail (nosms M5).

The single unproven link in the rail: `jmp-sms-listener.py` proves the *reply*
direction (`msg.reply(...).send()`). This probe proves the *cold* direction —
a brand-new `<message>` built from scratch and addressed to
`+<E164>@cheogram.com`, with no reply/thread context — which is the only shape
a service rail can use.

Honesty rules:
  * The destination number is **redacted by construction** in everything this
    script writes: one convention, :func:`mask` (`+1` and the last four digits),
    applied to the `to_masked`/`to_masked_jid` fields *and* to every stanza in
    the evidence record. There is no field carrying an un-masked destination, so
    a published record cannot disagree with the README about how it is masked.
    The un-redacted record is opt-in (`--raw-out`) and belongs outside version
    control.
  * ``accepted`` is a *rail-level* statement: the stanza went out and no error /
    refusal stanza came back inside the wait window. It is **not** a delivery
    receipt — this rail has none (ADR-0002). Handset receipt is operator-verified.
  * Exit 0 = accepted, 2 = refusal/error stanza observed, 3 = connection failed.

Usage
-----
  python scripts/jmp_cold_send_probe.py --list-peers
  python scripts/jmp_cold_send_probe.py --from-inbox --dry-run
  python scripts/jmp_cold_send_probe.py --from-inbox --out evidence/cold-send.json
  python scripts/jmp_cold_send_probe.py --to +1XXXXXXXXXX --body "hi" --out ev.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import uuid

DEFAULT_JID = "hermes-jmp@jabber.fr"
DEFAULT_CRED = "~/.xmpp-hermes-jmp@jabber.fr.json"
DEFAULT_INBOX = "~/.hermes/profiles/manager/state/jmp_inbox.db"
SMS_JID = re.compile(r"^(\+\d{7,15})@cheogram\.com$", re.I)
#: Any E.164 run in a free-text blob (a stanza's to=/from=, or a body).
E164_ANY = re.compile(r"\+\d{7,15}")


def mask(number: str) -> str:
    """The ONE masking convention: `+1` and the last four digits.

    Used for human-readable logs, for the evidence record's destination fields,
    and for redacting stanzas. Every committed artefact uses exactly this shape;
    nothing else may invent a second mask for the same number.
    """
    d = "".join(ch for ch in number if ch.isdigit())
    if len(d) <= 5:
        return "*" * len(d)
    return f"+{d[:1]}{'*' * (len(d) - 5)}{d[-4:]}"


def redact(text: str) -> str:
    """Replace every E.164 run in ``text`` with :func:`mask` of itself.

    Idempotent (a masked number has no 7-digit run left to match), so it can be
    applied to a stanza more than once without mangling it.
    """
    return E164_ANY.sub(lambda m: mask(m.group(0)), text or "")


def e164(number: str) -> str:
    return "+" + "".join(ch for ch in number if ch.isdigit())


def redacted_jid(number: str) -> str:
    """The masked Cheogram JID — the only form of the destination ever published."""
    return f"{mask(number)}@cheogram.com"


def build_evidence(*, jid: str, to_number: str, msg_id: str, body: str,
                   sent: bool, sent_at: float, stanza_xml: str,
                   responses: list[dict]) -> dict:
    """Build the publishable evidence record — redacted by construction.

    There is deliberately no ``to_raw`` field: a field named "raw" must not hold
    a masked value (cold-review finding), and the raw destination must not be
    committed at all. Callers that need the un-redacted record write it
    separately via ``--raw-out``.
    """
    return {
        "probe": "jmp_cold_send",
        "jid": jid,
        "to_masked": mask(to_number),
        "to_masked_jid": redacted_jid(to_number),
        "msg_id": msg_id,
        "body": body,
        "sent": sent,
        "sent_at": sent_at,
        "outbound_stanza_xml": redact(stanza_xml),
        "responses": [{**r, "stanza_xml": redact(r.get("stanza_xml", ""))}
                      for r in responses],
        "accepted": bool(sent and not [
            r for r in responses
            if r.get("kind") == "stream_error" or r.get("type") == "error"]),
        "note": ("accepted == stanza left the client and no error/refusal stanza "
                 "arrived within the wait window; NOT a delivery receipt"),
        "redaction_note": (
            "the destination is masked with mask() (+1 plus the last four digits) "
            "in every field and in every stanza above — this file holds no "
            "un-masked destination. The un-redacted record is kept outside "
            "version control (--raw-out)."),
    }


def raw_evidence(*, jid: str, to_number: str, msg_id: str, body: str,
                 sent: bool, sent_at: float, stanza_xml: str,
                 responses: list[dict]) -> dict:
    """The un-redacted record. Never commit this: it carries the real JID."""
    return {
        "probe": "jmp_cold_send",
        "jid": jid,
        "to_masked": mask(to_number),
        "to_raw": f"{e164(to_number)}@cheogram.com",
        "msg_id": msg_id,
        "body": body,
        "sent": sent,
        "sent_at": sent_at,
        "outbound_stanza_xml": stanza_xml,
        "responses": responses,
        "accepted": bool(sent and not [
            r for r in responses
            if r.get("kind") == "stream_error" or r.get("type") == "error"]),
        "note": ("accepted == stanza left the client and no error/refusal stanza "
                 "arrived within the wait window; NOT a delivery receipt"),
        "WARNING": "UN-REDACTED: contains the real destination JID. Keep out of "
                   "version control; publish the masked record from --out instead.",
    }


def inbox_peers(db_path: str) -> list[tuple[str, str]]:
    """Return [(peer_jid, direction)] from the listener's sqlite inbox."""
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path, timeout=10)
    try:
        return list(con.execute(
            "SELECT peer, direction FROM messages ORDER BY id"))
    finally:
        con.close()


def last_inbound_number(db_path: str) -> str | None:
    """Newest inbound `+<E164>@cheogram.com` peer, normalised to +<E164>.

    Read straight out of the listener's DB column (raw, unmasked): the value was
    written by the listener process, not echoed through a redacting stdout path.
    """
    for peer, direction in reversed(inbox_peers(db_path)):
        if direction != "in":
            continue
        m = SMS_JID.match((peer or "").strip())
        if m:
            return e164(m.group(1))
    return None


def build_cold_stanza(xmpp, to_number: str, body: str, msg_id: str):
    """Build a fresh chat <message> — deliberately NOT a reply()."""
    msg = xmpp.Message()
    msg["to"] = f"{e164(to_number)}@cheogram.com"
    msg["type"] = "chat"
    msg["id"] = msg_id
    msg["body"] = body
    return msg


def _tostring(stanza) -> str:
    """Raw XML. slixmpp stanzas are not ElementTree elements: use ``.xml``."""
    from slixmpp.xmlstream.tostring import tostring
    return tostring(getattr(stanza, "xml", stanza))


async def run(args) -> int:
    import slixmpp  # imported here so --help / --list-peers work without it

    cred = json.load(open(os.path.expanduser(args.cred)))
    jid, password = cred["jid"], cred["password"]

    to_number = args.to or last_inbound_number(os.path.expanduser(args.inbox))
    if not to_number:
        print(json.dumps({"event": "error",
                          "err": "no destination: pass --to or seed the inbox"}))
        return 3
    await asyncio.sleep(0)

    body = args.body or (
        f"nosms cold-send probe {args.nonce} — unsolicited outbound, "
        f"not a reply. {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    msg_id = uuid.uuid4().hex

    x = slixmpp.ClientXMPP(jid, password)
    x.register_plugin("xep_0030")
    x.register_plugin("xep_0199")
    if args.resource:
        x.boundjid.resource = args.resource
    connected = asyncio.get_event_loop().create_future()
    responses: list[dict] = []

    def on_start(_event):
        if not connected.done():
            connected.set_result(True)

    def on_message(msg):
        responses.append({
            "kind": "message",
            "from": mask(msg["from"].bare),
            "from_domain": msg["from"].bare.split("@")[-1],
            "type": msg["type"],
            "body": msg["body"],
            "stanza_xml": _tostring(msg),
        })

    def on_stream_error(event):
        responses.append({"kind": "stream_error", "body": str(event),
                          "stanza_xml": ""})

    x.add_event_handler("session_start", on_start)
    x.add_event_handler("message", on_message)
    x.add_event_handler("stream_error", on_stream_error)
    x.add_event_handler("failed_auth", lambda e: connected.done()
                        or connected.set_result(False))

    x.connect()
    try:
        ok = await asyncio.wait_for(connected, timeout=args.timeout)
    except asyncio.TimeoutError:
        ok = False
    if not ok:
        print(json.dumps({"event": "connect_failed", "jid": jid}))
        return 3

    x.send_presence()
    await x.get_roster()

    msg = build_cold_stanza(x, to_number, body, msg_id)
    stanza_xml = _tostring(msg)

    sent_at = time.time()
    if args.dry_run:
        sent = False
        detail = "dry-run: stanza built, not sent"
    else:
        msg.send()          # slixmpp 1.17: stanza.send() is sync (returns once queued)
        sent = True
        detail = "sent; awaiting error/refusal stanzas"
    print(json.dumps({"event": "cold_sent" if sent else "cold_dry_run",
                      "to": mask(to_number), "msg_id": msg_id,
                      "body_len": len(body), "detail": detail}))

    if sent:
        await asyncio.sleep(args.wait)

    refusal = [r for r in responses if r["kind"] == "stream_error"
               or r["type"] == "error"]
    fields = dict(jid=jid, to_number=to_number, msg_id=msg_id, body=body,
                  sent=sent, sent_at=sent_at, stanza_xml=stanza_xml,
                  responses=responses)
    evidence = build_evidence(**fields)
    if args.out:
        out = os.path.expanduser(args.out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as fh:
            json.dump(evidence, fh, indent=2)
        print(json.dumps({"event": "evidence_written", "path": out,
                          "accepted": evidence["accepted"],
                          "responses": len(responses)}))
    if args.raw_out:
        raw = os.path.expanduser(args.raw_out)
        os.makedirs(os.path.dirname(raw) or ".", exist_ok=True)
        with open(raw, "w") as fh:
            json.dump(raw_evidence(**fields), fh, indent=2)
        os.chmod(raw, 0o600)
        print(json.dumps({"event": "raw_evidence_written", "path": raw,
                          "warning": "un-redacted: keep out of version control"}))
    print(json.dumps({"event": "result", "accepted": evidence["accepted"],
                      "refusals": len(refusal),
                      "response_kinds": [r["kind"] for r in responses]}))
    try:
        x.disconnect()
    except Exception:                                     # noqa: BLE001
        pass
    return 0 if evidence["accepted"] else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", help="destination E.164 (default: newest inbox peer)")
    ap.add_argument("--from-inbox", action="store_true",
                    help="explicitly source the destination from the inbox DB")
    ap.add_argument("--body")
    ap.add_argument("--nonce", default=uuid.uuid4().hex[:8])
    ap.add_argument("--out", help="publishable evidence JSON path (redacted)")
    ap.add_argument("--raw-out",
                    help="un-redacted evidence JSON path — never commit it")
    ap.add_argument("--jid", default=DEFAULT_JID)
    ap.add_argument("--cred", default=DEFAULT_CRED)
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--resource", default="coldprobe",
                    help="XMPP resource (default: coldprobe, never the listener's)")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--wait", type=float, default=20.0,
                    help="seconds to wait for an error/refusal stanza")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-peers", action="store_true")
    args = ap.parse_args()

    if args.list_peers:
        rows = inbox_peers(os.path.expanduser(args.inbox))
        print(json.dumps({"inbox": os.path.expanduser(args.inbox),
                          "count": len(rows),
                          "peers": [{"peer": redact(p) if SMS_JID.match(p or "")
                                     else p,
                                     "direction": d} for p, d in rows]}))
        return 0

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
