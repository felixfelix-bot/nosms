"""NIP-44 v2 encrypted payloads — pure Python, offline-testable.

Why this module exists (and why it is hand-written rather than a dependency):
the service must hand an operator their own account credential **without ever
putting the plaintext on the wire**. The only envelope that survives the trip is
one that the operator's own key is required to open — a NIP-44 payload addressed
to their npub. Relays carry (and retain) the ciphertext of whatever carries the
response, so a plaintext secret inside a gift wrap is a credential leak to every
relay on the path, forever.

The server box has `coincurve` (already required by app/nip98.py) but neither
`cryptography` nor `nostr_sdk`, and this code must run on the box that serves the
tool — so the primitive is implemented here, from the NIP-44 spec, and proved
against the **official test vectors** (`tests/fixtures/nip44.vectors.json`,
vendored from github.com/paulmillr/nip44, the vectors the spec points at).

Scope: version 2 only (`0x02`: secp256k1 ECDH, HKDF-SHA256, spec padding,
ChaCha20, HMAC-SHA256, base64). Version 0/1 and the `#`-prefixed non-base64 form
are refused explicitly rather than silently mis-decrypted.

The MAC is verified with a constant-time comparison, and the padding is checked
exactly as the spec's `unpad` does, so a tampered payload cannot be partially
trusted.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import struct

from coincurve import PrivateKey, PublicKey

#: the only payload version this module produces or accepts.
VERSION = 2

#: HKDF-extract salt, utf8_encode('nip44-v2') exactly as the spec specifies.
SALT = b"nip44-v2"

MIN_PLAINTEXT_SIZE = 1
#: The spec's own maximum (what `pad` supports).
MAX_PLAINTEXT_SIZE = 2 ** 32 - 1
#: What `encrypt` will actually produce. The official vector set treats 65536
#: and above as invalid *inputs* to encrypt, and no relay will carry such a
#: payload anyway; a plaintext larger than this is refused early with a reason
#: instead of being silently produced. `pad`/`unpad` still support the extended
#: prefix the spec defines, which is what a decrypt of someone else's long
#: payload would need.
MAX_ENCRYPT_PLAINTEXT = 65535
EXTENDED_PREFIX_THRESHOLD = 65536

#: a payload shorter than this cannot even hold version+nonce+mac (65 bytes).
MIN_DECODED_PAYLOAD = 99
MIN_BASE64_PAYLOAD = 132

_MASK = 0xFFFFFFFF


class Nip44Error(ValueError):
    """A payload could not be produced or opened (never partially trusted)."""


# --------------------------------------------------------------------------
# HKDF-SHA256 (RFC 5869) — the two halves, exactly as NIP-44 names them
# --------------------------------------------------------------------------
def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    if length < 0 or length > 255 * 32:
        raise Nip44Error("invalid HKDF output length")
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


# --------------------------------------------------------------------------
# ChaCha20 (RFC 8439), counter starting at 0, 12-byte nonce
# --------------------------------------------------------------------------
def _rotl(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & _MASK


def _quarter_round(x: list[int], a: int, b: int, c: int, d: int) -> None:
    x[a] = (x[a] + x[b]) & _MASK
    x[d] = _rotl(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & _MASK
    x[b] = _rotl(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & _MASK
    x[d] = _rotl(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & _MASK
    x[b] = _rotl(x[b] ^ x[c], 7)


def _chacha20_block(state: list[int]) -> bytes:
    w = list(state)
    for _ in range(10):                       # 20 rounds = 10 column+diagonal pairs
        _quarter_round(w, 0, 4, 8, 12)
        _quarter_round(w, 1, 5, 9, 13)
        _quarter_round(w, 2, 6, 10, 14)
        _quarter_round(w, 3, 7, 11, 15)
        _quarter_round(w, 0, 5, 10, 15)
        _quarter_round(w, 1, 6, 11, 12)
        _quarter_round(w, 2, 7, 8, 13)
        _quarter_round(w, 3, 4, 9, 14)
    return struct.pack("<16I", *[(w[i] + state[i]) & _MASK for i in range(16)])


def chacha20_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """ChaCha20 with a 32-byte key and a 12-byte nonce (counter 0)."""
    if len(key) != 32:
        raise Nip44Error("ChaCha20 key must be 32 bytes")
    if len(nonce) != 12:
        raise Nip44Error("ChaCha20 nonce must be 12 bytes")
    key_words = list(struct.unpack("<8I", key))
    nonce_words = list(struct.unpack("<3I", nonce))
    out = bytearray()
    for offset in range(0, len(data), 64):
        block_index = offset // 64
        state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
        state += key_words + [block_index & _MASK] + nonce_words
        stream = _chacha20_block(state)
        chunk = data[offset:offset + 64]
        out += bytes(a ^ b for a, b in zip(chunk, stream))
    return bytes(out)


# --------------------------------------------------------------------------
# key schedule
# --------------------------------------------------------------------------
def conversation_key(privkey_hex: str, pubkey_hex: str) -> bytes:
    """`conv(a, B)` — HKDF-extract over the **unhashed** ECDH x coordinate."""
    try:
        priv = PrivateKey(bytes.fromhex(privkey_hex))
    except (ValueError, binascii.Error, TypeError) as exc:
        raise Nip44Error("invalid private key") from exc
    try:
        pub = bytes.fromhex(pubkey_hex)
    except (ValueError, binascii.Error, TypeError) as exc:
        raise Nip44Error("invalid public key") from exc
    if len(pub) != 32:
        raise Nip44Error("public key must be 32-byte x-only (BIP-340)")
    # `PrivateKey.ecdh()` (coincurve >= 21) applies libsecp256k1's DEFAULT hash
    # function, i.e. sha256 of the compressed shared point — NIP-44 needs the
    # UNHASHED x coordinate, and libraries that hash it do not interoperate.
    # Point multiplication gives the shared point directly, so its x is taken
    # from the compressed encoding: 33 bytes = 0x02/0x03 prefix + x.
    # BIP-340 x-only keys imply the even-y lift, and the x coordinate of the
    # shared point is the same for P and -P, so the 0x02 prefix is safe.
    shared_point = PublicKey(b"\x02" + pub).multiply(priv.secret)
    shared_x = shared_point.format(compressed=True)[1:]
    return hkdf_extract(SALT, shared_x)


def message_keys(conv_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    if len(conv_key) != 32:
        raise Nip44Error("conversation key must be 32 bytes")
    if len(nonce) != 32:
        raise Nip44Error("nonce must be 32 bytes")
    keys = hkdf_expand(conv_key, nonce, 76)
    return keys[:32], keys[32:44], keys[44:76]


# --------------------------------------------------------------------------
# padding
# --------------------------------------------------------------------------
def calc_padded_len(unpadded_len: int) -> int:
    if unpadded_len <= 0:
        raise Nip44Error("invalid plaintext length")
    next_power = 1 << ((unpadded_len - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    if unpadded_len <= 32:
        return 32
    return chunk * ((unpadded_len - 1) // chunk + 1)


def pad(plaintext: bytes) -> bytes:
    length = len(plaintext)
    if length < MIN_PLAINTEXT_SIZE or length > MAX_PLAINTEXT_SIZE:
        raise Nip44Error("invalid plaintext length")
    if length >= EXTENDED_PREFIX_THRESHOLD:
        prefix = b"\x00\x00" + struct.pack(">I", length)
    else:
        prefix = struct.pack(">H", length)
    return prefix + plaintext + b"\x00" * (calc_padded_len(length) - length)


def unpad(padded: bytes) -> bytes:
    if len(padded) < 2:
        raise Nip44Error("invalid padding")
    first_two = struct.unpack(">H", padded[:2])[0]
    if first_two == 0:
        if len(padded) < 6:
            raise Nip44Error("invalid padding")
        unpadded_len = struct.unpack(">I", padded[2:6])[0]
        if unpadded_len < EXTENDED_PREFIX_THRESHOLD:
            raise Nip44Error("invalid padding")
        prefix_len = 6
    else:
        unpadded_len = first_two
        prefix_len = 2
    unpadded = padded[prefix_len:prefix_len + unpadded_len]
    if (unpadded_len == 0
            or len(unpadded) != unpadded_len
            or len(padded) != prefix_len + calc_padded_len(unpadded_len)):
        raise Nip44Error("invalid padding")
    return unpadded


# --------------------------------------------------------------------------
# encrypt / decrypt
# --------------------------------------------------------------------------
def encrypt(privkey_hex: str, pubkey_hex: str, plaintext: str,
            nonce: bytes | None = None) -> str:
    """Encrypt `plaintext` from `privkey_hex` to `pubkey_hex`. Returns base64.

    `nonce` is injectable only so the test vectors can pin it; in production it
    is always a fresh CSPRNG draw (a reused nonce makes two messages
    decryptable).
    """
    if not isinstance(plaintext, str):
        raise Nip44Error("plaintext must be a string")
    raw = plaintext.encode("utf-8")
    if len(raw) < MIN_PLAINTEXT_SIZE:
        raise Nip44Error("plaintext is empty")
    if len(raw) > MAX_ENCRYPT_PLAINTEXT:
        raise Nip44Error(
            f"plaintext is {len(raw)} bytes; the encrypt path caps at "
            f"{MAX_ENCRYPT_PLAINTEXT} (the official vectors treat longer inputs "
            f"as invalid and no relay carries them)")
    return encrypt_with_conversation_key(conversation_key(privkey_hex, pubkey_hex),
                                         plaintext, nonce=nonce)


def encrypt_with_conversation_key(conv_key: bytes, plaintext: str,
                                  nonce: bytes | None = None) -> str:
    """Encrypt with an already-derived conversation key (base64 payload)."""
    raw = plaintext.encode("utf-8")
    if len(raw) < MIN_PLAINTEXT_SIZE:
        raise Nip44Error("plaintext is empty")
    nonce = os.urandom(32) if nonce is None else nonce
    if len(nonce) != 32:
        raise Nip44Error("nonce must be 32 bytes")
    chacha_key, chacha_nonce, hmac_key = message_keys(conv_key, nonce)
    ciphertext = chacha20_xor(chacha_key, chacha_nonce, pad(raw))
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    payload = bytes([VERSION]) + nonce + ciphertext + mac
    return base64.b64encode(payload).decode("ascii")


def decrypt(privkey_hex: str, pubkey_hex: str, payload_b64: str) -> str:
    """Open a NIP-44 v2 payload. Any tampering is an error, never a partial read."""
    return decrypt_with_conversation_key(conversation_key(privkey_hex, pubkey_hex),
                                         payload_b64)


def decrypt_with_conversation_key(conv_key: bytes, payload_b64: str) -> str:
    """Open a payload given the conversation key itself.

    Exposed because the official vector set publishes invalid payloads as
    (payload, conversation_key) pairs — testing those against this code has to
    use the same entry point a decrypting relay would.
    """
    if not isinstance(payload_b64, str):
        raise Nip44Error("payload must be a base64 string")
    if payload_b64.startswith("#"):
        raise Nip44Error("version not supported (non-base64 payload flag)")
    if len(payload_b64) < MIN_BASE64_PAYLOAD:
        raise Nip44Error("payload is too short to be a NIP-44 v2 message")
    try:
        raw = base64.b64decode(payload_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Nip44Error("payload is not valid base64") from exc
    if len(raw) < MIN_DECODED_PAYLOAD:
        raise Nip44Error("decoded payload is too short")
    if raw[0] != VERSION:
        raise Nip44Error(f"unsupported payload version {raw[0]}")
    nonce, ciphertext, mac = raw[1:33], raw[33:-32], raw[-32:]
    chacha_key, chacha_nonce, hmac_key = message_keys(conv_key, nonce)
    expected = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise Nip44Error("MAC mismatch: payload was tampered with or mis-addressed")
    try:
        return unpad(chacha20_xor(chacha_key, chacha_nonce, ciphertext)).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Nip44Error("payload did not decode to UTF-8") from exc
