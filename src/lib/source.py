"""Base Source + the canonical Message/Trigger dataclasses.

A Source is a wake-input the agent reads from. Each Source owns whatever
background monitoring it needs (threads, asyncio tasks, IMAP IDLE, HTTP
long-polls, file watchers) and pushes a `Trigger(interface=self)` onto a
shared `asyncio.Queue` the moment new work lands. The main loop awaits
that queue — zero-CPU idle, sub-ms wake latency for in-process signals.

`lib.interface.Interface` extends Source with `send()` for channels that
are bidirectional (socket, email, telegram, web_chat). Receive-only inputs
(imessage, calendars, file feeds) subclass Source directly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Optional

from claude_agent_sdk import SdkMcpTool, tool

log = logging.getLogger("microagent.source")

ToolArgs = dict[str, Any]
ToolResult = dict[str, Any]


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
    """

    name: str
    message_class: type[Message] = Message
    required_env: ClassVar[list[str]] = []
    # Interfaces inherit Source and must always wake; source subclasses
    # override from their settings (defaulting False = agent-polled only).
    wake_on_event: bool = True

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
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

    def tools(self) -> list[SdkMcpTool[Any]]:
        """Receive-only tool surface. `Interface` extends this to add send."""
        iface_name = self.name
        receive_fn = self.receive

        @tool(
            f"{iface_name}_receive",
            f"Receive any pending messages from the {iface_name} interface. "
            f"Consumes them — they will not be returned again.",
            {},
        )
        async def receive_tool(args: ToolArgs) -> ToolResult:
            del args
            try:
                messages = await receive_fn()
            except Exception as e:
                log.exception("%s_receive failed", iface_name)
                return _error(f"receive failed: {e}")
            payload = [asdict(m) for m in messages]
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        return [receive_tool]


# --- helpers ---------------------------------------------------------------


_OMIT_ALWAYS = ("sender",)


def _derive_schema(msg_cls: type[Message]) -> dict[str, type]:
    """Build the MCP tool schema from the dataclass fields.

    The SDK's @tool takes a flat {name: type} mapping for input_schema. We
    map each dataclass field to its annotation type, except fields in
    TOOL_OMIT or the always-omitted set (currently just `sender`, which the
    harness fills in from its own knowledge of who's talking)."""
    omit = set(_OMIT_ALWAYS) | set(msg_cls.TOOL_OMIT)
    out: dict[str, type] = {}
    for f in dataclasses.fields(msg_cls):
        if f.name in omit:
            continue
        out[f.name] = f.type if isinstance(f.type, type) else str
    return out


def _error(msg: str) -> ToolResult:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}
