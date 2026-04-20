#!/usr/bin/env python3
"""Microagent daemon — long-running poll loop.

Polls each interface every POLL_INTERVAL seconds. When any interface returns a
Trigger, wakes the agent with all active triggers and lets it act via the
interface's MCP tools.
"""

import asyncio
import logging
import os

from agent_types import AGENT_TYPES
from interfaces import INTERFACES
from lib.config import DATA_DIR, load_config

POLL_INTERVAL = 3  # seconds


def load_interfaces(config):
    interfaces = []
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


async def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(DATA_DIR, "agent.log")),
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
