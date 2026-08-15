"""iMessage handle: imessage.poll only.

iMessage is a read-only feed (`/mnt/imessage` is mounted ro), so
`interface_tools` gives it no `send` — it isn't an Interface.
"""

from __future__ import annotations

from lib.tools import ToolContext, ToolSpec, interface_tools


def build(ctx: ToolContext) -> list[ToolSpec]:
    return interface_tools(ctx, "imessage")
