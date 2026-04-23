"""TCP line-protocol interface. One line in = one Message; agent replies go
to every connected client. Useful for `nc <host> <port>` smoke tests."""

from __future__ import annotations

import logging
import queue
import socket
import threading

from lib.interface import Interface, Message
from lib.settings import SocketSettings

log = logging.getLogger("microagent.socket")


class Socket(Interface):
    name = "socket"

    def __init__(self, agent_id: str, settings: SocketSettings) -> None:
        super().__init__(agent_id)
        self.host = settings.host
        self.port = settings.port
        self._inbox: "queue.Queue[Message]" = queue.Queue()
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()

    async def start(self, trigger_q) -> None:
        await super().start(trigger_q)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(8)
        log.info("socket listening on %s:%s", self.host, self.port)
        while True:
            try:
                conn, addr = srv.accept()
            except Exception:
                log.exception("accept failed")
                continue
            with self._lock:
                self._clients.append(conn)
            threading.Thread(
                target=self._handle_client, args=(conn, addr), daemon=True
            ).start()

    def _handle_client(self, conn: socket.socket, addr) -> None:
        log.info("socket client connected: %s", addr)
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").rstrip("\r").strip()
                    if text:
                        self._inbox.put(Message(body=text, sender="user", to="agent"))
                        self._signal()
        except Exception:
            log.exception("client recv error")
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
            log.info("socket client disconnected: %s", addr)

    async def receive(self) -> list[Message]:
        out: list[Message] = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    async def send(self, message: Message) -> str:
        data = (message.body + "\n").encode("utf-8")
        dead: list[socket.socket] = []
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c.sendall(data)
            except Exception:
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)
                    try:
                        c.close()
                    except Exception:
                        pass
        return f"sent to {len(clients) - len(dead)} client(s)"
