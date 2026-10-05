# Evidence — JMP/Cheogram rail cold-send proof (2026-10-05)

Task: **t_ac001750** (nosms M5-rail). This directory is the primary-source record for the
one link the reply-only listener never exercised: a **cold, unsolicited** outbound SMS from
the operator's JMP/Cheogram line.

## What is here

| file | what it proves |
|---|---|
| `cold-send-20261005T002427Z.json` | the exact outbound stanza (built by `scripts/jmp_cold_send_probe.py`) + the response window |
| `adapter-smoke-stdout.jsonl` | the adapter's own long-lived link, run as the service builds it (`scripts/jmp_rail_smoke.py`) |

## 1. Cold-send stanza (00:24:27Z)

Message id `ba7d0fd7000445c69745f915bad3b307`, built from scratch (type `chat`, no reply /
thread context) and addressed to the newest inbound peer:

```xml
<message xmlns="jabber:client" id="ba7d0fd7000445c69745f915bad3b307" xml:lang="en"
         to="+132****8875@cheogram.com" type="chat">
  <body>nosms cold-send probe 799a131b — unsolicited outbound, not a reply. 2026-10-05T00:24:27Z</body>
</message>
```

`accepted: true` — the stanza left the client and **no error/refusal stanza** arrived in a
25 s window (`responses: []`).

> **Expected text on the handset** (`+1 … 8875`):
> `nosms cold-send probe 799a131b — unsolicited outbound, not a reply. 2026-10-05T00:24:27Z`

## 2. Adapter smoke (00:28:01Z / 00:28:05Z)

`scripts/jmp_rail_smoke.py` built the rail through `build_transport("jmp_only")`, started the
long-lived link, and called `Transport.send()`:

```json
{"event": "connected", "jid": "hermes-jmp@jabber.fr", "capabilities": {"rail": "jmp_cheogram",
 "available": true, "best_effort": true, "delivery_receipts": false, "countries": ["US", "CA"],
 "down_reason": null, "countries_note": "US/Canada numbering only; SMS over SIP unsupported"}}
{"event": "send_result", "to": "+1******8875", "accepted": true, "rail": "jmp_cheogram",
 "receipt": null, "detail": "accepted by the JMP gateway; no delivery receipt exists"}
```

The outbound rows it wrote to the shared sqlite inbox
(`~/.hermes/profiles/manager/state/jmp_inbox.db`):

```
3  2026-10-05T00:28:01Z  out  nosms rail smoke 002801Z — adapter path, not a reply
4  2026-10-05T00:28:05Z  out  nosms rail smoke 002805Z — adapter path, not a reply
```

## 3. Inbound capture (the sqlite inbox)

The rail's inbound direction is already recorded, and it is what cleared JMP's anti-abuse
gate (a new account may not send until it has received one real inbound text):

```
1  2026-10-02T15:44:14Z  in   "Test again"     <- operator's handset, real inbound SMS
2  2026-10-02T15:44:14Z  out  "Hermes here - your SMS arrived. …"  <- the listener's reply
```

## What is **not** here, and why

* **Handset receipt is not a stanza.** This rail has no delivery receipts (ADR-0002, accepted
  risk #3) — nothing in the protocol proves the phone buzzed. Only the operator can confirm
  that. The texts to confirm, in order:
  1. `nosms cold-send probe 799a131b — unsolicited outbound, not a reply. 2026-10-05T00:24:27Z`
  2. `nosms rail smoke 002801Z — adapter path, not a reply`
  3. `nosms rail smoke 002805Z — adapter path, not a reply`
* **No truly-arbitrary destination.** Both sends went to the operator's own handset — the only
  number this worker has that it can observe. Sending to a number that has **never** messaged
  the JMP line would exercise JMP's cold-send gate on an unknown peer; that needs a second
  handset from the operator (`scripts/jmp_cold_send_probe.py --to +1…`).

The destination number is masked in every committed artefact; the raw E.164 value was read
only inside the process (from the listener's own sqlite column), never printed.
