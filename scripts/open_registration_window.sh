#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Temporarily open Matrix registration and API create modes, then auto-close.

Usage:
  ./scripts/open_registration_window.sh [options]

Options:
  --minutes N           Window duration in minutes (default: 10)
  --without-user-api    Keep USER_CREATE_MODE unchanged
  --without-bot-api     Keep BOT_CREATE_MODE unchanged
  -h, --help            Show help

Examples:
  ./scripts/open_registration_window.sh --minutes 10
  ./scripts/open_registration_window.sh --minutes 5 --without-bot-api
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$BASE_DIR/.env"
CONF_FILE="$BASE_DIR/conf/conduwuit.toml"

WINDOW_MINUTES=10
ENABLE_USER_API=1
ENABLE_BOT_API=1

while [ $# -gt 0 ]; do
  case "$1" in
    --minutes)
      WINDOW_MINUTES="${2:-}"
      shift 2
      ;;
    --without-user-api)
      ENABLE_USER_API=0
      shift
      ;;
    --without-bot-api)
      ENABLE_BOT_API=0
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

if ! [[ "$WINDOW_MINUTES" =~ ^[0-9]+$ ]] || [ "$WINDOW_MINUTES" -le 0 ]; then
  echo "Invalid --minutes: $WINDOW_MINUTES" >&2
  exit 1
fi

if [ "$ENABLE_USER_API" -eq 0 ] && [ "$ENABLE_BOT_API" -eq 0 ]; then
  echo "Both API switches are disabled; nothing to open." >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo ".env not found: $ENV_FILE" >&2
  exit 1
fi

if [ ! -f "$CONF_FILE" ]; then
  echo "Conduwuit config not found: $CONF_FILE" >&2
  exit 1
fi

get_env_or_default() {
  local key="$1"
  local default="$2"
  local value
  value="$(awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1); found=1} END{if(!found) exit 1}' "$ENV_FILE" 2>/dev/null || true)"
  if [ -z "$value" ]; then
    echo "$default"
  else
    echo "$value"
  fi
}

set_env_key() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_conf_bool_or_default() {
  local key="$1"
  local default="$2"
  local value
  value="$(awk -F= -v key="$key" '
    $1 ~ "^[[:space:]]*" key "[[:space:]]*$" {
      v=$2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
      print v
      found=1
    }
    END { if(!found) exit 1 }
  ' "$CONF_FILE" 2>/dev/null || true)"
  if [ -z "$value" ]; then
    echo "$default"
  else
    echo "$value"
  fi
}

set_conf_bool() {
  local key="$1"
  local value="$2"
  sed -i -E "s|^([[:space:]]*${key}[[:space:]]*=[[:space:]]*).*$|\\1${value}|" "$CONF_FILE"
}

get_conf_server_name() {
  awk -F= '
    /^[[:space:]]*server_name[[:space:]]*=/ {
      v=$2
      gsub(/^[[:space:]]*"/, "", v)
      gsub(/"[[:space:]]*$/, "", v)
      print v
      exit
    }
  ' "$CONF_FILE"
}

set_conf_server_name() {
  local server_name="$1"
  sed -i -E "s|^([[:space:]]*server_name[[:space:]]*=[[:space:]]*).*$|\\1\"${server_name}\"|" "$CONF_FILE"
}

orig_user_mode="$(get_env_or_default USER_CREATE_MODE disabled)"
orig_bot_mode="$(get_env_or_default BOT_CREATE_MODE disabled)"
orig_allow_registration="$(get_conf_bool_or_default allow_registration false)"
orig_open_registration_flag="$(get_conf_bool_or_default yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse false)"
env_server_name="$(get_env_or_default MATRIX_SERVER_NAME "")"
conf_server_name="$(get_conf_server_name)"

closing_done=0
restore_secure_mode() {
  if [ "$closing_done" -eq 1 ]; then
    return
  fi
  closing_done=1

  echo "Restoring secure defaults..."
  if [ "$ENABLE_USER_API" -eq 1 ]; then
    set_env_key USER_CREATE_MODE "$orig_user_mode"
  fi
  if [ "$ENABLE_BOT_API" -eq 1 ]; then
    set_env_key BOT_CREATE_MODE "$orig_bot_mode"
  fi
  set_conf_bool allow_registration "$orig_allow_registration"
  set_conf_bool yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse "$orig_open_registration_flag"

  (cd "$BASE_DIR" && docker compose up -d --force-recreate matrix matrix-control-api)
  echo "Secure mode restored."
}

on_interrupt() {
  echo
  echo "Interrupted. Closing registration window now..."
  restore_secure_mode
  exit 130
}

on_error() {
  echo "Error occurred. Attempting to restore secure mode..."
  restore_secure_mode
}

trap on_interrupt INT TERM
trap on_error ERR

if [ -n "$env_server_name" ] && [ "$env_server_name" != "$conf_server_name" ]; then
  echo "Aligning conf server_name ($conf_server_name) to MATRIX_SERVER_NAME ($env_server_name) for safe restart."
  set_conf_server_name "$env_server_name"
fi

echo "Opening registration window for ${WINDOW_MINUTES} minute(s)..."
if [ "$ENABLE_USER_API" -eq 1 ]; then
  set_env_key USER_CREATE_MODE legacy_register
fi
if [ "$ENABLE_BOT_API" -eq 1 ]; then
  set_env_key BOT_CREATE_MODE legacy_register
fi
set_conf_bool allow_registration true
set_conf_bool yes_i_am_very_very_sure_i_want_an_open_registration_server_prone_to_abuse true

(cd "$BASE_DIR" && docker compose up -d --force-recreate matrix matrix-control-api)

echo "Window is open until approximately: $(date -d "+${WINDOW_MINUTES} minutes" '+%F %T %Z')"
sleep "$((WINDOW_MINUTES * 60))"

restore_secure_mode
trap - ERR INT TERM
echo "Done."
