"""bash.run — shell access, for agent types that don't bring their own.

The Claude type gets the full Claude Code toolset (Bash, Read, Write, …) for
free, so it has no reason to load this. Gemini and anything else hand-rolled
does: without it the agent can only talk, not act. Runs in /space, the agent's
own scratch dir, with a hard timeout so a hung command can't wedge the wake
loop — the daemon is single-threaded and nothing else runs until on_wake
returns.
"""

from __future__ import annotations

import asyncio
import logging

from lib.paths import SPACE_DIR
from lib.tools import ToolArgs, ToolContext, ToolResult, ToolSpec, _error, _ok, tool

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600
MAX_OUTPUT = 20_000


def build(ctx: ToolContext) -> list[ToolSpec]:
    del ctx

    @tool(
        "run",
        f"Run a shell command. Working directory is {SPACE_DIR} (your own "
        f"space) unless you cd elsewhere. Returns combined stdout/stderr, "
        f"truncated at {MAX_OUTPUT} characters, with the exit code. Default "
        f"timeout {DEFAULT_TIMEOUT_S}s — raise it for anything slow rather "
        f"than backgrounding, since a killed command leaves no output.",
        {"command": str, "timeout": int},
    )
    async def run(args: ToolArgs) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return _error("run: 'command' is required")
        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            return _error("run: 'timeout' must be an integer number of seconds")
        timeout = max(1, min(timeout, MAX_TIMEOUT_S))

        SPACE_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(SPACE_DIR),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("bash.run timed out after %ss: %s", timeout, command)
            return _error(f"run: timed out after {timeout}s; no output captured")

        text = out.decode("utf-8", "replace")
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n… (truncated at {MAX_OUTPUT} chars)"
        body = f"exit {proc.returncode}\n{text}".rstrip()
        return _ok(body) if proc.returncode == 0 else _error(body)

    return [run]
