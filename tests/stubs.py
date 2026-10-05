"""Offline doubles for the M1b send path: a stub Cashu mint plus app builders.

Nothing here touches the network. ``StubMint`` implements exactly the calls the
service makes against a real mint (``keyset_id``, ``fee_ppk_for``,
``check_states``, ``swap``) and *enforces the NUT-02 input-fee invariant the way
a real mint does* — outputs must sum to inputs minus the per-input fee. So the
service's fee arithmetic is executed, not assumed.

A v4 (``cashuB``) token minted from the real testnut mint by an independent
client (the ``cashu`` python CLI) lives in ``tests/fixtures/v4_token.txt`` and is
used to pin the decoder against a token this repo did not produce.
"""
from __future__ import annotations

import secrets as _secrets
from math import ceil

from app.cashu import CashuError, encode_token
from app.main import create_app

MINT_URL = "https://testnut.cashu.space"
#: the real, active sat keyset of testnut at the time of writing (cdk-mintd 0.17)
KEYSET = "0184237e63ce3423df7db2dcedc7329cff722a12b90206db53185fc31a4ca5ed96"


def proof(amount: int, secret: str | None = None, keyset: str = KEYSET) -> dict:
    return {
        "amount": int(amount),
        "id": keyset,
        "secret": secret or _secrets.token_hex(32),
        "C": "02" + _secrets.token_hex(32),
    }


def make_token(amounts, mint: str = MINT_URL, keyset: str = KEYSET) -> str:
    """A v3 (``cashuA``) token carrying one proof per amount in `amounts`."""
    return encode_token(mint, [proof(a, keyset=keyset) for a in amounts], unit="sat")


class StubMint:
    """Deterministic offline stand-in for ``app.cashu.MintClient``."""

    def __init__(self, mint_url: str = MINT_URL, fee_ppk: int = 0, keyset: str = KEYSET):
        self.mint_url = mint_url
        self.fee_ppk = int(fee_ppk)
        self.keyset = keyset
        self.spent: set[str] = set()
        self.swap_calls: list[dict] = []
        self.fail_swap: str | None = None

    # --- surface the service uses -------------------------------------------

    def keyset_id(self) -> str:
        return self.keyset

    def fee_ppk_for(self, keyset_id: str) -> int:
        return self.fee_ppk

    def fee_for(self, proofs) -> int:
        """NUT-02: ceil(input_fee_ppk * n_inputs / 1000) sats."""
        return ceil(self.fee_ppk * len(proofs) / 1000)

    def check_states(self, proofs) -> list[str]:
        return ["SPENT" if p["secret"] in self.spent else "UNSPENT" for p in proofs]

    def swap(self, inputs, output_amounts) -> list[dict]:
        if self.fail_swap:
            raise CashuError("mint_unreachable", self.fail_swap)
        total_in = sum(p["amount"] for p in inputs)
        fee = self.fee_for(inputs)
        total_out = sum(output_amounts)
        if total_out != total_in - fee:
            raise AssertionError(
                f"mint fee invariant violated: outputs={total_out} != inputs-fee={total_in - fee}")
        for p in inputs:
            self.spent.add(p["secret"])
        self.swap_calls.append(
            {"inputs": [p["secret"] for p in inputs], "outputs": list(output_amounts)})
        return [proof(a, keyset=self.keyset) for a in output_amounts]

    # --- helpers for assertions ---------------------------------------------

    def spent_secrets(self) -> set[str]:
        return set(self.spent)


def make_app(tmp_path, *, transport=None, mint=None, **overrides):
    """Build the app with a throwaway sqlite file so tests never touch ./nosms.db."""
    cfg = dict(overrides)
    cfg.setdefault("escrow_db", str(tmp_path / "nosms.db"))
    return create_app(config=cfg, transport=transport, mint=mint)
