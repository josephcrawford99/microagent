#!/usr/bin/env python3
"""Talk to microagent via the terminal interface. Type stuff, get responses."""

import json
import os
import sys
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "./data")
INBOX = os.path.join(DATA_DIR, "interfaces", "terminal", "inbox")
OUTBOX = os.path.join(DATA_DIR, "interfaces", "terminal", "outbox")


def send(text):
    os.makedirs(INBOX, exist_ok=True)
    ts = str(int(time.time() * 1000))
    msg = {
        "id": ts,
        "channel": "terminal",
        "from": "user",
        "to": "agent",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "body": text,
        "thread": "terminal",
    }
    with open(os.path.join(INBOX, f"{ts}.json"), "w") as f:
        json.dump(msg, f)


def watch_outbox():
    while True:
        try:
            if os.path.isdir(OUTBOX):
                for fname in sorted(os.listdir(OUTBOX)):
                    if not fname.endswith(".json"):
                        continue
                    path = os.path.join(OUTBOX, fname)
                    with open(path) as f:
                        msg = json.load(f)
                    os.remove(path)
                    print(f"\n< {msg.get('body', '')}")
                    print("> ", end="", flush=True)
        except Exception:
            pass
        time.sleep(0.5)


if __name__ == "__main__":
    print("microagent terminal (ctrl-c to quit)")
    print("---")
    threading.Thread(target=watch_outbox, daemon=True).start()
    while True:
        try:
            text = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text.strip():
            continue
        send(text.strip())
