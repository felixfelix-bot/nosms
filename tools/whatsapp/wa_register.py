#!/usr/bin/env python3
"""One-pass WhatsApp registration driver for the wa-dev emulator (E2).

Shape (b) of the cross-host handoff, per the card: this runs on CobradorWave,
talks to the adb server on DQ05 through the ssh port-forward
(ADB_SERVER_SOCKET=tcp:127.0.0.1:15037), and reads the OTP locally from
jmp_inbox.db — the whole loop is local; only the emulator stays on dq05.

Registration target: the operator's LIVE JMP number (810) 294-4652. The
manager's 2026-10-06 card comment settles the number; 258-3253 (released) and
258-3233 (older doc) are WRONG and this script hard-refuses them.

SAFETY CONTRACT (card TASKS #3): ONE pass, NO blind retry. If WhatsApp demands
a voice call, refuses the VoIP number, or the OTP does not arrive within the
bound, we STOP loudly and dump the UI state — a retry loop turns a warning into
a permanent ban.

Phases (each idempotent-ish, all evidence-dumped):
  gate     show dq05 load; abort unless load < 4 (card precondition)
  state    dump UI XML, show where registration stands
  register tap the phone field, type the number, tap NEXT
  otp      poll jmp_inbox.db for a new inbound SMS, extract the code, type it
  evidence dumpsys versionName + screenshots + UI XML

Usage:
  wa_register.py gate|state|register|otp|evidence [--out DIR]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_ui  # noqa: E402  (same dir; adb-over-forward client)

SERIAL = os.environ.get("SERIAL", "emulator-5554")

# --- The number (settled by manager comment 2026-10-06; see module docstring) ---
JMP_NUMBER = os.environ.get("JMP_NUMBER", "8102944652")
FORBIDDEN = {"8102583253", "8102583233"}  # released / older-doc numbers

INBOX_DB = os.path.expanduser(os.environ.get(
    "JMP_INBOX_DB", "~/.hermes/profiles/manager/state/jmp_inbox.db"))

OTP_WAIT_S = int(os.environ.get("OTP_WAIT_S", "420"))     # 7 min bound
OTP_POLL_S = 5
VOICE_REFUSAL_MARKERS = (
    "call me", "voice call", "phone call", "we're unable", "we are unable",
    "not supported", "unsupported", "invalid number", "incorrect number",
    "try again", "resend", "wrong number",
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fail_loud(msg: str, out_dir: str | None = None) -> None:
    """Card TASKS #3: fail LOUDLY and stop. Never retry from inside this tool."""
    print(f"\n!!! STOP — {msg}", file=sys.stderr)
    print("!!! No retry will be attempted (a retry loop risks a permanent ban).",
          file=sys.stderr)
    if out_dir:
        dump_ui(os.path.join(out_dir, "STOP.xml"))
        screenshot(os.path.join(out_dir, "STOP.png"))
    sys.exit(42)


def adb_shell(cmd: str, timeout: int = 90) -> str:
    rc, out, err = wa_ui.adb("shell", cmd, timeout=timeout)
    if rc != 0 and "error" in err.lower():
        raise RuntimeError(f"adb shell {cmd!r} failed: {err.strip()}")
    return out


def dump_ui(path: str) -> str:
    xml = wa_ui.dump_xml()
    with open(path, "w") as f:
        f.write(xml)
    return xml


def screenshot(path: str) -> None:
    wa_ui.adb("shell", "screencap -p /sdcard/wa_ev.png", timeout=60)
    wa_ui.adb("pull", "/sdcard/wa_ev.png", path, timeout=60)


# --- uiautomator2 interaction layer (run-173 fix path (1)) -----------------
# Run-172 root cause: on the headless guest, synthetic `input tap` never gives
# the WhatsApp EditText view focus (mServedView stuck on menuitem_overflow,
# mInputShown=false), so `input text` AND keyevent digits both silently drop.
# Fix: interaction goes through uiautomator2 ON dq05 — an accessibility click
# sets real focus and ACTION_SET_TEXT bypasses the IME entirely. u2's sockets
# bind to the adb server (dq05), so the probe runs there over ssh and only a
# JSON result crosses the boundary. NO gates/halt/refuse logic lives there.
U2_PY = os.environ.get("U2_PY", "~/wa_u2_venv/bin/python")
U2_PROBE = os.environ.get("U2_PROBE", "/tmp/u2_probe.py")


def u2(action: str, *args: str) -> dict:
    """Run one u2 primitive on dq05; return its JSON result (last stdout line)."""
    argline = " ".join(shlex.quote(a) for a in (action, *args))
    p = subprocess.run(
        ["ssh", "dq05", f"{U2_PY} {U2_PROBE} {argline}"],
        capture_output=True, text=True, timeout=120)
    lines = [l for l in p.stdout.strip().splitlines() if l.strip()]
    if p.returncode != 0 or not lines:
        raise RuntimeError(
            f"u2 {action!r} failed rc={p.returncode}: {p.stderr.strip()[-400:]}")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        raise RuntimeError(f"u2 {action!r} non-JSON output: {lines[-1][:200]}")



def check_refusals(xml: str, out_dir: str) -> None:
    """Voice-call demand / VoIP refusal / ban warning => STOP, no retry."""
    haystack = " ".join(
        n["text"] + " " + n["content-desc"]
        for n in wa_ui.nodes(xml) if (n["text"] or n["content-desc"]))
    low = haystack.lower()
    for marker in VOICE_REFUSAL_MARKERS:
        if marker in low:
            fail_loud(f"WhatsApp UI shows refusal marker {marker!r} — voice call "
                      f"demanded or VoIP number refused", out_dir)


def inbox_baseline() -> int:
    with sqlite3.connect(f"file:{INBOX_DB}?mode=ro", uri=True) as db:
        return db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]


def poll_otp(baseline: int, out_dir: str) -> str:
    """Poll jmp_inbox.db for the first inbound SMS with id > baseline; return code."""
    t0 = time.time()
    log(f"polling jmp_inbox.db for id > {baseline} (bound {OTP_WAIT_S}s)")
    while time.time() - t0 < OTP_WAIT_S:
        xml = dump_ui(os.path.join(out_dir, "otp_waiting.xml"))
        check_refusals(xml, out_dir)
        try:
            with sqlite3.connect(f"file:{INBOX_DB}?mode=ro", uri=True) as db:
                rows = db.execute(
                    "SELECT id, ts, peer, body FROM messages WHERE id > ? "
                    "AND direction='in' ORDER BY id", (baseline,)).fetchall()
        except sqlite3.Error as e:
            log(f"inbox read error (continuing): {e}")
            rows = []
        for rid, ts, peer, body in rows:
            log(f"inbox id={rid} peer={peer} body={body!r}")
            m = re.search(r"\b(\d{6})\b", body or "")
            if m:
                log(f"OTP found in inbox id={rid}: {m.group(1)}")
                return m.group(1)
            if rows:
                log("inbound arrived without a 6-digit code; keeping watch")
        time.sleep(OTP_POLL_S)
    fail_loud(f"no OTP arrived within {OTP_WAIT_S}s — STOP, not retrying", out_dir)
    return ""  # unreachable


def cmd_gate(_args) -> int:
    load = subprocess.run(
        ["ssh", "dq05", "cat /proc/loadavg"],
        capture_output=True, text=True, timeout=30).stdout.split()[0]
    lv = float(load)
    log(f"dq05 1-min load = {lv}")
    if lv >= 4.0:
        print("GATE: FAIL — dq05 too busy for the emulator; try later")
        return 1
    print("GATE: PASS")
    return 0


def cmd_state(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    xml = dump_ui(os.path.join(args.out, "state.xml"))
    wa_ui.print_nodes(xml)
    ver = adb_shell("dumpsys package com.whatsapp | grep versionName")
    print(f"installed: {ver.strip()}")
    return 0


def cmd_register(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    if JMP_NUMBER in FORBIDDEN:
        fail_loud(f"refusing to register with forbidden number {JMP_NUMBER}")
    if not re.fullmatch(r"\d{10}", JMP_NUMBER):
        fail_loud(f"JMP_NUMBER must be 10 digits, got {JMP_NUMBER!r}")
    log(f"registering with JMP number {JMP_NUMBER[:3]}-{JMP_NUMBER[3:6]}-{JMP_NUMBER[6:]}")

    xml = dump_ui(os.path.join(args.out, "10_pre_register.xml"))
    check_refusals(xml, args.out)
    phone_node = wa_ui.find(xml, rid="com.whatsapp:id/registration_phone")
    if phone_node is None:
        # maybe already past this screen — dump and fail loudly with context
        wa_ui.print_nodes(xml)
        fail_loud("phone-entry field not on screen — registration state unexpected", args.out)
    log("phone-entry screen confirmed; entering number via uiautomator2")
    # Run-172 lesson: `input tap` never focuses this EditText on the headless
    # guest (accessibility focus transfer is the ONLY thing that works). u2's
    # click sets real view focus; set_text (ACTION_SET_TEXT) bypasses the IME.
    # Field edits NEVER trigger an OTP — only NEXT/OK does — so one bounded
    # re-entry attempt stays ban-safe.
    res = u2("set-text", "com.whatsapp:id/registration_phone", JMP_NUMBER)
    shown = "".join(re.findall(r"\d", res.get("text") or ""))
    log(f"set-text result: text={res.get('text')!r} digits={shown!r}")
    # uiautomator2's immediate get_text() is the authoritative result of
    # ACTION_SET_TEXT. A follow-up .info RPC is deliberately avoided: on the
    # remote atx-agent it can disconnect transiently after clear/set_text,
    # while the returned text is already the same accessibility value.
    if JMP_NUMBER not in shown:
        log(f"field shows {shown!r}; one ban-safe re-entry attempt")
        res = u2("set-text", "com.whatsapp:id/registration_phone", JMP_NUMBER)
        shown = "".join(re.findall(r"\d", res.get("text") or ""))
        if JMP_NUMBER not in shown:
            xml = dump_ui(os.path.join(args.out, "11_number_failed.xml"))
            wa_ui.print_nodes(xml)
            fail_loud(f"phone field shows {shown!r}, expected {JMP_NUMBER} — abort before NEXT", args.out)
    xml = dump_ui(os.path.join(args.out, "11_number_typed.xml"))
    check_refusals(xml, args.out)
    # capture the inbox baseline NOW, immediately before NEXT triggers the OTP
    # send — if we read it later, a fast OTP could land at id <= baseline.
    baseline = inbox_baseline()
    baseline_path = os.path.join(args.out, "otp_baseline.txt")
    with open(baseline_path, "w") as f:
        f.write(str(baseline))
    log(f"otp baseline {baseline} persisted to {baseline_path}")
    log(f"field verified: {shown!r}; tapping NEXT")
    res = u2("click-text", "NEXT")
    log(f"NEXT click: {res}")
    time.sleep(4)
    xml = dump_ui(os.path.join(args.out, "12_after_next.xml"))
    check_refusals(xml, args.out)
    wa_ui.print_nodes(xml)
    log("NEXT tapped; screen after:")
    # allow the confirm-dialog variant
    if wa_ui.find(xml, text="OK") and wa_ui.find(xml, text="confirm", exact=False):
        log("confirmation dialog detected; tapping OK")
        res = u2("click-text", "OK")
        log(f"OK click: {res}")
        time.sleep(4)
        xml = dump_ui(os.path.join(args.out, "13_after_confirm.xml"))
        check_refusals(xml, args.out)
    log("register phase done — now run the otp phase (baseline captured at NEXT-tap)")
    return 0


def cmd_otp(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    # prefer the baseline persisted at NEXT-tap time (see cmd_register); the OTP
    # can land in the gap between NEXT and this phase's own read.
    baseline_path = os.path.join(args.out, "otp_baseline.txt")
    if os.path.exists(baseline_path):
        baseline = int(open(baseline_path).read().strip())
        log(f"inbox baseline id={baseline} (persisted at NEXT-tap)")
    else:
        baseline = inbox_baseline()
        log(f"inbox baseline id={baseline} (fresh read — WARNING: register phase did not persist one)")
    code = poll_otp(baseline, args.out)
    xml = dump_ui(os.path.join(args.out, "20_otp_screen.xml"))
    check_refusals(xml, args.out)
    entry = wa_ui.find(xml, rid="com.whatsapp:id/verification_code")
    if entry is None:
        # some builds present a plain editable code field without that id
        entry = wa_ui.find(xml, rid="com.whatsapp:id/eula_prompt")
    if entry is None:
        wa_ui.print_nodes(xml)
        fail_loud("verification-code entry not found on screen", args.out)
    log(f"typing code {code} via uiautomator2")
    res = u2("set-text", entry["resource-id"], code)
    log(f"set-text result: text={res.get('text')!r}")
    time.sleep(6)
    xml = dump_ui(os.path.join(args.out, "21_code_typed.xml"))
    check_refusals(xml, args.out)
    wa_ui.print_nodes(xml)
    log("code entered; check evidence phase for the resulting state")
    return 0


def cmd_evidence(args) -> int:
    os.makedirs(args.out, exist_ok=True)
    ver = adb_shell("dumpsys package com.whatsapp | grep -E 'versionName|versionCode'")
    print(ver)
    xml = dump_ui(os.path.join(args.out, "30_registered_state.xml"))
    wa_ui.print_nodes(xml)
    screenshot(os.path.join(args.out, "30_registered_state.png"))
    acct = adb_shell(
        "dumpsys account 2>/dev/null | grep -iA2 whatsapp | head -10")
    print(acct or "(no whatsapp account in dumpsys account)")
    log("evidence captured")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["gate", "state", "register", "otp", "evidence"])
    p.add_argument("--out", default=os.path.join(HERE, "evidence", "e2"),
                    help="dir for UI dumps / screenshots")
    args = p.parse_args()
    if args.phase == "gate":
        return cmd_gate(args)
    if args.phase == "state":
        return cmd_state(args)
    if args.phase == "register":
        return cmd_register(args)
    if args.phase == "otp":
        return cmd_otp(args)
    return cmd_evidence(args)


if __name__ == "__main__":
    sys.exit(main())
