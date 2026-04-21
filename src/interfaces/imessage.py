import json
import logging
import os
import sqlite3
from typing import Any, Optional

from claude_agent_sdk import SdkMcpTool

from lib.interface import Interface, Message, Trigger

log = logging.getLogger("microagent.imessage")


class IMessage(Interface):
    """iMessage receive-only feed via host's chat.db (read-only bind-mount).

    No send path — sending is delegated to other channels. The agent uses this
    interface purely to observe messages you receive during the day.

    `allowed_senders` is optional: empty list = ingest every inbound message;
    non-empty list filters to those handles at trigger time so unknown senders
    don't cost a wake.
    """

    name = "imessage"

    def __init__(
        self,
        db_path: str = "/data/chat.db",
        state_path: str = "/data/imessage_state.json",
        allowed_senders: Optional[list[str]] = None,
    ) -> None:
        self.db_path = db_path
        self.state_path = state_path
        self.allowed_senders = [s.lower() for s in (allowed_senders or [])]
        # Seed last_seen to current max ROWID so we don't flood on first boot
        # with years of backlog. If state file exists, keep it.
        if not os.path.exists(self.state_path):
            self._save_last_seen(self._current_max_rowid())

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
        # Only expose the receive tool — no outbound path for this interface.
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
                    f"WHERE m.ROWID > ? AND m.is_from_me = 0 "
                    f"AND m.text IS NOT NULL AND m.text != '' "
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
            with open(self.state_path) as f:
                return int(json.load(f).get("last_seen", 0))
        except FileNotFoundError:
            return 0
        except Exception:
            log.exception("corrupt imessage state; resetting to 0")
            return 0

    def _save_last_seen(self, rowid: int) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_seen": int(rowid)}, f)
        os.rename(tmp, self.state_path)
