#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: restore.sh --backup /path/to/backup.tar.gz"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
BACKUP_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --backup)
      BACKUP_FILE="$2"
      shift 2
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

if [ -z "$BACKUP_FILE" ] || [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

if [ -f "$COMPOSE_FILE" ]; then
  docker compose -f "$COMPOSE_FILE" down >/dev/null || true
fi

tar -C "$BASE_DIR" -xzf "$BACKUP_FILE"

if [ -f "$BASE_DIR/.env" ]; then
  chmod 600 "$BASE_DIR/.env"
fi

docker compose -f "$BASE_DIR/docker-compose.yml" up -d >/dev/null

echo "Restore completed from: $BACKUP_FILE"
