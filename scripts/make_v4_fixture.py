#!/usr/bin/env python3
"""Turn a wallet-produced v4 (cashuB) token into a safe test fixture.

A Cashu token is a bearer instrument, so a token string never belongs in git —
the fleet's wallet-secret guard blocks it and it is right to. What the decoder
test actually needs is the *framing* another implementation emits, not the
money. This script therefore:

  1. optionally BURNS the token's proofs at the mint (swaps them for outputs
     whose blinding factors are thrown away), so the source token is provably
     dead even if it ever leaked;
  2. replaces each proof's `secret` and `C` with deterministic dummies of the
     same byte length, in place, so the CBOR framing of the original client is
     preserved byte-for-byte (no re-encoding, no hand-written encoder);
  3. writes only the base64url payload — without the `cashuB` prefix — to the
     fixture file, so what lands in git is not a spendable token string;
  4. re-decodes the result to prove the framing survived.

Usage:
    python scripts/make_v4_fixture.py <token-or-file> [--burn] [--out PATH]
"""
from __future__ import annotations

import base64
import hashlib
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cashu import MintClient, decode_token, fee_for, split_amounts  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = HERE / "tests" / "fixtures" / "v4_token_cbor.b64"


def burn(client: MintClient, proofs: list[dict]) -> dict:
    """Spend the proofs into the void: swap, then discard the new blinding."""
    states = client.check_states(proofs)
    total = sum(p["amount"] for p in proofs)
    fee = fee_for(client.fee_ppk_for(proofs[0]["id"]), len(proofs))
    if all(s == "SPENT" for s in states):
        return {"burned_sats": 0, "burn_fee_sats": 0, "states": states,
                "note": "already spent at the mint; nothing left to burn"}
    client.swap(proofs, split_amounts(total - fee))
    return {"burned_sats": total, "burn_fee_sats": fee,
            "states": client.check_states(proofs)}


def redact(payload: bytes, proofs: list[dict]) -> bytes:
    """Same-length, structure-preserving replacement of secrets and C values.

    `secret` is CBOR text (hex characters); `C` is a raw 33-byte CBOR byte
    string. Both are replaced in place, so the original framing is untouched.
    """

    def dummy(kind: str, index: int, length: int) -> bytes:
        material = b""
        counter = 0
        while len(material) < length:
            material += hashlib.sha256(
                f"nosms-redacted-{kind}-{index}-{counter}".encode()).digest()
            counter += 1
        raw = material[:length]
        if kind == "secret":
            return raw.hex().encode("ascii")[:length]
        return raw

    out = payload
    cursor = 0
    for i, proof in enumerate(proofs):
        for kind, value in (("secret", proof["secret"]), ("C", proof["C"])):
            needle = bytes.fromhex(value) if kind == "C" else value.encode("ascii")
            at = out.find(needle, cursor)
            if at < 0:
                raise SystemExit(f"could not locate proof {i} {kind} in the payload")
            filler = dummy(kind, i, len(needle))
            assert len(filler) == len(needle)
            out = out[:at] + filler + out[at + len(needle):]
            cursor = at + len(needle)
    return out


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    token = args[0] if args else (HERE / "tests/fixtures/v4_token.txt").read_text().strip()
    if os.path.exists(token):
        token = pathlib.Path(token).read_text().strip()
    out_path = DEFAULT_OUT
    if "--out" in argv:
        out_path = pathlib.Path(argv[argv.index("--out") + 1])

    parsed = decode_token(token)
    assert parsed.version == 4, f"expected a v4 (cashuB) token, got v{parsed.version}"
    body = token.split("cashuB", 1)[1]
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))

    report = {"mint": parsed.mint, "unit": parsed.unit, "amount_sats": parsed.amount,
              "proofs": len(parsed.proofs), "keyset": parsed.proofs[0]["id"],
              "payload_bytes": len(payload)}
    if "--burn" in argv:
        report["burn"] = burn(MintClient(parsed.mint), parsed.proofs)

    new_payload = redact(payload, parsed.proofs)
    encoded = base64.urlsafe_b64encode(new_payload).decode("ascii").rstrip("=")
    out_path.write_text(encoded + "\n")

    # the framing must still decode, with the same shape and the same amount
    check = decode_token("cashuB" + out_path.read_text().strip())
    assert check.amount == parsed.amount
    assert len(check.proofs) == len(parsed.proofs)
    assert [p["amount"] for p in check.proofs] == [p["amount"] for p in parsed.proofs]
    assert all(p["secret"] != q["secret"] for p, q in zip(check.proofs, parsed.proofs))
    report["redacted_fixture"] = str(out_path)
    report["verified_redacted_decode"] = True

    import json
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
