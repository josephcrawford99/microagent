"""Step 4c: the iMessage receive-only feed against a fixture chat.db —
rows appearing in the database come out as envelopes on `out`, and the
first-boot watermark keeps history from flooding.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

import pytest

from lib.message import Message
from services.imessage import IMessage

from helpers import next_line


@pytest.fixture
def chat_db(tmp_path):
    db = tmp_path / "chat.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
        conn.execute(
            "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, "
            "is_from_me INTEGER, handle_id INTEGER)"
        )
        conn.execute("INSERT INTO handle VALUES (1, '+15550001111')")
        conn.execute("INSERT INTO message VALUES (1, 'ancient history', 0, 1)")
    return db


def _insert(db, rowid: int, text: str, is_from_me: int = 0) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO message VALUES (?, ?, ?, 1)", (rowid, text, is_from_me))


async def test_new_rows_emit_but_history_and_own_messages_do_not(harness, chat_db):
    svc = IMessage(
        f"t-im-{uuid.uuid4().hex[:6]}",
        {"db_path": str(chat_db), "poll_interval_s": 0.05},
    )
    q = await harness(svc)

    _insert(chat_db, 2, "sent by me", is_from_me=1)
    _insert(chat_db, 3, "hello from a friend")
    line = await next_line(q)
    assert (line.address, line.body) == ("+15550001111", "hello from a friend")
    await asyncio.sleep(0.2)  # a couple more poll cycles
    assert q.empty(), "pre-existing rows and own messages must not go out"


async def test_receive_only_contract(harness, chat_db):
    svc = IMessage(
        f"t-im-{uuid.uuid4().hex[:6]}",
        {"db_path": str(chat_db), "poll_interval_s": 0.05},
    )
    q = await harness(svc)

    assert svc.handle.in_path.exists(), "every handle dir has an `in` FIFO"
    svc.handle.write("in", Message(channel="imessage", address="", body="x"))
    line = await next_line(q)
    assert line.body.startswith("error: ValueError:")
    assert "receive-only" in line.body
