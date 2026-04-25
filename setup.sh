#!/usr/bin/env bash
set -e

# Find docker. ssh-non-interactive often has a stripped PATH that omits
# MacPorts / Homebrew bin dirs, so probe common locations too.
if ! command -v docker >/dev/null 2>&1; then
  for d in /opt/homebrew/bin /usr/local/bin /opt/local/bin; do
    if [ -x "$d/docker" ]; then PATH="$d:$PATH"; break; fi
  done
fi
command -v docker >/dev/null 2>&1 || { echo "docker not found in PATH"; exit 1; }

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/microagent"
REPO="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$CONFIG_DIR"

[ -f "$CONFIG_DIR/config.toml" ] || cp "$REPO/src/defaults/config.default.toml" "$CONFIG_DIR/config.toml"
[ -f "$CONFIG_DIR/soul.md"     ] || cp "$REPO/src/defaults/soul.default.md"     "$CONFIG_DIR/soul.md"

if [ ! -f "$CONFIG_DIR/.env" ]; then
  read -rp "CLAUDE_CODE_OAUTH_TOKEN: " TOKEN
  {
    echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN"
    echo "DASHBOARD_TOKEN=$(openssl rand -hex 24)"
  } > "$CONFIG_DIR/.env"
fi

echo "config dir: $CONFIG_DIR"
exec docker compose -f "$REPO/docker-compose.yml" up -d --build
