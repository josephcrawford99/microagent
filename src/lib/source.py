"""Base Source + the canonical Message/Trigger dataclasses.

A Source is a wake-input the agent reads from. Each Source owns whatever
background monitoring it needs (threads, asyncio tasks, IMAP IDLE, HTTP
long-polls, file watchers) and pushes a `Trigger(interface=self)` onto a
shared `asyncio.Queue` the moment new work lands. The main loop awaits
that queue — zero-CPU idle, sub-ms wake latency for in-process signals.

`lib.interface.Interface` extends Source with `send()` for channels that
are bidirectional (socket, email, telegram, web_chat). Receive-only inputs
(imessage, calendars, file feeds) subclass Source directly.

A Source has no tool surface of its own — it is plumbing. What the agent is
allowed to *do* with it lives in `src/tools/` (see `lib.tools`), which is what
lets one agent get `telegram.send` and another not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from lib.paths import CONFIG_ENV
from lib.settings import RootConfig

log = logging.getLogger(__name__)

__all__ = ["InputSettings", "Message", "Source", "Trigger"]


class InputSettings(BaseSettings):
    """Base for every Source/Interface settings model. Constructible from a
    parent RootConfig — `SocketSettings(settings)` extracts the
    `[<KIND>.<SECTION>]` slice and feeds it to BaseSettings as init kwargs;
    pydantic-settings' env+dotenv sources fill in any `validation_alias`
    fields (credentials)."""

    model_config = SettingsConfigDict(
        env_file=str(CONFIG_ENV),
        case_sensitive=True,
        extra="allow",
    )

    KIND: ClassVar[str]                            # "interfaces" | "sources"
    SECTION: ClassVar[str]                         # "socket"
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = ()

    enabled: bool = False

    def __init__(self, parent: Optional[RootConfig] = None, /, **kwargs: Any) -> None:
        if isinstance(parent, RootConfig):
            cls = type(self)
            section = getattr(parent, cls.KIND, {}).get(cls.SECTION, {}) or {}
            super().__init__(**{**section, **kwargs})
        else:
            super().__init__(**kwargs)


@dataclass
class Message:
    """Canonical message payload. Subclasses add extra fields via @dataclass.

    `sender` is harness-populated and therefore omitted from the send-tool
    schema by default. Subclasses can omit more fields by setting TOOL_OMIT.
    """

    body: str = ""
    to: str = ""
    sender: str = ""

    TOOL_OMIT: ClassVar[tuple[str, ...]] = ()


@dataclass
class Trigger:
    """Wake signal. The `interface` field points at the Source that fired
    the trigger (it's named `interface` for historical reasons; any Source
    subclass, send-capable or not, can populate it)."""

    interface: "Source"


class Source:
    """Base for wake-capable inputs. Subclasses set `name`, implement
    `receive()`, and override `start()` to launch background monitoring
    that calls `self._signal()` whenever there's new work for the agent.

    Subclass `__init__(agent_id, settings)` should `super().__init__(...)`
    then build its typed cfg via `MyPluginSettings(settings)` and read
    fields off it — `cfg.host`, `cfg.password.get_secret_value()`, etc.
    """

    name: str
    message_class: type[Message] = Message
    # Pydantic settings model for this Input. The dashboard introspects it
    # to discover ui-tagged fields (whitelists, etc.) and render editors.
    settings_cls: ClassVar[Optional[type[InputSettings]]] = None
    # Interfaces inherit Source and must always wake; source subclasses
    # override from their settings (defaulting False = agent-polled only).
    wake_on_event: bool = True

    def __init__(self, agent_id: str, settings: "RootConfig") -> None:
        self.agent_id = agent_id
        self.settings = settings
        self._trigger_q: Optional[asyncio.Queue[Trigger]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self, trigger_q: asyncio.Queue[Trigger]) -> None:
        """Called once at boot on the running event loop. Stores the queue
        and loop handle; subclasses should call super() and then kick off
        whatever background monitoring they need (asyncio tasks, threads).
        Default: passive — no monitoring, never signals."""
        self._trigger_q = trigger_q
        self._loop = asyncio.get_running_loop()

    def _signal(self) -> None:
        """Thread-safe: enqueue a Trigger for this Source. Safe to call
        from any thread — hops back to the event loop via
        call_soon_threadsafe before touching the asyncio.Queue. No-op when
        wake_on_event is False (passive source — agent reads via tool)."""
        if not self.wake_on_event:
            log.debug("%s: wake_on_event=False, dropping signal", self.name)
            return
        loop = self._loop
        q = self._trigger_q
        if loop is None or q is None:
            return  # start() hasn't run yet; silently drop
        loop.call_soon_threadsafe(q.put_nowait, Trigger(interface=self))

    async def receive(self) -> list[Message]:
        raise NotImplementedError

    async def indicate_pending(self, note: str) -> None:
        """Optional live-status hook. Default no-op; Sources with a
        'typing…' affordance (telegram, web_chat) override."""
        del note

    async def indicate_idle(self) -> None:
        """Optional hook called once when a wake ends so Sources can tear
        down any transient status posted by indicate_pending."""
