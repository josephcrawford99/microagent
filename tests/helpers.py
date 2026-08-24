"""Small utilities for poking the handle tree and the dashboard over HTTP."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
from typing import Any

from lib.handle import Handle
from lib.message import ADDRESS, Message


def write_in(handle: Handle, envelope: dict[str, Any]) -> bool:
    """Deliver one envelope to a started service's `in` FIFO."""
    return handle.write("in", Message.from_wire(handle.name, envelope, ADDRESS))


def write_status(handle: Handle, address: str, text: str) -> bool:
    """Put one line on a started service's `status` FIFO."""
    return handle.write("status", Message(handle.name, address, text))


def write_raw(handle: Handle, which: str, data: bytes) -> None:
    """Write raw bytes to a FIFO, like an external `echo >` would."""
    fd = os.open(handle.path / which, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


async def next_line(q: asyncio.Queue, timeout: float = 5.0) -> Message:
    return await asyncio.wait_for(q.get(), timeout)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_request(
    server,
    method: str,
    path: str,
    body: dict | str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One request against a live DashboardServer; returns (status, headers,
    body). A dict body is sent as JSON, a str body as-is (form posts)."""
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body).encode() if isinstance(body, dict) else (
            body.encode() if isinstance(body, str) else None
        )
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()
