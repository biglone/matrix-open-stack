#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Prepare this repository for first run.

Usage:
  bootstrap.sh [--server-name DOMAIN]

Examples:
  ./scripts/bootstrap.sh --server-name matrix.biglone.tech
  ./scripts/bootstrap.sh
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$BASE_DIR/.env"
ENV_EXAMPLE="$BASE_DIR/.env.example"
CONF_FILE="$BASE_DIR/conf/conduwuit.toml"
SERVER_NAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --server-name)
      SERVER_NAME="$2"
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

if [ ! -f "$ENV_FILE" ]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "Created .env from .env.example"
fi

mkdir -p "$BASE_DIR/data" "$BASE_DIR/backups" "$BASE_DIR/bin"

if grep -q '^CONTROL_API_TOKEN=change-me-to-a-long-random-token$' "$ENV_FILE"; then
  token="$(openssl rand -hex 24)"
  sed -i "s|^CONTROL_API_TOKEN=.*$|CONTROL_API_TOKEN=${token}|" "$ENV_FILE"
  echo "Generated random CONTROL_API_TOKEN in .env"
fi

if [ -n "$SERVER_NAME" ]; then
  sed -i "s|^MATRIX_SERVER_NAME=.*$|MATRIX_SERVER_NAME=${SERVER_NAME}|" "$ENV_FILE"
fi

server_from_env="$(awk -F= '/^MATRIX_SERVER_NAME=/{print $2}' "$ENV_FILE" | tail -n 1)"
if [ -n "$server_from_env" ]; then
  sed -i "s|^server_name = .*$|server_name = \"${server_from_env}\"|" "$CONF_FILE"
fi

"$SCRIPT_DIR/download_conduwuit.sh"

docker compose -f "$BASE_DIR/docker-compose.yml" config >/dev/null

echo "Bootstrap completed. Next: docker compose up -d --build"
