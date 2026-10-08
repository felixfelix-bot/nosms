# Test fixtures

## `v4_token_cbor.b64`

The CBOR payload of a **v4** (`cashuB`) token produced by an independent client —
the `cashu` Python CLI, against the live `https://testnut.cashu.space` mint — with
the bearer values redacted. It exists so the token decoder is pinned to framing
this repo did not write: the top-level `t`/`m`/`u` map, a keyset id (`i`) as a
CBOR *byte string*, and each proof's `C` as a CBOR *byte string* while `secret`
is CBOR *text*. A decoder that assumes hex strings everywhere silently
mis-decodes real tokens; `test_cashu.py` fails loudly on this fixture instead.

Why it is stored the way it is:

- A Cashu token is a bearer instrument, so no token string is committed. The
  `cashuB` prefix is stripped and only the base64url payload is stored — the file
  is not a spendable token in any wallet.
- Every proof's `secret` and `C` is replaced in place with a deterministic dummy
  of the same byte length, so the original CBOR framing is preserved byte for
  byte with no re-encoding and no hand-written CBOR encoder.
- The source token this came from is **spent at the mint** (all three proofs
  report `SPENT` on NUT-07), so even the original string is worthless.

Regenerate with:

    python scripts/make_v4_fixture.py <path-to-a-cashuB-token> --burn

The tests never touch the network: `test_cashu.py` reads this file,
`test_mint_client.py` drives the mint client against a scripted HTTP transport,
and `scripts/live_escrow_roundtrip.py` (not a test) does the real network run.
