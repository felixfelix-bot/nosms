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


#: a second, older keyset — a token assembled across a keyset upgrade (or a
#: hand-built v3 token) can carry proofs from BOTH, with different input fees.
#: Derived from KEYSET rather than written out: a fresh 64-hex literal in the
#: source trips gitleaks' generic-api-key rule, and the value only needs to
#: be a distinct, structurally valid id — not a real keyset.
KEYSET_OLD = "00" + KEYSET[2:]


def make_mixed_token(entries, mint: str = MINT_URL) -> str:
    """A v3 token spanning several keysets: [(keyset, [amounts]), ...]."""
    proofs = [proof(a, keyset=ks) for ks, amounts in entries for a in amounts]
    return encode_token(mint, proofs, unit="sat")


class StubMint:
    """Deterministic offline stand-in for ``app.cashu.MintClient``."""

    def __init__(self, mint_url: str = MINT_URL, fee_ppk: int = 0, keyset: str = KEYSET,
                 max_denomination: int | None = None, fee_ppk_by_keyset=None):
        self.mint_url = mint_url
        self.fee_ppk = int(fee_ppk)
        self.keyset = keyset
        self.max_denomination = max_denomination
        # Per-keyset input fees, exactly like a real mint: a rotated keyset
        # carries its own input_fee_ppk, so a token spanning both is charged
        # with each keyset's rate for that keyset's proofs.
        self._fee_ppk_by_keyset = dict(fee_ppk_by_keyset or {})
        self.spent: set[str] = set()
        self.swap_calls: list[dict] = []
        self.fail_swap: str | None = None

    # --- surface the service uses -------------------------------------------

    def keyset_id(self) -> str:
        return self.keyset

    def keys(self, keyset_id: str | None = None) -> dict[int, str]:
        """The published keyset — amount -> public key, like a real mint.

        ``max_denomination`` models a mint whose keyset stops early: the
        Amperstrand micronuts mint publishes 1..128 only. A swap asking it to
        sign a 2048 fails with KEYSET_NOT_FOUND on the wire, which in tests is
        the AssertionError raised by ``swap`` below.
        """
        top = self.max_denomination
        amounts, bit = [], 1
        while bit <= (top if top else 1 << 30):
            amounts.append(bit)
            bit *= 2
        return {a: "02" + _secrets.token_hex(32) for a in amounts}

    def fee_ppk_for(self, keyset_id: str) -> int:
        return self._fee_ppk_by_keyset.get(str(keyset_id), self.fee_ppk)

    def fee_for(self, proofs) -> int:
        """NUT-02 the way the mints actually charge it (cdk-mintd, nutshell).

        The raw fee is summed per proof from that proof's keyset rate, then a
        SINGLE ceil is applied to the total — not a ceil per keyset. Verified
        against cdk ``calculate_fee`` and nutshell ``get_fees_for_proofs``:
        both compute ``(sum(ppk_i * n_i) + 999) // 1000``.
        """
        raw = sum(self.fee_ppk_for(p["id"]) for p in proofs)
        return (raw + 999) // 1000

    def check_states(self, proofs) -> list[str]:
        return ["SPENT" if p["secret"] in self.spent else "UNSPENT" for p in proofs]

    def swap(self, inputs, output_amounts) -> list[dict]:
        if self.fail_swap:
            raise CashuError("mint_unreachable", self.fail_swap)
        # A real mint rejects outputs it has no keys for (KEYSET_NOT_FOUND).
        if self.max_denomination:
            unsignable = [a for a in output_amounts if a > self.max_denomination]
            if unsignable:
                raise CashuError(
                    "keyset_not_found",
                    f"no keys for denominations {sorted(set(unsignable))} "
                    f"(keyset stops at {self.max_denomination})")
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
