"""Claude via the claude-code CLI — one `claude -p` subprocess per wake.

The CLI keeps its full toolset (bash, file edits) with the home's space/ as
cwd, so the
model can block, make files, take notes — a human as an API. To the harness
it's still text in -> text out; the JSON message contract is just part of the
prompt.

Session continuity: the CLI's `--resume` carries the transcript between
wakes; we store only the session id, rotating to a fresh session once per
day after `rotation_time`. State: state/<agent_id>/claude.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time
from typing import Any

from lib.paths import SPACE_DIR
from providers.base_provider import Provider

log = logging.getLogger(__name__)


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
            "--output-format", "json",
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
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude CLI timed out after {self.timeout:.0f}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exit {proc.returncode}: {stderr.decode()[:500]}"
            )
        out = json.loads(stdout)
        log.info(
            "claude wake done | turns=%s cost=$%s duration=%sms",
            out.get("num_turns"), out.get("total_cost_usd"), out.get("duration_ms"),
        )
        return str(out.get("result", "")), out.get("session_id")

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
