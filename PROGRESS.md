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
