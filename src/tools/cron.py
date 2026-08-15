"""Self-scheduling handle: cron.wake_in / wake_at / wake_daily / list / cancel.

Thin wrappers over `sources/cron.py`'s scheduling API — this file owns the
descriptions and argument parsing, the Source owns the state and the caps.
Contributes nothing when `[sources.cron]` isn't enabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from lib.tools import (
    ToolArgs,
    ToolContext,
    ToolResult,
    ToolSpec,
    _error,
    _json,
    _ok,
    tool,
)

if TYPE_CHECKING:
    from sources.cron import Cron


def build(ctx: ToolContext) -> list[ToolSpec]:
    src = ctx.source("cron")
    if src is None:
        return []
    cron = cast("Cron", src)

    @tool(
        "wake_in",
        "Schedule a one-shot wake after N seconds from now. Include a short "
        "'reason' so you know why you woke when the wake fires.",
        {"seconds": int, "reason": str},
    )
    async def wake_in(args: ToolArgs) -> ToolResult:
        try:
            seconds = int(args["seconds"])
        except (KeyError, TypeError, ValueError):
            return _error("wake_in: 'seconds' must be an integer")
        reason = str(args.get("reason", "")).strip()
        if not reason:
            return _error("wake_in: 'reason' is required")
        try:
            return _ok(f"scheduled {cron.schedule_in(seconds, reason)}")
        except ValueError as e:
            return _error(f"wake_in: {e}")

    @tool(
        "wake_at",
        "Schedule a one-shot wake at a specific time. 'at' accepts either an "
        "ISO datetime (e.g. '2026-04-25T14:30:00') or HH:MM, in which case the "
        "next future occurrence today or tomorrow is used. Time is "
        "container-local.",
        {"at": str, "reason": str},
    )
    async def wake_at(args: ToolArgs) -> ToolResult:
        at_raw = str(args.get("at", "")).strip()
        reason = str(args.get("reason", "")).strip()
        if not at_raw:
            return _error("wake_at: 'at' is required")
        if not reason:
            return _error("wake_at: 'reason' is required")
        try:
            return _ok(f"scheduled {cron.schedule_at(at_raw, reason)}")
        except ValueError as e:
            return _error(f"wake_at: {e}")

    @tool(
        "wake_daily",
        "Schedule a recurring daily wake at HH:MM (container-local time). "
        "Fires every day at that time until cancelled. Use for routine "
        "check-ins, not one-off reminders.",
        {"time_of_day": str, "reason": str},
    )
    async def wake_daily(args: ToolArgs) -> ToolResult:
        tod = str(args.get("time_of_day", "")).strip()
        reason = str(args.get("reason", "")).strip()
        if not reason:
            return _error("wake_daily: 'reason' is required")
        try:
            return _ok(f"scheduled {cron.schedule_daily(tod, reason)}")
        except ValueError as e:
            return _error(f"wake_daily: {e}")

    @tool(
        "list",
        "List currently pending wake schedules. Returns JSON: array of "
        "{id, kind, fires_at, reason, [time_of_day]}.",
        {},
    )
    async def list_tool(args: ToolArgs) -> ToolResult:
        del args
        return _json(cron.list_schedules())

    @tool(
        "cancel",
        "Cancel a pending wake schedule by id (from the list tool).",
        {"id": str},
    )
    async def cancel_tool(args: ToolArgs) -> ToolResult:
        sid = str(args.get("id", "")).strip()
        if not sid:
            return _error("cancel: 'id' is required")
        if not cron.cancel_schedule(sid):
            return _error(f"cancel: no schedule with id={sid!r}")
        return _ok(f"cancelled {sid}")

    return [wake_in, wake_at, wake_daily, list_tool, cancel_tool]
