# nosms deployment artefacts

Written against the landed M1b service (branch `pr/m1b-send-escrow`; M1a is on `master`).
**Not deployed** — go-live is a separate, reversible operator step (the card that produced
this scoped deployment as conditional on vps2 access), and nothing here has been applied
to vps2.

Files:

- `nosms.service` — systemd unit for `/opt/nosms`, bound to `127.0.0.1:8088`.
  Configuration is read from `/etc/nosms.env`; no secret belongs in the unit.
- `nosms-refund-sweep.service` + `nosms-refund-sweep.timer` — the T+15 min auto-refund
  sweep (oneshot, every 5 min) that returns undelivered postage to the payer. It reads the
  same `/etc/nosms.env` and the same SQLite file as the service, so it must run as the same
  user with access to the same state directory.
- `Caddyfile.nosms` — vhost snippet for `nosms.orangesync.tech` ->
  `127.0.0.1:8088`, appended to the Caddy already fronting `*.orangesync.tech`.

DNS note (as of 2026-10-02, manager-verified): `nosms.orangesync.tech` already
resolves to `23.182.128.51` (vps2). Only the host-side steps remain.

## Environment contract (read from `app/config.py`)

| Variable | Default in code | Deploy value | Why |
|---|---|---|---|
| `NOSMS_TRANSPORT` | `fake` | `email_gateway` | the in-memory `fake` rail is a test double; today's real rail is the email-to-SMS gateway (`telnyx` needs `NOSMS_SMS_GATEWAY_PATH` + `TELNYX_*`, see below) |
| `NOSMS_HOST` | `127.0.0.1` | `127.0.0.1` | Caddy is the only thing that should reach it |
| `NOSMS_PORT` | `8000` | `8088` | must match the Caddy vhost and the unit's `--port` |
| `NOSMS_VERSION` | `0.1.0` | `0.1.0-m1b` | surfaced by `GET /api/health` |
| `NOSMS_COMMIT` | falls back to the local git sha | `git rev-parse --short HEAD` | surfaced by `GET /api/health` |
| `NOSMS_NIP98_FRESHNESS_SECONDS` | `120` | leave unset | documented NIP-98 window |
| `NOSMS_REPLAY_TTL_SECONDS` | `300` | leave unset | must stay >= the freshness window |
| `NOSMS_DB_PATH` | `:memory:` | `/var/lib/nosms/nosms.db` | escrow + quota ledger. `:memory:` loses every escrow on restart — never acceptable in prod, and the sweep must open the *same* file the service writes |
| `NOSMS_MINT_URL` | `https://testnut.cashu.space` | testnut for now | the mint postage is escrowed against; a token from any other mint is refused (`token_wrong_mint`) |
| `NOSMS_REFUND_AFTER_SECONDS` | `900` | leave unset | the T+15 min refund watermark; set to `0` only to make a test sweep fire immediately |
| `NOSMS_DESTINATION_COOLDOWN_SECONDS` | `60` | leave unset | per (identity, destination) cooldown; `0` disables |
| `NOSMS_DAILY_CAP` | `100` | leave unset | per-identity rolling-24 h cap; `0` disables |
| `NOSMS_SMS_GATEWAY_PATH` | `~/repos/sms-gateway` | `/opt/sms-gateway` when using the `telnyx` rail | the checkout the Telnyx adapter imports; in prod an absolute path, not `~` (the unit runs with `ProtectHome=true`) |
| `NOSMS_MAX_BODY_CHARS` | `640` | leave unset | HTTP-level body ceiling (`body_too_long`) |
| `TELNYX_API_KEY` / `TELNYX_FROM_NUMBER` / `TELNYX_MESSAGING_PROFILE_ID` | unset | only for the `telnyx` rail | **secrets** — `/etc/nosms.env` must be `chmod 600`. Telnyx cannot currently sell us an SMS-capable number (PLAN §8a), so the rail stays unconfigured and refuses to send |

`NOSMS_ENV` is **not** read by this service — do not set it expecting an effect.

## Steps (operator-run, on vps2)

```sh
sudo useradd --system --home /opt/nosms --shell /usr/sbin/nologin nosms
sudo git clone https://github.com/felixfelix-bot/nosms /opt/nosms
sudo git clone https://github.com/felixfelix-bot/sms-gateway /opt/sms-gateway   # only for NOSMS_TRANSPORT=telnyx
cd /opt/nosms && sudo -u nosms python3 -m venv .venv
sudo -u nosms ./.venv/bin/pip install -r requirements.txt

sudo mkdir -p /var/lib/nosms && sudo chown nosms:nosms /var/lib/nosms

sudo install -m 600 /dev/null /etc/nosms.env
sudo tee /etc/nosms.env >/dev/null <<EOF
NOSMS_TRANSPORT=email_gateway
NOSMS_HOST=127.0.0.1
NOSMS_PORT=8088
NOSMS_VERSION=0.1.0-m1b
NOSMS_DB_PATH=/var/lib/nosms/nosms.db
NOSMS_MINT_URL=https://testnut.cashu.space
NOSMS_SMS_GATEWAY_PATH=/opt/sms-gateway
NOSMS_COMMIT=$(git -C /opt/nosms rev-parse --short HEAD)
EOF

sudo cp deploy/nosms.service deploy/nosms-refund-sweep.service \
        deploy/nosms-refund-sweep.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nosms nosms-refund-sweep.timer
curl -sS http://127.0.0.1:8088/api/health

# the sweep is visible as a timer and its runs land in the journal:
systemctl list-timers nosms-refund-sweep.timer
journalctl -u nosms-refund-sweep.service -n 5

# then append deploy/Caddyfile.nosms to the Caddyfile and: sudo systemctl reload caddy
```

End-to-end check once the vhost is live:

```sh
curl -sS https://nosms.orangesync.tech/api/health
curl -sS https://nosms.orangesync.tech/llms.txt | head
```

## Rollback

```sh
sudo systemctl disable --now nosms nosms-refund-sweep.timer
sudo rm /etc/systemd/system/nosms.service \
        /etc/systemd/system/nosms-refund-sweep.service \
        /etc/systemd/system/nosms-refund-sweep.timer
sudo systemctl daemon-reload
sudo rm /etc/nosms.env
# state (escrow ledger) lives in /var/lib/nosms/nosms.db — keep it if a refund is outstanding
# remove the nosms.orangesync.tech block from the Caddyfile and reload caddy
```

The service now serves the full M1b surface: `POST /api/send` (NIP-98 + Cashu escrow),
`GET /api/message/<id>/status`, `GET /api/refund/<id>`, plus the M1a auth/health/pricing/
errors/llms. A real SMS still needs an SMS-capable number; with the `email_gateway` rail the
send path is best-effort and — because that rail cannot observe delivery — it is *never*
auto-refunded (silence is not proof of non-delivery).

## Verification

```sh
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest            # suite + the >=80% coverage gate (see .coveragerc)
```

Two network-touching checks are deliberately *not* part of the suite:

- `scripts/live_escrow_roundtrip.py [mint_url]` — real mint round-trip (mint → NUT-07 →
  escrow swap → respend → refund token) against testnut; proves the escrow path with real
  cryptography and no real sats.
- `scripts/live_probe.py <base_url>` — drives a running instance over real HTTP (health,
  pricing, llms, and the auth failure modes). Run it against the local port before touching
  Caddy:

```sh
/opt/nosms/.venv/bin/pip install httpx coincurve    # already implied by requirements-dev.txt
/opt/nosms/.venv/bin/python scripts/live_probe.py http://127.0.0.1:8088
```
