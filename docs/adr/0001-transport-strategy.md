# ADR-0001 — Transport strategy: international-first, JMP rejected, DIY as fallback

- **Status:** Proposed (awaiting operator ratification)
- **Date:** 2026-09-23
- **Deciders:** Felix (operator), manager session (`cashu.sms.orangesync.tech`)
- **Supersedes:** v1 PLAN §1–2 (2026-08-21), which put an Android burner-SIM gateway + India DLT route on the critical path.

## Context

The service must deliver SMS on behalf of a nostr key, paid in sats, with no account, no KYC, and no API-key onboarding for the sender — `nomail.name` / `cashu.email` semantics applied to SMS. The v1 plan was scoped to an Auroville election OTP mailout, which made `+91` delivery mandatory and therefore required an India-registered entity to complete TRAI DLT registration (5–10 working days, human-gated, external documents). That dependency blocked all progress.

On 2026-09-23 the operator removed the constraint: **a `+91` number is not required.** A lead from a telco contact (SIP trunks are cheap; FreePBX + a $5 VPS; bitcall.io) prompted a re-evaluation, including whether JMP.chat could serve as the backend.

## Decision

1. **International-first.** No `+91` capability is required for v1. India/DLT is parked; the MSG91/Fast2SMS adapters stay in `sms-gateway` but are not exercised.
2. **JMP.chat is rejected as service infrastructure.** Its FAQ forbids the exact use: *"no automations, marketing, or campaigns"* on the P2P routes; numbers are US/Canada only; and SMS/MMS over SIP is unsupported, leaving XMPP as the only programmable surface — the surface the policy forbids automating. JMP is acceptable only as a human's personal number.
3. **Provider strategy:** Telnyx first (public API docs, webhook support, testable immediately); `bitcall.io` as the crypto-payable candidate (BTC/USDT/ETH, self-service A2P SMS + DIDs, no sales call) once an account exists. Provider-specific code lives behind the existing `sms-gateway` adapter interface (`sendMessage(to, body) → {telcoRef}`), so adding or dropping a provider is one file.
4. **Permissionless fallback:** a self-hosted **Asterisk + USB LTE dongle bank** (`chan_quectel`) with cash-bought prepaid SIMs replaces v1's Android-phone + Termux poleper. Same pacing discipline; strictly better uptime and physical security.
5. **Inbound/voice:** a **FreePBX/Asterisk host with rented DIDs** (~$1/mo) is the inbound enabler and the future voice/IVR surface. Inbound delivery to a nostr key uses NIP-17 gift-wrapped DMs.
6. **Runtime:** vps2 behind the existing Caddy, Python/FastAPI, `sms-gateway` as a library. No Cloudflare dependency (no CF credential exists; the v1 Worker design is not required).

## Consequences

**Positive**
- Critical path no longer includes any human-gated regulatory process.
- Existing tested code is reused rather than rewritten; a second provider is additive.
- Crypto-funded providers align the payment rail with the product's permissionless premise.
- DIDs make "receive" economically possible; SIP makes voice/IVR nearly free to add later.

**Negative / accepted risks**
- US A2P requires 10DLC registration; other jurisdictions have their own regimes. DIY infrastructure removes vendor margin, **not** carrier registration. Volume must stay P2P-grade and human-shaped.
- A single VPS hosts the service alongside other workloads; blast radius must be managed with a nightly state dump and a stated loss cap.
- Provider pricing is dynamic and (for bitcall) documented only behind a login ⇒ the adapter cannot be written until an account exists; cost claims must be re-measured, never quoted from memory.
- DLT work is deferred, not cancelled: if a `+91` sender is wanted later, the parked runbook must be resumed.

## Alternatives considered

- **JMP.chat backend** — rejected (policy above).
- **Cloudflare Worker + D1** — deferred: coherent design, but needs a CF credential that does not exist and rewrites working Python adapter code into TypeScript for no functional gain.
- **MSG91/Fast2SMS primary** — demoted with the `+91` constraint.
- **Twilio/Vonage SGX** — same SaaS class as Telnyx with stricter onboarding; revisit only if Telnyx quality disappoints.
