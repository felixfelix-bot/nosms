"""Run the nosms ContextVM server over real Nostr relays.

Wire layer only: it owns the relay client, the gift-wrap envelope, the CEP-6
announcements, and dispatch into :mod:`app.cvm_rpc` / :mod:`app.cvm_tools`. The
tool logic is pure and lives in the modules under test.

Usage
-----
    # publish the CEP-6 catalog and exit
    python3 scripts/run_cvm_server.py --announce

    # announce, then listen for gift-wrapped tools/call
    python3 scripts/run_cvm_server.py

Environment
-----------
CVM_NSEC            server private key (nsec or hex). Generated + written to
                    --key-file (default .cvm-server.nsec) when absent, so a
                    restart keeps the same npub.
CVM_OWNER_PUBKEYS   comma-separated hex pubkeys that send free (the operator).
NOSMS_MINT_URL      mint every postage token is escrowed against.
NOSMS_TRANSPORT     rail name: jmp | email_gateway | telnyx | whatsapp | fake.
CVM_BTC_USD         live BTC/USD used by sms.pricing for the quote.

Protocol notes measured 2026-10-04/05 (python nostr_sdk 0.44.x):
* ``ClientBuilder().build()`` is SYNC (not awaitable).
* ``client.handle_notifications(handler)`` is the notification LOOP — it never
  returns, so it must run as a background task, never be awaited.
* ``Event.tags()`` returns a ``Tags`` object with no public iterator: read
  values via ``tag.as_vec()`` over ``tags.to_vec()``.
* ``client.gift_wrap(receiver_pubkey, rumor, extra_tags)`` takes an
  ``UnsignedEvent`` rumor (``EventBuilder.build(pubkey)``), not a builder.
* ``UnwrappedGift.rumor`` is a METHOD: ``.rumor()``.
* ``#p`` filters on kind 1059 are unreliable on some relays, so the
  subscription is broad and filtered client-side.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.cvm import (                                              # noqa: E402
    ANNOUNCE_RESOURCES,
    ANNOUNCE_SERVER,
    ANNOUNCE_TOOLS,
    CVM_MESSAGE_KIND,
    GIFT_WRAP_KINDS,
    ContractContext,
    SERVER_ABOUT,
    SERVER_NAME,
    SERVER_VERSION,
    WORKING_RELAYS,
    contract_tags,
)
from app.cvm_rpc import handle_rpc                                 # noqa: E402
from app.cvm_tools import CvmTools                                 # noqa: E402
from app.escrow import EscrowStore                                 # noqa: E402
from app.cashu import MintClient                                   # noqa: E402

CONTRACT_DIR = REPO / "docs" / "cvm"


def load_docs() -> dict[str, str]:
    return {
        "llms": (CONTRACT_DIR / "llms.txt").read_text(),
        "llms-full": (CONTRACT_DIR / "llms-full.txt").read_text(),
    }


def build_transport(name: str):
    """Map a rail name onto a Transport. The rail is the only pluggable part.

    The JMP/Cheogram rail lives in its own module and is wired by config alone,
    so replacing it with Telnyx or the email rail touches nothing here.
    """
    if name in ("email", "email_gateway"):
        from app.transports import EmailGatewayTransport
        from app.transports.email_gateway import load_carrier_map
        # The carrier for a handset is not derivable from the number, and a
        # guessed gateway silently loses mail — so it is explicit config.
        return EmailGatewayTransport(
            smtp_host=os.environ.get("NOSMS_SMTP_HOST", "localhost"),
            smtp_port=int(os.environ.get("NOSMS_SMTP_PORT", "25")),
            sender=os.environ.get("NOSMS_SMTP_SENDER", "sms@orangesync.tech"),
            carrier_map=load_carrier_map(os.environ.get("NOSMS_CARRIER_MAP")))
    if name in ("jmp", "jmp_cheogram"):
        try:
            from app.transports.jmp_cheogram import JmpCheogramTransport
        except ImportError as exc:
            # Refuse to start rather than serve fabricated capability flags: a
            # rail we cannot load cannot be described honestly to a caller.
            raise SystemExit(
                f"rail {name!r} is not available in this checkout ({exc}); "
                f"the JMP/Cheogram transport lands with the sibling M5 card. "
                f"Set NOSMS_TRANSPORT=email_gateway or fake.") from exc
        return JmpCheogramTransport.from_env()
    if name in ("telnyx",):
        from app.transports import TelnyxTransport
        return TelnyxTransport.from_config(os.environ.get(
            "NOSMS_SMS_GATEWAY_PATH", "~/repos/sms-gateway"))
    if name in ("whatsapp", "wa"):
        # ADR-0003. Unknown names must not fall through to the test double: a
        # rail we cannot build is a rail we must not describe.
        from app.transports import WhatsAppTransport
        return WhatsAppTransport.from_env()
    from app.transports import FakeTransport
    return FakeTransport()


def build_mint() -> MintClient | None:
    url = os.environ.get("NOSMS_MINT_URL", "")
    return MintClient(url) if url else None


def tag_values(tag) -> list[str]:
    return tag.as_vec()


def p_tag_of(event) -> str | None:
    for tag in event.tags().to_vec():
        values = tag_values(tag)
        if values and values[0] == "p":
            return values[1]
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relays", nargs="+", default=list(WORKING_RELAYS))
    ap.add_argument("--transport", default=os.environ.get("NOSMS_TRANSPORT", "fake"))
    ap.add_argument("--key-file", default=str(REPO / ".cvm-server.nsec"))
    ap.add_argument("--db", default=os.environ.get("NOSMS_DB_PATH", str(REPO / "cvm-escrow.db")))
    ap.add_argument("--announce", action="store_true",
                    help="publish the CEP-6 catalog and exit")
    ap.add_argument("--announce-only", action="store_true",
                    help="alias for --announce")
    ap.add_argument("--contract-url", default=os.environ.get(
        "NOSMS_CVM_CONTRACT_URL", "https://nosms.orangesync.tech/cvm/llms.txt"))
    ap.add_argument("--btc-usd", type=float,
                    default=float(os.environ["CVM_BTC_USD"]) if os.environ.get("CVM_BTC_USD") else None)
    args = ap.parse_args()
    if args.announce_only:
        args.announce = True

    from nostr_sdk import (ClientBuilder, EventBuilder, Filter, HandleNotification,
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

    npub = keys.public_key().to_bech32()
    owners = {p.strip().lower() for p in
              os.environ.get("CVM_OWNER_PUBKEYS", "").split(",") if p.strip()}
    transport = build_transport(args.transport)
    contract = ContractContext(npub=npub, relays=tuple(args.relays),
                               contract_url=args.contract_url)
    # The JMP capability (jmp.status / jmp.funding / jmp.credentials). It reads
    # its credential from OpenBao and fails closed when it is absent; the server
    # key below is what NIP-44-encrypts a released credential to the caller.
    from scripts.jmp_live import build_jmp_service                     # noqa: E402
    jmp = build_jmp_service(server_secret_hex=keys.secret_key().to_hex(),
                            transcript_path=os.environ.get("NOSMS_JMP_TRANSCRIPT"))
    tools = CvmTools(transport, docs=load_docs(), owner_pubkeys=owners,
                     mint=build_mint(), escrow=EscrowStore(args.db),
                     contract=contract, btc_usd=args.btc_usd, jmp=jmp)

    signer = NostrSigner.keys(keys)
    client = ClientBuilder().signer(signer).build()
    for url in args.relays:
        try:
            await client.add_relay(RelayUrl.parse(url))
        except Exception as exc:                                  # noqa: BLE001
            print(f"[nosms-cvm] add_relay failed {url}: {exc}", file=sys.stderr)
    await client.connect()

    print(f"[nosms-cvm] server npub {npub}")
    print(f"[nosms-cvm] transport   {transport.name} "
          f"(best_effort={transport.capabilities.best_effort}, "
          f"delivery_receipts={transport.capabilities.delivery_receipts})")
    print(f"[nosms-cvm] relays      {', '.join(args.relays)}")
    print(f"[nosms-cvm] price       {tools.sms_pricing()['price']} sats flat "
          f"(quote {tools.sms_pricing()['quote_sats']})")

    async def announce() -> None:
        """Publish the CEP-6 service catalog (replaceable kinds)."""
        server_info = tools.server_announcement()
        payloads = [
            (ANNOUNCE_SERVER, json.dumps(server_info)),
            (ANNOUNCE_TOOLS, json.dumps({"tools": tools.tool_definitions(), **server_info})),
            (ANNOUNCE_RESOURCES, json.dumps({"resources": tools.resource_list()})),
        ]
        tags = contract_tags(contract, tools.capability_tags())
        for kind, content in payloads:
            builder = EventBuilder(Kind(kind), content).tags(
                [Tag.parse(t) for t in tags])
            try:
                out = await client.send_event_builder(builder)
                print(f"[nosms-cvm] announcement kind {kind}: {out}")
            except Exception as exc:                              # noqa: BLE001
                print(f"[nosms-cvm] announce kind {kind} failed: {exc}", file=sys.stderr)

    await announce()
    if args.announce:
        await asyncio.sleep(2)
        await client.shutdown()
        return 0

    class Handler(HandleNotification):
        async def handle(self, relay_url, subscription_id, event):
            if event.kind().as_u16() not in GIFT_WRAP_KINDS:
                return
            if p_tag_of(event) != keys.public_key().to_hex():
                return                    # broad subscription; filter for us here
            try:
                unwrapped = await client.unwrap_gift_wrap(event)
            except Exception as exc:                              # noqa: BLE001
                print(f"[nosms-cvm] unwrap failed: {exc}", file=sys.stderr)
                return
            rumor = unwrapped.rumor()
            if rumor.kind().as_u16() != CVM_MESSAGE_KIND:
                return
            try:
                rpc = json.loads(rumor.content())
            except Exception:                                     # noqa: BLE001
                return
            caller = unwrapped.sender().to_hex()
            print(f"[nosms-cvm] {rpc.get('method')} id={rpc.get('id')} from={caller[:12]}")

            response = handle_rpc(rpc, tools, caller=caller)
            if response is None:              # a true notification: nothing to send
                return

            reply = EventBuilder(Kind(CVM_MESSAGE_KIND), json.dumps(response)).tags(
                [Tag.public_key(unwrapped.sender())]).build(keys.public_key())
            try:
                out = await client.gift_wrap(unwrapped.sender(), reply, [])
                err = " isError" if (response.get("result") or {}).get("isError") else ""
                print(f"[nosms-cvm] replied -> {out}{err}")
            except Exception as exc:                              # noqa: BLE001
                print(f"[nosms-cvm] reply failed: {exc}", file=sys.stderr)

        async def handle_msg(self, relay_url, msg):
            pass

    asyncio.get_running_loop().create_task(client.handle_notifications(Handler()))
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
