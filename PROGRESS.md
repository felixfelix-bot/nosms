# PROGRESS — t_c930a683 (nosms WhatsApp E2: APK + JMP OTP registration)

Branch: wt/t_c930a683 (pushed to origin). Worktree: ~/repos/nosms/.worktrees/t_c930a683

## Findings → status → files
- Recovered prior-run assets from ~/worktrees/t_c930a683 (old workspace): wa_ui.py, adb37, ui dumps → committed as tools/whatsapp/wa_ui.py (31fd2b8)
- Number settled per manager comment: (810) 294-4652; 258-3253 RELEASED (forbidden), 258-3233 forbidden → hardcoded refuse-list in wa_register.py
- One-pass registration driver written, fail-loud (exit 42), NO retry: tools/whatsapp/wa_register.py (31fd2b8)
- dq05 load source identified: state_db_autorepair.py --apply (Hermes infra, bounded budget) hammering sda; NOT killed (not ours) → tools/probe_disk_writers.py
- Bring-up wrapper reusing canonical harness over ssh: tools/whatsapp/e2_bringup.sh (dcdd49c)
- adb tunnel 127.0.0.1:15037 -> dq05:5037 UP; JMP listener alive (pid 2731), inbox baseline id=4
- Emulator: DOWN at run start; wa-dev AVD + SDK intact on dq05; WhatsApp 2.26.39.75 installed (manager-verified dumpsys)

## Runbook (the one pass, in order)
1. `python3 tools/whatsapp/wa_register.py gate` — dq05 load < 4 required
2. `bash tools/whatsapp/e2_bringup.sh boot` (canonical harness; ~2-4 min boot)
3. `bash tools/whatsapp/e2_bringup.sh tunnel` (idempotent)
4. `python3 tools/whatsapp/wa_register.py state` — confirm phone-entry screen
5. `python3 tools/whatsapp/wa_register.py register` — types 8102944652, verifies, taps NEXT (STOPS on any voice-call/refusal marker)
6. `python3 tools/whatsapp/wa_register.py otp` — polls jmp_inbox.db id>baseline for 6-digit code, types it
7. `python3 tools/whatsapp/wa_register.py evidence` — dumpsys versionName + screenshot + UI dump
8. `bash tools/whatsapp/e2_bringup.sh teardown` — never leave the 3.3G guest on the 4-core box
9. commit evidence/ + push; kanban_complete (child E3 t_9f3b1c8a released by completion)

- Run 173: uiautomator2 set-text returned and verified live number; follow-up info RPC disconnected, so validation now uses set-text response; NEXT accessibility click returned success but screen stayed on phone-entry and inbox max id remained 4; stopped with zero OTP/ban risk → tools/whatsapp/wa_register.py, tools/whatsapp/evidence/e2/

## STOP conditions (fail-loud, exit 42, never retry)
- voice call demanded / VoIP number refused / "we're unable" / "invalid number" / retry-nudges
- OTP not in inbox within 420s
- phone field shows unexpected digits

## 2026-10-08 14:15 — run 171 outcome: LOAD-GATED, NOT ATTEMPTED
- dq05 never dropped below load ~7 for the whole run (gate needs <4).
- Cause (measured, probe_io_long.py): Hermes state_db repair cycle saturating sda:
  state_db_autorepair.py --apply ran 45min (pid 3288833, ~12MB/s r+w, D-state),
  then state_db_snapshot.py + a SECOND autorepair started (pids 3423901/3396235).
  iostat: sda 76% util, w_await 53-188ms. Not killed — manager-owned infra.
- Per card doctrine the registration pass was NOT attempted (no OTP requested,
  no NEXT tapped; ban risk zero). No retry loop.
- Next worker: when `ssh dq05 'cat /proc/loadavg'` shows 1-min < 4, run the
  runbook above top-to-bottom (~10 min). Everything is committed on
  wt/t_c930a683 (HEAD ba094df) — driver, harness wrapper, offline tests.

## 2026-10-09 00:15 — run 172 (manager, load window opened): REGISTER BLOCKED — root cause found
- dq05 load 3.67 → GATE PASS. Emulator already up (emulator-5554), WhatsApp 2.26.39.75,
  phone-entry screen confirmed via `state` (US/+1, NEXT present).
- `register` aborts fail-loud at its own focus check (correct: no OTP requested, ban risk ZERO).
- Manual diagnosis (bounded, no retry loop):
  * `input text` AND `input keyevent` digits BOTH drop — field never receives characters.
  * `dumpsys input_method`: mServedView stuck on `menuitem_overflow` (or null), mInputShown=false —
    the IME never starts serving the EditText. Taps land (window focus = RegisterPhone) but
    view focus never transfers.
  * Screenshot shows a green underline on the phone field in EVERY frame including pre-tap —
    that is WhatsApp's static field styling, NOT a focus indicator. The prior "focus probe"
    heuristic (underline = focused) is unreliable; `focused=false` in the dump was right.
- ROOT CAUSE (structural): headless emulator + Gboard → EditText on WhatsApp's registration
  screen does not take view focus from synthetic taps, so no text commits. Known class; the
  standard fixes (pick one on the next pass):
  1. **uiautomator2 (python) driven click + set_text** — accessibility click CAN set focus and
     set_text bypasses the IME entirely. Preferred: no extra APK, same evidence path.
  2. ADBKeyboard IME (broadcast text) — proven for headless WhatsApp automation, but adds an
     APK and still needs focus for field targeting.
- NEXT PASS: rewrite wa_register.py's interaction layer on uiautomator2 (keep the fail-loud
  gates + refuse-lists verbatim), then rerun steps 4-9 of the runbook. Everything else is DONE:
  boot wrapper, tunnel, OTP handoff, inbox baseline (id=4), evidence dir.
bounded NEXT selector and Enter both left WhatsApp on phone-entry screen; no OTP requested -> diagnostic evidence captured -> tools/whatsapp/bounded_next_probe.py, tools/whatsapp/evidence/e2/next_probe/*
