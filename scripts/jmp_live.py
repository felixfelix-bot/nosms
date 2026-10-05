"""Wire the live JMP driver for the CVM server (wire layer — no tool logic).

Reads the account credential from OpenBao and hands :class:`JmpService` two
callables:

* ``status_probe``  — the read-only probe (roster + disco + register form text).
* ``live_driver``   — the funding drive, which stops at the funding step.

Both are ``None`` unless ``NOSMS_JMP_LIVE=1`` **and** the credential is actually
present in the store: a service that cannot read its credential must not pretend
it can drive anything. Nothing here prints a secret; the drive's transcript is
redacted with every value read from the store.

The credential is read once at construction for the redaction list, and again
inside each call, so a rotation takes effect without a restart.
"""
from __future__ import annotations

import asyncio
import os

from app.jmp_flow import drive_funding_live, probe_status_live
from app.jmp_secrets import ACCOUNT_SECRET, OpenBaoSecretStore, OperatorAllowList
from app.jmp_tools import JmpService


def build_jmp_service(env: dict | None = None, server_secret_hex: str | None = None,
                      transcript_path: str | None = None) -> JmpService:
    """Build the JmpService the server serves. Never raises on a missing secret."""
    env = os.environ if env is None else env
    store = OpenBaoSecretStore(fleet_secret_path=env.get("NOSMS_FLEET_SECRET"))
    allow_list = OperatorAllowList.from_env(env)
    service = JmpService(secret_store=store, server_secret_hex=server_secret_hex,
                         allow_list=allow_list)

    if env.get("NOSMS_JMP_LIVE", "").strip() not in ("1", "true", "yes"):
        # Cached-only: jmp.funding will refuse with a visible reason, jmp.status
        # answers from the capture. That is the honest default.
        return service

    account = store.read(ACCOUNT_SECRET)
    if not account.present:
        print("[nosms-cvm] NOSMS_JMP_LIVE is set but the account credential is not "
              f"in the store ({account.store}/{ACCOUNT_SECRET}: {account.detail}); "
              "jmp.funding will refuse.")
        return service
    jid, secret = account.field("jid"), account.field("secret")
    if not jid or not secret:
        print(f"[nosms-cvm] NOSMS_JMP_LIVE is set but {ACCOUNT_SECRET} is missing "
              f"fields {account.missing_fields(('jid', 'secret'))}.")
        return service

    redact = [value for value in (jid, secret) if value]

    def _status_probe() -> dict:
        return asyncio.run(probe_status_live(jid, secret, transcript_path=transcript_path,
                                             redact=redact))

    def _live_driver() -> dict:
        return asyncio.run(drive_funding_live(jid, secret, transcript_path=transcript_path,
                                              redact=redact))

    service.status_probe = _status_probe
    service.live_driver = _live_driver
    print(f"[nosms-cvm] JMP live driver wired (account {account.store}/{ACCOUNT_SECRET}, "
          f"allow-list {len(allow_list.pubkeys)} npub(s)); funding never pays.")
    return service
