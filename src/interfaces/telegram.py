"""Telegram bot interface — HTTP-only, no extra deps.

`allowed_chat_ids` is the cost-guard: only whitelisted chats wake the agent.
Empty list denies everything. Watermark (highest consumed update_id + 1)
persists via ComponentState under /state/<agent_id>/telegram.json.

Live status: `indicate_pending` posts/edits a chat message that reflects
what the agent is doing right now ("thinking", "using Read", …). The next
real `send()` deletes it so the user never sees a stale "working…" linger.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from lib.interface import Interface, Message, Trigger
from lib.settings import TelegramSettings
from lib.state import ComponentState

log = logging.getLogger("microagent.telegram")

API_BASE = "https://api.telegram.org"


class Telegram(Interface):
    name = "telegram"

    def __init__(
        self, agent_id: str, settings: TelegramSettings, token: str
    ) -> None:
        super().__init__(agent_id)
        self.token = token
        self.allowed_chat_ids = set(settings.allowed_chat_ids)
        self.poll_timeout = settings.poll_timeout
        self._state = ComponentState(agent_id, self.name)
        self._pending: list[dict[str, Any]] = []
        self._all_fetched: list[dict[str, Any]] = []
        self._active_chats: set[int] = set()
        # chat_id -> (message_id, last_note) for the live status message.
        self._status: dict[int, tuple[int, str]] = {}

    # --- lifecycle ---

    def trigger_wake(self) -> Optional[Trigger]:
        if not self.token:
            return None
        try:
            updates = self._fetch_updates()
        except Exception:
            log.exception("trigger_wake: getUpdates failed")
            return None
        allowed = [u for u in updates if self._is_allowed(u)]
        if not allowed:
            self._advance_watermark(updates)
            return None
        self._pending = allowed
        self._all_fetched = updates
        self._active_chats = {
            (u.get("message") or {}).get("chat", {}).get("id") for u in allowed
        }
        self._active_chats.discard(None)
        # Fresh wake — any stale status msg id is from the previous cycle.
        self._status = {}
        return Trigger(interface=self)

    async def receive(self) -> list[Message]:
        updates = self._pending
        self._pending = []
        out: list[Message] = []
        for u in updates:
            msg = u.get("message") or {}
            text = msg.get("text") or ""
            if not text:
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            out.append(
                Message(
                    body=text,
                    to="me",
                    sender=str(chat_id) if chat_id is not None else "",
                )
            )
        self._advance_watermark(self._all_fetched or updates)
        return out

    async def indicate_pending(self, note: str) -> None:
        if not self.token:
            return
        body = f"_{_escape_md(note)}_"
        for chat_id in self._active_chats:
            try:
                # Always fire typing so the ••• shows instantly too.
                self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                existing = self._status.get(chat_id)
                if existing is None:
                    resp = self._api(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": body,
                            "parse_mode": "MarkdownV2",
                        },
                    )
                    msg_id = (resp.get("result") or {}).get("message_id")
                    if msg_id:
                        self._status[chat_id] = (int(msg_id), note)
                else:
                    msg_id, last_note = existing
                    if last_note == note:
                        continue  # identical edits are rejected by telegram
                    self._api(
                        "editMessageText",
                        {
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "text": body,
                            "parse_mode": "MarkdownV2",
                        },
                    )
                    self._status[chat_id] = (msg_id, note)
            except Exception:
                log.exception("indicate_pending failed for chat_id=%s", chat_id)

    async def indicate_idle(self) -> None:
        if not self.token or not self._status:
            return
        stale = list(self._status.items())
        self._status = {}
        for chat_id, (msg_id, _) in stale:
            try:
                self._api(
                    "deleteMessage", {"chat_id": chat_id, "message_id": msg_id}
                )
            except Exception:
                log.exception(
                    "indicate_idle: failed to delete status msg=%s chat=%s",
                    msg_id,
                    chat_id,
                )

    async def send(self, message: Message) -> str:
        if not self.token:
            raise RuntimeError("telegram token not set")
        if not message.to:
            raise RuntimeError("telegram send requires `to` (chat_id or @username)")
        # Clear the status message for this chat before the real reply, so
        # the chat doesn't end with a lingering italicized "using X".
        try:
            chat_id_int = int(message.to)
        except (TypeError, ValueError):
            chat_id_int = None
        if chat_id_int is not None and chat_id_int in self._status:
            msg_id, _ = self._status.pop(chat_id_int)
            try:
                self._api(
                    "deleteMessage", {"chat_id": message.to, "message_id": msg_id}
                )
            except Exception:
                log.exception(
                    "failed to clear status message %s in chat %s",
                    msg_id,
                    message.to,
                )
        self._send_text(message.to, message.body)
        return f"sent to {message.to}"

    # --- helpers ---

    def _send_text(self, chat_id: str, body: str) -> None:
        """Try Markdown first so **bold**, `code`, fences render naturally.
        Fall back to plain text if telegram rejects the markup."""
        try:
            self._api(
                "sendMessage",
                {"chat_id": chat_id, "text": body, "parse_mode": "Markdown"},
            )
        except RuntimeError as e:
            log.warning("markdown send rejected (%s); retrying as plain", e)
            self._api("sendMessage", {"chat_id": chat_id, "text": body})

    def _is_allowed(self, update: dict[str, Any]) -> bool:
        msg = update.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        return chat_id is not None and chat_id in self.allowed_chat_ids

    def _fetch_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": self.poll_timeout}
        offset = self._load_offset()
        if offset is not None:
            params["offset"] = offset
        resp = self._api("getUpdates", params)
        return resp.get("result", []) or []

    def _advance_watermark(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        highest = max(int(u["update_id"]) for u in updates if "update_id" in u)
        self._save_offset(highest + 1)

    def _api(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{API_BASE}/bot{self.token}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.poll_timeout + 10) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"telegram {method} HTTP {e.code}: "
                f"{e.read().decode('utf-8', 'replace')}"
            ) from e
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} error: {body.get('description')}")
        return body

    def _load_offset(self) -> Optional[int]:
        raw = self._state.load().get("offset")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _save_offset(self, offset: int) -> None:
        self._state.save({"offset": int(offset)})


# MarkdownV2 requires these chars to be backslash-escaped in literal text.
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def _escape_md(text: str) -> str:
    return "".join("\\" + c if c in _MDV2_SPECIAL else c for c in text)
