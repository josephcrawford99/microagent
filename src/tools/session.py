"""session.session_idle — the agent telling the harness the exchange is over.

Sets `ctx.flags["idle"]`, which the agent type reads after the wake. The Claude
type uses it to decide whether a session is safe to rotate; an agent type with
no notion of sessions can simply ignore it.
"""

from __future__ import annotations

from lib.tools import ToolArgs, ToolContext, ToolResult, ToolSpec, _ok, tool


def build(ctx: ToolContext) -> list[ToolSpec]:
    flags = ctx.flags

    @tool(
        "session_idle",
        "Mark the current conversation as complete. Call this when you have "
        "nothing more to do and aren't expecting an immediate follow-up — the "
        "daemon may then rotate to a fresh session at the next scheduled "
        "rotation time. Don't call it if you just asked a question or are "
        "mid-task; wait for the exchange to settle first.",
        {},
    )
    async def session_idle(args: ToolArgs) -> ToolResult:
        del args
        flags["idle"] = True
        return _ok("marked idle")

    return [session_idle]
