"""Minimal Cashu client for nosms escrow (NUT-00/01/02/03/07).

Only what escrow needs, implemented directly against the mint's HTTP API, so the
service keeps a small dependency surface (``coincurve`` + ``httpx``, both already
required).

* ``MintClient`` — keyset discovery, NUT-07 proof states, NUT-03 swap.
* ``decode_token`` / ``encode_token`` — bearer tokens in both encodings:
  v3 (``cashuA``, base64url JSON) and v4 (``cashuB``, base64url CBOR).
* ``blind_message`` / ``unblind`` — the NUT-00 blind-signature construction.

**Why a swap and not just a check.** Holding an un-swapped token is not escrow:
the sender keeps the secrets and can spend them at the mint at any time. Every
accepted send therefore *swaps* the sender's proofs at the mint for fresh proofs
the service controls, so the value is genuinely captured. The mint's NUT-02
input fee (testnut's sat keyset charges ``input_fee_ppk=100``, i.e. 0.1 sat per
input proof) is borne by the sender: it is deducted from the received amount
before postage is computed.

Known risk, stated rather than hidden: if the swap request times out *after* the
mint processed it, the sender's proofs are spent while the service holds nothing.
The service then answers ``mint_error`` and the sender's token is burned. v1
accepts this (it is the standard Cashu failure mode and needs NUT-09/19 recovery
to close); it never silently keeps sats without sending.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets as _secrets
import struct
from dataclasses import dataclass, field
from math import ceil

import httpx
from coincurve import PrivateKey, PublicKey

#: NUT-00 domain separation tag for hash_to_curve.
HASH_TO_CURVE_DOMAIN = b"Secp256k1_HashToCurve_Cashu_"

#: secp256k1 group order, for scalar negation.
CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

#: testnut's sat keyset fee, in parts-per-thousand per input proof (NUT-02).
MINT_FEE_PPK_DEFAULT = 100

V3_PREFIX = "cashuA"
V4_PREFIX = "cashuB"

_HINTS = {
    "token_invalid": "The Cashu token is malformed; expected a 'cashuA...' (v3) or 'cashuB...' (v4) string.",
    "token_unsupported": "That token encoding is not supported; re-send it as a v3 ('cashuA...') token.",
    "token_empty": "The Cashu token carries no proofs.",
    "mint_unreachable": "The Cashu mint did not answer; retry.",
    "mint_error": "The Cashu mint refused the request.",
}


class CashuError(Exception):
    """A Cashu failure carrying the X-Reason token and an X-Hint sentence."""

    def __init__(self, reason: str, hint: str | None = None):
        self.reason = reason
        self.hint = hint or _HINTS.get(reason, "The Cashu request failed.")
        super().__init__(f"{reason}: {self.hint}")


@dataclass
class Token:
    mint: str
    unit: str = "sat"
    proofs: list[dict] = field(default_factory=list)
    memo: str | None = None
    version: int = 3

    @property
    def amount(self) -> int:
        return sum(int(p["amount"]) for p in self.proofs)


# --------------------------------------------------------------------------- #
# crypto (NUT-00)
# --------------------------------------------------------------------------- #

def hash_to_curve(message: bytes) -> bytes:
    """Map a message to a secp256k1 point, exactly as NUT-00 specifies.

    Note the two-stage hash: the counter is appended to ``sha256(domain ||
    message)``, not to ``domain || message``. Getting this wrong produces proofs
    a real mint rejects with "Token not verified" while every local check still
    passes — it is not detectable offline, only against a mint.
    """
    msg_to_hash = hashlib.sha256(HASH_TO_CURVE_DOMAIN + message).digest()
    for counter in range(2 ** 16):
        digest = hashlib.sha256(msg_to_hash + struct.pack("<I", counter)).digest()
        try:
            return PublicKey(b"\x02" + digest).format(compressed=True)
        except ValueError:
            continue
    raise CashuError("token_invalid", "Could not map the proof secret to a curve point.")


def blind_message(amount: int, keyset_id: str) -> tuple[dict, dict]:
    """Create one NUT-00 blinded message for `amount`.

    Returns ``(blinded_message, blinding_factor)``; the blinding factor must be
    kept until the mint's signature comes back, to unblind it.
    """
    secret = _secrets.token_hex(32)
    y = PublicKey(hash_to_curve(secret.encode("utf-8")))
    r = PrivateKey(_secrets.token_bytes(32))
    b_ = PublicKey.combine_keys([y, r.public_key])
    blinded = {"amount": int(amount), "id": keyset_id, "B_": b_.format(compressed=True).hex()}
    return blinded, {"secret": secret, "r": r.to_int()}


def unblind(signature: dict, blinding: dict, keyset_id: str, mint_pubkey: str) -> dict:
    """Turn a blind signature into a spendable proof.

    NUT-00: ``C_ = k*(Y + r*G) = k*Y + r*K``, so ``C = C_ - r*K`` where ``K`` is
    the mint's public key *for this amount*. Subtracting ``r*G`` instead — the
    obvious-looking mistake — yields a proof the mint will reject.
    """
    try:
        c_ = PublicKey(bytes.fromhex(signature["C_"]))
        key = PublicKey(bytes.fromhex(mint_pubkey))
    except (KeyError, ValueError):
        raise CashuError("mint_error", "The mint's signature was not a valid curve point.") from None
    neg_r = (CURVE_ORDER - int(blinding["r"])) % CURVE_ORDER
    c = PublicKey.combine_keys([c_, key.multiply(neg_r.to_bytes(32, "big"))])
    return {
        "amount": int(signature.get("amount", 0)),
        "id": signature.get("id", keyset_id),
        "secret": blinding["secret"],
        "C": c.format(compressed=True).hex(),
    }


# --------------------------------------------------------------------------- #
# amounts (NUT-01/02)
# --------------------------------------------------------------------------- #

def split_amounts(total: int) -> list[int]:
    """Decompose `total` into the powers of two a mint has keys for."""
    out: list[int] = []
    bit = 1
    remaining = int(total)
    while remaining > 0:
        if remaining & 1:
            out.append(bit)
        remaining >>= 1
        bit <<= 1
    return sorted(out, reverse=True)


def fee_for(ppk: int, n_inputs: int) -> int:
    """NUT-02 input fee: ceil(input_fee_ppk * n_inputs / 1000) sats."""
    if ppk <= 0 or n_inputs <= 0:
        return 0
    return ceil(int(ppk) * int(n_inputs) / 1000)


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #

def _b64url_decode(body: str) -> bytes:
    padded = body + "=" * (-len(body) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise CashuError("token_invalid") from None


def encode_token(mint: str, proofs: list[dict], unit: str = "sat",
                 memo: str | None = None) -> str:
    """Encode proofs as a v3 (``cashuA``) token — the most widely accepted form."""
    proofs = list(proofs or [])
    if not proofs:
        raise CashuError("token_empty")
    entry = {"mint": mint,
             "proofs": [{"amount": int(p["amount"]), "id": p["id"], "secret": p["secret"],
                         "C": p["C"]} for p in proofs]}
    payload: dict = {"token": [entry], "unit": unit}
    if memo:
        payload["memo"] = memo
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return V3_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: str) -> Token:
    """Decode a v3 or v4 Cashu token. Raises ``CashuError`` on anything else."""
    if not token or not isinstance(token, str):
        raise CashuError("token_invalid")
    token = token.strip()
    if token.startswith(V3_PREFIX):
        return _decode_v3(token[len(V3_PREFIX):])
    if token.startswith(V4_PREFIX):
        return _decode_v4(token[len(V4_PREFIX):])
    raise CashuError("token_unsupported",
                     "Unrecognised Cashu token prefix; expected 'cashuA' or 'cashuB'.")


def _decode_v3(body: str) -> Token:
    raw = _b64url_decode(body)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CashuError("token_invalid") from None
    if not isinstance(payload, dict):
        raise CashuError("token_invalid")
    entries = payload.get("token")
    if not isinstance(entries, list) or not entries:
        raise CashuError("token_invalid")
    mints = {str(e.get("mint", "")).rstrip("/") for e in entries if isinstance(e, dict)}
    if len(mints) != 1 or not mints or not next(iter(mints)):
        raise CashuError("token_unsupported",
                         "A token carrying more than one mint is not supported; send one mint per token.")
    proofs = []
    for entry in entries:
        for p in entry.get("proofs") or []:
            proofs.append(_clean_proof(p))
    if not proofs:
        raise CashuError("token_empty")
    return Token(mint=next(iter(mints)), unit=str(payload.get("unit") or "sat"),
                 proofs=proofs, memo=payload.get("memo"), version=3)


def _decode_v4(body: str) -> Token:
    raw = _b64url_decode(body)
    value, _ = _cbor_decode(raw, 0)
    if not isinstance(value, dict):
        raise CashuError("token_invalid")
    mint = str(value.get("m") or "").rstrip("/")
    if not mint:
        raise CashuError("token_invalid")
    proofs = []
    for entry in value.get("t") or []:
        if not isinstance(entry, dict):
            raise CashuError("token_invalid")
        keyset = entry.get("i")
        for p in entry.get("p") or []:
            if not isinstance(p, dict):
                raise CashuError("token_invalid")
            proofs.append(_clean_proof({
                "amount": p.get("a"), "id": keyset, "secret": p.get("s"),
                "C": p.get("c"), "dleq": p.get("d"),
            }))
    if not proofs:
        raise CashuError("token_empty")
    return Token(mint=mint, unit=str(value.get("u") or "sat"), proofs=proofs,
                 memo=value.get("d"), version=4)


def _as_hex(value) -> str:
    """CBOR (v4) carries proofs' C and keyset id as byte strings; v3 as text."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)


def _clean_proof(p: dict) -> dict:
    if not isinstance(p, dict):
        raise CashuError("token_invalid")
    try:
        amount = int(p["amount"])
    except (KeyError, TypeError, ValueError):
        raise CashuError("token_invalid") from None
    c = _as_hex(p.get("C"))
    secret = _as_hex(p.get("secret"))
    keyset = _as_hex(p.get("id"))
    if amount <= 0 or not c or not secret or not keyset:
        raise CashuError("token_invalid")
    return {"amount": amount, "id": keyset, "secret": secret, "C": c}


def _cbor_decode(data: bytes, offset: int):
    """Minimal CBOR decoder: uint, nint, bytes, text, array, map.

    Enough for NUT-00 v4 tokens, and deliberately strict anywhere else — a token
    that needs CBOR tags or floats is not a token this service will guess at.
    """
    if offset >= len(data):
        raise CashuError("token_invalid")
    initial = data[offset]
    offset += 1
    major, info = initial >> 5, initial & 0x1F
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        width = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        chunk = data[offset:offset + width]
        if len(chunk) != width:
            raise CashuError("token_invalid")
        value = int.from_bytes(chunk, "big")
        offset += width
    elif info == 31:
        value = None                      # indefinite length
    else:
        raise CashuError("token_unsupported", f"Unsupported CBOR header 0x{initial:02x}.")

    if major == 0:
        return value, offset
    if major == 1:
        return -1 - value, offset
    if major == 2:
        if value is None:
            chunks = []
            while offset < len(data) and data[offset] != 0xFF:
                chunk, offset = _cbor_decode(data, offset)
                chunks.append(chunk)
            return b"".join(chunks), offset + 1
        if offset + value > len(data):
            raise CashuError("token_invalid")
        return data[offset:offset + value], offset + value
    if major == 3:
        if value is None:
            parts = []
            while offset < len(data) and data[offset] != 0xFF:
                chunk, offset = _cbor_decode(data, offset)
                parts.append(chunk)
            return "".join(parts), offset + 1
        if offset + value > len(data):
            raise CashuError("token_invalid")
        try:
            return data[offset:offset + value].decode("utf-8"), offset + value
        except UnicodeDecodeError:
            raise CashuError("token_invalid") from None
    if major == 4:
        out = []
        if value is None:
            while offset < len(data) and data[offset] != 0xFF:
                item, offset = _cbor_decode(data, offset)
                out.append(item)
            return out, offset + 1
        for _ in range(value):
            item, offset = _cbor_decode(data, offset)
            out.append(item)
        return out, offset
    if major == 5:
        out = {}
        if value is None:
            while offset < len(data) and data[offset] != 0xFF:
                key, offset = _cbor_decode(data, offset)
                item, offset = _cbor_decode(data, offset)
                out[key] = item
            return out, offset + 1
        for _ in range(value):
            key, offset = _cbor_decode(data, offset)
            item, offset = _cbor_decode(data, offset)
            out[key] = item
        return out, offset
    raise CashuError("token_unsupported", f"Unsupported CBOR major type {major}.")


# --------------------------------------------------------------------------- #
# mint
# --------------------------------------------------------------------------- #

class MintClient:
    """The mint calls escrow needs: keyset, proof states, swap."""

    def __init__(self, mint_url: str, http: httpx.Client | None = None, timeout: float = 20.0):
        self.mint_url = mint_url.rstrip("/")
        self._http = http
        self._timeout = timeout
        self._keyset_cache: list[dict] | None = None
        self._keys_cache: dict[str, dict[int, str]] = {}

    # --- plumbing -----------------------------------------------------------

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=self.mint_url,
                                      timeout=httpx.Timeout(self._timeout))
        return self._http

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One mint call. Transport failures and refusals map to distinct reasons."""
        client = self._client()
        try:
            resp = client.request(method, path, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200] if exc.response is not None else ""
            raise CashuError("mint_error",
                             f"The mint refused {path} (HTTP {exc.response.status_code}): {detail}"
                             ) from None
        except httpx.HTTPError as exc:
            raise CashuError("mint_unreachable",
                             f"The mint at {self.mint_url} did not answer ({type(exc).__name__}); retry."
                             ) from None
        try:
            data = resp.json()
        except ValueError:
            raise CashuError("mint_error",
                             f"The mint answered {path} with a body that is not JSON.") from None
        if not isinstance(data, dict):
            raise CashuError("mint_error", "The mint returned a non-object response.")
        return data

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    # --- keysets ------------------------------------------------------------

    def keysets(self) -> list[dict]:
        if self._keyset_cache is None:
            data = self._get("/v1/keysets")
            keysets = data.get("keysets")
            if not isinstance(keysets, list):
                raise CashuError("mint_error", "The mint did not return a keyset list.")
            self._keyset_cache = keysets
        return self._keyset_cache

    def keyset_id(self, unit: str = "sat") -> str:
        for ks in self.keysets():
            if ks.get("unit") == unit and ks.get("active"):
                return str(ks["id"])
        raise CashuError("mint_error", f"The mint has no active {unit} keyset.")

    def fee_ppk_for(self, keyset_id: str) -> int:
        for ks in self.keysets():
            if str(ks.get("id")) == str(keyset_id):
                return int(ks.get("input_fee_ppk") or 0)
        return MINT_FEE_PPK_DEFAULT

    def keys(self, keyset_id: str) -> dict[int, str]:
        """amount -> compressed public key for one keyset."""
        if keyset_id not in self._keys_cache:
            data = self._get(f"/v1/keys/{keyset_id}")
            entries = data.get("keysets") or []
            if not entries:
                raise CashuError("mint_error", f"The mint does not know keyset {keyset_id}.")
            self._keys_cache[keyset_id] = {int(a): k for a, k in (entries[0].get("keys") or {}).items()}
        return self._keys_cache[keyset_id]

    # --- NUT-07 -------------------------------------------------------------

    def check_states(self, proofs: list[dict]) -> list[str]:
        """``UNSPENT`` / ``SPENT`` / ``PENDING`` per proof, in order."""
        if not proofs:
            raise CashuError("token_empty")
        ys = [hash_to_curve(str(p["secret"]).encode("utf-8")).hex() for p in proofs]
        data = self._post("/v1/checkstate", {"Ys": ys})
        states = data.get("states")
        if not isinstance(states, list) or len(states) != len(proofs):
            raise CashuError("mint_error", "The mint returned an unexpected state list.")
        return [str(s.get("state", "UNKNOWN")) for s in states]

    # --- NUT-03 -------------------------------------------------------------

    def swap(self, inputs: list[dict], output_amounts: list[int]) -> list[dict]:
        """Swap `inputs` for fresh proofs with exactly the given denominations."""
        if not inputs:
            raise CashuError("token_empty")
        if sum(int(p["amount"]) for p in inputs) <= 0:
            raise CashuError("token_empty")
        keyset = self.keyset_id()
        outputs, blindings = [], []
        for amount in output_amounts:
            blinded, blinding = blind_message(int(amount), keyset)
            outputs.append(blinded)
            blindings.append(blinding)
        payload = {
            "inputs": [{"amount": int(p["amount"]), "id": p["id"],
                        "secret": p["secret"], "C": p["C"]} for p in inputs],
            "outputs": outputs,
        }
        data = self._post("/v1/swap", payload)
        signatures = data.get("signatures")
        if not isinstance(signatures, list) or len(signatures) != len(outputs):
            raise CashuError("mint_error", "The mint did not sign every swap output.")
        keys = self.keys(keyset)
        proofs = []
        for sig, blinding, output in zip(signatures, blindings, outputs):
            amount = int(sig.get("amount", output["amount"]))
            mint_pubkey = keys.get(amount)
            if not mint_pubkey:
                raise CashuError("mint_error",
                                 f"The mint signed {amount} sats with a key it never published.")
            proofs.append(unblind(sig, blinding, keyset, mint_pubkey))
        return proofs
