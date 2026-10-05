"""The three JMP tools the CVM serves: status, funding, credentials.

`jmp.status`     — free, read-only. The funding facts (number, address, amount),
                   `source` honestly labelled `cached` or `live-flow`.
`jmp.funding`    — free. Drives the ad-hoc flow to the funding step and returns
                   the same payload with `source: live-flow`. It cannot pay: the
                   driver selects the *bitcoin* branch (which is what makes the
                   bot print the deposit address) and stops; no card path, no
                   Braintree, no submit of a payment form. A human sends the BTC.
`jmp.credentials`— PAID and allow-listed. The account's access material,
                   NIP-44-encrypted to the requesting operator npub.

Why the secret is never returned in the clear
---------------------------------------------
A CVM response travels gift-wrapped over relays, and relays retain whatever they
carry. A plaintext secret inside that envelope is a credential leak to every
relay on the path — and to anyone who later obtains one of those keys. So the
response body carries a NIP-44 v2 ciphertext addressed to the caller's own npub:
only the operator's key can open it. The allow-list check happens **before** any
payment is captured, so an unauthorised caller cannot pay their way in.

Payment is part of the CvmTools dispatch (one payment path for the whole
service); this module owns authorization, the store read, the payload, and the
encryption — and it is pure, so all of that is unit-testable offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .cvm import ToolError
from .jmp_facts import ACTIVATION_NOTE, TOS_ACCEPTANCE, cached_payload
from .jmp_flow import NUMBER_CAVEAT, now_iso
from .jmp_secrets import (
    ACCOUNT_SECRET,
    ACCOUNT_FIELDS,
    FUNDING_SECRET,
    NUMBER_SECRET,
    OperatorAllowList,
    SecretRead,
    SecretStore,
    redaction_list,
)
from .nip44 import encrypt as nip44_encrypt
from .pricing import DEFAULT_PRICE_SATS

#: tool names, in contract order.
TOOL_JMP_STATUS = "jmp.status"
TOOL_JMP_FUNDING = "jmp.funding"
TOOL_JMP_CREDENTIALS = "jmp.credentials"
JMP_TOOLS = (TOOL_JMP_STATUS, TOOL_JMP_FUNDING, TOOL_JMP_CREDENTIALS)

#: What the credential release costs. Priced at the same flat rate as postage
#: (ADR-0002) rather than invented: one number, one request, one price.
CREDENTIALS_PRICE_SATS = DEFAULT_PRICE_SATS

#: How long a released credential is meant to live before rotation.
ROTATE_AFTER_DAYS = 90

#: The revoke path, served with every credential so a leaked secret has a
#: documented way out. Both halves are real: the password can be changed at the
#: account's server, and the caller can be removed from the allow-list, which
#: stops any further release immediately.
REVOKE_HINT = (
    "Revoke in two steps: (1) change the XMPP password on the account's server "
    "and update secret/jmp/account in OpenBao (the old secret then opens "
    "nothing); (2) remove the npub from NOSMS_JMP_ALLOWLIST on the CVM server, "
    "which stops any further release to it. Keep the allow-list removed until "
    "the new credential is in place."
)

SECRET_KIND = "xmpp-password"


def rotate_by(issued_at: str, days: int = ROTATE_AFTER_DAYS) -> str:
    """The date by which the released credential should have been rotated."""
    try:
        stamp = datetime.strptime(issued_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        stamp = datetime.now(timezone.utc)
    return (stamp + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class JmpService:
    """The JMP capability. Pure: driver, store, allow-list and clock injected."""

    def __init__(self, *, live_driver=None, status_probe=None,
                 secret_store: SecretStore | None = None,
                 server_secret_hex: str | None = None,
                 allow_list: OperatorAllowList | None = None,
                 encrypt=nip44_encrypt, now=now_iso,
                 credentials_price: int = CREDENTIALS_PRICE_SATS):
        #: callable() -> funding payload, or None when no live rail is wired.
        self.live_driver = live_driver
        #: callable() -> read-only payload, or None when nothing is wired.
        self.status_probe = status_probe
        self.secret_store = secret_store
        self.server_secret_hex = server_secret_hex
        self.allow_list = allow_list or OperatorAllowList()
        self.encrypt = encrypt
        self.now = now
        self.credentials_price = credentials_price

    # --- authorization (checked BEFORE payment, always) --------------------

    def is_allowed(self, caller: str | None) -> bool:
        return self.allow_list.allows(caller)

    def authorize_credentials(self, caller: str | None) -> None:
        """Refuse a caller who is not on the allow-list.

        Deliberately the first thing `jmp.credentials` does and deliberately
        separate from the payment step: an unauthorised caller must not be able
        to reach the payment path at all, so they cannot pay to obtain the
        secret, and the refusal names no field of it.
        """
        if not self.is_allowed(caller):
            raise ToolError(
                "owner_only",
                "jmp.credentials serves the account credential only to an "
                "allow-listed operator npub, and it checks the list before any "
                "payment is taken. This caller is not on the list.")

    # --- free tools --------------------------------------------------------

    def status(self) -> dict:
        """The funding facts, with `source` honest about where they came from.

        Read-only by design: with a probe wired the account is re-observed over
        IQ `get` only (roster + disco + the register form's text), and the
        *facts* still come from the cached capture — a roster lookup does not
        produce a deposit address, so stamping them `live-flow` would be a lie.
        Without a probe the cached record is served as-is. A failed probe is
        recorded in the payload, never hidden.
        """
        if self.status_probe is None:
            return cached_payload()
        try:
            payload = dict(self.status_probe())
        except Exception as exc:                                 # noqa: BLE001
            payload = cached_payload()
            payload["live_attempt"] = {"ok": False, "error": type(exc).__name__,
                                       "detail": str(exc)[:200]}
            return payload
        payload["source"] = "cached"          # the probe proves reachability only
        payload.setdefault("number_note", NUMBER_CAVEAT)
        payload.setdefault("activation_note", ACTIVATION_NOTE)
        payload.setdefault("tos", TOS_ACCEPTANCE)
        return payload

    def funding(self) -> dict:
        """Drive the flow to the funding step. Never falls back to cached.

        A caller asked for a live reading; answering with a cached one and a
        different `source` would be the exact dishonesty the contract forbids.
        """
        if self.live_driver is None:
            raise ToolError(
                "rail_unavailable",
                "No live JMP driver is configured on this server, so the flow "
                "cannot be driven. Use jmp.status for the cached record (it says "
                "so) or wire NOSMS_JMP_LIVE with the account credential present.")
        try:
            payload = dict(self.live_driver())
        except Exception as exc:                                 # noqa: BLE001
            raise ToolError(
                "rail_unavailable",
                "The JMP flow could not be driven "
                f"({type(exc).__name__}: {str(exc)[:200]}); no cached value is "
                "substituted for a live-flow request.") from exc
        payload["source"] = "live-flow"
        payload.setdefault("number_note", NUMBER_CAVEAT)
        payload.setdefault("activation_note", ACTIVATION_NOTE)
        payload.setdefault("tos", TOS_ACCEPTANCE)
        return payload

    # --- paid, allow-listed tool ------------------------------------------

    def reads(self) -> dict[str, SecretRead]:
        """Read every secret this capability needs, recording each outcome."""
        if self.secret_store is None:
            absent = SecretRead("none", ACCOUNT_SECRET, False, {},
                                "no secret store is configured")
            return {ACCOUNT_SECRET: absent,
                    NUMBER_SECRET: SecretRead("none", NUMBER_SECRET, False, {}, "no secret store is configured"),
                    FUNDING_SECRET: SecretRead("none", FUNDING_SECRET, False, {}, "no secret store is configured")}
        return {name: self.secret_store.read(name)
                for name in (ACCOUNT_SECRET, NUMBER_SECRET, FUNDING_SECRET)}

    def preconditions(self) -> dict:
        """Exactly what the store said, with no value in it.

        This is the diagnosable half of the "fail closed" rule: a missing
        credential must name the store and the secret, not answer `null`.
        """
        return {name: read.as_public_dict() for name, read in self.reads().items()}

    def redaction_values(self) -> list[str]:
        """Secret values for the driver's transcript redactor (never printed)."""
        return redaction_list(*self.reads().values())

    def credentials(self, caller: str) -> dict:
        """Build the encrypted credential payload for an allow-listed caller.

        Called only after CvmTools has (1) authorized the caller and (2) captured
        postage — in that order. The plaintext never leaves this method.
        """
        self.authorize_credentials(caller)
        if not self.server_secret_hex:
            raise ToolError(
                "credentials_encryption_unconfigured",
                "No server key is configured, so the credential cannot be NIP-44 "
                "encrypted to the caller. Refusing to release it in the clear.")
        reads = self.reads()
        account = reads[ACCOUNT_SECRET]
        if not account.present:
            raise ToolError(
                "credential_unavailable",
                f"The account credential is not in the {account.store} store "
                f"(name={ACCOUNT_SECRET}: {account.detail}). This node has no "
                f"write capability, so storing it is an operator action — the "
                f"tool fails closed rather than falling back to a local file.",
                precondition=account.as_public_dict(),
                preconditions={n: r.as_public_dict() for n, r in reads.items()})
        jid = account.field("jid")
        secret = account.field("secret")
        if not jid or not secret:
            raise ToolError(
                "credential_unavailable",
                f"The {account.store} record {ACCOUNT_SECRET} exists but is "
                f"missing {account.missing_fields(ACCOUNT_FIELDS)}.",
                precondition=account.as_public_dict())
        issued_at = self.now()
        body = json.dumps({
            "jid": jid,
            "secret": secret,
            "secret_kind": SECRET_KIND,
            "number": reads[NUMBER_SECRET].field("number"),
            "issued_at": issued_at,
            "rotate_by": rotate_by(issued_at),
            "revoke_hint": REVOKE_HINT,
        }, sort_keys=True)
        ciphertext = self.encrypt(self.server_secret_hex, caller, body)
        return {
            "tool": TOOL_JMP_CREDENTIALS,
            "enc": "nip44-v2",
            "to": str(caller).lower(),
            "ciphertext": ciphertext,
            "secret_kind": SECRET_KIND,
            "issued_at": issued_at,
            "rotate_by": rotate_by(issued_at),
            "revoke_hint": REVOKE_HINT,
            "preconditions": {name: read.as_public_dict()
                              for name, read in reads.items()},
            "note": ("NIP-44 v2 payload, addressed to your npub. Decrypt it with "
                     "your own key; the server never sends the plaintext. Rotate "
                     "by the date in rotate_by and use revoke_hint if it leaks."),
        }
