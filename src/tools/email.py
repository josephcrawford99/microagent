"""Email handle: email.poll / email.send / email.emit_pending.

`send` picks up `subject` automatically — its schema is derived from
`EmailMessage`'s dataclass fields.
"""

from __future__ import annotations

from lib.tools import ToolContext, ToolSpec, interface_tools


def build(ctx: ToolContext) -> list[ToolSpec]:
    return interface_tools(ctx, "email")
