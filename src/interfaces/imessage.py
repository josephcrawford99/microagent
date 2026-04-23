"""iMessage receive-only feed via host's chat.db (read-only bind-mount at
/mnt/imessage/chat.db by default).

No send path — outbound is delegated to other channels. `allowed_senders`
filters at trigger time so unknown senders don't cost a wake. Watermark
(last observed ROWID) persists via ComponentState; first boot seeds it
to current max ROWID to avoid flooding the agent with historical messages.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from claude_agent_sdk import SdkMcpTool

from lib.interface import Interface, Message, Trigger
from lib.settings import IMessageSettings
from lib.state import ComponentState

log = logging.getLogger("microagent.imessage")


class IMessage(Interface):
    name = "imessage"

    def __init__(self, agent_id: str, settings: IMessageSettings) -> None:
        super().__init__(agent_id)
        self.db_path = settings.db_path
        self.allowed_senders = [s.lower() for s in settings.allowed_senders]
        self._state = ComponentState(agent_id, self.name)
        # First-boot: seed watermark to current max ROWID so we don't flood.
        self._state.load_or_init(lambda: {"last_seen": self._current_max_rowid()})

    def trigger_wake(self) -> Optional[Trigger]:
        try:
            count = self._count_new_from_allowed()
        except Exception:
            log.exception("trigger_wake: chat.db read failed")
            return None
        if count == 0:
            return None
        return Trigger(interface=self)

    async def receive(self) -> list[Message]:
        last_seen = self._load_last_seen()
        rows = self._fetch_new(last_seen)
        out: list[Message] = []
        max_rowid = last_seen
        for rowid, sender, text in rows:
            max_rowid = max(max_rowid, rowid)
            sender_lc = (sender or "").lower()
            if self.allowed_senders and sender_lc not in self.allowed_senders:
                continue
            out.append(Message(body=text, sender=sender_lc, to="me"))
        if max_rowid > last_seen:
            self._save_last_seen(max_rowid)
        return out

    def tools(self) -> list[SdkMcpTool[Any]]:
        # Receive-only — no outbound tool.
        receive_tool, _ = super().tools()
        return [receive_tool]

    # --- helpers ---

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0
        )

    def _count_new_from_allowed(self) -> int:
        last_seen = self._load_last_seen()
        with self._connect() as conn:
            if self.allowed_senders:
                placeholders = ",".join("?" * len(self.allowed_senders))
                sql = (
                    "SELECT COUNT(*) FROM message m "
                    "JOIN handle h ON m.handle_id = h.ROWID "
                    "WHERE m.ROWID > ? AND m.is_from_me = 0 "
                    "AND m.text IS NOT NULL AND m.text != '' "
                    f"AND LOWER(h.id) IN ({placeholders})"
                )
                cur = conn.execute(sql, [last_seen, *self.allowed_senders])
            else:
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
