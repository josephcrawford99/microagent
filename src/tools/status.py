"""status.emit_pending — the live "working on it" indicator.

This module owns the implementation (`set_pending`), and agent types that can
detect their own activity call it directly: `agent_types/claude.py` invokes it
when the stream shows a thinking block or a tool use, so the indicator tracks
what the model is actually doing without the model having to narrate.

It's a fast call, not a held one — `indicate_pending` returns as soon as the
channel has been told (for telegram, one `sendChatAction` POST plus a status
message edit). Telegram's `•••` expires after a few seconds on its own, which
is why this is called repeatedly as the work changes rather than once at the
start. The next real `send()` clears the status message; `emit_idle` (driven by
the harness in `on_wake`'s finally) clears it if the wake ends without one.

`src/tools/<channel>.py` also exposes a per-channel `emit_pending`; this one
fans out to every interface that triggered the wake.
"""

from __future__ import annotations

import logging

from lib.tools import ToolArgs, ToolContext, ToolResult, ToolSpec, _error, _ok, tool

log = logging.getLogger(__name__)


async def set_pending(ctx: ToolContext, note: str) -> None:
    """Show `note` on every interface that woke this wake. Never raises — a
    dead indicator must not take down the wake around it."""
    for src in ctx.triggering():
        try:
            await src.indicate_pending(note)
        except Exception:
            log.exception("indicate_pending failed on %s", src.name)


def build(ctx: ToolContext) -> list[ToolSpec]:
    @tool(
        "emit_pending",
        "Show a short live-status note to whoever you're currently talking to "
        "('thinking', 'checking your calendar', 'writing that up') so they "
        "know you're working instead of staring at silence. Call it whenever "
        "what you're doing changes — especially before anything slow. Your "
        "next message clears it.",
        {"note": str},
    )
    async def emit_pending(args: ToolArgs) -> ToolResult:
        note = str(args.get("note", "")).strip()
        if not note:
            return _error("emit_pending: 'note' is required")
        await set_pending(ctx, note)
        return _ok("ok")

    return [emit_pending]
