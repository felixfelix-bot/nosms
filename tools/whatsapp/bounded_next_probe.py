#!/usr/bin/env python3
"""Run on emulator host: one NEXT selector click, then at most one Enter.

Never confirms a number, requests a resend, or proceeds past phone entry.
Output is JSON with sanitized view trees; a returned click is not success.
"""
import json
import re
import time
import xml.etree.ElementTree as ET

import uiautomator2 as u2

PHONE = "com.whatsapp:id/registration_phone"


def signature(xml):
    return [(n.get("resource-id"), n.get("text"), n.get("content-desc"))
            for n in ET.fromstring(xml).iter("node")
            if n.get("package") == "com.whatsapp"]


def emit(stage, xml, changed):
    root = ET.fromstring(xml)
    for node in root.iter("node"):
        if node.get("resource-id") == PHONE:
            node.set("text", "[PHONE REDACTED]")
        for key in ("text", "content-desc"):
            value = node.get(key, "")
            node.set(key, re.sub(r"\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}", "[PHONE REDACTED]", value))
    print(json.dumps({"stage": stage, "changed": changed,
                      "xml": ET.tostring(root, encoding="unicode")}), flush=True)


def observe(d, before, stage):
    deadline = time.monotonic() + 10
    while True:
        xml = d.dump_hierarchy()
        changed = signature(xml) != signature(before)
        if changed or time.monotonic() >= deadline:
            emit(stage, xml, changed)
            return changed
        time.sleep(1)


def main():
    d = u2.connect("emulator-5554")
    before = d.dump_hierarchy()
    emit("before", before, False)
    if not d(resourceId=PHONE).exists or not d(text="NEXT").exists:
        raise SystemExit("STOP: not the expected phone-entry screen")
    d(text="NEXT").click(timeout=10)
    if observe(d, before, "after_selector"):
        return
    # Only the manager-authorized fallback; no second NEXT click.
    field = d(resourceId=PHONE)
    value = field.get_text()
    field.click(timeout=10)
    field.set_text(value)
    d.press("enter")
    observe(d, before, "after_enter")
    print(json.dumps({"stage": "stop", "reason": "bounded diagnostic finished; no further actions"}))


if __name__ == "__main__":
    main()
