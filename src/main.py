#!/usr/bin/env python3
"""Microagent daemon — long-running poll loop.

Polls each interface every POLL_INTERVAL seconds. When any interface returns a
Trigger, wakes the agent with all active triggers and lets it act via the
interface's MCP tools.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_types import AGENT_TYPES  # noqa: E402
from interfaces import INTERFACES  # noqa: E402
from lib.config import DATA_DIR, load_config  # noqa: E402

POLL_INTERVAL = 3  # seconds


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

    interfaces = []
    for name, conf in config.get("interfaces", {}).items():
        if not conf.get("enabled"):
            continue
        if name not in INTERFACES:
            raise RuntimeError(
                f"unknown interface '{name}', available: {list(INTERFACES)}"
            )
        interfaces.append(INTERFACES[name](conf))

    agent_name = config.get("agent_type", "ping")
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
