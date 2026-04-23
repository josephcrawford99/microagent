"""Smoke-test agent. No LLM — iterates triggers and replies 'pong' to 'ping'.

Builds the reply via the interface's own `message_class` (so it works with
EmailMessage etc.) and swaps `to` / `sender` so the reply routes back to
the original sender on any interface."""

from __future__ import annotations

import logging

from lib.agent import AgentType

log = logging.getLogger("microagent.ping")


class Ping(AgentType):
    name = "ping"

    async def on_wake(self, triggers):
        log.info(
            "ping woke on %d trigger(s): %s",
            len(triggers),
            [t.interface.name for t in triggers],
        )
        for t in triggers:
            iface = t.interface
            for m in await iface.receive():
                body = (m.body or "").strip().lower()
                log.info("%s <- %s: %r", iface.name, m.sender or "?", body)
                if "ping" not in body:
                    continue
                reply = iface.message_class(
                    body="pong",
                    to=m.sender,
                    sender=m.to,
                )
                status = await iface.send(reply)
                log.info("%s -> %s: pong (%s)", iface.name, m.sender or "?", status)
