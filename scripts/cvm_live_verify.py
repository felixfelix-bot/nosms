#!/usr/bin/env python3
"""Live CVM verification: call the nosms server's tools from a second key.

This is the acceptance harness the card asks for. It exercises the real wire
path — MCP JSON-RPC inside a kind-25910 rumor inside a NIP-59 gift wrap, over
real relays — from a key that is NOT the server's (a shared key makes the client
receive its own requests).

Steps, each reported with the elapsed time and the raw reply:

  1. `sms.capabilities`   — must report the rail's REAL flags (best_effort etc.)
  2. `sms.pricing`        — must report the live flat price
  3. `docs`               — must return the contract verbatim (the primary
                            deliverable: contract knowledge with no out-of-band
                            fetch)
  4. `sms.send` UNPAID    — must come back as a VISIBLE isError refusal. Silence
                            here is the bug this harness exists to catch.
  5. `sms.send` PAID      — pays a freshly minted testnut token, so the send is
                            real end to end (escrow swap at the mint, then the
                            rail).
  6. `sms.status`         — the record for the paid send.

Usage
-----
    python3 scripts/cvm_live_verify.py --server-npub npub1... --to +1...
    python3 scripts/cvm_live_verify.py --server-npub npub1... --no-pay

Exit code 0 only if every step produced the expected reply. Prints a JSON
report on stdout.
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

from app.cashu import MintClient, blind_message, split_amounts, unblind  # noqa: E402
from app.cvm import WORKING_RELAYS                                       # noqa: E402

#: how long to wait for one reply before calling it a failure.
REPLY_TIMEOUT = 45.0


def mint_token(mint_url: str, amount: int, unit: str = "sat") -> str:
    """Mint `amount` sats on the spot and return a v3 bearer token.

    Uses the service's own cashu client, so the mint path is the one under test.
    testnut's FakeWallet settles a bolt11 quote by itself; the quote is polled to
    PAID before the mint will sign.
    """
    from app.cashu import encode_token

    client = MintClient(mint_url)
    keyset = client.keyset_id(unit)
    keys = client.keys(keyset)
    amounts = split_amounts(amount)
    outputs, blindings = [], []
    for value in amounts:
        blinded, blinding = blind_message(value, keyset)
        outputs.append(blinded)
        blindings.append(blinding)
    quote = client._post("/v1/mint/quote/bolt11", {"amount": amount, "unit": unit})
    state = quote.get("state")
    for _ in range(30):
        if state == "PAID":
            break
        time.sleep(0.5)
        state = client._get(f"/v1/mint/quote/bolt11/{quote['quote']}").get("state")
    if state != "PAID":
        raise RuntimeError(f"testnut quote never settled (state={state})")
    signed = client._post("/v1/mint/bolt11",
                          {"quote": quote["quote"], "outputs": outputs})
    proofs = [unblind(sig, blinding, keyset, keys[sig["amount"]])
              for sig, blinding in zip(signed["signatures"], blindings)]
    return encode_token(mint_url, proofs)


async def run(args) -> int:
    from nostr_sdk import (ClientBuilder, EventBuilder, Filter, HandleNotification,
                           Keys, Kind, NostrSigner, PublicKey, RelayUrl, Tag)

    server_pk = PublicKey.parse(args.server_npub)
    key_file = pathlib.Path(args.key_file)
    if key_file.exists():
        keys = Keys.parse(key_file.read_text().strip())
    else:
        keys = Keys.generate()
        key_file.write_text(keys.secret_key().to_bech32())
        key_file.chmod(0o600)
    client_npub = keys.public_key().to_bech32()
    if client_npub == args.server_npub:
        print("FATAL: client and server share a key — the client would receive "
              "its own requests.", file=sys.stderr)
        return 2
    print(f"[verify] client npub {client_npub}", file=sys.stderr)

    client = ClientBuilder().signer(NostrSigner.keys(keys)).build()
    for url in args.relays:
        try:
            await client.add_relay(RelayUrl.parse(url))
        except Exception as exc:                                  # noqa: BLE001
            print(f"[verify] add_relay failed {url}: {exc}", file=sys.stderr)
    await client.connect()
    await asyncio.sleep(1.0)

    replies: dict[str, tuple[float, dict]] = {}

    class Handler(HandleNotification):
        async def handle(self, relay_url, subscription_id, event):
            if event.kind().as_u16() not in (1059, 21059):
                return
            p = None
            for tag in event.tags().to_vec():
                values = tag.as_vec()
                if values and values[0] == "p":
                    p = values[1]
                    break
            if p != keys.public_key().to_hex():
                return
            try:
                unwrapped = await client.unwrap_gift_wrap(event)
            except Exception:                                     # noqa: BLE001
                return
            rumor = unwrapped.rumor()
            if rumor.kind().as_u16() != 25910:
                return
            try:
                message = json.loads(rumor.content())
            except Exception:                                     # noqa: BLE001
                return
            replies[str(message.get("id"))] = (time.time(), message)

        async def handle_msg(self, relay_url, msg):
            pass

    asyncio.get_running_loop().create_task(client.handle_notifications(Handler()))
    await client.subscribe(Filter().kinds([Kind(1059), Kind(21059)]))
    await asyncio.sleep(2.0)

    async def call(name: str, arguments: dict | None, call_id: str,
                   method: str = "tools/call") -> dict:
        params = {"name": name, "arguments": arguments or {}} \
            if method == "tools/call" else (arguments or {})
        request = {"jsonrpc": "2.0", "id": call_id, "method": method, "params": params}
        rumor = EventBuilder(Kind(25910), json.dumps(request)).tags(
            [Tag.public_key(server_pk)]).build(keys.public_key())
        await client.gift_wrap(server_pk, rumor, [])
        started = time.time()
        while time.time() - started < REPLY_TIMEOUT:
            if call_id in replies:
                return replies[call_id][1]
            await asyncio.sleep(0.4)
        return {}

    def text_of(reply: dict) -> str:
        result = reply.get("result") or {}
        blocks = result.get("content") or []
        return blocks[0].get("text", "") if blocks else ""

    def parse(reply: dict) -> dict:
        try:
            return json.loads(text_of(reply))
        except Exception:                                         # noqa: BLE001
            return {}

    steps: list[dict] = []
    ok = True

    def record(step: str, reply: dict, expected_error: bool | None = None,
               note: str = "") -> None:
        nonlocal ok
        result = reply.get("result") or {}
        is_error = bool(result.get("isError"))
        got_reply = bool(reply and "result" in reply)
        passed = got_reply and (expected_error is None or is_error == expected_error)
        ok = ok and passed
        payload = parse(reply)
        steps.append({
            "step": step,
            "reply_received": got_reply,
            "isError": is_error,
            "ok": passed,
            "reason": payload.get("reason"),
            "payload": payload if len(json.dumps(payload)) < 1500 else "<large>",
            "note": note,
        })

    # --- 1..3 the free surface ------------------------------------------------
    reply = await call("sms.capabilities", {}, "cap1")
    caps = parse(reply)
    record("sms.capabilities", reply,
           expected_error=False,
           note=f"rail={caps.get('rail')} best_effort={caps.get('best_effort')} "
                f"delivery_receipts={caps.get('delivery_receipts')}")

    reply = await call("sms.pricing", {"to": args.to}, "price1")
    pricing = parse(reply)
    record("sms.pricing", reply, expected_error=False,
           note=f"price={pricing.get('price')} quote={pricing.get('quote_sats')} "
                f"rail_replacement_usd={pricing.get('rail_replacement_usd')}")

    reply = await call("docs", {}, "docs1")
    contract = text_of(reply)
    record("docs", reply, expected_error=False, note=f"{len(contract)} chars returned")
    if contract:
        note = {"chars": len(contract), "starts": contract[:60],
                "has_cap_flags": "best_effort:        true" in contract,
                "has_price": "2900" in contract}
        steps[-1]["contract"] = note

    # --- 4 the refusal must be VISIBLE ---------------------------------------
    reply = await call("sms.send", {"to": args.to, "body": "unpaid probe"},
                       "unpaid1")
    unpaid = parse(reply)
    record("sms.send(unpaid)", reply, expected_error=True,
           note=f"reason={unpaid.get('reason')} cap={unpaid.get('cap')}")

    # --- 5 the paid send ------------------------------------------------------
    if args.no_pay:
        steps.append({"step": "sms.send(paid)", "skipped": True,
                      "note": "--no-pay requested"})
    else:
        price = int(pricing.get("price") or 0)
        token = mint_token(args.mint, price + 1024)      # headroom for the input fee
        replies.pop("paid1", None)
        reply = await call("sms.send", {"to": args.to, "body": args.body,
                                        "cashu_token": token}, "paid1")
        paid = parse(reply)
        record("sms.send(paid)", reply,
               expected_error=False if paid.get("accepted") else None,
               note=f"accepted={paid.get('accepted')} price={paid.get('price_sats')} "
                    f"best_effort={paid.get('best_effort')} "
                    f"delivery_confirmed={paid.get('delivery_confirmed')}")
        steps[-1]["token_minted_sats"] = price + 1024

        # --- 6 status ---------------------------------------------------------
        if paid.get("id"):
            reply = await call("sms.status", {"id": paid["id"]}, "status1")
            record("sms.status", reply, expected_error=False,
                   note=f"status={parse(reply).get('status')}")

    await client.shutdown()

    report = {"ok": ok, "server_npub": args.server_npub, "client_npub": client_npub,
              "relays": list(args.relays), "to": args.to, "steps": steps}
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-npub", required=True)
    ap.add_argument("--relays", nargs="+", default=list(WORKING_RELAYS))
    ap.add_argument("--key-file", default=str(REPO / ".cvm-client.nsec"))
    # No default destination: the live target is a real handset, and this repo
    # is public, so the operator passes --to at run time.
    ap.add_argument("--to", required=True)
    ap.add_argument("--body", default="nosms CVM live verification")
    ap.add_argument("--mint", default=os.environ.get("NOSMS_MINT_URL",
                                                     "https://testnut.cashu.space"))
    ap.add_argument("--no-pay", action="store_true",
                    help="skip the paid send (free-surface probe only)")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
