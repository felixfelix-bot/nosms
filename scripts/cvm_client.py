"""Live CVM client: call the nosms server's tools over real relays.

This is the same wire path Paja's NAP-CVM service uses (kind 25910 gift wrap →
tools/call → gift-wrapped reply), so a green run here proves the server's
answers before the napplet asks for them through the shell.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RELAYS = ["wss://relay.primal.net"]


async def rpc(client, keys, server_pk, name, args, timeout=35):
    from nostr_sdk import EventBuilder, Kind, Tag, Filter

    req = {"jsonrpc": "2.0", "id": name, "method": "tools/call",
           "params": {"name": name, "arguments": args or {}}}
    rumor = EventBuilder(Kind(25910), json.dumps(req)).tags(
        [Tag.public_key(server_pk)]).build(keys.public_key())
    await client.gift_wrap(server_pk, rumor, [])
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-npub", required=True)
    ap.add_argument("--relays", nargs="+", default=RELAYS)
    ap.add_argument("--key-file", default=str(REPO / ".cvm-client.nsec"))
    ap.add_argument("--settlement", default=None,
                    help="CEP-8 settlement receipt for sms.send")
    ap.add_argument("--to", default="+14155550100")
    ap.add_argument("--body", default="nosms live verification")
    args = ap.parse_args()

    from nostr_sdk import (ClientBuilder, Filter, HandleNotification, Keys, Kind,
                           NostrSigner, RelayUrl, PublicKey)

    server_pk = PublicKey.parse(args.server_npub)
    key_file = pathlib.Path(args.key_file)
    if key_file.exists():
        keys = Keys.parse(key_file.read_text().strip())
    else:
        keys = Keys.generate()
        key_file.write_text(keys.secret_key().to_bech32())
        key_file.chmod(0o600)
    print(f"client npub {keys.public_key().to_bech32()}")

    client = ClientBuilder().signer(NostrSigner.keys(keys)).build()
    for url in args.relays:
        await client.add_relay(RelayUrl.parse(url))
    await client.connect()

    replies: dict[str, dict] = {}

    class Handler(HandleNotification):
        async def handle(self, relay_url, subscription_id, event):
            if event.kind().as_u16() not in (1059, 21059):
                return
            p = None
            for tag in event.tags().to_vec():
                v = tag.as_vec()
                if v and v[0] == "p":
                    p = v[1]
                    break
            if p != keys.public_key().to_hex():
                return
            try:
                u = await client.unwrap_gift_wrap(event)
            except Exception:
                return
            rumor = u.rumor()
            if rumor.kind().as_u16() != 25910:
                return
            try:
                msg = json.loads(rumor.content())
            except Exception:
                return
            replies[str(msg.get("id"))] = msg

        async def handle_msg(self, relay_url, msg):
            pass

    asyncio.get_running_loop().create_task(client.handle_notifications(Handler()))
    await client.subscribe(Filter().kinds([Kind(1059), Kind(21059)]))
    await asyncio.sleep(1.5)

    calls = [("sms.capabilities", None), ("sms.pricing", {"to": args.to}), ("docs", None)]
    if args.settlement:
        calls.insert(0, ("sms.send", {"to": args.to, "body": args.body,
                                      "settlement_receipt": args.settlement}))

    for name, a in calls:
        await rpc(client, keys, server_pk, name, a)
        waited = 0.0
        while str(name) not in replies and waited < 40:
            await asyncio.sleep(0.5)
            waited += 0.5
        msg = replies.get(str(name))
        print(f"\n===== {name}  (after {waited:.1f}s) =====")
        if not msg:
            print("NO REPLY")
            continue
        result = msg.get("result") or {}
        for block in result.get("content", []):
            text = block.get("text", "")
            if name == "docs":
                print(f"[{len(text)} chars] {text[:120]!r} ...")
            else:
                print(json.dumps(json.loads(text), indent=2))
        if result.get("isError"):
            print("isError: true")

    await client.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
