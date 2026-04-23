#!/usr/bin/env bash
set -e

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/microagent"
REPO="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$CONFIG_DIR"

[ -f "$CONFIG_DIR/config.toml" ] || cp "$REPO/src/examples/config.example.toml" "$CONFIG_DIR/config.toml"
[ -f "$CONFIG_DIR/soul.md"     ] || cp "$REPO/src/examples/soul.example.md"     "$CONFIG_DIR/soul.md"

if [ ! -f "$CONFIG_DIR/.env" ]; then
  read -rp "CLAUDE_CODE_OAUTH_TOKEN: " TOKEN
  {
    echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN"
    echo "DASHBOARD_TOKEN=$(openssl rand -hex 24)"
  } > "$CONFIG_DIR/.env"
fi

echo "config dir: $CONFIG_DIR"
exec docker compose -f "$REPO/docker-compose.yml" up -d --build
