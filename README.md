# Matrix Open Stack

Open-source Matrix stack with:

- Conduwuit (Matrix homeserver)
- Matrix Control API (create spaces/rooms and manage bot workflows)
- Web control panel
- Cloudflared template for public exposure

This repository is designed for **clone -> bootstrap -> docker compose up**.

## Clone

```bash
git clone <YOUR_REPO_URL>
cd matrix-open-stack
```

## Repository Layout

- `docker-compose.yml`: main deployment stack
- `conf/conduwuit.toml`: Matrix homeserver config
- `control-plane/`: API + web panel
- `scripts/`: bootstrap, backup, restore, binary downloader
- `cloudflared/`: tunnel template config

## Quick Start

```bash
./scripts/bootstrap.sh --server-name matrix.example.com
docker compose up -d --build
docker compose ps
```

Local URLs:

- Matrix: `http://127.0.0.1:6167`
- Control panel: `http://127.0.0.1:6180`

## Environment Variables

1. Copy `.env.example` to `.env` (bootstrap does this automatically).
2. Set at least:
   - `MATRIX_SERVER_NAME`
   - `MATRIX_ADMIN_USER` / `MATRIX_ADMIN_PASSWORD` (or `MATRIX_ADMIN_TOKEN`)
   - `CONTROL_API_TOKEN`
3. Optional hardening controls:
   - `BOT_CREATE_MODE` (default `disabled`)
   - `AUDIT_LOG_PATH` (default `/var/log/matrix-control/audit.log`)
   - `INVITE_RATE_LIMIT_WINDOW_SECONDS` / `INVITE_RATE_LIMIT_MAX`

## Cloudflared (Public Access)

1. Copy `cloudflared/matrix-openclaw.template.yml` to `~/.cloudflared/<your>.yml`
2. Replace placeholders:
   - `<TUNNEL_ID>`
   - `<CREDENTIALS_FILE_ABS_PATH>`
   - `<MATRIX_DOMAIN>`
   - `<MATRIX_ADMIN_DOMAIN>`
3. Route DNS to your tunnel and run cloudflared as a service.

## Secure Bot Provisioning (Recommended)

By default, `BOT_CREATE_MODE=disabled`, so `POST /api/bots` is blocked.

Create bot users through local secure script (no open registration required):

```bash
./scripts/create_bot_secure.sh --username opsbot --display-name "Ops Bot"
```

The script performs a short maintenance window and prints generated credentials once.

`POST /api/bots/invite` has built-in throttling and audit logs:

- per IP + token fingerprint rate limit
- default `12` invites per `60` seconds
- logs written to `./audit/audit.log`
- secure bot creation logs written to `./audit/security-audit.log`

## Source Development (Optional)

This repository runs Conduwuit from official release binaries downloaded by `scripts/download_conduwuit.sh`.

If you want Conduwuit source code for development, clone it separately:

```bash
git clone https://github.com/girlbossceo/conduwuit.git
```

## Security Checklist

- Do not commit `.env`, `data/`, or cloudflared credentials JSON.
- Keep `CONTROL_API_TOKEN` long and random.
- Add Cloudflare Access (or equivalent) before exposing admin panel publicly.

## Backup / Restore

```bash
./scripts/backup.sh
./scripts/restore.sh --backup ./backups/<file>.tar.gz
```
