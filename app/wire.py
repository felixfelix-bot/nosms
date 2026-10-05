"""The CVM wire envelope: receive-side unwrap and send-side wrap.

Why this module exists (2026-10-05, found live)
-----------------------------------------------
The napplet is only reachable through a REAL shell. The shell's NAP-CVM
transport (Kehto/Paja ``@kehto/services`` ``cvm-nostr-transport``) does **not**
ship a NIP-59 gift wrap. It calls the outer envelope
``support_encryption`` / ``support_encryption_ephemeral`` (ContextVM **CEP-4**),
and puts a *signed kind-25910 event* straight into NIP-44 ciphertext addressed
to the server::

    kind 21059/1059  tags [[p, SERVER]]
      content = nip44_encrypt(json({kind: 25910, ..., content: json(mcp)}))

whereas this repo's own client (``scripts/cvm_client.py``) uses ``nostr_sdk``'s
``gift_wrap()``, which builds a NIP-59 wrap: an *unsigned kind-13 seal* inside
the ciphertext.

Both are legitimate and both are in use — `cvmi` (the ContextVM CLI) uses the
NIP-59 form, the Kehto shell uses the CEP-4 form (measured: ``cvmi call`` against
this server times out with ``MCP error -32001`` while the shell path silently
drops, from the same cause). A server that handles only one dialect silently
drops the other: ``nostr_sdk``'s ``unwrap_gift_wrap()`` fails on a CEP-4 envelope
("Not a Gift Wrap" / base64 error) and a shell's transport fails on a NIP-59
seal. **The receive path accepts both, and the send path answers in the same
dialect the caller used.**

Nothing here touches a relay; it is pure crypto/envelope work and unit-testable
offline.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from nostr_sdk import (
    Event,
    EventBuilder,
    Keys,
    Kind,
    Nip44Version,
    NostrSigner,
    PublicKey,
    Tag,
    UnsignedEvent,
    UnwrappedGift,
    nip44_decrypt,
    nip44_encrypt,
)

#: kind of the inner ContextVM JSON-RPC event
CVM_KIND = 25910
#: NIP-59 gift-wrap kinds (what ``gift_wrap()`` emits, and what CEP-4 reuses)
NIP59_WRAP_KINDS = (1059, 21059)
#: the CEP-4 dialect always uses the ephemeral range for its outer envelope
CEP4_WRAP_KIND = 21059

#: capability flags the CEP-4 dialect publishes in the outer envelope
SUPPORT_ENCRYPTION = "support_encryption"
SUPPORT_ENCRYPTION_EPHEMERAL = "support_encryption_ephemeral"


@dataclass(frozen=True)
class InboundEnvelope:
    """A decrypted CVM request, whichever dialect delivered it."""

    rpc: dict
    sender: PublicKey
    #: True when the caller used a NIP-59 seal; False for a CEP-4 envelope.
    nip59: bool


def _p_tag_value(event: Event) -> str | None:
    for tag in event.tags().to_vec():
        values = tag.as_vec()
        if values and values[0] == "p":
            return values[1]
    return None


async def unwrap_envelope(keys: Keys, event: Event) -> InboundEnvelope:
    """Decrypt an inbound envelope in **either** dialect.

    Tries the NIP-59 gift-wrap path first (what ``gift_wrap()`` and ``cvmi``
    produce, and what the SDK understands natively), then falls back to a direct
    NIP-44 decrypt of the outer content (CEP-4, what the shell's transport
    produces).

    Async because ``UnwrappedGift.from_gift_wrap`` is a coroutine in
    ``nostr_sdk`` >= 0.44 (measured: calling it without ``await`` yields
    ``'coroutine' object has no attribute 'rumor'``).

    Raises ``ValueError`` when neither works, so the caller can log a real reason
    instead of a transport-specific string.
    """
    errors: list[str] = []

    # --- dialect 1: NIP-59 gift wrap (kind-13 seal inside) -------------------
    try:
        unwrapped = await UnwrappedGift.from_gift_wrap(NostrSigner.keys(keys), event)
        rumor = unwrapped.rumor()
        if rumor.kind().as_u16() == CVM_KIND:
            return InboundEnvelope(
                rpc=json.loads(rumor.content()),
                sender=unwrapped.sender(),
                nip59=True,
            )
        errors.append(f"nip59 rumor kind {rumor.kind().as_u16()} != {CVM_KIND}")
    except Exception as exc:  # noqa: BLE001 - any failure means "not this dialect"
        errors.append(f"nip59: {exc}")

    # --- dialect 2: CEP-4, a signed kind-25910 event inside NIP-44 ----------
    if _p_tag_value(event) != keys.public_key().to_hex():
        errors.append("cep4: outer p-tag does not name this server")
        raise ValueError("not a ContextVM envelope (" + "; ".join(errors) + ")")
    try:
        plaintext = nip44_decrypt(keys.secret_key(), event.author(), event.content())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cep4: {exc}")
        raise ValueError("not a ContextVM envelope (" + "; ".join(errors) + ")") from exc

    try:
        inner = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        errors.append(f"cep4 inner not JSON: {exc}")
        raise ValueError("not a ContextVM envelope (" + "; ".join(errors) + ")") from exc

    if not isinstance(inner, dict) or inner.get("kind") != CVM_KIND:
        errors.append(f"cep4 inner kind {inner.get('kind') if isinstance(inner, dict) else type(inner).__name__} != {CVM_KIND}")
        raise ValueError("not a ContextVM envelope (" + "; ".join(errors) + ")")

    # Trust the inner signature: the envelope only proves who could read it, the
    # signed inner event proves who sent it.
    signed_inner = Event.from_json(json.dumps(inner))
    if not signed_inner.verify():
        raise ValueError("cep4 inner event signature is invalid")
    return InboundEnvelope(
        rpc=json.loads(signed_inner.content()),
        sender=signed_inner.author(),
        nip59=False,
    )


def build_inner_event(keys: Keys, recipient: PublicKey, rpc: dict) -> UnsignedEvent:
    """The kind-25910 JSON-RPC event, addressed to ``recipient``.

    Returned *unsigned*: the NIP-59 dialect seals it, the CEP-4 dialect signs
    and encrypts it. Never mutate the returned object.
    """
    return (
        EventBuilder(Kind(CVM_KIND), json.dumps(rpc, separators=(",", ":")))
        .tags([Tag.public_key(recipient)])
        .build(keys.public_key())
    )


def wrap_cep4(keys: Keys, recipient: PublicKey, inner: UnsignedEvent) -> Event:
    """Answer a CEP-4 caller in-kind: a kind-21059 envelope with a signed inner.

    This is the shape the Kehto shell's transport requires: it decrypts the outer
    content with ``client_secret × wrap_author`` and then ``JSON.parse``es the
    result straight into a **signed** event whose ``pubkey`` it takes as the
    server identity, and whose ``p`` tag it checks against its own pubkey.
    """
    signed = inner.sign_with_keys(keys)
    outer = (
        EventBuilder(
            Kind(CEP4_WRAP_KIND),
            nip44_encrypt(keys.secret_key(), recipient, signed.as_json(), Nip44Version.V2),
        )
        .tags([Tag.public_key(recipient)])
        .build(keys.public_key())
    )
    return outer.sign_with_keys(keys)


async def wrap_nip59(client: Any, recipient: PublicKey, inner: UnsignedEvent,
                     relay_urls: list[str] | None = None) -> Any:
    """Answer a NIP-59 caller in-kind, via the client's own ``gift_wrap``.

    ``client.gift_wrap`` both builds and publishes, so this needs the live
    relay client. Returns the SDK's ``SendEventOutput``.
    """
    del relay_urls  # the client already holds its relay set
    return await client.gift_wrap(recipient, inner, [])


def reply_event(keys: Keys, recipient: PublicKey, rpc: dict, *, nip59: bool) -> Event | None:
    """Convenience: build the CEP-4 reply, or ``None`` when the caller was NIP-59.

    NIP-59 replies are async (``wrap_nip59``), so sync callers get ``None`` and
    are expected to hand the inner event to the async helper.
    """
    inner = build_inner_event(keys, recipient, rpc)
    if nip59:
        return None
    return wrap_cep4(keys, recipient, inner)


__all__ = [
    "CEP4_WRAP_KIND",
    "CVM_KIND",
    "NIP59_WRAP_KINDS",
    "SUPPORT_ENCRYPTION",
    "SUPPORT_ENCRYPTION_EPHEMERAL",
    "InboundEnvelope",
    "build_inner_event",
    "reply_event",
    "unwrap_envelope",
    "wrap_cep4",
    "wrap_nip59",
]
