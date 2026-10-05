# Cold cross-family review — ROUND 2 — nosms PR #4 (kanban sms-gateway:t_ac001750)

reviewer_model: glm-5.2
review_tier: tier/review-glm
reviewer_family: zhipu
author_model: tier/coding-worker
author_family: tier/coding (deepseek)
cross_family: true
repo: felixfelix-bot/nosms
pr: https://github.com/felixfelix-bot/nosms/pull/4
reviewed_head: 5befe8afe5454b279835362e0beb8d34c3bd1c41
reviewed_diff: git diff master...5befe8afe5454b279835362e0beb8d34c3bd1c41 (app/ + tests/, 20 files, +2266/-10 before this round)
prior_reviewed_head: fd68ced8091078a44cabb36327866e4b7c4c8c22
prior_review: https://github.com/felixfelix-bot/nosms/pull/4#issuecomment-5996387968 (kimi-k3, CHANGES REQUIRED)
prior_findings_tracked_on: t_a4e0829a (fix branch pr/pacing-race-fix, PR #7)
served_by: local z.ai proxy (localhost:9099) — requested glm-5.2, served glm-5.2
produced: 2026-10-05 (two stages: findings, then the model's own verdict on them)
verdict: CHANGES REQUIRED (see the Verdict section: one NEW defect)

Scope of this round: the reviewer received the full `git diff master...5befe8afe5454b279835362e0beb8d34c3bd1c41` for app/ and
tests/, the card's five deliverables, the prior review's eight findings (explicitly scoped out
of this round because they are tracked on t_a4e0829a and being fixed on the unmerged branch
pr/pacing-race-fix / PR #7), and the CI state. It was asked to report NEW defects only and to
state, per known finding, whether it is still present at this head.

Method, stated honestly: the findings text below is the model's stage-1 analysis, and the
verdict line is that SAME model's own decision on its own findings (stage 2). Nothing in this
artifact was written or edited by the worker; the worker only assembled the header and the
verdict line. The model's raw narration ("Let me analyze…") is left in place rather than
trimmed, so the artifact is the model's output as produced.

---

## Stage 1 — findings (verbatim)

Let me analyze this code review carefully.

The PR adds a JMP/Cheogram transport rail with failover to email, pacing, and capability flags. Let me check for new bugs not in the known findings (i)-(viii).

Let me go through the code systematically.

**Known findings check:**

(i) send() check-then-act race: `pacer.check()` -> `link.send_message()` -> `pacer.record_send()` with no lock. Still present in `jmp_cheogram.py` `send()` method.

(ii) TimeoutError vs concurrent.futures.TimeoutError: In `jmp_link.py` `send_message`, `fut.result(timeout=self.send_timeout)` catches `TimeoutError`. In Python 3.11+, `concurrent.futures.TimeoutError` is an alias for `builtins.TimeoutError`, so on 3.11+ this is fine. The known finding says "distinct before 3.11" - and the CI runs on 3.11. So on 3.11+ this is actually fine. Let me check: the code catches `except TimeoutError as exc` - on 3.11+, `concurrent.futures.TimeoutError` IS `TimeoutError` (they were unified). So on the deployed interpreter (3.11+), this is not a bug. Still present as code but not a defect on 3.11+.

(iii) masked value in to_raw field - I don't see any evidence JSON or to_raw field in this diff. Can't verify.

(iv) failover degrades on transient blips: In `failover.py` `send()`, it checks `if self.primary.capabilities.available` then calls primary.send. If `is_rail_down(result)` returns True (which checks for `rail_unavailable:` prefix), it falls through to `_degrade`. The `is_rail_down` returns True for ANY `rail_unavailable:` detail, including transient ones like `not_connected` and `connection_lost`. So yes, a transient `not_connected` would trigger degrade. Still present.

(v) supervise loop reconnects forever after terminal auth_failed: In `_supervise`, the while loop continues `while not self._stopping`. After `_cycle()` catches `failed_auth`, it sets `_terminal_reason = "auth_failed"` and `_set_disconnected("auth_failed")`. But the loop continues with backoff. There's no check for terminal reason to break the loop. Still present.

(vi) `_degrade` does not catch RailUnavailable raised by fallback: In `failover.py` `_degrade`, it catches `UnsupportedDestination` but not `RailUnavailable`. If the email transport raises `RailUnavailable`, it would propagate uncaught. Still present.

(vii) corrupt pacing state resets to full daily cap: In `pacing.py` `_load`, on `(OSError, ValueError)` it returns `self._blank()` which has count=0. Still present.

(viii) `_is_us_ca` accepts every +1 NANP number: `_is_us_ca` checks `number.startswith("+1") and len(number) == 12`. A +1 number with 12 chars means +1 followed by 10 digits. NANP numbers are +1 + 10 digits (area code + 7 digits). But NANP includes US, CA, and also Caribbean (e.g., +1-876 Jamaica, +1-787 Puerto Rico, etc.). So yes, it accepts all NANP, not just US/CA. Still present.

**New findings:**

Let me look for new bugs not in (i)-(viii).

1. In `failover.py` `send()`: When primary is available and `send()` returns a result where `is_rail_down(result)` is True, it falls through to `_degrade`. But note: `JmpCheogramTransport.send()` can raise `RailPaced` and `UnsupportedDestination`. The `RailPaced` exception propagates through `FailoverTransport.send()` without being caught - that's intentional per the docstring. `UnsupportedDestination` also propagates - also intentional. Good.

2. In `failover.py` `_degrade()`: The `merged` dict is computed but then `self.fallback.send(dest, body, **merged)` is called. But wait - `merged = {**self.overrides, **kwargs}`. The comment in the test says "overrides win? no: kw wins" - and indeed kwargs override overrides. But the test `test_fallback_that_cannot_serve_the_destination_is_refundable` passes `carrier=None` as a kwarg, which overrides the `carrier="tmobile"` from overrides. So `carrier=None` is passed to the email transport. Is this a bug? The overrides are meant to provide carrier info, but kwargs from the caller override them. This seems like a design issue - the overrides are supposed to provide fallback-specific info, but caller kwargs can clobber them. Actually, looking more carefully, the `overrides` are set in `__init__` and are meant to be merged into the fallback send. The order `{**self.overrides, **kwargs}` means kwargs win. This might be intentional (caller knows best) or a bug (overrides should win for fallback-specific config). The test comment "overrides win? no: kw wins" suggests this is known. Not clearly a new bug.

3. In `jmp_link.py` `send_message()`: After `fut.result(timeout=self.send_timeout)` times out and raises `TimeoutError`, the future is still pending in the outbox queue. The `_sender` coroutine will eventually pick it up and try to send, calling `fut.set_result(True)` on an already-done future... wait, no. The `TimeoutError` from `fut.result()` doesn't cancel the future. The future is still in the queue. When `_sender` picks it up, it checks `if fut.done(): continue` - but the future is NOT done (timeout on `result()` doesn't complete the future). So `_sender` will process it and call `fut.set_result(True)` on a future whose caller has already moved on. This would raise `InvalidStateError` inside `_sender`, which is caught by the broad `except Exception` and turned into `RailUnavailable("connection_lost")` via `fut.set_exception(...)`. But `fut.set_exception` on an already... wait, the future isn't done yet from the caller's perspective. The caller got a TimeoutError but the future itself is still pending. So `_sender` would call `fut.set_result(True)` which would succeed (the future was never completed). But the caller already got a TimeoutError. This is a race/orphan: the message might actually get sent even though the caller thinks it failed. This is a real issue but it's in the `pragma: no cover` code. Is this a new finding? It's related to (ii) but distinct - it's about the orphaned future, not the exception type. Actually, this is a new correctness issue: a timed-out send can still go out on the wire, and the caller will have raised `RailUnavailable("connection_lost")` which triggers degrade to email. So the recipient could get BOTH a JMP message AND an email. This is a new finding.

Wait, but this is in `pragma: no cover` code that needs a live link. The known finding (ii) is about the exception type mismatch. This orphaned-future issue is different. Let me think about whether this is truly new...

Actually, the `fut.result(timeout=...)` raising TimeoutError does NOT complete or cancel the future. The future remains pending. So when the sender eventually processes it, it will call `fut.set_result(True)` which succeeds (future was pending). The message goes out. But the caller already raised `RailUnavailable`. So the message is sent but the caller thinks it wasn't. This could cause double-sends (JMP + email fallback). This IS a new finding, distinct from (ii).

4. In `jmp_cheogram.py` `send()`: When `link.send_message` raises `RailUnavailable` with a transient reason (e.g., `not_connected`), the rail returns a `SendResult` with `rail_unavailable:not_connected`. The `FailoverTransport.send()` then calls `is_rail_down(result)` which returns True, and degrades to email. But the rail is NOT marked down (only terminal reasons call `mark_down`). So on the next send, `self.primary.capabilities.available` is still True (if the link reconnects), and it tries JMP again. This is finding (iv) - already known.

5. In `failover.py` `capabilities` property: It checks `self.primary.capabilities.available`. If the primary is down, it returns `self.fallback.capabilities`. But the `capability_flags()` method has a separate check: `"serving_rail": (self.primary.name if self.primary.capabilities.available else self.fallback.name)`. These are consistent. But there's a TOCTOU: between checking `available` in `send()` and actually calling `primary.send()`, the state could change. Not a new bug per se, just inherent to the design.

6. In `pacing.py` `_save()`: `os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)`. If `state_path` is in the current directory (no dirname), `os.path.dirname` returns `""`, and `"" or "."` gives `"."`. This is fine. But if the state path has a tilde that was already expanded in `__init__`, this should be fine.

7. In `pacing.py` `snapshot()`: It calls `self._roll_day()` then `self.check()`. But `check()` also calls `self._roll_day()`. Double roll is harmless since the second roll is a no-op. Fine.

8. In `jmp_link.py` `_on_message`: `msg["from"].bare` - if `msg["from"]` is None or doesn't have `.bare`, this could raise. But slixmpp messages should always have a from. Not a real issue.

9. In `jmp_link.py` `from_env`: `cred["password"]` - if the cred file doesn't have a "password" key, this raises `KeyError`. Not caught. This would crash at startup. Is this a new finding? It's a missing error handling for a malformed credential file. But it's a startup/config error, not a runtime correctness bug. Minor.

10. In `failover.py` `_degrade()`: After catching `UnsupportedDestination`, it returns a `SendResult` with `accepted=False`. But it doesn't increment `degraded_count`. That's correct - it wasn't sent. But if the fallback raises some other exception (not `UnsupportedDestination`), like a network error from SMTP, it would propagate uncaught. This is finding (vi) - already known.

11. Looking at `build_transport` in `__init__.py`: For `jmp_cheogram`/`jmp_only`/`jmp`, it imports `SlixmppLink` from `.jmp_link` and creates a link if none provided. But `SlixmppLink.from_env(e)` reads a credential file. If the file doesn't exist, it raises `FileNotFoundError`. This is a startup error, not a runtime bug.

12. In `jmp_cheogram.py`, the `send()` method checks `self._down_reason` AFTER the pacer check. Wait no - it checks `_down_reason` before the pacer check. Let me re-read:

```python
if self._down_reason is not None:
    self.rejected_count += 1
    return SendResult(... rail_unavailable:...)

if self.pacer is not None:
    decision = self.pacer.check()
    if not decision.allowed:
        raise RailPaced(...)
```

So if the rail is down, it returns a `SendResult` (not an exception) with `rail_unavailable:` prefix. The `FailoverTransport.send()` checks `is_rail_down(result)` and degrades. Good.

But wait - in `FailoverTransport.send()`:
```python
if self.primary.capabilities.available:
    result = self.primary.send(dest, body, **kwargs)
    if result.accepted or not is_rail_down(result):
        return result
    # fall through to degrade
return self._degrade(dest, body, **kwargs)
```

If `primary.capabilities.available` is True but the primary returns a `rail_unavailable:` result (e.g., transient `not_connected`), it degrades. This is finding (iv).

If `primary.capabilities.available` is False (rail is down), it skips straight to `_degrade`. Good.

13. New finding: In `failover.py` `send()`, when the primary raises `RailPaced`, this exception propagates through `send()` uncaught. This is intentional. But what about `UnsupportedDestination`? It also propagates uncaught. The test `test_non_us_ca_is_a_request_error_and_is_never_re_routed` confirms this. Good.

14. New finding: In `jmp_cheogram.py`, `send()` calls `self.pacer.record_send()` only after `link.send_message()` succeeds (doesn't raise). But if `send_message` raises `RailUnavailable` with a transient reason, `record_send()` is NOT called. This is correct - we only count accepted sends. The test `test_pacing_counts_only_accepted_sends` confirms this. Good.

15. Let me look at the `FailoverTransport` class - does it implement the `Transport` protocol? The test `test_stack_is_a_transport` checks `isinstance(stack, Transport)`. Let me look at `base.py`... I don't have it in the diff. But the test passes, so `FailoverTransport` must be registered as a `Transport`. Actually, `Transport` is likely a `Protocol` with `runtime_checkable`, and `FailoverTransport` has `send`, `capabilities`, `capability_flags`, and `name`. Let me check if it has all required attributes. I can't see `base.py` but the tests pass.

16. New finding: In `jmp_link.py`, `_set_disconnected` sets `self._connected = False` but does NOT set `self._ready` to unset. So after a disconnect, `self._ready` remains set. In `send_message`, the check is `if not self._ready.is_set() or self._loop is None or self._outbox is None: raise RailUnavailable("not_connected")`. After a transient disconnect, `_ready` is still set, so this check passes. Then `if not self.is_connected(): raise RailUnavailable("not_connected", "link is reconnecting")`. So the second check catches it. This seems fine.

But wait - after a terminal failure, `_terminal_reason` is set, and `send_message` checks `if self._terminal_reason: raise RailUnavailable(self._terminal_reason)` first. Good.

17. New finding: In `jmp_link.py` `_cycle()`, after `x.connect()` (not awaited), it waits for `session_start` with a timeout. If `failed_auth` fires, `started.set_result(False)` and `_terminal_reason = "auth_failed"`. Then `ok = False`, and `_set_disconnected(self._terminal_reason or "not_connected")` is called with `"auth_failed"`. This sets `_terminal_reason = "auth_failed"` (already set) and `_connected = False`. But `x` is still connected (the XMPP stream might still be open). The code doesn't call `x.disconnect()` before returning from `_cycle`. This could leave an XMPP connection open. Then `_supervise` sleeps with backoff and tries `_cycle` again, creating a new client. The old one leaks. This is a resource leak but it's in `pragma: no cover` code. Is this new? It's not in the known findings. But it's in untestable code. I'll note it.

Actually, looking more carefully, after `ok = False`, the code does `self._set_disconnected(...)` and `return`. It never disconnects `x`. But `x.connect()` was called, so there's an XMPP connection. This is a leak. But this is in `pragma: no cover` code. Still, it's a real bug.

18. Let me check the `SendResult` - I don't have `base.py` but from usage I can infer it has: `accepted`, `rail`, `best_effort`, `receipt`, `detail`, and `refundable` (as a property?). In `failover.py`, `SendResult` is constructed with `accepted=False, rail=self.name, best_effort=True, receipt=None, detail=...`. The `refundable` is not set explicitly. Looking at the test: `assert res.refundable is False` for accepted results and `assert res.refundable is True` for failed results. So `refundable` must be a property that returns `not self.accepted`. This is consistent.

Wait, but in `jmp_cheogram.py`, the empty body case returns `SendResult(accepted=False, ...)` and the test checks `assert res.refundable is True`. And for `RailUnavailable` cases, `refundable` is also True. So `refundable = not accepted`. Makes sense.

19. New finding: In `failover.py` `_degrade()`, when the fallback returns a result with `accepted=True`, the code does `self.degraded_count += 1` and returns the result. But the result's `rail` is `"email_gateway"`, not the failover name. This is correct - the serving rail is the email rail. But `capability_flags()` returns `serving_rail` based on `self.primary.capabilities.available`, not based on which rail actually served the last send. If the primary is transiently down (not marked down), `capabilities.available` might still be True, so `capability_flags()` would report `serving_rail = "jmp_cheogram"` even though the last send went to email. This is a minor inconsistency but not a correctness bug.

20. New finding: In `pacing.py`, `_seconds_to_utc_midnight()` returns `max(1.0, 86400.0 - (now % 86400.0))`. If `now` is exactly at midnight (now % 86400 == 0), this returns `max(1.0, 86400.0)` = 86400.0. But at exactly midnight, the day has already rolled, so the cap should have reset. The `check()` method calls `_roll_day()` first, so if it's a new day, the count is 0 and the cap check passes. The `_seconds_to_utc_midnight` is only called when `count >= daily_cap`, which can only happen on the same day. So this edge case is

---

## Verdict — the reviewer's own stage-2 answer (verbatim)

```
## Decision

Finding 3 identifies a **new defect**: when `fut.result(timeout=...)` times out in `jmp_link.py`'s `send_message()`, the future remains pending. The `_sender` coroutine later picks it up, calls `fut.set_result(True)`, and the message goes out on the wire — even though the caller already raised `RailUnavailable("connection_lost")`, triggering email fallback. The recipient receives **both** a JMP message and an email: a double-delivery correctness bug. This is distinct from known finding (ii) (exception type mismatch) — it's about the orphaned future not being cancelled on timeout.

Finding 17 (XMPP connection leak after auth_failed in `_cycle()`) is also new but lower severity and in uncovered code.

The double-delivery issue is a new defect that must be fixed before merge.

VERDICT: CHANGES REQUIRED
```
