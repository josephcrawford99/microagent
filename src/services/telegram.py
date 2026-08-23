"""Telegram bot channel. HTTP-only, no extra deps."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram drops the "typing" state after ~5s, so it needs re-asserting while
# a long tool call runs; edits to a message are rate limited, so they don't.
TYPING_INTERVAL_S = 4.0
STATUS_INTERVAL_S = 3.0
MARKUP = re.compile(r"[_*`\[\]]")  # a tool argument is full of these


@dataclass
class Status:
    """The live indicator in one chat: the task holding the typing state, and
    the message being edited in place once it has been posted."""

    typing: asyncio.Task[None]
    message_id: int | None = None
    text: str = ""
    at: float = 0.0


def _italic(text: str) -> str:
    """One status line as Markdown italic. Markup the line didn't mean is
    blanked rather than escaped: telegram rejects the whole message over one
    stray `_`, and a status is not worth a retry."""
    return f"_{MARKUP.sub(' ', text).strip()}_"


class Telegram(Service):
    name = "telegram"
    REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN",)
    ADDRESS_HINT = "<chat id>"
    # No poll_interval_s override: the inherited 0.0 is right. `getUpdates`
    # long-polls, so the fetch itself blocks on the API waiting for updates —
    # near-zero traffic idle, ~RTT latency live. Sleeping after it would only
    # add latency. `poll_timeout` below sets how long the server holds it.

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.token = self.secrets["TELEGRAM_BOT_TOKEN"]
        self.allowed_chat_ids = set(config.get("allowed_chat_ids", []))
        # getUpdates long-poll timeout. 30s keeps traffic near-zero while idle.
        self.poll_timeout = int(config.get("poll_timeout", 30))
        # The live "working on it" message per chat, while one is up. Drawing
        # and taking down are serialized: a reply arriving while the indicator
        # is still being posted would otherwise strand it in the chat.
        self.status: dict[str, Status] = {}
        self.drawing = asyncio.Lock()

    async def poll(self) -> None:
        updates = await asyncio.to_thread(self._fetch_updates)
        # Blocked chats count against the watermark too, or we'd loop
        # forever on the same disallowed update_id.
        self._advance_watermark(updates)
        for u in updates:
            msg: dict[str, Any] = u.get("message") or {}
            chat: dict[str, Any] = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = msg.get("text") or ""
            if chat_id not in self.allowed_chat_ids or not text:
                continue
            self.write_out(sender=str(chat_id), body=text)

    async def handle_in(self, msg: Message) -> None:
        if not self.token:
            raise RuntimeError("telegram token not set")
        if not msg.address:
            raise RuntimeError("telegram needs an `address` (a chat id)")
        # The reply is the answer the indicator was standing in for, so take
        # the indicator down first and let the reply land in its place.
        await self.clear_status(msg.address)
        await asyncio.to_thread(self._send_text, msg.address, msg.body)

    async def handle_status(self, msg: Message) -> None:
        """Keep the chat looking busy: the typing indicator, plus one italic
        message that follows what the agent is doing."""
        if not self.token or not msg.address:
            return
        if not msg.status:
            await self.clear_status(msg.address)
            return
        async with self.drawing:
            status = self.status.get(msg.address)
            if status is None:
                self.status[msg.address] = status = Status(
                    typing=self.spawn(self._keep_typing(msg.address))
                )
            await asyncio.to_thread(
                self._show_status, msg.address, status, msg.status
            )

    async def clear_status(self, chat_id: str) -> None:
        """Take down the indicator, if one is up. Never raises: a stale
        indicator is not worth failing a delivery over."""
        async with self.drawing:
            status = self.status.pop(chat_id, None)
            if status is None:
                return
            status.typing.cancel()
            if status.message_id is None:
                return
            try:
                await asyncio.to_thread(
                    self._api,
                    "deleteMessage",
                    {"chat_id": chat_id, "message_id": status.message_id},
                )
            except Exception as e:
                log.warning("could not clear status in %s: %s", chat_id, e)

    async def _keep_typing(self, chat_id: str) -> None:
        """Hold the "typing" state for as long as the agent is working."""
        while True:
            try:
                await asyncio.to_thread(
                    self._api, "sendChatAction",
                    {"chat_id": chat_id, "action": "typing"},
                )
            except Exception as e:
                log.warning("no typing action for %s: %s", chat_id, e)
            await asyncio.sleep(TYPING_INTERVAL_S)

    # --- helpers ---

    def _show_status(self, chat_id: str, status: Status, text: str) -> None:
        """Post the indicator, then edit that same message from then on.
        Telegram refuses a no-op edit and rate limits the rest, so a repeat or
        a line inside the interval is simply not drawn."""
        now = time.monotonic()
        if status.message_id is None:
            sent = self._api(
                "sendMessage",
                {"chat_id": chat_id, "text": _italic(text), "parse_mode": "Markdown"},
            )
            status.message_id = int(sent["result"]["message_id"])
        elif text == status.text or now - status.at < STATUS_INTERVAL_S:
            return
        else:
            self._api("editMessageText", {
                "chat_id": chat_id, "message_id": status.message_id,
                "text": _italic(text), "parse_mode": "Markdown",
            })
        status.text, status.at = text, now

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

    def _fetch_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": self.poll_timeout}
        offset = self.state.get_int("offset")
        if offset is not None:
            params["offset"] = offset
        return self._api("getUpdates", params).get("result", []) or []

    def _advance_watermark(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        highest = max(int(u["update_id"]) for u in updates if "update_id" in u)
        self.state.save({"offset": highest + 1})

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
