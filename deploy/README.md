# nosms deployment artefacts

Written against the landed M1a service on `master`. **Not deployed** — go-live is a
separate, reversible operator step (the card that produced this scoped deployment
as conditional on vps2 access), and nothing here has been applied to vps2.

Files:

- `nosms.service` — systemd unit for `/opt/nosms`, bound to `127.0.0.1:8088`.
  Configuration is read from `/etc/nosms.env`; no secret belongs in the unit.
- `Caddyfile.nosms` — vhost snippet for `nosms.orangesync.tech` ->
  `127.0.0.1:8088`, appended to the Caddy already fronting `*.orangesync.tech`.

DNS note (as of 2026-10-02, manager-verified): `nosms.orangesync.tech` already
resolves to `23.182.128.51` (vps2). Only the host-side steps remain.

## Environment contract (read from `app/config.py`)

| Variable | Default in code | Deploy value | Why |
|---|---|---|---|
| `NOSMS_TRANSPORT` | `fake` | `email_gateway` | the in-memory `fake` rail is a test double; the real rail is the email-to-SMS gateway |
| `NOSMS_PORT` | `8000` | `8088` | must match the Caddy vhost and the unit's `--port` |
| `NOSMS_HOST` | `127.0.0.1` | `127.0.0.1` | Caddy is the only thing that should reach it |
| `NOSMS_VERSION` | `0.1.0` | `0.1.0-m1a` | surfaced by `GET /api/health` |
| `NOSMS_COMMIT` | falls back to the local git sha | `git rev-parse --short HEAD` | surfaced by `GET /api/health` |
| `NOSMS_NIP98_FRESHNESS_SECONDS` | `120` | leave unset | documented NIP-98 window |
| `NOSMS_REPLAY_TTL_SECONDS` | `300` | leave unset | must stay >= the freshness window |

`NOSMS_ENV` is **not** read by this service — do not set it expecting an effect.

## Steps (operator-run, on vps2)

```sh
sudo useradd --system --home /opt/nosms --shell /usr/sbin/nologin nosms
sudo git clone https://github.com/felixfelix-bot/nosms /opt/nosms
cd /opt/nosms && sudo -u nosms python3 -m venv .venv
sudo -u nosms ./.venv/bin/pip install -r requirements.txt

sudo tee /etc/nosms.env >/dev/null <<EOF
NOSMS_TRANSPORT=email_gateway
NOSMS_HOST=127.0.0.1
NOSMS_PORT=8088
NOSMS_VERSION=0.1.0-m1a
NOSMS_COMMIT=$(git -C /opt/nosms rev-parse --short HEAD)
EOF

sudo cp deploy/nosms.service /etc/systemd/system/nosms.service
sudo systemctl daemon-reload && sudo systemctl enable --now nosms
curl -sS http://127.0.0.1:8088/api/health

# then append deploy/Caddyfile.nosms to the Caddyfile and: sudo systemctl reload caddy
```

End-to-end check once the vhost is live:

```sh
curl -sS https://nosms.orangesync.tech/api/health
curl -sS https://nosms.orangesync.tech/llms.txt | head
```

## Rollback

```sh
sudo systemctl disable --now nosms && sudo rm /etc/systemd/system/nosms.service
sudo rm /etc/nosms.env
# remove the nosms.orangesync.tech block from the Caddyfile and reload caddy
```

M1a serves auth/health/pricing/errors/llms only; `POST /api/send` answers 501
until the M1b card lands the escrow + transport path. Deploying it earlier than
the M1b work is harmless but buys little beyond the `/llms.txt` surface.

## Live probe

`scripts/live_probe.py` exercises a running instance over real HTTP (health,
pricing, llms, and the auth failure modes). Run it against the local port before
touching Caddy:

```sh
/opt/nosms/.venv/bin/pip install httpx coincurve    # already implied by requirements-dev.txt
/opt/nosms/.venv/bin/python scripts/live_probe.py http://127.0.0.1:8088
```
