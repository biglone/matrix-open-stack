#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Create a one-click backup for the Matrix Docker stack.

Usage:
  backup.sh [--output-dir DIR] [--no-stop]

Options:
  --output-dir DIR   Backup output directory (default: ../backups)
  --no-stop          Do not stop the Matrix container before backup
  -h, --help         Show help
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
OUTPUT_DIR="$BASE_DIR/backups"
STOP_STACK=1

while [ $# -gt 0 ]; do
  case "$1" in
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --no-stop)
      STOP_STACK=0
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

mkdir -p "$OUTPUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$OUTPUT_DIR/matrix-open-stack-backup-$TS.tar.gz"

FILES=(
  data
  audit
  conf
  cloudflared
  docker-compose.yml
  Dockerfile.conduwuit
  control-plane
  scripts
  README.md
  Makefile
)

if [ -f "$BASE_DIR/.env" ]; then
  FILES+=(".env")
fi

if [ -f "$BASE_DIR/.env.example" ]; then
  FILES+=(".env.example")
fi

if [ -f "$BASE_DIR/bin/conduwuit" ]; then
  FILES+=("bin/conduwuit")
fi

WAS_RUNNING=0
if [ "$STOP_STACK" -eq 1 ] && [ -f "$COMPOSE_FILE" ]; then
  if docker compose -f "$COMPOSE_FILE" ps --status running --services | grep -qx "matrix"; then
    WAS_RUNNING=1
    docker compose -f "$COMPOSE_FILE" stop matrix >/dev/null
  fi
fi

restore_stack() {
  if [ "$WAS_RUNNING" -eq 1 ]; then
    docker compose -f "$COMPOSE_FILE" start matrix >/dev/null || true
  fi
}
trap restore_stack EXIT

tar -C "$BASE_DIR" -czf "$ARCHIVE" "${FILES[@]}"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"

chmod 600 "$ARCHIVE" "$ARCHIVE.sha256"

echo "Backup created: $ARCHIVE"
echo "Checksum file: $ARCHIVE.sha256"
