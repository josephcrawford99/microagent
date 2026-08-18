#!/usr/bin/env python3
"""Microagent daemon.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run from anywhere

from dotenv import load_dotenv  # noqa: E402
from agents.default import Agent  # noqa: E402
from lib import paths  # noqa: E402
from lib.log import setup_logging  # noqa: E402
from lib.plugins import load_provider, load_service  # noqa: E402
from lib.settings import Config  # noqa: E402

log = logging.getLogger(__name__)


async def main() -> None:
    paths.ensure_home()
    load_dotenv(paths.CONFIG_ENV, override=True)
    setup_logging()
    config = Config()

    if not config.provider:
        raise RuntimeError("[agents.<id>] is missing `provider` in config.toml")
    provider = load_provider(config.provider)(config.agent_id, config.agent)

    services = [
        load_service(name)(config.agent_id, cfg)
        for name, cfg in config.enabled_services().items()
    ]
    for svc in services:
        await svc.start()

    if config.dashboard_enabled:
        from dashboard import DashboardServer  # optional component

        DashboardServer(config).start()

    log.info(
        "microagent up | provider=%s (agent=%s) services=%s dashboard=%s",
        provider.name, config.agent_id, [s.name for s in services],
        config.dashboard_enabled,
    )
    await Agent(provider, services).run()


if __name__ == "__main__":
    asyncio.run(main())
