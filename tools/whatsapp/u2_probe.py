#!/usr/bin/env python3
"""Dumb uiautomator2 primitives for the wa-dev emulator — runs ON dq05.

Invoked from CobradorWave (wa_register.py) over ssh:

    ssh dq05 '~/wa_u2_venv/bin/python -' < u2_probe.py <action> [args...]

This script contains NO decision logic — no load gates, no halt conditions,
no refuse-lists. Those all stay in wa_register.py on CobradorWave (card
t_c930a683 fix path (1): rewrite ONLY the interaction layer). Each action is
a primitive; the LAST stdout line is a JSON result object; diagnostics go
to stderr.

Why this runs on dq05: uiautomator2's device connection is backed by adb
forward/reverse sockets bound by the adb SERVER, which lives on the emulator
host (dq05). Driving it from CobradorWave through the 5037 tunnel is
unreliable, so the primitives execute next to the adb server and only the
JSON result crosses the ssh boundary.

Actions:
  dump                 full hierarchy XML (same format `uiautomator dump` emits)
  info <rid>           exists / focused / text of the first resource-id match
  click-id <rid>       accessibility click on the resource-id match
  click-text <text>    click first text== / desc== / text-contains match
  set-text <rid> <val> click (sets real focus) + clear_text + set_text
                       (ACTION_SET_TEXT bypasses the IME — the run-172 fix:
                       synthetic taps never focus a WhatsApp EditText on the
                       headless guest, so `input text`/keyevent digits drop)
"""
import json
import sys

import uiautomator2 as u2

SERIAL = "emulator-5554"


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> int:
    if len(sys.argv) < 2:
        emit({"ok": False, "error": "no action"})
        return 2
    action, args = sys.argv[1], sys.argv[2:]
    d = u2.connect(SERIAL)

    if action == "dump":
        emit({"ok": True, "xml": d.dump_hierarchy()})
        return 0

    if action == "info":
        el = d(resourceId=args[0])
        ex = bool(el.exists)
        emit({
            "ok": True,
            "exists": ex,
            "focused": (el.info.get("focused") if ex else None),
            "text": (el.get_text() if ex else None),
        })
        return 0

    if action == "click-id":
        d(resourceId=args[0]).click(timeout=15)
        emit({"ok": True})
        return 0

    if action == "click-text":
        t = args[0]
        for kind, sel in (("text", d(text=t)),
                          ("desc", d(description=t)),
                          ("contains", d(textContains=t))):
            if sel.exists:
                sel.click(timeout=15)
                emit({"ok": True, "match": kind})
                return 0
        emit({"ok": False, "error": f"no node matching {t!r}"})
        return 3

    if action == "set-text":
        rid, val = args[0], args[1]
        el = d(resourceId=rid)
        el.click(timeout=15)      # accessibility click => real view focus
        el.clear_text()
        el.set_text(val)          # ACTION_SET_TEXT => bypasses the IME
        emit({"ok": True, "text": el.get_text()})
        return 0

    emit({"ok": False, "error": f"unknown action {action!r}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
