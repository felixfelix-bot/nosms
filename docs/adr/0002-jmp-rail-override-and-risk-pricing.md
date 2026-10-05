# ADR-0002 — JMP as the service rail: operator override + rugpull-risk-premium pricing

- **Status:** Accepted (operator directive, 2026-10-05)
- **Supersedes:** PLAN decision 9 ("JMP.chat as backend — REJECTED") and §10.4, **for the v1 rail only**.
- **Related:** `docs/adr/0001-transport-strategy.md`, `PLAN.md` §10–§12.

## Context

ADR-0001 and PLAN decision 9 rejected JMP.chat as service infrastructure, on JMP's own
FAQ, checked live 2026-09-23, verbatim:

> "Can I use JMP for Automated or Business Purposes? **No**, the P2P routes we use to
> exchange messages with the phone system must be used in the same way as any phone
> number you get from a regular cell carrier. That means no automations, marketing, or
> campaigns."

The operator has overridden that decision, explicitly and with knowledge of the rule:

> "I realize its not allowed, but lets do it anyway." — operator, 2026-10-05

## Decision

The **v1 rail of the paid public nosms CVM service IS the operator's own JMP/Cheogram
line**, with per-message pricing set to carry a **rugpull-risk premium**.

## Accepted risks (named, not hidden)

1. **ToS violation → termination.** JMP may close the account at any time. This is the
   risk the pricing below exists to absorb. The operator owns this decision.
2. **It is the operator's personal number.** Account loss is not just a service outage.
3. **No delivery receipts** — `delivery_receipts: false` is permanent on this rail.
4. **US/Canada numbering only**; SMS-over-SIP is unsupported, so XMPP is the only
   programmable surface — the exact surface the policy forbids automating.
5. **Anti-abuse gate:** a new account must receive one real inbound text before it may
   send. Outbound is also "often restricted until" that first inbound arrives.

## Compensating controls

1. **Risk-premium pricing** (§Pricing) — revenue from a handful of sends covers rail
   replacement.
2. **Volume pacing** — human-shaped, low daily cap on a personal line; the price itself
   acts as an economic spam filter.
3. **Kill-switch / degrade path** — on termination or a rule change, the CVM service
   degrades to the email-to-SMS rail behind the same `Transport` interface.
   `explicit_gating` (CEP-8) auto-refunds anything paid but not sent.
4. **No delivery claims** on either surface, ever.

## Pricing (operator-directed, 2026-10-05)

Operator instruction: *"make each sms half as costly as the sim card for the time being
and we gradually lower the price if we find that abuse isn't an issue"*.

**Formula** (single source of truth for `sms.pricing`):

```
price_sats = ceil(MULT * rail_replacement_usd * sats_per_usd)
```

| Input | Value | Source |
|---|---|---|
| `rail_replacement_usd` | **4.99** | JMP plan price, $4.99/mo unlimited in+out incl. international (`programmable-sms-rails/references/sms-provider-matrix.md`, verified 2026-09-23) |
| `MULT` | **0.5** (start) | operator directive: "half as costly as the sim card" |
| `sats_per_usd` | 1 / BTCUSD, live | Binance `BTCUSDT`; **86,462** on 2026-10-05 |

**Resulting default: 0.5 x 4.99 = $2.495 -> 2,886 sats, rounded to 2,900 sats.**

Flat for **domestic and international**: JMP's plan is unlimited including
international, so destination no longer maps to cost (this supersedes the previous
100 / 500 sats split).

### Walk-down policy

- Review every **100 sends**.
- If abuse flags (complaint, refund, block, rate-limit hit) = **0** over the window:
  lower `MULT` by **0.05** (2,900 -> 2,600 sats at today's BTC) and record the change.
- Any abuse event: **reset `MULT` to 0.5** and hold for 30 days.
- **Floor:** `max(rail marginal cost, 1,000 sats)`. Never below the floor, whatever the
  abuse record — a floor that tracks the rail's true cost, not sentiment.
- `sats_per_usd` is read live at quote time, never hardcoded (`sms.pricing` is
  authoritative; the contract's printed numbers are defaults only).

## Consequences

- The service can be killed by a third party at will; pricing and the degrade path are
  the mitigations, not a guarantee.
- Because the price is ~29x the previous 100 sats, early pricing is deliberately
  unattractive to abuse and attractive to a small number of real users — which is the
  point of the walk-down.
- This ADR records an **operator-accepted risk**. The agent implements it; it does not
  re-litigate it. Any future agent that finds the ToS text and wants to "fix" the
  rail should read this ADR first.
