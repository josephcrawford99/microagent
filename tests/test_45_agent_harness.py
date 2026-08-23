"""Step 4.5: the harness's own handle dir — `run/agent/` looks like any other
handle dir. A raw line on `in` wakes the agent, `log` mirrors traffic, and
messages addressed to `agent` land on `out`. Booting is one more line on `in`.
"""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import datetime

import pytest

from agents.base_agent import BaseAgent
from lib.message import Batch, Message
from providers.ping import Ping
from services.base_service import Service

from helpers import write_raw


class Listener(Service):
    """A service that does nothing but remember it was told about the boot."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__("t-agent", {})
        self.booted_at = ""
        self.delivered: list[str] = []
        self.shown: list[tuple[str, str | None]] = []
        self.drew = asyncio.Event()
        self.cleared = asyncio.Event()

    def boot(self, at: str) -> None:
        self.booted_at = at


class Watcher(Listener):
    """A channel that can show the agent working, and keeps what it was told."""

    async def handle_in(self, msg):
        self.delivered.append(msg.body)

    async def handle_status(self, msg):
        self.shown.append((msg.address, msg.status))
        (self.cleared if not msg.status else self.drew).set()


class Deaf(Listener):
    """A service whose boot notice fails."""

    def boot(self, at: str) -> None:
        raise RuntimeError("no")


class Recorder(BaseAgent):
    """Harness that records each wake's batch instead of calling a provider."""

    coalesce_s = 0.05

    def __init__(self, services: list[Service] | None = None) -> None:
        super().__init__(Ping("t-agent", {}), services or [])
        self.batches: list[Batch] = []
        self.woke = asyncio.Event()

    async def wake(self, batch: Batch) -> None:
        self.batches.append(batch)
        self.woke.set()


class Failing(Recorder):
    """A harness that stays in its wake until the channel has drawn the
    receipt, then blows up. A wake that returns sooner than the channel can
    draw shows nothing at all, which is the point of coalescing."""

    async def wake(self, batch: Batch) -> None:
        await super().wake(batch)
        for channel in batch.channels:
            if (svc := self.get_service(channel)) is not None:
                await asyncio.wait_for(svc.drew.wait(), 5)
        raise RuntimeError("boom")


@pytest.fixture
async def agent():
    """A running harness with its boot wake already spent, so a test sees only
    the lines it writes itself."""
    a = Recorder()
    task = asyncio.create_task(a.run())
    await asyncio.wait_for(a.woke.wait(), 5)
    a.woke.clear()
    a.batches.clear()
    yield a
    task.cancel()


@pytest.fixture
async def booting():
    """A harness caught at its boot: nothing drained, services attached."""
    services = [Listener("l-one"), Deaf("l-deaf"), Listener("l-two")]
    a = Recorder(services)
    task = asyncio.create_task(a.run())
    yield a, services
    task.cancel()


async def test_boot_wakes_the_agent_with_when(booting):
    a, _ = booting
    await asyncio.wait_for(a.woke.wait(), 5)
    msg = a.batches[0][0]
    assert msg.channel == "agent" and msg.address == "boot"
    datetime.fromisoformat(msg.body.removeprefix("restarted at "))
    assert a.handle.read_log()[-1]["address"] == "boot", "the restart is on record"


async def test_every_service_hears_the_boot(booting):
    """A deaf service costs the others nothing, and the agent nothing — the
    restart happened either way."""
    a, services = booting
    await asyncio.wait_for(a.woke.wait(), 5)
    at = a.batches[0][0].body.removeprefix("restarted at ")
    assert [s.booted_at for s in services] == [at, "", at]


async def test_poke_wakes_and_logs(agent):
    write_raw(agent.handle, "in", b"hi\n")
    await asyncio.wait_for(agent.woke.wait(), 5)
    assert agent.batches[0][0] == Message("agent", "", "hi")
    assert agent.handle.read_log()[-1]["body"] == "hi", "poke mirrored to log"


async def test_dir_is_service_shaped(agent):
    assert stat.S_ISFIFO(os.lstat(agent.handle.in_path).st_mode)
    assert stat.S_ISFIFO(os.lstat(agent.handle.out_path).st_mode)


async def test_send_to_agent_lands_on_own_dir(agent):
    agent.send(Message("agent", "", "note to self"))
    assert agent.handle.read_log()[-1]["body"] == "note to self"


async def test_unknown_channel(agent):
    assert agent.get_service("nope") is None
    with pytest.raises(LookupError):
        agent.send(Message("nope", "", "x"))


async def test_status_bookends_a_wake_on_the_channel_it_came_from():
    """A read receipt before the provider, a clear after, whatever happened."""
    watcher = Watcher("w-one")
    await watcher.start()
    a = Failing([watcher])
    task = asyncio.create_task(a.run())
    try:
        await asyncio.wait_for(a.woke.wait(), 5)  # the boot wake, spent
        watcher.write_out(sender="caller", body="you there?")
        await asyncio.wait_for(watcher.cleared.wait(), 5)
    finally:
        task.cancel()

    assert watcher.shown[0] == ("caller", BaseAgent.OPENING_STATUS)
    assert watcher.shown[-1] == ("caller", ""), "cleared even though the wake raised"
    assert watcher.delivered == ["Wake failed: boom"], "a notice is still a message"


async def test_last_channel_falls_back_to_agent(agent):
    batch = Batch([Message("agent", "", "poke")])
    channel = agent.last_channel(batch)
    assert channel == "agent"
    agent.send(Message(channel, batch.reply_to(channel), "orphan text"))
    assert agent.handle.read_log()[-1]["body"] == "orphan text"
