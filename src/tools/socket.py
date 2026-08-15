"""Socket handle: socket.poll / socket.send / socket.emit_pending."""

from __future__ import annotations

from lib.tools import ToolContext, ToolSpec, interface_tools


def build(ctx: ToolContext) -> list[ToolSpec]:
    return interface_tools(ctx, "socket")
