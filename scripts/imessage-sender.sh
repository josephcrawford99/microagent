#!/bin/bash
# Host-side iMessage sender.
#
# Polls $OUTBOX for *.json files dropped by the container's IMessage
# interface, dispatches each via osascript -> Messages.app, and moves the
# file to sent/ or failed/.
#
# Deps: only what ships with macOS (bash, python3, osascript). No brew/port.
# Permissions: first run will prompt for Automation access to Messages.
#
# Run via launchd (see scripts/com.microagent.imessage.plist) for auto-start.

set -u

OUTBOX="${IMSG_OUTBOX:-$HOME/microagent/imessage-outbox}"
SENT="$OUTBOX/sent"
FAILED="$OUTBOX/failed"
LOG="$OUTBOX/sender.log"
POLL_INTERVAL="${IMSG_POLL_INTERVAL:-1}"

mkdir -p "$SENT" "$FAILED"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

# Read a field from a JSON file without jq. Prints the value on stdout,
# exit code 0 on success, 1 on any parse/lookup error.
read_field() {
    /usr/bin/python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as f:
        v = json.load(f).get(sys.argv[2], "")
    sys.stdout.write(v if isinstance(v, str) else "")
except Exception:
    sys.exit(1)
' "$1" "$2"
}

send_one() {
    local f="$1"
    [[ "$f" == *.json ]] || return 0
    [[ -f "$f" ]] || return 0

    local to body
    if ! to=$(read_field "$f" to) || ! body=$(read_field "$f" body); then
        log "bad json: $(basename "$f")"
        mv "$f" "$FAILED/"
        return
    fi
    if [[ -z "$to" ]]; then
        log "missing .to in $(basename "$f")"
        mv "$f" "$FAILED/"
        return
    fi

    # Pass body via env var so AppleScript pulls it with `system attribute`,
    # avoiding all shell/osascript string escaping hazards.
    if IMSG_BODY="$body" osascript \
        -e 'on run argv' \
        -e '  set targetBuddy to item 1 of argv' \
        -e '  set msgBody to (system attribute "IMSG_BODY")' \
        -e '  tell application "Messages"' \
        -e '    set svc to 1st service whose service type = iMessage' \
        -e '    send msgBody to buddy targetBuddy of svc' \
        -e '  end tell' \
        -e 'end run' \
        -- "$to" >>"$LOG" 2>&1
    then
        log "sent -> $to ($(basename "$f"))"
        mv "$f" "$SENT/"
    else
        log "send FAILED -> $to ($(basename "$f"))"
        mv "$f" "$FAILED/"
    fi
}

log "imessage-sender starting, watching $OUTBOX (poll=${POLL_INTERVAL}s)"

shopt -s nullglob
while true; do
    for f in "$OUTBOX"/*.json; do
        send_one "$f"
    done
    sleep "$POLL_INTERVAL"
done
