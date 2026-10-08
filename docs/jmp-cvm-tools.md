# JMP tools on the CVM: `jmp.status`, `jmp.funding`, `jmp.credentials`

The machine surface for the operator's JMP/Cheogram account. Three tools, one
rule each: **status never lies about freshness, funding never pays, credentials
never travel in the clear.**

- Served by `app/jmp_tools.py` (`JmpService`), dispatched by
  `app/cvm_tools.py`, driven by `app/jmp_flow.py`, encrypted with
  `app/nip44.py`, read from `app/jmp_secrets.py`.
- Contract text: `docs/cvm/llms.txt` (short) and `docs/cvm/llms-full.txt`
  (manual). Both are served verbatim by the `docs` / `llms` tool.
- Evidence of a real run of the served path: `evidence/jmp-cvm-probe.json`
  (produced by `scripts/jmp_cvm_probe.py`).

## 1. What each tool does

- **`jmp.status`** — free, read-only (IQ `get` only), cannot spend. Answers from
  the committed capture and labels itself `cached`.
- **`jmp.funding`** — free, drives the ad-hoc flow, **cannot spend** (see §3).
  Answers from the bot's own words and labels itself `live-flow`.
- **`jmp.credentials`** — paid, tier financial, reads the vault. Answers from
  OpenBao (through `fleet_secret.py`) and returns ciphertext only.

`source` is not decoration: `cached` carries the capture timestamp in `as_of`,
`live-flow` means *this run* drove the flow. `jmp.funding` refuses with
`rail_unavailable` rather than answering from cache, because a caller who asked
for a live reading did not ask for a cached one.

## 2. The port, and how it is proved

The driver is **ported, not rewritten**: `app/jmp_flow.py` comes from
`cashu-sms/tools/xmpp_provision.py` (felixfelix-bot/soveng-archive, branch
`worker-base/t_3910f6fc`, commit `3a179dc`), where a live run drove the JMP
registration to the funding step.

The proof is mechanical: the first nine classes of `tests/test_jmp_flow.py` are
that file's **37 tests**, ported with only the import path changed (same class
names, same test names, same assertions):

```console
$ python -m pytest tests/test_jmp_flow.py --collect-only -q \
    -k "not TestJmpFlowDefinition and not TestNoPaymentGuarantee \
        and not TestSelectedNumberAndFacts and not TestLiveEntryPoints"
tests/test_jmp_flow.py: 37
```

What changed in the port: the CLI is gone (it is a library now), the JMP flow is
`JMP_FUNDING_STEPS` in code instead of `flows/jmp-register-phase-a.json`, and the
"stop before payment" note became an executable check
(`assert_no_payment_emitted`).

## 3. Incapable of spending — and proved

The flow selects the **bitcoin** activation method and stops. That is not a
payment: selecting bitcoin is what makes the bot print the deposit address the
operator asked for; the money moves only when a human sends BTC to that address.
Card selection and card submission are absent from the flow by design.

Two independent checks run on every drive (`assert_no_payment_emitted`):

1. **Static** — no step in `JMP_FUNDING_STEPS` is payment-shaped
   (`credit_card`, `card-number`, `cvv`, `braintree`, `auto_top_up`, …).
2. **Dynamic** — no outgoing stanza in the run's transcript is payment-shaped and
   no out-of-band payment URL (`pay.jmp.chat/.../credit_cards`) was emitted.

Both checks are themselves tested by feeding them poisoned inputs
(`tests/test_jmp_flow.py::TestNoPaymentGuarantee`): a guarantee test that cannot
fail proves nothing.

## 4. The number: the card's cross-check, corrected from the evidence

The card asked which number is real — `(810) 258-3233` (the report) or
`(810) 258-3253` (the blocked-reason comment) — and concluded 3233, adding that
3253 "was menu OPTION #2 and was never chosen" and "must not appear anywhere in
this tool's output".

Reading the three verbatim transcripts instead of the summaries gives a different
answer, and it is the answer the tool serves:

- **run 1** (`jmp-register-transcript-2026-09-26.txt:22,38`): option 1 was
  (810) 258-3233, and the bot answered "You've selected (810) 258-3233".
- **run 2, session A** (`jmp-register-payment-2026-09-26.txt:18,34`): option 1 was
  (810) 202-2913, and the bot answered "You've selected (810) 202-2913".
- **run 2, session B** (`jmp-register-payment-2026-09-26.txt:85,101,141`):
  option 1 was (810) 258-3253, and the bot answered "You've selected
  (810) 258-3253" — this is the session that also printed the deposit address.

So `258-3253` **was** option #1 and **was** selected — in the very session that
printed the deposit address this record serves (`0.000244` →
`bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk`, the same file, lines 122–124). The
report is internally inconsistent too: its §9 table (line 346) carries 3253 while
its §1 carries 3233.

Consequences, all of which are encoded in the payload:

- `number` is the number of the session that produced the address/amount — the
  values travel as one observation, never spliced from two.
- `number_state: reserved` and `number_note` name it as a **session-scoped
  reservation, not ownership**: three sessions offered three different option-1
  numbers, so the number can and does change.
- `number_history` carries every observed selection with its evidence line, so
  nothing is hidden from a caller diffing the payload.

## 5. `jmp.credentials`: the security design (operator-approved)

- **Allow-list first, payment second.** `CvmTools.jmp_credentials` calls
  `authorize_credentials(caller)` *before* it enters the payment path, so an
  unauthorised caller cannot pay their way in; the refusal (`owner_only`) names no
  field of the credential. Tested by asserting the mint is never touched.
- **NIP-44 v2 to the caller's npub.** The response carries
  `{enc, to, ciphertext, issued_at, rotate_by, revoke_hint, preconditions}` and
  nothing else. `app/nip44.py` is a from-spec implementation proved against the
  official vectors (35 conversation keys, 32 message-key sets, 24 padding cases,
  10 exact payloads, 12 invalid payloads refused, 8 invalid keys refused) —
  `tests/fixtures/nip44.vectors.json`, vendored from github.com/paulmillr/nip44.
  Then it is proved on the *service* path: the test decrypts the served ciphertext
  with the operator's key and asserts the JID and password come out, and that a
  different key cannot open it.
- **Never a plaintext file.** The credential is read through
  `fleet_secret.py get <name>` (OpenBao, ADR-014/ADR-020). A missing credential is
  `credential_unavailable`, naming the store and the secret, with a `preconditions`
  report — never a fallback to KeePass (retired) or a local `.env`.
- **Redaction.** `JmpService.redaction_values()` feeds the driver's transcript
  redactor, so a password that ends up in a logged stanza is replaced by
  `<redacted:N chars>` before it is written. Tested, including "the response body
  contains no plaintext" and "the log-facing precondition report contains no
  values".
- **Rotation and revocation.** Every release carries `rotate_by` (issue + 90 days)
  and `revoke_hint`, which documents both halves of the revoke path: change the
  XMPP password on the account's server and update OpenBao; and remove the npub
  from `NOSMS_JMP_ALLOWLIST`, which stops further releases immediately.

## 6. Wiring it on the server

```console
# where the credential lives (read-only on this node: see §7)
secret/jmp/account      -> {"jid": "...", "secret": "..."}
secret/jmp/number       -> {"number": "...", "number_state": "RESERVED", "reserved_at": "..."}
secret/jmp/funding      -> {"btc_address": "...", "amount_btc": "...", "amount_usd_min": "...", "captured_at": "..."}

# server environment
CVM_OWNER_PUBKEYS       comma-separated hex pubkeys that send free (unchanged meaning)
NOSMS_JMP_ALLOWLIST     comma-separated hex pubkeys allowed to call jmp.credentials
NOSMS_JMP_LIVE=1         wire the live XMPP driver (default: cached only)
NOSMS_JMP_TRANSCRIPT    optional path for the redacted drive transcript
NOSMS_FLEET_SECRET      optional override for the fleet_secret.py path
```

The server's own key (`CVM_NSEC`) is what NIP-44-encrypts a release. Without it
the tool refuses (`credentials_encryption_unconfigured`) rather than emitting
anything readable.

## 7. Blockers, honestly (as of this writing)

1. **OpenBao is not serving reads from this node right now.**
   `scripts/jmp_cvm_probe.py` reports `store=openbao, present=false, detail:
   "fleet_secret: no OpenBao token (set OPENBAO_TOKEN / OPENBAO_TOKEN_FILE)"` for
   all three secrets, and `fleet_secret.py health` returns `Connection refused`.
   The card's §4 instructs exactly this behaviour: fail closed, report which store
   was asked and whether the credential was present, and **do not** attempt a
   write or a fallback. The tool is correct; the vault is the missing piece.
2. **The JMP secrets are not in OpenBao yet, and this node cannot write them.**
   Its role has `read` on `secret/data/fleet/*` and no `create`/`update` on any
   path, and `fleet_secret.py` has no write subcommand by design (ADR-020). Writing
   `jmp/account`, `jmp/number` and `jmp/funding` is an **operator/admin action**
   (admin token or a deliberate scoped `create`/`update` grant). Until then
   `jmp.credentials` and `jmp.funding` refuse with a visible reason — which is the
   honest state, not a defect.
3. **No live drive was performed for this card.** A live drive needs the account
   credential (blocker 2) and would exercise the operator's personal line; the
   flow's live path is implemented and covered offline end-to-end against a fake
   bot, and `evidence/jmp-cvm-probe.json` records what the served tools answer
   today. When blocker 2 is cleared, `NOSMS_JMP_LIVE=1` turns on both live paths
   without a code change.

## 8. ToS / risk (operator-accepted, recorded here and in the tool descriptions)

JMP's terms restrict automated use of its personal rail. Verbatim from the card
thread, 2026-09-26: **"lets take the risk."** That acceptance covers the
operator's own single line. These tools are not a licence for bulk or resale use,
and they do not widen themselves; the acceptance is stated in every `jmp.*` tool
description so a caller (or a reviewer) sees it before using them.
