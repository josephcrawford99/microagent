"""Claude via the claude-code CLI — one `claude -p` subprocess per wake.

The CLI keeps its full toolset (bash, file edits) with the home's space/ as
cwd, so the
model can block, make files, take notes — a human as an API. To the harness
it's still text in -> text out; the JSON message contract is just part of the
prompt.

Session continuity: the CLI's `--resume` carries the transcript between
wakes; we store only the session id, rotating to a fresh session once per
day after `rotation_time`. State: state/<agent_id>/claude.json.

The CLI streams NDJSON rather than answering once, so every tool the model
picks up becomes a progress line on `on_status` while the wake runs, and the
final `result` event is the reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time
from typing import Any, cast

from lib.paths import SPACE_DIR
from providers.base_provider import Provider

log = logging.getLogger(__name__)

READ_CHUNK = 65536
STATUS_CHARS = 80  # a progress line is a glance, not a transcript
# The field of a tool's input worth showing, most telling first. Anything
# unlisted falls back to the first string the tool was given.
ARGUMENT_FIELDS = ("command", "file_path", "path", "pattern", "url", "prompt",
                   "description", "query")


def tool_lines(event: dict[str, Any]) -> list[str]:
    """One `Name: argument` line per tool the model just reached for."""
    out: list[str] = []
    for raw in _blocks(_fields(event.get("message")).get("content")):
        block = _fields(raw)
        if block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "?")
        out.append(f"{name}: {_argument(block.get('input'))}"[:STATUS_CHARS])
    return out


def _fields(obj: Any) -> dict[str, Any]:
    """One JSON object out of the stream, or an empty one if it isn't."""
    return cast(dict[str, Any], obj) if isinstance(obj, dict) else {}


def _blocks(obj: Any) -> list[Any]:
    """One JSON array out of the stream, or an empty one if it isn't."""
    return cast(list[Any], obj) if isinstance(obj, list) else []


async def _read_all(stream: asyncio.StreamReader | None) -> bytes:
    """Everything a pipe has to say, to EOF."""
    return await stream.read() if stream is not None else b""


def _event(line: bytes) -> dict[str, Any]:
    """One NDJSON line as an event; empty for anything unreadable."""
    try:
        return _fields(json.loads(line))
    except json.JSONDecodeError:
        return {}


def _argument(tool_input: Any) -> str:
    """The one field that says what a tool call is about."""
    fields = _fields(tool_input)
    for key in ARGUMENT_FIELDS:
        if isinstance(fields.get(key), str) and fields[key]:
            return " ".join(str(fields[key]).split())
    first = next((v for v in fields.values() if isinstance(v, str) and v), "")
    return " ".join(str(first).split())


class Claude(Provider):
    name = "claude"
    REQUIRED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN",)
    has_filesystem = True  # the CLI brings bash + file edits, cwd = space/

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.rotation_time = _parse_hhmm(str(config.get("rotation_time", "03:00")))
        self.timeout = float(config.get("request_timeout", 600))

    async def generate(self, prompt: str) -> str:
        SPACE_DIR.mkdir(parents=True, exist_ok=True)  # the CLI needs a real cwd
        state = self.state.load()
        session = state.get("session_id")
        rotated = self._should_rotate(state)
        if rotated:
            session = None

        try:
            result, new_session = await self._query(prompt, session)
        except Exception:
            if session:
                # A stale/corrupt --resume is the usual culprit: drop the
                # session and retry once fresh before giving up.
                log.exception("claude failed with resume=%s; retrying fresh", session)
                session = None
                result, new_session = await self._query(prompt, None)
            else:
                raise

        # A missing last_rotation initializes to today — otherwise a
        # brand-new session would be "rotated" away on the very next wake.
        self.state.save({
            "session_id": new_session or session,
            "last_rotation": (
                date.today().isoformat()
                if rotated or not state.get("last_rotation")
                else state["last_rotation"]
            ),
        })
        return result

    async def _query(
        self, prompt: str, session: str | None
    ) -> tuple[str, str | None]:
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
        ]
        if session:
            cmd += ["--resume", session]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(SPACE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # stderr has to drain alongside stdout or a chatty CLI fills its pipe
        # and both ends wait on each other forever.
        errors = asyncio.get_running_loop().create_task(_read_all(proc.stderr))
        try:
            out = await asyncio.wait_for(
                self._stream(proc.stdout), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            errors.cancel()
            raise RuntimeError(f"claude CLI timed out after {self.timeout:.0f}s")
        await proc.wait()
        stderr = await errors
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exit {proc.returncode}: {stderr.decode()[:500]}"
            )
        if not out:
            raise RuntimeError("claude CLI ended without a result")
        log.info(
            "claude wake done | turns=%s cost=$%s duration=%sms",
            out.get("num_turns"), out.get("total_cost_usd"), out.get("duration_ms"),
        )
        return str(out.get("result", "")), out.get("session_id")

    async def _stream(self, stdout: asyncio.StreamReader | None) -> dict[str, Any]:
        """Drain the CLI's NDJSON, reporting each tool call as it happens, and
        return the `result` event that closes the run. Read in chunks rather
        than by line: one event carrying a big tool result runs past any line
        limit a stream reader would impose."""
        if stdout is None:
            return {}
        result: dict[str, Any] = {}
        buf = bytearray()
        while True:
            chunk = await stdout.read(READ_CHUNK)
            buf.extend(chunk)
            if not chunk:  # EOF, so whatever is left is the last line
                buf.extend(b"\n")
            while (i := buf.find(b"\n")) >= 0:
                line = bytes(buf[:i])
                del buf[: i + 1]
                event = _event(line)
                if event.get("type") == "result":
                    result = event
                elif event.get("type") == "assistant":
                    for text in tool_lines(event):
                        self.on_status(text)
            if not chunk:
                return result

    def _should_rotate(self, state: dict[str, Any]) -> bool:
        """Fresh session on the first wake past rotation_time each day."""
        if not state.get("session_id"):
            return False
        if state.get("last_rotation") == date.today().isoformat():
            return False
        return datetime.now().time() >= self.rotation_time


def _parse_hhmm(raw: str) -> time:
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except ValueError:
        log.warning("bad rotation_time %r; using 03:00", raw)
        return time(3, 0)
