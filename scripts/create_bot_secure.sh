#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Create a Matrix bot account through Conduwuit admin commands without enabling open registration.

Usage:
  create_bot_secure.sh --username localpart [--display-name "Bot Name"]

Notes:
  - This script performs a short maintenance window by stopping Matrix temporarily.
  - The generated password is printed once; store it securely.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
CONFIG_FILE="$BASE_DIR/conf/conduwuit.toml"

USERNAME=""
DISPLAY_NAME=""

while [ $# -gt 0 ]; do
  case "$1" in
    --username)
      USERNAME="$2"
      shift 2
      ;;
    --display-name)
      DISPLAY_NAME="$2"
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

if [ -z "$USERNAME" ]; then
  echo "--username is required" >&2
  exit 1
fi

if ! [[ "$USERNAME" =~ ^[a-z0-9._=-]+$ ]]; then
  echo "Invalid username. Allowed: a-z, 0-9, ., _, =, -" >&2
  exit 1
fi

server_name="$(awk -F'=' '/^server_name/{gsub(/[" ]/, "", $2); print $2}' "$CONFIG_FILE" | head -n1)"
if [ -z "$server_name" ]; then
  echo "Cannot detect server_name from $CONFIG_FILE" >&2
  exit 1
fi

was_running=0
if docker compose -f "$COMPOSE_FILE" ps --status running --services | grep -qx "matrix"; then
  was_running=1
  docker compose -f "$COMPOSE_FILE" stop matrix >/dev/null
fi

restore_stack() {
  if [ "$was_running" -eq 1 ]; then
    docker compose -f "$COMPOSE_FILE" start matrix >/dev/null || true
  fi
}
trap restore_stack EXIT

# Conduwuit keeps running after --execute; use timeout to stop the one-shot container after command output is emitted.
raw_output="$(timeout --signal=TERM 35s docker compose -f "$COMPOSE_FILE" run --rm --no-deps matrix --config /etc/conduwuit/conduwuit.toml --execute "users create-user $USERNAME" 2>&1 || true)"
clean_output="$(printf "%s" "$raw_output" | sed -r 's/\x1B\[[0-9;]*[A-Za-z]//g')"
oneline="$(printf "%s" "$clean_output" | tr '\n' ' ')"

user_id="$(printf "%s" "$oneline" | sed -n 's/.*Created user with user_id:[[:space:]]*\(@[^[:space:]]*\)[[:space:]]*and password:[[:space:]]*\([^[:space:]]*\).*/\1/p')"
password="$(printf "%s" "$oneline" | sed -n 's/.*Created user with user_id:[[:space:]]*\(@[^[:space:]]*\)[[:space:]]*and password:[[:space:]]*\([^[:space:]]*\).*/\2/p')"

if [ -z "$user_id" ] || [ -z "$password" ]; then
  echo "Failed to create bot user. Raw output:" >&2
  printf "%s\n" "$clean_output" >&2
  exit 1
fi

if [ -n "$DISPLAY_NAME" ]; then
  # Wait for homeserver to be available again, then set display name with the new user token.
  for _ in $(seq 1 45); do
    if curl -fsS "http://127.0.0.1:6167/_matrix/client/versions" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  token=""
  for _ in $(seq 1 20); do
    token="$(curl -sS "http://127.0.0.1:6167/_matrix/client/v3/login" \
      -H "Content-Type: application/json" \
      -d "{\"type\":\"m.login.password\",\"user\":\"$user_id\",\"password\":\"$password\"}" | jq -r '.access_token // empty')"
    if [ -n "$token" ]; then
      break
    fi
    sleep 1
  done

  if [ -n "$token" ]; then
    encoded_user_id="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$user_id")"
    curl -sS -X PUT "http://127.0.0.1:6167/_matrix/client/v3/profile/$encoded_user_id/displayname" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "{\"displayname\":\"$DISPLAY_NAME\"}" >/dev/null || true
  else
    echo "WARN: Bot created, but failed to set display name automatically." >&2
  fi
fi

echo "Bot user created securely."
echo "user_id=$user_id"
echo "password=$password"
echo "homeserver=https://$server_name"
