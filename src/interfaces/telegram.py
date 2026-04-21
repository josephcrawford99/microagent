import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

from lib.interface import Interface, Message, Trigger

log = logging.getLogger("microagent.telegram")

API_BASE = "https://api.telegram.org"

# Emoji used to acknowledge receipt of an inbound message. Telegram only
# accepts a small set of reaction emojis for bots; 👀 is in the default set.
ACK_REACTION = "👀"


@dataclass
class TelegramMessage(Message):
    """Telegram payload. `to` is a numeric chat_id stringified (e.g. "12345")
    or an @username. `sender` is the chat_id the message came from."""

    SCHEMA: ClassVar[dict[str, type]] = {"to": str, "body": str}


class Telegram(Interface):
    """Telegram bot interface — HTTP-only, no extra deps.

    Receive uses `getUpdates` with timeout=0 so it returns immediately when
    nothing is pending, fitting the main loop's 3s cadence. Watermark is the
    highest `update_id + 1` we've consumed, persisted to `state_path`.

    `allowed_chat_ids` is the cost-guard — only messages from whitelisted
    chats wake the agent. An empty list denies everything; set it via the
    dashboard overlay after you know your own chat_id.

    Live status: `indicate_pending` posts a status message in the chat and
    edits it in place as the agent moves through thinking/tool-use states.
    The first real `send()` deletes the status message before posting the
    actual reply, so the user never sees a stale "working…" linger.
    """

    name = "telegram"
    message_class = TelegramMessage

    def __init__(
        self,
        token_env: str = "TELEGRAM_BOT_TOKEN",
        state_path: str = "/data/telegram_state.json",
        allowed_chat_ids: Optional[list[int]] = None,
        poll_timeout: int = 0,
    ) -> None:
        self.token = os.environ.get(token_env, "")
        self.state_path = state_path
        self.allowed_chat_ids = set(allowed_chat_ids or [])
        self.poll_timeout = poll_timeout
        self._pending: list[dict[str, Any]] = []
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
            (u.get("message") or {}).get("chat", {}).get("id")
            for u in allowed
        }
        self._active_chats.discard(None)
        # Fresh wake — any stale status message id is discarded, since a
        # new reply cycle starts its own status message.
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
            msg_id = msg.get("message_id")
            # 👀 reaction as an explicit "seen and queued" ack beyond the ✓✓.
            if chat_id is not None and msg_id is not None:
                self._react(chat_id, msg_id, ACK_REACTION)
            out.append(TelegramMessage(
                body=text,
                to="me",
                sender=str(chat_id) if chat_id is not None else "",
            ))
        self._advance_watermark(getattr(self, "_all_fetched", updates))
        return out

    async def indicate_pending(self, note: str) -> None:
        """Live status. Shows the real agent activity (thinking / using X)
        instead of a generic 'working…' placeholder, mirroring the dashboard."""
        if not self.token:
            return
        body = f"_{_escape_md(note)}_"
        for chat_id in self._active_chats:
            try:
                # Always fire typing so the ••• shows instantly too.
                self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                existing = self._status.get(chat_id)
                if existing is None:
                    resp = self._api("sendMessage", {
                        "chat_id": chat_id,
                        "text": body,
                        "parse_mode": "MarkdownV2",
                    })
                    msg_id = (resp.get("result") or {}).get("message_id")
                    if msg_id:
                        self._status[chat_id] = (int(msg_id), note)
                else:
                    msg_id, last_note = existing
                    if last_note == note:
                        continue  # no-op edit; Telegram rejects identical edits
                    self._api("editMessageText", {
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": body,
                        "parse_mode": "MarkdownV2",
                    })
                    self._status[chat_id] = (msg_id, note)
            except Exception:
                log.exception("indicate_pending failed for chat_id=%s", chat_id)

    async def send(self, message: Message) -> str:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        if not message.to:
            raise RuntimeError("telegram send requires `to` (chat_id or @username)")
        # Clear the status message for this chat before the real reply lands,
        # so the chat doesn't end with a lingering italicized "using X".
        try:
            chat_id_int = int(message.to)
        except (TypeError, ValueError):
            chat_id_int = None
        if chat_id_int is not None and chat_id_int in self._status:
            msg_id, _ = self._status.pop(chat_id_int)
            try:
                self._api("deleteMessage", {
                    "chat_id": message.to,
                    "message_id": msg_id,
                })
            except Exception:
                log.exception("failed to clear status message %s in chat %s",
                              msg_id, message.to)
        self._send_text(message.to, message.body)
        return f"sent to {message.to}"

    # --- helpers ---

    def _send_text(self, chat_id: str, body: str) -> None:
        """Send a text message. Tries Markdown first so agent replies with
        **bold**, _italic_, `code`, ```fenced``` render naturally. Falls back
        to plain text if Telegram rejects the markup (e.g. an unclosed `*`)."""
        try:
            self._api("sendMessage", {
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "Markdown",
            })
        except RuntimeError as e:
            log.warning("markdown send rejected (%s); retrying as plain", e)
            self._api("sendMessage", {"chat_id": chat_id, "text": body})

    def _react(self, chat_id: int, message_id: int, emoji: str) -> None:
        try:
            self._api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            })
        except Exception:
            log.exception("setMessageReaction failed on chat=%s msg=%s",
                          chat_id, message_id)

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
            raise RuntimeError(f"telegram {method} HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from e
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} error: {body.get('description')}")
        return body

    def _load_offset(self) -> Optional[int]:
        try:
            with open(self.state_path) as f:
                return int(json.load(f).get("offset"))
        except (FileNotFoundError, TypeError, ValueError):
            return None

    def _save_offset(self, offset: int) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"offset": int(offset)}, f)
        os.rename(tmp, self.state_path)


# MarkdownV2 requires these chars to be backslash-escaped in literal text.
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def _escape_md(text: str) -> str:
    return "".join("\\" + c if c in _MDV2_SPECIAL else c for c in text)
