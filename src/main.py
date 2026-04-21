#!/usr/bin/env python3
"""Microagent daemon — long-running poll loop.

Polls each interface every POLL_INTERVAL seconds. When any interface returns a
Trigger, wakes the agent with all active triggers and lets it act via the
interface's MCP tools.
"""

import asyncio
import json
import logging
import logging.handlers
import os
from typing import Any

from dotenv import load_dotenv

# Python owns .env — docker-compose doesn't pass secrets through, so a
# restart after `!env` picks up new values directly from the file.
load_dotenv("/repo/.env")

from agent_types import AGENT_TYPES
from interfaces import INTERFACES
from lib.config import DATA_DIR, load_config
from lib.interface import Interface

POLL_INTERVAL = 3  # seconds


def load_interfaces(config: dict[str, Any]) -> list[Interface]:
    interfaces: list[Interface] = []
    for name, conf in config.get("interfaces", {}).items():
        kwargs = dict(conf)
        if not kwargs.pop("enabled", False):
            continue
        if name not in INTERFACES:
            raise RuntimeError(
                f"unknown interface '{name}', available: {list(INTERFACES)}"
            )
        interfaces.append(INTERFACES[name](**kwargs))
    return interfaces


def _ensure_js_workspace() -> None:
    """Seed /data/js as a persistent Node workspace so the agent can `npm
    install` without wrecking the image or losing deps across rebuilds.
    Idempotent: only writes package.json if it doesn't already exist."""
    js_dir = os.path.join(DATA_DIR, "js")
    os.makedirs(js_dir, exist_ok=True)
    pkg = os.path.join(js_dir, "package.json")
    if not os.path.exists(pkg):
        with open(pkg, "w") as f:
            json.dump(
                {
                    "name": "microagent-js",
                    "version": "0.0.0",
                    "private": True,
                    "description": "Agent scratch workspace. Safe to `npm install` here; survives restarts.",
                },
                f,
                indent=2,
            )
            f.write("\n")


async def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    _ensure_js_workspace()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                os.path.join(DATA_DIR, "agent.log"),
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )
    log = logging.getLogger("microagent")

    config = load_config()
    interfaces = load_interfaces(config)

    agent_name = config.get("agent_type")
    if not agent_name:
        raise RuntimeError("config missing 'agent_type'")
    if agent_name not in AGENT_TYPES:
        raise RuntimeError(
            f"unknown agent type '{agent_name}', available: {list(AGENT_TYPES)}"
        )
    agent = AGENT_TYPES[agent_name](interfaces=interfaces)

    log.info(
        "microagent up | agent=%s interfaces=%s",
        agent.name,
        [i.name for i in interfaces],
    )

    while True:
        try:
            triggers = [
                t for t in (i.trigger_wake() for i in interfaces) if t is not None
            ]
            if triggers:
                log.info(
                    "waking on %s",
                    ", ".join(t.interface.name for t in triggers),
                )
                await agent.wake(triggers)
        except Exception:
            log.exception("error in main loop")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
