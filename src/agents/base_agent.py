"""Base harness: anything that is not prompt policy
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from functools import partial
from typing import TYPE_CHECKING

from lib.handle import Handle
from lib.message import Batch, Message
from lib.paths import (
    CONFIG_ENV, CONFIG_TOML, REPO_DIR, RUN_DIR, SOUL_PATH, SPACE_DIR, STATE_DIR,
)

if TYPE_CHECKING:
    from providers.base_provider import Provider
    from services.base_service import Service

log = logging.getLogger(__name__)

ENVIRONMENT = """
Your filesystem on this host:

  {space}
      Your cwd, and yours. Full read/write, and the only durable space you own
      — notes, todos, pages. Keep context between wakes here.
  {run}
      The handle tree, one directory per channel: appending a JSON line to
      <channel>/in sends a message, <channel>/log is that channel's history.
      Your reply already routes through these; prefer it to writing by hand.
      Wiped on every boot — nothing here survives a restart.
  {config}
  {soul}
  {env}
      User-controlled config; the second is your soul. Read-only by convention.
  {state}
      Harness runtime state. Don't touch.
  {repo}
      The microagent source. Edit it if asked to improve the harness, but an
      update wipes local changes — commit or PR rather than keep edits there.
"""


class BaseAgent(ABC):
    """The wake loop. Owns the handle wiring; a subclass owns the wake.

    The harness holds its own dir, `run/agent/`, shaped like any other handle
    dir: a line on `in` pokes the agent awake (`echo hi > run/agent/in`),
    `out` carries what the agent addresses to `agent`, `log` mirrors both."""

    coalesce_s = 0.5  # how long a wake waits for the rest of a burst

    def __init__(self, provider: Provider, services: list[Service]) -> None:
        self.provider = provider
        self.services = services
        self.handle = Handle("agent")
        self.pending = Batch()
        self.wake_event = asyncio.Event()
        # Fixed for the process: paths resolve at import time.
        self.environment = ENVIRONMENT.format(
            space=SPACE_DIR, run=RUN_DIR, config=CONFIG_TOML, soul=SOUL_PATH,
            env=CONFIG_ENV, state=STATE_DIR, repo=REPO_DIR,
        ) if provider.has_filesystem else ""

    def soul(self) -> str:
        """The system prompt every harness starts from: who the agent is, plus
        where it lives if the provider can act on a filesystem. Re-read every
        wake so soul.md edits land without a restart."""
        try:
            soul = SOUL_PATH.read_text()
        except OSError as e:
            raise RuntimeError(f"can't read soul at {SOUL_PATH}: {e}") from e
        if not soul.strip():
            raise RuntimeError(f"soul at {SOUL_PATH} is empty")
        return soul + "\n" + self.environment

    async def run(self) -> None:
        """Main agent harness loop"""
        self.handle.add_reader("in", partial(self.receive, True), tee=True)
        for svc in self.services:
            svc.handle.add_reader("out", partial(self.receive, svc.wake))
        log.info(
            "agent up | harness=%s provider=%s services=%s",
            type(self).__name__, self.provider.name, [s.name for s in self.services],
        )
        while True:
            await self.wake_event.wait()
            await asyncio.sleep(self.coalesce_s)  # coalesce bursts
            self.wake_event.clear()
            batch, self.pending = self.pending, Batch()
            if not batch:
                continue
            try:
                await self.wake(batch)
            except Exception as e:
                log.exception("wake failed")
                self.notify_all(batch, f"Wake failed: {e}")

    @abstractmethod
    async def wake(self, batch: Batch) -> None:
        """All the agent harness really does is decide what to do on wake"""

    def receive(self, wake: bool, msg: Message) -> None:
        """One inbound message off a channel: buffer it, and wake if that
        channel is one we wake on."""
        self.pending.append(msg)
        if wake:
            self.wake_event.set()

    def get_service(self, channel: str) -> Service | None:
        """The service owning a channel by name, or None if nothing owns it."""
        return next((s for s in self.services if s.name == channel), None)

    def send(self, msg: Message) -> None:
        """Put one message on its channel. `channel` picks it. Messages to
        `agent` land on the harness's own `out` handle. Raises on anything
        the channel won't take; callers log and move on."""
        if msg.channel == "agent":
            self.handle.write("out", msg, tee=True)
            return
        svc = self.get_service(msg.channel)
        if svc is None:
            raise LookupError(f"no such channel: {msg.channel!r}")
        svc.handle.write("in", msg)

    def last_channel(self, batch: Batch) -> str:
        """Where a reply belongs when the agent didn't say"""
        for channel in reversed(batch.channels):
            svc = self.get_service(channel)
            if svc is not None and svc.conversational and svc.wake:
                return channel
        return "agent"

    def notify_all(self, batch: Batch, text: str) -> None:
        """Send raw text to every conversational channel in the batch."""
        text = text.strip()
        for channel in batch.channels:
            svc = self.get_service(channel)
            if svc is None or not svc.conversational:
                continue
            try:
                self.send(Message(channel, batch.reply_to(channel), text))
            except Exception as e:
                log.warning("no notice via %s: %s", channel, e)
