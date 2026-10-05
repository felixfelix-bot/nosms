"""The JMP funding facts, as captured — and the provenance that says so.

This module holds the *cached* record: the values a live run captured on
2026-09-26, with the evidence file each one came from. It exists so the CVM can
answer `jmp.status` without driving anything, while never pretending those
values are fresh.

Honesty rules encoded here (each one is a test in `tests/test_jmp_tools.py`):

* ``source`` is ``cached`` — a caller must be told the facts were not observed
  by this run, and ``as_of`` is the capture time, not "now".
* The number is named as a **session-scoped reservation**, and the payload
  carries the observed history rather than one value presented as owned.
* The BTC address and amount come from the same session as the number they are
  paired with, because that is how the bot produced them.

The cross-check the card asked for, resolved
--------------------------------------------
The card said the number was ``(810) 258-3233`` and that ``(810) 258-3253`` "was
menu OPTION #2 and was never chosen". Reading the evidence rather than the
summary contradicts that, and the correction is load-bearing:

===================  ==================================  ==================
session (2026-09-26)  option 1 in the bot's menu          the bot's answer
===================  ==================================  ==================
run 1 transcript      (810) 258-3233                      "You've selected
                                                           (810) 258-3233"
run 2, session A      (810) 202-2913                      "You've selected
                                                           (810) 202-2913"
run 2, session B      (810) 258-3253                      "You've selected
  (the one that                                             (810) 258-3253"
   printed the
   deposit address)
===================  ==================================  ==================

So 258-3253 *was* option #1 **and was selected** — in the very session that
produced the deposit address this record serves — and the number is not stable
across sessions at all. The three sessions each offered a different option 1,
which is exactly the caveat the card's hard rule 4 demands be visible: this is a
per-session reservation, not ownership.

Evidence (felixfelix-bot/soveng-archive, branch ``worker-base/t_3910f6fc``):
``cashu-sms/evidence/jmp-register-transcript-2026-09-26.txt`` line 38,
``cashu-sms/evidence/jmp-register-payment-2026-09-26.txt`` lines 34 and 101;
write-up ``cashu-sms/jmp-signup-flow-2026-09-26.md`` section 9 (whose own table
line 346 carries 258-3253, i.e. the document was internally inconsistent — the
transcripts are the source of truth).
"""
from __future__ import annotations

from .jmp_flow import NUMBER_CAVEAT

#: when the live capture happened (the run-2 window, 20:47–21:20 UTC).
CAPTURED_AT = "2026-09-26T21:20:00Z"

#: evidence files the record was read from (soveng-archive, branch
#: worker-base/t_3910f6fc, commit 3a179dc).
EVIDENCE = (
    "soveng-archive@3a179dc:cashu-sms/evidence/jmp-register-payment-2026-09-26.txt",
    "soveng-archive@3a179dc:cashu-sms/evidence/jmp-register-transcript-2026-09-26.txt",
)

#: every number selection observed live, in order. The last entry is the one
#: paired with the deposit address below.
NUMBER_HISTORY = (
    {"number": "(810) 258-3233", "session": "run 1 (registration transcript)",
     "selected": True, "evidence": "jmp-register-transcript-2026-09-26.txt:38"},
    {"number": "(810) 202-2913", "session": "run 2, session A",
     "selected": True, "evidence": "jmp-register-payment-2026-09-26.txt:34"},
    {"number": "(810) 258-3253", "session": "run 2, session B (the one that printed the deposit address)",
     "selected": True, "evidence": "jmp-register-payment-2026-09-26.txt:101"},
)

#: the cached record. `number` is the number of the session that produced the
#: address/amount below — the values travel as one observation, never spliced.
CACHED_FACTS: dict = {
    "number": "(810) 258-3253",
    "number_state": "reserved",
    "btc_address": "bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk",
    "amount_btc": "0.000244",
    "amount_usd_min": "20.00",
    "balance_usd": "0.00",
    "activation_state": "unfunded",
    "as_of": CAPTURED_AT,
    "source": "cached",
}

#: JMP's own wording that puts the number, and the service, in context.
ACTIVATION_NOTE = (
    "After payment is complete, your number will be activated for inbound calls "
    "and texts. Calling out, or sending a text, will often be restricted until "
    "you receive at least one text from a person or port in a number."
)

#: The operator accepted the account-loss risk of automating their own single
#: personal line ("lets take the risk.", 2026-09-26). It is recorded with the
#: tool description because it is a licence for ONE line, not for bulk use.
TOS_ACCEPTANCE = (
    "JMP's terms restrict automated use of its personal rail. The operator has "
    "explicitly accepted that risk for their own single line "
    "('lets take the risk.', 2026-09-26). This capability is scoped to that one "
    "account: it is not a licence for bulk or resale use, and it never widens "
    "itself."
)

PROVENANCE_NOTE = (
    "Cached from a live capture; not observed by this run. Re-drive with "
    "jmp.funding for a live-flow reading."
)


def cached_payload() -> dict:
    """The cached funding payload, source honestly labelled `cached`."""
    payload = dict(CACHED_FACTS)
    payload["number_note"] = NUMBER_CAVEAT
    payload["number_history"] = [dict(item) for item in NUMBER_HISTORY]
    payload["provenance"] = {"mode": "cached", "captured_at": CAPTURED_AT,
                             "evidence": list(EVIDENCE), "note": PROVENANCE_NOTE}
    payload["activation_note"] = ACTIVATION_NOTE
    payload["tos"] = TOS_ACCEPTANCE
    return payload
