# PLAN.md — nosms: permissionless Cashu-paid SMS OTP delivery

**Status:** DRAFT for operator review — 2026-08-21
**Mission:** nomail.name for SMS. A Cloudflare Worker front (Nostr-key auth, Cashu/Lightning postage, quotas, ledger) bridging to a self-hosted Android SMS gateway (burner phone + cash prepaid SIMs). Fully permissionless: no KYC, no account, no identity. Primary integration: auditable-voting's client-only coordinator (browser tab) sending 6-digit OTP codes to ~100 voters (near-term) → 2,000 (ambition).
**Verdict on prior research (2026-08-21):** JMP.chat (automation ban), Telnyx standalone (KYC/10DLC), nadanada.me + sms2nostr (receive-only) all rejected as primary. Android gateway is the only permissionless outbound path.

---

## 1. Gateway strategy

**DECISION: self-hosted Android gateway is the primary and only MVP adapter. Telnyx is a M4+ optional adapter behind the same interface, not MVP.**

- Android gateway (burner phone + cash prepaid SIMs) is the only route with zero identity linkage. SIMs are bought with cash, topped with cash, discarded when burned. Pacing 300–400 msgs/day/SIM (below the researched 400–500 ceiling) with random jitter keeps the human-pattern profile.
- Telnyx fallback: keep as a **planned adapter, not a dependency**. Tradeoff: reliable, unlimited scale, but KYC/10DLC means identity-adjacent — adopting it as primary collapses the permissionless property that justifies the project. If a 2,000-voter election needs burst capacity beyond SIM pacing (2,000 codes ÷ 400/day = 5 days minimum on one SIM), the answer is **more SIMs, not Telnyx** — operator already accepts spreading over days. Implement the adapter interface (see §7) so Telnyx can be added later for compliance-grade jobs if ever wanted.
- **Cashu vs fiat SIM cost:** upstream cost is fiat (prepaid SIM + top-up), revenue is sats. The service is its own FX desk: sats accumulate in the Worker hot wallet; operator converts to cash informally (sell sats, buy SIMs) — no on-chain loop needed at this scale. Price per SMS must cover: (a) amortized SIM+top-up cost, (b) **SIM-burn replacement premium** (a burned SIM is the real unit of loss, not the SMS), (c) abuse margin.
  - Cost basis (India): ₹200–300 pack ≈ 100 SMS/day × 28 days ⇒ ~₹0.10–0.30/SMS ≈ **10–40 sats** at current FX, plus burn premium.
  - **Pricing: 100 sats/SMS domestic (nomail mental-model parity, ~3–5× margin), 500 sats international**, operator-configurable in a pricing table (`/api/pricing` exposes it). Margin ≥3× on the blended cost; revisit after first SIM burn.
  - Election mode: prepaid balance (see §4) so a 100-voter mailout = one payment of ~10k sats, not 100 token exchanges.

## 2. Architecture

```
Browser tab (auditable-voting coordinator, client-only)
  │ 1. POST /api/send  — Authorization: Nostr <NIP-98 event>
  │    body = NIP-44(msg, gateway_pubkey) → Worker NEVER sees OTP plaintext
  │    payment = cashu token OR debit prepaid balance
  ▼
Cloudflare Worker  (nosms — auth, quota, ledger, pricing, queue)
  │ D1: sends, ledger, quotas · KV: replay cache, nonces
  │ holds Cashu proofs in escrow until delivery receipt
  ▲
  │ 3. phone polls: GET /api/gateway/next (bearer gateway-token, long-poll 25s)
  │ 4. phone posts: receipt/heartbeat → POST /api/gateway/receipt|heartbeat
  │
Android phone (home, AC power, behind NAT — outbound only)
  └─ Termux node poller ── localhost ──> SMS gateway app (bogkonstantin OSS)
        │ decrypt NIP-44 body with gateway nsec
        └─ sends SMS via SIM, paces (jittered gaps), rotates SIMs
```

**DECISION: phone-polls-Worker (HTTPS long-poll) is the primary bridge.** NAT-proof (outbound only), zero third-party tunnel dependency, ~30 lines on the phone, trivially debuggable (it's just HTTP). Receipts/heartbeats return over the same easy direction (phone → Worker POST). The phone's only required network privilege is ordinary outbound HTTPS — indistinguishable from any app.

**Bridge options evaluated:**

| Option | Verdict | Tradeoffs |
|---|---|---|
| **Phone long-polls Worker** | ✅ PRIMARY | Simple, robust, NAT-proof. Latency = poll interval (long-poll makes it ~1–2 s). Worker holds a small D1 queue — acceptable. Failure domain: Worker only. |
| Tailscale/Netbird tunnel | ❌ v2 fallback | Works (operator runs Netbird already), but Worker can't join a tailnet — would need a home relay box (DQ05) as an extra hop and second failure domain. Adds battery drain + another moving part on the phone. Keep as documented fallback if CF ever blocks long-polling patterns. |
| Nostr NIP-17/44 control channel | 🟡 SERIOUSLY EVALUATED, tabled for M4 | Elegant: Worker (or even the browser) publishes a gift-wrapped DM to the phone's key; phone subscribes to 2–3 relays, decrypts NIP-44, executes, returns receipt via direct HTTPS POST. Removes the queue and polling entirely; transport is free public relays; metadata-protected by gift wrap. Blockers for MVP: (a) relay latency/availability variance vs a 100-voter election deadline, (b) needs a persistent relay client on the phone (nostr-tools in Termux — doable but a real component), (c) Worker-side relay subscription requires a Durable Object holding outbound WebSockets (more moving parts than a D1 row). Verdict: the **adapter boundary makes this a drop-in swap later** — the phone-side interface (`next()`/`receipt()`) is identical whether fed by poll or DM. Build poll now, swap in nostr channel as M4 experiment. |

Note E2E body encryption (§6) is **orthogonal to transport**: it works identically over poll or nostr DM. The Worker being unable to read OTP bodies does not require the nostr channel.

## 3. API surface

All endpoints JSON. All 4xx/5xx carry **`X-Reason`** (concise cause) + **`X-Hint`** (actionable fix) headers — nomail convention, kept verbatim. `llms.txt` + `llms-full.txt` published from day one (M1), auto-derived from the endpoint table.

**Auth: NIP-98 HTTP Auth (kind 27235) in the Authorization header.** This is the deliberate improvement over nomail's `SameSite=Strict` cookie, which cannot be called cross-origin from a browser tab.

- Header: `Authorization: Nostr <base64(signed event JSON)>`
- Event spec: `kind 27235`, `content ""`, tags `u` (full URL), `method` (HTTP verb), optional `payload` (SHA-256 of body — enforce on `/api/send`). `created_at` freshness window **±120 s** (client clocks skew; NIP-98 norm is 60 s — we widen for phone-less browser tabs with bad clocks, tighten later).
- **Replay protection:** event `id` dedup in KV (TTL 10 min) — a replayed event inside the freshness window is rejected; outside it, freshness kills it. No nonce/challenge round-trip needed (NIP-98 is challenge-free by design).
- CORS: `Access-Control-Allow-Origin: *` on API endpoints (auth is per-request header, not ambient cookie — safe), `Access-Control-Allow-Headers: authorization, content-type`.

**Endpoints:**

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/send` | NIP-98 | Send SMS: `{ to, body_nip44, cashuToken? | debit balance }`. Returns `{ id, status, priceSats }` |
| POST | `/api/send/quote` | NIP-98 | Lightning invoice for top-up `{ amountSats }` → `{ quoteId, invoice }` |
| GET | `/api/send/quote/:id/status` | NIP-98 | Payment state (`PAID`/`ISSUED` — nomail gotcha #4 honored) |
| POST | `/api/topup` | NIP-98 | Add Cashu token to sender's balance |
| GET | `/api/message/:id/status` | NIP-98 | `queued → sent_to_gateway → submitted → delivered | failed`; includes `refundToken` when auto-refunded |
| GET | `/api/balance` | NIP-98 | Held balance + proofs summary for caller's pubkey |
| GET | `/api/pricing` | none | Price table per destination prefix (sats) |
| GET | `/api/health` | none | Gateway heartbeat age, queue depth, per-SIM budgets remaining (no SIM identities) |
| GET | `/api/gateway` | none | Gateway **pubkey** (hex) for NIP-44 body encryption + key fingerprint |
| GET | `/.well-known/nostr.json?name=gateway` | none | NIP-05 mapping for the gateway key (ecosystem discoverability) |
| — | `/llms.txt`, `/llms-full.txt` | none | Machine docs from day one |

Gateway-facing (bearer `gateway-token`, constant-time compared, rotated manually):

| POST | `/api/gateway/next` | long-poll (≤25 s) → next queued job (already-decrypted-by-nothing ciphertext blob + dest) or 204 |
|---|---|---|
| POST | `/api/gateway/receipt` | `{ jobId, status, telcoRef? }` → triggers delivery ledger close / refund |
| POST | `/api/gateway/heartbeat` | every 60 s; `health` goes stale after 5 min |

## 4. Payment

**DECISION: both, with balance as primary for elections.**

- **Per-SMS Cashu token** (nomail parity): token inline in `/api/send`; Worker verifies + escrows proofs, exact change kept in ledger. Good for one-off agents.
- **Prepaid balance** (primary for auditable-voting): `/api/topup` once (e.g., 10k sats), `/api/send` debits per message. A 100-voter mailout = 1 payment, and a mid-mailout failure doesn't strand 99 payments. Whitelisted operator key bypasses payment entirely during election drills (nomail precedent) — keep a whitelist flag, default off.
- Lightning quote as top-up path for users without a Cashu wallet (`/api/send/quote` + status poll; `testnut.cashu.space` auto-pays for dev).
- **Per-destination pricing:** prefix-table in pricing config — India mobile `+91`: 100 sats; intl: 500 sats default; unknown/unsupported: 422 with `X-Hint` listing supported prefixes. No per-number price discrimination beyond prefix class.
- **Refund on delivery failure:** proofs stay escrowed; `status` ≠ `delivered` by **T+15 min** ⇒ auto-refund by re-issuing the escrowed proofs as a fresh Cashu token, returned in `GET /message/:id/status` and held against the payer's pubkey (`/api/balance` shows claimable refunds). No mint round-trip needed — we hold the proofs.
- **Who holds sats:** Worker hot wallet = ecash proofs in D1, AES-GCM-encrypted with a Worker secret. Float cap (default 100k sats): alert + manual sweep to operator self-custody above cap. Mint: operator-chosen mint for prod, testnut for dev. Loss surface = mint failure/rug or Worker secret leak — capped at float, disclosed in llms.txt.

## 5. Storage & privacy

**D1 tables (the only persistent state):**

- `sends(id, payer_pubkey, dest_enc, dest_hash, body_enc, status, price_sats, quote_id?, created_at, delivered_at?, refund_issued?)`
- `ledger(id, pubkey, amount_sats, direction[topup|debit|refund], proof_refs, created_at)` — append-only, auditable.
- `quotas(pubkey, day, count)` — 100 sends/day default (nomail parity), operator key exempt.
- `gateway_state(heartbeat_at, sim_budget_json)` — pacing bookkeeping.
- KV: NIP-98 replay cache (event id → 1, TTL 10 min).

**Retention (the core privacy decision):**

- **OTP body: never stored in plaintext, anywhere.** Client NIP-44-encrypts to the gateway pubkey before POST; Worker stores only the ciphertext blob (`body_enc`); phone decrypts with the gateway nsec. On `delivered`/`failed`, `body_enc` is **null'ed immediately** (column zeroed in same txn as ledger close). TTL sweeper (cron trigger, hourly) nulls anything >24 h regardless.
- **Phone numbers:** AES-GCM encrypted (`dest_enc`, key = Worker secret) for the queue + status window; plus salted SHA-256 (`dest_hash`, pepper) retained for dedupe/cooldowns/abuse analysis. Plaintext numbers exist only in transit and in the phone's RAM. Purge `dest_enc` 24 h after terminal status; hashes retained (they're pseudonymous).
- **Encryption at rest:** AES-256-GCM, NIP-44-v2 key-wrapping of the data key (nomail pattern) so the nsec-holder can rotate; data key in Worker secret store, never in D1.
- **GDDR-ish posture:** minimal PII (number only), documented retention (24 h terminal-state purge), per-pubkey erasure endpoint (`DELETE /api/data` — clears dest_enc/hashes/bodies, keeps financial ledger totals for accounting), no third-party analytics, no IP logging beyond CF defaults. State it plainly in llms-full.txt. Honest limit: Worker operator (us) and Cloudflare see metadata (who sent to which encrypted dest, when); content is blind by design.

## 6. Security threat model

| Adversary | Vector | Mitigation |
|---|---|---|
| Worker operator / Cloudflare (curious) | Reads OTP bodies from D1 or live traffic | **NIP-44 E2E browser→phone from M2** — Worker stores ciphertext only, nulls on delivery. Metadata (sender key, dest hash, time) remains visible — declared, not hidden. |
| Replay attacker | Reuses captured Authorization header | Freshness ±120 s + KV event-id dedupe. Body-binding: `payload` tag (SHA-256 of body) required on `/api/send` — a replayed auth can't carry a different body. |
| Spammer / phishing OTP mill | Uses service to send bulk SMS at sats cost | Cost per send + 100/day quota + per-destination cooldown (1 msg/number/5 min, 3/day) + content policy (OTP-only: reject bodies >160 chars or >1 segment) + **election allowlist mode** (dest set pre-registered, everything else 403) + global kill switch env var. |
| SIM-bank thief | Steals the phone (SIMs = the asset) | Phone at home, SIM PINs set, gateway nsec in Termux (lost = rotate key + republish — not funds-bearing), float cap means nothing worth stealing beyond SIMs; no self-custody keys on the phone. Receipts signed by gateway key so a stolen phone can't forge history for long (key rotation on detected misuse). |
| Carrier / TRAI | Pattern-detects bulk OTP from P2P SIM | Pacing 300–400/day/SIM, 30–120 s jittered gaps, human-hours shaping (avoid 03:00 bursts), multi-carrier SIM spread, SIM warm-up (10/day first week). Accept SIM burn as routine loss — priced in (§1). |
| Mint failure / hot-wallet loss | Proofs worthless or stolen | Float cap 100k sats, sweep at threshold, escrow window ≤15 min keeps at-risk customer sats small, refund path documented. |
| Gateway-token thief | Replays `/api/gateway/*` | Constant-time compare, manual rotation, gateway endpoints emit only to authenticated phone (receipt forgery needs the gateway nsec to sign receipts — bearer alone can't spend anything). |

Explicit non-goal: hiding sender/destination **metadata** from the Worker (that's what the service is — a post office). Goal: **content blindness** + replay-proof + abuse-priced.

## 7. Tech stack

- **Worker: TypeScript.** Decisive: `nostr-tools` (NIP-01/44/98) and `@cashu/cashu-ts` (v4, `wallet.ops` API) are TS-first; nomail codebase patterns are directly reusable; Wrangler DX; this service is IO-bound — Rust (workers-rs) buys nothing but build pain. Vitest + miniflare for tests.
- **Android gateway: reuse bogkonstantin OSS app + Termux node poller.** The app already exposes send/status over HTTP; bind it to **localhost only** (never LAN/WAN) and let the Termux poller talk to it over loopback — zero network exposure of the SMS API. The poller is a ~150-line TS script (nostr-tools for NIP-44 decrypt) under `phone/` in the repo. Fork the app or write a custom Android app **only if** maintenance forces it (M4+) — not now.
- **Phone redundancy:** dual-SIM phone minimum (2 carriers), or 2 single-SIM phones (second = hot spare, polling same Worker, SIM budgets disjoint). Termux: wake-lock, boot autostart (`termux-boot`), watchdog that restarts the poller; heartbeat → `/api/health` staleness is the public degradation signal.
- **Keys:** dedicated `gateway nsec` (identity of the phone, signs receipts, decrypts bodies — published via `/api/gateway` + NIP-05); Worker has no secret key of its own beyond the data-encryption secret (auth is signature **verification** only).
- **Adapter interface** (the Telnyx escape hatch): `sendMessage(to, body) → { telcoRef }` implemented today only by the Termux poller; a future adapter implements the same fn against Telnyx Verify/messaging. One function, zero architectural commitment.

## 8. Milestones (build order = election-first)

| Milestone | Scope | Effort | Exit criterion |
|---|---|---|---|
| **M0 — SPIKE** | Burner phone + SIM live; bogkonstantin app on localhost; curl → SMS received; measure send latency; set SIM PIN; Termux boot autostart | 1–2 d | Manual send to operator's real phone works; pacing notes written |
| **M1 — WORKER MVP** | NIP-98 auth (freshness + dedupe), `/api/send` + queue + `/api/gateway/*`, gateway pubkey endpoint, Cashu token receive/escrow (testnut), plaintext body allowed *temporarily*, `/api/health`, llms.txt skeleton | 3–5 d | curl with signed event + testnut token → SMS on phone, end to end |
| **M2 — ELECTION READY** ⚡ build this first after M1 | NIP-44 body E2E (client helper + poller decrypt), prepaid balance + topup, `/api/message/:id/status`, auto-refund, quotas; **auditable-voting `sms.ts` DeliveryChannel wired** (Authorization-header signing in browser, CSV `phone` column, send-loop with pacing, status display) | 3–4 d | Coordinator tab delivers real OTPs to 10 test numbers, paid, with delivery status + refund path |
| **M3 — HARDENING** | X-Reason/X-Hint everywhere, destination cooldowns, allowlist mode, `payload` tag enforcement, `DELETE /api/data`, llms-full.txt, float cap + sweep alert, heartbeat staleness page | 2–3 d | Dogfood pass: every failure mode returns actionable headers; abuse controls on |
| **M4 — SCALE** | Second SIM/phone + disjoint budgets + rotation; batch scheduler for 2,000 (spread over days, jittered); Telnyx adapter behind flag (only if demanded); nostr NIP-17 control-channel experiment (swap-in per §2) | 3–5 d | Sustained 300/day/SIM for 3 days, no burn; scheduler dry-run of 2,000 |
| **M5 — OPS** | SIM rebuy runbook, key-rotation runbook, monitoring dashboard, public announcement + llms.txt final, pricing review after first burn | ongoing | One unassisted SIM-burn recovery |

**Critical path to 100-voter election: M0 → M1 → M2 ≈ 7–11 days.** Everything in M3+ can trail the election if needed (except X-Reason headers — cheap, do in M1).

## 9. Repo name

**`nosms`** — direct symmetry with `nomail` (no + service); ecosystem-styled like `nomail`, `nodns`, `nak`. Service domain: `nosms.name` (check availability; fallback `nosms.voting`, `sendsats.ms`). Alternatives considered: `satsms` (cute but reads as "sats-ms"), `smsnomail` (clunky), `otp-relay` (generic).

**Public repo, recommended.** The trust proposition is "we can't read your OTPs" — that claim is only credible against auditable source: open Worker + poller, reproducible deploy (`wrangler deploy` from clean checkout), published llms.txt. What stays **private (never committed, gitignored + pre-commit hook)**: SIM purchase/rotation log, phone numbers touched, SIM budget history, gateway token values, the gateway **nsec** (Termux-only). The skill-set's git-secret-detection hooks already cover the nsec/token class.

## 10. Top 5 risks & mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | **SIM burn mid-election** (carrier kills SIM during 100-voter send) | Med | High (mailout stalls) | 2+ SIMs on different carriers from day one; per-SIM budget 300/day; rotation; hot spare SIM pre-activated; scheduler resumes by number on SIM swap; burn priced into sats margin |
| 2 | **Phone dies silently** (Android kills Termux, power cut, reboot) | Med | High (queue stalls, refunds fire) | AC + wake-lock + termux-boot autostart + watchdog; 60 s heartbeat, `/api/health` stale >5 min = visible degraded state; election-day checklist: phone on charger, Termux foreground-locked, operator monitoring health |
| 3 | **Regulatory (TRAI DLT / unregistered A2P)** — volume P2P SMSing is ToS-violating and legally gray in India | Med | Med (service shutdown, not personal) | Keep volumes human-scale, never market as bulk SMS, election-burst framing, Telnyx adapter as compliance escape hatch, accept SIM burn as the designed failure mode rather than seeking scale via gray channels |
| 4 | **Hot-wallet / mint loss** (ecash float wiped) | Low | Med (customer sats + reputation) | Float cap 100k sats, escrow window ≤15 min, sweep at threshold, mint choice disclosed, refund policy in llms.txt; loss bounded by cap |
| 5 | **Abuse relay** (service used for phishing OTPs at scale, becomes known as spam source) | Med | High (blocklisting, SIM burn accelerates) | Sats cost + quotas + per-dest cooldown + OTP-only content policy (1 segment max) + allowlist mode for elections + kill switch + per-prefix pricing; monitor complaint channel |

(Runner-up: NIP-98 clock-skew rejections on voter laptops — mitigated by the ±120 s window and `X-Hint` telling the client to sync; revisit after first real election.)

---

**Operator decisions requested before M1:** (1) repo/domain name sign-off; (2) mint choice for prod float; (3) election-day SIM count (recommend 2 carriers × 1 SIM + 1 spare); (4) whether coordinator key is whitelisted (free sends) or prepaid balance (recommended — exercises the payment path the service exists to prove).
