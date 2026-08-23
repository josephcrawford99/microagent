"""Step 4: the Service contract at the FIFO boundary — an envelope written to
`run/<name>/in` is delivered, and what a service sees becomes a line on
`run/<name>/out` plus the log. Uses a tiny echo subclass for the base
contract, then the real web_chat and cron services.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from lib.state import ComponentState
from services.base_service import Service
from services.cron import Cron
from services.web_chat import WebChat
from lib.message import Message

from helpers import next_line, write_in, write_raw


class Echo(Service):
    name = "echo_test"

    async def handle_in(self, msg):
        self.write_out(sender="echo", body=msg.body)


class Boom(Service):
    name = "boom_test"

    async def handle_in(self, msg):
        raise ValueError("nope")


class Shower(Service):
    """A service that can show the agent working, and remembers what it saw."""

    name = "shower_test"

    def __init__(self, agent_id, config):
        super().__init__(agent_id, config)
        self.shown: list[str | None] = []
        self.drawn = asyncio.Event()
        self.hold = asyncio.Event()
        self.hold.set()

    async def handle_in(self, msg):
        self.write_out(sender="shower", body=msg.body)

    async def handle_status(self, msg):
        await self.hold.wait()
        self.shown.append(msg.status)
        self.drawn.set()


class Ticker(Service):
    """A polling service, to check how the base loop is paced."""

    name = "ticker_test"
    poll_interval_s = 30.0

    async def handle_in(self, msg):
        pass

    async def poll(self):
        pass


def test_shows_status_follows_the_override():
    assert Shower("t-show", {}).shows_status is True
    assert Echo("t-echo", {}).shows_status is False, "no handle_status, nothing to show"


async def test_status_reaches_a_channel_that_shows_it(harness):
    svc = Shower("t-show", {})
    await harness(svc)

    assert write_in(svc.handle, {"address": "42", "status": "Bash: git log"}) is True
    await asyncio.wait_for(svc.drawn.wait(), 5)
    assert svc.shown == ["Bash: git log"]


async def test_status_is_not_a_delivery_and_is_not_logged(harness):
    """An `in` line is mirrored to the log; a status is what the agent is doing
    this second, so it is neither delivered nor kept."""
    svc = Echo("t-echo", {})
    q = await harness(svc)

    assert write_in(svc.handle, {"address": "", "status": "Bash: git log"}) is True
    assert write_in(svc.handle, {"address": "", "body": "hi"}) is True
    line = await next_line(q)
    assert line.body == "hi", "the status was dropped, the message got through"
    assert [e.get("body") for e in svc.handle.read_log()] == ["hi", "hi"]


async def test_a_burst_of_status_coalesces_to_the_newest(harness):
    """An indicator that falls behind should catch up to now, not replay."""
    svc = Shower("t-show", {})
    await harness(svc)
    svc.hold.clear()

    for text in ("first", "second", "third"):
        write_in(svc.handle, {"address": "42", "status": text})
    await asyncio.sleep(0.05)
    svc.hold.set()
    await asyncio.wait_for(svc.drawn.wait(), 5)
    assert svc.shown == ["third"]


def test_poll_interval_defaults_to_the_subclass_and_config_overrides():
    assert Ticker("t-tick", {}).poll_interval_s == 30.0
    assert Ticker("t-tick", {"poll_interval_s": 0.5}).poll_interval_s == 0.5
    assert Echo("t-echo", {}).poll_interval_s == 0.0, "base default for non-pollers"


async def test_in_to_out_round_trip(harness):
    svc = Echo("t-echo", {})
    q = await harness(svc)

    assert write_in(svc.handle, {"address": "", "body": "hi"}) is True
    line = await next_line(q)
    assert line.address == "echo"
    assert line.body == "hi"

    log = svc.handle.read_log()
    assert {"body": "hi"}.items() <= log[0].items(), "raw in-line logged first"
    assert log[-1]["from"] == "echo", "out envelope logged too"
    assert log[-1]["ts"], "write stamps a timestamp"


async def test_raw_text_line_becomes_body(harness):
    svc = Echo("t-echo-raw", {})
    q = await harness(svc)
    write_raw(svc.handle, "in", b"plain text line\n")
    line = await next_line(q)
    assert line.body == "plain text line"


async def test_handler_error_surfaces_on_out(harness):
    svc = Boom("t-boom", {})
    q = await harness(svc)
    write_in(svc.handle, {"body": "anything"})
    line = await next_line(q)
    assert line.address == "boom_test"
    assert line.body == "error: ValueError: nope"


async def test_web_chat_round_trip(harness):
    svc = WebChat("t-web", {})
    q = await harness(svc)

    # The dashboard plays the remote user: a line on `out` reaches the harness.
    svc.handle.write("out", Message("web_chat", "web", "hello agent"))
    line = await next_line(q)
    assert (line.address, line.body) == ("web", "hello agent")

    # Agent reply on `in`: delivery is the log append; no error may surface.
    write_in(svc.handle, {"address": "", "body": "hello user"})
    await asyncio.sleep(0.2)
    assert q.empty(), "web_chat delivery must not write anything out"
    assert any(m.get("body") == "hello user" for m in svc.handle.read_log())


def _cron(agent_id: str, **overrides) -> Cron:
    config = {"min_delay_seconds": 0, "max_active": 8, "max_fires_per_day": 24}
    config.update(overrides)
    return Cron(agent_id, config)


def _agent_id() -> str:
    return f"t-cron-{uuid.uuid4().hex[:6]}"


async def test_cron_one_shot_fires_and_clears(harness):
    svc = _cron(_agent_id())
    q = await harness(svc)

    write_in(svc.handle, {"body": "in 0 water plants"})
    line = await next_line(q)
    assert line.address.startswith("c-")
    assert line.body == "[cron once] water plants"

    await asyncio.sleep(0.2)
    assert (svc.handle.path / "schedules").read_text() == "", "fired one-shot cleared"


async def test_cron_daily_schedule_and_cancel(harness):
    svc = _cron(_agent_id())
    q = await harness(svc)

    tod = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
    write_in(svc.handle, {"body": f"daily {tod} standup"})
    await asyncio.sleep(0.3)
    snapshot = (svc.handle.path / "schedules").read_text()
    assert "daily" in snapshot and "standup" in snapshot

    sid = snapshot.split()[0]
    write_in(svc.handle, {"body": f"cancel {sid}"})
    await asyncio.sleep(0.3)
    assert (svc.handle.path / "schedules").read_text() == ""
    assert q.empty(), "scheduling and cancelling write nothing out"


async def test_cron_grammar_errors_surface_on_out(harness):
    svc = _cron(_agent_id(), min_delay_seconds=60)
    q = await harness(svc)

    cases = {
        "frobnicate now": "usage:",
        "in abc x": "usage:",
        "daily 25:00 x": "daily takes HH:MM",
        "in 5 too soon": "below min_delay_seconds=60",
        "cancel c-doesnotexist": "no pending schedule",
    }
    for body, expected in cases.items():
        write_in(svc.handle, {"body": body})
        line = await next_line(q)
        assert line.body.startswith("error: ValueError:"), (body, line)
        assert expected in line.body, (body, line)


async def test_cron_enforces_limits(harness):
    svc = _cron(_agent_id(), max_active=2)
    q = await harness(svc)

    for i, hours in enumerate((2, 3)):
        tod = (datetime.now() + timedelta(hours=hours)).strftime("%H:%M")
        write_in(svc.handle, {"body": f"daily {tod} job{i}"})
    tod = (datetime.now() + timedelta(hours=4)).strftime("%H:%M")
    write_in(svc.handle, {"body": f"daily {tod} onetoomany"})
    line = await next_line(q)
    assert "max_active=2" in line.body

    strict = _cron(_agent_id(), max_fires_per_day=1)
    q2 = await harness(strict)
    for i, hours in enumerate((2, 3)):
        tod = (datetime.now() + timedelta(hours=hours)).strftime("%H:%M")
        write_in(strict.handle, {"body": f"daily {tod} fire{i}"})
    line = await next_line(q2)
    assert "max_fires_per_day=1" in line.body


async def test_cron_catchup_fires_overdue_on_restart(harness):
    agent_id = _agent_id()
    reason = f"late-{agent_id}"
    ComponentState(agent_id, "cron").save({"schedules": [{
        "id": "c-overdue1",
        "kind": "once",
        "fires_at": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"),
        "reason": reason,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }]})

    svc = _cron(agent_id)
    await harness(svc)  # catchup emits during start(), before any out-reader: check the log
    assert any(
        m.get("body") == f"[cron once] {reason}" for m in svc.handle.read_log()
    )
    assert ComponentState(agent_id, "cron").load()["schedules"] == []
