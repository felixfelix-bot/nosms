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
| 1 | Repo + domain | `nosms`, public, `nosms.orangesync.tech` (this Signal group's `cashu.sms.orangesync.tech` as branded alias) | ✅ |
| 2 | Scope | public product (cashu.email parity); Auroville election = first customer, not the design driver | ✅ |
| 3 | v1 direction | outbound postage only; inbound = option (1) in §3 | ✅ |
| 4 | Route + provider key | Telnyx first (public docs, testable today); bitcall adapter once an account exists | ✅ (route blocked at the account level — see §8a) |
| 5 | Runtime | vps2 Caddy + Python (§4) | ✅ |
| 6 | Repo shape | new `nosms` importing `sms-gateway` as a lib | ✅ |
| 7 | Prod mint for escrow | testnut for dev; pick before first paid traffic (reliability + NUT-17 CRR) | ✅ |
| 8 | `+91` delivery | NOT required (operator, 2026-09-23) ⇒ DLT parked | ✅ |
| 9 | JMP.chat as backend | REJECTED (automation ban, US/CA-only, no SMS-over-SIP) | ✅ |

**Ratification note (2026-09-26).** 1–7 were accepted as recommended on the operator's
"telnyx is now funded. please continue." — i.e. the recommendation set, not a per-item debate.
Any item can still be reversed by the operator; none of them is load-bearing for M1a.

### 8a. Telnyx is funded but cannot sell us a sending number (live finding, 2026-09-26)

Balance is now **15.41 USD** (funding: done). The numbers API still refuses every order:

- US local **and** US toll-free orders both return 422 `10027` *"We don't recognize the number(s)
  […]. Did you first search for the number(s)?"* — because inventory is served **redacted**
  (`+16183xxxxxx`). A redacted number can never be ordered.
- A 63-country scan: every country's numbers come back redacted **except Portugal**, whose local
  numbers are shown in full and have **no SMS feature** (`voice`, `fax`, `emergency`,
  `local_calling` only).
- `/v2/number_reservations` → 403 `10038` "not permitted at this account level"; the targeted
  `filter[phone_number][starts_with]` search → 403; `/v2/porting_orders` → 403.
- Telnyx documents exactly this: *"Full number display limited to local numbers of the account's
  country of origin"* and *"Number ordering limited to local numbers whose country code matches the
  account's country of origin"*
  (<https://developers.telnyx.com/docs/account-setup/levels-and-capabilities/trial>, `/paid`,
  `/verified`). At this tier 10DLC and Toll-Free verification are also *no access*.

⇒ The account's origin country is **PT** and it sits at the **Trial** tier: the only orderable
inventory is voice-only. This is a portal/KYC task, not an API task — tracked as the operator card
on board `sms-gateway`. M1a/M1b proceed behind `FakeTransport` in the meantime; the live end-to-end
send is gated on this card.

---

## 8. Operator action items (only the human can do these)

1. **Fund a provider** — ✅ done 2026-09-26 (Telnyx, balance 15.41 USD). The route is still blocked at
   the **account level** (§8a): origin country PT + Trial tier ⇒ no SMS-capable number can be ordered.
   Portal work only: check/change the account country, or raise the tier to Paid/Verified.
2. Settle decisions 1–6 above — ✅ done 2026-09-26 (accepted as recommended, see the ratification note).
3. **Provide one destination number** for the first live test SMS and verify it in the portal
   (`GET /v2/verified_numbers` is empty; the trial tier only sends to a verified destination).

---

## 9. Sources

- <https://cashu.email/llms.txt>, `/llms-full.txt` — parity target (cached `~/reports/cashu-sms/`)
- <https://jmp.chat/faq> — automation/business prohibition, numbers, SIP limitations (cached)
- <https://bitcall.io/llms.txt>, `/llms-full.txt` — crypto payment, A2P SMS / DID / SIP catalogue (cached)
- `~/repos/sms-gateway`, `~/handover-sms-gateway-india-operator.md`, `auditable-voting/web/src/otpDelivery/`

---

## 10. Machine-facing surfaces — paid CVM server + napplet (operator-ordered 2026-10-02)

**Status:** queued. **Urgency: SOON** (operator, 2026-10-02: *"go, urgency: soon"*).
Quota basis at classification: `zai-quota-gate.sh` exit 0, but the flat router was
returning *all providers exhausted* for every model, so the work is release-gated on
a **live completion probe**, not on the gate's verdict alone.

Board `sms-gateway`: `t_119a4ab8` (CVM server), `t_4fa1415a` (napplet),
`t_3ccdef32` (M1a, surface the HTTP API). All three release together via
`nosms-m1a-release-watch.sh` when the router can actually serve.

### 10.1 Why two surfaces, not one

`cashu.email` is documented for machines (`llms.txt`) and driven by humans through a
web page. nosms needs the *same* pair, but the two surfaces cannot share a client:
an napplet runs in a sandbox with **no `fetch`, no WebSocket, no HTTP verb at all** —
`NAP-RESOURCE` is `bytes`-in/`bytes`-out, so a `POST /api/send` with a JSON body is
**not expressible** from a napplet (verified against the SDK types). The machine
surface therefore has to be the Nostr transport, not the HTTP one.

```
napplet (UI only: no keys, no tokens, no network)
  └─ shell NAP-CVM (`cvm` domain)   ← the shell signs, encrypts, pays
        └─ paid CVM server (npub-addressed; kind 25910 JSON-RPC)
              └─ Transport interface → email-to-SMS rail (best-effort) | Telnyx (post-KYC)
```

The HTTP API (§4) stays the surface for scripts, `curl` and LLMs that *do* have network
access; the CVM surface is for in-shell clients. Both sit on the same `Transport`.

### 10.2 CVM server

- **Transport:** kind `25910` (ephemeral) for traffic; kinds `11316`–`11320` for the
  CEP-6 service catalog so `cvmi discover` finds it. Discovery does **not** rely on the
  ephemeral kind.
- **Tools:** `sms.send` (paid), `sms.status` (free), `sms.pricing` + `sms.capabilities`
  (free). `sms.capabilities` reports `best_effort` and `delivery_receipts` **from the
  rail itself**, never hardcoded.
- **Payment (CEP-8):** `cap:tool:sms.send:<sats>:sats` — 100 domestic / 500 international,
  nosms parity. `pmi: bitcoin-cashu` first (Cashu-native, refund is a token, no LN node
  needed); `bitcoin-lightning-bolt11` + NWC as a second PMI later. Lifecycle
  `explicit_gating`: **nothing is sent before payment lands**, and a hard, detectable
  failure triggers an automatic refund.
- **Implementation:** Python + `nostr_sdk` bindings — verified 0.58 s gift-wrap round
  trip; the TypeScript `NostrServerTransport` silently hangs (skill-documented).
- **Constraints that bite:** client and server MUST use different keys (a shared key
  echoes its own requests); `#p` filtering on kind 1059 is unreliable on some relays, so
  subscribe broadly and filter client-side; working relays are `nostr.mom`,
  `relay.primal.net`, `nos.lol`, `relay2.contextvm.org`, `relay.nostr.band` —
  `relay.contextvm.org` is unreachable.

### 10.3 Napplet

- **Hard requirement:** `cvm` only. Every other domain optional, each shell call wrapped
  in `try/catch` with an explicit fallback (a partial shell otherwise fails conformance).
- **llms.txt parity:** `llms.txt` + `llms-full.txt` are bundled at build time (`?raw`)
  **and** the live price + capability flags are read from the server, so the displayed
  contract cannot drift from the server's actual behaviour. The "best effort, no
  delivery receipt" warning is rendered from the server's own flags.
- **One job:** destination, body, price shown *before* send, `sms.status` after.
- **The napplet never holds a token or a key.** Payment runs through the shell's CEP-8
  flow; any design that puts an ecash token in napplet `storage` is wrong by construction.
- **Boundary hygiene:** no `nsec`, no `localStorage`, no direct `fetch`, no `window.nostr`.

### 10.4 Exclusions and honesty constraints

- **JMP/Cheogram is NOT service infrastructure.** Its FAQ prohibits automated or business
  use. It stays the operator's personal number and the inbound-delivery QA rail. Shipping
  a paid public service on it would break both the product and the ToS.
- **The v1 rail has no delivery receipts.** Price and copy must say so on both surfaces;
  a "delivered" claim we cannot observe is the one thing this service must never emit.
- **No simulated data.** Capability flags, prices and statuses come from the live rail or
  they are absent (standing operator rule).

### 10.5 Milestones added

| # | Scope | Exit criterion |
|---|---|---|
| **M5** | Paid CVM server: catalog announcement, `sms.pricing`/`sms.capabilities`/`sms.status`, then `sms.send` behind CEP-8 Cashu gating + refund on detectable failure | a second key pays a testnut token via `cvmi call` and a real SMS lands on a handset through the email rail, result honestly labelled best-effort |
| **M6** | Napplet wrapping the CVM: one-screen sender + bundled `llms.txt`/`llms-full.txt` + live capability flags | `pnpm verify` + `pnpm test:conformance` green in a real sandbox (skips reported as skips), deployed to Blossom + relays, live call from inside Paja returns the server's real pricing |
