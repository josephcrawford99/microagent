"""Agent-schedulable wake source.

Exposes five MCP tools to the agent — cron_wake_in, cron_wake_at,
cron_wake_daily, cron_list, cron_cancel — plus the standard cron_receive
that delivers fired schedules as Messages (body = reason) at wake time.

Hard caps in config (max_active, min_delay_seconds, max_fires_per_day) keep
a runaway agent from scheduling a wake storm. Limits are enforced at
tool-call time and return an is_error result so the model sees the refusal.

State at /state/<agent_id>/cron.json. Daily schedules reschedule themselves
for the next occurrence after firing. One-shots more than 24h past their
fires_at are discarded on boot (stale intent).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, ClassVar

from claude_agent_sdk import SdkMcpTool, tool

from lib.settings import RootConfig
from lib.source import InputSettings, Message, Source, ToolArgs, ToolResult, _error
from lib.state import ComponentState

log = logging.getLogger(__name__)

STALE_CUTOFF_HOURS = 24
IDLE_SLEEP_S = 3600
TIME_OF_DAY_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class CronSettings(InputSettings):
    # Agent-schedulable wake source. Hard caps below bound how much the agent
    # can spend on self-scheduled wakes; see this file's tool implementations.
    KIND: ClassVar[str] = "sources"
    SECTION: ClassVar[str] = "cron"
    wake_on_event: bool = True
    max_active: int = 8
    min_delay_seconds: int = 60
    max_fires_per_day: int = 24


class Cron(Source):
    name = "cron"
    settings_cls = CronSettings

    def __init__(self, agent_id: str, settings: RootConfig) -> None:
        super().__init__(agent_id, settings)
        cfg = CronSettings(settings)
        self._cfg = cfg
        self.wake_on_event = cfg.wake_on_event
        self._state = ComponentState(agent_id, "cron")
        self._wake_sched = asyncio.Event()
        self._lock = asyncio.Lock()
        self._state.load_or_init(_empty_state)

    # --- lifecycle ---

    async def start(self, trigger_q):
        await super().start(trigger_q)
        await self._catchup_missed()
        asyncio.create_task(self._run_loop(), name="cron-loop")

    async def _run_loop(self) -> None:
        """Sleep until the next scheduled fire (or indefinitely if none), then
        fire anything due. The wake event short-circuits the sleep when the
        agent adds/cancels a schedule, so new entries kick in immediately."""
        while True:
            sleep_s = self._seconds_until_next_fire()
            timeout = sleep_s if sleep_s is not None else IDLE_SLEEP_S
            try:
                await asyncio.wait_for(self._wake_sched.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake_sched.clear()
            try:
                await self._fire_due()
            except Exception:
                log.exception("cron _fire_due failed")

    async def _catchup_missed(self) -> None:
        """On boot: coalesce any overdue schedules into a single fire, advance
        daily schedules to their next future occurrence, and drop one-shots
        that are too stale to still be meaningful."""
        async with self._lock:
            state = self._state.load(_empty_state())
            now = datetime.now()
            stale_cutoff = now - timedelta(hours=STALE_CUTOFF_HOURS)
            remaining: list[dict] = []
            to_fire: list[dict] = []
            for s in state["schedules"]:
                fires_at = _parse_iso(s["fires_at"])
                if fires_at > now:
                    remaining.append(s)
                    continue
                if s["kind"] == "once" and fires_at < stale_cutoff:
                    log.info(
                        "cron: discarding stale one-shot %s (fires_at=%s)",
                        s["id"], s["fires_at"],
                    )
                    continue
                to_fire.append({**s, "fired_at": now.isoformat(timespec="seconds")})
                if s["kind"] == "daily":
                    next_at = _next_daily(s["time_of_day"], now)
                    remaining.append({**s, "fires_at": next_at.isoformat(timespec="seconds")})
            state["schedules"] = remaining
            state["fired"].extend(to_fire)
            self._state.save(state)
        if to_fire:
            log.info("cron: catchup fired %d overdue schedule(s)", len(to_fire))
            self._signal()

    # --- firing ---

    def _seconds_until_next_fire(self) -> float | None:
        state = self._state.load(_empty_state())
        if not state["schedules"]:
            return None
        now = datetime.now()
        next_at = min(_parse_iso(s["fires_at"]) for s in state["schedules"])
        return max(0.0, (next_at - now).total_seconds())

    async def _fire_due(self) -> None:
        async with self._lock:
            state = self._state.load(_empty_state())
            now = datetime.now()
            remaining: list[dict] = []
            to_fire: list[dict] = []
            for s in state["schedules"]:
                if _parse_iso(s["fires_at"]) <= now:
                    to_fire.append({**s, "fired_at": now.isoformat(timespec="seconds")})
                    if s["kind"] == "daily":
                        next_at = _next_daily(s["time_of_day"], now)
                        remaining.append({**s, "fires_at": next_at.isoformat(timespec="seconds")})
                else:
                    remaining.append(s)
            if not to_fire:
                return
            state["schedules"] = remaining
            state["fired"].extend(to_fire)
            self._state.save(state)
        log.info("cron fired %d schedule(s)", len(to_fire))
        self._signal()

    # --- receive: fired schedules surfaced to the agent ---

    async def receive(self) -> list[Message]:
        async with self._lock:
            state = self._state.load(_empty_state())
            fired = state.get("fired", [])
            state["fired"] = []
            self._state.save(state)
        out: list[Message] = []
        for s in fired:
            when = s.get("fired_at", s.get("fires_at", ""))
            body = f"[cron {s['kind']} @ {when}] {s.get('reason', '(no reason)')}"
            out.append(Message(body=body, sender="cron", to="agent"))
        return out

    # --- tools ---

    def tools(self) -> list[SdkMcpTool[Any]]:
        return [*super().tools(), *self._writer_tools()]

    def _writer_tools(self) -> list[SdkMcpTool[Any]]:
        cfg = self._cfg
        st = self._state
        wake = self._wake_sched

        def _add(new: dict) -> ToolResult:
            state = st.load(_empty_state())
            active = state["schedules"]
            if len(active) >= cfg.max_active:
                return _error(
                    f"cron: max_active={cfg.max_active} pending schedules reached; "
                    f"cancel one with cron_cancel before scheduling another"
                )
            projected = _count_projected_fires(active + [new])
            if projected > cfg.max_fires_per_day:
                return _error(
                    f"cron: adding this schedule would project {projected} fires in the "
                    f"next 24h, above max_fires_per_day={cfg.max_fires_per_day}"
                )
            state["schedules"].append(new)
            st.save(state)
            wake.set()
            return _ok(f"scheduled {new['id']} for {new['fires_at']}")

        @tool(
            "cron_wake_in",
            "Schedule a one-shot wake after N seconds from now. Include a short "
            "'reason' so you know why you woke when the wake fires.",
            {"seconds": int, "reason": str},
        )
        async def cron_wake_in(args: ToolArgs) -> ToolResult:
            try:
                seconds = int(args["seconds"])
            except (KeyError, TypeError, ValueError):
                return _error("cron_wake_in: 'seconds' must be an integer")
            reason = str(args.get("reason", "")).strip()
            if not reason:
                return _error("cron_wake_in: 'reason' is required")
            if seconds < cfg.min_delay_seconds:
                return _error(
                    f"cron_wake_in: seconds={seconds} is below "
                    f"min_delay_seconds={cfg.min_delay_seconds}"
                )
            fires_at = datetime.now() + timedelta(seconds=seconds)
            return _add(_new_schedule(kind="once", fires_at=fires_at, reason=reason))

        @tool(
            "cron_wake_at",
            "Schedule a one-shot wake at a specific time. 'at' accepts either "
            "an ISO datetime (e.g. '2026-04-25T14:30:00') or HH:MM, in which "
            "case the next future occurrence today or tomorrow is used. Time "
            "is container-local.",
            {"at": str, "reason": str},
        )
        async def cron_wake_at(args: ToolArgs) -> ToolResult:
            at_raw = str(args.get("at", "")).strip()
            reason = str(args.get("reason", "")).strip()
            if not at_raw:
                return _error("cron_wake_at: 'at' is required")
            if not reason:
                return _error("cron_wake_at: 'reason' is required")
            try:
                fires_at = _parse_at(at_raw)
            except ValueError as e:
                return _error(f"cron_wake_at: {e}")
            delta = (fires_at - datetime.now()).total_seconds()
            if delta < cfg.min_delay_seconds:
                return _error(
                    f"cron_wake_at: target is {int(delta)}s away, below "
                    f"min_delay_seconds={cfg.min_delay_seconds}"
                )
            return _add(_new_schedule(kind="once", fires_at=fires_at, reason=reason))

        @tool(
            "cron_wake_daily",
            "Schedule a recurring daily wake at HH:MM (container-local time). "
            "Fires every day at that time until cron_cancel'd. Use for routine "
            "check-ins, not one-off reminders.",
            {"time_of_day": str, "reason": str},
        )
        async def cron_wake_daily(args: ToolArgs) -> ToolResult:
            tod = str(args.get("time_of_day", "")).strip()
            reason = str(args.get("reason", "")).strip()
            if not TIME_OF_DAY_RE.match(tod):
                return _error("cron_wake_daily: 'time_of_day' must be HH:MM (00:00-23:59)")
            if not reason:
                return _error("cron_wake_daily: 'reason' is required")
            fires_at = _next_daily(tod, datetime.now())
            return _add(_new_schedule(
                kind="daily", fires_at=fires_at, reason=reason, time_of_day=tod,
            ))

        @tool(
            "cron_list",
            "List currently pending cron schedules. Returns JSON: array of "
            "{id, kind, fires_at, reason, [time_of_day]}.",
            {},
        )
        async def cron_list_tool(args: ToolArgs) -> ToolResult:
            del args
            state = st.load(_empty_state())
            items = []
            for s in state["schedules"]:
                item = {
                    "id": s["id"],
                    "kind": s["kind"],
                    "fires_at": s["fires_at"],
                    "reason": s.get("reason", ""),
                }
                if s["kind"] == "daily":
                    item["time_of_day"] = s.get("time_of_day", "")
                items.append(item)
            return {"content": [{"type": "text", "text": json.dumps(items)}]}

        @tool(
            "cron_cancel",
            "Cancel a pending cron schedule by id (from cron_list).",
            {"id": str},
        )
        async def cron_cancel_tool(args: ToolArgs) -> ToolResult:
            sid = str(args.get("id", "")).strip()
            if not sid:
                return _error("cron_cancel: 'id' is required")
            state = st.load(_empty_state())
            before = len(state["schedules"])
            state["schedules"] = [s for s in state["schedules"] if s["id"] != sid]
            if len(state["schedules"]) == before:
                return _error(f"cron_cancel: no schedule with id={sid!r}")
            st.save(state)
            wake.set()
            return _ok(f"cancelled {sid}")

        return [cron_wake_in, cron_wake_at, cron_wake_daily, cron_list_tool, cron_cancel_tool]


# --- helpers ---------------------------------------------------------------


def _empty_state() -> dict[str, Any]:
    return {"schedules": [], "fired": []}


def _new_schedule(
    *,
    kind: str,
    fires_at: datetime,
    reason: str,
    time_of_day: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": f"c-{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "fires_at": fires_at.isoformat(timespec="seconds"),
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if time_of_day is not None:
        out["time_of_day"] = time_of_day
    return out


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_at(raw: str) -> datetime:
    """Accept either HH:MM (next future occurrence) or a full ISO datetime."""
    m = TIME_OF_DAY_RE.match(raw)
    if m:
        now = datetime.now()
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"'at' must be HH:MM or ISO datetime, got {raw!r}")


def _next_daily(time_of_day: str, now: datetime) -> datetime:
    m = TIME_OF_DAY_RE.match(time_of_day)
    if not m:
        raise ValueError(f"bad time_of_day: {time_of_day!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _count_projected_fires(schedules: list[dict]) -> int:
    """Count scheduled fires falling in the next 24h. Each daily schedule
    always contributes 1 (next occurrence is always ≤24h away by construction)."""
    horizon = datetime.now() + timedelta(days=1)
    return sum(1 for s in schedules if _parse_iso(s["fires_at"]) <= horizon)


def _ok(msg: str) -> ToolResult:
    return {"content": [{"type": "text", "text": msg}]}


Plugin = Cron
