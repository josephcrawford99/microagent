#!/bin/sh
set -e

DATA_DIR="${DATA_DIR:-/data}"
SOUL_DIR="${SOUL_DIR:-/soul}"
SRC_DIR="/app/src"

mkdir -p "$DATA_DIR/interfaces" "$DATA_DIR/sessions"

# Cron: scheduled wakes from config
SCHEDULE=$(python3 -c "import json; print(json.load(open('${SOUL_DIR}/config.json'))['schedule'])")
echo "$SCHEDULE cd $SRC_DIR && python3 main.py >> ${DATA_DIR}/agent.log 2>&1" > /etc/crontabs/root
crond -l 2

# inotifyd: wake on new inbox messages
while true; do
    for inbox in "$DATA_DIR"/interfaces/*/inbox; do
        [ -d "$inbox" ] && inotifyd "$SRC_DIR/inbox_trigger.sh" "$inbox":c &
    done
    sleep 60
    pkill -f "inotifyd.*inbox_trigger" 2>/dev/null || true
done
