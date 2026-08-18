"""iMessage receive-only feed via the host's chat.db (~/Library/Messages by
default; in docker, the read-only bind-mount at /mnt/imessage/chat.db).
Sends are rejected in handle_in — outbound goes via another channel. wake
defaults false"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)


class IMessage(Service):
    name = "imessage"
    default_wake = False
    BODY_HINT = "<cannot send: receive-only channel>"
    poll_interval_s = 15

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        default_db = Path("~/Library/Messages/chat.db").expanduser()
        self.db_path = str(config.get("db_path", default_db))

    async def start(self, *, poll: bool = True) -> None:
        # First-boot: seed watermark to current max ROWID so we don't flood.
        if not self.state.load():
            self.state.save({"last_seen": self._current_max_rowid()})
        await super().start(poll=poll)

    async def handle_in(self, msg: Message) -> None:
        raise ValueError("imessage is receive-only; reply on another channel")

    async def poll(self) -> None:
        last_seen = self.state.get_int("last_seen", 0)
        rows = await asyncio.to_thread(self._fetch_new, last_seen)
        max_rowid = last_seen
        for rowid, sender, text in rows:
            max_rowid = max(max_rowid, rowid)
            self.write_out(sender=(sender or "").lower(), body=text)
        if max_rowid > last_seen:
            self.state.save({"last_seen": max_rowid})

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0)

    def _fetch_new(self, last_seen: int) -> list[tuple[int, str, str]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT m.ROWID, h.id, m.text FROM message m "
                "JOIN handle h ON m.handle_id = h.ROWID "
                "WHERE m.ROWID > ? AND m.is_from_me = 0 "
                "AND m.text IS NOT NULL AND m.text != '' "
                "ORDER BY m.ROWID ASC",
                [last_seen],
            )
            return [(int(r[0]), r[1], r[2]) for r in cur.fetchall()]

    def _current_max_rowid(self) -> int:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message")
                (rowid,) = cur.fetchone()
                return int(rowid)
        except Exception:
            log.exception("could not read initial max ROWID; starting from 0")
            return 0
