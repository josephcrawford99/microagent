"""Web-chat interface. Tiny shim between the agent and whatever HTTP view
is reading it (the dashboard, today). Knows nothing about HTTP itself —
the dashboard calls `submit()` / `get_log()` / `get_pending()` directly."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

from lib.interface import Interface, Message, Trigger
from lib.settings import WebChatSettings

log = logging.getLogger("microagent.web_chat")


class WebChat(Interface):
    name = "web_chat"

    def __init__(self, agent_id: str, settings: WebChatSettings) -> None:
        super().__init__(agent_id)
        del settings  # reserved for future knobs
        # Agent-facing inbox (drained by receive()).
        self._inbox: "queue.Queue[Message]" = queue.Queue()
        # UI-facing chat log (user + agent messages, the view polls for updates).
        self._lock = threading.Lock()
        self._log: list[dict] = []
        self._next_id = 1
        # Transient "agent is thinking / using X" indicator. Cleared by next
        # real send() or by indicate_idle. Monotonic pending_id lets the UI
        # detect clears even if the text happens to repeat.
        self._pending: Optional[str] = None
        self._pending_id = 0

    # --- Interface contract ---

    def trigger_wake(self) -> Optional[Trigger]:
        if self._inbox.empty():
            return None
        return Trigger(interface=self)

    async def receive(self) -> list[Message]:
        out: list[Message] = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    async def send(self, message: Message) -> str:
        with self._lock:
            self._pending = None
            self._pending_id += 1
        self._append("agent", message.body or "")
        return "delivered"

    async def indicate_pending(self, note: str) -> None:
        with self._lock:
            self._pending = note
            self._pending_id += 1

    async def indicate_idle(self) -> None:
        with self._lock:
            self._pending = None
            self._pending_id += 1

    # --- view-facing helpers (dashboard calls these) ---

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._append("user", text)
        self._inbox.put(Message(body=text, sender="web", to="agent"))

    def get_log(self, after: int) -> dict:
        with self._lock:
            msgs = [m for m in self._log if m["id"] > after]
            latest = self._next_id - 1
            pending = {"note": self._pending, "id": self._pending_id}
        return {"messages": msgs, "latest": latest, "pending": pending}

    # --- internal ---

    def _append(self, role: str, body: str) -> None:
        with self._lock:
            self._log.append(
                {"id": self._next_id, "ts": time.time(), "role": role, "body": body}
            )
            self._next_id += 1
            if len(self._log) > 500:
                self._log = self._log[-500:]
