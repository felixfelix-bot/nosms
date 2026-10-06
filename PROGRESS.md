# t_b60aa373 — nosms WhatsApp T4

Repo: felixfelix-bot/nosms @ /home/c03rad0r/worktrees/t_b60aa373  branch pr/whatsapp-t4
base: eab350b (merged WhatsApp rail, ADR-0003)

## status log
- recon done: parity tests currently GREEN (18 passed). llms.txt is RENDERED from app/llms.py
  via scripts/render_llms.py (byte-parity test) -> edit app/llms.py, then render.
- local .venv -> symlink to ~/worktrees/t_35fd8871/.venv (gitignored); added .venv to
  $GIT_COMMON_DIR/info/exclude (symlink slips past `.venv/` ignore pattern).
- T4 scope decided: own pacing knobs for the WhatsApp rail (NOSMS_WHATSAPP_*), fail-loud
  ban/rate-limit with latch+alert, FailoverTransport must survive a primary that RAISES,
  HTTP 429 rail_paced + Retry-After / 503 rail_unavailable, docs honesty.
- cluster 2 DONE: pacing loader + WhatsApp rail pacer injection (claim/release, RailPaced),
  ban/unregistered => halt() latches + persists + CRITICAL alert + hook, rate_limited =>
  loud RailUnavailable without latching, halt file fails closed, clear_halt operator path.
  88 tests green in test_whatsapp_transport.py + test_pacing.py.
- cluster 3+4 DONE: registry passes the halt path + `whatsapp_email` (FailoverTransport
  over the paced WhatsApp rail); FailoverTransport handles a primary that RAISES
  RailUnavailable (terminal -> degrade, transient -> refundable, RailPaced/destination
  re-raise); HTTP /api/send maps RailPaced -> 429 rail_paced + Retry-After (postage
  refunded) and RailUnavailable -> 503 rail_unavailable. error_response grew retry_after.
  tests: test_send (2 new), test_failover (3 new), test_whatsapp_transport (1 new) all green.
- KNOWN RED (expected, this IS the deliverable): test_llms_artifact
  ::test_every_emitted_x_reason_token_is_documented now fails naming
  ['rail_paced','rail_unavailable'] -> docs cluster next.
- cluster 6 DONE: app/llms.py (render -> llms.txt), docs/cvm/llms.txt (rails + pacing/stop
  section, footer v3), docs/cvm/llms-full.txt (limits 6/7/8, footer v3), ADR-0003 consequences
  1+5 marked landed; parity tests strengthened (REQUIRED += rail_paced/rail_unavailable/
  Retry-After; test_cvm_contract += rails+gates, ban-stop-rule).
