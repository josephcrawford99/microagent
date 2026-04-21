import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import ClassVar, Optional

from lib.interface import Interface, Message, Trigger

log = logging.getLogger("microagent.imessage")


@dataclass
class IMessageMessage(Message):
    """iMessage payload — no subject field, unlike email."""

    SCHEMA: ClassVar[dict[str, type]] = {"to": str, "body": str}


class IMessage(Interface):
    """iMessage via host's chat.db (read) + file-drop outbox (send).

    Receive: bind-mounted `chat.db` (read-only) is polled for new rows by ROWID.
    Send: drops a JSON file into `outbox_dir`; a host-side launchd script picks
    it up and dispatches via `osascript` → Messages.app. We can't send from
    inside the container because iMessage's auth stack is macOS-only.

    `allowed_senders` mirrors email.py's cost-guard — every wake is a paid
    agent run, so unknown senders are filtered at trigger time, not after.
    """

    name = "imessage"
    message_class = IMessageMessage

    def __init__(
        self,
        db_path: str = "/data/chat.db",
        outbox_dir: str = "/data/imessage-outbox",
        state_path: str = "/data/imessage_state.json",
        allowed_senders: Optional[list[str]] = None,
    ) -> None:
        self.db_path = db_path
        self.outbox_dir = outbox_dir
        self.state_path = state_path
        self.allowed_senders = [s.lower() for s in (allowed_senders or [])]
        os.makedirs(self.outbox_dir, exist_ok=True)
        # Seed last_seen to current max ROWID so we don't flood on first boot
        # with years of backlog. If state file exists, keep it.
        if not os.path.exists(self.state_path):
            self._save_last_seen(self._current_max_rowid())

    # --- lifecycle ---

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
                log.info("ignoring imessage from non-allowed sender: %s", sender)
                continue
            out.append(IMessageMessage(body=text, sender=sender_lc, to="me"))
        if max_rowid > last_seen:
            self._save_last_seen(max_rowid)
        return out

    async def send(self, message: Message) -> str:
        if not message.to:
            raise RuntimeError("imessage send requires `to` to be set")
        payload = {"to": message.to, "body": message.body}
        name = f"{uuid.uuid4().hex}.json"
        final = os.path.join(self.outbox_dir, name)
        tmp = final + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.rename(tmp, final)
        log.info("queued imessage to %s (%s)", message.to, name)
        return f"queued to {message.to}"

    # --- helpers ---

    def _connect(self) -> sqlite3.Connection:
        # Read-only URI so we never risk mutating the host's chat.db.
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
