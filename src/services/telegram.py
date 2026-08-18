"""Telegram bot channel. HTTP-only, no extra deps."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class Telegram(Service):
    name = "telegram"
    REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN",)
    ADDRESS_HINT = "<chat id>"
    # Server-side long-poll: `getUpdates?timeout=N` blocks on the API waiting
    # for updates — near-zero traffic idle, ~RTT latency live.
    poll_interval_s = 0.0

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.token = self.secrets["TELEGRAM_BOT_TOKEN"]
        self.allowed_chat_ids = set(config.get("allowed_chat_ids", []))
        # getUpdates long-poll timeout. 30s keeps traffic near-zero while idle.
        self.poll_timeout = int(config.get("poll_timeout", 30))

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
        await asyncio.to_thread(self._send_text, msg.address, msg.body)

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
