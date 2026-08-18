"""Step 4b: the socket service over a real TCP connection — a client line
becomes an `out` envelope, an `in` envelope is broadcast to every client.
"""

from __future__ import annotations

import asyncio
import socket

from services.socket import Socket

from helpers import free_port, next_line, write_in


def _connect(port: int) -> socket.socket:
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    c.settimeout(5)
    return c


def _recv_line(conn: socket.socket) -> str:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(1024)
        if not chunk:
            break
        buf += chunk
    return buf.decode().rstrip("\n")


async def test_client_line_reaches_out(harness):
    port = free_port()
    svc = Socket("t-sock", {"host": "127.0.0.1", "port": port})
    q = await harness(svc)

    with _connect(port) as conn:
        conn.sendall(b"hello harness\n")
        line = await next_line(q)
    assert line.body == "hello harness"
    assert line.address.startswith("127.0.0.1:")


async def test_in_envelope_broadcasts_to_all_clients(harness):
    port = free_port()
    svc = Socket("t-sock-b", {"host": "127.0.0.1", "port": port})
    q = await harness(svc)

    with _connect(port) as one, _connect(port) as two:
        # A line from each proves both clients are registered before we send.
        one.sendall(b"ping one\n")
        await next_line(q)
        two.sendall(b"ping two\n")
        await next_line(q)

        write_in(svc.handle, {"address": "ignored", "body": "hello everyone"})
        got_one = await asyncio.to_thread(_recv_line, one)
        got_two = await asyncio.to_thread(_recv_line, two)
    assert got_one == "hello everyone"
    assert got_two == "hello everyone"
