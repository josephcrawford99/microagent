#!/bin/sh
set -e

echo "=== microagent setup ==="
echo ""

# Detect compose command (v2 plugin vs v1 standalone)
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' found."
    echo "  brew install docker docker-compose"
    exit 1
fi

# Check docker daemon
if ! docker info >/dev/null 2>&1; then
    if command -v colima >/dev/null 2>&1; then
        echo "docker daemon not running, starting colima..."
        colima start
    else
        echo "ERROR: docker daemon not running."
        echo "  install colima: brew install colima"
        echo "  then: colima start"
        exit 1
    fi
fi

echo "using: $DC"
echo ""

echo "[1/3] building image..."
$DC build

echo ""
echo "[2/3] health check with ping agent..."
$DC run --rm -T --entrypoint "" microagent sh -c '
    mkdir -p /data/interfaces/terminal/inbox /data/interfaces/terminal/outbox
    echo "{\"id\":\"health\",\"channel\":\"terminal\",\"from\":\"setup\",\"to\":\"agent\",\"body\":\"ping\",\"thread\":\"healthcheck\"}" > /data/interfaces/terminal/inbox/health.json
    cd /app/src
    SOUL_DIR=/soul DATA_DIR=/data python3 -c "
import sys, os
sys.path.insert(0, \".\")
from lib.config import load_config, load_soul_prompt
from agent_types.ping import Ping
from lib.messages import read_message
msg = read_message(\"/data/interfaces/terminal/inbox/health.json\")
agent = Ping(load_config(), load_soul_prompt(), \"/data\")
result = agent.wake([msg])
os.remove(\"/data/interfaces/terminal/inbox/health.json\")
if result == \"pong\":
    print(\"health check passed\")
    sys.exit(0)
else:
    print(\"health check FAILED: expected pong, got \" + str(result))
    sys.exit(1)
"
'
if [ $? -ne 0 ]; then
    echo "ERROR: health check failed"
    exit 1
fi

echo ""
echo "[3/3] starting microagent..."
$DC up -d

echo ""
echo "=== microagent is running ==="
echo ""
echo "  status:  $DC ps"
echo "  logs:    $DC logs -f"
echo "  talk:    python3 talk.py"
echo "  stop:    $DC down"
echo ""
echo "if using the claude agent type, send your first message."
echo "if not authenticated, the agent will tell you what to do."
echo ""
