#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
SOUL_DIR="${SOUL_DIR:-/soul}"

mkdir -p "$DATA_DIR/interfaces" "$DATA_DIR/workspace" "$DATA_DIR/sessions"

# Generate crontab from config
SCHEDULE=$(python3 -c "import json; print(json.load(open('${SOUL_DIR}/config.json'))['schedule'])")
echo "$SCHEDULE cd /app/src && python3 main.py >> ${DATA_DIR}/agent.log 2>&1" > /etc/crontabs/root

echo "microagent starting"
echo "  schedule: $SCHEDULE"
echo "  soul: $SOUL_DIR"
echo "  data: $DATA_DIR"

# Start crond in background (scheduled wakes)
crond -l 2 &

# Watch all interface inboxes — trigger main.py on new files
# Respawn watchers every 60s to pick up new interfaces
while true; do
    for inbox in "$DATA_DIR"/interfaces/*/inbox; do
        [ -d "$inbox" ] && inotifyd /app/src/inbox_trigger.sh "$inbox":c &
    done
    sleep 60
    pkill -f "inotifyd.*inbox_trigger" 2>/dev/null || true
done
