"""poll.poll_all — drain every enabled source in one call.

The usual start-of-wake move: you're told which sources triggered, but a
burst may have landed on others while you were busy. `poll_all` consumes
everything pending everywhere; the per-channel `poll` tools are there for when
you only want one.
"""

from __future__ import annotations

import logging
from typing import Any

from lib.tools import (
    ToolArgs,
    ToolContext,
    ToolResult,
    ToolSpec,
    _json,
    drain,
    tool,
)

log = logging.getLogger(__name__)


def build(ctx: ToolContext) -> list[ToolSpec]:
    sources = ctx.sources
    if not sources:
        return []

    names = ", ".join(s.name for s in sources)

    @tool(
        "poll_all",
        f"Read pending messages from every source at once ({names}). Consumes "
        f"them — they will not be returned again. Returns JSON: an object "
        f"keyed by source name, each holding an array of message objects. "
        f"Sources with nothing pending are omitted.",
        {},
    )
    async def poll_all(args: ToolArgs) -> ToolResult:
        del args
        out: dict[str, Any] = {}
        for src in sources:
            try:
                messages = await drain(src)
            except Exception as e:
                log.exception("poll_all: %s receive failed", src.name)
                out[src.name] = {"error": f"{type(e).__name__}: {e}"}
                continue
            if messages:
                out[src.name] = messages
        return _json(out)

    return [poll_all]
