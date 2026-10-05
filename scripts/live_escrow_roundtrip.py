#!/usr/bin/env python3
"""Live escrow round-trip against a real Cashu mint (testnut by default).

Proves, with network and real cryptography, that the escrow path this service
uses actually works against a mint:

  1. real funds are minted on the spot (testnut's FakeWallet marks any bolt11
     invoice paid, so no real sats are involved) — the proof of work there is
     that our blind/unblind construction round-trips through the mint's own
     signer;
  2. the sender's proofs check as UNSPENT (NUT-07);
  3. they are swapped for fresh proofs (NUT-03) — the escrow itself;
  4. the fresh proofs are *spent again at the mint*, the only real proof that the
     unblinding was correct and the value is genuinely held;
  5. the original proofs are now SPENT: the sender's token is dead, which is what
     makes this escrow rather than a promise;
  6. a refund token is built from the re-captured proofs and decodes back;
  7. the mint's NUT-02 input fee is measured, not assumed.

Usage:  python scripts/live_escrow_roundtrip.py [mint_url]
Exit code 0 only if every step succeeded. Prints a JSON report on stdout.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cashu import (  # noqa: E402
    MintClient, blind_message, decode_token, encode_token, fee_for, split_amounts, unblind,
)

FUND_SATS = 512 + 256            # minted at the start; 500 postage + change
POSTAGE = 500                    # what a +49 / unknown-prefix send costs


def mint_funds(client: MintClient, amount: int) -> tuple[dict, list[dict]]:
    """Mint `amount` sats, blinding the outputs ourselves (NUT-04)."""
    keyset = client.keyset_id()
    keys = client.keys(keyset)
    amounts = split_amounts(amount)
    outputs, blindings = [], []
    for value in amounts:
        blinded, blinding = blind_message(value, keyset)
        outputs.append(blinded)
        blindings.append(blinding)
    quote = client._post("/v1/mint/quote/bolt11", {"amount": amount, "unit": "sat"})
    # testnut's FakeWallet settles a quote on its own schedule; the invoice must
    # be polled to PAID before the mint will sign anything.
    state = quote.get("state")
    for _ in range(20):
        if state == "PAID":
            break
        time.sleep(0.5)
        state = client._get(f"/v1/mint/quote/bolt11/{quote['quote']}").get("state")
    assert state == "PAID", f"quote never settled: {state}"
    signed = client._post("/v1/mint/bolt11", {"quote": quote["quote"], "outputs": outputs})
    proofs = []
    for sig, blinding in zip(signed["signatures"], blindings):
        proofs.append(unblind(sig, blinding, keyset, keys[sig["amount"]]))
    return quote, proofs


def main(mint_url: str) -> int:
    steps: list[dict] = []
    client = MintClient(mint_url)

    # --- 1. fund a wallet at the mint --------------------------------------
    quote, minted_proofs = mint_funds(client, FUND_SATS)
    assert sum(p["amount"] for p in minted_proofs) == FUND_SATS
    steps.append({"step": "mint_funds", "status": "ok", "quote": quote["quote"],
                  "invoice": (quote.get("request") or "")[:45] + "...",
                  "proofs": len(minted_proofs), "amount_sats": FUND_SATS})

    parsed = decode_token(encode_token(mint_url, minted_proofs))
    assert parsed.amount == FUND_SATS, parsed.amount

    # --- 2. NUT-07: the sender's proofs must be unspent ---------------------
    before = client.check_states(minted_proofs)
    assert all(s == "UNSPENT" for s in before), before
    steps.append({"step": "check_state_before", "status": "ok", "states": before})

    # --- 3. escrow: swap them for proofs the service holds ------------------
    keyset = client.keyset_id()
    ppk = client.fee_ppk_for(keyset)
    fee = fee_for(ppk, len(minted_proofs))
    net = parsed.amount - fee
    assert net >= POSTAGE, f"minted {parsed.amount}, fee {fee}, net {net} < postage {POSTAGE}"
    escrow_amounts = split_amounts(POSTAGE)
    change_amounts = split_amounts(net - POSTAGE)
    captured = client.swap(minted_proofs, escrow_amounts + change_amounts)
    escrow_proofs = captured[:len(escrow_amounts)]
    change_proofs = captured[len(escrow_amounts):]
    steps.append({"step": "escrow_swap", "status": "ok", "keyset": keyset,
                  "input_fee_ppk": ppk, "mint_fee_sats": fee, "net_sats": net,
                  "postage_sats": sum(p["amount"] for p in escrow_proofs),
                  "change_sats": sum(p["amount"] for p in change_proofs),
                  "held_sats": sum(p["amount"] for p in captured),
                  "escrow_token": encode_token(mint_url, escrow_proofs)[:14] + "..."})

    # --- 4. the sender's original token is now dead -------------------------
    after = client.check_states(minted_proofs)
    assert all(s == "SPENT" for s in after), after
    steps.append({"step": "check_state_after", "status": "ok", "states": after})

    # --- 5. the escrowed proofs are genuinely spendable ---------------------
    fresh_states = client.check_states(escrow_proofs + change_proofs)
    assert all(s == "UNSPENT" for s in fresh_states), fresh_states
    total = sum(p["amount"] for p in captured)
    respend_fee = fee_for(ppk, len(captured))
    respent = client.swap(captured, split_amounts(total - respend_fee))
    steps.append({"step": "escrow_proofs_are_spendable", "status": "ok",
                  "fresh_states": fresh_states, "held_sats": total,
                  "respent_total_sats": sum(p["amount"] for p in respent),
                  "respent_fee_sats": respend_fee, "respent_proofs": len(respent)})

    # --- 6. the refund token a payer would receive --------------------------
    refund_token = encode_token(mint_url, respent)
    steps.append({"step": "refund_token", "status": "ok",
                  "refund_sats": decode_token(refund_token).amount,
                  "token_prefix": refund_token[:14] + "..."})

    # --- 7. the mint publishes a key for every denomination we escrowed -----
    keys = client.keys(keyset)
    assert all(a in keys for a in escrow_amounts + change_amounts)
    steps.append({"step": "mint_pubkeys", "status": "ok",
                  "amounts_available": sorted(keys)[:12],
                  "has_postage_denomination": True})

    print(json.dumps({"ok": True, "mint": mint_url, "steps": steps}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "https://testnut.cashu.space"))
