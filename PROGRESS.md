# PROGRESS — SMS CVM end-to-end + public mint (2026-10-12)

## 2026-10-13 — task t_fde3f7cf: mint fee priced per keyset, not proofs[0] (F3, PR #14)

- F3 (spurious 502 on a mixed-keyset token): the escrow path priced the mint's
  NUT-02 input fee from `proofs[0]["id"]` only and applied that one keyset's
  `input_fee_ppk` to every proof. `input_fee_ppk` is per KEYSET, and a token
  can span two keysets with different rates (v3/v4 mix, or assembled across a
  rotation) → swap mis-sized → the mint's balance check rejects it as an
  opaque `502 mint_error`. No funds lost, but a legitimate payer was refused.
- `input_fee_for(ppks, proofs)` in app/cashu.py prices each keyset's proofs
  with its own rate, then ONE ceil over the summed raw fees — exactly what
  cdk-mintd `calculate_fee` and nutshell `get_fees_for_proofs` charge
  (`(sum(ppk_i*n_i) + 999) // 1000`). Summing per-keyset CEILS (the card's
  sketch) would overprice the fractional case (2+3 proofs across two 100-ppk
  keysets = 1 sat at the mint, not 2) — same 502, other direction.
- Fixed at all three `proofs[0]` call sites: HTTP send (app/main.py), CVM
  sms.send (app/cvm_tools.py), fixture burn helper (scripts/make_v4_fixture.py).
- StubMint models per-keyset fees; 3 RED-on-master tests now GREEN (2 in
  test_send.py, 1 in test_cvm_tools.py) + formula pins in test_cashu.py.
  Suite 825 passed, coverage 82.64% (gate ≥80%), gitleaks clean.

## DONE and PUSHED

### nosms (felixfelix-bot/nosms, master)
- 169a935  settlement_receipt bypass CLOSED (SettlementStore, live-refused)
- a4b690f  micronuts IaC bring-up (local)
- 00786f5  mint-aware split_amounts (micronuts keys only go 1..128)
- efdcf4c  live bypass regression probe + this progress map
- 434a396  mint decision record (docs/CVM-MINT-DECISION.md)
- 2a48109  PUBLIC mint on vps2 + CVM repointed to it

### cashu.me PWA (felixfelix-bot/cashu.me, branch wt/cashu-services-ui)
- f624372  pay step wired for real (src/js/cvmPayment.ts + mintPaymentToken)
- 3c0c6f9  vitest hookTimeout fix (337/337 green)
- earlier: 5d53015 3bbf603 808549e fd1f1a1 932d236 72ac566

## PROVEN ON THE WIRE
- bypass: forged settlement_receipt -> bad_settlement (twice, incl. after repoint)
- paid e2e over PUBLIC mint https://mint-test.orangesync.tech:
  snd_15e3366f58ce0214 accepted=true delivery_confirmed=false, escrow 2900 sats
- escrow ledger ALSO shows an earlier paced send as status=failed with
  refunded_at set -> escrow refunding a real failure, unprompted
- browser CORS preflight from the PWA origin: HTTP/2 204 allow-origin *

## PACER — NOT A BUG (was blocking my test, may confuse the next worker)
app/transports/pacing.py draws the next allowed send from the JITTERED window
[NOSMS_JMP_MIN_GAP_SECONDS=60, NOSMS_JMP_MAX_GAP_SECONDS=300]. So "wait 60s" is
wrong — it can be up to 5 minutes. State: ~/.hermes/profiles/manager/state/
jmp_pacing.json (count/20 per day). ADR-0002 personal-line protection.

## MINT FACTS
- mint.orangesync.tech is NOT usable: its log says "Mint info already set, not
  using config file settings" + "melts disabled: private mint has no Lightning
  backend". It ignores its own fakewallet config. Separate card needed.
- mint-test.orangesync.tech = micronuts (rust), keyset 00254ad69403a823,
  keys 1..128 only, input_fee_ppk 0, FakeWallet auto-settle.
- *.orangesync.tech is a DNS wildcard.
- The LIVE Caddy is the HOST systemd unit, /etc/caddy/Caddyfile (pid 780).
  The tollgate-caddy container is stale and NOT running.

## REMAINING
1. build:pwa -> deploy nsite -> restart nsyte-gateway -> operator phone test
2. open the PR for wt/cashu-services-ui
3. optional card: fix cdk-mint env/config gap
