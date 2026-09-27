"""nosms — SMS for nostr keys (the SMS version of `cashu.email`).

This package is the service layer described in `PLAN.md`. The modules present
today are the sats-side building blocks:

* :mod:`nosms.lnurl_pay` — LNURL-pay (LUD-16 lightning address) resolution and
  invoice requests, with Machankura (`8333.mobi`) as a first-class host.
* :mod:`nosms.payments` — the escrow/auto-refund adapter built on top of
  LNURL-pay, with all real sat movement behind an injectable ``LnBackend``.
"""

__all__ = ["lnurl_pay", "payments"]
