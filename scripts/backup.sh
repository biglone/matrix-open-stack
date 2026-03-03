#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$BASE_DIR/backups"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"

mkdir -p "$OUTPUT_DIR"
ts="$(date +%Y%m%d-%H%M%S)"
archive="$OUTPUT_DIR/matrix-open-stack-backup-${ts}.tar.gz"

files=(
  data
  conf
  cloudflared
  control-plane
  scripts
  docker-compose.yml
  Dockerfile.conduwuit
  .env.example
  README.md
  Makefile
)

if [ -f "$BASE_DIR/.env" ]; then
  files+=(.env)
fi

if [ -f "$BASE_DIR/bin/conduwuit" ]; then
  files+=(bin/conduwuit)
fi

was_running=0
if docker compose -f "$COMPOSE_FILE" ps --status running --services | grep -q '^matrix$'; then
  was_running=1
  docker compose -f "$COMPOSE_FILE" stop matrix >/dev/null
fi

restore_stack() {
  if [ "$was_running" -eq 1 ]; then
    docker compose -f "$COMPOSE_FILE" start matrix >/dev/null || true
  fi
}
trap restore_stack EXIT

tar -C "$BASE_DIR" -czf "$archive" "${files[@]}"
sha256sum "$archive" > "$archive.sha256"
chmod 600 "$archive" "$archive.sha256"

echo "Backup created: $archive"
