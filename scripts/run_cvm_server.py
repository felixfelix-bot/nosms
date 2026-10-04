"""Run the nosms ContextVM server over real Nostr relays.

Wire layer only: it owns the relay client, the gift-wrap envelope, the CEP-6
announcements and the dispatch into :class:`app.cvm_tools.CvmTools`. The tool
logic (pricing, pricing table, payment gating, refunds, docs) is pure and lives
in the module under test.

Usage
-----
    CVM_NSEC=<nsec> python3 scripts/run_cvm_server.py --relays wss://relay.primal.net

Environment
-----------
CVM_NSEC           server private key (nsec or hex). Generated + written to
                   --key-file (default .cvm-server.nsec) when absent, so a
                   restart keeps the same npub.
CVM_OWNER_PUBKEYS  comma-separated hex pubkeys that send free.

Protocol notes measured on 2026-10-04 (python nostr_sdk 0.44.x):
* ``ClientBuilder().build()`` is SYNC (not awaitable).
* ``client.handle_notifications(handler)`` is the notification LOOP — it never
  returns, so it must run as a background task, never be awaited.
* ``Event.tags()`` returns a ``Tags`` object with **no** public value accessor
  and no iterator: read values via ``tag.as_vec()`` on ``tags.to_vec()``.
* ``client.gift_wrap(receiver_pubkey, rumor, extra_tags)`` takes an
  ``UnsignedEvent`` rumor (``EventBuilder.build(pubkey)``), not a builder.
* ``UnwrappedGift.rumor`` is a METHOD: ``.rumor()``.
* ``#p`` tag filters on kind 1059 are unreliable on some relays, so the
  subscription is broad and filtered client-side.
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

from app.cvm_tools import CvmTools                      # noqa: E402
from app.transports import EmailGatewayTransport, FakeTransport  # noqa: E402

CONTRACT_DIR = REPO / "napplet" / "src" / "contract"
DEFAULT_RELAYS = ["wss://relay.primal.net"]
CVM_KIND = 25910
ANNOUNCE_SERVER = 11316
ANNOUNCE_TOOLS = 11317
GIFT_WRAP_KINDS = (1059, 21059)
SERVER_INFO = {
    "name": "nosms",
    "about": "Send SMS on behalf of others over ContextVM. Agent-first: pay per "
             "send in Cashu. No account, no API key, no signup.",
    "version": "0.1.0",
}


def load_docs() -> dict[str, str]:
    return {
        "llms": (CONTRACT_DIR / "llms.txt").read_text(),
        "llms-full": (CONTRACT_DIR / "llms-full.txt").read_text(),
    }


def build_transport(name: str):
    if name in ("email", "email_gateway"):
        return EmailGatewayTransport()
    if name == "fake":
        return FakeTransport()
    raise SystemExit(f"unknown transport {name!r} (v1 wires email|fake; JMP is the "
                     "operator's own rail and is not wired as service infrastructure)")


def tag_values(tag) -> list[str]:
    return tag.as_vec()


def p_tag_of(event) -> str | None:
    for tag in event.tags().to_vec():
        v = tag_values(tag)
        if v and v[0] == "p":
            return v[1]
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relays", nargs="+", default=DEFAULT_RELAYS)
    ap.add_argument("--transport", default=os.environ.get("NOSMS_TRANSPORT", "email"))
    ap.add_argument("--key-file", default=str(REPO / ".cvm-server.nsec"))
    ap.add_argument("--announce", action="store_true",
                    help="publish the CEP-6 catalog and exit")
    ap.add_argument("--contract-url", default="https://nosms.orangesync.tech/llms.txt")
    args = ap.parse_args()

    from nostr_sdk import (Client, ClientBuilder, EventBuilder, Filter, HandleNotification,
                           Keys, Kind, NostrSigner, RelayUrl, Tag)

    key_file = pathlib.Path(args.key_file)
    if os.environ.get("CVM_NSEC"):
        keys = Keys.parse(os.environ["CVM_NSEC"])
    elif key_file.exists():
        keys = Keys.parse(key_file.read_text().strip())
    else:
        keys = Keys.generate()
        key_file.write_text(keys.secret_key().to_bech32())
        key_file.chmod(0o600)
        print(f"[nosms-cvm] generated a server key -> {key_file}")

    owners = {p.strip().lower() for p in
              os.environ.get("CVM_OWNER_PUBKEYS", "").split(",") if p.strip()}
    transport = build_transport(args.transport)
    tools = CvmTools(transport, docs=load_docs(), owner_pubkeys=owners)

    signer = NostrSigner.keys(keys)
    client = ClientBuilder().signer(signer).build()
    for url in args.relays:
        try:
            await client.add_relay(RelayUrl.parse(url))
        except Exception as exc:                                  # noqa: BLE001
            print(f"[nosms-cvm] add_relay failed {url}: {exc}", file=sys.stderr)
    await client.connect()

    npub = keys.public_key().to_bech32()
    print(f"[nosms-cvm] server npub {npub}")
    print(f"[nosms-cvm] transport   {transport.name} "
          f"(best_effort={transport.capabilities.best_effort}, "
          f"delivery_receipts={transport.capabilities.delivery_receipts})")
    print(f"[nosms-cvm] relays      {', '.join(args.relays)}")

    async def announce() -> None:
        """Publish the CEP-6 service catalog (replaceable kinds)."""
        contract = {
            "contract_url": args.contract_url,
            "docs_tool": "docs",
            "tools": [t["name"] for t in tools.tool_definitions()],
            "capabilities": tools.sms_capabilities(),
        }
        tag_sets = [
            (ANNOUNCE_SERVER, json.dumps(SERVER_INFO)),
            (ANNOUNCE_TOOLS, json.dumps({
                "tools": tools.tool_definitions(), **contract})),
        ]
        for kind, content in tag_sets:
            builder = EventBuilder(Kind(kind), content).tags([
                Tag.parse(["d", "nosms"]),
                Tag.parse(["name", SERVER_INFO["name"]]),
                Tag.parse(["t", "sms"]),
                Tag.parse(["t", "contextvm"]),
                Tag.parse(["contract", args.contract_url]),
                *[Tag.parse(t) for t in tools.capability_tags()],
            ])
            out = await client.send_event_builder(builder)
            print(f"[nosms-cvm] announcement kind {kind}: {out}")

    if args.announce:
        await announce()
        await asyncio.sleep(2)
        await client.shutdown()
        return 0

    await announce()

    class Handler(HandleNotification):
        async def handle(self, relay_url, subscription_id, event):
            if event.kind().as_u16() not in GIFT_WRAP_KINDS:
                return
            if p_tag_of(event) != keys.public_key().to_hex():
                return            # broad subscription; filter for us here
            try:
                unwrapped = await client.unwrap_gift_wrap(event)
            except Exception as exc:                              # noqa: BLE001
                print(f"[nosms-cvm] unwrap failed: {exc}", file=sys.stderr)
                return
            rumor = unwrapped.rumor()
            if rumor.kind().as_u16() != CVM_KIND:
                return
            try:
                rpc = json.loads(rumor.content())
            except Exception:                                     # noqa: BLE001
                return
            method = rpc.get("method")
            rpc_id = rpc.get("id")
            caller = unwrapped.sender().to_hex()
            print(f"[nosms-cvm] {method} id={rpc_id} from={caller[:12]}")

            if method == "tools/list":
                result = {"tools": tools.tool_definitions()}
            elif method == "initialize":
                result = {"protocolVersion": "2025-11-25",
                          "serverInfo": {"name": "nosms", "version": "0.1.0"},
                          "capabilities": {"tools": {}}}
            elif method == "tools/call":
                params = rpc.get("params") or {}
                result = tools.call(params.get("name", ""),
                                    params.get("arguments") or {}, caller=caller)
            else:
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"reason": "unsupported method", "hint": f"Unknown method {method!r}."})}],
                    "isError": True}

            response = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
            reply = EventBuilder(Kind(CVM_KIND), json.dumps(response)).tags(
                [Tag.public_key(unwrapped.sender())]).build(keys.public_key())
            try:
                out = await client.gift_wrap(unwrapped.sender(), reply, [])
                print(f"[nosms-cvm] replied -> {out}")
            except Exception as exc:                              # noqa: BLE001
                print(f"[nosms-cvm] reply failed: {exc}", file=sys.stderr)

        async def handle_msg(self, relay_url, msg):
            pass

    handler = Handler()
    # The notification loop never returns: run it as a task.
    asyncio.get_running_loop().create_task(client.handle_notifications(handler))
    await client.subscribe(Filter().kinds([Kind(1059), Kind(21059)]))
    print("[nosms-cvm] listening (broad kind 1059/21059, p-tag filtered client-side)")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await client.shutdown()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
