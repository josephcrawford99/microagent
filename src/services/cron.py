"""Agent-schedulable wakes, served over handles.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)

STALE_CUTOFF_HOURS = 24
IDLE_SLEEP_S = 3600
TIME_OF_DAY_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
USAGE = "usage: in <seconds> <reason> | at <HH:MM|ISO> <reason> | daily <HH:MM> <reason> | cancel <id>"


@dataclass(frozen=True)
class Schedule:
    """One pending fire. `time_of_day` is set for dailies only."""

    id: str
    kind: str  # "once" | "daily"
    fires_at: datetime
    reason: str
    created_at: str
    time_of_day: str | None = None

    @classmethod
    def new(
        cls, kind: str, fires_at: datetime, reason: str, time_of_day: str | None = None
    ) -> Schedule:
        return cls(
            f"c-{uuid.uuid4().hex[:8]}", kind, fires_at, reason,
            datetime.now().isoformat(timespec="seconds"), time_of_day,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Schedule:
        return cls(
            d["id"], d["kind"], datetime.fromisoformat(d["fires_at"]),
            d.get("reason", ""), d.get("created_at", ""), d.get("time_of_day"),
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "kind": self.kind,
            "fires_at": self.fires_at.isoformat(timespec="seconds"),
            "reason": self.reason,
            "created_at": self.created_at,
        }
        if self.time_of_day:
            out["time_of_day"] = self.time_of_day
        return out


class Cron(Service):
    name = "cron"
    conversational = False  # `in` takes grammar, not prose — no notices here
    BODY_HINT = USAGE.removeprefix("usage: ")  # same grammar the parser enforces

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.max_active = int(config.get("max_active", 8))
        self.min_delay_seconds = int(config.get("min_delay_seconds", 60))
        self.max_fires_per_day = int(config.get("max_fires_per_day", 24))
        self._schedules: list[Schedule] = []
        self._kick = asyncio.Event()

    async def start(self, *, poll: bool = True) -> None:
        await super().start(poll=poll)
        self._schedules = [
            Schedule.from_dict(d) for d in self.state.load().get("schedules", [])
        ]
        self._catchup_missed()
        self._persist()
        self.spawn(self._run_loop())

    # --- the grammar over `in` ---

    async def handle_in(self, msg: Message) -> None:
        """Parse one request line. ValueErrors propagate — the Service base
        writes them as error lines on `out`."""
        line = msg.body.strip()
        verb, _, rest = line.partition(" ")
        rest = rest.strip()
        if verb == "cancel":
            if not self._cancel(rest):
                raise ValueError(f"no pending schedule with id {rest!r}")
            log.info("cron: cancelled %s", rest)
            return
        arg, _, reason = rest.partition(" ")
        if verb == "in":
            if not arg.lstrip("-").isdigit():
                raise ValueError(USAGE)
            fires_at, time_of_day = datetime.now() + timedelta(seconds=int(arg)), None
        elif verb == "at":
            fires_at, time_of_day = _parse_at(arg), None
        elif verb == "daily":
            if not TIME_OF_DAY_RE.match(arg):
                raise ValueError("daily takes HH:MM (00:00-23:59)")
            fires_at, time_of_day = _next_daily(arg, datetime.now()), arg
        else:
            raise ValueError(USAGE)
        delta = round((fires_at - datetime.now()).total_seconds())
        if delta < self.min_delay_seconds:
            raise ValueError(
                f"target is {delta}s away, below "
                f"min_delay_seconds={self.min_delay_seconds}"
            )
        sid = self._add(Schedule.new(
            "daily" if time_of_day else "once", fires_at, reason, time_of_day,
        ))
        log.info("cron: scheduled %s (%s)", sid, line)

    # --- firing loop ---

    async def _run_loop(self) -> None:
        """Sleep until the next scheduled fire (or indefinitely if none), then
        fire anything due. The kick event short-circuits the sleep when a
        schedule is added/cancelled, so new entries take effect immediately."""
        while True:
            sleep_s = self._seconds_until_next_fire()
            timeout = sleep_s if sleep_s is not None else IDLE_SLEEP_S
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            try:
                self._fire_due()
            except Exception:
                log.exception("cron _fire_due failed")

    def _catchup_missed(self) -> None:
        """On boot: write out any overdue schedules, advance dailies to their
        next future occurrence, drop one-shots too stale to still matter."""
        now = datetime.now()
        stale_cutoff = now - timedelta(hours=STALE_CUTOFF_HOURS)
        remaining: list[Schedule] = []
        fired = 0
        for s in self._schedules:
            if s.fires_at > now:
                remaining.append(s)
                continue
            if s.kind == "once" and s.fires_at < stale_cutoff:
                log.info("cron: discarding stale one-shot %s", s.id)
                continue
            self._write_fire(s)
            fired += 1
            if s.kind == "daily" and s.time_of_day:
                remaining.append(replace(s, fires_at=_next_daily(s.time_of_day, now)))
        self._schedules = remaining
        if fired:
            log.info("cron: catchup fired %d overdue schedule(s)", fired)

    def _seconds_until_next_fire(self) -> float | None:
        if not self._schedules:
            return None
        next_at = min(s.fires_at for s in self._schedules)
        return max(0.0, (next_at - datetime.now()).total_seconds())

    def _fire_due(self) -> None:
        now = datetime.now()
        remaining: list[Schedule] = []
        fired = 0
        for s in self._schedules:
            if s.fires_at > now:
                remaining.append(s)
                continue
            self._write_fire(s)
            fired += 1
            if s.kind == "daily" and s.time_of_day:
                remaining.append(replace(s, fires_at=_next_daily(s.time_of_day, now)))
        if fired:
            self._schedules = remaining
            self._persist()
            log.info("cron fired %d schedule(s)", fired)

    def _write_fire(self, s: Schedule) -> None:
        self.write_out(
            sender=s.id, body=f"[cron {s.kind}] {s.reason or '(no reason)'}"
        )

    # --- schedule table ---

    def _add(self, new: Schedule) -> str:
        if len(self._schedules) >= self.max_active:
            raise ValueError(
                f"max_active={self.max_active} pending schedules reached; "
                f"cancel one before scheduling another"
            )
        projected = _count_projected_fires(self._schedules + [new])
        if projected > self.max_fires_per_day:
            raise ValueError(
                f"this schedule would project {projected} fires in the next 24h, "
                f"above max_fires_per_day={self.max_fires_per_day}"
            )
        self._schedules.append(new)
        self._persist()
        self._kick.set()
        return new.id

    def _cancel(self, sid: str) -> bool:
        kept = [s for s in self._schedules if s.id != sid]
        if len(kept) == len(self._schedules):
            return False
        self._schedules = kept
        self._persist()
        self._kick.set()
        return True

    def _persist(self) -> None:
        """One write for both records: the state file, and cron/schedules —
        the current table as a cat-able status file, one line per entry."""
        self.state.save({"schedules": [s.to_dict() for s in self._schedules]})
        rows = [
            f"{s.id}  {s.kind:<5}  {s.fires_at.isoformat(timespec='seconds')}  {s.reason}"
            for s in self._schedules
        ]
        self.handle.snapshot("schedules", "\n".join(rows) + "\n" if rows else "")


def _parse_at(raw: str) -> datetime:
    """Accept either HH:MM (next future occurrence) or a full ISO datetime."""
    if TIME_OF_DAY_RE.match(raw):
        return _next_daily(raw, datetime.now())
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"'at' takes HH:MM or an ISO datetime, got {raw!r}")


def _next_daily(time_of_day: str, now: datetime) -> datetime:
    hh, mm = time_of_day.split(":")
    candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _count_projected_fires(schedules: list[Schedule]) -> int:
    """Count scheduled fires falling in the next 24h. Each daily schedule
    always contributes 1 (next occurrence is ≤24h away by construction)."""
    horizon = datetime.now() + timedelta(days=1)
    return sum(1 for s in schedules if s.fires_at <= horizon)
