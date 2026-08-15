#!/usr/bin/env bash
# Run the daemon on the host instead of in the container, against a gitignored
# ./local/ tree. Secrets (GEMINI_API_KEY, …) go in the repo-root .env, which
# main.py loads before resolving any path.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"

export MICROAGENT_CONFIG_DIR="${MICROAGENT_CONFIG_DIR:-$REPO/local/config}"
export MICROAGENT_STATE_DIR="${MICROAGENT_STATE_DIR:-$REPO/local/state}"
export MICROAGENT_SPACE_DIR="${MICROAGENT_SPACE_DIR:-$REPO/local/space}"
export MICROAGENT_REPO_DIR="${MICROAGENT_REPO_DIR:-$REPO}"

mkdir -p "$MICROAGENT_CONFIG_DIR" "$MICROAGENT_STATE_DIR" "$MICROAGENT_SPACE_DIR"

[ -f "$MICROAGENT_CONFIG_DIR/config.toml" ] \
  || cp "$REPO/src/defaults/config.local.toml" "$MICROAGENT_CONFIG_DIR/config.toml"
[ -f "$MICROAGENT_CONFIG_DIR/soul.md" ] \
  || cp "$REPO/src/defaults/soul.default.md" "$MICROAGENT_CONFIG_DIR/soul.md"

PY="python3"
[ -x "$REPO/.venv/bin/python3" ] && PY="$REPO/.venv/bin/python3"

echo "config: $MICROAGENT_CONFIG_DIR"
echo "state:  $MICROAGENT_STATE_DIR"
echo "space:  $MICROAGENT_SPACE_DIR"

# cd matters: the Dockerfile's WORKDIR /repo/src is what makes the bare
# `lib.*` / `sources.*` / `agent_types.*` imports resolve.
cd "$REPO/src"
exec "$PY" -u main.py
