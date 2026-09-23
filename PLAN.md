# PLAN — nosms: SMS for nostr keys (the SMS version of cashu.email)

**Status:** v2 DRAFT for operator review — 2026-09-23
**Mission:** `nomail.name` / `cashu.email`, but for SMS. Your nostr key is your identity; sending costs sats; the API is HTTP + Nostr, never SMTP-and-API-keys. Postage economics, not accounts.
**Lineage:** v1 (2026-08-21) was election-first (Auroville OTP mailout) and put an Android burner-SIM gateway on the critical path. v2 re-scopes to the *product* (`cashu.email` parity) and removes the human-gated India route from the critical path.

---

## 1. Parity target — what "SMS version of cashu.email" means

Source of truth: <https://cashu.email/llms.txt> (cached: `~/reports/cashu-sms/llms-full.txt`).

| cashu.email (email) | nosms (SMS) |
|---|---|
| `npub1…@nomail.name` free address | nostr key = sender identity; recipient = phone number |
| Receiving free | inbound: pooled number + routing alias → delivered to the owner's npub |
| Sending 100 sats (Cashu token or LN) | 100 sats/SMS domestic-parity, 500 sats international |
| Custom aliases 1k–1M sats | short/vanity numbers, or `+NNN… @alias` routing handles |
| NIP-01 signed-event auth → `__Host-session` cookie | **NIP-98 (kind 27235) per-request auth** — CORS-safe, so a browser tab can call it (nomail's cookie cannot) |
| `/api/send`, `/api/messages`, `/api/addresses`, `/api/proofs`, `/api/pricing` | same shape: `/api/send`, `/api/message/:id/status`, `/api/balance`, `/api/topup`, `/api/pricing` |
| `X-Reason` + `X-Hint` on every error | kept verbatim |
| `llms.txt` + `llms-full.txt` from day one | kept verbatim |
| 100 sends/day/user | kept, plus per-destination cooldown |
| Encrypted at rest (NIP-44 + AES-GCM) | body NIP-44-encrypted client→gateway; never plaintext at the service |

Deliberate improvement over nomail: **NIP-98 in the `Authorization` header** replaces the SameSite=Strict cookie, which is the reason a browser-only client (e.g. auditable-voting's coordinator tab) can never call nomail cross-origin.

---

## 2. Transport — what we evaluated and what it costs

**DECISION (2026-09-23, operator): a `+91` number is NOT required.** India/DLT leaves the critical path. That deletes the 5–10 working-day human KYC dependency that v1 was blocked on, and opens international A2P + cheap DIDs.

| Route | Verdict | Evidence / reason |
|---|---|---|
| **JMP.chat** | ❌ REJECTED (re-confirmed 2026-09-23) | FAQ verbatim: *"Can I use JMP for Automated or Business Purposes? No, the P2P routes we use to exchange messages with the phone system must be used in the same way as any phone number you get from a regular cell carrier. That means no automations, marketing, or campaigns."* Also US/Canada numbers only, and **SMS/MMS over SIP is not supported** (SIP = voice only), so the only programmable surface is XMPP — the exact thing their policy forbids. Fine as a *personal* human number; unusable as service infra. |
| **bitcall.io** | 🟡 CANDIDATE #1 (crypto-payable) | Bitcall Ltd (London). Self-service, no sales call, accepts BTC / USDT (TRC-20, ERC-20) / ETH / card. Advertises *"a2p sms provider, wholesale sms routes, bulk sms api, sender id registration"*, DIDs in 195+ countries, SIP trunking, white-label reseller (~$120/mo). Its own `llms.txt` warns pricing is dynamic and server-refreshed — **quote from the live API, never from memory**. SMS API docs are behind the panel login ⇒ account required before the adapter can be written. |
| **Telnyx** | 🟡 CANDIDATE #2 (best API docs) | Fully documented messaging API + webhooks; no key on this box yet. US A2P needs 10DLC registration; long-code elsewhere does not. Best first *testable* route once a key exists. |
| **MSG91 / Fast2SMS (+DLT)** | ⬇️ DEMOTED | Only needed for `+91` delivery. No `+91` requirement ⇒ off the critical path. Keep the adapters (`~/repos/sms-gateway`), reactivate only if an Indian number class is requested later. |
| **Asterisk + USB LTE dongle bank** | ✅ PERMISSIONLESS FALLBACK (v2 addition, from the telco lead) | `chan_quectel`/`chan_dongle` + cash-bought prepaid SIMs on a shelf box. Replaces v1's Android-phone + Termux poleper: no phone to steal, better uptime, SIM-per-dongle budgeting. Same pacing discipline (300–400/day/SIM, jittered, human hours). |
| **FreePBX/Asterisk + SIP trunk ($5 VPS)** | ✅ INBOUND + VOICE | Cheap DIDs (~$1/mo) make *inbound* economic; SIP gives voice for free, which unlocks an IVR ("call this number, hear/leave a message for a nostr key") as a later milestone. |

**What DIY does NOT buy:** A2P registration is a carrier/regulatory gate — US 10DLC, India DLT, etc. A $5 VPS + FreePBX removes SaaS margin and the vendor relationship, not the registration. P2P-grade volume and human-shaped pacing are what keep the service in the defensible band. Provider marketing ("No Limits. No Restrictions") is not a compliance position.

---

## 3. Inbound model (the "receive free" half)

SMS numbers cost money, unlike a mail domain, so "free receive" is a cost decision, not a protocol one. Options, ranked:

1. **Pooled DID + routing alias (recommended)** — one number, per-sender reply token: a reply or a new message to the pool carrying the token routes to the owner's npub. Cost = one DID for many users. Delivery = NIP-17 gift-wrapped DM (free, nostr-native) and/or mirrored into the nomail inbox so a user has one place to read everything.
2. **Per-user DID rental** — real dedicated number, ~$1/mo cost basis ⇒ price it as a subscription in sats or a one-time deposit. Closest to true parity, only economic for people who actually want a number.
3. **nomail email bridge** — inbound SMS → email to `npub…@nomail.name`; outbound nomail email → SMS. Reuses cashu.email's free receive + storage entirely; nosms becomes the SMS leg. Best integration story, adds a 100-sat cost per bridged email.
4. **No inbound in v1** — postage-only, like a stamp. Cheapest to ship; replies are impossible.

---

## 4. Runtime

**Recommendation: vps2 (`23.182.128.51`) behind the existing Caddy — Python/FastAPI, reusing `sms-gateway` as the transport library.**
- `*.orangesync.tech` is a wildcard A record to vps2; Caddy is already the TLS front. No new credential needed.
- The tested provider adapters, router, and rate limiter already exist in Python. Rewriting them in TypeScript buys nothing.
- The v1 Cloudflare-Worker shape needs a CF token, and **no Cloudflare credential exists on this box** — that is a hard blocker today.
- Tradeoff, stated honestly: vps2 is a single box, and it already runs mints/relays/tenants. Mitigation: keep state in SQLite/Postgres with a nightly dump, and note that the Worker path stays available later if edge latency or blast-radius isolation becomes the deciding concern.

---

## 5. Reuse map (build on what exists, do not rewrite)

| Piece | Where | State |
|---|---|---|
| Provider adapters (Telnyx, MSG91, Fast2SMS), router, rate limiter, structured logs, tests | `~/repos/sms-gateway` (GitHub `felixfelix-bot/sms-gateway`) | ✅ working, pushed, 6 test files |
| Architecture, threat model, payment/refund model, API surface | this file (v1) | ✅ written |
| Consumer reference | `auditable-voting/web/src/otpDelivery/sms.ts` | ✅ existing |
| Nostr-side patterns (NIP-44 encryption, hex content key, cookie quirks) | `nomail-cashu-email` skill | ✅ documented |
| India DLT/KYC runbook (now off critical path) | `~/handover-sms-gateway-india-operator.md` | ⏸ parked |

`nosms` becomes the nomail-parity service layer (auth, postage, ledger, quotas, llms.txt); `sms-gateway` stays the service-agnostic transport lib. Nothing gets rewritten.

---

## 6. Milestones (revised — India removed from the path)

| # | Scope | Effort | Exit criterion |
|---|---|---|---|
| **M0** | Decisions frozen (below) + provider key acquired + account funded | 0.5 d | A working API key exists and a test SMS lands on a real phone |
| **M1** | `nosms` service: NIP-98 auth (freshness ±120 s + replay cache), `/api/send` with Cashu escrow (testnut), prefix pricing table, `X-Reason`/`X-Hint`, `/api/health`, `llms.txt` | 2–3 d | signed curl + testnut token → real SMS, end to end |
| **M2** | balance/topup, `/api/message/:id/status`, auto-refund on non-delivery (T+15 min), quotas + per-destination cooldown, inbound option (1) implemented | 2 d | OTP mailout of 10 numbers paid from one top-up, with statuses + refund path |
| **M3** | `llms-full.txt`, GGDR-ish posture + `DELETE /api/data`, allowlist mode, float cap + sweep, asterisk/DID pool provisioning | 2–3 d | every failure mode returns actionable headers; abuse controls on |
| **M4** | dongle-bank SIM fallback, second provider behind the adapter, IVR/voice experiment, public announcement | 3–5 d | one unassisted SIM-burn recovery; 300/day sustained |

---

## 7. Open decisions

| # | Decision | Recommendation | Status |
|---|---|---|---|
| 1 | Repo + domain | `nosms`, public, `nosms.orangesync.tech` (this Signal group's `cashu.sms.orangesync.tech` as branded alias) | ⏳ |
| 2 | Scope | public product (cashu.email parity); Auroville election = first customer, not the design driver | ⏳ |
| 3 | v1 direction | outbound postage only; inbound = option (1) in §3 | ⏳ |
| 4 | Route + provider key | Telnyx first (public docs, testable today); bitcall adapter once an account exists | ⏳ |
| 5 | Runtime | vps2 Caddy + Python (§4) | ⏳ |
| 6 | Repo shape | new `nosms` importing `sms-gateway` as a lib | ⏳ |
| 7 | Prod mint for escrow | testnut for dev; pick before first paid traffic (reliability + NUT-17 CRR) | ⏳ |
| 8 | `+91` delivery | NOT required (operator, 2026-09-23) ⇒ DLT parked | ✅ |
| 9 | JMP.chat as backend | REJECTED (automation ban, US/CA-only, no SMS-over-SIP) | ✅ |

---

## 8. Operator action items (only the human can do these)

1. **Fund a provider** — Telnyx (key) or bitcall (crypto top-up, $10–20 → DID + ~10 test SMS to measure real per-SMS cost and DLR behaviour).
2. Settle decisions 1–6 above (a one-word "all recs" is enough to unblock M1).

---

## 9. Sources

- <https://cashu.email/llms.txt>, `/llms-full.txt` — parity target (cached `~/reports/cashu-sms/`)
- <https://jmp.chat/faq> — automation/business prohibition, numbers, SIP limitations (cached)
- <https://bitcall.io/llms.txt>, `/llms-full.txt` — crypto payment, A2P SMS / DID / SIP catalogue (cached)
- `~/repos/sms-gateway`, `~/handover-sms-gateway-india-operator.md`, `auditable-voting/web/src/otpDelivery/`
