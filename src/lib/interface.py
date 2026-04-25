"""Interface: a Source that can also send.

Channels that are bidirectional (socket, email, telegram, web_chat)
subclass this. Receive-only feeds (imessage, calendars, file watchers)
subclass `lib.source.Source` directly.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from lib.source import (
    Message,
    Source,
    ToolArgs,
    ToolResult,
    Trigger,
    _derive_schema,
    _error,
)

log = logging.getLogger(__name__)


class Interface(Source):
    """A Source that also has an outbound path. Subclasses implement
    `send()`; the MCP tools gain a `{name}_send` in addition to the
    inherited `{name}_receive`."""

    async def send(self, message: Message) -> str:
        del message
        raise NotImplementedError

    def tools(self) -> list[SdkMcpTool[Any]]:
        iface_name = self.name
        msg_cls = self.message_class
        send_fn = self.send
        schema = _derive_schema(msg_cls)

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

        return [*super().tools(), send_tool]


# Re-exports so `from lib.interface import Interface, Message, Trigger`
# (the pattern every existing interface file uses) keeps working.
__all__ = ["Interface", "Message", "Trigger"]
