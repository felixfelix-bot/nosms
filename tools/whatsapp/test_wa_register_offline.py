#!/usr/bin/env python3
"""Offline validation of wa_register logic against the prior run's real UI dumps.

Replays evidence/ui/*.xml from the old workspace through the same find/refusal
code paths the live register phase uses. No device touched.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp"))
import wa_register as wr  # noqa: E402
import wa_ui  # noqa: E402

UI_DIR = os.path.expanduser("~/worktrees/t_c930a683/evidence/ui")

cases = [
    # (file, expectation)
    ("04_phone_entry.xml", "phone-entry screen: registration_phone present, NEXT present"),
    ("05d.xml", "number typed: registration_phone present, NEXT present"),
]

failures = 0
for fname, expect in cases:
    path = os.path.join(UI_DIR, fname)
    if not os.path.exists(path):
        print(f"SKIP {fname} (missing)")
        continue
    xml = open(path).read()
    phone = wa_ui.find(xml, rid="com.whatsapp:id/registration_phone")
    nxt = wa_ui.find(xml, text="NEXT", exact=True)
    # refusal check: reuse markers without failing hard
    hay = " ".join(n["text"] + " " + n["content-desc"]
                   for n in wa_ui.nodes(xml) if (n["text"] or n["content-desc"])).lower()
    refused = [m for m in wr.VOICE_REFUSAL_MARKERS if m in hay]
    ok = (phone is not None) and (nxt is not None) and not refused
    print(f"{'PASS' if ok else 'FAIL'} {fname}: phone={phone is not None} "
          f"next={nxt is not None} refusal_markers={refused}  # {expect}")
    if not ok:
        failures += 1
    # show what digits the field holds (the pre-verify logic)
    if phone:
        import re
        digits = "".join(re.findall(r"\d", phone["text"]))
        print(f"      field digits: {digits!r}")

# negative control: an unrelated screen must NOT look like phone entry
neg = os.path.join(UI_DIR, "02_after_eula.xml")
if os.path.exists(neg):
    xml = open(neg).read()
    phone = wa_ui.find(xml, rid="com.whatsapp:id/registration_phone")
    print(f"{'PASS' if phone is None else 'FAIL'} negative control (02_after_eula has no phone field): {phone is None}")
    failures += 0 if phone is None else 1

sys.exit(1 if failures else 0)
