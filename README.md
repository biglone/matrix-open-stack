# Matrix Open Stack

Open-source Matrix stack with:

- Conduwuit (Matrix homeserver)
- Matrix Control API (create spaces/rooms/bot users)
- Web control panel
- Cloudflared template for public exposure

This repository is designed for **clone -> bootstrap -> docker compose up**.

## Clone

```bash
git clone --recurse-submodules <YOUR_REPO_URL>
cd matrix-open-stack
```

## Repository Layout

- `docker-compose.yml`: main deployment stack
- `conf/conduwuit.toml`: Matrix homeserver config
- `control-plane/`: API + web panel
- `scripts/`: bootstrap, backup, restore, binary downloader
- `cloudflared/`: tunnel template config
- `matrix-core/conduwuit/`: Matrix upstream source (git submodule)

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

## Cloudflared (Public Access)

1. Copy `cloudflared/matrix-openclaw.template.yml` to `~/.cloudflared/<your>.yml`
2. Replace placeholders:
   - `<TUNNEL_ID>`
   - `<CREDENTIALS_FILE_ABS_PATH>`
   - `<MATRIX_DOMAIN>`
   - `<MATRIX_ADMIN_DOMAIN>`
3. Route DNS to your tunnel and run cloudflared as a service.

## Submodule Note

This repo includes Matrix upstream source as a submodule:

```bash
git submodule update --init --recursive
```

It is for source inspection/custom development. Runtime defaults to the released binary downloaded by `scripts/download_conduwuit.sh`.

## Security Checklist

- Do not commit `.env`, `data/`, or cloudflared credentials JSON.
- Keep `CONTROL_API_TOKEN` long and random.
- Add Cloudflare Access (or equivalent) before exposing admin panel publicly.

## Backup / Restore

```bash
./scripts/backup.sh
./scripts/restore.sh --backup ./backups/<file>.tar.gz
```
