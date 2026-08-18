"""Web-chat channel — the purest demo of the architecture. This service does
nothing but own its handle dir: the dashboard plays the remote user, writing
user lines to `web_chat/out` (and mirroring them into `log`) exactly like an
external `echo` would, and rendering the conversation by tailing `log`.
Agent replies arrive on `in`, and delivery *is* the log append the Service
base already does."""

from __future__ import annotations

from lib.message import Message
from services.base_service import Service


class WebChat(Service):
    name = "web_chat"

    async def handle_in(self, msg: Message) -> None:
        pass  # the base's log append is the delivery; the dashboard tails it
