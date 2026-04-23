#!/usr/bin/env python3
"""Microagent daemon — event-driven wake loop.

Each Source/Interface owns a background task/thread that pushes a Trigger
onto a shared asyncio.Queue the moment it has work for the agent. The main
loop awaits that queue — zero-CPU idle, sub-ms wake latency for in-process
signals. Bursts get coalesced + deduped into a single wake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import shutil
from pathlib import Path

from dotenv import load_dotenv

# /config/.env is the canonical secrets file; load before any Settings
# instantiation so the values are available as environment variables.
load_dotenv("/config/.env", override=True)

from agent_types import AGENT_TYPES
from interfaces.email import Email
from interfaces.socket import Socket
from interfaces.telegram import Telegram
from interfaces.web_chat import WebChat
from lib.agent import AgentType
from lib.interface import Interface
from lib.settings import CONFIG_DIR, CONFIG_TOML, SOUL_MD, Settings
from lib.source import Source, Trigger
from sources.imessage import IMessage
from dashboard import DashboardServer

EXAMPLES_DIR = Path(__file__).parent / "examples"


def seed_config_if_missing() -> None:
    """First-boot: copy example config/soul into /config/ if they aren't there.
    .env is never seeded — secrets must be placed intentionally."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_TOML.exists():
        src = EXAMPLES_DIR / "config.example.toml"
        if src.exists():
            shutil.copy(src, CONFIG_TOML)
            logging.getLogger("microagent").info(
                "seeded %s from example", CONFIG_TOML
            )
    if not SOUL_MD.exists():
        src = EXAMPLES_DIR / "soul.example.md"
        if src.exists():
            shutil.copy(src, SOUL_MD)
            logging.getLogger("microagent").info(
                "seeded %s from example", SOUL_MD
            )


def ensure_js_workspace() -> None:
    """Seed /space/js as a persistent Node workspace so the agent can `npm
    install` without wrecking the image or losing deps across rebuilds."""
    js_dir = Path("/space/js")
    js_dir.mkdir(parents=True, exist_ok=True)
    pkg = js_dir / "package.json"
    if not pkg.exists():
        with pkg.open("w") as f:
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


def build_interfaces(settings: Settings) -> list[Interface]:
    """Instantiate every enabled send-capable interface."""
    agent_id = settings.agent_id
    out: list[Interface] = []
    ic = settings.interfaces

    if ic.socket.enabled:
        out.append(Socket(agent_id, ic.socket))
    if ic.email.enabled:
        out.append(Email(
            agent_id,
            ic.email,
            password=settings.email_password.get_secret_value(),
        ))
    if ic.telegram.enabled:
        out.append(Telegram(
            agent_id,
            ic.telegram,
            token=settings.telegram_bot_token.get_secret_value(),
        ))
    if ic.web_chat.enabled:
        out.append(WebChat(agent_id, ic.web_chat))
    return out


def build_sources(settings: Settings) -> list[Source]:
    """Instantiate every enabled receive-only source."""
    agent_id = settings.agent_id
    out: list[Source] = []
    sc = settings.sources

    if sc.imessage.enabled:
        out.append(IMessage(agent_id, sc.imessage))
    return out


def build_agent(settings: Settings, inputs: list[Source]) -> AgentType:
    name = settings.agent_type
    if name not in AGENT_TYPES:
        raise RuntimeError(
            f"unknown agent type '{name}', available: {list(AGENT_TYPES)}"
        )
    return AGENT_TYPES[name](settings.agent_id, settings, inputs)


def setup_logging() -> logging.Logger:
    Path("/state").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                "/state/agent.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )
    return logging.getLogger("microagent")


async def main() -> None:
    log = setup_logging()
    seed_config_if_missing()
    ensure_js_workspace()

    settings = Settings()
    (Path("/state") / settings.agent_id).mkdir(parents=True, exist_ok=True)

    interfaces = build_interfaces(settings)
    sources = build_sources(settings)
    inputs: list[Source] = [*interfaces, *sources]
    agent = build_agent(settings, inputs)

    if settings.dashboard.enabled:
        web_chat = next((i for i in interfaces if i.name == "web_chat"), None)
        dashboard = DashboardServer(
            settings=settings, agent=agent, web_chat=web_chat
        )
        dashboard.start()

    trigger_q: asyncio.Queue[Trigger] = asyncio.Queue()
    for inp in inputs:
        await inp.start(trigger_q)

    log.info(
        "microagent up | agent=%s (id=%s) interfaces=%s sources=%s dashboard=%s",
        agent.name,
        settings.agent_id,
        [i.name for i in interfaces],
        [s.name for s in sources],
        settings.dashboard.enabled,
    )

    while True:
        try:
            first = await trigger_q.get()
            triggers = [first]
            # Coalesce any bursts that arrived while we were waking.
            while not trigger_q.empty():
                try:
                    triggers.append(trigger_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            # Dedupe: a chatty source firing 5x still yields one Trigger.
            seen: set[int] = set()
            deduped: list[Trigger] = []
            for t in triggers:
                k = id(t.interface)
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(t)
            log.info(
                "waking on %s",
                ", ".join(t.interface.name for t in deduped),
            )
            await agent.wake(deduped)
        except Exception:
            log.exception("error in main loop")


if __name__ == "__main__":
    asyncio.run(main())
