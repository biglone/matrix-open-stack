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
   - `USER_CREATE_MODE` (default `disabled`)
   - `AUDIT_LOG_PATH` (default `/var/log/matrix-control/audit.log`)
   - `INVITE_RATE_LIMIT_WINDOW_SECONDS` / `INVITE_RATE_LIMIT_MAX`
4. Optional restart controls (for UI-triggered restart):
   - `RESTART_API_MODE=disabled|docker_socket` (default `disabled`)
   - `DOCKER_GID` should match host `docker.sock` group id (`stat -c '%g' /var/run/docker.sock`)
5. Optional temporary registration window controls (for UI-triggered open/close):
   - `REGISTRATION_WINDOW_API_MODE=disabled|docker_socket` (default `disabled`)
   - `STACK_HOST_PATH` should point to this repo absolute path on host
   - `HOST_HELPER_IMAGE` defaults to `local/matrix-control-api:0.1.0`

Detailed field-by-field guide:

- `docs/ENVIRONMENT.md`

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

## Secure User Provisioning (Recommended)

By default, `USER_CREATE_MODE=disabled`, so `POST /api/users` is blocked.

Create regular local users through local secure script (no open registration required):

```bash
./scripts/create_user_secure.sh --username alice --display-name "Alice"
```

The script performs a short maintenance window and prints generated credentials once.

`POST /api/bots/invite` has built-in throttling and audit logs:

- per IP + token fingerprint rate limit
- default `12` invites per `60` seconds
- logs written to `./audit/audit.log`
- secure bot creation logs written to `./audit/security-audit.log`

## Control API Endpoints

All endpoints below require `Authorization: Bearer <CONTROL_API_TOKEN>`.

- `GET /api/config`
- `GET /api/overview` (spaces + rooms + known bots for dashboard)
- `GET /api/spaces`
- `GET /api/rooms`
- `GET /api/bots`
- `GET /api/users` (regular users from snapshot + logical status)
- `GET /api/users/full` (full local users snapshot)
- `POST /api/spaces`
- `POST /api/rooms`
- `POST /api/users` (only when `USER_CREATE_MODE=legacy_register`)
- `POST /api/users/invite`
- `POST /api/bots` (only when `BOT_CREATE_MODE=legacy_register`)
- `POST /api/bots/invite`
- `POST /api/spaces/{space_room_id}/archive`
- `POST /api/rooms/{room_id}/archive`
- `DELETE /api/spaces/{space_room_id}` (leave + forget from admin view)
- `DELETE /api/rooms/{room_id}` (leave + forget from admin view)
- `POST /api/bots/{user_id}/status` (`active|archived|deleted`, logical status in control-plane)
- `POST /api/users/{user_id}/status` (`active|archived|deleted`, logical status in control-plane)
- `POST /api/ops/restart` (`matrix|control_api|stack`, disabled by default)
- `GET /api/ops/registration-window`
- `POST /api/ops/registration-window/open` (temporary user/bot create window, auto-close)
- `POST /api/ops/registration-window/close`

Notes on list scope:

- Spaces/rooms are based on rooms joined by the configured admin account.
- Bot list combines audit logs (`audit.log`, `security-audit.log`) and joined-room member heuristic.
- Full users list comes from a host-generated snapshot file.

## Full Users Snapshot

To populate true full local users/bots inventory:

```bash
./scripts/refresh_full_users_snapshot.sh
```

This script calls Conduwuit admin command `users list-users`, writes:

- `audit/full-users-snapshot.json` (default)

and performs a short maintenance window while collecting data.

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

## Restart Helpers

```bash
./scripts/restart.sh matrix
./scripts/restart.sh control-api
./scripts/restart.sh stack
./scripts/restart.sh tunnel
./scripts/restart.sh status
```

## Temporary Registration Window

For short-term onboarding (users and bots) with auto-close, you now have two options:

1. Admin UI: `服务重启` page -> `临时创建窗口`
2. Host script:

```bash
./scripts/open_registration_window.sh --minutes 10
```

This temporarily enables:

- Conduwuit registration (`allow_registration=true`)
- `USER_CREATE_MODE=legacy_register`
- `BOT_CREATE_MODE=legacy_register`

Then it auto-restores secure defaults after the window ends.
