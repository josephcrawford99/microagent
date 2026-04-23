"""Base Interface and the canonical Message/Trigger dataclasses.

Interfaces are the agent's channels to the outside world — one Interface
instance per enabled channel (socket, email, telegram, …). The Interface
base auto-generates MCP tools `{name}_receive` and `{name}_send` from the
interface's message class, so subclasses only need to implement the three
I/O methods.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Optional

from claude_agent_sdk import SdkMcpTool, tool

log = logging.getLogger("microagent.interface")

ToolArgs = dict[str, Any]
ToolResult = dict[str, Any]


@dataclass
class Message:
    """Canonical message payload. Subclasses add extra fields via @dataclass.

    `sender` is harness-populated (we fill it from the interface's own idea of
    who sent the message) and therefore omitted from the send-tool schema by
    default. Subclasses can omit more fields by setting TOOL_OMIT.
    """

    body: str = ""
    to: str = ""
    sender: str = ""

    # Fields to exclude from the auto-derived send-tool schema. `sender` is
    # always excluded at the Interface level; subclasses add their own.
    TOOL_OMIT: ClassVar[tuple[str, ...]] = ()


@dataclass
class Trigger:
    """Wake signal. Subclasses can add fields if the interface wants to carry
    wake-specific metadata; agents that don't care just see the interface ref."""

    interface: "Interface"


class Interface:
    """Base for communication interfaces.

    Subclasses set `name`, optionally set `message_class`, and take a typed
    settings slice + `agent_id` in their __init__. The default tools() pair
    (`{name}_receive`, `{name}_send`) is auto-generated from the message
    class's dataclass fields — no hand-written SCHEMA needed.
    """

    name: str
    message_class: type[Message] = Message

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def trigger_wake(self) -> Optional[Trigger]:
        raise NotImplementedError

    async def receive(self) -> list[Message]:
        raise NotImplementedError

    async def send(self, message: Message) -> str:
        del message
        raise NotImplementedError

    async def indicate_pending(self, note: str) -> None:
        """Optional live-status hook. Default no-op; interfaces with a 'typing…'
        affordance (telegram, web chat) override to surface current activity."""
        del note

    async def indicate_idle(self) -> None:
        """Optional hook called once when a wake ends so interfaces can tear
        down any transient status posted by indicate_pending."""

    def tools(self) -> list[SdkMcpTool[Any]]:
        iface_name = self.name
        msg_cls = self.message_class
        receive_fn = self.receive
        send_fn = self.send
        schema = _derive_schema(msg_cls)

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

        @tool(
            f"{iface_name}_send",
            f"Send a message via the {iface_name} interface.",
            schema,
        )
        async def send_tool(args: ToolArgs) -> ToolResult:
            try:
                status = await send_fn(msg_cls(**args))
            except Exception as e:
                log.exception("%s_send failed", iface_name)
                return _error(f"send failed: {e}")
            return {"content": [{"type": "text", "text": status or "sent"}]}

        return [receive_tool, send_tool]


# --- helpers ---------------------------------------------------------------


_OMIT_ALWAYS = ("sender",)


def _derive_schema(msg_cls: type[Message]) -> dict[str, type]:
    """Build the MCP tool schema from the dataclass fields.

    The SDK's @tool takes a flat {name: type} mapping for input_schema. We map
    each dataclass field to its annotation type, except fields in TOOL_OMIT or
    the always-omitted set (currently just `sender`, which the harness fills
    in from its own knowledge of who's talking)."""
    omit = set(_OMIT_ALWAYS) | set(msg_cls.TOOL_OMIT)
    out: dict[str, type] = {}
    for f in dataclasses.fields(msg_cls):
        if f.name in omit:
            continue
        out[f.name] = f.type if isinstance(f.type, type) else str
    return out


def _error(msg: str) -> ToolResult:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}
