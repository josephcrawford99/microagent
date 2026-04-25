#!/usr/bin/env python3
"""Microagent daemon — event-driven wake loop.

Each Source/Interface owns a background task/thread that pushes a Trigger
onto a shared asyncio.Queue the moment it has work for the agent. The main
loop awaits that queue
"""
from __future__ import annotations
import asyncio
import logging
from dotenv import load_dotenv
load_dotenv("/config/.env", override=True)

from agent_types import AGENT_TYPES
from lib.agent import AgentType
from lib.log import setup_logging
from lib.settings import Settings, enabled_sources
from lib.source import Trigger
from dashboard import DashboardServer

log = logging.getLogger(__name__)


def get_agent(settings: Settings) -> AgentType:
    """Pick the first [agents.*] entry and build it with its inputs attached.
    Multi-agent is config-only for now — extras in settings.agents are parsed
    but ignored."""
    if not settings.agents:
        raise RuntimeError("no [agents.*] section in config.toml")
    agent_id, agent_cfg = next(iter(settings.agents.items()))
    if agent_cfg.agent_type not in AGENT_TYPES:
        raise RuntimeError(
            f"unknown agent type '{agent_cfg.agent_type}', "
            f"available: {list(AGENT_TYPES)}"
        )
    return AGENT_TYPES[agent_cfg.agent_type](
        agent_id, settings, enabled_sources(settings)
    )


async def main() -> None:
    setup_logging()
    settings = Settings()

    agent = get_agent(settings)
    inputs = agent.interfaces

    if settings.dashboard.enabled:
        DashboardServer(settings=settings, agent=agent).start()

    trigger_q: asyncio.Queue[Trigger] = asyncio.Queue()
    for inp in inputs:
        await inp.start(trigger_q)

    log.info(
        "microagent up | agent=%s (id=%s) inputs=%s dashboard=%s",
        agent.name,
        agent.agent_id,
        [i.name for i in inputs],
        settings.dashboard.enabled,
    )

    while True:
        try:
            triggers = [await trigger_q.get()]
            # Coalesce any bursts that arrived while we were waking.
            while True:
                try:
                    triggers.append(trigger_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            deduped = list({id(t.interface): t for t in triggers}.values())
            log.info(
                "waking on %s",
                ", ".join(t.interface.name for t in deduped),
            )
            await agent.wake(deduped)
        except Exception:
            log.exception("error in main loop")


if __name__ == "__main__":
    asyncio.run(main())
