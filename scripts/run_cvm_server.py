"""Run the nosms ContextVM server over real Nostr relays.

Wire layer only: it owns the relay client, the gift-wrap envelope, the CEP-6
announcements, and dispatch into :mod:`app.cvm_rpc` / :mod:`app.cvm_tools`. The
tool logic is pure and lives in the modules under test.

Lifecycle
---------
``build_transport`` is a pure factory: it constructs a rail and opens nothing.
The rail is brought up by :func:`started_transport` — the process that serves is
the process that holds the session — and closed on every exit path. **Not**
starting it was a live bug (2026-10-11): the JMP link was never connected, so
``sms.capabilities.available`` was ``False`` for the whole process and
``sms.send`` refused ``rail_not_configured`` on every call, before the CEP-8
payment gate was ever reached, while every free tool answered normally.

A rail that cannot come up does not stop the server: the free tools keep
answering, ``sms.capabilities`` honestly reports ``available: false`` and
``sms.send`` answers ``rail_unavailable:<reason>``.

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
NOSMS_RAIL_START_TIMEOUT  seconds to wait for the rail's link (default 45).

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
import contextlib
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
from app.transports.base import rail_refusal_reason                # noqa: E402

CONTRACT_DIR = REPO / "docs" / "cvm"

#: How long to wait for a rail's link (the JMP XMPP session) to come up.
START_TIMEOUT_SECONDS = 45.0


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


def start_transport(transport, timeout: float | None = START_TIMEOUT_SECONDS) -> bool:
    """Bring the rail up BEFORE the first tool call is served.

    See the module docstring: this call is the whole fix for the live
    ``rail_not_configured``-on-every-call bug. The rail answers for itself
    (:meth:`app.transports.base.Transport.start`) — a stateless rail is a no-op,
    the JMP/Cheogram rail starts its long-lived XMPP link.

    A rail that cannot come up is NOT fatal: the free tools keep answering and
    ``sms.capabilities.available`` stays ``false``, which is the truth. The
    reason is printed loudly, because a rail that fails silently is exactly the
    failure mode that hid this bug for a whole session.
    """
    try:
        ready = bool(transport.start(timeout=timeout))
        reason = None if ready else (rail_refusal_reason(transport) or "no reason given")
    except Exception as exc:                       # noqa: BLE001 -- see docstring
        # A rail that RAISES is the same fact as a rail that answers False, and
        # the contract above makes that fact non-fatal. Letting it propagate
        # killed the WHOLE server before it served anything -- taking the free
        # tools, which need no rail at all, down with it.
        ready, reason = False, f"start raised {type(exc).__name__}: {exc}"
    if not ready:
        print(f"[nosms-cvm] RAIL DOWN ({reason}): {transport.name} cannot serve "
              f"sms.send; the free tools stay up and sms.capabilities.available "
              f"is false", file=sys.stderr, flush=True)
    return ready


@contextlib.contextmanager
def started_transport(transport, timeout: float | None = START_TIMEOUT_SECONDS):
    """The rail's lifetime: start it for the run, close it on the way out.

    ``close()`` is registered for every exit path, including the ``--announce``
    early return, because this rail's session is a *personal* line: a process
    that exits leaving its XMPP session behind is a second device on it.
    """
    try:
        start_transport(transport, timeout=timeout)
        yield transport
    finally:
        transport.close()


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
    ap.add_argument("--start-timeout", type=float, default=float(
        os.environ.get("NOSMS_RAIL_START_TIMEOUT", START_TIMEOUT_SECONDS)),
        help="seconds to wait for the rail's link before serving anyway")
    args = ap.parse_args()
    if args.announce_only:
        args.announce = True

    from nostr_sdk import Keys

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
    # build_transport is a pure factory — it opens nothing. The rail is brought
    # up HERE, by the process that will serve from it, and closed on every exit
    # path (see started_transport). Never starting it was the live bug: the JMP
    # link stayed unconnected, so `available` was False for the whole process
    # and `sms.send` refused `rail_not_configured` before the payment gate — on
    # every call, while every free tool answered normally.
    with started_transport(transport, timeout=args.start_timeout):
        return await _serve(args, keys, npub, owners, transport)


async def _serve(args, keys, npub, owners, transport) -> int:
    """Announce, then serve tools until interrupted. The rail is already up."""
    from nostr_sdk import (ClientBuilder, EventBuilder, Filter, HandleNotification,
                           Kind, NostrSigner, RelayUrl, Tag)

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
