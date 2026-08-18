"""Step 4.5: the harness's own handle dir — `run/agent/` looks like any other
handle dir. A raw line on `in` wakes the agent, `log` mirrors traffic, and
messages addressed to `agent` land on `out`.
"""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from agents.base_agent import BaseAgent
from lib.message import Batch, Message
from providers.ping import Ping

from helpers import write_raw


class Recorder(BaseAgent):
    """Harness that records each wake's batch instead of calling a provider."""

    coalesce_s = 0.05

    def __init__(self) -> None:
        super().__init__(Ping("t-agent", {}), [])
        self.batches: list[Batch] = []
        self.woke = asyncio.Event()

    async def wake(self, batch: Batch) -> None:
        self.batches.append(batch)
        self.woke.set()


@pytest.fixture
async def agent():
    a = Recorder()
    task = asyncio.create_task(a.run())
    for _ in range(100):
        # A non-blocking open for write succeeds once run() holds the reader.
        try:
            os.close(os.open(a.handle.in_path, os.O_WRONLY | os.O_NONBLOCK))
            break
        except OSError:
            await asyncio.sleep(0.01)
    yield a
    task.cancel()


async def test_poke_wakes_and_logs(agent):
    write_raw(agent.handle, "in", b"hi\n")
    await asyncio.wait_for(agent.woke.wait(), 5)
    assert agent.batches[0][0] == Message("agent", "", "hi")
    assert agent.handle.read_log()[0]["body"] == "hi", "poke mirrored to log"


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


async def test_last_channel_falls_back_to_agent(agent):
    batch = Batch([Message("agent", "", "poke")])
    channel = agent.last_channel(batch)
    assert channel == "agent"
    agent.send(Message(channel, batch.reply_to(channel), "orphan text"))
    assert agent.handle.read_log()[-1]["body"] == "orphan text"
