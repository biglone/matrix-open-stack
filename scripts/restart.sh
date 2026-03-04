#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Restart helpers for matrix-open-stack.

Usage:
  ./scripts/restart.sh [target]

Targets:
  matrix        Restart matrix service container only
  control-api   Recreate matrix-control-api container
  stack         Recreate matrix + control-api containers (default)
  tunnel        Restart cloudflared-matrix.service
  status        Show docker and tunnel status
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${1:-stack}"

cd "$BASE_DIR"

case "$TARGET" in
  -h|--help|help)
    usage
    exit 0
    ;;
  matrix)
    docker compose restart matrix
    ;;
  control-api|control_api|control)
    docker compose up -d --force-recreate matrix-control-api
    ;;
  stack|all)
    docker compose up -d --force-recreate matrix matrix-control-api
    ;;
  tunnel)
    sudo systemctl restart cloudflared-matrix.service
    ;;
  status)
    docker compose ps
    echo
    systemctl status cloudflared-matrix.service --no-pager -l | sed -n '1,20p' || true
    exit 0
    ;;
  *)
    echo "Unknown target: $TARGET" >&2
    usage
    exit 1
    ;;
esac

docker compose ps
