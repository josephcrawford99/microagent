import json
import os
import time


def make_message(channel, sender, recipient, body, subject=None, thread=None, extra=None):
    ts = int(time.time() * 1000)
    msg = {
        "id": str(ts),
        "channel": channel,
        "from": sender,
        "to": recipient,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "body": body,
    }
    if subject:
        msg["subject"] = subject
    if thread:
        msg["thread"] = thread
    if extra:
        msg.update(extra)
    return msg


def write_message(directory, message):
    os.makedirs(directory, exist_ok=True)
    filename = f"{message['id']}.json"
    path = os.path.join(directory, filename)
    with open(path, "w") as f:
        json.dump(message, f, indent=2)
    return path


def read_message(path):
    with open(path) as f:
        return json.load(f)


def list_messages(directory):
    if not os.path.isdir(directory):
        return []
    files = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
    return [os.path.join(directory, f) for f in files]
