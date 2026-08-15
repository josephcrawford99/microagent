"""Telegram handle: telegram.poll / telegram.send / telegram.emit_pending."""

from __future__ import annotations

from lib.tools import ToolContext, ToolSpec, interface_tools


def build(ctx: ToolContext) -> list[ToolSpec]:
    return interface_tools(ctx, "telegram")
