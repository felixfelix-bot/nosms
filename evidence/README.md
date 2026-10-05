# evidence/

Real output from the shipped code, committed so a reviewer does not have to take
a claim on trust.

| file | produced by | what it shows |
|---|---|---|
| `jmp-cvm-probe.json` | `python3 scripts/jmp_cvm_probe.py --out evidence/jmp-cvm-probe.json` | what the served JMP tools answer today: `jmp.status` returning the capture with `source: cached`, `jmp.funding` refusing with `rail_unavailable` (no live driver wired), and `jmp.credentials` refusing with `owner_only` plus the store precondition report (`store: openbao`, `present: false`) — the documented OpenBao blocker, from a real run. |
| `cold-send-20261005T002427Z.json` | `scripts/jmp_cold_send_probe.py` | the exact outbound JMP/Cheogram stanza plus the response window; the destination is redacted by construction — details in the rail section below. |
| `adapter-smoke-stdout.jsonl` | `scripts/jmp_rail_smoke.py` | the adapter's own long-lived link, run the way the service builds it — details below. |

Nothing in this directory is hand-written: re-run the commands above and the files
should be byte-identical apart from the timestamps the tool returns.

---

# Evidence — JMP/Cheogram rail cold-send proof (2026-10-05)

Task: **t_ac001750** (nosms M5-rail). This directory is the primary-source record for the
one link the reply-only listener never exercised: a **cold, unsolicited** outbound SMS from
the operator's JMP/Cheogram line.

## What is here

| file | what it proves |
|---|---|
| `cold-send-20261005T002427Z.json` | the exact outbound stanza (built by `scripts/jmp_cold_send_probe.py`) + the response window; the destination is redacted **by construction** (see §4) |
| `adapter-smoke-stdout.jsonl` | the adapter's own long-lived link, run as the service builds it (`scripts/jmp_rail_smoke.py`) |

## 1. Cold-send stanza (00:24:27Z)

Message id `ba7d0fd7000445c69745f915bad3b307`, built from scratch (type `chat`, no reply /
thread context) and addressed to the newest inbound peer:

```xml
<message xmlns="jabber:client" id="ba7d0fd7000445c69745f915bad3b307" xml:lang="en"
         to="+1******8875@cheogram.com" type="chat">
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

The destination number is masked in every committed artefact by
`scripts/jmp_cold_send_probe.py`'s single `mask()` convention: the country digit plus the
last four, everything between replaced by `*` (`+1******8875`, and the JID form
`+1******8875@cheogram.com`). The evidence JSON is *generated* with that mask applied to
every field and to every stanza, so it is redacted by construction rather than by hand and
holds no un-masked destination; the file itself says so in-band (`redaction_note`). The
un-redacted record (the one carrying the real E.164, used to regenerate the published copy)
is kept outside version control under `~/reports/nosms/`.

## 4. Redaction — one convention, by construction

There is exactly **one** masking convention for this destination, implemented once in
`scripts/jmp_cold_send_probe.py::mask` and applied by `redact()` to every string the probe
writes. The record is machine-generated by `build_evidence()` from the un-redacted raw
record, so the JSON and this README cannot drift apart and cannot disagree about the mask.

* No field is named `raw` while holding a masked value: the destination fields are
  `to_masked` and `to_masked_jid`, and both carry the same mask.
* `outbound_stanza_xml` and any response stanza are passed through `redact()`, so an
  E.164 appearing in the `to=`/`from=` attribute is masked too (the mask is idempotent).
* An un-redacted copy is only produced on request (`--raw-out`), is chmod `0600`, is
  labelled `WARNING: UN-REDACTED` in the file, and is never committed.

Re-scan after a change: `gitleaks detect -c .gitleaks.toml` covers the repo, and

```bash
python - <<'PY'
import re, pathlib
blob = pathlib.Path("evidence/cold-send-20261005T002427Z.json").read_text()
# An un-masked E.164 is a '+' followed by 11 CONSECUTIVE digits. Do NOT strip
# '*' first: that would splice the mask's kept prefix and suffix into a run and
# report a false positive (e.g. '+1******8875' -> '+18875').
bad = re.findall(r"\+\d{11,}", blob)
print("un-masked E.164 runs:", bad or "none")
PY
```

prints the un-masked E.164 runs (expected: none). `tests/test_jmp_probe.py` pins this same
check, so a hand-edit that reintroduced a real number would fail the suite, not just the scan.
