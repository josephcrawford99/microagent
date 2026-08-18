"""TCP line-protocol channel. One line in = one envelope on `out`; envelopes
written to `in` are broadcast to every connected client. Useful for
`nc <host> <port>` smoke tests."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)


class Socket(Service):
    name = "socket"
    BODY_HINT = "<text broadcast to every connected client>"

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.host = str(config.get("host", "0.0.0.0"))
        self.port = int(config.get("port", 8765))
        self._clients: set[asyncio.StreamWriter] = set()

    async def start(self, *, poll: bool = True) -> None:
        await super().start(poll=poll)
        await asyncio.start_server(self._on_client, self.host, self.port)
        log.info("socket listening on %s:%s", self.host, self.port)

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = "%s:%s" % (writer.get_extra_info("peername") or ("?", "?"))[:2]
        log.info("socket client connected: %s", addr)
        self._clients.add(writer)
        try:
            while line := await reader.readline():
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self.write_out(sender=addr, body=text)
        except Exception:
            log.exception("client read error")
        finally:
            self._clients.discard(writer)
            writer.close()
            log.info("socket client disconnected: %s", addr)

    async def handle_in(self, msg: Message) -> None:
        """Broadcast `body` to every connected client (`address` is ignored —
        a TCP line socket has no per-client addressing worth having)."""
        data = (msg.body + "\n").encode("utf-8")
        for writer in list(self._clients):
            try:
                writer.write(data)
                await writer.drain()
            except Exception:
                self._clients.discard(writer)
                writer.close()
