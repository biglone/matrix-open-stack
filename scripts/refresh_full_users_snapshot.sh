#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Refresh full local user snapshot for the control-plane dashboard.

Usage:
  refresh_full_users_snapshot.sh [--output FILE]

Notes:
  - This command performs a short maintenance window by stopping Matrix temporarily.
  - Snapshot is generated from `admin users list-users`.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$BASE_DIR/docker-compose.yml"
ENV_FILE="$BASE_DIR/.env"
OUTPUT_FILE="$BASE_DIR/audit/full-users-snapshot.json"

while [ $# -gt 0 ]; do
  case "$1" in
    --output)
      OUTPUT_FILE="$2"
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

server_name=""
if [ -f "$ENV_FILE" ]; then
  server_name="$(awk -F= '/^MATRIX_SERVER_NAME=/{print $2}' "$ENV_FILE" | head -n1 | tr -d '[:space:]')"
fi
if [ -z "$server_name" ]; then
  server_name="matrix.example.com"
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

raw_output="$(timeout --signal=TERM 40s docker compose -f "$COMPOSE_FILE" run --rm --no-deps matrix --config /etc/conduwuit/conduwuit.toml --execute "users list-users" 2>&1 || true)"
clean_output="$(printf "%s" "$raw_output" | sed -r 's/\x1B\[[0-9;]*[A-Za-z]//g')"

user_lines="$(printf "%s\n" "$clean_output" | sed -n 's/^\(@[^[:space:]]*:[^[:space:]]*\)$/\1/p' | sort -u)"
if [ -z "$user_lines" ]; then
  echo "Failed to parse local users from admin output." >&2
  printf "%s\n" "$clean_output" >&2
  exit 1
fi

tmp_users="$(mktemp)"
printf "%s\n" "$user_lines" > "$tmp_users"

output_dir="$(dirname "$OUTPUT_FILE")"
mkdir -p "$output_dir"

jq -Rn \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg server_name "$server_name" \
  '
  [inputs | select(length > 0)] as $users |
  {
    generated_at: $generated_at,
    source: "conduwuit admin users list-users",
    server_name: $server_name,
    users: ($users | map({
      user_id: .,
      username: (ltrimstr("@") | split(":")[0]),
      is_bot: ((ltrimstr("@") | split(":")[0] | ascii_downcase | contains("bot")))
    }))
  }
  ' < "$tmp_users" > "$OUTPUT_FILE"

rm -f "$tmp_users"
chmod 600 "$OUTPUT_FILE" 2>/dev/null || true

echo "Snapshot written: $OUTPUT_FILE"
echo "User count: $(jq '.users | length' "$OUTPUT_FILE")"
