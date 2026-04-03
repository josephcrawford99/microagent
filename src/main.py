#!/usr/bin/env python3
"""Microagent — lightweight personal assistant daemon.

Wakes on schedule or inbox activity. Processes messages and exits.
The entrypoint (or cron/inotifyd) is responsible for re-triggering.
"""

import fcntl
import logging
import os
import sys

from agent_types import AGENT_TYPES
from interfaces import INTERFACES
from lib.config import load_config, load_soul_prompt, DATA_DIR
from lib.messages import read_message, list_messages, make_message, write_message
from lib.sessions import get_session_id

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "agent.log")),
    ],
)
log = logging.getLogger("microagent")

LOCK_PATH = os.path.join(DATA_DIR, ".lock")

config = {}
agent = None
interfaces = []


def acquire_lock():
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except OSError:
        log.info("another instance is running, exiting")
        lock_fd.close()
        return None


def load_agent():
    name = config.get("agent_type", "ping")
    if name not in AGENT_TYPES:
        raise RuntimeError(f"unknown agent type '{name}', available: {list(AGENT_TYPES)}")
    log.info("loaded agent: %s", name)
    return AGENT_TYPES[name](config, load_soul_prompt(), DATA_DIR)


def load_interfaces():
    result = []
    for name, iface_conf in config.get("interfaces", {}).items():
        if not iface_conf.get("enabled", False):
            continue
        if name not in INTERFACES:
            raise RuntimeError(f"unknown interface '{name}', available: {list(INTERFACES)}")
        result.append(INTERFACES[name](iface_conf, DATA_DIR))
        log.info("loaded interface: %s", name)
    return result


def collect_inbox():
    for iface in interfaces:
        try:
            iface.poll()
        except Exception:
            log.exception("error polling %s", iface.name)
    messages = []
    for iface in interfaces:
        for path in list_messages(iface.inbox_dir):
            msg = read_message(path)
            msg["_source_interface"] = iface.name
            msg["_source_path"] = path
            messages.append(msg)
    messages.sort(key=lambda m: m.get("id", "0"))
    return messages


def send_response(response_text, source_msg):
    iface_name = source_msg.get("_source_interface")
    iface = next((i for i in interfaces if i.name == iface_name), None)
    if not iface:
        log.warning("no interface %s, dropping response", iface_name)
        return
    reply = make_message(
        channel=iface_name,
        sender="agent",
        recipient=source_msg.get("from", ""),
        body=response_text,
        subject=f"Re: {source_msg.get('subject', '')}",
        thread=source_msg.get("thread"),
    )
    iface.send(write_message(iface.outbox_dir, reply))


def process_messages(messages):
    if not messages:
        return
    thread = messages[0].get("thread", f"default_{int(time.time())}")
    ttl = config.get("session_ttl", "daily")
    session_id = get_session_id(DATA_DIR, thread, ttl=ttl)
    response = agent.wake(messages, session_id=session_id)
    if response:
        send_response(response, messages[0])
    for msg in messages:
        path = msg.get("_source_path")
        if path and os.path.exists(path):
            os.remove(path)


def run():
    global config, agent, interfaces

    lock_fd = acquire_lock()
    if not lock_fd:
        return

    try:
        config = load_config()
        agent = load_agent()
        interfaces = load_interfaces()

        log.info("microagent waking | agent=%s interfaces=%s",
                 config.get("agent_type"), [i.name for i in interfaces])

        messages = collect_inbox()

        if messages:
            process_messages(messages)
        else:
            log.info("no messages, autonomous wake")
            response = agent.wake([], session_id=None)
            if response:
                log.info("autonomous response: %s", response[:200])

        log.info("done, exiting")

    except Exception:
        log.exception("fatal error")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    run()
