#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Restore a Matrix Docker stack backup.

Usage:
  restore.sh --backup FILE [--no-start]

Options:
  --backup FILE      Backup .tar.gz file created by backup.sh
  --no-start         Do not start containers after restore
  -h, --help         Show help
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
BACKUP_FILE=""
START_AFTER=1

while [ $# -gt 0 ]; do
  case "$1" in
    --backup)
      BACKUP_FILE="$2"
      shift 2
      ;;
    --no-start)
      START_AFTER=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [ -z "$BACKUP_FILE" ]; then
  echo "--backup is required" >&2
  usage
  exit 1
fi
if [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

mkdir -p "$BASE_DIR/backups"
SAFETY_TS="$(date +%Y%m%d-%H%M%S)"
SAFETY_ARCHIVE="$BASE_DIR/backups/pre-restore-$SAFETY_TS.tar.gz"

# Safety snapshot of current state before overwrite.
if [ -f "$BASE_DIR/docker-compose.yml" ]; then
  SAFETY_FILES=(data audit conf cloudflared docker-compose.yml Dockerfile.conduwuit control-plane scripts README.md Makefile .env.example)
  if [ -f "$BASE_DIR/.env" ]; then
    SAFETY_FILES+=(".env")
  fi
  if [ -f "$BASE_DIR/bin/conduwuit" ]; then
    SAFETY_FILES+=("bin/conduwuit")
  fi
  tar -C "$BASE_DIR" -czf "$SAFETY_ARCHIVE" "${SAFETY_FILES[@]}" 2>/dev/null || true
fi

if [ -f "$COMPOSE_FILE" ]; then
  docker compose -f "$COMPOSE_FILE" down >/dev/null || true
fi

rm -rf "$BASE_DIR/data"
rm -rf "$BASE_DIR/audit"
rm -rf "$BASE_DIR/conf"
rm -rf "$BASE_DIR/control-plane"
rm -rf "$BASE_DIR/scripts"
rm -rf "$BASE_DIR/cloudflared"
rm -rf "$BASE_DIR/bin"
rm -f "$BASE_DIR/docker-compose.yml" "$BASE_DIR/Dockerfile.conduwuit" "$BASE_DIR/README.md" "$BASE_DIR/Makefile" "$BASE_DIR/.env" "$BASE_DIR/.env.example"

tar -C "$BASE_DIR" -xzf "$BACKUP_FILE"

if [ -f "$BASE_DIR/.env" ]; then
  chmod 600 "$BASE_DIR/.env"
fi

if [ "$START_AFTER" -eq 1 ] && [ -f "$BASE_DIR/docker-compose.yml" ]; then
  docker compose -f "$BASE_DIR/docker-compose.yml" up -d >/dev/null
fi

echo "Restore completed from: $BACKUP_FILE"
echo "Safety snapshot: $SAFETY_ARCHIVE"
