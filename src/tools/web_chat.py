"""Web chat handle: web_chat.poll / web_chat.send / web_chat.emit_pending."""

from __future__ import annotations

from lib.tools import ToolContext, ToolSpec, interface_tools


def build(ctx: ToolContext) -> list[ToolSpec]:
    return interface_tools(ctx, "web_chat")
