"""The handle tree. Everything is a file(tm).

Each service owns a directory under RUN_DIR (`run/<name>/` in the home):
`in` and `out` FIFOs plus an append-only `log` mirror. The wire key is
fixed by direction: `in` lines carry `address`, `out` lines carry `from`.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat as stat_mod
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, cast
from asyncio import get_running_loop

from lib.message import ADDRESS, SENDER, Message
from lib.paths import RUN_DIR, write_atomic

log = logging.getLogger(__name__)

READ_CHUNK = 65536
WIRE_KEY = {"in": ADDRESS, "out": SENDER}
TAIL_LINES = 20


def parse_line(raw: bytes | str) -> dict[str, str]:
    """One line is one envelope: a flat str-to-str dict. Non-JSON lines become
    {"body": <line>}; non-string JSON values are re-dumped as JSON text."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    try:
        obj: object = json.loads(text)
    except json.JSONDecodeError:
        return {"body": text}
    if not isinstance(obj, dict):
        return {"body": text}
    items = cast(dict[str, object], obj).items()  # JSON object keys are always str
    return {k: v if isinstance(v, str) else json.dumps(v) for k, v in items}


def _encode(msg: dict[str, str]) -> bytes:
    """Dump one wire line, stamping a ts if the writer didn't supply one."""
    if "ts" not in msg:
        msg = {**msg, "ts": datetime.now().isoformat(timespec="seconds")}
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class Handle:
    """One handle dir. A second Handle on the same dir attaches
    to the existing FIFOs
    """

    def __init__(self, name: str, root: Path = RUN_DIR) -> None:
        self.name = name
        self.path = root / name
        self.in_path = self.path / "in"
        self.out_path = self.path / "out"
        self.log_path = self.path / "log"
        self.path.mkdir(parents=True, exist_ok=True)
        self.path.chmod(0o700)
        for fifo in (self.in_path, self.out_path):
            if not fifo.exists():
                os.mkfifo(fifo, 0o600)

    def add_reader(
        self, which: Literal["in", "out"], callback: Callable[[Message], None]
    ) -> int:
        """Register a per-line callback on the event loop for one FIFO.
        Opens as the sole reader (O_RDWR keeps the FIFO from ever hitting EOF).
        Buffers partial lines; each complete line is parsed to a Message.
        Reads from `in` are mirrored to the log, which catches external
        writers that bypass write(); `out` lines were logged at write time."""
        loop = get_running_loop()
        tee = which == "in"
        fd = os.open(self.path / which, os.O_RDWR | os.O_NONBLOCK)
        buf = bytearray()

        def on_readable() -> None:
            while True:
                try:
                    chunk = os.read(fd, READ_CHUNK)
                except BlockingIOError:
                    break
                if not chunk:  # unreachable with O_RDWR, but don't spin
                    break
                buf.extend(chunk)
            while (i := buf.find(b"\n")) >= 0:
                line = bytes(buf[:i])
                del buf[: i + 1]
                if not line.strip():
                    continue
                envelope = parse_line(line)
                if tee:
                    self.append_log(envelope)
                try:
                    callback(Message.from_wire(self.name, envelope, WIRE_KEY[which]))
                except Exception:
                    log.exception("%s/%s: reader callback failed", self.name, which)

        loop.add_reader(fd, on_readable)
        return fd

    def write(self, which: Literal["in", "out"], msg: Message) -> bool:
        """Send one message down a FIFO. Writes to `out` are mirrored to the
        log, kept even when no reader takes the FIFO write; `in` lines are
        logged by the sole reader instead."""
        data = _encode(msg.to_wire(WIRE_KEY[which]))
        ok = self._write_fifo(which, data)
        if which == "out":
            with self.log_path.open("ab") as f:
                f.write(data)
        return ok

    def _write_fifo(self, which: str, data: bytes) -> bool:
        try:
            fd = os.open(self.path / which, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENXIO:
                log.warning("%s/%s: no reader, dropping line", self.name, which)
                return False
            raise
        try:
            for _ in range(3):
                try:
                    os.write(fd, data)
                    return True
                except BlockingIOError:
                    time.sleep(0.05)
            log.error("%s/%s: pipe full, dropping line", self.name, which)
            return False
        finally:
            os.close(fd)

    def append_log(self, msg: dict[str, str]) -> None:
        with self.log_path.open("ab") as f:
            f.write(_encode(msg))

    def read_log(self) -> list[dict[str, str]]:
        """Every line in the log file, parsed to envelopes."""
        if not self.log_path.exists():
            return []
        return [
            parse_line(line)
            for line in self.log_path.read_bytes().splitlines()
            if line.strip()
        ]

    def snapshot(self, name: str, text: str) -> None:
        """Atomically rewrite a /proc-style status file (e.g. cron/schedules)."""
        write_atomic(self.path / name, text)


def describe_tree() -> list[dict[str, Any]]:
    """Walk the handle tree: every dir, every handle's type and size, plus a
    tail of each regular file."""
    out: list[dict[str, Any]] = []
    if not RUN_DIR.is_dir():
        return out
    for d in sorted(p for p in RUN_DIR.iterdir() if p.is_dir()):
        entry: dict[str, Any] = {"name": d.name, "handles": []}
        for f in sorted(d.iterdir()):
            st = f.lstat()
            kind = "fifo" if stat_mod.S_ISFIFO(st.st_mode) else "file"
            row: dict[str, Any] = {"name": f.name, "type": kind, "size": st.st_size}
            if kind == "file" and st.st_size:
                row["tail"] = _tail(f, st.st_size)
            entry["handles"].append(row)
        out.append(entry)
    return out


def _tail(path: Path, size: int, max_bytes: int = 4096) -> list[str]:
    with path.open("rb") as f:
        f.seek(max(0, size - max_bytes))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # started mid-line
    return lines[-TAIL_LINES:]
