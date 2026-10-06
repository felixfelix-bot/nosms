# ADR-0003 — WhatsApp as a rail: operator override of the ToS prohibition + UI-automation, no delivery receipt

- **Status:** Accepted (operator directive, 2026-10-06)
- **Deciders:** Felix (operator), manager session (`cashu.sms.orangesync.tech`)
- **Supersedes:** nothing. It **knowingly departs from** `docs/adr/0001-transport-strategy.md` §2
  ("JMP.chat is rejected as service infrastructure"), on the precedent of ADR-0002.
- **Related:** `docs/adr/0001-transport-strategy.md`, `docs/adr/0002-jmp-rail-override-and-risk-pricing.md`,
  `app/transports/base.py` (the `Transport` contract), `app/transports/whatsapp.py`.
- **Rail id:** `NOSMS_TRANSPORT=whatsapp`

## Context

The operator asked for the paid nosms service to deliver over WhatsApp, driven from the official
Android client running on a headless emulator, riding the same personal number the JMP rail already
uses. A planning pass (2026-10-06) read WhatsApp's Terms of Service, delivered the finding below, and
recommended against the rail. The operator overruled the finding. This ADR records the collision, the
override, and what the rail is therefore allowed to claim — it does **not** re-litigate the decision.

### The clauses the rail collides with (verbatim)

From `https://www.whatsapp.com/legal/terms-of-service`, § *Acceptable Use Of Our Services*
(fetched live 2026-10-06 via a text-extraction proxy; the site 403s plain `curl`):

> "…you must not use (or assist others to use) our Services … in ways that …
> (e) involve sending illegal or impermissible communications such as
> **bulk messaging, auto-messaging, auto-dialing**, and the like; or
> (f) involve any **non-personal use** of our Services unless otherwise authorized by us."

> "…you must not directly or through automated means: … (e) **create accounts for our
> Services through unauthorized or automated means**; … (g) **sell, resell, rent, or
> charge for our Services or data obtained from us** or our Services in an
> unauthorized manner"

A ContextVM that sends WhatsApp messages on behalf of arbitrary nostr keys, **paid in sats**, trips
(e) auto-messaging, (f) non-personal use, and (g) charging for the service. Registering an account
from an emulator trips the second quotation's (e). Running the official client under `adb` UI
automation changes none of that: it is still a personal WhatsApp account being driven
programmatically for a paid service.

## Decision

The **`whatsapp` rail ships**, as a `Transport` implementation (`app/transports/whatsapp.py`) that
drives the official Android client on the emulator host over `adb`, normalises the UI state into
`NORMALIZED_STATUSES`, and advertises its capability flags honestly:

```
available            = the adb device is configured AND reachable
best_effort          = True    (the default; see "Honesty" below)
delivery_receipts    = False   (permanent — see "Honesty" below)
countries            = ["*"]
```

### The operator override

The ToS finding was delivered to the operator in full. The operator overruled it explicitly and
accepted the ban risk **on his own number**:

> "I realize its not allowed, but lets do it anyway." — operator, on the JMP rail, 2026-10-05
> (quoted in ADR-0002), reaffirmed for this rail on 2026-10-06 when shown the WhatsApp clauses.

This ADR records an **operator-accepted risk**. Agents implement it; they do not re-litigate it. Any
future agent that finds the ToS text and wants to "fix" the rail should read this section first.

### Departure from ADR-0001 §2

ADR-0001 §2 rejected JMP.chat as service infrastructure on JMP's own policy (*"no automations,
marketing, or campaigns"*), leaving JMP *"acceptable only as a human's personal number"*. This rail
departs from that position in two ways at once: it automates against a consumer messenger's ToS, and
it does so **on the same personal line** ADR-0002 already made the v1 rail.

ADR-0002 is the precedent for exactly that shape — an operator override of a consumer-messaging
policy, recorded with named accepted risks and a compensating control set. ADR-0003 reuses that shape
rather than inventing a new one.

## Honesty: what this rail may and may not claim

The codebase treats an unprovable receipt as a bug, so the flags are set by what the rail can
actually demonstrate, not by what would be convenient:

1. **`delivery_receipts = False`, permanently.** The rail can *scrape* a UI state out of the client
   (`uiautomator` dump), and a scraped render is **not a delivery proof**: it is this process's
   reading of pixels on a guest we control, it can be stale, and it cannot be attested to a third
   party. The scraped string is preserved in `SendResult.detail` / `TransportStatus.raw` for humans;
   it never becomes the service's `delivered`.
2. **A scraped "delivered" is normalised to `sent`.** The rail's UI→vocabulary map deliberately
   downgrades every delivered/read signal. That downgrade is a test, not a comment: if someone later
   "improves" the map, the suite fails.
3. **`best_effort = True` by default.** Per the plan's §4.3 default: conservative until E4's harness
   can *demonstrate* something stronger. Nothing in the emulator harness can, so this is expected to
   be permanent rather than a placeholder — and it is the flag a caller reads, so it must not be
   aspirational.
4. **No `receipt` is ever returned** (`receipt=None` on every path), the `email_gateway.py` pattern:
   there is no provider-issued id here, and manufacturing a local one would let the refund sweep
   read "our own token" as a delivery handle.

## Consequences

**Positive**

- One more rail behind the same `Transport` interface, so the price table, escrow ledger and HTTP/CVM
  surfaces are unchanged; the config is the only thing a deployer edits.
- The status vocabulary cannot drift: the rail maps into `NORMALIZED_STATUSES` using the shared
  normaliser plus one documented downgrade.

**Negative / accepted risks (named, not hidden)**

1. **Ban / account termination.** The risk the operator accepted. A detected ban must fail loudly and
   stop — never retry, because a retry loop is what turns a warning into a permanent ban. (The
   fail-loud alerting/kill-switch and the pacing hook land in T4; this rail raises the shared
   `RailUnavailable("terminated", …)` so the caller can branch.)
2. **It rides the operator's personal line.** Account loss is not merely a service outage.
3. **Correlated failure with the JMP rail.** ADR-0002 already made the operator's personal JMP line
   the v1 rail of the paid service. Sending WhatsApp over the *same* number means one ban can take
   out **both** rails at once. This is a stronger risk than the ToS question and only the operator
   can accept it; it is recorded here because it is a property of the deployment, not of the code.
4. **The rail is inherently flaky.** It depends on a 3 GB guest, a KVM host, and a UI tree that
   WhatsApp changes on its own release cadence. No test may depend on a live emulator — that is why
   the driver is injected and every test runs offline against a double.
5. **Volume is the abuse surface.** A burst is what trips WhatsApp's anti-abuse gate. Pacing
   (ADR-0002 compensating control #2) must be wired to this rail before it carries real traffic.
6. **No delivery claims, ever.** Anything downstream that needs delivery must not use this rail.

## Alternatives considered

- **WhatsApp Business Platform (Cloud API / a BSP)** — the sanctioned programmatic surface, and the
  rail a policy-first reading would build. **Rejected for now**: it requires a WhatsApp Business
  profile with support contact info, a Meta business portfolio (or a BSP's own identity-verification
  flow — on the funded Telnyx account all three `/whatsapp/*` endpoints returned `403 10038`,
  a verification-level gate), and **per-recipient opt-in** before any contact. Opt-in cannot be
  assumed for a cold nostr-key-addressed service, so the Cloud API rail is not a drop-in twin of the
  SMS rail. It remains the compliant option if the operator ever wants one; nothing here forecloses
  it, and no business-identity decision was needed for this rail.
- **A third-party unofficial WhatsApp library / reverse-engineered protocol** — rejected: it moves
  the same ToS collision into a library we do not control and adds a second thing that can be banned,
  while the official client on an emulator is at least the real client on the real surface.
- **Do nothing / keep SMS-only** — rejected by the operator (see "The operator override").
- **JMP/Cheogram only** — already the v1 rail (ADR-0002); this ADR is additive, not a replacement.
