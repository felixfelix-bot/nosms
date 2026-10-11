# Live evidence — t_f2a6f901: the rail is started, the paid path is reachable

Box: cobrador (Linux), 2026-10-11 ~22:0x UTC. Same server identity as the card's
original failing run (`npub1yfd2zvsq3lmz7n6ckjt77rm0a56s3d49n2xvpc0ct2tzx24c4wuqn4h6hf`).

Both legs were run under my own control on this box, with one harness:

- **before** = the server from `master` (142b0d5), in the throwaway worktree
  `/home/c03rad0r/worktrees/t_cvm_rail_start-RED`
- **after** = the server from this branch, in the task worktree

## Commands

    # server (branch pr/cvm-rail-start, worktree .worktrees/t_cvm_rail_start)
    NOSMS_TRANSPORT=jmp NOSMS_JMP_RESOURCE=cvmrail \
    NOSMS_MINT_URL=https://testnut.cashu.space NOSMS_RAIL_START_TIMEOUT=60 \
    python3.13 scripts/run_cvm_server.py --db ~/cvm-escrow-rail-start.db

    # client — unpaid leg (capabilities must be true, send must reach the gate)
    python3.13 scripts/cvm_live_verify.py --server-npub npub1yfd2zs... --to +155****0100 --no-pay

    # client — paid leg (mints a testnut token, then a REAL SMS leaves the line)
    python3.13 scripts/cvm_live_verify.py --server-npub npub1yfd2zs... --to +1******8875

## Observed

| step | before (this box, card's bug) | after (this run) |
|---|---|---|
| announcements 11316/11317/11318 | published OK | published OK, 3/3 relays, 0 failed |
| `sms.capabilities` | `available: false` | **`available: true`** |
| `sms.send` UNPAID | `isError: true, reason=rail_not_configured` | **`isError: true, reason=payment_required`** (`cap:tool:sms.send:2900:sats`) |
| `sms.send` PAID | never reached | **`accepted=True price=2900 best_effort=True`** |
| `sms.status` | n/a | answered (`status=unknown`) |

Artifacts:

- `cvm-rail-start-live-before.json` — the master server, whole report
  (`available: false`, `rail_not_configured`), with
  `cvm-rail-start-live-before-server.log` beside it.

- `cvm-rail-start-live-after-nopay.json` — the `--no-pay` leg, whole report.
- `cvm-rail-start-live-after.json` — the paid leg, all six steps (an unpaid
  `payment_required` and the accepted paid send in one report).
- `cvm-rail-start-live-after-server.log` — server stdout: the free tools' setup,
  the three announcements on 3/3 relays, `listening`.
- `cvm-rail-start-before.json` / `cvm-rail-start-after.json` /
  `cvm-rail-start-rail-down.json` — the same three states, deterministic and
  offline (`scripts/cvm_rail_start_probe.py`), incl. the honest
  `rail_unavailable:not_connected` answer.
- `cvm-rail-start-red-on-master.txt` — the RED-first run on master (9 failed).

Destination numbers are masked by the harness; the paid leg's SMS went to the
newest inbound peer of the operator's own JMP line (the same target
`scripts/jmp_rail_smoke.py` uses). Nothing here contains a raw number.
