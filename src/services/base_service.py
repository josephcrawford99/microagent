"""Base Service: a task that owns one handle directory.

Subclasses set `name`, implement `handle_in()` (delivery) and whatever
background monitoring feeds `write_out()`. A channel that can show the agent
working also implements `handle_status()`, which gives it a `status` handle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import cached_property
from typing import Any, Coroutine

from lib.handle import Handle
from lib.message import Message
from lib.state import ComponentState

log = logging.getLogger(__name__)


class Service:
    """A handle which the agent interact with"""
    name: str
    default_wake: bool = True  # whether lines on `out` wake the agent
    # False for command channels (cron): free text on `in` is a parse error,
    # so the harness must never notify unaddressed text or errors here
    conversational: bool = True
    # Env vars the service needs; filled into `secrets`, and the dashboard's
    # missing-cred hints.
    REQUIRED_ENV: tuple[str, ...] = ()

    ADDRESS_HINT: str | None = None    # `address` placeholder; None = no addressing
    BODY_HINT: str = "<text to send>"  # `body` placeholder (cron: a grammar)

    # Pacing for the base poll loop; only read when `poll()` is overridden.
    # The subclass sets the default, config.toml overrides it per install.
    poll_interval_s: float = 0.0  # 0 = the fetch itself blocks (long-poll)
    error_backoff_s: float = 5.0

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        if not getattr(self, "name", None):
            raise TypeError(f"{type(self).__name__} must set `name`")
        self.agent_id = agent_id
        self.config = config
        self.handle = Handle(
            self.name, extra=("status",) if self.shows_status else ()
        )
        self.poll_interval_s = float(
            config.get("poll_interval_s", type(self).poll_interval_s)
        )
        self.secrets = {k: os.environ.get(k, "") for k in self.REQUIRED_ENV}
        self._tasks: set[asyncio.Task[None]] = set()
        self._latest_status: Message | None = None
        self._status_waiting = asyncio.Event()

    @cached_property
    def state(self) -> ComponentState:
        """This plugin's state file under state/<agent_id>/."""
        return ComponentState(self.agent_id, self.name)

    def message_schema(self) -> dict[str, str]:
        """One example message object for the wake prompt. Field order is the
        order the model should write them in."""
        return {
            "channel": self.name,
            **({"address": self.ADDRESS_HINT} if self.ADDRESS_HINT else {}),
            "body": self.BODY_HINT,
        }

    @property
    def wake(self) -> bool:
        return bool(self.config.get("wake", self.default_wake))

    @property
    def shows_status(self) -> bool:
        """True when the subclass implements `handle_status()`, which is what
        gives this channel a `status` handle to read."""
        return type(self).handle_status is not Service.handle_status

    async def start(self, *, poll: bool = True) -> None:
        """Start reading `in`, and `status` for a channel that shows one (the
        handle dir itself exists since __init__), and run the poll loop when
        the subclass overrides `poll()` and no required env var is missing.
        `poll=False` skips the loop."""
        self.handle.add_reader("in", self._on_in)
        if self.shows_status:
            self.handle.add_reader("status", self._on_status)
            self.spawn(self._status_loop())
        if not poll or type(self).poll is Service.poll:
            return
        if missing := [k for k, v in self.secrets.items() if not v]:
            log.warning("%s: %s not set; not polling", self.name, ", ".join(missing))
            return
        self.spawn(self._poll_loop())

    def boot(self, at: str) -> None:
        """The process just respun and the handle tree is readable again; `at`
        is the local timestamp of that boot. Called once, after `start()`, and
        late enough that `write_out` reaches the harness. Override to react;
        most services have nothing to say about a restart."""

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run a background coroutine, holding a strong reference so the
        event loop can't garbage-collect it mid-flight."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def poll(self) -> None:
        """One background fetch: read the outside world, `write_out` what
        arrived. The base loop owns pacing and retry; leave unimplemented
        for services with nothing to poll."""

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception:
                log.exception("%s: poll failed", self.name)
                await asyncio.sleep(self.error_backoff_s)
                continue
            await asyncio.sleep(self.poll_interval_s)

    def _on_in(self, msg: Message) -> None:
        """One line off `in`, delivered on its own task so a slow send never
        blocks the reader."""
        self.spawn(self._dispatch(msg))

    async def _dispatch(self, msg: Message) -> None:
        try:
            await self.handle_in(msg)
        except Exception as e:
            log.exception("%s: handle_in failed", self.name)
            # Surface delivery failures on `out` so the model learns next wake.
            self.write_out(sender=self.name, body=f"error: {type(e).__name__}: {e}")

    def _on_status(self, msg: Message) -> None:
        """Keep only the newest line off `status`. An indicator that falls
        behind the agent should catch up to now, not replay a backlog."""
        self._latest_status = msg
        self._status_waiting.set()

    async def _status_loop(self) -> None:
        """Render status lines one at a time. A failure here is logged and
        dropped, never written to `out`: an indicator that could not be drawn
        must not wake the agent to say so."""
        while True:
            await self._status_waiting.wait()
            self._status_waiting.clear()
            msg, self._latest_status = self._latest_status, None
            if msg is None:
                continue
            try:
                await self.handle_status(msg)
            except Exception:
                log.exception("%s: handle_status failed", self.name)

    async def handle_in(self, msg: Message) -> None:
        """Deliver one outbound message. Whether the channel accepts it (an
        address it needs, a body it can parse) is this method's call."""
        raise NotImplementedError

    async def handle_status(self, msg: Message) -> None:
        """Show that the agent is working on `address`'s message, the `body`
        being what it is doing right now. An empty body means the work is over
        and whatever was shown should be taken down. Leave unimplemented for a
        channel with nowhere to put it."""

    def write_out(self, *, sender: str, body: str) -> None:
        """Publish one inbound event on `out`, mirrored to the log."""
        self.handle.write("out", Message(self.name, sender, body))
