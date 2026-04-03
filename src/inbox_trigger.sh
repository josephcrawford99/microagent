#!/bin/sh
# Triggered by inotifyd when a new file appears in an inbox
exec python3 /app/src/main.py
