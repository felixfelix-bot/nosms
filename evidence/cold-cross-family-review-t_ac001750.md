# Cold cross-family review — nosms PR #4 (kanban sms-gateway:t_ac001750)

reviewer_model: kimi-k3
review_tier: tier/review-kimi
verdict: CHANGES REQUIRED

- target repo: felixfelix-bot/nosms
- target PR: https://github.com/felixfelix-bot/nosms/pull/4
- reviewed head (frozen): fd68ced8091078a44cabb36327866e4b7c4c8c22
- served by: local z.ai proxy (localhost:9099) as `kimi-k3`
- model family: moonshot (cross-family vs the author profile's default `tier/coding-worker`)
- elapsed: 65.1s, usage: {"prompt_tokens": 37473, "completion_tokens": 4917, "total_tokens": 42390, "prompt_tokens_details": {"cached_tokens": 37472}}
- review produced: 2026-10-05T14:20:50Z

The reviewer received the full PR diff plus the verbatim evidence files and the card's
acceptance criteria, and was instructed to look for correctness bugs, unsupported evidence
claims, abuse-surface gaps in the pacing, and acceptance-criteria mismatches.

---

## Findings

### BLOCKER — Pacing daily cap is check-then-act; concurrent callers blow the cap
`JmpCheogramTransport.send` (jmp_cheogram.py, send()) does `self.pacer.check()` → `link.send_message()` → `self.pacer.record_send()`. `Pacer` has no lock and no atomic claim/reserve; `check()` and `record_send()` are separate, and `record_send` only runs *after* the blocking send returns (up to 30 s). Two concurrent callers (uvicorn sync endpoints run in a threadpool) both pass `check()` at `count == daily_cap - 1`, both send, both record → cap exceeded; the jittered min-gap is similarly void (both see `next_allowed_ts` in the past and send back-to-back). This is exactly the reserve-now vs count-after-send race the review brief flags, on the rail where "volume IS the abuse surface." Needs a lock around check+reserve (reserve slot before send, release on failure) or a serialized send path.

### MAJOR — Acceptance criterion 1 evidence is incomplete (honestly, but incomplete)
The card requires a cold send to an **arbitrary** +E164 **plus recipient handset receipt**. Delivered: a cold stanza to the *operator's own handset, which had already messaged the line* (evidence/README.md: "No truly-arbitrary destination"), and **no handset receipt** — only an operator to-do checklist. The honesty about acceptance ≠ delivery is exemplary (the `note` field, README "What is not here"), and the inbound-gate capture is documented. But as written, criterion 1 is not satisfied: the JMP cold-send gate on an unknown peer is unexercised, and handset receipt is unconfirmed. Either the card needs an explicit waiver or the evidence does.

### MAJOR — Wrong exception type caught on send timeout (Python ≤ 3.10)
jmp_link.py `send_message`: `fut.result(timeout=...)` raises `concurrent.futures.TimeoutError`; the code catches builtin `TimeoutError`. These are distinct classes before 3.11 (aliased only in 3.11+). The code uses 3.10-compatible syntax (`str | None`), so a 3.10 runtime is in scope: a send timeout escapes as an unexpected exception, violating the "only raises RailUnavailable" contract that `JmpCheogramTransport.send` and the failover's refund branching rely on — a paid-but-unsent send with no `accepted=False` signal.

### Non-blocking observations
- **Failover degrades on transient blips, not just auth/termination.** `send()` degrades whenever `capabilities.available` is False or the result is `rail_unavailable:*` — including `not_connected`/`connection_lost` during a routine reconnect. The card and docstring frame the degrade path as auth-failure/termination; in practice any reconnect window diverts paid traffic to the email rail (its own abuse surface). Deliberate? Undocumented either way.
- **`_supervise` never stops on terminal failure.** After `auth_failed`, the loop keeps reconnecting with backoff forever — repeated auth attempts against the server, and the link can never recover even with fixed credentials (terminal reason is sticky). Wasteful and potentially abusive-looking.
- **`_degrade` only catches `UnsupportedDestination`.** A `RailUnavailable` (or anything else) from the fallback propagates as a raw exception instead of `accepted=False`; refund then depends on the escrow layer handling exceptions, which is nowhere shown.
- **Corrupt pacing state resets the counter to 0** (`_load` → `_blank`). The comment calls this "safer," but it silently restores a full daily cap after corruption — the unsafe direction for the abuse surface.
- `evidence/cold-send-*.json` field `"to_raw"` contains the *masked* value — mislabeled; masking also differs between README (`+132****8875`) and JSON (`+1******8875`), suggesting hand-editing of "verbatim" artifacts.
- `degraded_count` increments even when the fallback fails; `call_soon_threadsafe` on a closed loop raises `RuntimeError`, not `RailUnavailable`; `_is_us_ca` accepts all NANP +1 (e.g., Caribbean), slightly broader than `countries: [US, CA]`.
- Capability flags as module constants in the rail itself is a fair reading of "served from the rail" (the service layer reads them via `capability_flags()`); no issue there.

The code is unusually honest about acceptance-vs-delivery and the refund matrix is well thought out, but the pacing race is a real abuse-surface hole and the criterion-1 evidence is short of what the card demands.

VERDICT: CHANGES REQUIRED
