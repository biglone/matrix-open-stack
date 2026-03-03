#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$BASE_DIR/.env" ]; then
  # shellcheck disable=SC1091
  source "$BASE_DIR/.env"
fi

VERSION="${CONDUWUIT_VERSION:-v0.4.6}"
ARCH="$(uname -m)"

case "$ARCH" in
  x86_64)
    ASSET="static-x86_64-unknown-linux-musl"
    ;;
  aarch64|arm64)
    ASSET="static-aarch64-unknown-linux-musl"
    ;;
  *)
    echo "Unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

URL="https://github.com/girlbossceo/conduwuit/releases/download/${VERSION}/${ASSET}"
TARGET="$BASE_DIR/bin/conduwuit"

mkdir -p "$BASE_DIR/bin"
curl -fL "$URL" -o "$TARGET"
chmod 755 "$TARGET"

echo "Downloaded conduwuit ${VERSION} (${ASSET}) -> $TARGET"
