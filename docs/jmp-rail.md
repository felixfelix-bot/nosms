# The JMP/Cheogram rail (nosms M5-rail)

Implementation of **ADR-0002** — *operator override of JMP's automation policy +
rugpull-risk-premium pricing*. The v1 rail of the paid public nosms service is the
operator's own JMP/Cheogram line. The ADR lives with the plan
(`~/worktrees/nosms-plan/docs/adr/0002-jmp-rail-override-and-risk-pricing.md`); this
document is the code's half of it.

> ToS note, restated because it matters: JMP's FAQ forbids automated/business use
> ("no automations, marketing, or campaigns"). The operator has explicitly overridden
> that decision and owns the risk. The code does not re-litigate it; it prices it
> (walk-down pricing) and contains it (pacing, degrade path, no delivery claims).

## Shape

```
        ┌─────────────────────────── one process ───────────────────────────┐
        │  SlixmppLink            JmpCheogramTransport      FailoverTransport│
        │  (jmp_link.py)          (jmp_cheogram.py)         (failover.py)    │
        │  long-lived XMPP   ──▶  cold send + pacing   ──▶  JMP ─▶ email      │
        │  client + sqlite        + capability flags        degrade path     │
        │  inbox + reconnect                                                 │
        └───────────────────────────────────────────────────────────────────┘
                        ▲                                    ▲
                        │ Transport.send(dest, body)         │ ADR-0002 §2 pacing
                        │                                    │ ADR-0002 §3 degrade
```

* `JmpCheogramTransport.send()` sends a **cold** chat `<message>` to
  `+<E164>@cheogram.com` — never `msg.reply(...)`, which is the shape only a human
  conversation can use.
* The link is **in-process** (a thread + its own asyncio loop), so the process that
  serves the CVM tools is the process holding the XMPP session. One process, not two.
* `FailoverTransport` puts the email rail behind JMP and keeps refundability honest.

## The one link that was unproven — now proven

`jmp-sms-listener.py` proved the *reply* direction. A service cannot use a reply.
On **2026-10-05T00:24:27Z** a brand-new stanza was sent to the newest inbound peer:

```xml
<message xmlns="jabber:client" id="ba7d0fd7000445c69745f915bad3b307" xml:lang="en"
         to="+1******8875@cheogram.com" type="chat">
  <body>nosms cold-send probe 799a131b — unsolicited outbound, not a reply. 2026-10-05T00:24:27Z</body>
</message>
```

Result: **accepted** — no error/refusal stanza in a 25 s window. Raw artefact:
[`evidence/cold-send-20261005T002427Z.json`](../evidence/cold-send-20261005T002427Z.json)
(the destination is redacted by construction; `evidence/README.md` §4 has the convention).
The adapter’s own link was then exercised live (`scripts/jmp_rail_smoke.py`): the
long-lived client connected, served its capability flags, and the Transport returned
`accepted=True rail=jmp_cheogram receipt=None`.

Reproduce:

```bash
# raw stanza proof (any interpreter with slixmpp; the fleet uses ~/.venvs/jmp-probe)
python scripts/jmp_cold_send_probe.py --dry-run            # build the stanza, send nothing
python scripts/jmp_cold_send_probe.py --out evidence/cold-send-$(date -u +%Y%m%dT%H%M%SZ).json

# the adapter, exactly as the service builds it
python scripts/jmp_rail_smoke.py            # newest inbox peer
python scripts/jmp_rail_smoke.py --to +1XXXXXXXXXX
```

## Capability flags — served from the rail, never hardcoded

`GET /api/health` / the CVM `sms.capabilities` tool must read these from the rail that
is actually serving. `JmpCheogramTransport.capability_flags()` (and
`FailoverTransport.capability_flags()` when degraded) is the source:

| flag | value | why |
|---|---|---|
| `available` | live: link state, and false once the rail is marked down | a health flag that is always `true` is not a health flag |
| `best_effort` | `true` | acceptance is not delivery |
| `delivery_receipts` | `false` | permanent on this rail (ADR-0002 accepted risk #3) — `receipt` is always `None` |
| `countries` | `["US", "CA"]` | JMP numbering is US/Canada only; SMS over SIP is unsupported |

A non-`+1` destination raises `destination_unsupported` **before** the link is touched.
US/CA is enforced by an explicit NANP gate (`app/transports/nanp.py`, shared with the
email rail): a Caribbean `+1` (Jamaica 876, Trinidad 868, …) or a US territory
(Puerto Rico 787, USVI 340, Guam 671, …) is **not** US/CA and is refused, so the gate
means exactly what `countries` advertises.

## Acceptance = the stanza reached the stream, and the deadline cannot lie about it

`SlixmppLink.send_message` blocks until the outbox entry is settled and raises
`RailUnavailable` when the rail could not take the message — it is the only
signal the refund path has, so a wrong answer is a money bug, not a cosmetics
bug. Two properties hold (`app/transports/jmp_link.py`):

* **The deadline and the hand-off are mutually exclusive.** `_sender` holds
  `_handoff_lock` across the whole hand-off — *is the entry still wanted? →
  `msg.send()` → settle the future* — and `send_message`'s timeout path takes the
  same lock. Either the future is still pending (the cancel wins: `_sender`
  skips a `done()` entry, so the stanza provably never goes out and a refundable
  `connection_lost` is honest), or the hand-off already won and the future
  carries its real outcome: accepted, or the sender's own failure. Reporting
  "not sent" on a stanza that went out refunds a delivered message **and** lets
  `FailoverTransport` re-send it over the email rail.
* **Nothing inside the critical section may block.** The ack is settled
  immediately after `msg.send()` (slixmpp's `send()` is non-blocking queueing)
  and the outbox sqlite `INSERT` runs after it, off the loop thread. The log was
  the >30 s stall that used to make a deadline expire on a message that had
  already left the client; a broken inbox can no longer turn an accepted send
  into a refund, or kill `_sender` (which would mute the rail while it still
  reported itself available).

A caller waiting on the lock is therefore bounded: the sender is only ever
inside it for a non-blocking hand-off. If it somehow is not, the bounded wait
expires and `send_message` returns acceptance rather than claiming "not sent" —
a best-effort rail with no delivery receipts cannot prove the stanza did not
go out.

## Pacing (ADR-0002 compensating control #2)

The line is personal, so volume *is* the abuse surface. Two persisted limits, in
`app/transports/pacing.py`:

| knob | env | default |
|---|---|---|
| daily cap (UTC day) | `NOSMS_JMP_DAILY_CAP` | `20` |
| jittered minimum gap | `NOSMS_JMP_MIN_GAP_SECONDS` / `NOSMS_JMP_MAX_GAP_SECONDS` | `60` / `300` s |
| state file | `NOSMS_JMP_PACING_STATE` | `~/.hermes/profiles/manager/state/jmp_pacing.json` |

The next gap is re-drawn uniformly from the range after **every** accepted send — a
fixed interval is itself a signature. State is persisted, so a reconnect or a process
restart cannot reset the counter.

**Reserve before the send.** A service endpoint is served from a threadpool, so two
callers can arrive at the same instant. `check()` → blocking send → `record_send()`
would be a check-then-act race: both callers see `count == daily_cap - 1` and both
send, voiding both the cap and the gap. The only supported path for a real send is
therefore:

1. `claim()` — atomically **reserves** the slot under a lock *before* the send is
   attempted, and persists it (a process that dies mid-send does not hand the same
   slot to its successor);
2. `release(claim, accepted=True)` — confirms the send and starts the jittered gap;
   `release(claim, accepted=False)` — **refunds** the slot, because a message that
   never left the client must not consume the allowance or move the clock.

`check()` remains read-only (the 429 signal, never reserves). A paced call raises
`RailPaced` — it is **not** a failed `SendResult`. The message was never attempted, so
charging-then-refunding would be wrong; the caller answers `429` + `Retry-After` (from
`RailPaced.retry_after`) and the payer retries. Concurrent callers are serialised: one
sends, the others get `daily_cap_reached` / `send_in_flight` and retry.

**A corrupt counter fails closed.** State that cannot be parsed used to degrade to a
blank day, i.e. silently restore a *full* daily cap — the unsafe direction for an abuse
surface. It is now refused with `pacing_state_corrupt` on every claim, the unreadable
bytes are left in place (never overwritten), and the refusal survives restarts until an
operator clears the file. `snapshot()` reports `corrupt: true` so `/api/health` shows it.

## Degrade path (ADR-0002 compensating control #3)

`FailoverTransport(primary=JMP, fallback=email)`. Refunds are the escrow layer's job
(CEP-8 `explicit_gating` refunds when `SendResult.accepted` is `False`), so the wrapper
exists to keep that flag *honest*:

| situation | returns | refund? |
|---|---|---|
| primary accepted | `accepted=True`, `rail=jmp_cheogram` | no |
| primary down, fallback accepted | `accepted=True`, `rail=email_gateway` | no (it was sent) |
| both fail | `accepted=False`, `refundable=True` | **yes** |
| fallback cannot serve the destination | `accepted=False`, `refundable=True` | **yes** |
| fallback rail itself raises (`RailUnavailable` / any exception) | `accepted=False`, `refundable=True`, logged | **yes** |
| primary paced | `RailPaced` propagates | no — defer, do not refund |
| primary rejects the request (bad destination / empty body) | returned as-is | per the result |
| primary transient (reconnect window) | `accepted=False`, `primary_transient:*` | **yes** — never diverted to email |

Degrading around pacing would defeat pacing and dump the load on the email rail's own
abuse surface, so `RailPaced` deliberately propagates.

**Only a terminal failure degrades.** The degrade path is for a rail that cannot
recover by itself (`auth_failed` / `terminated`). A routine reconnect
(`not_connected` / `connection_lost`) does **not** divert paid traffic to the email
rail — every reconnect window would otherwise hand the load to the email rail's own
abuse surface. The caller gets a refundable `primary_transient:*` result and the send
can be retried on the primary. The fallback failing is itself made visible: a
`RailUnavailable` or any other exception from the fallback becomes an
`accepted=False` `all_rails_failed:*` result and a `logging.warning`, never a raw
exception the escrow layer is merely assumed to catch.

## Operations

```bash
# run the service on the JMP rail (email degrade behind it)
NOSMS_TRANSPORT=jmp_cheogram            # -> FailoverTransport(JMP, email)
NOSMS_TRANSPORT=jmp_only                # -> JMP alone (no degrade)
NOSMS_JMP_CRED_JSON=~/.xmpp-hermes-jmp@jabber.fr.json   # 0600 runtime credential
NOSMS_JMP_RESOURCE=nosms                # XMPP resource (must not collide with a listener)
```

* **Kill switch.** `transport.mark_down("terminated")` (or setting the service back to
  `NOSMS_TRANSPORT=email_gateway`) sends nothing further over JMP and moves the service
  straight to the email rail; anything already paid and unsent is refundable.
* **`slixmpp` is optional at import time.** It is imported lazily inside the link, so
  `import app.transports` works without an XMPP stack (the package is in
  `requirements.txt` for the deployed service).

## Honest limitations (do not paper over)

1. **No delivery receipts, ever.** A send is `accepted`, never `delivered`.
2. **Cold outbound is gated by JMP.** A new account may not send until it has received
   one real inbound text from a person. The account used here already cleared that gate.
3. **The handset receipt is human evidence.** No stanza proves the phone buzzed — only
   the operator can confirm that (see `evidence/README.md`).
4. **US/Canada only.**
5. **Inbound is one-shot.** It exists only while a client is attached and is consumed on
   read; that is why the link stays attached rather than polling.
6. **The ToS risk is the operator's, and it is priced, not solved.**
7. **A terminal link failure stops retrying.** `auth_failed` / `terminated` is sticky, so
   the link reconnects at most `MAX_TERMINAL_RETRIES` (3) times and then stops — repeated
   re-auth against the server is itself abusive-looking on a personal line, and it can
   never succeed without an operator fixing the credential. The rail stays down and
   `capability_flags()["available"]` is `false`.

## Tests

`tests/test_jmp_rail.py`, `tests/test_pacing.py`, `tests/test_failover.py`,
`tests/test_jmp_link.py`, `tests/test_jmp_probe.py` — all offline (the XMPP link is a
double; `slixmpp` is never imported). The live paths are covered by
`scripts/jmp_cold_send_probe.py` and `scripts/jmp_rail_smoke.py`, run by hand against
the real account.

The pacing race is pinned by two concurrency tests (two threads at `daily_cap - 1`, and
two threads with the jittered gap elapsed): exactly one send is attempted, and the
loser is refused with a real retry hint rather than fire-and-forget. A third test proves
a refused send refunds its reserved slot.
