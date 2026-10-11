# Live evidence re-run on the merge HEAD — t_f2a6f901 / PR #12

Box: cobrador, 2026-10-12 ~00:5x UTC. Server identity unchanged
(`npub1yfd2zvsq3lmz7n6ckjt77rm0a56s3d49n2xvpc0ct2tzx24c4wuqn4h6hf`).

Why this file exists: the earlier live evidence (`cvm-rail-start-live-after*.json`)
was captured at `77a1bfd`, i.e. BEFORE the three review-loop commits
(`1d612c3`, `9a8927b`, `794c26d`). Evidence that does not cover the merge HEAD is
not evidence for the merge, so the run was repeated on `794c26d`.

    NOSMS_TRANSPORT=jmp NOSMS_JMP_RESOURCE=cvmrail \
    NOSMS_MINT_URL=https://testnut.cashu.space NOSMS_RAIL_START_TIMEOUT=60 \
    python3.13 scripts/run_cvm_server.py --db ~/cvm-escrow-rail-start.db

    python3.13 scripts/cvm_live_verify.py \
    --server-npub npub1yfd2zvs... --to +15550100 --no-pay

## Observed on 794c26d

| check | result |
|---|---|
| rail link started by the serving process | yes — `transport start` before `listening` |
| announcements 11316/11317/11318 | accepted 3/3 relays, 0 failed |
| `sms.capabilities` | `available: true`, `rail: jmp_cheogram` |
| `sms.pricing` | 2900 sats flat (quote 2886) |
| `docs` | 7306 chars |
| `sms.send` UNPAID | `isError: true`, `reason: payment_required`, `cap: cap:tool:sms.send:2900:sats` |
| `sms.send` PAID | skipped (`--no-pay`) |
| replies received | 5/5 (relay set includes `relay.primal.net`) |

The card's defect was that `sms.send` answered `rail_not_configured` on master.
On this HEAD it reaches the payment gate and answers `payment_required`, which is
the card's stated done-condition.

Files: `cvm-rail-start-live-after-nopay-head794c26d.json` (whole report),
`cvm-rail-start-live-after-head794c26d-server.log` (server side).
