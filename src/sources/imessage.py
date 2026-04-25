"""iMessage receive-only source via host's chat.db (read-only bind-mount
at /mnt/imessage/chat.db by default).

No send path — outbound is delegated to other channels. The agent reads
via the imessage_receive MCP tool; sender filtering (if any) is the agent's
job. Watermark (last observed ROWID) persists via ComponentState; first
boot seeds it to current max ROWID to avoid flooding the agent with
historical messages.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from lib.settings import IMessageSettings
from lib.source import Message, Source
from lib.state import ComponentState

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 15
DRAIN_TIMEOUT_S = 120


class IMessage(Source):
    name = "imessage"
    settings_cls = IMessageSettings

    def __init__(self, agent_id: str, settings: IMessageSettings) -> None:
        super().__init__(agent_id)
        self.wake_on_event = settings.wake_on_event
        self.db_path = settings.db_path
        self._state = ComponentState(agent_id, self.name)
        # First-boot: seed watermark to current max ROWID so we don't flood.
        self._state.load_or_init(lambda: {"last_seen": self._current_max_rowid()})
        self._drain = asyncio.Event()

    async def start(self, trigger_q):
        await super().start(trigger_q)
        asyncio.create_task(self._poll_loop(), name="imessage-poll")

    async def _poll_loop(self) -> None:
        """Poll chat.db at POLL_INTERVAL_S. When wake_on_event: signal on
        new messages and await drain (receive() sets it) before the next
        check — so we don't re-signal the same unread run while the agent
        is still processing. Passive (wake_on_event=False): just keep the
        watermark fresh; agent will pull via imessage_receive on its own."""
        while True:
            try:
                count = await asyncio.to_thread(self._count_new)
            except Exception:
                log.exception("chat.db read failed")
                await asyncio.sleep(POLL_INTERVAL_S)
                continue
            if count > 0 and self.wake_on_event:
                self._drain.clear()
                self._signal()
                try:
                    await asyncio.wait_for(
                        self._drain.wait(), timeout=DRAIN_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    log.warning("imessage drain timeout; re-checking")
            await asyncio.sleep(POLL_INTERVAL_S)

    async def receive(self) -> list[Message]:
        last_seen = self._load_last_seen()
        rows = await asyncio.to_thread(self._fetch_new, last_seen)
        out: list[Message] = []
        max_rowid = last_seen
        for rowid, sender, text in rows:
            max_rowid = max(max_rowid, rowid)
            sender_lc = (sender or "").lower()
            out.append(Message(body=text, sender=sender_lc, to="me"))
        if max_rowid > last_seen:
            self._save_last_seen(max_rowid)
        self._drain.set()
        return out

    # --- helpers ---

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0
        )

    def _count_new(self) -> int:
        last_seen = self._load_last_seen()
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM message "
                "WHERE ROWID > ? AND is_from_me = 0 "
                "AND text IS NOT NULL AND text != ''",
                [last_seen],
            )
            (count,) = cur.fetchone()
            return int(count)

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

    def _load_last_seen(self) -> int:
        try:
            return int(self._state.load().get("last_seen", 0))
        except (TypeError, ValueError):
            return 0

    def _save_last_seen(self, rowid: int) -> None:
        self._state.save({"last_seen": int(rowid)})
