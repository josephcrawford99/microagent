"""A general purpose default harness loop to run out of the box"""

from __future__ import annotations

import json
import logging

from agents.base_agent import BaseAgent
from lib.message import Batch, Message, parse_reply
from providers.base_provider import Provider
from services.base_service import Service

log = logging.getLogger(__name__)

CONTRACT = """
You are woken with batches of messages from named channels. Reply with ONLY a
JSON array of messages, one object per message you want to send. Reply with []
if nothing needs sending. Angle brackets below mark values you fill in; fields
not shown for a channel are ignored.

Available channels and the envelope each accepts:
"""


class Agent(BaseAgent):
    def __init__(self, provider: Provider, services: list[Service]) -> None:
        super().__init__(provider, services)
        self.contract = CONTRACT + "\n".join(
            "  " + json.dumps(s.message_schema()) for s in services
        )

    async def wake(self, batch: Batch) -> None:
        """One wake of the agent"""
        log.info("waking on %s", ", ".join(batch.channels))
        preamble = self.soul() + self.contract  # before the provider call: no soul, no wake
        messages = "\n".join(["New messages:", "", *(m.prompt_line() for m in batch)])
        prompt = preamble + "\n\n" + messages
        log.debug("%s prompt:\n%s", self.provider.name, prompt)
        raw = await self.provider.generate(prompt)
        log.debug("%s response:\n%s", self.provider.name, raw)
        for msg in parse_reply(raw):
            try:
                if not msg.channel:
                    # model sent back bad json
                    channel = self.last_channel(batch)
                    self.send(Message(channel, batch.reply_to(channel), msg.body.strip()))
                else:
                    self.send(msg)
            except Exception as e:
                log.warning("dropping message %r: %s", msg, e)
