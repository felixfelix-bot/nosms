# nosms deployment artefacts (M1a)

Written, **not deployed**. Deployment is a separate, reversible operator step: the
card that produced this directory explicitly scoped go-live as "only if vps2 access
exists", and nothing here has been applied to vps2.

Files:

- `nosms.service` - systemd unit for `/opt/nosms`, bound to `127.0.0.1:8088`.
  Configuration is read from `/etc/nosms.env`; no secret belongs in the unit.
- `Caddyfile.nosms` - vhost snippet for `nosms.orangesync.tech` -> `127.0.0.1:8088`,
  appended to the Caddy already fronting `*.orangesync.tech` on vps2.

DNS note (as of 2026-10-02, manager-verified): `nosms.orangesync.tech` already
resolves to `23.182.128.51` (vps2). So the only remaining steps are host-side.

## Steps (operator-run, on vps2)

```sh
sudo useradd --system --home /opt/nosms --shell /usr/sbin/nologin nosms
sudo git clone https://github.com/felixfelix-bot/nosms /opt/nosms
cd /opt/nosms && sudo -u nosms python3 -m venv .venv
sudo -u nosms ./.venv/bin/pip install -r requirements.txt

sudo tee /etc/nosms.env >/dev/null <<'EOF'
NOSMS_ENV=prod
NOSMS_VERSION=0.1.0-m1a
NOSMS_COMMIT=<git rev-parse --short HEAD>
NOSMS_TRANSPORT=email_gateway
EOF

sudo cp deploy/nosms.service /etc/systemd/system/nosms.service
sudo systemctl daemon-reload && sudo systemctl enable --now nosms
curl -sS http://127.0.0.1:8088/api/health

# then append deploy/Caddyfile.nosms to the Caddyfile and: sudo systemctl reload caddy
```

## Rollback

```sh
sudo systemctl disable --now nosms && sudo rm /etc/systemd/system/nosms.service
# remove the nosms.orangesync.tech block from the Caddyfile and reload caddy
```

M1a serves auth/health/pricing/errors/llms only; `POST /api/send` answers 501 until
the M1b card lands the escrow + transport path.
